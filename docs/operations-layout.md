# 生产目录与 QQ 适配器运维

## 一个稳定入口

生产运维统一从 `/opt/bili-group-gatekeeper/` 进入。目录布局如下：

```text
/opt/bili-group-gatekeeper/
├── CURRENT-DEPLOYMENT.md       本机当前提交、真实路径和常用命令
├── current -> releases/<版本> 当前应用入口
├── data -> current/data       当前业务数据
├── logs -> current/logs       当前日志
├── shared/process.env         systemd 私有环境，0600
├── releases/                  当前与历史发布版本
├── backups/deployments/       部署备份、配置和校验回执
└── archive/                   旧原始工作目录，仅留档
```

systemd 使用 `current` 下的 Python 和 `bot.py`，工作目录也为 `current`。环境固定读取 `shared/process.env`。业务规则和 B站凭证仍以 SQLite 为准。当前数据库位于 `data/records/gatekeeper.sqlite3`；其他发布或归档目录内的数据不是当前生产数据。

确认实际运行位置：

```bash
readlink -f /opt/bili-group-gatekeeper/current
systemctl show bili-group-gatekeeper.service \
  -p MainPID -p WorkingDirectory -p ExecStart -p EnvironmentFiles -p DropInPaths
journalctl -u bili-group-gatekeeper.service -f
```

本机 `CURRENT-DEPLOYMENT.md` 必须随着实际发布更新。迁移已有目录时，先冻结最新数据、备份完整环境和有效 systemd 配置。流程 YAML 的绝对路径仅在逐项核对内容后迁移，并通过配置版本 CAS 留下新修订；其他业务字段和凭证应保持一致。旧的 systemd 覆盖配置需归档停用，避免多个历史路径相互覆盖。

此布局的数据跟随发布目录。回退前必须保留上线后最新数据，不能直接把 `current` 指向含陈旧数据库的旧版本。完整切换和回退约束见[部署文档](deploy-systemd.md)。

## 当前 SnowLuma 部署

当前生产使用 SnowLuma v2.2.0，适配器与审核 bot 是独立运行单元。固定版本部署、全部 OneBot 节点和 QQ 数据迁移见[SnowLuma 迁移](snowluma-migration.md)，实际只读核验与未验收项见[生产记录](production-acceptance-20261026.md)。

| 位置 | 用途 |
| --- | --- |
| `/opt/bili-group-gatekeeper/current` | 唯一审核服务入口，指向当前发布 |
| `/opt/snowluma/docker-compose.yml` | 当前适配器部署定义 |
| `/opt/snowluma/CURRENT-DEPLOYMENT.md` | 真实镜像摘要、运行参数、端口、备份及验收限制 |
| `/opt/snowluma/data` | 映射 `/app/data`，保存网关配置与管理认证 |
| `/opt/snowluma/qq-config` | 映射 `/app/.config`，保存 QQ 配置与登录数据 |
| `/opt/snowluma/qq-data` | 映射 `/app/.local/share`，保存其他 QQ 数据 |
| `/opt/napcat` | 迁移前私有配置和登录数据，仅作回滚资料 |

当前容器 `snowluma` 使用 `restart=always`、官方默认 UID/GID 1000 与 QQ 启动参数，只有一个 QQ 主进程。HTTP 3000、WS 2389、WS 3001 及原反向连接保留；审核 WS 3001 使用原 `/onebot/v11/ws` 路径。管理端口 5099/6081 只绑定主机回环，通过临时 SSH 隧道访问。此处是已有生产的配置，新部署 Compose 示例的网络和路径见接入文档。

检查当前实例：

```bash
docker compose -f /opt/snowluma/docker-compose.yml ps
docker exec snowluma supervisorctl status
curl http://127.0.0.1:1128/healthz
```

还须通过带令牌的 OneBot 连接核对实际 QQ、`get_version_info`、`get_status` 和业务接口。不得把原始配置、展开后的 Compose 或完整启动日志复制到公开反馈中。

QQ 已恢复在线，临时登录隧道已关闭。**重启免登录仍未验收**，当前实例保持运行；维护重启前需安排账号持有人可完成登录。QQ 切换账号期间曾出现客户端自行重启与 supervisor 拉起的重复窗口，登录结束后需确认只保留一个主实例；不能把多个窗口中的任意扫码成功当作目标账号已就绪。

## 历史 NapCat 运维与回滚

下述 NapCat v4.18.41 操作是迁移前的历史维护记录；不能因此同时启动旧容器，也不能把其 Node Loader 数据路径直接当作 SnowLuma GUI QQ 的路径。

NapCat 与审核 bot 是两个独立运行单元。升级前分别记录正在运行的 OneBot `get_version_info`、镜像、容器配置、端口/网络、卷挂载、环境和重启策略，私有保存完整配置及真实 QQ 登录数据。仅使用[官方发行包](https://github.com/NapNeko/NapCatQQ/releases)，校验发行附件的 SHA256；包内通用 `package.json` 版本号不能代替运行版本回执。

应先确认所用镜像/加载器实际把 QQ 数据写在哪里。图形 QQ 镜像的示例挂载可能为 `/app/.config/QQ`，Node Loader 的现有部署实际使用 `/root/.config/QQ`。仅挂载一个空目录会导致登录数据仍留在容器可写层，重建容器时无法保留登录。应在停机后复制真实数据，并把主机持久化目录挂到实际位置；不能用空目录覆盖现有登录数据。

本次 Node Loader 运维升级使用官方 `v4.18.41` Shell 发行包，在原加载器快照上构建本机镜像，保留原网络、端口、环境、主机标识和配置挂载。QQ 数据映射为 `/opt/napcat/ntqq:/root/.config/QQ`。自定义镜像与回滚快照可能包含私有数据，必须仅留在受控生产机，不得推送到公共镜像仓库。

旧容器作为回滚副本应保持停止并设置 `restart=no`；新容器沿用原服务重启策略。避免 Docker 守护进程重启时同时拉起两个账号实例。回退时先停新容器，保留其最新数据，再恢复旧容器和原策略；不可盲目覆盖升级后已更新的 QQ 数据。

升级验收包括：

1. `get_version_info` 确认真实版本，`get_status` 的 `online`、`good` 均为 true。
2. 实际资料查询恢复，不能只依据容器 `Up`、登录标志或 WebSocket 数量。
3. 再次受控重启 NapCat，确认保存的登录可自动恢复，并验证 bot 重连和启动补审日志。
4. 原申请仍按精确请求键去重，核对审计、计数和真实动作回执；隐私或证据不足仍按当前规则保留人工处理。

人工扫码或 QQ 风控验证可能仍需要账号持有人完成，持久化数据不保证外部平台永久允许自动登录。
