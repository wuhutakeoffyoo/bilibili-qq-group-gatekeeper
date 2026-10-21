# V3 部署、全量迁移与恢复

默认启动 `bot.py` 或 `python -m app.main`，两者均进入同一个独立 FastAPI/OneBot 生命周期。生产依赖不含 NoneBot；受支持模式为一个进程、一个 Uvicorn worker，不能同时运行两个接收同一账号事件的版本。

## 首次安装

按 [README](../README.md#安装与启动) 克隆并填写私有配置，生产安装与启动：

```bash
uv sync --locked --no-dev
uv run --locked --no-dev python bot.py
```

`.env` 选择环境，`.env.<环境>` 提供初始值，进程环境覆盖。持续生效的基础设施包括 OneBot、超级管理员、监听地址/端口与日志；业务/凭证/SMTP/CookieCloud 等在首次导入后由 SQL 决定，见[配置文档](configuration.md)。

## systemd

示例沿用 `bili-group-gatekeeper.service` 与 `/opt/bili-group-gatekeeper`，实际已有生产路径可能不同，必须读取当前单元核对。

1. 创建或确认运行账号，授予应用、数据和日志目录必要权限。
2. 停止已知的手动实例；核对 PID 与工作目录，不停止其他 Bot。
3. 复制并编辑服务模板：

   ```bash
   sudo cp deploy/bili-group-gatekeeper.service /etc/systemd/system/
   sudoedit /etc/systemd/system/bili-group-gatekeeper.service
   ```

4. 核对 `User=`、`WorkingDirectory=`、虚拟环境 Python 和所有 `EnvironmentFile=` 路径，保留 `UMask=0077`。模板通过 `SIGTERM` 关闭 Uvicorn、连接与后台任务。
5. 私有环境文件和 SQLite 为 `0600`，私有备份目录为 `0700`，属主为服务账号。日志目录需可写；勿把真实值复制到 Git。
6. 校验并启动：

   ```bash
   sudo systemd-analyze verify /etc/systemd/system/bili-group-gatekeeper.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now bili-group-gatekeeper.service
   systemctl show bili-group-gatekeeper.service --property=ActiveState,SubState,MainPID,NRestarts,WorkingDirectory
   journalctl -u bili-group-gatekeeper.service -f
   ```

`Restart=on-failure` 只负责退出恢复，不能证明 NapCat 下游处理正常。`/healthz` 也不证明 Cookie、动作回执与业务状态正确。

## 管理台与反向代理

ASGI、管理台、反向 WS 和 H5 共用端口。管理台实际监听与 SQL `webui_host` 均须回环；HTTP 本机访问，远程通过同机可信 HTTPS 代理。启用代理信任只影响回环对端，不能把任意公网转发头视为可信。

管理台已有 SQL 配置时，修改 `.env.prod` 的 WebUI 开关或 Token 不会覆盖它；本地恢复操作见[管理控制台](admin-console.md)。H5 可以只公开 `/verify` 相关路径，OneBot 则公开相应 WS 路径并校验令牌，不必公开管理 API。

## Docker / Compose

仓库 `Dockerfile` 使用锁定依赖和非 root 用户构建，默认运行独立 `bot.py`。基础 `compose.yaml` 将主机回环端口映射到容器 `0.0.0.0:1128`，适用于不启用管理台的桥接部署；管理安全策略会拒绝非回环监听的控制台，即使主机端口仅映射到回环。

Linux 上需要控制台时，使用 [host-network 覆盖](../deploy/compose.host.yaml) 让应用在 `127.0.0.1` 监听并清除 `ports:`，再由同机代理访问：

```bash
docker compose --env-file .env.prod -f compose.yaml -f deploy/compose.host.yaml config
docker compose --env-file .env.prod -f compose.yaml -f deploy/compose.host.yaml up -d --build
```

需要支持覆盖文件 `!reset` 的 Docker Compose。`--env-file .env.prod` 同时供覆盖文件的变量插值使用；只设置 service `env_file:` 不会为 `${WEBUI_ENABLED}` 插值提供值。输出 `config` 前确保不把展开后的秘密发送到公开日志。

若需同时建立独立 NapCat 容器，可用 [NapCat 覆盖](../deploy/compose.napcat.yaml)：在私有环境设置 `NAPCAT_WEBUI_TOKEN`，然后运行：

```bash
docker compose --env-file .env.prod -f compose.yaml -f deploy/compose.napcat.yaml up -d --build
```

此模板使用容器网络 `ws://napcat:3001`，6099 管理端口仅映射到回环。首次需通过 NapCat WebUI 扫码登录，再把 OneBot WS 服务令牌设为与应用相同；不代表无人值守登录已完成。`NAPCAT_IMAGE` 可选固定发布标签。保留已有生产 NapCat 时不启动这个覆盖文件。来源为 [NapCat-Docker](https://github.com/NapNeko/NapCat-Docker/blob/42e4f9e8373c739f4d2ee599980ab8f87cff39b6/compose/ws.yml)。

挂载至少包括完整 `/app/data` 和 `/app/logs`。已有数据目录需与镜像运行 UID 的权限匹配。容器内 `127.0.0.1` 在桥接模式下不是宿主机 NapCat；正向地址应选实际可达端点。反向模式由 NapCat 连接应用公开的 WS。

没有实际 Docker 守护进程的检查只能证明静态文件结构，不能称为镜像构建/容器运行验收。

## V2 → V3 全量迁移

### 找到真实运行状态

```bash
systemctl show bili-group-gatekeeper.service --property=MainPID,WorkingDirectory,FragmentPath,DropInPaths
```

备份和核对范围：

| 范围 | 内容 |
| --- | --- |
| 源码和依赖 | 旧提交、服务入口、锁文件、实际 Python |
| 环境 | `.env`、实际环境文件、进程覆盖、systemd 单元/drop-in、EnvironmentFile |
| 旧业务配置 | runtime/legacy JSON、群覆盖、群 YAML 以及每群实际流程文件，含外置文件/符号链接目标 |
| 数据 | 完整 `data/`，含 SQLite、JSON 兼容记录、扩展状态和备份 |
| 其他文件 | 自定义 CSS、日志位置和被配置引用的文件 |

### 在副本预检查

- 在独立发布目录准备候选源码与锁定依赖，避免覆盖仍运行的代码。
- 在线 SQLite 用一致性备份接口；不要只复制 `gatekeeper.sqlite3` 主文件。
- 保持首次迁移路径规则，核对旧覆盖项中的绝对路径是否仍指向正确文件。
- 候选数据库升级到 v4，并首次编译完整旧有效配置；比较每群开关、门槛、UID、分支/跳转、反馈、管理员、Cookie、SMTP、CookieCloud、WebUI 和监控设置。
- 核对旧业务表所有原有列与完整行内容，不只比较行数；运行 `PRAGMA integrity_check`，pending 必须保留。
- 原始源文件与有效配置可做私有哈希/结构比较，不在日志公开原始凭证和 QQ 记录。

仓库提供只读比较工具，可在启动候选应用前核对冻结副本：

```bash
python deploy/verify_migration.py before.sqlite3 after.sqlite3 --config-before effective-config.json
```

基线 JSON 为旧有效 `PluginConfig.model_dump()` 的私有文件。工具逐个旧表的全部原有列/行比较数量和散列，检查完整性与旧配置字段，只输出差异字段路径、不输出值。应用开始处理新事件后，行内容可能合法变化；完整迁移比较应在切换前完成。

### 冻结最新数据再切换

```bash
sudo systemctl stop bili-group-gatekeeper.service
systemctl show bili-group-gatekeeper.service --property=ActiveState,MainPID
```

确认旧 PID 已退出，备份冻结后的完整数据和外置配置。预检查期间旧进程可能继续收申请，不能用早先副本覆盖最终数据。

将最新冻结数据复制到候选目录，重新迁移/比较；v3→v4 会生成私有数据库备份，配置首次导入保留原始来源。然后只修改同一个服务单元的工作目录/Python/环境路径，校验、daemon-reload 并启动。保留旧发布目录和备份。

### 上线验收

1. 核对新 PID 的真实工作目录、启动入口、服务重启计数和唯一实例。
2. OneBot 只读状态及真实命令往返；B站 nav、UID 和昵称单路查询。
3. 再核对有效 SQL 配置、数据库完整性及 pending；旧环境不得在重启时覆盖 SQL。
4. 运行临时数据库/模拟 OneBot 的整链 smoke，核对拒绝参数、审计与管理员反馈。
5. 若执行真实受控申请、扫码/刷新、评论验证、关注或巡检移除，逐项单独记录实际回执和业务结果。

模拟、只读查询、进程存活不能代替真实审批结果；未执行的外部写操作必须明确说明。

## 配置回滚与软件回退

**配置回滚**：在当前 V3 内选择历史版本，使用当前 expected_version 追加一个新修订。恢复业务字段，保留当前 B站 Cookie/token/generation 和刷新待确认状态，不复活历史会话。

**软件回退**：先停止新进程并保留其最新数据。旧 schema3 程序拒绝 v4 库，不能直接切回旧二进制；需验证兼容的数据恢复/导出方案。新版本已经处理业务时，恢复切换前旧备份会丢记录，不可当作通用回退办法。

可选 `legacy` 安装的是 NoneBot 兼容桥，显式用 `uv run --locked --extra legacy python legacy_bot.py` 启动，由共享 V3 Runtime/SQL 服务处理事件；它不是旧 schema3 程序，也不能与独立入口同时运行。不要再让旧插件自动发现加载归档实现。
