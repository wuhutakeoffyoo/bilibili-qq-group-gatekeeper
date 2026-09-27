"""
NoneBot2 机器人入口。

初始化框架，注册 OneBot V11 适配器并启动。
"""

from pathlib import Path

import nonebot
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot（自动读取 .env.prod 配置）
nonebot.init()

# 日志按天写入本地文件；不设置 retention，程序不会自动删除历史日志。
log_directory = Path(
    getattr(nonebot.get_driver().config, "local_log_directory", "logs") or "logs"
).expanduser()
log_directory.mkdir(parents=True, exist_ok=True)
logger.add(
    log_directory / "bot_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention=None,
    compression=None,
    enqueue=True,
    encoding="utf-8",
)

# 注册 OneBot V11 适配器（用于连接 NapCat 等 OneBot 实现）
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 从 pyproject.toml 加载插件
nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
