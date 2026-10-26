# V3 架构实施与验收

核对日期：2026-10-21。默认入口、配置存储、协议层、审核和管理台均已迁入独立应用；当前实现对应下表。生产发布的提交、迁移结果和烟测应另附发布记录。

## 原始蓝图对应

| 能力 | 当前实现 | 已有验证 |
| --- | --- | --- |
| 独立通信与统一 ASGI | `app/main.py`、`app/runtime.py`、`app/onebot/`；正反向 WS、echo、重连、事件队列和生命周期 | `test_v3_onebot.py`、`test_v3_runtime.py`；默认依赖的干净环境中无 NoneBot，工厂可创建 |
| 启动及重连补审 | `app/onebot/recovery.py`；检查 QQ online/good、NapCat 身份和精确未处理申请；完整当前规则审核、实时事件共用锁及持久化去重 | `test_startup_request_recovery.py`；覆盖并发、重启、QQ 离线恢复、短暂重连、忽略通知去重与 pending；系统消息窗口最多请求 100 条，不保证无限历史 |
| SnowLuma 接入 | `app/onebot/snowluma.py`、`inspector.py`；v2.2.0 数组、申请 QQ、规范 flag、跨适配器去重及精确 pending 证据；提供 Compose 和迁移文档 | `test_snowluma.py`；真实 SQLite 覆盖历史键、收件箱变体、并发、QQ 冲突、邀请隔离、未知回执及错误接口契约 |
| SQL 配置统一 | v4 保留旧业务表，按旧有效优先级一次导入；原始来源私有备份、版本 CAS、YAML 导入导出和追加式回滚 | `test_v3_config_migration.py`、`test_v3_storage.py`；逐项字段、来源和旧记录核对 |
| 安全表达式 | Kleene T/F/I、`when`、原始数值、`medal` 数值别名、v2 数组与 goto | `test_v3_expressions.py` 及旧核心回归；未知资料不伪装成失败 |
| UID/昵称分流 | 标记 UID 清洗、纯数字直查、含字符昵称搜索、无跨路回退、QQ 误填本地阻断 | 输入回归与 `test_v3_bilibili.py`；找不到账号时提示检查，并要求不要填写昵称或 UID 以外的内容 |
| 请求控制 | 每个实际 HTTP 请求共享限速、60 秒有限资料缓存、在途合并、取消与凭证隔离 | `test_v3_bilibili.py`；不缓存最终审批，不把工作流并发当 QPS |
| 凭证与指纹 | WBI、缺失指纹补齐、手动/CookieCloud/管理员原生扫码、refresh-token 协议与确认恢复、generation CAS、失效告警 | HTTP 协议 fixture、并发凭证保存与恢复回归；真实账号刷新另行验收 |
| 局部 pending | QQ/UID/原请求预约、先持久化再发动作、精确证据、只读退避与最多一次重发、人工确认 | `test_v3_decisions.py`；重发失败不抹掉原未知动作的预约 |
| UP 视角关注兜底 | 验证当前登录确为目标账号后读取其粉丝；正面命中和完整否定分别处理 | 目标身份、分页不完整、多目标及隐藏资料 fixture；未提供大航海/舰长接口 |
| 所有权与 H5 | 可选 DM/动态暗号；申请者原生扫码与管理员凭证隔离；挑战、有效期、单次消费及原申请受控恢复 | `test_v3_ownership_patrol.py`、`test_v3_bilibili.py`；真实 SQLite + H5 HTTP → 原申请重新审核；不接收任意 Token |
| 成员生命周期 | 显式独立巡检表达式、时区 Cron、低速报告/提醒/移除、宽限期、动作前重查、退群释放保护 | Cron、角色、Unknown、策略变化、动作幂等、跨群完整性回归 |
| 管理控制台 | 群表单/管道/拒绝文案、审计详情、绑定/退群清理、pending 确认、凭证健康、配置历史及导入导出 | `test_v3_admin.py`；真实 SQL、会话/CSRF/CSP、代理边界、CAS；浏览器操作和窄屏核对 |
| 发布与运维 | uv 默认独立依赖、systemd、Docker/Compose、NapCat 配套示例、日志与邮件、只读迁移比较工具 | 编译、Ruff、完整回归；容器构建及生产迁移以对应发布记录为准 |

