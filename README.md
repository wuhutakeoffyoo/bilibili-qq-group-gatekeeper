# Bili Group Gatekeeper

基于 NoneBot2、OneBot v11 和 NapCat 的 QQ 群申请管理机器人。

[![CI](https://github.com/wuhutakeoffyoo/bili-group-gatekeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/wuhutakeoffyoo/bili-group-gatekeeper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

项目仍处于早期阶段，升级前请备份本地 `data/` 和 `.env.prod`。欢迎通过 [Issue](https://github.com/wuhutakeoffyoo/bili-group-gatekeeper/issues) 报告问题，或阅读 [贡献指南](CONTRIBUTING.md) 参与开发。

## 功能

### 审核规则

- 分组审核管道：每组直接配置条件列表，并使用 `all_pass` 或 `any_pass` 汇总 T/F/I 状态
- 支持的条件：B站账号搜索、B站等级、QQ 等级、关注指定 B站 UID、粉丝牌、无退群记录、无冒用、不更换身份
- 自定义拒绝名单：可配置特定 B站 UID 的黑名单，命中后直接拒绝并附带自定义理由
- 条件审核结果展示：`/检查qq <QQ号>` 可查看每次申请的逐条件通过/未通过详情

### 审核条件

- QQ 等级校验：检查申请者 QQ 等级是否达到设定门槛
- 关注校验：优先通过申请者与 Bot 登录账号的共同关注检查目标 UID；未命中或接口异常时自动降级到关注列表检查
- 粉丝牌校验：检查申请者是否拥有指定主播粉丝牌，并满足最低等级
- 退群记录：记录退群用户 QQ、退群时间、退群时昵称
- 退群拦截：可选启用，发现申请者存在退群记录时拒绝或忽略
- 防冒用检测（`no_conflict`）：若不同 QQ 重复使用已登记的 B站 UID，由所在分组的 F 分支决定后续动作
- 防换绑检测（`no_identity_change`）：同一 QQ 不允许更换到其他 B站 UID；可使用 `/解除绑定 <QQ号>` 后重新绑定
- 未搜到 B站账号处理：由审核分组中 `bili_account` 条件的 F 分支决定

### 管理命令

- `/设置群 <群号> [<参数名> <值> ...]`：配置指定群的审核参数（仅超级管理员）；参数和值可选，不填写时创建基础配置或保留已有配置
- `/查看配置 [<群号>]`：不填写群号时查看全局配置，填写后查看指定群配置
- `/检查qq <QQ号>`：查看指定 QQ 的申请记录、绑定信息与条件结果
- `/检查bili <群号> <B站昵称>`：检查指定 B站昵称在指定群的审核结果，不写入持久化数据
- `/移除退群记录 <QQ号>`：删除指定 QQ 在所有群的退群记录（仅超级管理员）
- `/解除绑定 <QQ号>`：清除指定 QQ 的 B站绑定归属，允许重新绑定
- `/待确认审批`：查看进程异常退出后尚未确认结果的审批（仅超级管理员）
- `/确认审批 <审计ID> <applied|failed>`：人工确认实际审批结果（仅超级管理员）

格式说明：`<...>` 为必填值，`[...]` 为可选值。B站昵称包含空格时请使用引号，例如 `/检查bili 123456789 "昵称 带空格"`。

### 运维

- Cookie 管理：支持手动设置、CookieCloud 自动同步和扫码登录引导；Cookie 生效后会检查并自动关注配置中的目标主播
- 本地日志：日志仅保存在本地，不上传云端、不自动清理
- 磁盘空间监控：定期检查剩余空间，过低时私聊通知超级管理员
- 申请审计：每次加群申请记录 QQ、B站昵称、UID、审核结果、条件详情
- 多群配置：支持单群、多群共享、多群分别配置三种方式
- 数据持久化：申请审计、绑定与退群记录保存在本地 SQLite，配置文件保存在 `data/` 目录

## 目录结构

```text
bili-group-gatekeeper/
├── bot.py
├── pyproject.toml
├── start.sh
├── start.ps1
├── .env                  ← 仅声明当前环境名，无敏感信息，可提交
├── .env.prod             ← 生产环境真实配置，禁止提交（已被 .gitignore 忽略）
├── .env.prod.example     ← 生产环境配置模板，无敏感信息，可提交
├── data/
│   ├── records/
│   └── runtime/
└── src/plugins/group_request_manager/
    ├── __init__.py
    ├── async_storage.py
    ├── bili_api.py
    ├── bili_runtime.py
    ├── config.py
    ├── cookie_manager.py
    ├── database.py
    ├── formatters.py
    ├── leave_record.py
    ├── request_record.py
    ├── user_report.py
    ├── webui.py
    └── main.py
```

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

**国内用户建议配置 PyPI 镜像源**，否则 `uv sync` 可能下载很慢甚至超时。常用镜像：

| 镜像      | 配置命令                                                                          |
| ------- | ----------------------------------------------------------------------------- |
| 清华 tuna | `uv pip config set --user index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| 阿里云     | `uv pip config set --user index-url https://mirrors.aliyun.com/pypi/simple/`  |
| 中科大     | `uv pip config set --user index-url https://pypi.mirrors.ustc.edu.cn/simple/` |

也可以手动写入 uv 配置文件 `~/.config/uv/uv.toml`：

```toml
[pip]
index-url = "https://pypi.tuna.tsinghua.edu.cn/simple"
```

### 2. 安装依赖

```bash
cd bili-group-gatekeeper
uv sync --locked
```

### 3. 配置环境变量

本项目采用 NoneBot 多环境配置方案，敏感信息与模板分离：

| 文件                  | 是否提交 Git | 作用                                    |
| ------------------- | :------: | ------------------------------------- |
| `.env`              |     是    | 仅声明当前使用哪个环境（`ENVIRONMENT=prod`），无敏感信息 |
| `.env.prod`         |   **否**  | 生产环境真实配置，存放所有敏感信息（Token、QQ号、Cookie 等） |
| `.env.prod.example` |     是    | 配置模板文件，只保留键名和示例值，供协作者参考需要填写哪些字段       |

<br />

**初次部署只需要两步：**

```bash
# 1. 从模板生成真实配置文件
cp .env.prod.example .env.prod

# 2. 编辑 .env.prod，把所有占位值替换为你的真实信息
nano .env.prod
```

`.env.prod` 的重点配置项说明：

- `SUPERUSERS`：机器人超级管理员 QQ，可用所有命令（包括 Cookie 操作），接收异常加群事件通知。支持多个，例如 `["123456789"]` 或 `["123456789","987654321"]`
- `ADMIN_QQS`：普通管理员 QQ，可用群配置、记录查看等常规命令，不可操作 Cookie 类敏感信息；只有在 `NOTIFY_ADMINS_ON_ABNORMAL_REQUEST=all` 时才会接收异常加群事件通知。多个可用逗号分隔，例如 `123456789,987654321`
- `NOTIFY_ADMINS_ON_ABNORMAL_REQUEST`：异常加群事件通知模式。异常加群事件仅指“疑似冒用他人 B 站账号”和“命中退群记录”。`all` = 通知 `SUPERUSERS` 和 `ADMIN_QQS`；`superusers_only` = 仅通知 `SUPERUSERS`（默认）；`none` = 不通知任何人
- `ONEBOT_WS_URLS`：NapCat OneBot v11 WebSocket 地址，例如 `["ws://127.0.0.1:3001/onebot/v11/ws"]`
- `ONEBOT_ACCESS_TOKEN`：NapCat 中配置的访问令牌
- 群审核配置支持单群和多群两种写法：
  - 共享同一套规则：使用 `GROUP_ID=111111111,222222222`，其余 `GROUP_ENABLED`、`GROUP_TARGET_UIDS` 等字段会同时应用到这些群
  - 分别配置每个群：使用 `GROUP_1_ID`、`GROUP_2_ID` 等编号格式字段
  - 详细示例见下文的 `## .env.prod 群配置参数说明`
- `BILI_COOKIE`：可选，B 站 Cookie；也可以之后用 `/设置cookie` 写入，或通过 CookieCloud 自动同步（推荐）
- `COOKIECLOUD_HOST`：CookieCloud 服务地址
- `COOKIECLOUD_UUID`：CookieCloud UUID
- `COOKIECLOUD_KEY`：CookieCloud 密钥

基础配置（一般无需修改）：

- `DRIVER`：NoneBot 驱动，默认 `~httpx+~websockets`
- `LOG_LEVEL`：日志级别，默认 `INFO`
- `NICKNAME`：机器人昵称，默认 `["群管"]`
- `COMMAND_START`：命令前缀，默认 `["/", ""]`
- `COMMAND_SEP`：命令分隔符，默认 `["."]`

**关于 CookieCloud：**

[CookieCloud](https://github.com/easychen/CookieCloud) 是一个独立的浏览器 Cookie 同步工具。推荐用户使用它在浏览器和服务器之间自动同步 B 站 Cookie，无需手动复制粘贴。

**获取 B 站 Cookie 的三种方式：**

| 方式              | 说明                                             |
| --------------- | ---------------------------------------------- |
| CookieCloud（推荐） | 浏览器安装 CookieCloud 插件并配置，自动同步，无需手动操作            |
| 手动粘贴            | 在 `.env.prod` 的 `BILI_COOKIE` 中直接填入 Cookie 字符串 |
| 扫码登录            | 私聊机器人发送 `/登录二维码` 生成登录链接；扫码后仍需通过 CookieCloud 或 `/设置cookie` 将 Cookie 同步给 Bot |

说明：

- 所有管理指令都只对 `SUPERUSERS` 和 `ADMIN_QQS` 中配置的 QQ 私聊响应
- Bot 启动、`/设置cookie` 或 `/获取cookie` 使 Cookie 生效时，会检查登录账号是否关注所有群配置中的 `target_uids`；明确未关注时自动关注
- 自动关注需要 Cookie 包含 `bili_jct`；检查或关注失败只记录日志并返回摘要，不会阻止 Bot 启动
- 群聊中发送这些命令不会返回结果

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

首次运行会自动安装 `uv`、创建 `.env.prod` 并询问必填项。自动化部署可跳过问答：

```bash
./start.sh --quick
```

也可以手动启动：

```bash
uv run python bot.py
```

生产环境建议使用 systemd 托管（崩溃自动拉起、开机自启），见 [docs/deploy-systemd.md](docs/deploy-systemd.md)。

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

首次运行还会自动安装 `uv`、创建 `.env.prod` 并询问必填项。无需问答时可使用：

```powershell
.\start.ps1 -Quick
```

也可以手动启动：

```powershell
.\venv\Scripts\Activate.ps1
python bot.py
```

## 命令

以下所有命令均仅支持管理员私聊，群聊中发送不会响应。

| 命令                     | 可用权限          | 说明                      |
| ---------------------- | ------------- | ----------------------- |
| `/help`                | 超级管理员 / 普通管理员 | 查看所有可用指令                |
| `/查看配置 [<群号>]`         | 超级管理员 / 普通管理员 | 查看全局或指定群的配置             |
| `/设置群 <群号> [<参数> <值> ...]` | 仅超级管理员    | 配置加群审核参数                |
| `/移除退群记录 <QQ号>`        | 仅超级管理员        | 删除该 QQ 在所有群的退群记录        |
| `/解除绑定 <QQ号>`          | 超级管理员 / 普通管理员 | 清除该 QQ 当前的 B站 绑定归属      |
| `/检查qq <QQ号>`            | 超级管理员 / 普通管理员 | 查看该 QQ 的申请与绑定记录         |
| `/检查bili <群号> <B站昵称>` | 超级管理员 / 普通管理员 | 检查某 B站昵称的审核结果（不持久化）   |
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
  - 可使用群配置、记录查看和用户检查命令
  - 不可操作 B 站 Cookie 等敏感信息
  - 默认不接收异常加群事件通知；仅当 `NOTIFY_ADMINS_ON_ABNORMAL_REQUEST=all` 时接收

可用参数：

- `enable on/off`：启用或禁用该群
- `required_qq_level 等级`：设置 QQ 最低等级
- `required_bili_level 等级`：设置 B站最低等级
- `target_uids uid1,uid2`：设置需要关注的 B 站 UID
- `target_medal_uids uid1,uid2`：设置目标主播 UID
- `required_medal_level 等级`：设置粉丝牌最低等级
- `reject_reason_with_leave_time on/off`：拒绝原因是否附带退群时间
- `reject_reasons JSON`：按条件自定义拒绝原因，例如 `{"bili_account":"未搜索到账号","qq_level":"QQ等级过低"}`
- `custom_reject_uids JSON`：自定义拒绝名单，JSON 格式的键值对；key 为要拒绝的 B站 UID，value 为该 UID 对应的拒绝理由；可以定义多个。例如：`{"123456":"疑似主播本人ID","654321":"已知违规用户"}`
- `/解除绑定 <QQ号>`：清除该 QQ 当前持有的 B站绑定归属；解除后再次申请并填写新的 B站昵称时，会重新建立绑定
- `/检查bili <群号> <B站昵称>`：返回逐条件检查结果和最终判定，不会写入任何持久化数据

## .env.prod 群配置参数说明

### 推荐方式

```env
GROUP_CONFIG_FILE=groups.yaml
```

推荐将群基础配置写到独立 `groups.yaml`，把审核流转写到 `review_pipeline.yaml`。\
项目根目录提供了两个可直接使用的示例文件（不含敏感信息，可提交至 Git）：

- `groups.yaml`
- `review_pipeline.yaml`

`groups.yaml` 示例：

```yaml
defaults:
  enabled: true
  required_qq_level: 4
  required_bili_level: 2
  reject_reason_with_leave_time: false
  reject_reasons:
    bili_account: 未搜索到你填写的B站昵称，请不要输入与b站昵称无关的字段并检查你的昵称是否输入有误
    follow: 你没有关注主播,本群为粉丝群,禁止非粉丝加入,若你确定已经关注了主播,请检查你填写的b站昵称是否正确
    bili_level: 你的B站等级过低,疑似机器人账号
    qq_level: 你的Q等级过低,疑似机器人账号
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
| `GROUP_CUSTOM_REJECT_UIDS`            | JSON 字符串         | 自定义拒绝名单，例如 `{"123456":"疑似主播本人ID"}`；命中直接拒绝                      |

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
- YAML 文件不含敏感信息，可直接提交至 Git

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
| `defaults.reject_reasons`                   | defaults | 键值对                 | 按条件名自定义 F 状态的拒绝原因                 |
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
| `groups.<群号>.reject_reasons`                | 群        | 键值对                 | 覆盖该群各条件 F 状态的拒绝原因                 |
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
| `groups[].conditions` | 分组 | 字符串列表  | 该分组按顺序检查的条件名列表                    |
| `groups[].on_true`    | 分组 | 对象或字符串 | 条件为 `true` 时的动作；支持简写（直接写动作名）或完整写法 |
| `groups[].on_false`   | 分组 | 对象或字符串 | 条件为 `false` 时的动作                  |
| `groups[].on_unknown` | 分组 | 对象或字符串 | 条件无法判断时的动作                        |

每个分支的完整写法：

| 字段        | 类型    | 说明                                                |
| --------- | ----- | ------------------------------------------------- |
| `flow`    | 字符串   | 流转动作（必填）：`next` / `approve` / `reject` / `ignore` |
| `goto`    | 字符串   | 跳转目标分组 `id`；仅当 `flow=next` 时有效，可选                 |
| `effects` | 字符串列表 | 附加动作：`notify_admin` / `log_warn`；可选               |
| `reason`  | 字符串   | 覆盖默认原因文案；可选                                       |

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

通过 HTTPS 域名访问 WebUI，在登录页输入 `WEBUI_TOKEN`。不要把 Token 放在 URL 中；查询参数 Token 已不再支持。界面支持 YAML 编辑、分组表单、新增、删除、拖拽排序和流程预览；保存前会校验分组、条件、模式和跳转目标，再原子替换 `review_pipeline.yaml`。

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
- 申请审计、QQ/B站绑定和退群记录：`data/records/gatekeeper.sqlite3`
- 旧版记录：首次启动时会自动从 `data/records/join_request_records.json` 和 `data/records/leave_records/*.json` 导入 SQLite；导入完成后旧文件保持不变，作为回滚备份保留

SQLite 使用 WAL 和事务保证本地写入一致性。运行中可能同时存在
`gatekeeper.sqlite3-wal` 与 `gatekeeper.sqlite3-shm`，不要只复制主数据库文件进行在线备份。
最简单可靠的备份方法是先停止 Bot，再完整备份 `data/` 目录和 `.env.prod`。

数据库结构通过版本号自动升级。执行同意或拒绝前，Bot 会先写入一条 `pending`
审计；QQ 接口完成后再标记为 `applied` 或 `failed`，无需调用 QQ 接口的忽略记录标记为
`not_required`。每个 OneBot 加群请求还会保存唯一请求键，重复推送不会再次执行审批。
如果数据库不可写，Bot 会安全地
停止自动审批并通知超级管理员，避免出现“已经操作但没有记录”的情况。

如果 Bot 在 QQ 操作前后异常退出，重启后检测到 `pending` 会暂停新的自动同意和拒绝。
超级管理员先用 `/待确认审批` 查看记录并核对群内实际结果，再执行
`/确认审批 <审计ID> applied` 或 `/确认审批 <审计ID> failed`；全部确认后自动恢复。

B站检查复用 HTTP 连接，最多同时执行 4 个完整检查，每个检查总时限为 60 秒。
接口限流、持续异常或超时会按“无法判断”处理，不会因为网络问题直接拒绝申请。

升级或重装时保留 `data/` 目录即可延续已有配置和记录。

## 参与贡献与安全

- 开发环境、测试命令和 Pull Request 要求见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，不要创建公开 Issue
- 提交日志或截图前，务必删除 Cookie、Token、QQ 号、群号、邮箱和服务器地址

## 许可证

本项目采用 [MIT License](LICENSE)。你可以使用、修改和分发本项目，但需要保留版权和许可证声明。

## systemd 部署

创建服务文件：

```bash
sudo nano /etc/systemd/system/qqgroup-request-manager.service
```

写入以下内容，并替换路径和用户：

```ini
[Unit]
Description=Bili Group Gatekeeper Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/bili-group-gatekeeper
ExecStart=/path/to/bili-group-gatekeeper/venv/bin/python /path/to/bili-group-gatekeeper/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable qqgroup-request-manager
sudo systemctl start qqgroup-request-manager
sudo systemctl status qqgroup-request-manager
```
