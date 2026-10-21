# SQLite v4 配置与业务记录

- `gatekeeper.sqlite3`：权威业务配置修订、当前配置指针、申请审计、QQ/UID 绑定、退群记录与 `runtime_state`。
- `gatekeeper.sqlite3-wal` / `gatekeeper.sqlite3-shm`：运行时可能存在的 WAL/共享内存辅助文件。
- `backups/`：升级前的一致性 SQLite 私有备份；不要手工覆盖活动库。
- `join_request_records.json`、`leave_records/`：旧 JSON 业务数据，仅首次导入与保留备份用途，不持续同步。

v3 升级到 v4 保留原有业务列、所有行及未确认 pending。配置版本回滚只恢复业务配置、保留当前 B站凭证，不回退审计或绑定。

数据库包含凭证和个人业务记录，按服务账号保护，POSIX 文件 `0600`、私有备份目录 `0700`。不得提交 Git、发公开日志或将完整文件直接附到 Issue。

在线备份使用 SQLite 一致性备份接口；只复制主文件可能遗漏 WAL。停服务并确认 PID 退出后完整复制 `data/` 是另一种方案。升级时还需保存环境、外置文件和服务配置，见 [部署文档](../../docs/deploy-systemd.md)。
