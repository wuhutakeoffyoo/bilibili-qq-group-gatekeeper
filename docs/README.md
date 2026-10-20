# 文档目录与功能核对

本轮于 2026-10-20 对照代码基线 `a974209` 核对。以下文档描述当前实现；生产运行状态需另行查询，不能由本页推断。

## 按任务阅读

| 任务 | 文档 |
| --- | --- |
| 安装、命令、配置字段 | [项目 README](../README.md) |
| 理解配置优先级、迁移全部配置 | [配置来源与迁移](configuration.md) |
| 理解 T/F/I、UID/昵称分流、审核限制 | [审核规则](review-requirements.md) |
| 查看每种拒绝的反馈要求 | [拒绝反馈](rejection-feedback.md) |
| 安装 systemd、升级、备份、恢复 | [部署与升级](deploy-systemd.md) |
| 配置 Cookie/磁盘邮件告警 | [异常状态邮件告警](cookie-email-alert.md) |
| 查看已实现、已移除和待开发功能 | [功能状态与后续规划](development-roadmap.md) |
| 修改代码、交接和验证 | [贡献指南](../CONTRIBUTING.md)、[交接文档](../AI_HANDOFF.md) |
| 理解核心与适配器边界 | [领域说明](../CONTEXT.md)、[SQLite 决策](adr/0001-sqlite-record-storage.md)、[核心抽取决策](adr/0002-framework-independent-core.md) |
| 查看变更 | [更新日志](../CHANGELOG.md) |

## 功能与实现对应

| 功能 | 实现依据 | 已有回归 |
| --- | --- | --- |
| UID 清洗与严格分流 | [parsing.py](../src/gatekeeper/parsing.py)、[bili_api.py](../src/plugins/group_request_manager/bili_api.py) 的 `resolve_user_profile` | [test_bili_identity_routing.py](../tests/test_bili_identity_routing.py) |
| 八项条件、T/F/I、分支和跳转 | [conditions.py](../src/gatekeeper/conditions.py)、[pipeline.py](../src/gatekeeper/pipeline.py) | [test_core_pipeline.py](../tests/test_core_pipeline.py)、[test_property_pipeline.py](../tests/test_property_pipeline.py) |
| 真实事实、管理员补充、完整拒绝文案 | [formatters.py](../src/gatekeeper/formatters.py)、[main.py](../src/plugins/group_request_manager/main.py) 的 `_request_reject_reasons` | [test_core_rejection_feedback.py](../tests/test_core_rejection_feedback.py)、[输入处理烟测](../tests/test_bili_identity_routing.py) |
| 群命令、权限及配置覆盖 | [main.py](../src/plugins/group_request_manager/main.py) 的 `handle_set_group_config`、[config.py](../src/plugins/group_request_manager/config.py) 的 `_load_config` | [test_review_pipeline.py](../tests/test_review_pipeline.py)、[test_decision_recovery.py](../tests/test_decision_recovery.py) |
| pending、身份锁与人工确认 | [decisions.py](../src/gatekeeper/decisions.py)、[request_record.py](../src/plugins/group_request_manager/request_record.py) | [test_decision_recovery.py](../tests/test_decision_recovery.py) |
| WebUI 单文件编辑、鉴权、原子保存 | [webui.py](../src/plugins/group_request_manager/webui.py) | [test_review_pipeline.py](../tests/test_review_pipeline.py)、[test_audit_regressions.py](../tests/test_audit_regressions.py) |
| Cookie 和磁盘邮件 | [cookie_monitor.py](../src/plugins/group_request_manager/cookie_monitor.py)、[email_notifier.py](../src/plugins/group_request_manager/email_notifier.py)、[main.py](../src/plugins/group_request_manager/main.py) | [test_cookie_monitor.py](../tests/test_cookie_monitor.py)、[test_review_pipeline.py](../tests/test_review_pipeline.py) |
| 框架无关核心 | [src/gatekeeper/](../src/gatekeeper/) | [核心独立导入测试](../tests/test_core_imports.py) |

本轮修正了文档中被误写为现有功能的云端日志归档、回复式人工审核、布尔表达式、流程图拖线编辑；修正了命令参数、管理员通知触发条件、配置优先级、邮件环境注入、完整迁移和模拟检查的限制。对应正文均已更新。

## 历史资料

- [2026-06-21 开发目标原文](history/development-roadmap-20260621.md)
- [原始筛选需求](history/review-requirements-original.md)

历史资料保留原文，其中“已完成”和旧文案只代表当时的记录。当前状态以本目录各专题文档及实现为准。
