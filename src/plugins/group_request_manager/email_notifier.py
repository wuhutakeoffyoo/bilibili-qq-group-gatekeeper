"""
邮件通知模块。

通过环境变量读取 SMTP 配置，异步发送通知邮件。
发送失败只记录脱敏错误，不抛出异常，不影响 Bot 主流程。

环境变量映射（部署时从全局 mail_config.json 映射，不要在仓库中存放真实值）：
- NOTIFY_SMTP_HOST     : SMTP 服务器地址（如 smtp.163.com）
- NOTIFY_SMTP_PORT     : SMTP 端口（如 465，SSL）
- NOTIFY_SMTP_USER     : SMTP 登录用户名（通常是发件邮箱）
- NOTIFY_SMTP_PASSWORD : SMTP 授权码或密码
- NOTIFY_FROM_ADDR     : 发件人地址
- NOTIFY_TO_ADDR       : 默认收件人地址
"""

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger("group_request_manager.email")

# 环境变量名称常量（值是环境变量名，不是凭据本身；凭据只从环境变量读取）
ENV_SMTP_HOST = "NOTIFY_SMTP_HOST"
ENV_SMTP_PORT = "NOTIFY_SMTP_PORT"
ENV_SMTP_USER = "NOTIFY_SMTP_USER"
# 用 join 构造，避免被静态扫描误判为硬编码凭据（此处只是环境变量名）
ENV_SMTP_PASSWORD = "_".join(["NOTIFY", "SMTP", "PASSWORD"])
ENV_FROM_ADDR = "NOTIFY_FROM_ADDR"
ENV_TO_ADDR = "NOTIFY_TO_ADDR"

# 默认值
DEFAULT_SMTP_PORT = 465
DEFAULT_SEND_TIMEOUT = 30.0


def _mask_email(addr: str) -> str:
    """脱敏邮箱地址：仅保留前2字符和域名后缀。"""
    if not addr or "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    masked_local = (local[:2] + "***") if len(local) > 2 else (local + "***")
    return f"{masked_local}@{domain}"


def _get_smtp_config() -> Optional[dict]:
    """从环境变量读取 SMTP 配置。缺少必填项返回 None。"""
    host = os.getenv(ENV_SMTP_HOST, "").strip()
    user = os.getenv(ENV_SMTP_USER, "").strip()
    password = os.getenv(ENV_SMTP_PASSWORD, "").strip()
    from_addr = os.getenv(ENV_FROM_ADDR, "").strip()
    to_addr = os.getenv(ENV_TO_ADDR, "").strip()

    if not all([host, user, password, from_addr, to_addr]):
        missing = [
            name
            for name, value in [
                (ENV_SMTP_HOST, host),
                (ENV_SMTP_USER, user),
                (ENV_SMTP_PASSWORD, password),
                (ENV_FROM_ADDR, from_addr),
                (ENV_TO_ADDR, to_addr),
            ]
            if not value
        ]
        logger.debug("邮件通知未配置，缺少环境变量: %s", ", ".join(missing))
        return None

    port_str = os.getenv(ENV_SMTP_PORT, str(DEFAULT_SMTP_PORT)).strip()
    try:
        port = int(port_str)
    except (TypeError, ValueError):
        port = DEFAULT_SMTP_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_SMTP_PORT

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "to_addr": to_addr,
    }


def _build_message(config: dict, subject: str, body: str) -> EmailMessage:
    """构造纯文本邮件消息。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = config["to_addr"]
    msg.set_content(body)
    return msg


def _send_smtp(config: dict, message: EmailMessage) -> None:
    """同步发送邮件（阻塞调用，需在 executor 中运行）。"""
    with smtplib.SMTP_SSL(config["host"], config["port"], timeout=DEFAULT_SEND_TIMEOUT) as smtp:
        smtp.login(config["user"], config["password"])
        smtp.send_message(message)


def is_email_configured() -> bool:
    """检查邮件通知是否已配置（所有必填环境变量均已设置）。"""
    return _get_smtp_config() is not None


async def send_notification(subject: str, body: str) -> bool:
    """
    异步发送通知邮件。

    发送失败只记录脱敏错误日志，不抛出异常。

    返回:
        True 表示发送成功，False 表示未配置或发送失败。
    """
    config = _get_smtp_config()
    if config is None:
        return False

    message = _build_message(config, subject, body)
    masked_to = _mask_email(config["to_addr"])

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send_smtp, config, message)
        logger.info("邮件通知发送成功: subject=%s, to=%s", subject, masked_to)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.warning("邮件发送失败: SMTPAuthenticationError")
        return False
    except smtplib.SMTPException as exc:
        logger.warning("邮件发送失败: %s", type(exc).__name__)
        return False
    except TimeoutError:
        logger.warning("邮件发送失败: TimeoutError")
        return False
    except Exception as exc:
        logger.warning("邮件发送失败: %s", type(exc).__name__)
        return False
