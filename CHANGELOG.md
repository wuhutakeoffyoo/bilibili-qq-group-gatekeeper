# 更新日志

本文件记录项目的主要变更。下列日期条目不代表独立的语义化版本号。

## 2026-10-17

### 架构重构

- 新增不依赖 NoneBot 的 `src/gatekeeper/` 核心包，集中配置模型、条件计算、审核管道、输入解析、结果格式化和审批编排。
- 由现有插件负责 NoneBot 注册、OneBot 事件及异常转换、Bilibili HTTP、SQLite、配置读写、WebUI 和后台任务；通过适配器连接核心。
- 保留原有模型和格式化函数的导入路径，便于现有调用方逐步迁移。
- 新增 [ADR 0002](docs/adr/0002-framework-independent-core.md)，记录依赖方向、行为约束及后续重构边界。

### 兼容性与验证

- 本批重构保持现有管理命令、配置格式及覆盖顺序、SQLite 表结构和审核 T/F/I 语义。
- 保留审批去重、身份锁、先持久化 `pending` 再调用外部审批、结果不明确时暂停并等待人工确认的既有行为。
- 补充核心包独立导入、管道轨迹与副作用顺序、审批持久化和异常恢复等回归验证。
- 配置迁入版本化 SQLite、独立 OneBot 入口等仍属于后续工作；当前版本继续使用现有配置文件和 NoneBot 入口。

### 文档与源码包

- 将根目录的 `接下来的开发目标.md` 移至 [docs/development-roadmap.md](docs/development-roadmap.md)，保留原文。
- 将根目录的 `筛选需求.md` 移至 [docs/review-requirements.md](docs/review-requirements.md)，保留原文。
- 统一上述文档的文件名为 ASCII，减少压缩包查看和解压工具对中文文件名处理不一致造成的显示问题。
- 新增本更新日志，并在 README 中补充文档入口。
