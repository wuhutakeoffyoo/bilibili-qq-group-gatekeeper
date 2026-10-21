# 旧运行时来源与辅助文件

V3 默认业务配置与扩展状态已迁入 `../records/gatekeeper.sqlite3`。

- `plugin_state.json`：仅在 SQL 尚无配置修订时作为旧运行时来源。
- `group_overrides.json`：仅首次迁移按旧优先级编译；不再作为 `/设置群` 的写入目标。
- `bili_qrcode.png`：可能由旧兼容工具生成的二维码辅助文件，不是活动登录凭证来源。

首次迁移后不要通过编辑这些 JSON 更新业务配置；使用管理控制台、版本化导入或授权命令。旧文件保留用于核对和恢复，可能含 Cookie，不得提交 Git 或公开。

新扩展状态存放 SQLite `runtime_state`，包括所有权挑战、pending 精确动作与巡检元数据。完整备份保留整个 `data/`，操作说明见 [配置与迁移](../../docs/configuration.md)。
