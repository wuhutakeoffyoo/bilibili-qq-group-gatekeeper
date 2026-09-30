# Bilibili QQ Group Gatekeeper 交接文档（2026-09-28 版）

本文档取代旧版 `AI_HANDOFF.md`（旧版随旧 git 历史一并清除）。
读者对象：接手的 AI。维护者是编程新手，解释请用简单中文，优先说明实际影响、修改效果与验证结果。

> ✅ **git 历史重置已完成（2026-09-28）**：因旧历史含部署配置文件形状与维护者认定的敏感内容，
> 已删除旧 GitHub 仓库并以单提交干净历史重建（敏感扫描结论：全库无 API 密钥、无 Cookie 实值）。
> 本地旧历史（备份分支/stash/reflog）也已物理清除。下方状态是交付时快照，接手时先用 git status 和 git log 实查。
> **维护者仍建议轮换一次 B站 Cookie / SMTP 授权码 / CookieCloud 密钥作为保险**。

## 一、当前状态总览（2026-09-28 交付时快照）

| 位置 | 分支/状态 | 说明 |
| --- | --- | --- |
| `origin/main` / `origin/dev` | 同步 | 复审修复与开源整理已通过 PR #3 合入；接手时以 `git log` 实查最新提交 |
| 本地工作区 | `main` | `origin` 指向新仓库 URL；本地文件夹仍可保留旧名，不影响包名与远端仓库名 |

git 历史以 `062f77d` 为干净新根，之后可继续正常提交。旧线的备份分支、stash、
reflog 已全部物理清除（`gc --prune=now`），旧工作树 `eeed`/`3b13` 已删除。

## 二、2026-09-27~28 完成的修复（旧仓库 PR #12 的内容已纳入干净历史）

### B站搜索加固（`bili_api.py`）
- 搜索接口迁移到 `/x/web-interface/wbi/search/type` 并按官方算法签名：`nav` 接口取密钥（伪装 png URL 取文件名）、进程内缓存 6 小时、双检锁；**密钥获取失败自动回退未签名旧路径**，可用性不降级。
- 签名编码细节：百分号编码大写十六进制、空格编 `%20`、过滤 `!'()*`、手动序列化整体 query（不能让 httpx 二次编码把空格变 `+`）。`tests/test_bili_api_hardening.py` 用官方文档示例做了黄金基准断言。
- **分页完整性**：响应的 `numResults`（总条数，上限 1000）> 第 1 页实际条数且无精确匹配时判 `inaccessible`（不再把分页截断当成"昵称不存在"）；有唯一精确匹配时照常 passed（先短路再查完整性）。
- 502/503/504 重试加随机抖动；修正 `search_user` 过期 docstring。
- 已做**匿名真实链路验证**（无 Cookie 调真实 B站 API，签名搜索返回 code=0）。

### 测试与工程化
- Hypothesis 属性测试（`tests/test_property_pipeline.py`）：T/F/I 汇总不变量（含"有 unknown 不得判 true"红线）、QQ 等级解析、昵称提取（注意 `str.splitlines()` 会按换页符 `\x0c` 等切行）。
- ruff（E/F/W，E501 暂缓——存量约 70 处超长行）+ CI lint 任务；`per-file-ignores` 豁免 tests 的 E402（`nonebot.init()` 必须先于插件导入）。

### 部署
- `deploy/bili-group-gatekeeper.service`（systemd，`Restart=on-failure`，`KillSignal=SIGINT` 优雅退出）+ `docs/deploy-systemd.md`（含单实例检查、升级备份流程、NapCat"假在线"场景说明）。

### 依赖安全（Dependabot 7 条告警 → 4 个 CVE）
- anyio 4.14.0→4.14.2：CVE-2026-63374（critical，TLS 证书仿冒）、CVE-2026-63349（high）、CVE-2026-64847（moderate）
- cryptography 49.0.0→50.0.1：CVE-2026-69247（high，PKCS#7 Bleichenbacher 预言机）；`pyproject.toml` 下限提到 `>=50.0.0`
- 项目仅用 AES-CBC+PKCS7 稳定原语（`cookie_manager.py`），已冒烟验证。过期的 Dependabot PR #5/#7 已关闭，其远端分支已自动删除。

## 三、固定业务规则（重构不可破坏，从旧版交接继承）

- T/F/I 分组管道（T=通过、F=有证据不通过、I=无法判断），**不恢复**旧 `and/or` 布尔表达式规则。
- 组 A（拒绝组，`all_pass`，顺序 no_leave_record→bili_account→bili_level→follow→qq_level）有 F 拒绝、全 T 通过、无 F 有 I 进组 B；组 B（粉丝牌，`any_pass`）F/I 都忽略但原因必须区分（`未持有目标主播粉丝灯牌` / `无法检索目标主播粉丝灯牌`）。
- 未配置/未启用的群不审核；管理命令只响应管理员私聊；群聊不回复；命令英文不区分大小写。
- 用户填的是 B站昵称，**不能称"B站 ID"**（避免误导成 UID）。
- CookieCloud 仅提供可选兼容支持；文档、示例配置与用户提示应按此定位表述，不作为推荐项或默认依赖。
- 网络异常、隐私隐藏、字段缺失、分页不完整一律按 I 处理，不得当成"不满足条件"。
- 昵称唯一性：多个同名精确匹配拒绝；一个精确匹配+其他模糊结果可接受。
- `/检查bili` 不写申请审计、绑定或退群记录。日志本地存储，不自动删除。
- 生产为正常审核模式；`/待确认审批`、`/确认审批` 的 pending 恢复功能保留。

