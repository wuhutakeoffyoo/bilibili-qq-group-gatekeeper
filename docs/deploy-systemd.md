# 使用 systemd 部署与升级

支持单个 Bot 进程。兼容保留 `bili-group-gatekeeper.service` 单元名；目录可按部署环境选择，但不能因更名另起第二个审核实例。以下示例默认目录为 `/opt/bili-group-gatekeeper`，实际环境以服务配置为准。

## 首次配置服务

先完成 [README 安装步骤](../README.md#linux-部署)：创建虚拟环境、环境文件、真实群 YAML、审核流程及 Cookie。仓库的示例群默认关闭，不能直接当作生产配置。

1. 确认运行用户存在，且拥有程序、配置、数据库和日志目录的读写权限。若使用模板的 `gatekeeper` 用户且尚不存在，可创建：

   ```bash
   id gatekeeper || sudo useradd --system --home-dir /opt/bili-group-gatekeeper --shell /usr/sbin/nologin gatekeeper
   ```

2. 停止原来的手工实例，核对命令行和工作目录后再结束对应 PID。其他 Bot 进程不要一起停止；确认只有一个审核实例。

3. 从仓库根目录复制服务模板：

   ```bash
   sudo cp deploy/bili-group-gatekeeper.service /etc/systemd/system/
   sudoedit /etc/systemd/system/bili-group-gatekeeper.service
   ```

   核对 `User=`、`WorkingDirectory=` 和 `ExecStart=` 的实际路径，保留 `UMask=0077`。`ExecStart` 使用该目录的虚拟环境 Python，且工作目录内具备 `.env`、`.env.prod`。

4. 收紧已有凭据权限。以下路径和用户应与服务保持一致：

   ```bash
   sudo chown gatekeeper:gatekeeper /opt/bili-group-gatekeeper/.env.prod
   sudo chmod 600 /opt/bili-group-gatekeeper/.env.prod
   if [ -f /opt/bili-group-gatekeeper/data/runtime/plugin_state.json ]; then
       sudo chown gatekeeper:gatekeeper /opt/bili-group-gatekeeper/data/runtime/plugin_state.json
       sudo chmod 600 /opt/bili-group-gatekeeper/data/runtime/plugin_state.json
   fi
   ```

   SQLite、命令覆盖、私有 YAML 和服务环境文件也应仅由运行账号及管理员读取。复制后核对属主，避免新版本数据库不可写。

5. 如需邮件或自定义 Cookie 检查周期，按[邮件文档](cookie-email-alert.md)添加独立 `EnvironmentFile=`。NoneBot 读取 `.env.prod` 不会把内容全部导出到进程环境。

6. 校验并启动：

   ```bash
   sudo systemd-analyze verify /etc/systemd/system/bili-group-gatekeeper.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now bili-group-gatekeeper.service
   systemctl show bili-group-gatekeeper.service --property=ActiveState,SubState,MainPID,NRestarts,WorkingDirectory
   ```

## 日常检查

```bash
systemctl status bili-group-gatekeeper.service
journalctl -u bili-group-gatekeeper.service -f
sudo systemctl restart bili-group-gatekeeper.service
```

`Restart=on-failure` 在进程退出后重启；`KillSignal=SIGINT` 和停止超时用于优雅结束。它们不能发现“进程在、TCP 已连接但不收事件”的 NapCat 假在线。

## 升级与全量迁移

### 找出真实配置

先查询工作目录，不能假设旧固定路径仍在使用：

```bash
systemctl show bili-group-gatekeeper.service --property=MainPID,WorkingDirectory,FragmentPath,DropInPaths
```

核对[全量迁移清单](configuration.md#全量迁移清单)：完整 data、环境选择、群和流程 YAML、外置文件、systemd 单元/drop-in、EnvironmentFile 和启动器注入值。绝对路径在迁移目录后可能仍指向旧版本，须单独确认。

### 准备候选版本

- 保留旧源码和提交标识，在独立目录准备候选源码及锁定依赖；不在仍运行的版本上替换文件。
- 建立一致的预检查副本。在线 SQLite 必须用备份接口，不只复制主库。
- 比较有效配置与原始文件，验证候选规则、输入分流、拒绝反馈和数据库读取。
- 预检查副本不是最终数据；原进程此时可能继续收到新申请。

### 停服并冻结最新状态

```bash
sudo systemctl stop bili-group-gatekeeper.service
systemctl show bili-group-gatekeeper.service --property=ActiveState,MainPID
```

确认旧 PID 已退出，再备份旧版本、完整配置及 data；校验备份可读取。数据目录之外的 YAML、环境文件和服务配置也要备份。然后把最新冻结数据复制到候选目录，不能用更早的预检查副本覆盖。

### 验证后切换

- 比较新旧有效配置：每个群、门槛、目标、分支、通知、Cookie 来源和 WebUI 设置；路径按新旧根目录归一后核对实际文件。
- 核对原始配置文件及完整 SQLite 表结构、行数和数据内容；运行 `PRAGMA integrity_check`。仅比较行数不能证明一致。
- 确认升级后的 schema 与旧版本回退要求；保留旧 JSON，不把它当作持续同步的数据源。
- 修改同一个服务单元的工作目录、Python 路径及环境文件路径，重新校验，然后 `daemon-reload` 并启动。保留旧版本和备份。

### 上线验收

- 确认新进程实际工作目录、服务状态、重启计数及 Bot 的 OneBot 连接日志。
- 通过 OneBot 只读状态检查及 B站只读查询确认连接、Cookie 和 UID/昵称路径。
- 复查全部配置、数据库完整性、pending 状态；启动恢复任务不能消除尚未确认的审批。
- 用管理员私聊 `/查看配置` 验证实际收发；`/检查bili` 没有申请 QQ。
- 需要验证真实入群结果时，用受控申请核对 QQ 实际回执、审计与用户反馈。单元测试、只读 API 或进程存活均不能代替。

## 回退

先停止新进程并保留其最新配置和 data。新版本已收到业务记录时，回退要使用最新数据并确认旧代码 schema 兼容性；不能直接恢复停服前的旧备份丢弃新记录。无法兼容时需专门的数据恢复方案。

恢复原服务单元及对应环境配置，重新加载、启动并重复健康与配置检查。不要同时启动两个发布目录。
