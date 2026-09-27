# 异常状态邮件告警

Bot 会在启动后检查 B站 Cookie，之后按固定周期调用 B站 `nav` 接口验证登录状态；磁盘空间监控也会复用同一套邮箱配置发送告警。

- `code=-101` 或 `code=0` 且 `isLogin=false`：确认 Cookie 已失效并发送邮件。
- 网络超时、连接失败或其他 B站业务错误：发送“Cookie 检查异常”邮件，但不误报 Cookie 过期。
- 日志目录所在磁盘空间低于阈值：发送“磁盘告警”邮件，同时仍会私聊超级管理员。
- 同一种异常成功发送后会去重；发送失败会在下一周期重试。状态恢复正常后，再次异常可重新告警。

## 配置

在部署环境的私有 `.env.prod` 中配置：

```dotenv
COOKIE_CHECK_INTERVAL_SECONDS=3600
NOTIFY_SMTP_HOST="smtp.example.com"
NOTIFY_SMTP_PORT="465"
NOTIFY_SMTP_USER="sender@example.com"
NOTIFY_SMTP_PASSWORD="SMTP授权码"
NOTIFY_FROM_ADDR="sender@example.com"
NOTIFY_TO_ADDR="recipient@example.com"
```

当前实现使用 SMTP over SSL，通常对应端口 `465`。检查周期允许 `60` 到 `86400` 秒。

不要将真实 Cookie、邮箱密码或 SMTP 授权码提交到 Git。个人环境可从全局 `mail.md`/`mail_config.json` 将对应字段映射到上述环境变量；开源部署者只需使用自己的 SMTP 配置。

Cookie 失效后，可由超级管理员私聊 Bot 执行 `/获取cookie` 从 CookieCloud 刷新。
