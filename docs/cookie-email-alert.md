# 异常状态邮件告警

Bot 启动后会延迟检查 B站 Cookie，之后按周期调用 nav 验证登录状态。磁盘监控复用同一套 SMTP 配置。当前使用 SMTP over SSL，不支持用本页配置切换为 STARTTLS。

## 告警行为

- `code=-101` 或 `code=0` 且 `isLogin=false`：确认 Cookie 失效。
- 网络超时、连接失败或其他 B站业务错误：报告“Cookie 检查异常”，不当作 Cookie 过期。
- 剩余磁盘空间低于 1 GB/0.2 GB：警告/严重；取得 Bot 后也私聊超级管理员。磁盘读取失败会尝试邮件。
- 同类异常成功发信后去重，失败后在下一周期重试。恢复正常后再次异常可重新发送；去重状态仅在进程内，重启后可重新告警。
- 未配置 Cookie 时跳过过期告警；SMTP 缺少必填项时不发送邮件。
- 邮件失败不会触发自动清理或自动拒绝申请。

## 注入进程环境

`email_notifier.py` 和 Cookie 周期使用 `os.getenv`。**这些值必须存在于 Bot 进程环境；只写入由 NoneBot 解析的 `.env.prod` 不会自动导出。** 程序也不会主动读取个人的 `mail_config.json`。

Linux 交互启动可在同一终端设置：

```bash
export COOKIE_CHECK_INTERVAL_SECONDS=3600
export NOTIFY_SMTP_HOST="smtp.example.com"
export NOTIFY_SMTP_PORT=465
export NOTIFY_SMTP_USER="sender@example.com"
export NOTIFY_SMTP_PASSWORD="your-smtp-authorization-code"
export NOTIFY_FROM_ADDR="sender@example.com"
export NOTIFY_TO_ADDR="recipient@example.com"
uv run --locked python bot.py
```

示例地址和授权码需要替换为自己的私有配置。Windows 可用同名 `$env:变量名` 在启动终端注入；只在另一个终端设置无效。

## systemd 注入

首次创建服务专用私有文件：

```bash
sudo install -d -m 700 /etc/bili-group-gatekeeper
if ! sudo test -e /etc/bili-group-gatekeeper/notify.env; then
    sudo install -m 600 /dev/null /etc/bili-group-gatekeeper/notify.env
fi
sudoedit /etc/bili-group-gatekeeper/notify.env
sudo systemctl edit bili-group-gatekeeper.service
```

内容使用 `KEY=value`，逐行填写上节的七个变量，不写 `export`。drop-in 中加入：

```ini
[Service]
EnvironmentFile=/etc/bili-group-gatekeeper/notify.env
```

然后加载配置并重启：

```bash
sudo systemctl daemon-reload
sudo systemctl restart bili-group-gatekeeper.service
```

备份应包含该环境文件和 drop-in。不要把整个 `.env.prod` 直接当作 systemd 环境文件：其内容格式和 NoneBot 解析规则未必一致。

## 默认值与检查

| 字段 | 规则 |
| --- | --- |
| `COOKIE_CHECK_INTERVAL_SECONDS` | 默认 3600 秒；整数限制在 60–86400，解析失败用默认值 |
| `NOTIFY_SMTP_PORT` | 默认 465；非法端口回退 465 |
| 其他五个 SMTP/地址字段 | 均需非空，任一缺少则跳过发送 |

确认启动日志显示期望检查周期；真正发信需核对收件箱。模拟 SMTP 测试不能证明实际邮件已送达。不要输出密码、Cookie 或授权码，不要提交 Git。

Cookie 失效后由超级管理员用 `/设置cookie` 提供新值；已配置可选 CookieCloud 时可用 `/获取cookie`。若环境中仍有旧 `BILI_COOKIE`，须同步更新或清空，见[配置来源](configuration.md#cookie-与其他全局配置)。
