本目录用于存放本地业务记录。

- `gatekeeper.sqlite3`：当前使用的 SQLite 数据库，保存申请审计、绑定关系和退群记录
- `gatekeeper.sqlite3-wal` / `gatekeeper.sqlite3-shm`：Bot 运行时可能生成的 SQLite WAL 文件
- `join_request_records.json`：旧版申请与绑定记录，仅用于首次自动迁移和回滚备份
- `leave_records/`：旧版各群退群记录，仅用于首次自动迁移和回滚备份

旧版退群记录文件命名规则：

- `leave_records_<群号>.json`

例如：

- `leave_records_123456789.json`

备份时请先停止 Bot，再完整复制 `data/` 目录，避免遗漏 WAL 中尚未合并的数据。
