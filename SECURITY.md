# 安全策略

## 支持范围

安全修复以默认分支 `main` 的最新版本为准。旧提交和自行修改的分支可能不会单独维护。

## 私下报告漏洞

请不要创建公开 Issue，也不要在公开讨论中粘贴利用方法、Cookie、Token 或个人信息。

仓库公开并启用 GitHub Private Vulnerability Reporting 后，请通过仓库的 **Security -> Report a vulnerability** 页面提交报告。报告建议包含：

- 受影响的提交或版本
- 复现步骤与影响范围
- 已脱敏的日志或请求样例
- 可行的修复建议（如有）

维护者会尽快确认报告，并在修复可用后协调披露。

## 部署者责任

- 永远不要提交 `.env.prod`、B站 Cookie、OneBot Token 或 CookieCloud 凭据。
- WebUI 暴露到公网时必须使用 HTTPS、强随机 Token 和可信反向代理。
- 一旦凭据进入 Git 历史，应立即轮换凭据；仅删除当前文件并不能消除历史泄露。
