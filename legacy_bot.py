"""Explicit optional NoneBot adapter; the default bot.py is standalone."""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter


if __name__ == '__main__':
    nonebot.init(driver='~fastapi+~httpx+~websockets')
    nonebot.get_driver().register_adapter(Adapter)
    nonebot.load_plugin('app.nonebot_adapter')
    nonebot.run()
