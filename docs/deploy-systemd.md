# 使用 systemd 部署（替代 setsid 手工启动）

项目现名为 **Bilibili QQ Group Gatekeeper**。为避免升级时意外启动两个审核进程，现有 `bili-group-gatekeeper.service` 单元名暂时保留；改名不要求改动已部署的 `/opt/bili-group-gatekeeper` 目录。新部署若使用新的仓库目录名，务必在复制服务文件后同步修改 `WorkingDirectory=`、`ExecStart=` 和下文凭据文件路径。不要同时启用旧、新两个服务单元。

## 为什么改

历史上生产环境用 `setsid .venv/bin/python bot.py > bot.out 2> bot.err` 启动，有两个已知问题：

1. **进程挂了没人拉起**：Python 崩溃、机器重启后需要人工发现并手动重启，群审核在此期间静默停摆。
2. **进程在 ≠ 服务正常**：2026-08-25 曾发生 NapCat 假在线——Python 进程存在、TCP 已连接，但收不到任何新消息，只能靠人工发现。systemd 至少解决第 1 点；第 2 点需要配合监控和实际消息验证（见下文"健康检查"）。

## 部署步骤

1. 停掉旧的 `setsid` 实例，**确认只有一个实例在跑**（双实例会造成重复审批、重复发消息）：

   ```bash
   ps aux | grep "bot.py"
   # 找到旧进程后 kill <PID>，并确认 bot.out 不再增长
   ```

2. 复制服务单元文件并按需修改运行用户、目录和 Python 路径：

   ```bash
   sudo cp deploy/bili-group-gatekeeper.service /etc/systemd/system/
   sudoedit /etc/systemd/system/bili-group-gatekeeper.service
   # 核对：User=、WorkingDirectory=、ExecStart=；保留 UMask=0077
   ```

3. 收紧已有凭据文件的权限（路径及用户按服务单元的 `User=` 调整）：

   ```bash
   sudo chown gatekeeper:gatekeeper /opt/bili-group-gatekeeper/.env.prod
   sudo chmod 600 /opt/bili-group-gatekeeper/.env.prod
   if [ -f /opt/bili-group-gatekeeper/data/runtime/plugin_state.json ]; then
       sudo chown gatekeeper:gatekeeper /opt/bili-group-gatekeeper/data/runtime/plugin_state.json
       sudo chmod 600 /opt/bili-group-gatekeeper/data/runtime/plugin_state.json
   fi
   ```

4. 启动并设置开机自启：

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now bili-group-gatekeeper
   ```

## 日常操作

```bash
systemctl status bili-group-gatekeeper   # 看是否 active
journalctl -u bili-group-gatekeeper -f   # 跟踪实时输出
sudo systemctl restart bili-group-gatekeeper
```

## 升级版本时的流程

1. 备份 `data/records/gatekeeper.sqlite3`（停 Bot 后复制整个 data 目录，或使用在线备份接口；运行中只复制 `.sqlite3` 会漏掉 WAL）。
2. 拉取代码、`uv sync --locked`。
3. `sudo systemctl restart bili-group-gatekeeper`。
4. 验证：发送一条测试命令确认 Bot 收发正常、查看日志确认加载了预期的群配置（尤其是 `GROUP_CONFIG_FILE` 指向的实际群 YAML，而非根目录示例）。

## 健康检查（systemd 覆盖不到的部分）

`Restart=on-failure` 只在**进程退出**时拉起。像 NapCat 假在线这种"进程活着但收不到消息"的情况，systemd 无感知，仍需要：

- NapCat 侧自身的重启/监控机制；
- 定期用管理员 QQ 发 `/查看配置` 之类命令做实际链路验证；
- 检查日志中审核记录是否在正常增长。