## 四、验证命令

```bash
uv sync --locked
uv run --locked python -m unittest discover -s tests   # 应全过
uv run --locked python -m compileall -q bot.py src tests
uv run ruff check .
git diff --check
```
- 项目用 `unittest`（不是 pytest）。Windows 控制台中文乱码时设 `PYTHONIOENCODING=utf-8`。
- ⚠️ 测试运行时 NoneBot 会读 `Env: prod`（框架既有行为），测试均用 mock/临时目录，但**不要**在测试里访问真实账号或生产数据库。
- 现有测试大量直接 mock `api._get`：给搜索类 mock 的 payload 带 `numResults` 才会走"确认不存在"分支。

## 五、本地安全工具说明

维护者的个人开发环境曾为提交前扫描器的误报配置本地处理。该配置不属于本仓库，也不是项目运行或贡献的前提；接手者应使用自己的安全工具及其默认防护。项目代码中的 `bili_api.py` 使用 MD5 是 Wbi 签名算法的规定步骤，不用于密码存储。

## 六、git 历史重置（2026-09-28 已全部完成）

**敏感扫描结论**（全对象裁决性核查）：**无任何 API 密钥**（sk-/ghp_/AKIA/AIza/Slack/PEM 私钥 0 命中）、**无 B站 Cookie 实值**（SESSDATA/bili_jct 的 17 个历史命中对象全部是测试占位符 `SESSDATA=value; bili_jct=csrf-token`）；`.env.prod` 的 9 个历史版本全部为占位符。维护者记忆中"明文写 Cookie"应属未提交的本地编辑或生产机部署副本，未进过 git。

**已执行**：
- 本地提交前扫描器的已确认误报已处理，新库提交畅通
- 干净单提交 `062f77d` 构建并强推替换远端 main/dev
- 旧 GitHub 仓库已删除（`refs/pull/*` 旧历史缓存随之物理清除）、同名新仓库已重建并推送
- 本地旧历史物理清除：备份分支/stash/reflog 已过期删除（`gc --prune=now`），旧工作树已移除；与其他项目有关的本地文件未纳入此仓库

**保险动作（建议维护者执行）**：B站重新登录一次使旧 session 作废；SMTP 授权码、CookieCloud 密钥同理（扫描虽未见实值，轮换是低成本保险）。生产机（历史 `/opt/bili-group-gatekeeper`）下次维护时重新 clone 干净代码并按 `docs/deploy-systemd.md` 迁移 systemd。

## 七、遗留事项

- Dependabot 告警数量是旧快照；需要当前结论时到 https://github.com/wuhutakeoffyoo/bilibili-qq-group-gatekeeper/security/dependabot 实查
- `main.py` 约 2300 行、主测试文件偏大：可逐步拆分，但**先补测试再动行为**
- NapCat"假在线"（进程在但收不到消息）systemd 管不了，需保持实际消息验证习惯（详见 `docs/deploy-systemd.md` 末节）

## 八、给下一个 AI 的开场建议

1. 先跑第四节验证命令确认基线，再 `git status --short --branch`、`git log -5 --oneline` 核对状态。
2. 历史重置已完成；不要再按旧版交接文档执行删库重传。
3. 修任何问题前先读第三节固定规则；行为修改必须补对应测试。

## 九、2026-09-28 复审修复（已合入主线）

- B站身份或绑定查询未确认时，`no_conflict` / `no_identity_change` 判 I。
- WebUI 以群配置实际解析出的 YAML 路径为准；保存后更新运行中的群规则，重启时也从 YAML 重建阶段列表。
- 粉丝牌条目字段缺失或解析失败时保留 I；已有明确达标粉丝牌仍可判 T。
- 运行时 Cookie 状态文件使用私有权限；systemd 服务设置 `UMask=0077`，部署文档补充已有文件权限处理。
- 对应回归测试在 `tests/test_audit_regressions.py`；本地完整测试、Ruff 和编译已通过。接手时仍需重新运行第四节命令。

## 十、2026-09-28 开源整理（已合入主线）

- GitHub 仓库实查已是 `PUBLIC`；根目录 `LICENSE` 为 MIT，GitHub 许可证接口识别为 `MIT`。不要重复添加另一份许可证。
- 新增公开 `.env.example`；真实 `.env`、`.env.prod` 仍忽略。启动脚本会在缺失时从模板生成，Linux 脚本采用私有 umask。
- `*.local.yaml` 现已忽略，真实群号、目标 UID 和个性化理由应放在本地副本，仓库 YAML 保持占位示例。
- README 已移除过期的第二套 systemd 步骤和不可用的 `uv pip config set` 命令。当前公开部署说明以 `docs/deploy-systemd.md` 为准。
- 维护者已确认新英文名 **Bilibili QQ Group Gatekeeper**，仓库 slug 为 `bilibili-qq-group-gatekeeper`。GitHub 远端仓库、本地元数据和文档均已更名；生产 systemd 单元名与现有 `/opt` 路径保留兼容，禁止同时运行两个审核实例。
- GitHub 私密漏洞报告、secret scanning 与 push protection 已启用；Dependabot 漏洞告警已启用，启用后首次核查没有开放告警。接手时仍需重新查看安全面板，不能把这个快照当作持续无告警的保证。
