# 配置来源与迁移

配置仍使用环境文件、YAML 和运行时 JSON；SQLite 存放申请、绑定、退群记录。当前没有把全部配置迁入数据库，也没有内置版本快照或一键回滚。

## 群配置加载顺序

[ConfigManager._load_config](../src/plugins/group_request_manager/config.py) 按以下顺序合并，后加载的来源可覆盖同一群的对应字段：

1. `data/runtime/plugin_state.json`；该文件不存在时，才兼容读取 `data/config/plugin_config.json`。
2. `GROUP_CONFIG_FILE` 指向的 YAML；每个群先应用 `defaults`，再应用该群字段。YAML 中出现的群配置会替换运行时快照中的同群对象。
3. `data/runtime/group_overrides.json`，保存 `/设置群` 修改的字段。
4. 兼容的 `GROUP_CONFIGS` JSON 环境配置。
5. `GROUP_ID` 和同组 `GROUP_*` 字段。
6. `GROUP_1_ID`、`GROUP_2_ID` 等编号配置。
7. 根据最终 `review_pipeline_file` 重新编译审核阶段；不沿用 JSON 中的旧阶段快照。

同一来源内，`reject_reasons` 和 `custom_reject_uids` 是整份映射替换，不是逐键深合并。`GROUP_ID=111,222` 让多群共用字段；编号格式可分别配置各群。未启用或未配置的群不审核。

显式群环境字段可能在重启时覆盖命令修改。因此“命令刚刚修改成功”不能说明重启后仍会采用该值；应检查是否还保留了同名 `GROUP_*` 配置。

## 命令会保存什么

`/设置群` 支持：`enable`、`required_qq_level`、`required_bili_level`、`target_uids`、`target_medal_uids`、`required_medal_level`、`reject_reason_with_leave_time`、`reject_reasons`。

它只把实际修改的字段写入 `group_overrides.json`，不修改基础 YAML；保存成功后才替换运行中的群配置。保存失败时原配置继续生效。只输入群号可创建基础配置，但不会自动生成可用的审核管道。

`custom_reject_uids` 和 `review_pipeline_file` 不属于该命令参数，应在 YAML 或环境配置中维护。WebUI 编辑实际审核流程 YAML，不编辑群基础 YAML。

JSON 参数应加外层单引号，例如：

```text
/设置群 123456789 reject_reasons '{"qq_level":"本群设有等级门槛"}'
```

## 路径规则

| 配置 | 相对路径基准 |
| --- | --- |
| `GROUP_CONFIG_FILE` | Bot 启动工作目录 |
| 群 YAML 中的 `review_pipeline_file` | 该群 YAML 所在目录 |
| 扁平环境变量中的 `GROUP_REVIEW_PIPELINE_FILE` | Bot 启动工作目录 |
| `LOCAL_LOG_DIRECTORY`、`WEBUI_CUSTOM_CSS_FILE` | Bot 启动工作目录 |
| 内置 `data/` | Bot 启动工作目录 |

例如，`GROUP_CONFIG_FILE=data/config/groups.local.yaml` 配合该文件中的 `review_pipeline_file: review.local.yaml`，实际流程文件为 `data/config/review.local.yaml`。

命令覆盖文件可能保存已经解析的绝对流程路径。更换发布目录时，必须检查这些路径是否仍指向期望文件；不能只把字符串复制过去就认定迁移正确。

## Cookie 与其他全局配置

- NoneBot 根据 `.env` 的 `ENVIRONMENT=prod` 读取 `.env.prod`。systemd 的环境变量、`EnvironmentFile=` 和启动器注入也属于实际配置来源。
- 启动加载时，非空 `BILI_COOKIE` 会覆盖运行时 JSON 中的 Cookie。`/设置cookie` 会更新当前内存及运行时文件；若旧环境值没有更新，重启后会重新采用旧值。
- `ADMIN_QQS`、通知模式及 WebUI 参数也可由环境配置覆盖运行时状态。
- `NOTIFY_SMTP_*`、`NOTIFY_FROM_ADDR`、`NOTIFY_TO_ADDR` 和 `COOKIE_CHECK_INTERVAL_SECONDS` 由 `os.getenv` 直接读取，需要注入进程环境；不能只依赖 NoneBot 解析 `.env.prod`。详见[邮件配置](cookie-email-alert.md)。

## 全量迁移清单

| 范围 | 应迁移内容 |
| --- | --- |
| 环境选择与连接 | `.env`、`.env.prod`，以及实际使用的其他环境文件 |
| 群基础与审核规则 | `GROUP_CONFIG_FILE` 的真实文件、每个群实际引用的流程 YAML，保留相对目录关系 |
| 运行时配置 | 完整 `data/runtime/`，尤其是 Cookie 状态与群覆盖文件 |
| 业务记录 | 完整 `data/records/`，包括 SQLite 及保留的旧 JSON |
| 旧配置与扩展状态 | 完整 `data/config/` 和其余现有 `data/` 内容 |
| 服务环境 | systemd 单元、drop-in、所有 `EnvironmentFile=` 文件及启动器注入值 |
| 可选外部文件 | 自定义 CSS、外置 YAML、其他配置引用文件；有符号链接时核对实际目标 |
| 诊断日志 | 按运维需要另行保留实际日志目录，程序不会自动归档或删除 |

停 Bot 后复制整个 `data/` 最简单。在线备份必须使用 SQLite 一致性备份接口；只复制主数据库会遗漏 WAL 中尚未归并的记录。

迁移后比较：群数量与启用状态、每个门槛和目标 UID、全部分支及跳转、通知/邮件/WebUI 配置、Cookie 来源、原始配置文件；再比较数据库表结构、行数和完整行内容。路径变化应按旧新应用根目录归一后比较，同时确认文件实际存在。不要在公开日志中输出凭据或原始业务记录。

具体切换与回退流程见[部署与升级](deploy-systemd.md)。