CI 针对 Python 3.10/3.13 分别执行默认独立集成和含可选兼容入口的完整回归。精确命令见[贡献指南](../CONTRIBUTING.md)，不能用历史测试数量替代当前提交的 CI。

## 一致性合同

- 默认应用只导入 `app` 和 `src/gatekeeper`，不导入旧插件。`legacy_bot.py` 是显式可选桥；旧插件代码保留作历史回归材料，不能运行于 v4 数据。
- 业务配置初始化后以 SQL 为准；旧文件和环境业务变量仅作首次迁移来源。回滚业务配置保留当前凭证及刷新配对状态，导出默认脱敏。
- `ScopedDecisionService` 只隔离有关 QQ、UID 和请求。明确仍未处理才允许重发；已有成员、`checked=true`、查不到原申请都不能证明该动作生效。
- 申请者证明不绕过当前审核规则。扫码后只有原 bot/群/QQ/subtype/flag 的精确未处理证据才恢复原申请，其他情况要求重新申请或人工核实。
- 受支持部署是一个应用进程、一个 Uvicorn worker。多活审批不是 SQLite CAS 所能保证的能力。

## 与蓝图表述不同的具体边界

使用标准库异步平滑门控和有界缓存；同步 SQLite 通过 `asyncio.to_thread` 边界运行，没有为名称一致额外引入 aiolimiter、aiosqlite 或 cachetools。Docker 基于 uv/Python 3.13，多阶段构建和非 root 运行。

成员巡检当前核验关注/粉丝牌资格，不能把粉丝牌当成有效舰长、guard 等级或到期时间。UP 视角只提供可核验的关注兜底；社区接口没有“100%证明所有权”或“绝不风控”的保证。H5 使用原生扫码或暗号，不接收申请者自行粘贴的长期凭证。详细限制见[功能状态](development-roadmap.md)与[所有权/巡检](ownership-and-patrol.md)。

## 协议参考与验证层次

以下是核对过的协议实现参考，不是 B站对接口稳定性的承诺：

- [NapCat 系统消息实现](https://github.com/NapNeko/NapCatQQ/blob/450fb0429a064e3418e0c698b1b4d78324101929/packages/napcat-onebot/action/system/GetSystemMsg.ts)：精确识别 `join_requests` 中未处理申请，不能推断已处理结果。
- [B站登录与刷新协议参考](https://github.com/Ailitonia/omega-miya/blob/aa8b749378b6f599461c73cd2e4ff7b2a3ed676c/src/utils/bilibili_api/api/login.py)：二维码、correspond、refresh 与 confirm 的先后顺序。
- [私信协议参考](https://github.com/bromothymolb/bilibili-api-zoku/blob/a0a1566c7c462ab57a3891c3a5f12976cbd11e9e/bilibili_api/session.py)及[评论接口定义](https://github.com/bromothymolb/bilibili-api-zoku/blob/a0a1566c7c462ab57a3891c3a5f12976cbd11e9e/bilibili_api/data/api/common.json)：核对实际发送者、收信方、时间和随机码；没有发现证据不能产生通过结论。
- [NapCat Docker 编排参考](https://github.com/NapNeko/NapCat-Docker/blob/42e4f9e8373c739f4d2ee599980ab8f87cff39b6/compose/ws.yml)：首次登录、管理端口和 Token 仍需配置。

本地 fixture/浏览器检查证明代码行为；生产只读烟测证明当前连接、资料查询和规则运行。真实审批、原生扫码确认、凭证刷新、私信/动态读取和自动移除必须有对应账号/申请的受控回执。发布记录应明确分别完成了哪一层。
