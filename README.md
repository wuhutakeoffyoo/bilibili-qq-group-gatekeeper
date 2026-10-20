# Bilibili QQ Group Gatekeeper

基于 NoneBot2、OneBot v11 和 NapCat 的 QQ 群申请管理机器人。

[![CI](https://github.com/wuhutakeoffyoo/bilibili-qq-group-gatekeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/wuhutakeoffyoo/bilibili-qq-group-gatekeeper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

项目仍处于早期阶段，升级前请备份完整 `data/`、`.env`、`.env.prod`、实际使用的群配置及审核流程 YAML，以及服务注入的环境配置。迁移范围见[部署与升级](docs/deploy-systemd.md)。欢迎通过 [Issue](https://github.com/wuhutakeoffyoo/bilibili-qq-group-gatekeeper/issues) 报告问题，或阅读[贡献指南](CONTRIBUTING.md)参与开发。

项目文档：[文档目录与功能核对](docs/README.md) · [更新日志](CHANGELOG.md) · [功能状态与后续规划](docs/development-roadmap.md) · [当前审核规则](docs/review-requirements.md) · [拒绝反馈](docs/rejection-feedback.md)。历史需求原文已单独归档，不作为当前功能说明。

## 功能

### 审核规则

- 分组审核管道：每组直接配置条件列表，并使用 `all_pass` 或 `any_pass` 汇总 T/F/I 状态
- 支持的条件：B站账号确认、B站等级、QQ 等级、关注指定 B站 UID、粉丝牌、无退群记录、账号绑定冲突校验、不更换绑定账号
- 自定义拒绝名单：可配置特定 B站 UID 的黑名单，命中后直接拒绝并附带自定义理由
- 条件审核结果展示：`/检查qq <QQ号>` 可查看申请统计、绑定信息及最近 5 条申请的逐条件结果

### 审核条件

- QQ 等级校验：检查申请者 QQ 等级是否达到设定门槛
- 关注校验：优先通过申请者与 Bot 的 B 站登录账号之间的共同关注检查目标 UID；前提已验证且列表完整时，可快速判定未关注，不确定时回退到关注列表检查（见[设计亮点](#设计亮点)）
- 粉丝牌校验：检查申请者是否拥有指定主播粉丝牌，并满足最低等级
- 退群记录：记录退群用户 QQ、退群时间、退群时昵称
- 退群拦截：可选启用，发现申请者存在退群记录时拒绝或忽略
- 绑定冲突校验（`no_conflict`）：若 B站 UID 已归属其他 QQ，条件为 F，由所在分组决定动作；这不能证明首次申请者拥有该 B站账号
- 换绑校验（`no_identity_change`）：本次 UID 与该 QQ 已持有的绑定不一致时为 F；管理员可使用 `/解除绑定 <QQ号>`，重新申请并获准后再建立绑定
- 账号确认（`bili_account`）：UID/昵称确认成功为 T，确定不存在、格式错误或多个同名精确匹配为 F，接口异常或资料不足为 I

### 管理命令

- `/设置群 <群号> [<参数名> <值> ...]`：配置指定群的审核参数（仅超级管理员）；参数和值可选，不填写时创建基础配置或保留已有配置
- `/查看配置 [<群号>]`：不填写群号时查看全局配置，填写后查看指定群配置
- `/检查qq <QQ号>`：查看指定 QQ 的申请记录、绑定信息与条件结果
- `/检查bili <群号> <B站UID或昵称>`：模拟指定 B站账号的审核，不写申请审计、绑定和退群记录；未提供申请 QQ 的条件为 I
- `/移除退群记录 <QQ号>`：删除指定 QQ 在所有群的退群记录（仅超级管理员）
- `/解除绑定 <QQ号>`：清除指定 QQ 的 B站绑定归属，允许重新绑定
- `/待确认审批`：查看审批调用异常或进程退出后尚未确认结果的审批（仅超级管理员）
- `/确认审批 <审计ID> <applied|failed>`：人工确认实际审批结果（仅超级管理员）

格式说明：`<...>` 为必填值，`[...]` 为可选值。B站昵称包含空格时请使用引号，例如 `/检查bili 123456789 "昵称 带空格"`。

### 申请内容识别

- 明确带 `UID` 标记（如 `UID:123456`、`B站UID：１２３，４５６`）或纯数字的内容，只走 UID 资料接口 `/x/web-interface/card`，不会回退为昵称搜索。
- 标记后的 UID 会清洗全角字符、不可见格式字符、外层引号或括号，以及数字间的空白、逗号和下划线；格式错误时提示检查 UID。
- 其他含文字或符号的字符串按昵称走用户搜索接口，保留昵称原有的数字、符号和内部空格；只有唯一精确匹配才确认账号。
- 加群验证的 `问题：…\n答案：…` 格式先提取答案，再按上述规则分流。
- UID 不存在时提示检查 UID，昵称搜不到时提示检查昵称或改填正确 UID；账号输入相关的拒绝反馈会明确告知“请不要填写B站昵称或UID以外的内容”。接口异常、风控及搜索分页不完整保留为“无法判断”，不冒充“账号不存在”。
- 账号识别成功后，仍按本群配置检查等级、关注、绑定等条件，满足审核规则才会通过。

### 拒绝反馈

拒绝时会说明未通过的具体条件、实际等级或目标要求，以及下一步操作；自定义文案作为补充保留。接口暂时不可用时明确说明“无法核验”，按本群配置的 I 分支处理。详见[全部拒绝情况与反馈要求](docs/rejection-feedback.md)。

### 运维

- Cookie 管理：支持手动设置和扫码登录引导，并提供可选的 CookieCloud 同步支持；Cookie 生效后会检查并自动关注配置中的目标主播
- 本地日志：日志仅保存在本地，不上传云端、不自动清理
- 磁盘空间监控：定期检查剩余空间，过低时私聊通知超级管理员
- 申请审计：每次加群申请记录 QQ、B站昵称、UID、审核结果、条件详情
- 多群配置：支持单群、多群共享、多群分别配置三种方式
- 数据持久化：申请审计、绑定与退群记录保存在本地 SQLite；运行时配置在 `data/runtime/`，环境文件和 YAML 路径由部署配置决定

## 设计亮点

### 通过「共同关注」快速排除不符合关注条件的申请

项目作者在实际群审核场景中发现并落地了这样一条校验链路：让 Bot 使用的 B 站登录账号先关注所有目标 UID，再查询它与申请者的「共同关注」，把共同关注作为目标关注校验的短路路径。

原理是：当登录账号已关注全部目标时，申请者只要关注了任一目标，该目标就应出现在两者的共同关注中。因此，在共同关注列表完整、接口正常且此前提已确认的情况下，未命中任何目标即可判定关注条件不通过，无需继续遍历申请者的完整关注列表。这里判断的是关注条件，最终是否拒绝申请仍由审核分组的 T/F/I 分支配置决定。

实现区分三种情况：

- **命中目标**：共同关注中找到任一目标 UID，直接通过关注校验，不再扫描完整关注列表。
- **完整未命中，且前提已验证**：当前 Cookie 对应的登录账号已确认关注本群全部目标，共同关注列表也已完整读取，直接判定未关注任一目标。
- **无法确定**：登录账号的目标关注状态未确认、接口异常或共同关注列表不完整时，回退到申请者的关注列表；回退后仍无法完整读取且未命中目标时，保留未知状态，交由配置中的 I 分支处理，不把「没查到」等同于「没关注」。

当登录账号的关注范围较集中时，这条链路有机会减少分页请求和等待时间，尤其适合快速排除不符合关注条件的申请。实际收益取决于账号关注规模、分页情况和接口可用性，不承诺固定请求次数或耗时。

Bot 会在启动、设置 Cookie 或修改目标 UID 配置时检查并尝试补齐登录账号的目标关注关系。若从外部取消了登录账号对目标的关注，应重新执行关注检查（例如重启 Bot 或重新设置 Cookie），避免沿用旧的已确认状态。实现见 [`bili_api.py`](src/plugins/group_request_manager/bili_api.py) 的 `check_follow_any_status`，对应回归测试见 [`test_review_pipeline.py`](tests/test_review_pipeline.py) 的 `BiliFollowTests`。

## 目录结构

```text
bilibili-qq-group-gatekeeper/
├── bot.py
├── pyproject.toml
├── start.sh
├── start.ps1
├── .env.example          ← 环境选择模板，可提交
├── .env                  ← 本地环境选择文件，禁止提交
├── .env.prod             ← 生产环境真实配置，禁止提交（已被 .gitignore 忽略）
├── .env.prod.example     ← 生产环境配置模板，无敏感信息，可提交
├── data/
│   ├── records/
│   └── runtime/
└── src/
    ├── gatekeeper/                ← 可独立导入的审核与审批核心
    │   ├── models.py
    │   ├── config.py
    │   ├── conditions.py
    │   ├── pipeline.py
    │   ├── decisions.py
    │   ├── decision_runtime.py
    │   ├── parsing.py
    │   └── formatters.py
    └── plugins/group_request_manager/
        ├── __init__.py
        ├── async_storage.py
        ├── bili_api.py
        ├── bili_runtime.py
        ├── config.py
        ├── cookie_manager.py
        ├── database.py
        ├── decision_adapter.py
        ├── decision_runtime.py
        ├── formatters.py
        ├── leave_record.py
        ├── request_record.py
        ├── user_report.py
        ├── webui.py
        └── main.py
```

`src/gatekeeper/` 提供 T/F/I 条件计算、分组路由、审核轨迹和持久审批服务，
可以在无需初始化 NoneBot 的情况下测试。插件负责事件转换、命令注册与外部接口，
并注入 SQLite 存储、审批动作和通知。领域术语见 [CONTEXT.md](CONTEXT.md)，
架构边界见 [ADR 0002](docs/adr/0002-framework-independent-core.md)。

独立核心测试：`uv run python -m unittest discover -s tests -p 'test_core_*.py'`。

## Linux 部署

### 1. 准备环境

```bash
sudo apt update
sudo apt install -y python3 curl
```

推荐使用 `uv` 管理虚拟环境和依赖。

安装 `uv`：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

下载较慢时，可参考 [uv 官方索引配置文档](https://docs.astral.sh/uv/configuration/indexes/) 为本机设置镜像。不要使用 `uv pip config set`：`uv` 没有这个子命令；配置前也请确认镜像能提供锁文件要求的包。

### 2. 安装依赖

```bash
git clone https://github.com/wuhutakeoffyoo/bilibili-qq-group-gatekeeper.git
cd bilibili-qq-group-gatekeeper
uv sync --locked
```

### 3. 配置环境变量

本项目采用 NoneBot 多环境配置方案，敏感信息与模板分离：

| 文件                  | 是否提交 Git | 作用                                    |
| ------------------- | :------: | ------------------------------------- |
| `.env.example`      |     是    | 环境选择模板（`ENVIRONMENT=prod`） |
| `.env`              |   **否**  | 从模板复制的本地环境选择文件，可按部署需要修改 |
| `.env.prod`         |   **否**  | 生产环境真实配置，存放所有敏感信息（Token、QQ号、Cookie 等） |
| `.env.prod.example` |     是    | 配置模板文件，只保留键名和示例值，供协作者参考需要填写哪些字段       |

<br />

**手动部署时，先从模板生成本地配置：**

```bash
# 1. 选择 prod 环境，使 NoneBot 读取 .env.prod
cp .env.example .env

# 2. 从模板生成真实配置文件
cp .env.prod.example .env.prod
chmod 600 .env.prod

# 3. 编辑 .env.prod，把所有占位值替换为你的真实信息
nano .env.prod
```

`.env.prod` 的重点配置项说明：

- `SUPERUSERS`：机器人超级管理员 QQ，可用所有命令（包括 Cookie 操作），接收异常加群事件通知。支持多个，例如 `["123456789"]` 或 `["123456789","987654321"]`
- `ADMIN_QQS`：普通管理员 QQ，可查看配置、检查记录和解除绑定，不可执行 `/设置群`、Cookie 操作及审批恢复命令。多个可用逗号分隔，例如 `123456789,987654321`
- `NOTIFY_ADMINS_ON_ABNORMAL_REQUEST`：阶段分支包含 `effects: [notify_admin]` 时的收件人模式。`all` = 超级管理员和普通管理员；`superusers_only` = 仅超级管理员（默认）；`none` = 关闭阶段通知。仅命中退群或绑定冲突不会自动推送；磁盘及审批异常通知不由此项关闭
- `ONEBOT_WS_URLS`：NapCat OneBot v11 WebSocket 地址，例如 `["ws://127.0.0.1:3001/onebot/v11/ws"]`
- `ONEBOT_ACCESS_TOKEN`：NapCat 中配置的访问令牌
- 群审核配置支持单群和多群两种写法：
  - 共享同一套规则：使用 `GROUP_ID=111111111,222222222`，其余 `GROUP_ENABLED`、`GROUP_TARGET_UIDS` 等字段会同时应用到这些群
  - 分别配置每个群：使用 `GROUP_1_ID`、`GROUP_2_ID` 等编号格式字段
  - 详细示例见下文的 `## .env.prod 群配置参数说明`
- `BILI_COOKIE`：可选，B 站 Cookie；也可以之后用 `/设置cookie` 写入。已配置 CookieCloud 时，还支持自动同步
- `COOKIECLOUD_HOST`：可选，CookieCloud 服务地址；仅在使用该同步功能时填写
- `COOKIECLOUD_UUID`：可选，CookieCloud UUID
- `COOKIECLOUD_KEY`：可选，CookieCloud 密钥

基础配置（一般无需修改）：

- `DRIVER`：NoneBot 驱动，默认 `~httpx+~websockets`
- `LOG_LEVEL`：日志级别，默认 `INFO`
- `NICKNAME`：机器人昵称，默认 `["群管"]`
- `COMMAND_START`：命令前缀，默认 `["/", ""]`
- `COMMAND_SEP`：命令分隔符，默认 `["."]`

**CookieCloud 可选支持：**

[CookieCloud](https://github.com/easychen/CookieCloud) 是一个独立的浏览器 Cookie 同步工具。本项目仅提供可选的兼容支持；使用该工具的用户可以配置对应服务地址、UUID 和密钥以同步 B 站 Cookie。示例配置中的三项默认留空。

**B 站 Cookie 的配置与登录方式：**

| 方式 | 说明 |
| --- | --- |
| 手动粘贴 | 在 `.env.prod` 的 `BILI_COOKIE` 中直接填入 Cookie 字符串，或使用 `/设置cookie` 命令 |
| 扫码登录引导 | 私聊机器人发送 `/登录二维码` 生成登录链接；扫码后仍需将 Cookie 写入配置或通过 `/设置cookie` 提供给 Bot |
| CookieCloud（可选支持） | 已配置 CookieCloud 时，可在启动时自动同步，或使用 `/获取cookie` 命令同步 |

说明：

- 所有管理指令都只对 `SUPERUSERS` 和 `ADMIN_QQS` 中配置的 QQ 私聊响应
- Bot 启动、`/设置cookie` 或 `/获取cookie` 使 Cookie 生效时，会检查登录账号是否关注所有群配置中的 `target_uids`；明确未关注时自动关注
- 自动关注需要 Cookie 包含 `bili_jct`；检查或关注失败只记录日志并返回摘要，不会阻止 Bot 启动
- 群聊中发送这些命令不会返回结果
- 重启加载时，非空 `BILI_COOKIE` 环境配置会覆盖运行时 Cookie。使用 `/设置cookie` 后若希望重启仍使用新值，应同步更新旧环境值，或清空环境值并保留 `data/runtime/plugin_state.json`
- 邮件和 Cookie 检查周期使用进程环境变量；单独写进 `.env.prod` 不会自动导出。详见[邮件配置](docs/cookie-email-alert.md)

### 4. 配置 NapCat

在 NapCat 中启用 OneBot v11 WebSocket，并确保地址与 `.env.prod` 中一致，例如：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

### 5. 启动机器人

```bash
chmod +x start.sh
./start.sh
```

缺少 `uv` 时脚本会尝试安装。首次创建 `.env.prod` 时询问超级管理员 QQ、OneBot 地址和 Token；已有配置时直接复用。脚本不会代填群号、目标 UID 或 B站 Cookie，仍需完成这些配置。自动化部署可跳过问答：

```bash
./start.sh --quick
```

快速模式只跳过问答；缺少配置时仍只生成占位模板，不代表已经具备可用的审核配置。

也可以手动启动（先按上文创建本地配置）：

```bash
uv run --locked python bot.py
```

生产环境建议使用 systemd 托管（崩溃自动拉起、开机自启），见 [docs/deploy-systemd.md](docs/deploy-systemd.md)。
为兼容已有部署，systemd 单元名暂时仍是 `bili-group-gatekeeper.service`，无需因仓库改名而新建第二个服务。

## Windows 部署

Windows 环境下可直接用 `start.ps1` 一键启动。

### Windows首次运行

如果遇到"无法加载脚本"的权限错误，先以管理员身份运行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 启动

```powershell
# 方式 1：在项目目录下右键 "使用 PowerShell 运行"
# 方式 2：终端执行
.\start.ps1
```

`start.ps1` 会通过 `uv` 自动完成：创建虚拟环境 → 安装依赖 → 启动机器人。

缺少 `uv` 时会尝试安装；仅在首次创建 `.env.prod` 时询问 QQ、OneBot 地址和 Token。群配置和 Cookie 需另行填写。无需问答时可使用：

```powershell
.\start.ps1 -Quick
```

也可以手动启动（先创建本地 `.env` 和 `.env.prod`）：

```powershell
uv run --locked python bot.py
```

## 命令

以下所有命令均仅支持管理员私聊，群聊中发送不会响应。

| 命令                     | 可用权限          | 说明                      |
| ---------------------- | ------------- | ----------------------- |
| `/help`（别名 `/帮助`） | 超级管理员 / 普通管理员 | 查看所有可用指令 |
| `/查看配置 [<群号>]`         | 超级管理员 / 普通管理员 | 查看全局或指定群的配置             |
| `/设置群 <群号> [<参数> <值> ...]` | 仅超级管理员    | 配置加群审核参数                |
| `/移除退群记录 <QQ号>`        | 仅超级管理员        | 删除该 QQ 在所有群的退群记录        |
| `/解除绑定 <QQ号>`          | 超级管理员 / 普通管理员 | 清除该 QQ 当前的 B站 绑定归属      |
| `/检查qq <QQ号>`            | 超级管理员 / 普通管理员 | 查看该 QQ 的申请与绑定记录         |
| `/检查bili <群号> <B站UID或昵称>` | 超级管理员 / 普通管理员 | 模拟审核；不写申请、绑定和退群记录 |
| `/设置cookie <B站Cookie>` | 仅超级管理员        | 手动设置 B 站 Cookie         |
| `/获取cookie`            | 仅超级管理员        | 从 CookieCloud 同步 Cookie |
| `/登录二维码`               | 仅超级管理员        | 生成 B 站扫码登录二维码           |
| `/待确认审批`               | 仅超级管理员        | 查看尚未确认实际结果的审批         |
| `/确认审批 <审计ID> <applied\|failed>` | 仅超级管理员 | 核对 QQ 群结果后恢复自动审批       |

### 权限说明

- 超级管理员：`.env.prod` 中 `SUPERUSERS` 字段配置的 QQ
  - 可使用所有命令
  - 接收异常加群事件通知
- 普通管理员：`.env.prod` 中 `ADMIN_QQS` 字段配置的 QQ
  - 可查看配置、检查用户与记录、解除绑定；不能执行 `/设置群`
  - 不可操作 B 站 Cookie 等敏感信息
  - 默认不接收异常加群事件通知；仅当 `NOTIFY_ADMINS_ON_ABNORMAL_REQUEST=all` 时接收

`/设置群` 接受以下参数名，每个参数后必须有一个值：

- `enable on/off`：启用或禁用该群
- `required_qq_level 等级`：设置 QQ 最低等级
- `required_bili_level 等级`：设置 B站最低等级
- `target_uids uid1,uid2`：设置需要关注的 B 站 UID
- `target_medal_uids uid1,uid2`：设置目标主播 UID
- `required_medal_level 等级`：设置粉丝牌最低等级
- `reject_reason_with_leave_time on/off`：拒绝原因是否附带退群时间
- `reject_reasons JSON`：按条件补充管理员说明，例如 `{"qq_level":"本群设有等级门槛"}`；程序同时保留实际测得值、要求及处理建议

JSON 作为一个参数传入时，用外层单引号保留 JSON 的双引号，例如：

```text
/设置群 123456789 reject_reasons '{"qq_level":"本群设有等级门槛"}'
```

`custom_reject_uids`、`review_pipeline_file` 及审核分组通过 YAML 或环境配置设置，当前不是 `/设置群` 支持的参数。命令更新项保存在 `data/runtime/group_overrides.json`；保存失败时原配置继续生效，重启时仍可能被显式 `GROUP_*` 环境字段覆盖。

`/检查bili` 不接收申请 QQ，因此 QQ 等级、退群记录和绑定关系按 I 参与模拟；其输出不能代替真实申请结果或用于确认一次 pending 审批。

## .env.prod 群配置参数说明

### 推荐方式

```env
GROUP_CONFIG_FILE=groups.yaml
```

推荐将群基础配置和审核流转分别写到独立 YAML。仓库提供无敏感信息的示例：

- `groups.yaml`
- `review_pipeline.yaml`

实际部署时建议复制为被 Git 忽略的 `groups.local.yaml` 和 `review_pipeline.local.yaml`，在前者中设置 `review_pipeline_file: review_pipeline.local.yaml`，并在 `.env.prod` 中设置 `GROUP_CONFIG_FILE=groups.local.yaml`。真实群号、目标 UID 和个性化拒绝理由可能暴露运营信息，不要直接修改并提交示例文件。

`groups.yaml` 示例：

```yaml
defaults:
  enabled: true
  required_qq_level: 4
  required_bili_level: 2
  reject_reason_with_leave_time: false
  # 自动生成具体未通过的条件；可按条件补充管理员说明。
  reject_reasons: {}
  review_pipeline_file: review_pipeline.yaml

groups:
  "123456789":
    target_uids: [987654321]
    target_medal_uids: [987654321]
    required_medal_level: 1
    custom_reject_uids:
      "987654321": 请输入你自己的 B站昵称，而不是目标主播的昵称

  "987654321":
    enabled: false
```

`review_pipeline.yaml` 示例：

```yaml
version: 2
entry: reject_group_a
groups:
  - id: reject_group_a
    name: 筛选组A（拒绝组）
    mode: all_pass
    conditions: [no_leave_record, bili_account, bili_level, follow, qq_level]
    on_true:
      flow: approve
    on_false:
      flow: reject
    on_unknown:
      flow: next
      goto: approve_group_b
  - id: approve_group_b
    name: 筛选组B（通过组）
    mode: any_pass
    conditions: [medal]
    on_true: approve
    on_false: ignore
    on_unknown: ignore

```

### 兼容方式

如果你暂时不想切到 `groups.yaml`，仍可继续使用扁平环境变量：

```env
GROUP_ID=123456789
GROUP_ENABLED=true
GROUP_REQUIRED_QQ_LEVEL=32
GROUP_REQUIRED_BILI_LEVEL=2
GROUP_TARGET_UIDS=987654321
GROUP_TARGET_MEDAL_UIDS=987654321
GROUP_REQUIRED_MEDAL_LEVEL=10
GROUP_REVIEW_PIPELINE_FILE=review_pipeline.yaml
```

也仍支持编号格式的多群配置：

```env
GROUP_1_ID=111111111
GROUP_1_ENABLED=true
GROUP_1_REQUIRED_QQ_LEVEL=32
GROUP_1_TARGET_UIDS=987654321
GROUP_1_REVIEW_PIPELINE_FILE=review_pipeline_group1.yaml

GROUP_2_ID=222222222
GROUP_2_ENABLED=false
GROUP_2_REVIEW_PIPELINE_FILE=review_pipeline_group2.yaml
```

注意：

- `GROUP_CONFIG_FILE` 是当前推荐入口，适合长期维护
- `GROUP_ID=111,222` 这种写法表示这些群共用同一套 `GROUP_*` 配置
- `GROUP_1_ID=...` 这种写法表示每个群有自己独立的一套配置
- 如果同时配置了 `GROUP_CONFIG_FILE` 和扁平环境变量，后者会覆盖前者的同名字段
- 完整合并顺序、命令覆盖文件与路径规则见[配置来源与迁移](docs/configuration.md)

### 参数含义

| 参数                                    | 类型               | 说明                                                             |
| ------------------------------------- | ---------------- | -------------------------------------------------------------- |
| `GROUP_CONFIG_FILE`                   | 文件路径             | 推荐配置方式；指向独立 `groups.yaml`，用于定义多个群的基础审核参数、自定义拒绝名单、以及默认/专属审核流程文件 |
| `GROUP_ENABLED`                       | `true` / `false` | 启用该群的审核逻辑；`false` 则完全关闭                                        |
| `GROUP_REQUIRED_QQ_LEVEL`             | 数字               | QQ 最低等级要求                                                      |
| `GROUP_REQUIRED_BILI_LEVEL`           | 数字               | B站最低等级要求；当审核管道引用 `bili_level` 时必须配置为大于 0 的值                  |
| `GROUP_TARGET_UIDS`                   | 逗号分隔的数字          | 需要关注的 B 站 UID，例如 `123456,234567`                               |
| `GROUP_TARGET_MEDAL_UIDS`             | 逗号分隔的数字          | 要检查的主播 UID                                                     |
| `GROUP_REQUIRED_MEDAL_LEVEL`          | 数字               | 粉丝牌最低等级要求                                                      |
| `GROUP_REJECT_REASON_WITH_LEAVE_TIME` | `true` / `false` | 拒绝时是否附带退群时间                                                    |
| `GROUP_REVIEW_PIPELINE_FILE`          | 文件路径             | 推荐配置方式；指向独立 YAML 审核分组文件，支持命名分组、`goto` 跳转、`effects` 副作用         |
| `GROUP_CUSTOM_REJECT_UIDS`            | JSON 字符串         | 自定义拒绝名单，例如 `{"123456":"请填写你本人的账号"}`；确认 UID 后命中则直接拒绝 |

说明：

- 推荐优先使用 `GROUP_CONFIG_FILE` 加载 `groups.yaml`；它适合集中维护群号、基础门槛、自定义拒绝名单和审核流程文件路径
- `groups.yaml` 支持 `defaults + groups` 结构；群内字段会覆盖默认字段
- 使用 `GROUP_REVIEW_PIPELINE_FILE` 加载 YAML 审核分组；未配置管道时不会启用回退表达式
- YAML 分组支持命名 `id`、可选 `entry`、以及分支对象写法：`flow` / `goto` / `effects` / `reason`
- 当某个 YAML 分组分支的流转动作为 `next` 时，可通过 `goto` 指向其他分组；未填写则默认进入顺序下一个分组
- 每组使用 `conditions` 直接列出参与审核的条件，不支持 `and`、`or` 或括号表达式
- `mode: all_pass` 是“一票否决”式聚合，只决定分组结果：任一条件为 F 则分组结果为 false；无 F 但存在 I 则为 unknown；全部 T 才为 true
- `mode: any_pass` 是“一票通过”式聚合，只决定分组结果：任一条件为 T 则分组结果为 true；无 T 但存在 I 则为 unknown；全部 F 才为 false
- 分组结果对应的实际动作由 `on_true` / `on_false` / `on_unknown` 的 `flow` 决定，可配置为 `approve` / `reject` / `ignore` / `next`
- `no_identity_change` 的含义是“同一 QQ 不允许改绑到别的 B站 UID”；如需更换绑定，请先使用 `/解除绑定 <QQ号>`
- 多个 `GROUP_TARGET_UIDS` 之间仍是“满足任意一个即可”关系；多个 `GROUP_TARGET_MEDAL_UIDS` 之间同理
- 如果开启 QQ 等级校验但接口没有返回等级字段、返回无效值，或明确标记 `isHideQQLevel`，机器人会按无法判断处理并继续执行后续校验；未隐藏时返回的 `0` 才视为有效 0 级，并按最低等级门槛正常判断
- QQ 等级获取：优先通过 `get_stranger_info` 接口读取；若用户设置了等级不可见导致接口无返回，还会尝试从加群请求事件中提取
- `review_pipeline_file` 若写在 `groups.yaml` 里，相对路径会按该 `groups.yaml` 所在目录解析
- 仓库自带的 YAML 仅是占位示例；实际部署配置请使用 `*.local.yaml` 或其他已忽略的路径

### YAML 配置字段详解

#### groups.yaml

| 字段                                          | 层级       | 类型                  | 说明                                |
| ------------------------------------------- | -------- | ------------------- | --------------------------------- |
| `defaults`                                  | —        | 对象                  | 所有群共享的兜底默认值；群内同名字段会覆盖此处的值         |
| `defaults.enabled`                          | defaults | `true` / `false`    | 默认是否启用审核                          |
| `defaults.required_qq_level`                | defaults | 数字                  | 默认最低 QQ 等级                        |
| `defaults.required_bili_level`              | defaults | 数字                  | 默认最低 B站等级                         |
| `defaults.target_uids`                      | defaults | 数字列表                | 默认关注的 B站 UID                      |
| `defaults.target_medal_uids`                | defaults | 数字列表                | 默认检查粉丝牌的主播 UID                    |
| `defaults.required_medal_level`             | defaults | 数字                  | 默认粉丝牌最低等级                         |
| `defaults.reject_reason_with_leave_time`    | defaults | `true` / `false`    | 拒绝时是否附带退群时间                       |
| `defaults.reject_reasons`                   | defaults | 键值对                 | 按条件补充 F 拒绝说明，实际事实及要求仍保留 |
| `defaults.review_pipeline_file`             | defaults | 文件路径                | 默认审核流程文件；相对路径按 groups.yaml 所在目录解析 |
| `groups`                                    | —        | 对象                  | 各群独立配置，key 为群号字符串                 |
| `groups.<群号>.enabled`                       | 群        | `true` / `false`    | 该群是否启用审核                          |
| `groups.<群号>.required_qq_level`             | 群        | 数字                  | 该群最低 QQ 等级                        |
| `groups.<群号>.required_bili_level`           | 群        | 数字                  | 该群最低 B站等级                         |
| `groups.<群号>.target_uids`                   | 群        | 数字列表                | 关注的 B站 UID，例如 `[123456, 234567]`  |
| `groups.<群号>.target_medal_uids`             | 群        | 数字列表                | 检查粉丝牌的主播 UID                      |
| `groups.<群号>.required_medal_level`          | 群        | 数字                  | 粉丝牌最低等级                           |
| `groups.<群号>.reject_reason_with_leave_time` | 群        | `true` / `false`    | 拒绝时是否附带退群时间                       |
| `groups.<群号>.custom_reject_uids`            | 群        | 键值对                 | 自定义拒绝名单：key 为 B站 UID，value 为拒绝理由  |
| `groups.<群号>.reject_reasons`                | 群        | 键值对                 | 替换该群的补充说明映射，不隐藏具体原因 |
| `groups.<群号>.review_pipeline_file`          | 群        | 文件路径                | 该群专属审核流程文件；覆盖 defaults 中的值        |

#### review\_pipeline.yaml

| 字段                    | 层级 | 类型     | 说明                                |
| --------------------- | -- | ------ | --------------------------------- |
| `version`             | —  | 数字     | 配置格式版本，目前固定为 `2`                  |
| `entry`               | —  | 字符串    | 审核入口分组 `id`；可选，不填则从第一个分组开始        |
| `groups`              | —  | 列表     | 审核分组列表，按定义顺序分配内部编号                |
| `groups[].id`         | 分组 | 字符串    | 分组唯一标识，用于 `goto` 跳转               |
| `groups[].name`       | 分组 | 字符串    | 分组显示名称，出现在审核轨迹和 `/检查bili` 输出中   |
| `groups[].mode`       | 分组 | 字符串    | `all_pass`（全部通过）或 `any_pass`（任一通过） |
| `groups[].conditions` | 分组 | 字符串列表  | 参与该组汇总及原因展示的条件；不代表按此顺序逐个请求接口 |
| `groups[].on_true`    | 分组 | 对象或字符串 | 条件为 `true` 时的动作；支持简写（直接写动作名）或完整写法 |
| `groups[].on_false`   | 分组 | 对象或字符串 | 条件为 `false` 时的动作                  |
| `groups[].on_unknown` | 分组 | 对象或字符串 | 条件无法判断时的动作                        |

每个分支的完整写法：

| 字段        | 类型    | 说明                                                |
| --------- | ----- | ------------------------------------------------- |
| `flow`    | 字符串   | 流转动作（必填）：`next` / `approve` / `reject` / `ignore` |
| `goto`    | 字符串   | 跳转目标分组 `id`；仅当 `flow=next` 时有效，可选                 |
| `effects` | 字符串列表 | 附加动作：`notify_admin` / `log_warn`；可选               |
| `reason`  | 字符串   | 阶段说明；拒绝时会同时补充实际条件及处理提示，不会遮盖事实 |

### 查看配置

```text
/查看配置 [<群号>]
```

### 检查某个 QQ 的申请记录

```text
/检查qq <QQ号>
```

## 本地日志与磁盘告警

日志按天写入本地 `logs/` 目录，可通过 `LOCAL_LOG_DIRECTORY` 修改路径。日志不会上传到 WebDAV 或其他云端，也不会由程序自动删除。程序启动时会检查一次日志目录所在磁盘，之后每 6 小时检查一次；剩余空间低于 1 GB 时通知 `SUPERUSERS`，低于 0.2 GB 时发送严重告警，由管理员决定如何处理磁盘空间。

配置 SMTP 进程环境后，磁盘告警也会发送邮件；同类成功通知会去重、发送失败会在后续周期重试。详见[异常状态邮件告警](docs/cookie-email-alert.md)。

## 审核流程 WebUI

```env
WEBUI_ENABLED=true
WEBUI_HOST=127.0.0.1
WEBUI_PORT=1128
WEBUI_TOKEN="请替换为至少32字符的强随机字符串"
WEBUI_CUSTOM_CSS_FILE="webui-custom.css"
WEBUI_REQUIRE_HTTPS=true
WEBUI_TRUST_PROXY_HEADERS=true
WEBUI_SESSION_TIMEOUT_SECONDS=3600
WEBUI_LOGIN_MAX_ATTEMPTS=5
WEBUI_LOGIN_WINDOW_SECONDS=900
```

通过 HTTPS 域名访问 WebUI，在登录页输入 `WEBUI_TOKEN`。不要把 Token 放在 URL 中；查询参数 Token 已不再支持。界面支持 YAML 编辑、分组表单、新增、删除、拖拽排序和流程预览；保存前会校验分组、条件、模式和跳转目标，再原子替换 `review_pipeline.yaml`。保存成功后，使用该文件的群会立即对新收到的申请采用更新后的流程；已开始处理的申请按其已读取的流程继续。

当前 WebUI 只能编辑一个实际审核 YAML；多个群可共享该文件。配置了多个不同 `review_pipeline_file` 时不能使用当前 WebUI。界面编辑的是条件列表、模式和 `goto` 目标，不支持布尔表达式或拖线改跳转，也不编辑 `groups.yaml` 的群基础参数。

### 公网安全部署

WebUI 内置了短时服务端会话、`HttpOnly`/`SameSite=Strict` Cookie、CSRF 校验、登录失败限流、请求体大小限制、严格 CSP、防 iframe、安全响应头和恒定时间 Token 比较。公网部署仍必须遵循以下边界：

- `WEBUI_HOST` 保持 `127.0.0.1`，不要直接把 Python HTTP 服务监听到公网网卡
- 使用 Nginx、Caddy 或同等级反向代理终止 TLS，只开放 HTTPS 端口
- 公网环境设置 `WEBUI_REQUIRE_HTTPS=true` 和 `WEBUI_TRUST_PROXY_HEADERS=true`
- 反向代理必须覆盖而不是透传客户端提供的 `X-Forwarded-For`、`X-Forwarded-Proto`
- 防火墙禁止外部直接访问 `1128`，仅允许本机反向代理连接
- `WEBUI_TOKEN` 至少 32 字符，并与其他服务密码不同；可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成
- 建议在反向代理层再增加 IP 白名单、VPN/Zero Trust 或第二层身份认证，并定期更新依赖

Nginx 代理头示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:1128;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

当 `WEBUI_REQUIRE_HTTPS=true` 时，未携带可信 `X-Forwarded-Proto: https` 的请求会收到 `426`。WebUI 只允许监听 `localhost`、`127.0.0.1` 或 `::1`；配置非回环地址时会直接拒绝启动，避免客户端绕过反向代理伪造转发头。

### 自定义 WebUI 样式

在项目根目录创建 CSS 文件，例如 `webui-custom.css`，然后在 `.env.prod` 中设置：

```env
WEBUI_CUSTOM_CSS_FILE="webui-custom.css"
```

相对路径以程序启动时的工作目录（通常是项目根目录）为基准，也可以填写绝对路径。自定义 CSS 在内置样式之后加载，因此可以直接覆盖默认变量和选择器。修改 CSS 后刷新浏览器即可生效，无需重启 Bot；清空该配置并重启后恢复默认样式。

常用 CSS 变量和选择器：

- `:root`：`--ink` 文字色、`--paper` 页面底色、`--accent` 强调色、`--line` 连线色、`--card` 卡片色
- `body`：页面背景与全局字体
- `header`、`h1`：标题区域
- `main`：左右栏布局
- `.panel`：编辑器和流程区域的面板
- `.node`：流程分组卡片
- `textarea`：YAML 编辑器
- `button`、`button.alt`、`.remove`：按钮样式

示例：

```css
:root {
  --ink: #17212b;
  --paper: #eef4f7;
  --accent: #c84b31;
  --line: #7393a7;
  --card: #ffffff;
}

body {
  background: linear-gradient(135deg, #d9e8ed, #f8f3e8 65%);
  font-family: "Noto Sans SC", sans-serif;
}

.panel {
  border-radius: 10px;
  box-shadow: 0 14px 36px #17332d20;
}

.node {
  border-left-width: 10px;
}
```

自定义 CSS 端点使用登录会话鉴权。严格 CSP 默认阻止 CSS 从外部域名加载资源；仍请只使用可信的本地 CSS。

## 数据持久化

- 运行时配置：`data/runtime/plugin_state.json`
- `/设置群` 的字段覆盖：`data/runtime/group_overrides.json`
- 申请审计、QQ/B站绑定和退群记录：`data/records/gatekeeper.sqlite3`
- 旧版记录：首次启动时会自动从 `data/records/join_request_records.json` 和 `data/records/leave_records/*.json` 导入 SQLite；导入完成后旧文件保持不变，作为回滚备份保留

SQLite 使用 WAL 和事务保证本地写入一致性。运行中可能同时存在
`gatekeeper.sqlite3-wal` 与 `gatekeeper.sqlite3-shm`，不要只复制主数据库文件进行在线备份。
最简单可靠的备份方法是先停止 Bot，再按[全量迁移清单](docs/configuration.md#全量迁移清单)备份数据、环境文件、实际 YAML 及服务环境文件。

数据库结构通过版本号自动升级。执行同意或拒绝前，Bot 会先写入一条 `pending`
审计；QQ 接口确认成功后标记为 `applied`，明确返回失败时标记为 `failed`，无需调用 QQ 接口的忽略记录标记为
`not_required`。每个 OneBot 加群请求还会保存唯一请求键，重复推送不会再次执行审批；正在处理的重复事件会等待原审批完成。
如果数据库不可写，Bot 会安全地
停止自动审批并通知超级管理员，避免出现“已经操作但没有记录”的情况。

如果审批调用超时、断连或被取消，Bot 会保留 `pending` 并暂停新的自动同意和拒绝，因为 QQ 操作可能已经生效。
如果 Bot 在 QQ 操作前后异常退出，重启后检测到 `pending` 也会暂停新的自动同意和拒绝。
超级管理员先用 `/待确认审批` 查看记录并核对群内实际结果，再执行
`/确认审批 <审计ID> applied` 或 `/确认审批 <审计ID> failed`；全部确认后自动恢复。

B站检查复用 HTTP 连接，最多同时执行 4 个完整检查，每个检查总时限为 60 秒。
接口限流、持续异常或超时按 I 处理，最终动作由 `on_unknown` 决定；如果该分支配置 `reject`，反馈会说明暂无法核验，而不是断言账号不存在或未关注。

升级或重装时，除完整 `data/` 外，还需迁移实际环境文件、YAML 及服务注入的环境配置，并保留原路径关系。只复制 `data/` 无法保证全量配置迁移。

## 参与贡献与安全

- 开发环境、测试命令和 Pull Request 要求见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，不要创建公开 Issue
- 提交日志或截图前，务必删除 Cookie、Token、QQ 号、群号、邮箱和服务器地址

## 许可证

本项目采用 [MIT License](LICENSE)。你可以使用、修改和分发本项目，但需要保留版权和许可证声明。
