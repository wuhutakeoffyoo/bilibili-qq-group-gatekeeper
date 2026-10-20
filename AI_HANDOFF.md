# Bilibili QQ Group Gatekeeper 交接文档

更新时间：2026-10-20。对照代码基线 `a974209` 核对；分支、CI、生产目录和配置都可能变化，接手时重新检查。

## 接手顺序

1. `git status --short --branch`、`git log -5 --oneline`，确认工作区和提交。
2. 阅读[文档目录与实现对应](docs/README.md)、[领域说明](CONTEXT.md)、[配置来源](docs/configuration.md)。
3. 读所改模块及对应回归；使用锁定环境验证。不要把历史生产验收当成当前状态。
4. 文档或命令变更同步检查[贡献清单](CONTRIBUTING.md)。

2026-09-28 的仓库历史重置已经完成，不应再次执行删除仓库、强推或清除历史。旧安全告警数量、主机路径及测试计数是历史快照，不作为当前事实。

## 架构与存储

| 层 | 责任 |
| --- | --- |
| `src/gatekeeper/` | 纯配置模型、输入解析、条件、T/F/I 汇总、路由、格式化、身份锁、持久审批编排 |
| `src/plugins/group_request_manager/` | NoneBot 注册、OneBot 事件与异常转换、HTTP、SQLite、配置 I/O、WebUI 和后台任务 |
| `bot.py` | 当前 NoneBot 启动入口；独立 OneBot 入口尚未实现 |
| SQLite | `data/records/gatekeeper.sqlite3`，保存业务记录 |
| 配置文件 | 环境文件、群/流程 YAML、运行时 JSON；尚未迁入 SQLite |

核心不能导入 NoneBot/插件、创建运行文件或打开外部连接。旧插件模型及格式化导入路径保留兼容转发。参见 [ADR 0001](docs/adr/0001-sqlite-record-storage.md) 与 [ADR 0002](docs/adr/0002-framework-independent-core.md)。

## 业务约束

- T=已有证据通过；F=已有证据不通过；I=无法判断。`ignore` 是动作，不是 I 的别称。
- `all_pass` 与 `any_pass` 只汇总状态，实际动作由分支决定。仅支持 `conditions + mode`，不恢复旧布尔表达式。
- 仓库 A/B 流程是占位示例，不是所有群硬编码的规则；绑定校验只有被管道引用时才参与。
- 未配置或未启用的群不审核。管理命令仅响应管理员私聊；命令英文不区分大小写。
- UID 标记优先，清洗后只走 UID 资料接口；纯数字同样只走 UID。其他昵称只走搜索。显式 UID 格式错误本地失败，不能回退昵称。
- 账号查询成功仍要按群规则审核。接口异常、分页不完整且无精确匹配、缺失资料均保持 I。
- 搜索只认唯一精确匹配；一个精确匹配加其他模糊结果可接受，多个精确匹配拒绝。
- 关注目标满足任意一个即可；共同关注完整且登录账号已确认关注全部目标时，才可据未命中判 F，否则回退关注列表，仍不完整则 I。
- 粉丝牌条目解析缺失保持 I；已有明确达标条目可为 T。
- 绑定是已登记关系，不证明首次申请者的账号所有权。不能把绑定冲突写成已证实冒用。

详细规则及路由边界见[当前审核规则](docs/review-requirements.md)。

## 拒绝反馈

- 说明具体原因、测得值/目标/门槛及下一步。没有证据时不能声称“账号不存在”“未关注”。
- 账号输入失败需区分 UID 与昵称，要求检查，并告知“请不要填写B站昵称或UID以外的内容”。
- 条件和阶段自定义文案是补充；不能遮盖真实原因、要求和输入提示。
- T 分支拒绝说明策略；I 分支拒绝说明未完成核验和后续处理。
- 应用不再按 120 字静默截断。QQ 参数、审计、管理员阶段通知、检查命令共用反馈逻辑；不保证外部客户端无限长显示。
- 退群时间遵从配置，不泄露绑定冲突另一方 QQ。见[拒绝反馈说明](docs/rejection-feedback.md)。

## 审批、去重与恢复

- 先持久化 pending，再执行 OneBot 操作；成功为 applied，明确 `ActionFailed` 为 failed，非审批记录为 not_required。
- 超时、断连、取消及进程意外退出不能证明 QQ 操作失败。保留 pending，暂停新的自动同意/拒绝。
- 重启恢复 pending。超级管理员核对实际结果后使用 `/待确认审批` 和 `/确认审批 <审计ID> applied|failed`；全部 pending 解决后恢复。
- 身份锁覆盖绑定读取至最终写入；QQ、B站 UID 和重复请求保持串行。解除绑定、人工确认也用同一套锁。
- 不因成员查询结果自动重试未知操作，不用模拟检查结果确认 pending。
- 已移除“新申请推送后回复 y/n”的人工审核，不要与 pending 恢复混淆。

## 配置、命令与运维

- 群配置：运行时快照 → 群 YAML → 命令覆盖 → 旧 JSON 环境配置 → 单组环境配置 → 编号环境配置，最后从实际 YAML 重编译审核阶段。
- `/设置群` 只有 8 个参数，详见[配置说明](docs/configuration.md)。`custom_reject_uids` 和 `review_pipeline_file` 通过 YAML/环境维护。
- 普通管理员不能设置群、操作 Cookie 或恢复审批；可以查看配置、检查记录和解除绑定。
- 阶段通知须显式配置 `effects: [notify_admin]`；模式控制收件人。仅命中退群/冲突不自动推送；严重审批/存储异常仍通知超级管理员。
- `/检查bili` 不接收申请 QQ，QQ 等级/退群/绑定为 I，不写业务记录，不审批真实请求。
- 非空 `BILI_COOKIE` 在重启时覆盖运行时 Cookie。CookieCloud 仅为可选兼容；二维码仅做登录引导。
- SMTP 与 Cookie 检查周期读取 `os.getenv`，必须注入进程环境，见[邮件配置](docs/cookie-email-alert.md)。
- WebUI 只支持单个实际流程 YAML，条件列表/模式/goto 表单及拖拽排序；不支持表达式、拖线或群基础参数编辑。
- 日志仅本地按天写入、不自动上传或删除；磁盘监控不清理文件。
- 部署单实例。迁移完整 data、环境文件、实际 YAML、命令覆盖和服务环境，不能只复制数据库。见[部署说明](docs/deploy-systemd.md)。

## 验证与维护

```bash
uv sync --locked
uv run --locked python -m unittest discover -s tests
uv run --locked python -m compileall -q bot.py src tests
uv run --locked ruff check .
git diff --check
```

测试使用 unittest、mock 和临时数据库；不访问真实账号或生产记录。核心测试可独立运行 `uv run --locked python -m unittest discover -s tests -p 'test_core_*.py'`。Windows 控制台设 `PYTHONIOENCODING=utf-8`。

已有回归覆盖核心独立导入、规则属性、输入路由、拒绝完整性、绑定并发、丢失响应、取消、pending 恢复、配置保存失败、WebUI 和邮件状态。HTTP/QQ 模拟测试不能当作真实业务验收；生产只读查询不能证明真实加群审批已成功。

待开发边界见[功能状态与后续规划](docs/development-roadmap.md)。提交前检查文档正文、示例、帮助文案与模型注释；公开文档不含凭据、真实账号和主机连接信息。
