"""Optional event/action conversion only. All rules/config/storage use app.

Load via legacy_bot.py, never the archived src.plugins implementation. This
module is the only app module permitted to import the optional NoneBot extra.
"""
from types import SimpleNamespace

from nonebot import get_driver, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import Bot, Event
from nonebot.adapters.onebot.v11.exception import ActionFailed

from app.runtime import Runtime
from app.settings import get_settings
from app.web.admin_api import install_admin_routes
from app.web.verify_h5 import install_verify_routes
from src.gatekeeper.decisions import DefiniteActionFailure

driver = get_driver()
values = {**vars(get_settings()), 'onebot_ws_urls': []}
runtime = Runtime(settings=SimpleNamespace(**values))


class BotPort:
    def __init__(self, bot):
        self.bot, self.self_id, self.connected = bot, str(bot.self_id), True

    async def call_action(self, action, **params):
        try:
            return await self.bot.call_api(action, **params)
        except ActionFailed as exc:
            raise DefiniteActionFailure('OneBot rejected action') from exc

    def __getattr__(self, name):
        async def action(**params):
            return await self.call_action(name, **params)
        return action

    async def start(self):
        pass

    async def close(self):
        self.connected = False


@driver.on_startup
async def startup():
    await runtime.start()


@driver.on_shutdown
async def shutdown():
    await runtime.close()


@driver.on_bot_connect
async def connected(bot: Bot):
    runtime.clients[str(bot.self_id)] = BotPort(bot)


@driver.on_bot_disconnect
async def disconnected(bot: Bot):
    port = runtime.clients.get(str(bot.self_id))
    if port and port.bot is bot:
        runtime.clients.pop(str(bot.self_id), None)
        await port.close()


messages = on_message(priority=10, block=False)
requests = on_request(priority=10, block=False)
notices = on_notice(priority=10, block=False)


@messages.handle()
@requests.handle()
@notices.handle()
async def handle(bot: Bot, event: Event):
    port = runtime.clients.get(str(bot.self_id))
    if port is None:
        port = BotPort(bot)
        runtime.clients[str(bot.self_id)] = port
    await runtime.handle_event(event.model_dump(), port)


install_admin_routes(driver.server_app, runtime)
install_verify_routes(driver.server_app, runtime)


@driver.server_app.get('/healthz')
async def health():
    return {'status': 'ok' if runtime.started else 'starting', 'adapter': 'nonebot-compat',
            'onebot_connected': sum(port.connected for port in runtime.clients.values())}
