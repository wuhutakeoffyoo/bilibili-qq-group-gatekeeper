"""
插件注册文件。

声明插件元信息并导入主模块以注册事件处理器和命令。
"""

from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="QQ群申请管理",
    description="基于 B 站关注和粉丝牌校验的 QQ 群申请管理插件",
    usage="使用 /设置群、/查看配置、/检查qq、/检查bili 管理和检查审核流程",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 导入即注册事件和命令处理器（副作用导入，F401 误报）
from . import main  # noqa: F401
