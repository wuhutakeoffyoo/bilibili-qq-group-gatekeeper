# 管理控制台与 API

控制台由默认 FastAPI 应用在 `/`、`/admin` 提供，与 OneBot WS/H5 共享监听端口。它读取和保存 SQL 配置，不再直接编辑部署目录的 YAML 文件。

## 启用与访问

首次迁移前可用私有环境值 `WEBUI_ENABLED=true`、至少 32 字符的 `WEBUI_TOKEN` 初始化。监听必须为回环地址，默认 `127.0.0.1:1128`；本机 HTTP 或可信 HTTPS 反向代理访问。

SQL 建立后，WebUI 开关、令牌和安全设置是 SQL 字段。已有浏览器不能修改这些安全字段；修改环境文件也不会覆盖现有 SQL 修订。需要本地恢复访问时，在服务工作目录用服务账号执行本地配置操作，例如：

```bash
uv run --locked --no-dev python - <<'PY'
from getpass import getpass
from app.config import ConfigManager

ConfigManager.initialize()
snapshot = ConfigManager.get_snapshot(include_secrets=True)
snapshot['config']['webui_enabled'] = True
snapshot['config']['webui_host'] = '127.0.0.1'
snapshot['config']['webui_token'] = getpass('New admin token (at least 32 characters): ')
if len(snapshot['config']['webui_token']) < 32:
    raise ValueError('Token too short')
ConfigManager.replace_config(snapshot['config'], snapshot['version'], actor='local-admin-recovery')
PY
```

不要将新令牌打印或写入 shell 历史。若有版本冲突，刷新重新操作。监听 Host/Port 属于启动基础设施；改变监听需修改实际环境并重启。

## 安全合同

- Token 恒定时间比较、登录失败限流、有限会话与过期。
- 会话 Cookie 为 HttpOnly/SameSite=Strict；HTTPS 模式使用 Secure/`__Host-`。
- 写请求需要同源检查和 `X-CSRF-Token`，并限制 JSON 大小。
- CSP nonce、no-store、防 iframe、防 MIME 嗅探。
- 只有回环对端且显式启用信任代理头时才采用转发协议/客户端信息。
- 改变 Token 会使旧登录会话失效。重启会清掉内存会话。

直接用 `HOST=0.0.0.0` 暴露统一服务会使管理台被安全策略拒绝。Linux Docker 推荐 host-network/回环监听，或采用明确隔离的代理方案，见[部署文档](deploy-systemd.md)。H5 公开访问不应顺便公开管理路径。

## 功能与接口

浏览器提供群表单、SQL 管道分组/表达式/跳转编辑、完整 JSON 配置、审计/绑定/退群表、pending 确认、凭证、所有权和巡检。复杂 JSON 字段保存前需要合法格式，版本冲突返回 409，避免两个管理员互相覆盖。

| 范围 | API |
| --- | --- |
| 会话 | `POST /api/login`、`GET /api/session`、`POST /api/logout` |
| 配置 | `GET/PUT /api/config`、`GET /api/config/export`、`POST /api/config/import` |
| 历史 | `GET /api/config/versions`、`POST /api/config/restore` |
| 群与管道 | `GET /api/groups`、`PUT/DELETE /api/groups/{id}`、`GET/PUT /api/groups/{id}/pipeline` |
| 只读模拟 | `POST /api/simulate` |
| 审计/绑定/退群 | `GET /api/audits`、`GET /api/bindings`、`GET /api/leaves` |
| 解绑与退群删除 | `POST /api/bindings/{qq}/clear`、`DELETE /api/leaves/{group}/{qq}` |
| pending | `GET /api/pending`、`POST /api/pending/{id}/resolve` |
| 凭证 | `/api/credentials/status`、`qr`、`qr/poll`、`refresh`、`fingerprints` |
| 所有权 | `GET /api/ownership`、`POST /api/ownership/{id}/revoke` |
| 巡检 | `GET /api/patrol`、`POST /api/patrol/run`、`POST /api/patrol/{id}/act` |
| 运行状态 | `GET /api/health` |

配置/群/管道/导入/恢复写入需提交当前 `expected_version`。普通导出不含原始秘密；历史只返回版本元数据。业务回滚保留当前 B站 Cookie/token/generation 和待确认刷新状态，避免复活已失效历史会话。

绑定清除与 pending 确认有独立确认字段；pending `applied` 必须基于真实 QQ 结果核对。巡检运行只生成报告，提醒和移除是另一次显式操作。凭证刷新、指纹持久化、扫码及巡检动作可能发出外部请求，不能视为全是只读页面。
