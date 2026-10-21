# 参与贡献

感谢你愿意帮助改进 Bilibili QQ Group Gatekeeper。项目面向不同经验水平的贡献者，问题描述不需要使用复杂术语。

## 提交问题

- Bug 请使用 Bug 报告模板，并附上复现步骤、预期行为和已脱敏的日志。
- 功能建议请说明使用场景，而不只是描述实现方式。
- 不要在 Issue、截图或日志中提交 Cookie、Token、QQ 号、群号、邮箱、服务器地址等敏感信息。
- 安全漏洞不要提交公开 Issue，请按照 [SECURITY.md](SECURITY.md) 私下报告。

## 本地开发

项目需要 Python 3.10 或更高版本，推荐使用 `uv`：

```bash
uv sync --locked
uv run --locked python -m unittest discover -s tests -p 'test_v3_*.py'
uv sync --locked --extra legacy
uv run --locked --extra legacy python -m unittest discover -s tests
uv run --locked --extra legacy python -m compileall -q bot.py legacy_bot.py app src tests
uv run --locked ruff check .
git diff --check
```

基础设施配置放在被 Git 忽略的 `.env.prod`。首次迁移后业务配置以 SQLite 为准，使用管理台或版本化配置接口修改。不要提交 SQLite、私有导出、迁移备份、真实群号或凭证。默认安装不得安装 NoneBot；`legacy` 仅用于显式兼容入口及旧行为回归。

## Pull Request

1. 从最新的 `main` 创建分支。
2. 只处理一个明确的问题，避免混入无关格式化或重构。
3. 为行为变化补充或更新测试。
4. 确保完整测试通过，并在 PR 中说明验证方式。
5. 按下面清单同步文档，核对真实解析器、权限检查与回归，避免只改功能摘要。

## 文档同步清单

| 变更 | 同步范围 |
| --- | --- |
| 命令、参数或权限 | README 命令表与例子、Bot 帮助、[配置说明](docs/configuration.md) |
| 配置来源、字段或持久化 | `.env.prod.example`、YAML 示例、配置优先级和迁移清单 |
| UID/昵称、条件或分支 | [审核规则](docs/review-requirements.md)、[领域说明](CONTEXT.md)、交接文档 |
| 拒绝反馈 | [反馈要求](docs/rejection-feedback.md)、配置表中自定义原因的含义、实际参数与审计回归 |
| 架构或功能状态 | [ADR](docs/adr/)、[功能状态](docs/development-roadmap.md)、`AI_HANDOFF.md` |
| 运维、告警或升级 | 部署/邮件文档、备份清单、验证边界 |

更新 [CHANGELOG.md](CHANGELOG.md) 和[文档索引](docs/README.md)的相关入口；检查内部链接及示例是否可被当前解析器读取。旧需求可以归档，但应标注日期和“历史”，不能继续把已移除功能写成当前功能。

所有命令示例使用占位群号和凭据。JSON 命令参数需保留正确引号。验证应注明是单元测试、模拟动作、只读接口，还是受控真实业务；文档修订本身不代表完成生产验收。

提交贡献即表示你同意按照项目的 [MIT License](LICENSE) 发布所提交的内容。
