# 参与贡献

感谢你愿意帮助改进 Bili Group Gatekeeper。项目面向不同经验水平的贡献者，问题描述不需要使用复杂术语。

## 提交问题

- Bug 请使用 Bug 报告模板，并附上复现步骤、预期行为和已脱敏的日志。
- 功能建议请说明使用场景，而不只是描述实现方式。
- 不要在 Issue、截图或日志中提交 Cookie、Token、QQ 号、群号、邮箱、服务器地址等敏感信息。
- 安全漏洞不要提交公开 Issue，请按照 [SECURITY.md](SECURITY.md) 私下报告。

## 本地开发

项目需要 Python 3.10 或更高版本，推荐使用 `uv`：

```bash
uv sync --locked
uv run python -m unittest discover -s tests
uv run python -m compileall -q bot.py src tests
```

真实配置应写入被 Git 忽略的 `.env.prod`。提交前请确认示例配置只包含占位值。

## Pull Request

1. 从最新的 `main` 创建分支。
2. 只处理一个明确的问题，避免混入无关格式化或重构。
3. 为行为变化补充或更新测试。
4. 确保完整测试通过，并在 PR 中说明验证方式。
5. 如果修改配置字段或命令，同时更新 README 和 `.env.prod.example`。

提交贡献即表示你同意按照项目的 [MIT License](LICENSE) 发布所提交的内容。
