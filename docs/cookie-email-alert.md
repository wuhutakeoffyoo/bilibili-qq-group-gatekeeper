# 异常状态邮件告警

`app/bilibili/monitor.py` 定期调用 nav 检查活动 Cookie，`app/runtime.py` 检查日志磁盘；邮件由 `app/notifications/email.py` 发送。使用 SMTP over SSL，当前没有 STARTTLS 模式。

## 告警行为

- `code=-101` 或 `code=0` 且 `isLogin=false` 为明确失效。
- 网络超时、连接失败、非预期响应或业务错误为检查异常，不等同于过期。
- 启动后约 10 秒首次检查 Cookie；默认每小时检查一次，可设 60–86400 秒。
- 磁盘低于 1 GB/0.2 GB 为警告/严重，读取失败也可通知；不会自动删除日志或数据。
- 成功发出的同类异常在进程内去重；发送失败下一周期重试，恢复后再次异常可重新发送。重启可能重新告警。
- 未配置 Cookie 跳过 Cookie 过期告警；SMTP 缺字段则不发送。邮件失败不改变审批规则。

## 配置

新独立启动器按 `.env`、选中的环境文件、进程环境加载并发布有效设置。下列值可在首次迁移前写入私有 `.env.prod`，随后存为 SQL 当前修订；以后修改 SMTP 或周期需编辑版本化 SQL 配置，旧环境不再覆盖。程序不会主动读取个人 `mail_config.json`。

```dotenv
COOKIE_CHECK_INTERVAL_SECONDS=3600
NOTIFY_SMTP_HOST=smtp.example.com
NOTIFY_SMTP_PORT=465
NOTIFY_SMTP_USER=sender@example.com
NOTIFY_SMTP_PASSWORD=replace-with-private-authorization-code
NOTIFY_FROM_ADDR=sender@example.com
NOTIFY_TO_ADDR=recipient@example.com
```

除端口有默认 465 外，host/user/password/from/to 均需非空。非法端口回退 465；周期解析失败回退 3600。

首次导入也可从 systemd 的 `EnvironmentFile=/etc/bili-group-gatekeeper/notify.env` 取值，文件使用 `KEY=value`、权限 `0600`；记入完整备份范围。SQL 已建立时它不会替代当前 SMTP 配置。dotenv 文件与 systemd 环境文件格式不完全相同，不应直接互相替换。

启动与重启使用独立入口：

```bash
uv run --locked --no-dev python bot.py
# 已安装服务时：
sudo systemctl restart bili-group-gatekeeper.service
```

Cookie 后续从 SQL 当前修订读取；换新可用管理员扫码、`/设置cookie` 或可选 `/获取cookie`，见[凭证管理](credentials.md)。旧环境 Cookie 在首次迁移完成后不再覆盖它。

确认周期与发送日志后，还必须核对实际收件箱。模拟 SMTP、日志中的“发送成功”和服务存活不能单独证明最终邮件送达。不要在日志、截图或 Git 中公开密码和 Cookie。
