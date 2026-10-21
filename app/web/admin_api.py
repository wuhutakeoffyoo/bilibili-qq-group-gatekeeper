"""Authenticated management on the application's existing ASGI listener."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import ipaddress
import json
import secrets
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.config import ConfigVersionConflict, GroupConfig, load_review_stages_from_yaml_file, redact_secrets
from app.database.async_storage import run_storage
from app.database import legacy
from app.database.leaves import LeaveRecordManager
from app.database.legacy import database_connection
from app.database.records import JoinRequestRecordManager
from app.database.repository import AdminRecordRepository, ConfigRepository, RuntimeStateRepository
from src.gatekeeper.decision_runtime import identity_decision_keys

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1024 * 1024
SESSION_COOKIE = "gatekeeper_session"


def _json_safe(value: Any) -> Any:
    """Preserve 64-bit IDs across JavaScript editors without rounding them."""
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _bounded_data(value: Any) -> None:
    """Reject cyclic/expanding YAML aliases and excessively nested payloads."""
    pending, seen, count = [(value, 0)], set(), 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 32:
            raise ValueError("配置内容过于复杂")
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                raise ValueError("配置不支持重复引用或循环YAML别名")
            seen.add(id(node))
            values = node.values() if isinstance(node, dict) else node
            pending.extend((item, depth + 1) for item in values)


def _loopback(host: str) -> bool:
    if str(host).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _pipeline(group) -> dict[str, Any]:
    groups = []
    for stage in sorted(group.review_stages, key=lambda item: item.index):
        item = {"id": f"stage_{stage.index}", "name": stage.name}
        if stage.when:
            item["when"] = stage.when
        else:
            item.update(conditions=stage.conditions, mode=stage.mode)
        for branch in ("on_true", "on_false", "on_unknown"):
            actions = getattr(stage, branch)
            flow = next(action for action in actions if action in {"next", "approve", "reject", "ignore"})
            value: dict[str, Any] = {"flow": flow}
            effects = [action for action in actions if action != flow]
            target, reason = getattr(stage, branch + "_target"), getattr(stage, branch + "_reason")
            if effects:
                value["effects"] = effects
            if target is not None:
                value["goto"] = f"stage_{target}"
            if reason:
                value["reason"] = reason
            item[branch] = value
        groups.append(item)
    return {"version": 3, "entry": groups[0]["id"] if groups else "", "groups": groups}


def _parse_pipeline(text: str):
    # Reuse the same parser as first-import YAML without modifying live files.
    try:
        _bounded_data(yaml.safe_load(text))
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("审核管线YAML无效或过于复杂") from exc
    with tempfile.TemporaryDirectory(prefix="gatekeeper-pipeline-") as folder:
        path = Path(folder) / "pipeline.yaml"
        path.write_text(text, encoding="utf-8")
        return load_review_stages_from_yaml_file(path)


def _database_health() -> dict[str, Any]:
    with database_connection() as connection:
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "join_request_audits", "bili_bindings", "user_bindings", "leave_records",
        )}
        result = {"schema_version": connection.execute("PRAGMA user_version").fetchone()[0], "counts": counts}
    try:
        disk = shutil.disk_usage(legacy.DATABASE_FILE.resolve().parent)
        result["disk"] = {"state": "known", "free_bytes": disk.free, "total_bytes": disk.total,
                          "free_gib": round(disk.free / 1024**3, 2)}
    except OSError:
        result["disk"] = {"state": "unknown"}
    return result


def install_admin_routes(app, runtime) -> None:
    """Register the console, authenticated APIs and bounded in-memory sessions."""
    sessions: dict[str, dict[str, Any]] = {}
    failures: dict[str, list[float]] = defaultdict(list)
    qr_sessions: dict[str, dict[str, Any]] = {}

    def prune(cfg) -> None:
        now = time.monotonic()
        for key in list(sessions):
            if sessions[key]["expires"] <= now:
                sessions.pop(key, None)
        window = max(1, int(cfg.webui_login_window_seconds))
        for key in list(failures):
            kept = [attempt for attempt in failures[key] if now - attempt < window]
            if kept:
                failures[key] = kept
            else:
                failures.pop(key, None)
        for key in list(qr_sessions):
            if qr_sessions[key]["expires"] <= time.time():
                qr_sessions.pop(key, None)

    def cookie_name(cfg) -> str:
        return "__Host-" + SESSION_COOKIE if cfg.webui_require_https else SESSION_COOKIE

    def token_hash(cfg) -> str:
        return hashlib.sha256(cfg.webui_token.encode()).hexdigest()

    def session(request: Request) -> tuple[str, dict[str, Any]]:
        cfg = request.state.admin_config
        prune(cfg)
        identifier = request.cookies.get(cookie_name(cfg), "")
        value = sessions.get(identifier)
        if value is None or not hmac.compare_digest(value["token_hash"], token_hash(cfg)):
            raise HTTPException(401, "请先登录管理台")
        return identifier, value

    def authenticated(request: Request, *, mutate: bool = False) -> tuple[str, dict[str, Any]]:
        value = session(request)
        if mutate and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), value[1]["csrf"]):
            raise HTTPException(403, "CSRF 校验失败，请刷新管理台后重试")
        return value

    async def body(request: Request, *, limit: int = MAX_BODY) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "请求需要 application/json")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "请求内容过大")
        try:
            value = json.loads(data)
            _bounded_data(value)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise HTTPException(400, "请求不是有效的 JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "请求内容必须是 JSON 对象")
        return value

    def revision(data: dict[str, Any]) -> int:
        value = data.get("expected_version")
        if type(value) is not int or value < 1:
            raise HTTPException(422, "请提交当前 expected_version，防止覆盖其他管理员的修改")
        return value

    def identifier(value: str, label: str = "QQ或群号") -> str:
        if not value.isdecimal() or len(value) > 24:
            raise HTTPException(422, f"{label}必须是数字")
        return value

    def readonly_fields(candidate: dict[str, Any], previous: dict[str, Any]) -> None:
        # Trust/listener settings and the login token cannot be changed by an
        # already authenticated browser. They require a local config operation.
        for key, old in previous.items():
            if key.startswith("webui_") or key in {"onebot_access_token", "host", "port"}:
                if candidate.get(key) not in (old, "[redacted]"):
                    raise HTTPException(422, f"{key} 为管理台只读字段，请使用本地配置入口修改")

    async def replace(candidate: dict[str, Any], expected: int, actor: str = "web-admin") -> int:
        previous = await run_storage(runtime.config.get_public_config)
        readonly_fields(candidate, previous)
        try:
            return await run_storage(runtime.config.replace_config, candidate, expected_version=expected, actor=actor)
        except ConfigVersionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            # Pydantic exception text can echo credential input. Return only
            # the safe validation summary, never the original model payload.
            raise HTTPException(422, "配置校验失败，请检查条件、表达式、流转目标及字段类型") from exc

    async def component_call(component, name: str, *args, **kwargs):
        if component is None or not hasattr(component, name):
            raise HTTPException(503, "该服务尚未就绪，请检查应用运行状态")
        function = getattr(component, name)
        try:
            result = function(*args, **kwargs) if inspect.iscoroutinefunction(function) else await run_storage(function, *args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(422, "操作未能完成，请检查参数和当前状态") from exc
        except Exception as exc:
            raise HTTPException(502, "外部服务暂不可用，请稍后重试；已有配置已保留") from exc

    @app.middleware("http")
    async def admin_security(request: Request, call_next):
        path = request.url.path
        relevant = path in {"/", "/admin", "/admin/", "/custom.css"} or path.startswith(("/api/", "/admin/static/"))
        if not relevant:
            return await call_next(request)
        nonce = secrets.token_urlsafe(24)
        request.state.csp_nonce = nonce
        try:
            cfg = await run_storage(runtime.config.get_config)
            request.state.admin_config = cfg
            if not cfg.webui_enabled:
                response = JSONResponse({"detail": "管理台未启用"}, status_code=404)
            elif len(cfg.webui_token) < 32:
                response = JSONResponse({"detail": "管理台 Token 必须至少32字符，请通过本地配置入口设置"}, status_code=503)
            else:
                actual_host = str(getattr(runtime.settings, "host", cfg.webui_host))
                peer = request.client.host if request.client else ""
                proxy = bool(cfg.webui_trust_proxy_headers and _loopback(peer))
                forwarded = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
                https = request.url.scheme == "https" or (proxy and forwarded == "https")
                request.state.admin_https = https
                request.state.admin_peer = peer
                if proxy and request.headers.get("x-forwarded-for"):
                    forwarded_ip = request.headers["x-forwarded-for"].split(",")[-1].strip()
                    try:
                        ipaddress.ip_address(forwarded_ip)
                        request.state.admin_peer = forwarded_ip
                    except ValueError:
                        pass
                host_name = urlsplit("//" + request.headers.get("host", "")).hostname or ""
                origin = request.headers.get("origin")
                origin_ok = not origin or (
                    urlsplit(origin).scheme == ("https" if https else "http")
                    and urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()
                )
                if not _loopback(actual_host) or not _loopback(cfg.webui_host):
                    response = JSONResponse({"detail": "管理台仅允许本机监听，请通过HTTPS反向代理访问"}, status_code=503)
                elif cfg.webui_require_https and not https:
                    response = JSONResponse({"detail": "管理台需要HTTPS"}, status_code=426)
                elif not https and not proxy and (not _loopback(peer) or not _loopback(host_name)):
                    response = JSONResponse({"detail": "HTTP管理台仅允许本机访问"}, status_code=403)
                elif request.method not in {"GET", "HEAD", "OPTIONS"} and not origin_ok:
                    response = JSONResponse({"detail": "请求来源校验失败"}, status_code=403)
                elif int(request.headers.get("content-length", "0")) > MAX_BODY:
                    response = JSONResponse({"detail": "请求内容过大"}, status_code=413)
                else:
                    response = await call_next(request)
        except (TypeError, ValueError):
            response = JSONResponse({"detail": "请求头无效"}, status_code=400)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; "
            "style-src 'self' 'nonce-" + nonce + "'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        if getattr(request.state, "admin_https", False):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def console(request: Request):
        text = (STATIC / "admin.html").read_text(encoding="utf-8")
        return HTMLResponse(text.replace("__NONCE__", request.state.csp_nonce))

    @app.get("/custom.css", include_in_schema=False)
    async def custom_css(request: Request):
        authenticated(request)
        value = str(request.state.admin_config.webui_custom_css_file).strip()
        if not value:
            raise HTTPException(404, "未配置自定义样式")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(getattr(runtime.settings, "app_root", Path.cwd())) / path
        if not path.is_file() or path.stat().st_size > MAX_BODY:
            raise HTTPException(404, "自定义样式不存在或过大")
        return Response(await run_storage(path.read_bytes), media_type="text/css")

    @app.get("/api/session")
    async def get_session(request: Request):
        _, value = authenticated(request)
        return {"csrf": value["csrf"], "expires_in": max(0, int(value["expires"] - time.monotonic()))}

    @app.post("/api/login")
    async def login(request: Request):
        cfg = request.state.admin_config
        prune(cfg)
        peer = request.state.admin_peer
        if len(failures[peer]) >= max(1, int(cfg.webui_login_max_attempts)):
            return JSONResponse({"detail": "登录失败次数过多，请稍后重试"}, 429, headers={"Retry-After": str(cfg.webui_login_window_seconds)})
        data = await body(request, limit=4096)
        supplied = data.get("token", "")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), cfg.webui_token.encode()):
            if len(failures) >= 2048:
                failures.pop(next(iter(failures)))
            failures[peer].append(time.monotonic())
            raise HTTPException(401, "Token错误，请检查后重试")
        failures.pop(peer, None)
        if len(sessions) >= 256:
            raise HTTPException(429, "登录会话过多，请稍后重试")
        old = request.cookies.get(cookie_name(cfg), "")
        sessions.pop(old, None)
        key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[key] = {"expires": time.monotonic() + cfg.webui_session_timeout_seconds, "csrf": csrf, "token_hash": token_hash(cfg)}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(cookie_name(cfg), key, max_age=cfg.webui_session_timeout_seconds, httponly=True,
                            secure=bool(request.state.admin_https), samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        key, _ = authenticated(request, mutate=True)
        sessions.pop(key, None)
        for identifier in list(qr_sessions):
            if qr_sessions[identifier]["session"] == key:
                qr_sessions.pop(identifier, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name(request.state.admin_config), path="/", secure=request.state.admin_config.webui_require_https,
                               httponly=True, samesite="strict")
        return response

    @app.get("/api/health")
    async def health(request: Request):
        authenticated(request)
        coordinator = getattr(runtime.service, "bili_requests", None)
        current = request.state.admin_config
        targets = sorted({uid for group in current.groups.values() for uid in group.target_uids})
        checks = [{"uid": uid, "verified": coordinator.common_negative_is_definitive(current.bili_cookie, [uid])
                   if coordinator is not None else None} for uid in targets]
        return _json_safe({
            "started": bool(runtime.started), "database": await run_storage(_database_health),
            "config_version": await run_storage(runtime.config.get_version),
            "clients": [{"self_id": str(client.self_id), "connected": bool(client.connected)} for client in runtime.clients.values()],
            "pending_count": await runtime.service.decisions.storage.get_pending_decision_count(),
            "storage_paused": bool(runtime.service.decisions.paused),
            "credentials": redact_secrets(getattr(runtime.credentials, "last_status", {"state": "not_ready"})),
            "bilibili": {"metrics": coordinator.metrics() if coordinator is not None else {"state": "not_ready"},
                         "common_targets": checks, "all_targets_verified": all(item["verified"] is True for item in checks)},
        })

    @app.get("/api/config")
    async def config_get(request: Request):
        authenticated(request)
        return _json_safe(await run_storage(runtime.config.get_snapshot))

    @app.put("/api/config")
    async def config_put(request: Request):
        authenticated(request, mutate=True)
        data = await body(request)
        if not isinstance(data.get("config"), dict):
            raise HTTPException(422, "缺少config对象")
        version = await replace(data["config"], revision(data))
        return {"version": version, "config": _json_safe(await run_storage(runtime.config.get_public_config))}

    @app.get("/api/config/export")
    async def config_export(request: Request):
        authenticated(request)
        text = await run_storage(runtime.config.export_yaml, include_secrets=False)
        return Response(text, media_type="application/yaml", headers={"Content-Disposition": 'attachment; filename="gatekeeper-config.yaml"'})

    @app.post("/api/config/import")
    async def config_import(request: Request):
        authenticated(request, mutate=True)
        data = await body(request)
        try:
            payload = yaml.safe_load(str(data.get("yaml", "")))
            _bounded_data(payload)
        except (yaml.YAMLError, ValueError, RecursionError) as exc:
            raise HTTPException(422, "YAML格式无效") from exc
        if not isinstance(payload, dict) or payload.get("version") != 3 or not isinstance(payload.get("config"), dict):
            raise HTTPException(422, "导入文件需要 version: 3 和 config 对象")
        version = await replace(payload["config"], revision(data), actor="web-import")
        return {"version": version}

    @app.get("/api/config/versions")
    async def config_versions(request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        authenticated(request)
        return {"versions": await run_storage(runtime.config.list_versions, limit, offset)}

    @app.post("/api/config/restore")
    async def config_restore(request: Request):
        authenticated(request, mutate=True)
        data = await body(request)
        if type(data.get("version")) is not int:
            raise HTTPException(422, "请选择需要恢复的版本")
        target = await run_storage(ConfigRepository.get, data["version"])
        if target is None:
            raise HTTPException(422, "指定版本不存在")
        actual = await run_storage(runtime.config.get_config)
        readonly_fields(json.loads(target["config_json"]), actual.model_dump())
        try:
            version = await run_storage(runtime.config.restore_version, data["version"], expected_version=revision(data), actor="web-restore")
        except ConfigVersionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "指定版本不存在或无效") from exc
        return {"version": version}

    @app.get("/api/groups")
    async def groups_get(request: Request):
        authenticated(request)
        snapshot = await run_storage(runtime.config.get_snapshot)
        return {"version": snapshot["version"], "groups": _json_safe(snapshot["config"]["groups"])}

    @app.put("/api/groups/{group_id}")
    async def group_put(request: Request, group_id: str):
        authenticated(request, mutate=True)
        identifier(group_id, "群号")
        data = await body(request)
        raw = data.get("group")
        if not isinstance(raw, dict) or str(raw.get("group_id", "")) != group_id:
            raise HTTPException(422, "群配置和路径群号不一致")
        try:
            GroupConfig.model_validate(raw)
        except ValueError as exc:
            raise HTTPException(422, "群配置无效，请检查等级、审核表达式和动作") from exc
        current = await run_storage(runtime.config.get_public_config)
        current["groups"][group_id] = raw
        return {"version": await replace(current, revision(data), actor="web-group")}

    @app.delete("/api/groups/{group_id}")
    async def group_delete(request: Request, group_id: str):
        authenticated(request, mutate=True)
        data = await body(request)
        current = await run_storage(runtime.config.get_public_config)
        if current["groups"].pop(group_id, None) is None:
            raise HTTPException(404, "群配置不存在")
        return {"version": await replace(current, revision(data), actor="web-group-delete")}

    @app.get("/api/groups/{group_id}/pipeline")
    async def pipeline_get(request: Request, group_id: str):
        authenticated(request)
        group = await run_storage(runtime.config.get_group_config, group_id)
        if group is None:
            raise HTTPException(404, "群配置不存在")
        value = _pipeline(group)
        return {"version": await run_storage(runtime.config.get_version), "pipeline": value,
                "yaml": yaml.safe_dump(value, allow_unicode=True, sort_keys=False)}

    @app.put("/api/groups/{group_id}/pipeline")
    async def pipeline_put(request: Request, group_id: str):
        authenticated(request, mutate=True)
        data = await body(request)
        text = data.get("yaml")
        if text is None and isinstance(data.get("pipeline"), dict):
            text = yaml.safe_dump(data["pipeline"], allow_unicode=True, sort_keys=False)
        if not isinstance(text, str):
            raise HTTPException(422, "请提供审核管线YAML或pipeline对象")
        try:
            stages = await run_storage(_parse_pipeline, text)
        except ValueError as exc:
            raise HTTPException(422, "审核管线校验失败，请检查表达式、分组编号和跳转目标") from exc
        current = await run_storage(runtime.config.get_public_config)
        if group_id not in current["groups"]:
            raise HTTPException(404, "群配置不存在")
        current["groups"][group_id]["review_stages"] = [stage.model_dump() for stage in stages]
        return {"version": await replace(current, revision(data), actor="web-pipeline")}

    @app.post("/api/simulate")
    async def simulate(request: Request):
        authenticated(request, mutate=True)
        data = await body(request, limit=16384)
        group_id = identifier(str(data.get("group_id", "")), "群号")
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 2048:
            raise HTTPException(422, "请输入有效的B站UID或昵称")
        qq = str(data.get("qq", "")).strip() or None
        if qq:
            identifier(qq, "QQ")
        result = await component_call(runtime.service, "simulate", group_id, answer, qq=qq)
        return result if isinstance(result, (dict, list)) else {"result": str(result)}

    @app.get("/api/audits")
    async def audits(request: Request, qq: str = "", group_id: str = "",
                     bili_uid: int | None = Query(None, ge=1, le=2**63 - 1),
                     action_status: str = "", limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        authenticated(request)
        return {"items": _json_safe(await run_storage(AdminRecordRepository.audits, qq=qq, group_id=group_id, bili_uid=bili_uid,
                                                      action_status=action_status, limit=limit, offset=offset))}

    @app.get("/api/bindings")
    async def bindings(request: Request, qq: str = "", bili_uid: int | None = Query(None, ge=1, le=2**63 - 1),
                       limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        authenticated(request)
        return {"items": _json_safe(await run_storage(AdminRecordRepository.bindings, qq=qq, bili_uid=bili_uid, limit=limit, offset=offset))}

    @app.post("/api/bindings/{qq}/clear")
    async def binding_clear(request: Request, qq: str):
        authenticated(request, mutate=True)
        identifier(qq, "QQ")
        data = await body(request, limit=4096)
        if data.get("confirm_qq") != qq:
            raise HTTPException(422, "请输入同一QQ号确认解绑")
        decisions = runtime.service.decisions
        owned = await run_storage(JoinRequestRecordManager.get_owned_bili_uids, qq)
        keys = list(identity_decision_keys(qq, None)) + [f"identity:uid:{uid}" for uid in owned]
        async with decisions.locks.hold(*keys):
            if await decisions.has_pending_identity(qq) or any([await decisions.has_pending_identity("", uid) for uid in owned]):
                raise HTTPException(409, "存在相关待确认审批，请先核对审批结果后再解绑")
            removed = await run_storage(JoinRequestRecordManager.clear_identity_binding, qq)
        return {"removed_uids": _json_safe(removed)}

    @app.get("/api/leaves")
    async def leaves(request: Request, qq: str = "", group_id: str = "", limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0)):
        authenticated(request)
        return {"items": await run_storage(AdminRecordRepository.leaves, qq=qq, group_id=group_id, limit=limit, offset=offset)}

    @app.delete("/api/leaves/{group_id}/{qq}")
    async def leave_delete(request: Request, group_id: str, qq: str):
        authenticated(request, mutate=True)
        identifier(group_id, "群号")
        identifier(qq, "QQ")
        removed = await run_storage(LeaveRecordManager.remove_leave_record, group_id, qq)
        return {"removed": removed}

    @app.get("/api/pending")
    async def pending(request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        authenticated(request)
        items = await run_storage(AdminRecordRepository.audits, action_status="pending", limit=limit, offset=offset)
        for item in items:
            state = await run_storage(RuntimeStateRepository.get, "pending_decision", str(item["id"]), {})
            item["reconciliation"] = {key: state[key] for key in (
                "phase", "detail", "retry_count", "reconcile_attempts", "next_retry_at", "inspection_attempts", "next_inspection_at",
            ) if key in state}
        return {"items": _json_safe(items)}

    @app.post("/api/pending/{audit_id}/resolve")
    async def pending_resolve(request: Request, audit_id: int):
        authenticated(request, mutate=True)
        data = await body(request, limit=4096)
        if type(data.get("applied")) is not bool or data.get("confirmed") is not True:
            raise HTTPException(422, "请先核对QQ群的实际审批结果，再明确确认是否生效")
        result = await runtime.service.decisions.resolve_pending(audit_id, applied=data["applied"])
        if not result.found:
            raise HTTPException(404, "待确认审批不存在或已由其他操作处理")
        return {"found": result.found, "finalized": result.finalized, "remaining": result.remaining}

    @app.get("/api/credentials/status")
    async def credential_status(request: Request):
        authenticated(request)
        return redact_secrets(await component_call(runtime.credentials, "status"))

    @app.post("/api/credentials/qr")
    async def credential_qr(request: Request):
        key, _ = authenticated(request, mutate=True)
        result = await component_call(runtime.credentials, "start_qr_login")
        url = urlsplit(str(result.get("url", "")))
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not (host == "bilibili.com" or host.endswith(".bilibili.com")):
            raise HTTPException(502, "登录地址未通过校验，请稍后重试")
        identifier = result["session_id"]
        qr_sessions[identifier] = {"session": key, "url": result["url"], "expires": result["expires_at"]}
        return {**result, "image": f"/api/credentials/qr/{identifier}/image"}

    @app.get("/api/credentials/qr/{identifier}/image")
    async def credential_qr_image(request: Request, identifier: str):
        key, _ = authenticated(request)
        value = qr_sessions.get(identifier)
        if value is None or value["session"] != key or value["expires"] <= time.time():
            raise HTTPException(404, "二维码已过期，请重新生成")
        import io
        import qrcode
        from qrcode.image.svg import SvgPathImage

        stream = io.BytesIO()
        qrcode.make(value["url"], image_factory=SvgPathImage).save(stream)
        return Response(stream.getvalue(), media_type="image/svg+xml")

    @app.post("/api/credentials/qr/poll")
    async def credential_qr_poll(request: Request):
        key, _ = authenticated(request, mutate=True)
        data = await body(request, limit=4096)
        identifier = str(data.get("session_id", ""))
        if identifier not in qr_sessions or qr_sessions[identifier]["session"] != key:
            raise HTTPException(404, "二维码登录会话不存在")
        result = await component_call(runtime.credentials, "poll_qr_login", identifier)
        if result.get("state") in {"expired", "success"}:
            qr_sessions.pop(identifier, None)
        return redact_secrets(result)

    @app.post("/api/credentials/refresh")
    async def credential_refresh(request: Request):
        authenticated(request, mutate=True)
        return redact_secrets(await component_call(runtime.credentials, "refresh_if_due"))

    @app.post("/api/credentials/fingerprints")
    async def credential_fingerprints(request: Request):
        authenticated(request, mutate=True)
        return await component_call(runtime.credentials, "ensure_fingerprints")

    @app.get("/api/ownership")
    async def ownership(request: Request, limit: int = Query(100, ge=1, le=500)):
        authenticated(request)
        return {"status": redact_secrets(await component_call(runtime.ownership, "status")),
                "items": _json_safe(redact_secrets(await component_call(runtime.ownership, "list_challenges", limit=limit)))}

    @app.post("/api/ownership/{challenge_id}/revoke")
    async def ownership_revoke(request: Request, challenge_id: str):
        authenticated(request, mutate=True)
        return {"revoked": await component_call(runtime.ownership, "revoke", challenge_id, actor="web-admin")}

    @app.get("/api/patrol")
    async def patrol(request: Request, limit: int = Query(100, ge=1, le=500)):
        authenticated(request)
        return {"status": await component_call(runtime.patrol, "status"),
                "items": _json_safe(await component_call(runtime.patrol, "reports", limit=limit))}

    @app.post("/api/patrol/run")
    async def patrol_run(request: Request):
        authenticated(request, mutate=True)
        data = await body(request, limit=4096)
        group_id = identifier(str(data.get("group_id", "")), "群号")
        return await component_call(runtime.patrol, "run_group", group_id)

    @app.post("/api/patrol/{report_id}/act")
    async def patrol_action(request: Request, report_id: str):
        authenticated(request, mutate=True)
        data = await body(request, limit=4096)
        if data.get("action") not in {"remind", "kick"} or data.get("confirmed") is not True:
            raise HTTPException(422, "请选择提醒或移出，并确认执行")
        qq = str(data.get("qq", "")).strip() or None
        if qq:
            identifier(qq, "QQ")
        return await component_call(runtime.patrol, "act", report_id, data["action"], qq=qq)
