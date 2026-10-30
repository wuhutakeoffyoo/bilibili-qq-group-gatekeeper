# SnowLuma 接入与迁移

独立运行时通过 OneBot v11 接入 SnowLuma，业务 SQLite、B站接口和审核流程继续由本项目管理。本文基于 [SnowLuma v2.2.0](https://github.com/SnowLuma/SnowLuma/tree/v2.2.0) 的动作及申请转换源码；协议夹具与真实生产验收分别记录，不能把兼容 OneBot 等同于所有扩展接口结构相同。

当前生产已迁入 SnowLuma，接入与配置数据基线见 [2026-10-26 生产记录](production-acceptance-20261026.md)。10 月 30 日确认原版申请转换缺陷并以私有产物修复，已恢复真实遗漏申请审核，见[后续核查](snowluma-request-incident-20261030.md)。**重启免登录尚未验收**；当前实例保持运行。

## 新部署

官方 Docker 框架提供 QQ、SnowLuma、WebUI 和 noVNC。v2.2.0 原版存在申请转换缺陷；先按[私有修复说明](snowluma-request-incident-20261030.md)构建本机镜像，再将 `SNOWLUMA_IMAGE` 指向已修复产物。示例固定官方版本用于可复现基线，不能直接当作已修复部署。启动示例：

```bash
docker compose --env-file .env.prod -f compose.yaml -f deploy/compose.snowluma.yaml up -d --build
```

覆盖文件固定默认版本 v2.2.0，三个持久化目录分别映射到 `/app/data`、`/app/.config`、`/app/.local/share`。WebUI 5099 和 noVNC 6081 仅映射主机回环；OneBot 在 Compose 私网中供 bot 使用。管理员通过 SSH 隧道完成首次引导、协议确认和 QQ 登录，并把 WS 服务端 `accessToken` 设置为 bot 的 `ONEBOT_ACCESS_TOKEN`。不能同时启用同一账号的 SnowLuma 和 NapCat 容器。

启动需要官方框架要求的 `SYS_PTRACE` 和容器 seccomp 配置；参见[官方部署说明](https://github.com/SnowLuma/SnowLuma.Docker.Framework)。遥测默认在覆盖文件中关闭。初次使用的 EULA/隐私确认须由运营者审阅和同意，示例不自动接受。

优先沿用官方默认运行用户 UID/GID 1000 与 QQ 启动参数。已有数据的属主应与运行用户匹配；调整前保留完整私有备份，不能用空目录替换现有 QQ 数据。项目 Compose 示例不传入额外的账号启动参数；实际账号由 QQ 登录会话确定。

### 后台运行与登录恢复

官方 Docker 镜像在容器内用 Xvfb 提供虚拟显示环境，原生 Linux QQ 与 SnowLuma 可在没有实体显示器的服务器上后台运行。当前方案仍依赖 QQ 进程及其登录会话；noVNC 只是首次扫码和排障入口，浏览器关闭不影响后台运行。WebUI 的管理登录与 QQ 的扫码登录是两个不同入口。

准备登录恢复时，先确认目标账号；使用账号密码登录时，在输入账号后再次确认“自动登录”和“记住密码”均已勾选，再完成登录。扫码登录由手机实际使用的账号决定，可能与界面预填账号不同。以 `get_login_info.user_id` 核验真实账号，再确认 `get_status.online/good` 与原有 OneBot 连接；原端点不可用时先核对 SnowLuma 会话中的账号。其他缓存账号保持空网络配置，不能直接复制目标账号的网络配置来解决误登录。

持久化目录、登录页勾选项和首次在线成功都不能保证重启免登录。后续受控重启须在账号持有人可完成登录时安排，恢复后再次核对原账号、健康接口、bot 补扫及历史申请去重，才可记录恢复验收通过。本次生产在旧运行配置下未通过自动恢复；恢复官方默认用户及启动参数后重新登录，未再次重启，因此尚未确认这些调整解决了登录保存问题。QQ 恢复健康连接后的补审扫描已实机验证完成，自动登录与补审分别验收。

## 已有 NapCat 部署

迁移前记录所有 HTTP/WS 服务端、反向客户端、令牌、格式、原消息回报开关和重连参数，不能只迁移群审核所用的一个端口。SnowLuma 的配置键为 `networks.httpServers`、`httpClients`、`wsServers`、`wsClients`，令牌键为 `accessToken`；NapCat 的 `network.websocketServers` 等 JSON 不能原样覆盖。已禁用的节点不得被迁移成启用。

在独立目录准备固定版本镜像，校验摘要与官方发布记录。停止旧适配器后，冻结备份真实 QQ 登录数据，并复制到独立的新目录。Node Loader 原挂载可能是 `/root/.config/QQ`，SnowLuma 官方 GUI QQ 的 HOME 是 `/app`，因此目标为 `/app/.config/QQ`。不能让两个进程共享正在使用的 QQ 数据，也不能假定更换客户端后一定免扫码。

审核 bot 升级时保留全部业务表、配置修订、凭证和所有群规则；切换前逐表及全量配置比较。使用 `/opt/bili-group-gatekeeper/current` 稳定入口，旧版本、环境、容器和数据保留为私有回滚资料。确认新服务稳定后，旧适配器保持停止且 `restart=no`，避免宿主机重启时重复登录。

## 群申请与精确对账

- 根据 `get_version_info.app_name` 选择已核对的响应契约；SnowLuma 身份为 `SnowLuma`，版本类似 `2.2.0-node`。
- `get_group_system_msg(count=100)` 返回数组，申请人为 `requester_uin`，完整动作标识由 `flag` 提供。程序要求 `checked === false`、有效 QQ/群号、安全整数序号、原始留言字符串及唯一记录。
- v2.2.0 的完整格式是 `slreq:1:<sequence>:<group>:<event_type>:<filtered>`。仅 `event_type=1` 作为主动入群申请；2/22 等邀请不会自动转成 `add` 审核。申请 flag 中的序号和群号必须与行数据一致，执行动作时完整原样传回。
- 迁移去重核对 bot、群、申请序号及持久化申请 QQ；支持查询旧 NapCat 数字键和 SnowLuma 两个收件箱变体，共用同一申请锁。不重写历史审计或统计，不仅凭昵称或 QQ 猜测两次申请相同。
- pending 对账必须匹配原完整 flag、群、QQ 和预期动作，且唯一记录明确未处理才可提供重发证据。列表缺失、checked=true、邀请或语义未知均不能证明动作成功。旧 NapCat pending 不会自动通过 SnowLuma 重新发送，应在迁移前清空或人工确认。
- QQ 等级通过普通资料查询的 `level` / `qq_level` / `qqLevel` 读取；不可用继续作为 Unknown，不绕过当前群门槛。

SnowLuma 查询合并主收件箱与过滤收件箱，每个最多请求 100 条，没有实现无限历史翻页。达到窗口边界时会记录警告，旧申请可能仍需人工复核。

## 验收与回退

分别确认真实版本、`online/good`、当前登录 QQ、资料查询、群列表、系统申请数组及原有其他消费者的连接。然后验证 bot 启动/重连及健康运行期间的周期补审、原申请去重和现有配置不漂移。真实审批、私聊、扫码确认和踢人须单独记录实际回执，不能以只读探针代替。

若新适配器不能就绪，先停新实例，保留新产生的数据，再恢复原 OneBot 环境和旧容器；不得用过期 SQLite 覆盖上线后的新业务记录。适配器切换不能更改 B站身份路由、资格条件或隐私 Unknown 的处理规则。
