"""Authenticated WebUI for editing the review pipeline YAML."""

import ipaddress
import json
import logging
import secrets
import threading
import tempfile
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .config import PluginConfig, load_review_stages_from_yaml_file

logger = logging.getLogger("group_request_manager.webui")
SESSION_COOKIE = "gatekeeper_session"
MAX_LOGIN_BODY = 4096
MAX_PIPELINE_BODY = 1024 * 1024
PIPELINE_WRITE_LOCK = threading.Lock()

HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gatekeeper Flow Studio</title><style nonce="__NONCE__">
:root{--ink:#17332d;--paper:#f4f0e6;--accent:#df5b36;--line:#9bb8a8;--card:#fffdf7}*{box-sizing:border-box}
body{margin:0;color:var(--ink);background:radial-gradient(circle at 10% 10%,#f7ca72 0 7%,transparent 25%),linear-gradient(135deg,#dbe8d8,var(--paper) 55%);font-family:"Microsoft YaHei",sans-serif;min-height:100vh}
header{padding:36px clamp(20px,5vw,72px) 16px}h1{font-family:Georgia,serif;font-size:clamp(32px,6vw,68px);margin:0;letter-spacing:-2px}header p{max-width:680px;font-size:16px}
main{display:grid;grid-template-columns:minmax(300px,1fr) minmax(320px,1fr);gap:22px;padding:18px clamp(20px,5vw,72px) 60px}.panel{background:color-mix(in srgb,var(--card) 92%,transparent);border:1px solid #fff;box-shadow:0 18px 50px #49665a26;border-radius:22px;padding:22px}
textarea{width:100%;height:64vh;resize:vertical;border:0;border-radius:14px;background:#18352f;color:#eef6e9;padding:18px;font:14px/1.55 Consolas,monospace;outline:3px solid transparent}textarea:focus{outline-color:#f0b85a}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px}button{border:0;border-radius:999px;padding:11px 18px;background:var(--accent);color:white;font-weight:700;cursor:pointer}button.alt{background:#315d50}.status{margin-left:auto;font-size:13px}
.flow{display:flex;flex-direction:column;gap:12px}.node{position:relative;border-left:7px solid var(--accent);background:white;border-radius:12px;padding:15px 17px;box-shadow:0 7px 20px #17332d17;animation:rise .35s ease both;cursor:grab}.node:after{content:"↓";position:absolute;bottom:-20px;left:50%;color:var(--line)}.node:last-child:after{display:none}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.grid .wide{grid-column:1/-1}.node input,.node select{width:100%;border:1px solid #c7d7cd;border-radius:8px;padding:8px;background:#f7faf5}.node label{font-size:11px;color:#5c746d}.remove{float:right;background:#8e4434;padding:6px 10px}.hint{font-size:12px;color:#5c746d}@keyframes rise{from{opacity:0;transform:translateY(8px)}}
.login{width:min(430px,calc(100% - 32px));margin:12vh auto;background:var(--card);padding:30px;border-radius:20px;box-shadow:0 24px 70px #17332d35}.login h1{font-size:38px;letter-spacing:-1px}.login input{width:100%;padding:12px 14px;margin:18px 0 12px;border:1px solid var(--line);border-radius:10px}.login .error{color:#9d2f20;min-height:20px}[hidden]{display:none!important}
@media(max-width:850px){main{grid-template-columns:1fr}textarea{height:48vh}}
</style></head><body>
<section id="loginView" class="login"><h1>Flow Studio</h1><p>请输入 WebUI 管理 Token。凭据只用于本次登录，不会写入 URL 或浏览器存储。</p><input id="loginToken" type="password" autocomplete="current-password" placeholder="WEBUI_TOKEN"><button id="loginButton">登录</button><p id="loginError" class="error"></p></section>
<div id="appView" hidden><header><h1>Flow Studio</h1><p>把审核分组当作一条可读的路径来编辑。</p><button id="logoutButton" class="alt">退出登录</button></header><main><section class="panel"><div class="toolbar"><button id="saveButton">保存流程</button><button id="reloadButton" class="alt">重新载入</button><span id="status" class="status"></span></div><textarea id="editor" spellcheck="false"></textarea></section><section class="panel"><div id="flow" class="flow"></div></section></main></div>
<script nonce="__NONCE__">
let csrf='',pipeline={version:2,groups:[]};const loginView=document.querySelector('#loginView'),appView=document.querySelector('#appView'),loginToken=document.querySelector('#loginToken'),loginButton=document.querySelector('#loginButton'),loginError=document.querySelector('#loginError'),logoutButton=document.querySelector('#logoutButton'),saveButton=document.querySelector('#saveButton'),reloadButton=document.querySelector('#reloadButton'),editor=document.querySelector('#editor'),flow=document.querySelector('#flow'),statusEl=document.querySelector('#status');const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function apiHeaders(json=false){let h={'X-CSRF-Token':csrf};if(json)h['Content-Type']='application/json';return h}
function showApp(){loginView.hidden=true;appView.hidden=false;if(__CUSTOM_CSS_ENABLED__&&!document.querySelector('link[data-custom-css]')){let l=document.createElement('link');l.rel='stylesheet';l.href='/custom.css';l.dataset.customCss='1';document.head.appendChild(l)}}
function showLogin(message=''){appView.hidden=true;loginView.hidden=false;loginError.textContent=message;loginToken.focus()}
async function restore(){let r=await fetch('/api/session');if(!r.ok)return showLogin();let d=await r.json();csrf=d.csrf;showApp();loadPipeline()}
async function login(){loginError.textContent='';let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:loginToken.value})});let d=await r.json().catch(()=>({}));if(!r.ok)return showLogin(d.error||'登录失败');loginToken.value='';csrf=d.csrf;showApp();loadPipeline()}
async function logout(){await fetch('/api/logout',{method:'POST',headers:apiHeaders()});csrf='';showLogin('已退出登录')}
async function loadPipeline(){let r=await fetch('/api/pipeline');if(r.status===401)return showLogin('会话已过期，请重新登录');if(!r.ok)return setStatus('载入失败: '+r.status);let d=await r.json();editor.value=d.yaml;pipeline=d.pipeline;render();setStatus('已载入')}
async function save(){let r=await fetch('/api/pipeline',{method:'PUT',headers:apiHeaders(true),body:JSON.stringify({yaml:editor.value})});let d=await r.json().catch(()=>({}));if(r.status===401)return showLogin('会话已过期，请重新登录');if(!r.ok)return setStatus(d.error||'保存失败');pipeline=d.pipeline;render();setStatus('YAML 已保存')}
async function saveForm(){sync();let r=await fetch('/api/pipeline',{method:'PUT',headers:apiHeaders(true),body:JSON.stringify({pipeline})});let d=await r.json().catch(()=>({}));if(!r.ok)return setStatus(d.error||'保存失败');pipeline=d.pipeline;editor.value=d.yaml;render();setStatus('表单已保存')}
function val(v){return typeof v==='string'?{flow:v}:{flow:v?.flow||'ignore',goto:v?.goto||'',effects:v?.effects||[],reason:v?.reason||''}}
function render(){flow.innerHTML='<div class="toolbar"><button data-action="add">新增分组</button><button class="alt" data-action="save-form">保存表单</button></div><div class="hint">拖拽卡片可调整执行顺序。</div>'+pipeline.groups.map((g,i)=>{let t=val(g.on_true),f=val(g.on_false),u=val(g.on_unknown),cs=Array.isArray(g.conditions)?g.conditions.join(','):'';return `<article class="node" draggable="true" data-i="${i}"><button class="remove" data-action="remove" data-i="${i}">删除</button><div class="grid"><label>ID<input data-k="id" value="${esc(g.id)}"></label><label>名称<input data-k="name" value="${esc(g.name||'')}"></label><label>分组模式<select data-k="mode">${['all_pass','any_pass'].map(x=>`<option ${x===(g.mode||'all_pass')?'selected':''}>${x}</option>`).join('')}</select></label><label class="wide">条件（逗号分隔）<input data-k="conditions" value="${esc(cs)}"></label>${branchInputs('on_true','true',t)}${branchInputs('on_false','false',f)}${branchInputs('on_unknown','unknown',u)}</div></article>`}).join('');bindDrag()}
function branchInputs(k,label,v){return `<label>${label} 动作<select data-k="${k}.flow">${['next','approve','reject','ignore'].map(x=>`<option ${x===v.flow?'selected':''}>${x}</option>`).join('')}</select></label><label>${label} 跳转<input data-k="${k}.goto" value="${esc(v.goto)}"></label><label>${label} 原因<input data-k="${k}.reason" value="${esc(v.reason)}"></label><label>${label} 副作用<input data-k="${k}.effects" value="${esc(v.effects.join(','))}"></label>`}
function sync(){document.querySelectorAll('.node').forEach((n,i)=>n.querySelectorAll('[data-k]').forEach(el=>{let [a,b]=el.dataset.k.split('.'),v=el.value;if(!b)pipeline.groups[i][a]=a==='conditions'?v.split(',').map(x=>x.trim()).filter(Boolean):v;else{let o=val(pipeline.groups[i][a]);o[b]=b==='effects'?v.split(',').map(x=>x.trim()).filter(Boolean):v;if(b==='goto'&&!v)delete o.goto;if(b==='reason'&&!v)delete o.reason;if(b==='effects'&&!o.effects.length)delete o.effects;pipeline.groups[i][a]=o}}));pipeline.entry=pipeline.groups[0]?.id||''}
function add(){sync();let id='stage_'+(pipeline.groups.length+1);pipeline.groups.push({id,name:'新分组',mode:'all_pass',conditions:['no_conflict'],on_true:{flow:'approve'},on_false:{flow:'reject'},on_unknown:{flow:'ignore'}});render()}function removeGroup(i){sync();pipeline.groups.splice(i,1);render()}
function bindDrag(){let from;document.querySelectorAll('.node').forEach(n=>{n.ondragstart=()=>from=+n.dataset.i;n.ondragover=e=>e.preventDefault();n.ondrop=e=>{e.preventDefault();sync();let to=+n.dataset.i,[x]=pipeline.groups.splice(from,1);pipeline.groups.splice(to,0,x);render()}})}function setStatus(s){statusEl.textContent=s}
loginButton.addEventListener('click',login);loginToken.addEventListener('keydown',e=>{if(e.key==='Enter')login()});logoutButton.addEventListener('click',logout);saveButton.addEventListener('click',save);reloadButton.addEventListener('click',loadPipeline);flow.addEventListener('click',e=>{let a=e.target.dataset.action;if(a==='add')add();if(a==='save-form')saveForm();if(a==='remove')removeGroup(+e.target.dataset.i)});restore();
</script></body></html>"""


def _resolve_custom_css_path(value: str) -> Path | None:
    """Resolve a custom CSS path relative to the process working directory."""
    if not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_pipeline_file(config: PluginConfig) -> Path:
    """根据当前配置定位唯一的审核 YAML 文件。"""
    candidates = {
        Path(group.review_pipeline_file).expanduser()
        for group in config.groups.values()
        if group.review_pipeline_file.strip()
    }
    if not candidates:
        return Path("review_pipeline.yaml").resolve()
    if len(candidates) != 1:
        raise ValueError("检测到多个 review_pipeline_file，当前 WebUI 仅支持编辑单个审核 YAML")
    path = next(iter(candidates))
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _pipeline_payload(pipeline_file: Path) -> dict:
    text = pipeline_file.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {"yaml": text, "groups": data.get("groups", []), "pipeline": data}


def _save_pipeline(pipeline_file: Path, text: str) -> dict:
    pipeline_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    with PIPELINE_WRITE_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=pipeline_file.parent,
                prefix=f"{pipeline_file.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(text)
                temp_path = Path(handle.name)
            load_review_stages_from_yaml_file(temp_path)
            data = yaml.safe_load(text) or {}
            if int(data.get("version", 0)) != 2:
                raise ValueError("目前仅支持 version: 2（conditions + mode 分组格式）")
            temp_path.replace(pipeline_file)
            return {"groups": data.get("groups", []), "pipeline": data, "yaml": text}
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def start_webui(config: PluginConfig) -> ThreadingHTTPServer | None:
    if len(config.webui_token) < 32:
        logger.error("WEBUI_TOKEN 必须至少包含 32 个字符，WebUI 拒绝启动")
        return None
    public_bind = not _is_loopback_host(config.webui_host)
    if public_bind:
        logger.error(
            "为避免绕过 HTTPS 反向代理，WebUI 仅允许监听 localhost/127.0.0.1/::1"
        )
        return None

    custom_css_path = _resolve_custom_css_path(config.webui_custom_css_file)
    pipeline_file = _resolve_pipeline_file(config)
    if custom_css_path and not custom_css_path.is_file():
        logger.warning("WebUI 自定义 CSS 文件不存在：%s", custom_css_path)

    sessions: dict[str, tuple[float, str]] = {}
    failed_logins: dict[str, list[float]] = {}
    state_lock = threading.Lock()
    secure_cookie = config.webui_require_https
    cookie_name = f"__Host-{SESSION_COOKIE}" if secure_cookie else SESSION_COOKIE

    class Handler(BaseHTTPRequestHandler):
        server_version = "Gatekeeper"
        sys_version = ""

        def _client_ip(self) -> str:
            if config.webui_trust_proxy_headers:
                forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
                if forwarded:
                    return forwarded
            return self.client_address[0]

        def _is_https(self) -> bool:
            if not config.webui_require_https:
                return False
            if config.webui_trust_proxy_headers:
                return self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"
            return False

        def _security_headers(self, nonce: str | None = None) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if self._is_https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            if nonce:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; "
                    f"script-src 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                )
            else:
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")

        def _prune_state(self) -> None:
            now = time.monotonic()
            with state_lock:
                expired_sessions = [
                    session_id
                    for session_id, (expires_at, _) in sessions.items()
                    if expires_at <= now
                ]
                for session_id in expired_sessions:
                    sessions.pop(session_id, None)
                expired_attempt_ips = []
                for client_ip, attempts in failed_logins.items():
                    kept_attempts = [
                        value
                        for value in attempts
                        if now - value < config.webui_login_window_seconds
                    ]
                    if kept_attempts:
                        failed_logins[client_ip] = kept_attempts
                    else:
                        expired_attempt_ips.append(client_ip)
                for client_ip in expired_attempt_ips:
                    failed_logins.pop(client_ip, None)

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            extra_headers: dict[str, str] | None = None,
            nonce: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers(nonce)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self, status: int, value: dict, *, extra_headers: dict[str, str] | None = None
        ) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                extra_headers=extra_headers,
            )

        def _reject_insecure(self) -> bool:
            if config.webui_require_https and not self._is_https():
                self._json(HTTPStatus.UPGRADE_REQUIRED, {"error": "https required"})
                return True
            return False

        def _read_json(self, limit: int) -> dict:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type 必须是 application/json")
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > limit:
                raise ValueError("请求体大小无效")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return value

        def _session(self) -> tuple[str, str] | None:
            self._prune_state()
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(cookie_name)
            if morsel is None:
                return None
            session_id = morsel.value
            now = time.monotonic()
            with state_lock:
                session = sessions.get(session_id)
                if session is None:
                    return None
                expires_at, csrf = session
                if expires_at <= now:
                    sessions.pop(session_id, None)
                    return None
            return session_id, csrf

        def _require_session(self, *, csrf: bool = False) -> tuple[str, str] | None:
            session = self._session()
            if session is None:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
                return None
            if csrf and not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), session[1]
            ):
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid csrf token"})
                return None
            return session

        def _cookie_header(self, session_id: str, *, delete: bool = False) -> str:
            parts = [f"{cookie_name}={session_id}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if secure_cookie:
                parts.append("Secure")
            parts.append("Max-Age=0" if delete else f"Max-Age={config.webui_session_timeout_seconds}")
            return "; ".join(parts)

        def do_GET(self) -> None:
            if self._reject_insecure():
                return
            request_path = urlparse(self.path).path
            if request_path == "/":
                nonce = secrets.token_urlsafe(24)
                html = HTML.replace("__NONCE__", nonce).replace(
                    "__CUSTOM_CSS_ENABLED__", "true" if custom_css_path else "false"
                )
                self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8", nonce=nonce)
                return
            if request_path == "/api/session":
                session = self._require_session()
                if session:
                    self._json(HTTPStatus.OK, {"ok": True, "csrf": session[1]})
                return
            if request_path == "/api/pipeline":
                if not self._require_session():
                    return
                try:
                    self._json(HTTPStatus.OK, _pipeline_payload(pipeline_file))
                except Exception:
                    logger.exception("WebUI 读取审核管道失败")
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "读取配置失败"})
                return
            if request_path == "/custom.css":
                if not self._require_session():
                    return
                if not custom_css_path:
                    self._send(HTTPStatus.NOT_FOUND, b"", "text/css; charset=utf-8")
                    return
                try:
                    self._send(HTTPStatus.OK, custom_css_path.read_bytes(), "text/css; charset=utf-8")
                except OSError:
                    logger.exception("读取 WebUI 自定义 CSS 失败")
                    self._send(HTTPStatus.NOT_FOUND, b"", "text/css; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self._reject_insecure():
                return
            request_path = urlparse(self.path).path
            if request_path == "/api/login":
                self._prune_state()
                client_ip = self._client_ip()
                now = time.monotonic()
                with state_lock:
                    attempts = list(failed_logins.get(client_ip, []))
                    failed_logins[client_ip] = attempts
                    blocked = len(attempts) >= config.webui_login_max_attempts
                if blocked:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "登录尝试过多，请稍后再试"},
                        extra_headers={"Retry-After": str(config.webui_login_window_seconds)},
                    )
                    return
                try:
                    supplied = str(self._read_json(MAX_LOGIN_BODY).get("token", ""))
                except (ValueError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "请求格式无效"})
                    return
                if not secrets.compare_digest(supplied, config.webui_token):
                    with state_lock:
                        failed_logins.setdefault(client_ip, []).append(now)
                    time.sleep(0.15)
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "凭据无效"})
                    return
                session_id = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                with state_lock:
                    failed_logins.pop(client_ip, None)
                    sessions[session_id] = (
                        now + config.webui_session_timeout_seconds,
                        csrf_token,
                    )
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "csrf": csrf_token},
                    extra_headers={"Set-Cookie": self._cookie_header(session_id)},
                )
                return
            if request_path == "/api/logout":
                session = self._require_session(csrf=True)
                if not session:
                    return
                with state_lock:
                    sessions.pop(session[0], None)
                self._json(
                    HTTPStatus.OK,
                    {"ok": True},
                    extra_headers={"Set-Cookie": self._cookie_header("", delete=True)},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            if self._reject_insecure() or not self._require_session(csrf=True):
                return
            if urlparse(self.path).path != "/api/pipeline":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = self._read_json(MAX_PIPELINE_BODY)
                if "pipeline" in payload:
                    text = yaml.safe_dump(payload["pipeline"], allow_unicode=True, sort_keys=False)
                else:
                    text = str(payload.get("yaml", ""))
                result = _save_pipeline(pipeline_file, text)
                self._json(HTTPStatus.OK, {"ok": True, **result})
            except (ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                logger.exception("WebUI 保存审核管道失败")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "保存配置失败"})

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("WebUI: " + fmt, *args)

    server = ThreadingHTTPServer((config.webui_host, config.webui_port), Handler)
    threading.Thread(target=server.serve_forever, name="gatekeeper-webui", daemon=True).start()
    logger.info("WebUI 已启动：http://%s:%d/", config.webui_host, config.webui_port)
    return server
