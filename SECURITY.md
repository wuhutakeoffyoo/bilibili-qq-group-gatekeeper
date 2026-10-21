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
- SQLite 配置修订、原始迁移备份及完整私有导出包含凭证；应限制目录/文件权限，备份与主库同等保护。默认公开导出会隐藏秘密。
- 所有权任务只保存暗号摘要，但精确审批反馈、审计或动作意图可能包含用户专属链接；不要公开数据库、完整审计或链接截图。
- 默认 Uvicorn 不预处理代理头；仅按应用的显式回环代理信任配置接受转发信息。不要用额外的宽泛代理信任覆盖此边界。
- 一旦凭据进入 Git 历史，应立即轮换凭据；仅删除当前文件并不能消除历史泄露。

## CookieCloud 协议兼容约束

可选 CookieCloud 解密必须使用上游定义的 `MD5(uuid + '-' + password)` 密钥，以及 legacy 模式的 MD5 `EVP_BytesToKey`。这是现有密文互操作协议，不能在本地替换算法而继续解密。固定 IV 模式同样使用 MD5 主密钥；它不消除弱派生和无认证加密的限制。不得将这些辅助函数用于新数据加密或用户密码存储。

上游依据：[CookieCloud 解密示例](https://github.com/easychen/CookieCloud/blob/217612dd675d194db8036bc22e632173dc7ace34/examples/decrypt.py)及[固定 IV 实现](https://github.com/easychen/CookieCloud/blob/217612dd675d194db8036bc22e632173dc7ace34/examples/fixediv/python/decrypt.py)。请为 CookieCloud 使用独立的强随机密钥、可信 HTTPS/私有传输及私有服务；不需要同步时可关闭此功能，使用原生扫码或手动凭证。

V3 复制既有协议函数产生的 CodeQL 告警 #5/#6 按协议兼容约束记录为 `won't fix`，算法风险仍存在。该例外只适用于这两个互操作函数，不是对项目其他弱密码算法的豁免。
