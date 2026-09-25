# PlanPilot 开发日志(主日志)

按时间正序。**新条目一律追加在文件末尾**,规则见本目录 README.md。
标 ✓ 的数字为当事轮次实测复现;标 ⚠ 的为历史报告记载、本日志编写时未重跑。

---

## 2026-09-11 之前 — 合同阶段(v1.0 → v1.8)

- 类型:决策 / 实现
- 证据:`E:/PlanPilot-Hackathon/contract-review/`(V1.7、V1.8 changelog、
  adversarial probes、negative_control_v18);`PlanPilot_v1.2` ~ `v1.7` 版本目录;
  `OFFICIAL_ALIGNMENT.md`(对账官方题面与 rubric)
- 内容:合同从 v1.0 迭代到 v1.8,期间建立了对抗审查惯例
  (8 路变异全部必须被抓、600 静态断言、398 独立检查)。
  A1 纠偏:赛道名 "Product Planning" 系自 v1.0 起的转写错误,在 v1.8 修正。
  最终合同 SHA-256 `b92e53f4ff054105…`,此后所有实现阶段以此为身份锚。

## 2026-09-11 21:11 — 实现仓库 bootstrap

- 类型:实现
- 证据:commit `040d427`(Kiro workspace:steering/hooks/specs 由合同生成)、
  `4bd3ccd`(.gitattributes 钉 LF——合同哈希是身份承载,换行符即事故)、
  `5306b74`(requirements.txt 与合同自带 pins diff 验证一致)
- 内容:`planpilot-build` git 仓建立,分支 `master`。生成器保证
  steering 文档跟随合同重生成,手写指导语的修改视为无效。

## 2026-09-12 09:16–16:40 — spec 1 `plan-store-and-digest` 实现 + 外部审计第一轮(F1–F15)

- 类型:实现 / 审查
- 证据:commit `e2dea98`(阶段1-4)、`04fe31a`(负控+证据包)、
  `98ee9a8`(IMPLEMENTATION_NOTES.md 诞生)、`c88c083`、`1e3cc17`、
  `79777fc`、`17128fd`、`b07ac9f`(REVIEW_PROMPT.md)
- 内容:确定性 plan store + 规范化 digest 落地。**教训事件**:`e2dea98`
  声称缺陷"已记录在 REVIEW notes"而该文件不存在——`98ee9a8` 补写,
  从此"声称即证据"成为硬规则。第一轮外部审计 15 条发现(F1–F15),
  含 3 个独立攻击发现的守卫旁路,全部修复;第二轮审计 P0-1(缺 schema
  校验)、P0-2(版本检查是同义反复)修复并钉住(`3f13874`、`d8b6cc2`
  逐任务实质复核,4 个假勾选被纠正)。

## 2026-09-13 02:26–04:24 — 第三轮审计:权限旁路 + 持久化缺口

- 类型:审查 / 修复
- 证据:commit `5b23e5c`、`8ddbb2d`、`9d106c0`;IMPLEMENTATION_NOTES.md
  "Third external audit" 节
- 内容:1 个权限旁路(P0)、3 个持久化缺口,外加修复过程暴露的 2 个
  次生缺陷(含负控脚本的陈旧字节码 bug)。

## 2026-09-13 12:08 — 第四、五轮对抗审计:lifecycle 权威与 direct-SUPERSEDED 自锁

- 类型:审查 / 修复
- 证据:commit `fc908d7`;FOURTH/FIFTH_AUDIT_REMEDIATION_REPORT.md
  (两份均已标注"被下一轮取代",审计轨迹保留)
- 内容:第四轮发现 lifecycle 权威路径与失效不持久;第五轮发现
  direct-SUPERSEDED 路线自锁持久化。D20 机制声称改为可运行验证。

## 2026-09-13 19:28 — 第六轮审计与 `plan-store-and-digest` v1 基线 `c7c06ea`

- 类型:修复 / 证据
- 证据:commit `c7c06ea`(tag `plan-store-and-digest-v1-baseline`,
  annotated,剥 `^{}` 后指向本 commit ✓)、SIXTH_AUDIT_REMEDIATION_REPORT.md、
  `tests/evidence/plan-store-and-digest/EVIDENCE.json`
- 内容:三项修复(均来自独立审查轮):
  P1 负控源变异改为**临时副本沙箱**(SIGKILL 复现测试确认:强杀后真实
  工作树 0 污染、无 `# MUTATION:` 残留);P2 孤立代理对字符改抛
  `CanonicalizationError`(不再泄漏 UnicodeEncodeError);P3 `-0.0` 归一
  (digest 不再区分 0 与 -0)。EVIDENCE.json 新增 `signed_zero`、
  `isolated_surrogate`、`working_repository_unchanged` 断言。
- 审查方复核 ✓:unit 418、full 421、negctl 56 caught / 0 escaped /
  0 broken of 58、workspace 190/0 + 14/14、factcheck fails=0、
  合同 SHA 未动。

## 2026-09-14 08:56 — spec 2 `approval-service` v1 基线 `069f9f8`

- 类型:实现 / 审查
- 证据:commit `069f9f8`(tag `approval-service-v1-baseline` ✓ 指向正确),
  分支 `approval-service` 自 `c7c06ea` 切出;APPROVAL_SERVICE_HANDOFF.md、
  `tests/evidence/approval-service/EVIDENCE.json`、新 factcheck 工具
  `tools/factcheck_approval_handoff.py`
- 内容:确定性审批服务(审批集合生命周期、过期、replay/conflict、
  篡改加载拒绝)。词表守卫扩至 23 词表。
- 审查方复核 ✓:approval unit 39、变异 13/13 caught、full 461、
  vocab PASS、factcheck(新工具)fails=0。
- 审查方六路攻击探针:5 路干净;**发现 1(P2)**——commit_new_version
  产生的 supersede 事件在旧 lifecycle 从未进入 AWAITING_APPROVAL 时
  `approval_set_id=None`,`consume_superseded` 跳过作废,僵尸集合谎报
  APPROVED(store 层状态机兜住了发布,属纵深缺口而非直接可利用)。
  发现 2(P3):promised due date 变更信任 validator,记录为已知限制。
  发现 3(P3):旧 `factcheck_impl_handoff.py` 行数宣称过期(fails=15,
  无实义)+ 中文设计稿未跟踪。

## 2026-09-14 09:36 — 中文设计稿入库

- 类型:证据
- 证据:commit `9d2de86`(docs/PLANPILOT_AGENT_DESIGN_ZH.md)
- 内容:关闭第八轮 P3 的未跟踪文件部分。

## 2026-09-14 09:42 — `approval-service` v1.0.1 硬化:僵尸集合作废 `0cf1060`

- 类型:修复 / 审查
- 证据:commit `0cf1060`(tag `approval-service-v1.0.1-hardening` ✓),
  APPROVAL_SERVICE_HARDENING_REPORT.md
- 内容:按第八轮发现 1 修复。`consume_superseded` 事件 set_id 为空时按
  `(plan_id, plan_version, plan_digest)` 反查 `_set_by_binding` 作废;
  两条权威路径同时给出不同 set_id 时抛 `ApprovalInvariantError`
  (失败关闭,不选择性相信)。+1 回归测试 +1 变异防线(删 fallback
  一行必须被抓)。
- 审查方复核 ✓:approval unit 40、变异 14/14 caught 0 escaped、
  full 462、vocab PASS 23、合同 SHA `b92e53f4…` 未动、工作树干净、
  负控后 `src/` 零 `# MUTATION:` 残留;独立僵尸探针复演六连全过
  (APPROVED→consume 1 事件→旧集合持久 INVALIDATED→重复 consume 幂等)。
- **驳回审查方一条误报(P1 级流程教训)**:第九轮曾报
  `plan-store-and-digest-v1-baseline` 指错 commit——实为 annotated tag
  未加 `^{}` 后缀、比到 tag 对象 SHA(`f8b5af7`)的审查方工具错误。
  团队拒绝按该建议 `git tag -f` 重写已审计基线是**正确决定**;
  规则固化:检查 annotated tag 一律 `rev-parse <tag>^{}`。

## 2026-09-14(当日) — 决策:开发日志制度建立

- 类型:决策
- 证据:本文件夹(`docs/devlog/`)+ 本次 commit
- 内容:发现官方提交清单(OFFICIAL_ALIGNMENT A9:GitHub URL / 30min 视频 /
  Write-up PDF / 部署证据)未单列开发日志,但 write-up 的"show your work"
  与内部证据标准(设计稿 608 行:普通应用日志不是验收证据)都要求一份
  可重建时间线的过程记录。素材散在 8+ 份文件、SESSION_STATE.md 已停更
  (9/12),故立此单一主日志 + README 同步规则(五类触发条件、条目格式、
  永不改写历史)。今后任何 src/tests/contract 改动、commit/tag、决策、
  审查 finding、证据重生成,当次追加。

## 2026-09-14 10:30 — 全组审查意见落定 + git bundle 备份完成(对上一条目口径的更正)

- 类型:决策 / 更正 / 证据
- 证据:`D:/PlanPilot_backups/planpilot-build_all_20260914.bundle`
  (411 KB,`git bundle verify` = okay,含全部 6 refs:2 branches +
  3 annotated tags + HEAD,完整历史);本次 commit
- 更正一:**不存在"7 个 part"的路线**。正式规划是 `.kiro/specs/README.md`
  的 **15 个 spec**(依合同 implementation_migration)。此前审查报告中
  "3/7"的分母是我口头聚合、无仓库依据,作废。当前已审计完成:
  foundation、plan-store-and-digest、approval-service;其余按 15-spec 表推进。
- 更正二:"negctl 4 passed" 是 **pytest 外层 wrapper 测试数**,不是变异数。
  准确口径:全部负控 wrapper 4 passed;其中 approval-service
  **14/14 mutations caught**,store 为既有 58 路 mutation control
  (56 caught / 0 escaped / 0 broken)。
- 决策三:tool-error-middleware 与 publisher **不合并交付**。publisher 依赖
  audit chain、decision trace、PlanStore、ApprovalService、幂等事务,
  合并会造成单次审查范围过大。保留独立 spec 与独立验收边界,顺序:
  **tool-error-middleware → audit-hash-chain / decision-traces →
  publisher 事务**。
- 口径补充:approval-service"无未闭环发现"成立,但
  `change_promised_due_date` 的确定性来源验证仍是
  independent-validator / tool-integration 的**后续验收项**,
  不属本模块遗留缺陷。
- 最终结论修订定稿:`approval-service v1.0.1-hardening` 审查通过,
  commit `0cf1060` 及 tag 可作为下一阶段开发基线;**本结论仅覆盖审批服务,
  不代表 publisher、工具层或端到端 EVAL 已就绪。**

## 2026-09-14 10:42 — 下一 Part 自包含交接包建立

- 类型:决策 / 文档
- 证据:`.kiro/specs/tool-error-middleware/{README,design,tasks,START_PROMPT}.md`;
  本条目所在 commit 可用
  `git log -1 --format=%H -- .kiro/specs/tool-error-middleware` 解析
- 内容:为另一名组员和全新 Codex/Kiro task 建立 Design-First 交接入口。
  spec 钉住 18 个错误 details/retryability、UUIDv4 correlation、最终 UTF-8
  4096-byte 限制、可信异常白名单、未知异常清洗、output-before-commit 与
  staged transaction、deadline 和 solver exhaustion 分界、无隐藏 retry、
  observer seam、临时副本 mutation control、证据与 tag 验收。明确不把
  publisher、audit hash chain、decision trace persistence 或八个 public handler
  混入本 Part。`START_PROMPT.md` 是可直接交给执行 Agent 的完整启动指令。
- 附:bundle 首备完成,D 盘落地。远程仓库(gh CLI 未装)仍为待办。

## 2026-09-15 14:40 · 第 10 轮审查:队友包 E:\PlanPilot-Team-20260915(EVAL 撤回整改)

- 审查人:Hermes(独立复核,非转述)。对象:无 `.git` 的交付包,149 文件 manifest。
- 实测复现 ✓:全仓 534 passed(153.67s);negctl 4 wrappers passed;
  EVAL gate `0/0/30 BLOCKED` 退出码 2、acceptance_claimed=false;smoke 8/0;
  词表 PASS;契约 SHA `b92e53f4…` 未动;manifest 147/149 哈希匹配
  (2 个不匹配是我重跑 gate 重生成的时间戳 EVIDENCE,非交付缺陷)。
- 核心结论:**整改只做了 P0-1(EVAL 撤回),P0-2/3/4 未动一行代码**:
  api_server 仍走 persistence.Database(实测拒绝签名错误但接受
  `{"totally_made_up": true}`、孤立 surrogate 裸 UnicodeEncodeError、
  -0.0/0.0 digest 不同);contract_adapter :32 `calendar_window_ids:[]`、
  HC 仅 001/002;framework_error 无 message/无 UUIDv4;src/planpilot/tools 不存在。
- 来源比对:包内 store/approval/domain 共 10 文件,9 个与审计基线 `0cf1060`
  逐字节一致,plan_store.py = 审计版 +15 行时间戳校验(更严,良性);
  test_approval_service.py 与审计版逐字节一致。审批测试计 38 个 `def test_`
  (审计口径 40 含参数化展开,一致)。
- 修订:上一轮 setup_local.ps1 "UTF-8 修复未落地" 判定有误——实际以
  `python -X utf8`(等效 PYTHONUTF8)钉住安装路径,requirements 非 ASCII
  仅注释区,修复成立;仅 `pip list > $env:TEMP` 旁路未钉,极低风险,不阻塞。
- 判决:**不得并入,不得作为 v1.8 实现或 EVAL 成绩交付**;EVAL 撤回部分
  予以确认(factcheck 21 项中 20 PASS,唯一 FAIL 为上因我复跑所致)。
- 证据:本报告全部命令在 .venv-runtime 实测;src 污染检查 0;
  `# MUTATION:` 残留 0。

---

# 里程碑速览

| 基线 | commit | tag | 关键数字(独立复核 ✓) |
|---|---|---|---|
| plan-store-and-digest v1 | `c7c06ea` | ✓ 剥离后正确 | 418/421 ✓,negctl 56/0/0 ✓ |
| approval-service v1 | `069f9f8` | ✓ | 39 unit,13/13 变异,461 full ✓ |
| approval v1.0.1 硬化 | `0cf1060` | ✓ | 40 unit,14/14 变异,462 full ✓ |
| 合同锚 | — | — | SHA `b92e53f4…` 全程未动 ✓ |

# 尚未开始(诚实清单)

- tool-error-middleware(**下一 part**;主办方 Ollama 网关 tool_calls 恒空、
  已知伪造 tool result——工具结果溯源(provenance)必须是硬需求)
- audit-hash-chain / decision-traces → publisher 事务(独立 spec、独立验收,
  按此顺序,不与 middleware 合并)
- 其余 15-spec 表模块:lot-and-material、calendar-and-shifts、cpsat-scheduler、
  escalation-and-budget、independent-validator、inference-client、
  ui-approval-queue、lightsail-deploy
- 数据集(dataset-migration,critical path)+ EVAL-001~030
  (官方评分的实际战场;9/28 截止前须留 ≥5 整天)
- ~~git bundle 备份~~ ✅ 已完成(2026-09-14,D 盘);私有远程仓库仍为待办
---

# 里程碑速览

| 基线 | commit | tag | 关键数字(独立复核 ✓) |
|---|---|---|---|
| plan-store-and-digest v1 | `c7c06ea` | ✓ 剥离后正确 | 418/421 ✓,negctl 56/0/0 ✓ |
| approval-service v1 | `069f9f8` | ✓ | 39 unit,13/13 变异,461 full ✓ |
| approval v1.0.1 硬化 | `0cf1060` | ✓ | 40 unit,14/14 变异,462 full ✓ |
| 合同锚 | — | — | SHA `b92e53f4…` 全程未动 ✓ |

# 尚未开始(诚实清单)

- tool-error-middleware(**下一 part**;主办方 Ollama 网关 tool_calls 恒空、
  已知伪造 tool result——工具结果溯源(provenance)必须是硬需求)
- audit-hash-chain / decision-traces → publisher 事务(独立 spec、独立验收,
  按此顺序,不与 middleware 合并)
- 其余 15-spec 表模块:lot-and-material、calendar-and-shifts、cpsat-scheduler、
  escalation-and-budget、independent-validator、inference-client、
  ui-approval-queue、lightsail-deploy
- 数据集(dataset-migration,critical path)+ EVAL-001~030
  (官方评分的实际战场;9/28 截止前须留 ≥5 整天)
- ~~git bundle 备份~~ ✅ 已完成(2026-09-14,D 盘);私有远程仓库仍为待办

## 2026-09-18 02:21 — P0-5 八工具统一错误中间件完成

- 类型:实现 / 修复 / 证据 / 更正
- 证据:`src/planpilot/tools/`、`tests/unit/test_tool_error_middleware.py`、
  `tests/negative_control/test_tool_error_middleware_negctl.py`、
  `P0_5_TOOL_ERROR_MIDDLEWARE_REPORT_20260918.md`；专项 35 passed、单元
  595 passed、全量 600 passed、负向变异 11 caught / 0 escaped / 0 broken、
  封闭词表及其 self-test PASS、workspace 190 ok / 0 fail、合同 SHA
  `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`。
- 内容:按冠军审查 P0-5 建立合同派生的八工具/18 错误注册表、UUIDv4
  correlation、逐码 details 校验、4096-byte 完整 wire 限制、可信异常白名单、
  未知异常清洗、prepare/commit/rollback 原子边界、deadline、无隐藏 retry 和
  单次 observer seam；旧 `engine.framework_error()` 同步收口。
- 更正:上方“尚未开始”中的 `tool-error-middleware` 自本条起已完成实现证据。
  当前恢复包无 `.git`，所以 branch/commit/annotated tag 两项保持未完成；八个
  业务 handler、publisher、audit chain、decision trace persistence 与正式
  EVAL-001～030 仍未完成。

## 2026-09-18 03:08 — P0-5 后冠军标准复核，确认 P0-6 尚未整改

- 类型:审查 / 决策
- 证据:`P0_6_CHAMPION_READINESS_AUDIT_20260918.md`；正式 EVAL 实测
  0 PASS / 0 FAIL / 30 BLOCKED、退出码 1；EVT-005 针对性探针实测
  `state=BLOCKED`、`candidate_count=0`；安全/事件/导入专项 12 passed。
- 内容:确认 P0-1～P0-4 已按当前声明边界整改，P0-5 模块及证据完成，但尚未
  接入实际 API/Agent public-tool dispatcher。P0-6 的记录级隔离后继续排程、
  合同 `log_security_event`、decision trace 与 audit hash chain 均未实现；现有
  内存 security dict 和 `agent_step` 不满足合同。复核另列出 raw API error、
  quarantine 丢失、注入检测旁路、UI、模型绑定、Git provenance 等后续风险。
- 决策:下一轮按 audit-hash-chain → decision-traces → log_security_event →
  EVT-005 continuation → 八工具运行接入的顺序整改；正式 EVAL 保持 BLOCKED。

## 2026-09-18 — P0-6 注入隔离、安全事件、trace 与审计链完成

- 类型:实现 / 修复 / 证据 / 更正
- 证据:`src/planpilot/audit.py`、`tests/unit/test_security_audit.py`、
  `tests/negative_control/test_security_audit_negctl.py`、
  `P0_6_SECURITY_AUDIT_REMEDIATION_REPORT_20260918.md`；专项 13 passed、
  单元 604 passed、全量 610 passed、P0-6 变异 9 caught / 0 escaped /
  0 broken、P0-5 变异回归 11 caught / 0 escaped / 0 broken、workspace
  190 ok / 0 fail、网页渲染与隔离影响 PASS、合同 SHA 未变化。
- 内容:建立共享 append-only SHA-256 审计链和 typed security/trace index；
  `log_security_event` 通过 P0-5 middleware 执行并写入合同有效
  `decision_trace_record`。`EVT-005` 现在只隔离恶意记录、脱敏持久化安全事件，
  合法订单继续生成 3 套计划；HTTP 对抗测试验证 200、3 plans、无恶意原文回显、
  security/trace 各一条且链有效。UI 显示 `quarantine_impact`。
- 更正:上一条 P0-6“尚未整改”是整改前审查快照，自本条起其所列核心缺口已关闭。
  P0-5 证据按累计 604/610 回归刷新。其余七个业务 handler 的统一 dispatcher、
  publisher 和正式逐案 EVAL 仍未完成；正式 EVAL 保持 0 PASS / 0 FAIL /
  30 BLOCKED。恢复包无 `.git`，因此无法补做 commit/tag/祖先关系证明。

## 2026-09-18 — 第七部分 P1-1 模型绑定完成

- 类型:实现 / 修复 / 证据
- 证据:`src/planpilot/inference/bedrock_client.py`、
  `.kiro/specs/model-binding/{design,tasks}.md`、
  `tests/negative_control/test_model_binding_negctl.py`、
  `P1_1_MODEL_BINDING_REMEDIATION_REPORT_20260918.md`；专项 47 passed、
  单元 609 passed、全量 616 passed、变异 6 caught / 0 escaped / 0 broken、
  workspace 190 ok / 0 fail、合同 SHA 未变化。
- 内容:按冠军审查 P1-1 将 Python runtime、PowerShell、`.env.example`、
  Docker/Compose、交付 manifest 和操作文档统一绑定到 Bedrock Claude Sonnet 4.5
  全局推理配置 `global.anthropic.claude-sonnet-4-5-20250929-v1:0`，源区域
  `ap-southeast-1`。非合同模型在网络访问前失败关闭；旧 Agent 模块中的平行
  boto3 provider I/O 已移除。修复损坏的 `PLANPILO…ODE` 环境键。
- 交付修复:打包器不再把旧 `PACKAGE_MANIFEST.json` 当作源文件收入归档；
  生成的 ZIP 只含一份新 manifest，并通过逐文件哈希验证。
- 边界:本轮所有模型测试均使用注入 transport，没有调用 AWS 或消耗额度。
  团队账号权限、Lightsail 实测、时延、费用和演示证据仍待部署环境验证；正式
  EVAL 仍为 0 PASS / 0 FAIL / 30 BLOCKED。

## 2026-09-19 18:45 — G0 谱系收编:队友 B 包导入 p1-3-hardening(自我审查抓到漏文件)

- 类型:集成 / 修复 / 证据
- 内容:把无 git 的队友包 `PlanPilot-Team-20260915`(Hackton/qin/…/PlanPilot)整树导入本仓
  `p1-3-hardening` 分支,建立其首个 git 谱系。`adbeb48` 为主导入 commit(155 文件,
  +11098/-1230):devlog 三方合并保留本谱系第 10 轮审查与队友 P0-5..P1-2 条目;
  本谱系独有的撤回报告/factcheck/evidence 目录以 superset 恢复;合同导入后逐字节复验
  SHA-256 = `b92e53f4…fe639` 不变;compileall 全树干净;来源说明写入
  `IMPORT_PROVENANCE_p1-3-hardening.md`(含已知缺陷登记 P0-1/幂等/UTF-8)。
- 自我审查发现:导入脚本 KEEP_A 为保护父分支草稿跳过了 `src/planpilot/authority.py`,
  导致队友 375 行 `RuntimeAuthority` 未被拷入、`planpilot.authority` 断链(7 个测试模块
  + tools/api_server.py 依赖它)。`8d17916` 按字节恢复(SHA-256 `eba4e3a4…9a83a` 两侧一致),
  父分支 504 行草稿仍保留在 `docs/reference/authority_draft_parent_20260919.py` 作对拍参照。
- 合并副作用修复:A 独有的 `tools/factcheck_eval_retraction.py` 与 B 的闭词表守卫首次同树,
  证据状态字面量 `PENDING_UNTIL_EVAL_001_TO_030_EXECUTE` 触发 UNRECOGNISED TOKEN。
  按守卫自身说明书走 ALLOWED_LOCAL 正路:新增条目并把路径死锁到该 factcheck 文件;
  `test_allowlist_is_path_scoped_not_global` 的 tests/-only 假设同步放开到
  `tools/factcheck_*`(仍要求 classify() 能识别、作用域不得进 src/,未削弱否定测试)。
- 证据:`tests/unit` 导入后 **618 passed / 1 failed**(30.67s)。唯一红灯
  `test_http_approval_and_publish_use_runtime_authority` 实测 409 响应体 =
  `APPROVAL_WINDOW_CLOSED, server_now 2026-09-19T18:39 > horizon_guard 17:00`,即
  P0-1 时钟炸弹本体;在未修改的队友源包 B 原地跑同一测试得到同一 409 —— 非导入回归,
  归 G1 修复。
- 边界:本轮未动合同、未跑 negctl/EVAL、未触 AWS;tag 与 bundle 留待 G3 封口。

## 2026-09-19 深夜 — G1 服务端时钟抽象 + Windows 证据修复（含对 G0 条目的 4 处事实更正）

- 类型:修复 / 测试 / 证据更正
- G1 时钟修法（按评审更正后的目标「服务端时钟抽象与 fresh-horizon policy」执行,
  未放宽 horizon guard——那是合同法）:
  - 新增 `src/planpilot/clock.py`:Clock 抽象 + WallClock/ScenarioClock/FixedClock,
    单一时区 SGT,isoformat() 'T' 分隔符。
  - `RuntimeAuthority` 全部权威时间戳(revision/audits/approval service/授权复核)
    改走 self.clock;删除 server_now/decided_at 客户端参数通道(合同
    request_approval_input 无此字段且 additionalProperties:false,伪造时间戳实测
    403+schema 400 双重拒绝)。
  - `tools/api_server.py`:Server(..., clock=...) 缺省 WallClock(真机语义不变);
    main() 缺省 ScenarioClock(anchor 2026-09-14T08:00+08,PLANPILOT_SCENARIO=off
    关闭);新增免鉴权 GET /clock 返回 kind/now/scenario。
  - UI 横幅(index.html+p1_3.js):场景模式显示「演示场景时间」,墙钟模式显示
    「生产墙钟」,场景时间不伪装成生产时间。
  - 新增 tests/unit/test_server_clock_policy.py 5 条:stale horizon 在墙钟下仍
    APPROVAL_WINDOW_CLOSED(合同守卫保持);ScenarioClock 演示全流程(注:当时为合成 fixture 直调 Authority,非真 HTTP;真实链路 G1.0.1 补齐)(当天
    墙钟正是炸弹日 2026-09-19 18:xx,生成→审批→发布→重启回读全绿);客户端伪造
    server_now 被拒;重启换墙钟 digest/plan_version 不变;expiry 夹紧公式
    min(now+7200, horizon_end+24h) 与合同一致。
- 更正 1(测试数字环境限定,评审点名):上文 G0 条目「618 passed / 1 failed」
  实为 **PYTHONUTF8=1 环境** 618/1;默认 GBK Windows 为 617/2。第二失败是
  test_eval_evidence_honesty 对中文 Markdown 裸 read_text() GBK 解码错误。
  本轮 11 处裸 read_text() 全部显式 encoding="utf-8"(commit 9494928)。
- 更正 2(同类缺陷扩面):3 组 negctl 的 subprocess.run(text=True) 无 encoding,
  默认 GBK 下中文断言输出会解码失败误判判决,改 encoding="utf-8"
  errors="replace"(PlanStore 负控既有范式);3 组负控恢复段改
  read_bytes/write_bytes 字节级还原。修复后 tests/negative_control 全套
  7 passed,6 个受 mutate 文件 hash-object 前后完全一致。
- 更正 3(P1-2 回溯登记,评审点名):G0 条目「保留队友 P0-5..P1-2 条目」不准确
  ——合并本身没吞任何一方日志,但 B 包 devlog 实际只写到 P1-1;P1-2 只有报告
  `P1_2_SCHEDULING_ENGINE_REMEDIATION_REPORT_20260919.md` 与证据文件、无 devlog
  条目。现按该报告回溯登记:P1-2 调度引擎修复完成(2026-09-19 12:41 SGT),
  专项 9 passed、单元 618、全量 625。修复面(按原报告原话):历史换型上限误判回归、零预算 fallback、加班硬上限、
  显式基线、重新签名后篡改换型分钟仍被 HC-011 拦截、线容量/日历一致性与产能
  利用率公式修正。(本条旧版曾写 HC-014/016/017——合同闭词表仅 HC-001..013,
  虚构编号已删除;HC-011 为原报告实载真实编号。)
- 更正 4(交付卫生,评审点名):`PACKAGE_MANIFEST.json` 为 2026-09-15 旧快照
  (143 文件、Nova 默认模型),不代表本分支(git 现 266+ 文件)——已在
  IMPORT_PROVENANCE 补 STALE 声明,G3 重新生成。AGENTS.md 要求的
  `contract/verify_contract.py` 等验证脚本实际在 `E:\PlanPilot-Hackathon\
  contract-review` 而非仓库内,且 AGENTS.md 为受保护 agent 指令文件(改需授权)、
  由 generate_kiro_workspace.py 从模板再生——登记为 G3 交付项:合同验证包收入
  仓库或提供自包含入口。
- 验收(全部默认 Windows 即 env -u PYTHONUTF8,utf8_mode=0/preferred=cp936 实测):
  - tests/unit: 624 passed in 38.60s（619 原有+5 新增）
  - tests/ 全套: 631 passed in 356.46s(收集 631 = 评审基线 626 + 新增时钟政策 5)
  - negctl:7 passed,每组 mutation 静态计数 model 6 / security 9 / middleware 11
    + PlanStore/Approval 组,0 escaped,受 mutate 文件前后 SHA 一致
  - 合同 SHA 复验 b92e53f4…fe639 不变;compileall 全树干净
- 边界:未触 AWS;EVAL 仍诚实 0 PASS / 0 FAIL / 30 BLOCKED。tag 留 G3;
  bundle 已含 G1 全部 commit 落 D:\PlanPilot_backups。

## 2026-09-20 G1.0.1(评审回炉: 时钟边界 4+3 项收口, append-only 更正)

独立评审确认 G0/G1 数字与备份属实,但指出 4 个实质问题 + 3 个卫生问题。
本轮按「先小收口再开 G2」执行。措辞更正(G1.0.2 评审点名):下文
「更正」实际是**原位纠正**旧段落文字(3f8f793 的 diff 可证),并非一字不改
的纯追加;旧版本的原始文字由 Git 历史逐字节保留(git show 9743a06 可取回)。
append-only 仅对本节以下的新条目生效。追加/纠正内容如下:

- 更正 5(虚构 HC 编号,评审点名):上文「更正 3」把 P1-2 修复面写成
  HC-014/016/017 —— 合同封闭词表仅 HC-001..013,该编号不存在,属文档缺陷
  (闭词表守卫不扫 docs 才漏网)。按原报告(P1_2 报告§验证)实际表述更正为:
  历史换型上限误判回归、零预算 fallback、加班硬上限、显式基线、重新签名后
  篡改换型分钟仍被 HC-011 拦截、线容量/日历一致性与产能利用率公式修正。
  HC-011 为原报告实载真实编号。
- 更正 6(G1 时钟条目两处失实,评审点名):
  (a)「每个 authoritative timestamp 走 Clock」不实 —— 当时仅 lifecycle/
      approval 走 ScenarioClock,audit chain/decision trace/security event/
      factory state 仍走 persistence.now() 真实墙钟(实测同一次操作
      lifecycle=9-14T08:00 vs audit=9-19T23:35 双轨)。G1.0.1 已统一:
      Database 持有服务端 Clock,Server 组装时单点注入,AuditTrail/
      DecisionTraceWriter/SecurityEventService/FactoryStateStore/
      BedrockClient 全部经 db.clock;测试断言 lifecycle/audit/trace/
      state 时间戳前缀一致。token expiry 有意保留真实墙钟(安全语义)。
  (b)「ScenarioClock 全链路 HTTP 演示」过强 —— 该测试实为合成 fixture
      直调 Authority,非真实 HTTP,也无真实关-重开。G1.0.1 补
      test_real_http_demo_flow_survives_server_restart:真实
      factory_demo_v18.json、HTTP /schedule→/plans→/approval/request→
      /approval/decide→/publish、线程真停、SQLite 连接真关重开、
      /audit/status 链验证+时间一致性。原条目保留原文,以本条为准。
- 修复(评审 P0-1):生产假钟默认改 fail-safe —— 新增
  PLANPILOT_CLOCK_MODE(wall|scenario),不设/空=wall,未知值(含旧
  PLANPILOT_SCENARIO=off)启动报错,scenario 绑非本机地址拒绝启动;
  PLANPILOT_SCENARIO 变量废除。Dockerfile 显式 wall,start_local.ps1
  显式 scenario(本地演示器)。
- 修复(评审卫生1):/clock metadata 手抄 horizon_end=2026-09-18T17:00
  删除(合同 23:59/实际派生 9-19T00:00 三处矛盾),场景信息只留
  dataset+note,horizon 一律从加载态派生。
- 修复(评审卫生2):ScenarioClock 不再继承 FixedClock —— 按
  base + 真实 elapsed 前进,演示开久了审批照常过期;EVAL 钉死时间用
  FixedClock 显式注入,职责分离。
- 修复(评审 P1 日期脆弱):test_p1_3_web_approval /
  test_requirement_delivery 两处真实数据 HTTP 流注入 ScenarioClock
  (anchor 从数据集 planning_start 派生,不手抄;9-20 凌晨评审预言
  应验,修复前默认 Windows 实测 622/2 即此二条)。
- 新增测试 5 条(test_server_clock_policy.py 5→10):真实 HTTP 重启链、
  单钟源跨 audit/trace/state 一致、env fail-safe 策略、场景钟前进过期、
  未来墙钟 HTTP 409 APPROVAL_WINDOW_CLOSED 回归。
- 验收(默认 GBK Windows, env -u PYTHONUTF8): tests/unit 629 passed(收集=运行=
  629, 基线 624+净增 5), tests/ 全套 636 passed in 428.78s EXITCODE=0(基线
  631+5); 合同 SHA b92e53f4ff05 未动, EVAL 仍 0/0/30 BLOCKED。日期脆弱性
  已消除: 新基线不再依赖当前日期(场景钟钉死+未来钟回归双向锁定)。

## 2026-09-20 — G1.0.2 clock-hardening（评审阻断项收口）

独立复验判定 G1.0.1 **CHANGES REQUESTED**：P0 ScenarioClock 重启回拨可续命
审批窗口；P1×4 证据强度/错误码/锚点/计费钟；P2 时钟可变性约定、devlog 措辞、
证据未入仓。本轮按最小范围逐项修复，不扩 G2。

- **P0**：`clock.py` 新增 `clock_session` 持久会话（scenario_anchor /
  real_wall_started_at / last_issued_scenario_time），重启恢复取
  `max(anchor+真实流逝, last_issued)`，场景时间跨重启严格单调不减；重复重启
  无法再延长审批 TTL。停机（downtime）计入流逝时间。
- **P2**：`Database.clock` 改为只读属性；组装根一次性 `bind_clock()` 替换
  默认 WallClock，二次替换或替换成不同实例即 fail-closed。
- **P1-1**：四层（lifecycle/audit/trace/security/factory-state）逐列断言同一
  瞬间，scenario 与真实墙钟各一条，达到报告宣称强度。
- **P1-2**：断言 `error_code == APPROVAL_WINDOW_CLOSED` + 完整 details 三字段，
  不再依赖英文消息措辞。
- **P1-3**：scenario 模式必须显式 `PLANPILOT_SCENARIO_NOW`（严格 +08:00
  带时区校验，缺失/无时区/偏移不符启动失败）；`start_local.ps1` 从所选数据集
  `planning_start` 派生注入；`clock.py` 删除写死默认锚点。
- **P1-4**：Bedrock 日预算 `day` 列改用真实 SGT 计费日历（`calendar_clock`
  接缝），与业务场景钟分离；token 过期墙钟偏离获评审批准，维持。
- **P2 卫生**：devlog「一字不改」失实处已原位更正并注明 Git 保留原文；
  原始 stdout（unit/full/negctl）连同 SHA-256 入仓
  `tests/evidence/g1-0-2-clock-hardening/`。

验收数字（默认 Windows 环境，env -u PYTHONUTF8）：tests/unit **636 passed**
（G1.0.1 基线 629 + 本轮新增 7）；全量 tests/ **643 passed**（636 + 7）；
negative_control **7 passed**，变异目标文件前后哈希全部一致（RESTORE-MISMATCH:
none）；合同 SHA 前缀 `b92e53f4` 未变。证据：同目录 `EVIDENCE.json`。

## 2026-09-20 — G1.0.2 追加（复验 P1 收口：绑定原子性 + 并发高水位）

独立复验判定 P0 VERIFIED CLOSED，但 `bind_clock()` 失败后会残留未附着的
外来时钟（评审给出真实 DB 复现：异常被调用方吞掉后 RuntimeAuthority 仍
接受污染实例），且 `now()` 在 ThreadingHTTPServer 下可能把旧 stamp 交给
调用者。本补丁按评审修法收口，不扩范围：

- **P1a**：`bind_clock()` 改为先 `attach_database()` 验证、后提交
  `_clock`/`_clock_kind_explicit` 两字段——anchor 冲突抛错时 Database
  保持原状。回归 `test_failed_clock_bind_leaves_no_foreign_state`：失败
  绑定后旧时钟仍在、外来钟未附着、合法钟仍可绑定。
- **P1b**：`_persist()` 返回数据库高水位，`now()` 原样返回；SQL 加
  `AND last_issued_scenario_time < ?` 只前进。回归
  `test_concurrent_stale_writer_returns_high_water`（迟到旧值被抬回）与
  `test_threaded_now_stays_monotonic_per_caller`（8 线程 barrier，每个
  调用者自己的时间序列非降，行值=全局最大）。
- **P2 证据修正**：四层测试 lifecycle 层不再读 authority_state 信封，
  改读 `authority.get_plan(...)["lifecycle"]["updated_at"]`（信封另行
  单独断言与 lifecycle 亚秒一致）；测试参数化 scenario/wall 两种时钟，
  兑现报告「各一条」的说法。
- **文档**：旧重启测试注释「re-anchored at boot」改为持久会话恢复语义；
  devlog 变量名 `PLANPILOT_SCENARIO_ANCHOR` 更正为实际的
  `PLANPILOT_SCENARIO_NOW`。上一轮聊天报告把错误 details 写成
  horizon_end/min_server_now/max_server_now——代码与合同正确
  （server_now/horizon_guard/minimum_ttl_seconds），此处以本条为准。

验收：tests/unit 640 passed；tests/ 全量 647 passed EXITCODE=0；negctl
7 passed 恢复无差异；证据 `tests/evidence/g1-0-2b-bind-atomicity/`。

## 2026-09-20 — G1.0.2 二轮复验收口（双连接高水位 + 证据哈希自检）

二轮复验：`8360b57` 两项新阻塞。

- **P1 双连接**:条件 UPDATE 命中 0 行时代码仍把本地旧 stamp 存进 `_saved`
  并回吐——评审用两个同时打开的 Database 复现(conn1 持久 09:00,conn2
  now() 返回 08:00)。`_persist` 改单条原子 SQL
  (`UPDATE … SET last_issued = CASE WHEN last_issued < ? THEN ? ELSE
  last_issued END … RETURNING last_issued`),返回值一律取行内高水位并
  同步 `_saved`;新增 `test_two_open_connections_share_one_persisted_high_water`
  (公开 now() 路径,双 Database 同文件)。
- **P1 证据哈希**:两个 pack 的 6 条 SHA-256 按**提交内 LF blob** 重算
  (unit/clock 两条记的是 CRLF 预处理哈希,仓库 `.gitattributes` 强制
  eol=lf;full 一条两种表示都不匹配)。新增 `test_evidence_integrity.py`:
  每次套跑都对 `tests/evidence/**/EVIDENCE.json` 引用的日志用
  `git cat-file blob HEAD:…` 取提交字节重哈希比对;blob 未进 HEAD 时
  (证据先于提交产生)校验工作字节必须已是 LF。本自检上线当晚即抓到
  上一轮 pack 的同类错误,证明非装饰。
- devlog/EVIDENCE 措辞与实现保持一致;数字以本轮日志为准(unit
  642 passed,全量 649 passed, EXITCODE=0,含新回归)。
## 2026-09-20 — G1.0.2 审批通过；G2 基线 `5bf299a`

二轮复验 **APPROVED**：P0/P1 全关，跨连接高水位、绑定原子性、证据哈希自检
经独立复现验证（649 passed；bind 失败原子性 ✅）。

- **Baseline tag**：`g2-baseline-5bf299a`（annotated，解引用 `5bf299a`）——G2
  唯一开发起点。
- **登记遗留（P2，发布前防御纵深，非 G2 阻塞）**：
  `ScenarioClock._persist` 在 `clock_session` 行缺失时仍静默返回本地
  stamp（评审复现：手工删行 → 当进程返回本地时间，重启重新锚定）。
  正常应用无删行路径，需直接操作 SQLite 才触发。收口方案（评审原话）：
  1. `row is None` 改抛 `ClockSessionInvariantError`（fail-closed）；
  2. SQLite trigger 禁止删除 `clock_session` 行；
  3. trigger 禁止修改 anchor/start、禁止降低 `last_issued`。
  归入 G4 发布门禁前处理。

---

## 2026-09-20 — G2 Design-First：第三轮评审修订（纯文档）

- 类型：设计 / 评审
- 证据：`.kiro/specs/publisher-transaction/` 四文件；合同 SHA 不变
  `b92e53f4…fe639`；本轮修订后全库 40 处 `file.py:line` 引用程序化扫描
  逐一在界内通过。
- 评审结论链：三轮 **CHANGES REQUESTED** → 本轮第三轮纯文档修订。
  原六项 5/6 关闭，新增 1 个 P0 + 若干 P1/P2 矛盾，逐条落档：
  1. **P0 post-COMMIT 假阴性**：design §4.5 状态机新增
     `DURABLE_COMMITTED`——`conn.commit()` 返回即进入该态；自此
     `commit()` 绝不向 middleware 抛异常（抛出会走 ToolCommitFailure
     路径返回 "no business state was committed" 的 INTERNAL_ERROR，
     而 DB 实际已持久化——正是幂等发布最危险的"成功却报失败"窗口）；
     内存同步失败改为设 `_poisoned`、释放锁、照常返回已验证成功
     receipt；`rollback()` 在该态只放资源、绝不标 ROLLED_BACK；
     §8 case 13 改写为断言集：HTTP 200 + 字节一致 receipt + DB 已发布
     + poison 只拦后续调用 + reload 恢复 + 重试零新审计。
  2. **P1 validator evidence 映射**：证据缺失/被删 ≠ `VALIDATION_FAILED`
     （合同该码 details 强制 `status/errors/quarantined_entity_count`
     校验报告结构，塞内部证据丢失会逼实现者伪造 validation_issue）。
     改为 `ApprovalInvariantError`（approval/errors.py:145 已存在）→
     middleware → `INTERNAL_ERROR` + `diagnostic_class=
     "ApprovalInvariantError"`；`VALIDATION_FAILED` 仅留给真实 INVALID
     校验结果（design §4 步骤 5 / §8 case 17 / tasks 4.5）。
  3. 次级四项：删臆造码 `INVALID_STATE_TRANSITION`（非法 lifecycle
     转换=内部 invariant→`INTERNAL_ERROR/503`）；Case D HTTP 修正
     400→**403**（与 §6.3 表一致，撤销 409）；Case D 不再造
     "StoreError sibling"（`StoreError.RETRYABILITY` 无 POLICY_VIOLATION
     键，`.retryable` 会 KeyError——实测），直接用
     `FrameworkDomainError("POLICY_VIOLATION", …)`（errors.py:30，实测
     `resolve_error('POLICY_VIOLATION')` 存在非重试，middleware 直接
     消费零改动）；trace 保证收紧为 best-effort observability
     （observer 异常被 middleware 吞、进程可在 `_finish` 前死，
     "必产生一条"仅在健康路径成立）。
- 验证：`git diff --check` 干净；`ApprovalSetReplaySafe`/`VALIDATOR_`/
  `INVALID_STATE_TRANSITION`/`400 → 409` 残留 grep 清零；合同零改动；
  四份 spec + devlog 共 5 文件，无任何实现代码、无测试改动。
- 状态：G2 第三轮修订完成，Phase 1 待批准后开始。

---

## 2026-09-20 — G2 Design-First：spec 初稿 + 两轮评审修订（纯文档，无实现代码）

- 类型：设计 / 评审
- 证据：`.kiro/specs/publisher-transaction/`（design 607 / tasks 160 /
  START_PROMPT 125 / README 36 行）；初稿 commit `d1dea54`（父 `c1e9274`）；
  本条随二轮修订一并提交。
- 内容：publisher-transaction spec 初稿提交后，评审两轮均
  **CHANGES REQUESTED**（未批准进入 Phase 1）。二轮关闭清单已全部落入文档：
  1. §4.5 `PublisherPreparedCall` 跨阶段状态机（BEGIN IMMEDIATE 持锁跨
     prepare/commit、staged clone、commit 后内存同步、rollback 幂等、
     commit-sync 间隙异常 → reload/poison）；
  2. §4 步骤 5 显式 validator evidence 复核（`require_validated_binding`
     只读方法 + 五条件 + 负测），缺证据映射合同码 `VALIDATION_FAILED`
     （非 `VALIDATOR_*`，合同 18 码无此后缀）；
  3. §5 Case D 改 `POLICY_VIOLATION`/`approval_scope_exceeded`（新 key +
     不同 approval set 不是幂等冲突）；Case C alias 登记且不可变断言；
     `publication_receipt.audit_log_id` 加 UNIQUE；
  4. §6.2–6.4 middleware 唯一输入校验入口、`wire_bytes`→HTTP 传输表、
     DecisionTraceWriter observer 记账（重放可加 trace 不得加第二条
     `plan_published`）；现状说明改为"api_server 今日未构造
     ToolErrorMiddleware，wiring 属实现任务"；
  5. §8/tasks Phase 0 改条件式 HEAD 门禁（reviewer-approved spec commit，
     `c1e9274` 为其祖先）；删除虚构异常 `ApprovalSetReplaySafe`；新增两个
     真·硬杀 crash 测试（`os._exit` 子进程，case 14/15）。
- 终检（本轮实测）：`git diff --check` 干净；合同
  `b92e53f4…fe639` 不变；四文件 33 处行号引用逐一在范围内；
  `web/static/p1_3.js` 虚构引用已改为真实 `tools/web/p1_3.js:254`。
- 状态：停在评审点，等待批准进入 Phase 1；未写任何 G2 实现代码。

---

## 2026-09-20 — G2 Design-First：第三轮后三处勘误（reviewer：核心通过，Phase 1 有条件批准）

- 类型：文档勘误（reviewer 结论 *Conditionally Approved* 指定三处）
- 内容：①design §4 步骤 5 `INTERNAL_ERROR` 重试性纠错——原误写
  non-retryable，合同 `retryability_registry` 实测 `INTERNAL_ERROR` 为
  retryable:true（仅允许依赖健康检查成功后至多重试一次），按注册策略
  改写；②§4.5 commit() 伪码在 `conn.commit()` 返回后显式
  `state = DURABLE_COMMITTED` + `_txn_open = False`，杜绝双标志矛盾；
  ③tasks 4.5(e) trace 措辞与 design §6.4 best-effort 对齐（healthy
  observer path 恰好一条；observer 异常或 `_finish` 前进程死亡可缺失，
  不得影响业务结果）。
- 验证：合同 SHA `b92e53f4…fe639` 不变；全库再无 INTERNAL_ERROR
  non-retryable 残留；40 处 `file.py:line` 引用扫描全部界内；
  `git diff --check` 干净。
- 批准范围：Phase 0 + Phase 1 的 1.1–1.4（双表 DDL、canonical 指纹、
  Publisher skeleton、Case A/B、双连接测试）；不提前进入 Phase 2
  （lifecycle/audit/HTTP wiring）；不动 tag、不改合同。

---

## 2026-09-20 — 暂停点：Phase 0 开工前快照（明日续）

- 类型：进度快照（应作者要求暂停，非里程碑）。
- 仓状态：HEAD=`6cb13f9`（三勘误提交），工作区干净；tag
  `g2-baseline-5bf299a`→`5bf299a` 未动；合同 SHA `b92e53f4…fe639` 未动。
- Phase 0 未完成项：继承套件四连跑（`tests/unit` 642、`tests/` 649、
  clock 三文件、negative_control 7，约 15 分钟）后台跑到一半被中止；
  半成品证据目录 `tests/evidence/g2-phase0-baseline/` 已删除，未留残渣。
- 明日续点（按批准范围，勿越界）：
  1. 重跑上述四命令 → raw stdout 落 `tests/evidence/g2-phase0-baseline/`
     （格式照 g1-0-2c 包：EXITCODE 行 + EVIDENCE.json + LF sha256；
     脚本 `g2_phase0_run.sh` 逻辑照抄），另做 0.3 合同面钉死测试
     （publish 五字段输入/四字段输出/错误枚举/重试性 registry）；
  2. Phase 1 只做 tasks 1.1–1.4：`publication_receipt` +
     `idempotency_registry` DDL（design §3 原文照搬进
     `Database.__init__` executescript）、canonical 五字段指纹、
     PublisherService skeleton（§4 步骤 3–4 探测路径）、Case A/B +
     双连接 barrier 测试；
  3. 不碰 lifecycle/audit/HTTP wiring（Phase 2），不改合同，不动 tag。
- 环境备忘：跑测试 `.venv-review/Scripts/python.exe -m pytest`，脚本内
  `env -u PYTHONUTF8`；E 盘为 U 盘，开工先探挂载。

## 2026-09-21 — Phase 0 闭环 + Phase 1（tasks 1.1–1.4）完成

- **0.1 lineage**：HEAD 链 `c1e9274 → d1dea54 → 4b1cdf0 → 942e13d →
  6cb13f9 → 28c00a5`，spec 四文件在 HEAD、c1e9274 为祖先；tag
  `g2-baseline-5bf299a` → `5bf299adb1e06c2f061db4086cc3bf183944ff56`
  未移动；合同 SHA `b92e53f4…fe639` 不变。
- **0.2 四连（干净树重跑）**：unit 642 / full 649 / clock 57 / negctl 7
  （byte-restore 通过）。raw stdout + EXITCODE 行 + LF-sha256 落
  `tests/evidence/g2-phase0-baseline/`（EVIDENCE.json 过 integrity 扫描）。
  诚实性注记：第一轮 clock/negctl 跑在已含新 DDL 的树上发现串污，
  已回退 persistence 后在干净树重跑为准；unit/full 本就干净树。
- **0.3** 新 `tests/unit/test_publish_contract_surface.py`（10 passed）：
  publish 输入五 required + additionalProperties:false、key 16–128、
  输出四 required + plan_version const、details 三字段 +
  original_status 枚举 + version ≥1、IDEMPOTENCY_CONFLICT registry=False、
  two-safeguards 两 bool、POLICY_VIOLATION details required/枚举。
- **1.1** design §3 DDL 原文照搬进 `persistence.py` executescript；
  `tests/unit/test_publisher_schema.py`（5 passed）：列布局、UNIQUE×4、
  FK from/to/table（PRAGMA 列序 from=子列 to=父列）、reopen 约束存活、
  transaction() 内写与回滚。
- **1.2–1.4** 新 `src/planpilot/publisher.py`：`request_fingerprint`
  （`persistence.canonical` 同一函数 + sha256，恰好五字段）、
  `PublishIdempotencyConflictError`（继承 store.IdempotencyConflictError，
  code/details/registry 完全不变）、`PublisherService.probe`（§4 步骤
  3–4：replay / conflict+rollback / first 骨架零写入，事务由
  `Database.transaction()` 拥有，不自嵌套）。
  `tests/unit/test_publisher_idempotency.py`（21 passed）：键序无关、
  逐字段敏感、Case A verbatim 重放（响应非列派生）、Case B 冲突、
  双连接 barrier（case6 replay-replay；case7 conflict/replay + 文件
  字节零变化）、conflict 后字节不变（结构零变更）、first 三表零行。
- `tests/unit/test_errors_schema.py`：CONCRETE_ERRORS 注册新错误
  （hierarchy walk 守卫如期报警，按其要求登记，+4 参数化）。
- **复跑**：全量 `tests/` = **689 passed, 0 failed**（405s）。
- **边界**：本轮未触碰 lifecycle/audit/HTTP wiring/中间件状态机
  （Phase 2 范围）；合同与 tag 未动。

## 2026-09-21 — Phase 1.0.1 hardening（评审第4轮探针修复）

复审批复：Phase 0 Approved、Phase 1 数字 Verified、**进入 Phase 2
Changes Requested**。三个探针全部复现成立，本轮按最小范围修地基，
不动 Phase 2 范围。上一条目「文件字节零变化」措辞失实，以本条为准：
字节级断言既不成立（SQLite 页布局/freelist 不属于逻辑状态）也不可移植，
正确断言是**规范化逻辑状态未变**（全表 canonical dump 哈希）。

- **探针2（事务所有权）**：`PublisherService` 拆为公开 `probe()`（Phase 1
  独立入口，恰好开一个 `transaction()`）+ `_probe_in_open_transaction()`
  （§4 步骤 3–4 本体，**零事务管理**；无开钟即 `StoreInvariantError`
  fail-closed）。Phase 2 单 BEGIN 流程调本体。pin：外层事务内跑通
  （写→probe→rollback 全 erased）、无事务调用报错、公开 probe 嵌套调用
  复现 `sqlite3.OperationalError` 后回滚干净。
- **探针3（安全重放）**：新增 `replay_binding_violation()`——replay 前
  request↔receipt↔response 三方绑定：response 键集/类型/`status` 合同
  const、response.receipt 三对齐（plan_id / audit_log_id /
  published_version==plan_version）、request.receipt 对齐（digest/
  approval_set，抓 commit 后篡改）。矛盾 → `PublicationInvariantError`
  （非 StoreError，按 ApprovalInvariantError 先例走 middleware
  fall-through → 合同形 INTERNAL_ERROR/retryable=true，绝不 200 verbatim
  重放假响应）。负测 6+2 参数化：版本矛盾/audit 伪造/plan 伪造/status
  非 const/多余键/非对象/receipt 篡改×2；端到端 `validated_failure`
  证明 wire=INTERNAL_ERROR 且内部细节不外泄；外层事务回滚干净。
- **探针1（fixture 合法性）**：测试 digest 改合同合法 `^[a-f0-9]{64}$`
  （旧 `"sha256:"+64hex` 会被真合同 validator 拒收）；STORED_RESPONSE
  published_version 与 receipt plan_version 统一为 3（矛盾对转成负测素材）。
- 测试：`test_publisher_idempotency.py` 21→**36 passed**（净增 15：三方
  绑定/矛盾拒绝系 12——helper 2 + 矛盾响应 6 参数化 + receipt 篡改 2
  参数化 + wire 端到端 1 + 外层回滚 1；事务所有权/fail-closed 系 3）。
  加 `test_publisher_schema.py` 5 条（fixture 同步改合同合法形状），
  publisher 两文件共 **41 passed**。

## 2026-09-21 — Phase 1.0.2（评审第5轮：版本绑定缺口 + helper 措辞）

复审批复：704/0、negctl 7/0、Publisher targeted 99 均独立复现；前三项
探针修复 VERIFIED，但剩 1 个必须修复项。本轮按清单补两笔小修。

- **P1（绑定漏 expected_plan_version）**：`replay_binding_violation`
  此前比较 request 的 plan_id/plan_digest/approval_set_id 与
  response↔receipt 三对，唯独漏了乐观并发对的 request 侧——
  registry+receipt+response 可整体合谋"请求版本4、实际发布3"的故事仍被
  replay。补 `request["expected_plan_version"] == receipt["plan_version"]`
  （违反→同一 `PublicationInvariantError`→INTERNAL_ERROR）。
  新增 3 测试：helper 级版本对（含一致三元组仍 None）、reviewer 复现
  trio 端到端（seed 用 v4 指纹 + receipt/response 说 3 → probe(v4) 拒绝
  且 `db_digest` 逻辑状态未变）、wire 级（INTERNAL_ERROR +
  retryable=True（registry 实证）+ details 键集固定 + `expected_plan_version 4`
  与 `plan_version 3` 字样均不在 wire 上）。
- **P2（helper 保证比实现强）**：`_probe_in_open_transaction` 的断言
  收窄为字面事实——只验证"连接上存在开放事务"（`in_transaction`），
  **不**验证 `BEGIN IMMEDIATE`、**不**验证 `Database.lock` 归属（deferred
  BEGIN/不持锁的调用方目前会通过守卫，这是诚实承认的现状而非特性）。
  模块头、方法 docstring、raise 消息三处措辞同步更正；结构性三保证
  （锁归属、事务模式、释放时机）明确为 Phase 2 `PublisherPreparedCall`
  的职责，且 Phase 2 测试必须三项都证明。未加"deferred 也算过"的特征
  测试——避免把缺口固化为规格。
- 定向复跑：publisher 两文件 **44 passed**（41+3）。
- 收尾三件套（reviewer 指定，无需再设计审查）：publisher 两文件
  **44 passed**（41+3）；全套 `tests/` = **707 passed in 392.70s**
  （704+3 对账吻合，EXITCODE=0）；`tests/negative_control` =
  **7 passed in 347.92s**（EXITCODE=0）。
- **边界**：仍未触碰 lifecycle/audit/HTTP wiring/Phase 2；合同与 tag
  未动。

## 2026-09-22 — Phase 2.0.1（评审第7轮：PREPARED 失败释放时机 P0 + digest details P1）

复审批复：主体设计正确、19 项矩阵通过，但独立复现两个真实缺陷；
建议先做很小的 Phase 2.0.1 hardening 再继续 p2-5。本轮全部按探针
复现→修复→回归测试→复验，未进入 p2-5。

- **P0-a（commit():601 finally 无条件释锁）**：alias INSERT 或
  `conn.commit()` 在 durable point 前抛错时，`finally` 先释放
  `Database.lock`，middleware 稍后才 rollback——实测探针
  `AT_ROLLBACK_ENTRY {state: PREPARED, lock_held: False, in_txn: True}`，
  即事务仍开着而数据库锁已放，其他线程可拿同一连接进事务。
  修复按设计 §4.5 规则 1 的字面语义：`finally` 仅在状态已到
  DURABLE_COMMITTED（或 FINISHED）时释放；PREPARED 内失败保持
  PREPARED+持锁原样抛出，锁的释放责任移交 `rollback()`（先
  `conn.rollback()` 后释锁、恰好一次）。
- **P0-b（publish():751 异常保护只覆盖 prepare）**：commit 抛错不在
  guard 内，实测返回异常后 `CONNECTION_IN_TRANSACTION True`。修复：
  统一 rollback guard 覆盖 prepare → 输出校验 → commit 全周期
  （异常→`rollback()`→re-raise）；非 HTTP 驱动同样在 commit 前做
  输出 schema 校验（与 middleware.py:210 的镜像，双驱动同一保证）。
- **P1（PLAN_DIGEST_MISMATCH details 自相矛盾）**：请求 digest 错时
  details 传的是 `stored_digest` 与 `recomputed`——实测
  expected==recomputed 却声称 mismatch。修复：`expected_plan_digest`
  报告调用方声明的 `request["plan_digest"]`。存储内容篡改那一条腿
  本就由 `verify_digest()` 在 plan_store.py:434 用正确的
  stored/recomputed 对抛出，不需要（也不应该）在这里替调用方输入
  代言。
- **覆盖收紧（case 12 缺口）**：补三条回归进 §8 矩阵
  （test_publisher_transaction.py，19→22）：
  1. `test_p0_alias_insert_failure_keeps_lock_until_rollback`——
     patch `_insert_alias_row` 抛错，断言 rollback 入口
     `PREPARED+lock_held=True+in_txn=True` 且他线程拿不到锁，
     rollback 后事务关闭、锁释放一次、原收据零扰动；
  2. `test_p0_conn_commit_failure_then_publish_guard`——
     `conn.commit` 属性只读，用 `_CommitFailConn` 代理强制真实
     pre-durable 提交失败；(a) 直接 PreparedCall 协议保持持锁，
     (b) `publish()` guard 后 `in_transaction is False`，连接留在
     可用态（诚实重试发布成功）；
  3. `test_case12_invalidated_set_publish_zero_change`——
     APPROVED→invalidate 落 durable row（§4.5 staged reload 读
     authority_state 行），发布拒绝
     `APPROVAL_SET_INVALIDATED`+cause 上 wire+零变化。
     `plan_digest` 错误的 details 断言挂进 case F 既有测试。
- **actor 铺垫（p2-5 前置，不改变现行为）**：`plan_published` 审计
  actor 不再写死——`PublisherPreparedCall(actor=...)` 贯穿到
  `_audit`，默认 `"system"`；p2-5 middleware 接入后传
  `principal["sub"]`，不丢 `authority.publish_plan()` 路径的真实
  发布人。
- 证据：矩阵 **22 passed in 6.69s**；全量 `tests/unit` =
  **731 passed**（728+3 对账吻合，EXIT=0）；全套 `tests/` =
  **738 passed in 435.09s**（731 unit + 7 negctl，EXIT=0；首轮
  曾 1 failed——test_requirement_delivery 本地 HTTP 集成测试在
  negctl 子进程负载下读超时、单独复跑 13 passed，与本轮改动
  无关，如实记录；次轮 1 failed + 1 error 为证据包仍在收集路径
  内被完整性守卫哈希到半成品，属自撞，非产品缺陷；证据包迁出
  后第三次运行即本条 738 绿）；
  `tests/negative_control` = **7 passed in 377.06s**（EXIT=0；
  首轮一次 error 为 requirement_delivery 本地 HTTP 集成测试在
  negctl 子进程负载下的读超时，单独复跑 13 passed 与全套 731 均
  绿，与本轮改动无关，如实记录）；词表守卫 PASS；合同
  SHA-256 `b92e53f4…` 未动。原始 stdout+SHA-256 入仓
  `tests/evidence/g2-phase2-0-1-hardening/`。
- **边界**：未触碰 p2-5（HTTP 路由重接）与 p2-7 收口；按
  reviewer「先做一个 Phase 2.0.1 hardening commit」建议，p2-1…
  p2-4、p2-6 主体与本节硬化同仓提交（评审时它们尚未提交，
  HEAD 仍是 `2103b47`）。
- 提交：`29e1a5a fix(g2-phase2.0.1): hold Database.lock until
  rollback completes; fix digest-mismatch details`——13 文件
  （amend 前初始 SHA 为 `36961d2`，因 devlog 证据段补正被改写，
  如实记录；本行补正随下一 devlog-only 提交入仓）。

## 2026-09-22 — p2-5：/publish 路由重接为薄适配器（reviewer 七条红线逐条落地）

- **主线**：`api_server` 的 `/publish` 不再手写平行发布路径，重接为
  设计 §6.1 的薄适配器——认证留路由、执行入共享中间件、审计带真实
  actor、响应原样出字节。旧 `authority.publish_plan()` 改为委托
  Publisher，生产入口唯一化。
- **七条红线对账**（reviewer 原话 → 落点）：
  1. `_auth("approve_publish")` 留在 middleware 外：路由内先认证，
     缺失/权限不足的 token 仍按裸传输层 403 出（无 tool_error 信封），
     行为与旧路径逐字一致；
  2. `principal["sub"]` 传入 `prepared_call(actor=…)`：真实 actor 落在
     `plan_published` **审计记录**里（publication receipt 与 decision
     trace 的 schema 本身没有 actor 字段——receipt 通过 `audit_log_id`
     指针绑定到该条审计记录，trace 只证明该次调用被观察，身份由
     correlation_id 承担）；新测试钉的是**存回来的审计链内容**加
     receipt 的 audit_log_id 绑定，不是响应回显；
  3. 删除路由层重复的 `validate_tool_payload()`：payload 校验唯一
     归中间件（与 M2/MCP 驱动同一函数、同一处调用）；
  4. 路由必须调共享 `ToolErrorMiddleware.execute("publish_plan", …)`：
     错误码、retryable、tool_error 信封三驱动一处出；
  5. 原样写出 `outcome.wire_bytes`：新增 `_wire()`，重放测试断言
     两次响应**字节相同**且第二次**零新增审计行**（只加 trace）；
  6. 补 non-planner / 窗口关闭 / 非法 payload / 真实 actor / §6.3
     状态映射 / observer trace 测试：新文件
     `tests/unit/test_p2_5_http_publish.py` 共 8 项，全部对**真起
     的 HTTP 服务器**打真请求（对齐 `test_server_clock_policy` 的
     做法，而非直接调内部函数）；
  7. 旧 `authority.publish_plan()` 不再构成绕开 Publisher 事务的
     生产入口：`RuntimeAuthority.publish_plan` 改为惰性构造并委托
     `PublisherService.publish(...)`（§4.5 同一事务，约束见该节），
     方法 docstring 钉死语义差异。
- **§6.3 状态映射表**：路由内一张 `PUBLISH_STATUS_BY_ERROR_CODE`
  逐行照抄设计 §6.3；**一处主动修正**——补录 `STATE_NOT_FOUND`
  →409：旧路由将其归 store 域（StoreError→409），设计表原只点名
  PLAN_* 两个 store 码，漏收会让未知 plan 发布从 409 静默退化为
  503，故按旧行为收编，并同步补进 `.kiro` 设计 §6.3 表（reviewer
  第 8 轮：报告称补录而设计文档未改，本轮已文档/代码一致）。
- **兼容修正（响应形状变化引发，非回归）**：
  - `tools/web/p1_3.js`：新 publish 响应是 4 字段契约收据，不再回显
    `approval_set_id` 等；JS 从整体覆盖 lifecycle 改为 merge，
    lifecycle 视图字段不再被收据抹掉；
  - `tests/unit/test_runtime_authority.py`：委托后走 §4 步骤序，
    版本预检（step 4）先于审批检查（step 6），stale 版本发布报
    `PLAN_VERSION_CONFLICT` 而非旧的 `APPROVAL_SET_INVALIDATED`——
    测试断言改为钉委托语义（这是 publisher 的正统顺序，不是新行为）；
  - `tests/unit/test_p1_3_web_approval.py`：旧路由从不查幂等注册表,
    两处测试在同一 key 上发第二个 binding；委托后共享注册表按 §5
    case B 正确拒绝，测试改用独立 key + 新增 case A 重放断言
    （同 key 同 body 重放 → 同一收据、零新审计）。
- **过程错误如实记录**（两次，均在证据落盘前发现并纠正）：
  1. 导入虚构：初版从 publisher 导入了不存在的
     `PublisherPoisonedError`（毒态实际抛 `internal_error(...)` 的
     FrameworkDomainError，路由用不到），自查后清除，冻结 import 仅
     `PublisherService`；
  2. 证据污染：一次误把 finalize 脚本写进 `_pack_staging/`（树内），
     同时并发起了第二个全套——repo-scan 类守卫扫到新 .py 报 10 failed
     + 1 error（假阳性，非代码问题）。处置：finalize 移出到树外
     （Temp），孤儿 pytest 进程清理，**冻结树全套 + negctl 全部重跑**，
     本条目以下所有数字来自重跑后的终版日志。
- **第 8 轮 reviewer 阻塞（CHANGES REQUESTED 两项 P1）与修复**：
  1. **P1-1 非法幂等键**：委托处曾用 `f"{plan_id}:{version}"`，
     而合同对 key 有 16–128 长度约束、`plan_id` 本身无界，且
     `PublisherService.publish` 不重跑输入 schema——实测
     `plan_id="P"` 会把 3 字符 key 真实写进 registry。改为
     `legacy-publish-` + sha256(canonical([plan_id, version]))
     （固定 79 字符、同绑定确定性重放、版本敏感、分隔符注入安全：
     `A:1`@2 与 `A`@1 不同键）。回归 2 项进
     `test_runtime_authority.py`：1 字符/5000 字符 plan id、
     确定性、版本敏感性、registry 存的 key 满足 16–128、
     重放返回原收据。
  2. **P1-2 非对象 JSON 绕过信封**：分发前的全局 object guard 以
     裸 `{"error": …}` 400 拒绝数组/字符串/数字/null，绕过
     middleware 唯一校验入口（无 INVALID_INPUT/retryable/
     correlation_id/details，无 trace）。改为 `/publish` 把**任意**
     已解析 JSON 值交给 middleware（非 Mapping 时 refs={}，不再先
     `body.get()`），其余旧路由保留原 guard。HTTP 测试一组四例。
  3. **证据文字更正**：actor 声明按真实 schema 收窄（见上红线 2
     改写）；STATE_NOT_FOUND→409 同步补进 `.kiro` 设计 §6.3 表，
     文档/代码一致。
- **冻结树复跑（第 8 轮修复之后；此前 8/739/746 各次作废）**：
  `test_p2_5_http_publish.py` **9 passed**（+非对象四例一组）；
  RA+Web 兼容 **11 passed**（+legacy-key 回归 2）；publisher 两文件
  矩阵 **61 passed**；unit **742 passed**（731 基线 + 11 新增，对账
  吻合）；全套 `tests/` **749 passed in 438.90s**（742+7 对账吻合，
  EXIT=0）；`tests/negative_control` **7 passed in 436.44s**；词表
  守卫 **PASS**；合同 SHA `b92e53f4…` 未动，`contract_changed:
  false`。**如实记录**：修复后第一次全套出现 1 例 negctl
  沙箱上下文相关误报（allow_nan 变异在 58 连跑中报
  test_http_authentication_and_full_publish_flow 变红）；用同一变异
  做三种隔离复现（单测/单文件/整个 unit 套件）均绿，干净重跑 0 失败
  ——判定为该控制自身的 flake（其文件注释有既往同类记录），最终以
  干净重跑入包，变异目标文件全程先备份后字节级复原。
- 证据包：`tests/evidence/g2-phase2-5-http-wiring/EVIDENCE.json`
  （7 日志 + SHA，`raw_stdout.artifacts` 体例沿用 Phase 2.0.1；
  finalize 后复跑 integrity 与守卫；`_pack_staging` 清空，
  finalize 脚本在树外（Temp）运行后删除）。
- **边界**：本条目只覆盖 p2-5 实现证据；p2-7（Phase 2 收口 + G3
  复验包）另立条目。未动合同、tag、Phase 3。

## 2026-09-23 — p2-7：Phase 2 收口（negctl 上下文 flake 结构性关闭 + tasks 勾选）

- **前置**：reviewer 授权提交 p2-5（第 8 轮两项 P1 关闭后），入库
  `5d2b656`；提交后按点名复验：`git status --porcelain` 干净、
  `test_evidence_integrity` 对最终 Git blob **1 passed**。措辞更正
  （「类型标签」→「紧凑 JSON 数组编码」）已接受：仓库注释与实现
  本来就是 `json.dumps([plan_id, version], separators=(",", ":"))`，
  该词仅存在于聊天报告，已按 reviewer 口径使用。
- **flake 定根因（不靠重跑）**：defense-in-depth 变异（`float layer 2
  removed (allow_nan)`、`P1-a(2)`）的 hold 判定当时 `expected_suite`
  为 None → 在变异循环内重跑**整个 tests/unit**（~742 项，含本地
  HTTP 交付测试，子进程 600s 超时、单请求 30s urlopen）。该循环连跑
  58 个变异子进程，负载下 HTTP 计时偶发抖动被记为 escape——上一轮
  全套即出现 1 例，且三种隔离复现全绿、不可复现，即 reviewer 点名
  要消灭的"随机 escape"。
- **结构性修复（reviewer 处方第一条）**：
  1. hold 套件从"整个 unit 目录"收窄为**显式 socket-free 文件组**：
     layer-2 → `("test_digest_determinism.py",
     "test_tool_error_middleware.py")`（63 项，0.5s）；
     P1-a(2) → `("test_audit3_regressions.py",)`（43 项，0.7s）。
     两组各手工变异 ×5/×3 全绿验证（跑前备份、跑后字节级复原）。
  2. `test_mutations_are_detected` 的 hold 分支按 tuple 判定，新增硬
     断言 `still_held == 2`、`caught == 56`（58 = 56 fail + 2 hold，
     对账吻合）；详细失败报告保留（D5 教训：无 test-id 的报告不可行动）。
  3. 新增结构守卫 `test_defence_suites_are_http_free`：对 hold 套件
     源文件逐一断言不含 HTTP/socket 字样（urlopen、http.server、
     HTTPServer、socket、Request( 等），防止未来任何 hold 套件悄悄
     把网络计时重新引入变异判定。
- **tasks.md 2.1–2.6 逐项对账后勾选**：每条的落点（revalidate /
  require_validated_binding / audit id 捕获 / 输出 schema 闸门 /
  单 COMMIT + §6.3 映射 / case C 别名 + case D fail-closed / observer
  trace + envelope 唯一出口）均已在 p2-1~p2-5 实现并有测试钉住；
  无测试读取 tasks.md 勾选状态（grep 复核），勾选项随收口提交入仓。
- **冻结树复跑**（含上述修复）：全套 **750 passed in 323.97s**
  （749 基线 + 1 新守卫，EXIT=0、0 FAILED）；negctl **8 passed**
  （7 原有 + 1 新守卫，**连跑两次** 268.64s / 268.27s 双绿——收窄后
  的 hold 判定不再依赖任何网络路径）；词表守卫 PASS；
  integrity + binding 守卫 10 passed。合同 SHA `b92e53f4…` 未动。
- 证据包：`tests/evidence/g2-phase2-closeout/`（full/negctl/vocab
  三日志 + EVIDENCE.json，含 reviewer 指令原文与本方案的对应关系；
  finalize 在树外运行后删除，staging 清空）。
- **边界**：Phase 2 至此实现 + 证据 + flake 关闭三事齐备，等待
  reviewer 对收口提交（tasks.md 勾选 + negctl 收窄 + 证据包）的
  授权；G3 复验包与 Phase 3 另立条目。

## 2026-09-23 — G3 顺手项：p2-7 两项非阻断 P3 修正（reviewer 授权范围内）

- **背景**：p2-7 已入库 `7b6b238`（提交后 integrity 对最终
  blob **1 passed**、status 干净、`git show --stat` 与报告一致）。
  reviewer 批准进入 G3，同时点名两项非阻断 P3「可在 G3 顺手处理」。
- **P3-1 类型注解**：`MUTATIONS` 注解末项原写 `str`，p2-7 后实际
  语义是 `str`（must-fail 套件）| `tuple[str, ...]`（defence-in-depth
  hold 套件）。改为 `list[tuple[str, str, object, str | tuple[str, ...]]]`，
  并同步修正表头过时注释（原只写 "suite that must fail"）。
- **P3-2 守卫措辞与词表**：`test_defence_suites_are_http_free` 的
  docstring 原称 hold 文件"不含任何 HTTP/socket 机制"，措辞强于实现
  （实现是关键词扫描）。收紧为 **known network entry points 的
  tripwire，非网络缺席的全程证明**；词表按 reviewer 处方扩充
  `requests / httpx / aiohttp / socketserver / urllib`（三个 hold 文件
  对全部 10 词实测零命中，扩充不误伤）。
- **验证**：三条快守卫（http_free / vocab self-test / repo scan）
  **3 passed**；p2-5 HTTP + RuntimeAuthority 回归 **19 passed**；
  devlog 追加后词表守卫 **PASS**。58-mutation 长跑不受影响
  （本提交未触碰任何 MUTATION 条目与执行分支）。

## 2026-09-23 — G3：Publisher 事务复验包 + Phase 3–4 勾账（待 reviewer 授权提交）

- **前置**：p2-5 `5d2b656`、p2-7 `7b6b238`、P3 顺手项 `1d14a1e`
  均已入库并做提交后三连复验；reviewer 明确 Phase 0–2 关闭，
  G3 负责 crash matrix/并发/硬杀/证据包/handoff 收口。
- **§8 逐案对账（1–18）**：每条 case 映射到
  `tests/unit/test_publisher_transaction.py`（及 idempotency /
  p2_5_http_publish / runtime_authority / server_clock_policy /
  evidence_integrity）的具体测试名与断言向量，逐条人工核读，
  无凭空勾选项。恢复类 case（8/10/14）全部经真实
  `os._exit` 子进程 + 重开句柄验证，非内存模拟。
- **冻结树四组复跑**（串行，HEAD `1d14a1e` + 本次证据包）：
  matrix 定向 **92 passed**；全套 **750 passed / 352.48s**；
  negctl **8 passed / 276.10s**；词表 **PASS**（27 sets）。
- **证据包 `tests/evidence/g2-publisher-transaction/`**：
  5 份命令日志（targeted / unit / full / negctl / vocabulary_guard，
  按 4.1 阶梯含独立 full-unit 阶段）+ EVIDENCE.json（sha256 按打包终态
  字节，CRLF→LF 归一后计算；contract SHA `b92e53f4…fe639` 硬编码自复核
  值；`case_map` 逐案映射 + `known_gaps` 缺口注记）。
  finalizer 断言在 staging 抓到 targeted.log 缺 EXIT 标记的不一致，
  以**补跑真实一次**闭环而非回填文本——该纪律沿用第 7 轮教训。
- **integrity 路径澄清（reviewer 第 9 轮指正）**：本条目初稿曾写
  “主仓最终 Git blob 1 passed”——当时证据包仍是未跟踪目录，integrity
  实际走的是 working-file fallback，不是 committed blob；该说法撤回。
  committed-blob integrity、无 .git harness integrity、bundle→temp
  clone 恢复验证，均改在**证据主体 commit 之后**执行，结论以
  attestation commit 记录被验证的准确 SHA。
- **勾账**：tasks.md 3.1–3.6、4.1 逐项对账后勾选；**4.2、4.4 暂恢复
  未勾**（其验收对象是尚未发生的 committed-blob 验证与含 G3 证据的
  bundle），待验证通过后由 attestation commit 勾选并写明被验证的
  commit SHA。Phase 0–3 + 4.1/4.3 合计 21/23。
- **negctl 卫生**：沙箱恢复为字节比对断言，本轮 8 passed 内含
  自检；无 `# MUTATION:` 残留。
- **待办**：reviewer 授权后提交 `test(g2-p3): …` 之后的收口 commit
  （tasks.md + 证据包 + 本 devlog 条目），提交后照例三连复验。

## 2026-09-23 — G3 attestation：4.2 / 4.4 验收补账（reviewer 第 9 轮方案步骤 5–7）

- **被验证对象**：证据主体 commit `4c26366`（含 5 日志证据包、
  tasks 3.1–3.6/4.1/4.3、第 9 轮四笔修正：unit 阶段补跑
  **742 passed / EXIT=0** 入包、devlog 耗时改回原始
  **352.48s / 276.10s**、4.2/4.4 曾恢复未勾、EVIDENCE.json 措辞与
  `case_map`/`known_gaps` 实际键对齐）。
- **4.2 committed-blob integrity**：主仓 HEAD=`4c26366` 时
  `test_evidence_integrity` **1 passed**（`git cat-file blob HEAD:…`
  路径），`git ls-files --eol` 确认 6 文件 i/lf w/lf；无 .git 的
  Temp harness 拷贝（fallback 工作字节路径）**1 passed**，用后即删。
- **4.4 bundle 恢复**：`git bundle create --all` → `bundle verify`
  （complete history）→ temp clone：HEAD=`4c26366`、
  `g2-baseline-5bf299a^{}`=`5bf299a`、包内 6 个证据文件齐全、
  合同 SHA `b92e53f4…fe639` 不变、**clone 内** integrity
  **1 passed**（恢复出的树自证）。bundle/clone 均已清理。
- **勾账**：4.2、4.4 现按上述实证勾选，Phase 0–4 合计 **23/23**。
  本 attestation commit 自身完成后另出一支交付 bundle 做
  `bundle verify`（覆盖含本条目的最终历史），结果记入收口报告。

## 2026-09-23 — G4 Design-First：production-gates spec 起草（纯文档，待评审）

- 类型：设计 / 评审门禁前
- 证据：`.kiro/specs/production-gates/` 四文件（README / design / tasks /
  START_PROMPT）；未 commit，等评审修订批准后按 G2 先例入库。
- G2/G3 封口事实：HEAD `855a5c0`，交付 bundle（`959,264 bytes`，
  SHA `91d90657…29692c`）+ `#` 注释前缀 `.sha256` 旁证（卫生项落盘，
  `sha256sum -c` 输出仅 OK）。
- 侦察实测（写进 design §1 事实表，Phase 0.3 复核）：
  `_persist` 行缺失静默回退（clock.py:205-206）；`clock_session` 零
  触发器；`main()` 先开 `Database()` 才在 `Server.__init__` 撞 ≥32
  secret 检查；**restore 工具不存在**（只有 backup）；`/health` 无
  version/contract 字段；`run_evals.py` fail-closed inventory 已就位。
- 范围五工作流 A–E（clock 防御纵深 / startup 一次性校验 / 健康面分层 /
  恢复闭环 / 部署边界+EVAL 姿态钉死），非目标显式含"不跑 AWS、不解锁
  EVAL、不碰 G2/G3 冻结核心"。

## 2026-09-23 — G4 spec rev.2：评审 round-1 Changes-Requested 全量吸收（仍未提交）

- P0-1 验证者不得修复被验证物：`verify_audit_connection` 纯只读抽入
  audit.py，`verify_backup.py` 走 `mode=ro&immutable=1`+`query_only=ON`、
  有 sidecar 即拒；live `verify_audit()` 的 head INSERT OR IGNORE 只保留
  在线上库路径。负测矩阵含"验证前后备份 SHA-256 逐字节相等"。
- P0-2 健康面三分：`/health` 纯 liveness 不碰库；`/health/ready` 独立
  连接 `BEGIN IMMEDIATE→ROLLBACK`、busy_timeout=PLANPILOT_READY_TIMEOUT_MS
  （默认 1500，config 一次性读取），失败/未 ack receipt 返回 503；
  `/health/deep` 仅诊断（可 200+overall_ok:false），grep 锁测试保证其
  永不进 Dockerfile/compose/gate。外部持写锁→ready 503→释放→200 真实
  测试入 tasks 3.2。
- P0-3 restore 状态机：仅服务器停止（pidfile 门）可 restore；marker→
  verify→旧 main/WAL/SHM 重命名隔离（.restore-prev-<ts>，先隔离后让新库
  可见，顺序修正）→staged copy+fsync→os.replace→receipt→清 marker；
  receipt 存在且 ack SHA 不匹配时生产启动 fail-closed、dev ready=503
  （time-travel 的旧审批/幂等集不得被静默 serving）。publisher 核心不动，
  纯 startup gate。§8 崩溃矩阵含 mid-restore hard-kill 各边界。
- P1-4 config 两层：`parse_startup_env` 纯 resolver（零 FS，secret 本体
  repr=False，summary 仅布尔）+ `preflight` FS 层；`PLANPILOT_ENV` 封闭
  集合 development|production，未知值拒启；factory_root 需自身是目录；
  环境变量一次性快照（post-snapshot 变更不得影响行为，有测试）。
- P1-5 部署边界进范围：design §9 + tasks 5.1–5.3（wheelhouse 离线装、
  compose production+只读备份卷+HEALTHCHECK→ready、容器验收；Docker 不
  可用记 BLOCKED 不算 PASS）。
- P1-6 EVAL 按正文方案：`run_evals.py` 零改动，30 BLOCKED/0 PASS 由测试
  钉死；smoke 独立脚本 `tools/agent_smoke.py` 只写 results/smoke/、行带
  kind:smoke+not_eval_backed；AWS 实跑后仓内留脱敏 manifest（含 SHA 与
  evidence 指针），raw dump 仓外。我 G3 聊天报告里的 exit 77/local-fake
  /ACK 放行措辞正式撤回——设计从未有过。
- P1-7 gate 凭据与纯度：evaluate_env(env,flags) 纯逻辑；key-file 或
  PLANPILOT_BEDROCK_API_KEY 两式皆合法（合同允许）；Git tracked scan、
  符号链接/权限检查归 I/O probes；--profile 与 PLANPILOT_ENV 不一致=FAIL。
- P1-8 防同名空壳 trigger：Database 启动比对 sqlite_master 规范化 SQL
  摘要，缺失或摘要不符拒启；哨兵测试双向（drop 后 bypass 必红；plant
  no-op 必拒启）。
- P1-9 negctl §13 增列 ready 吞错 200 / restore 跳 verify / receipt 绕过
  / secret 门删除 / hollow trigger 接受 等 must-fail 变异，收口时锁精确
  计数（基线 58=56+2 起算）。
- A 部分按批准保留：A1 fail-closed _persist / A2 三触发器（DELETE、锚点
  UPDATE、version 回拨；UPDATE 放行合法高水位）/ A3 五腿篡改矩阵。
- tasks 现为 27 项全未勾（评审门禁）；README/design/tasks/START_PROMPT
  四件套措辞已对齐同一方案（无 /api/v1/health、无 exit-77 残留）。

## 2026-09-23 — G4 spec rev.3：§0 事实底座在 855a5c0 上整体重建（round-2 谱系串污退回；仍未提交）

- P0-1 承认：rev.2 §0 大面积来自另一套数据模型（host/pid/txn_id/boot_id/version
  列、db.clock/clock_history/clock_service_session、digest_history 与
  approval_records_source_key_check、静默匿名 _auth、只读 /health、
  verify_audit 修 head、backup 返回 dict、status.json、eval exit 2、
  docker-compose.yml、Sonnet 4.6、results/agent_eval——全部不存在或不成立）。
- 修法：评审员逐文件重读工作树（clock.py 277 行 / persistence.py 231 行 /
  api_server.py 425 行 / security.py / backup.py / audit.py / 两部署工具 /
  Dockerfile / compose.yaml / 合同 runtime 段），并跑两个实时探针：
  clean-close 后 -wal 仍在；run_evals 实测 cases=30 passed=0 blocked=30
  EXIT=1。design §0 重写为 F1-F7 fact table，§11 建撤回词表，四件套
  （design/tasks/README/START_PROMPT）全部按 rev.3 对齐；tasks 计数实测
  44 项（README 同步；上轮"36"口头数字错误，当时实际 27，再认一次）。
- 结构修订：A 触发器改在真实 4 列上（no_delete / anchor 两列禁 UPDATE /
  last_issued 禁回拨；正常 forward-UPDATE 保留为回归钉）；A1 明示必改
  clock.py；"删守卫必红"哨兵只进 negctl 沙箱。health：/health 语义原样
  保留（显式兼容决定，test_requirement_delivery:157 不红），三分只加新
  路径；deep 认证失败=403（本仓库惯例），clock.status 形状写实。restore：
  OS 级互斥锁（msvcrt/fcntl 分支）取代 pidfile、marker 带 phase
  （PREPARED→QUARANTINED→REPLACED→RECEIPTED）逐相幂等 resume/rollback、
  quarantine 保留原文件名、receipt 原子写并绑 backup SHA+audit head+
  generation、未 ack 全环境拒静默启动、diagnostics-only 结构性摘除写路由、
  hard-kill 后重跑必须收敛。deploy/EVAL 落回真实文件（compose.yaml、
  Dockerfile /health→/health/ready、30/0/exit1 按实测钉死、smoke 隔离
  目标 tests/evidence/runtime-eval、wheelhouse 只提交 manifest+生成器、
  二进制不进 Git）。大写合同状状态码全部收回为内部类型+小写报告值。
- 验证：撤回词全库 grep 仅存于 §11 撤回登记表本身；标题编号交叉引用修正
  （§3 config/§4 health/§5 restore/§6 eval 行、§11 互指）；封闭词表 PASS；
  git diff --check 0；HEAD=855a5c0 未动；合同 SHA 未动；纯文档零测试。
  清理杂散 NUL 文件（上轮探针重定向误建）。
- 边界：等 round-3 复审后再做 spec commit；Phase 1 之前零代码。

## 2026-09-23 — G4 spec rev.4：round-3 三个 P0 全部按评审实测重建（纯文档，仍未提交）

- P0-1（ready 证不了可写）：接受评审 mode=ro 实测（BEGIN 可过、INSERT 被拒）。`/health/ready` 改独立 **rw** 连接 + 短 busy_timeout + BEGIN IMMEDIATE→ROLLBACK；`checked_at=clock.now()` 撤销（readiness 探针不得含写路径，外部锁下还可能超预算）。补 falsifier：ro-open BEGIN-ok 连接不得判 ready；只读目录/文件 → 503。
- P0-2（diagnostics-only 污染未确认库）：整模式删除。`Database()` 构造本身执行 DDL/head 修复/trigger 安装，ScenarioClock attach 写 clock_session——"未 ack 库上做在线诊断"在本代码库是矛盾。R4 改 zero-HTTP：未 ack 全环境 socket 根本不 bind；诊断走既有离线 CLI（verify_backup / restore --status / --show-receipt），不新建 recovery_status.py。
- P0-3（四相 marker 盖不住 op↔marker 更新间隙）：marker 升级为 intent ledger——每笔文件操作带源/目标 SHA 记 pending/done，phase 仅当全部 op done 才推进（marker=下一步意图，ledger=已完成事实）；重跑先 reconcile（文件系统+SHA 推导实况，不信标签）；kill 矩阵扩到 6 点（K 后 staged 前 / stage 后 / main 移后 / WAL 部分移后 / replace 后 phase 更新前 / receipt 后清 marker 前），每例二次 restore 必须收敛。
- P1：[LP-1] 撤回（评审与我的复验一致：干净 close 无 -wal 残留；开放写事务不 close 才出现 wal/shm）→ 改为条件性表述，restore 仍无条件处理三件套，Phase 0 验收删"必残留"要求；audit hash 规则按 persistence.py:224 逐字改 `sha256((prev+row["record"]).encode())` 验存储串、禁 re-canonicalize（空白篡改负例）；verify_backup_file 补 integrity_check/WAL-sibling 拒绝/9 核心表/链走/SHA 不变；scheduled_backup 真接 verifier + last_verified_backup.json manifest + deep age 读 manifest（新增 task 4.3）；SecretStr→field(repr=False)；返回类型统一 tuple；"dev boots"改"dev with valid secret boots"；Bedrock 按调用重读列为快照制例外；env inventory 改 rg 生成器（实测 19 vars/21 points，PLANPILOT_HEALTH/AUTHORITATIVE_RUNTIME 零读取点确证）。
- 流程：spec commit 从 task 0.6 提出为"评审批准后的独立前置提交"（含四件套+devlog），Phase 0 验 855a5c0 为祖先而非相等；Phase 0 探针日志先落仓外 staging，evidence 目录 Phase 6 才建；Docker 缺位=G4 整体 BLOCKED（不许勾 5.3 继续宣称完成），仅 AWS/EVAL 允许 BLOCKED 非目标。design §11 增 rev.4 撤回登记 12 条；README/START_PROMPT/tasks 全同步；tasks rev.4 checkbox 实测 35 项（grep -c 于写入时计数，禁跨版本沿用）。
- 守卫：词表 PASS；git diff --check 0；HEAD 855a5c0 未动；合同 b92e53f4…fe639 未动；本轮零测试执行（评审指示：只重跑事实探针+词表+文档卫生）。

## 2026-09-23 — G4 spec rev.5：round-4 的 2 个 P0 + 6 项漂移对齐（小型修订，架构未动）

- P0-1（ACK 语义）：按评审建议定死。PLANPILOT_RECOVERY_ACK 环境变量彻底撤销；唯一路径 restore_database.py --ack 原子写 sidecar，绑定 receipt_sha256（receipt 文件自身哈希→任意字节改动即 void）+ generation + backup_sha256 + target_db + operator/ts + restored_db_sha256_at_ack（仅确认时证据，绝不做启动恒等比较）；每次新 restore 在首个文件写入前以 ledger op 形式原子失效旧 ACK（kill 安全）→ 同备份第二次恢复必须重新确认。四条回归进 4.7。
- P0-2（锁顺序）：钉死启动顺序 env→parse/non-DB preflight→OS lock→marker/receipt/ACK gate→Clock→Database→services→bind；关序 HTTP→DB close→lock 最后释放。依据：现 main() Database@:409 先于 Server@:414，锁放 Server 内 = 先污染后上锁。测试：lock/gate 拒绝时 Database.__init__ spy 零调用、socket 未 bind、恢复库 SHA 不变、启动中途 restore 抢不到同一把锁。
- P1 六项：8731/0.5s→8080（api_server.py:414+Dockerfile+compose 实测）/ready_timeout_ms=1500；"Dockerfile/compose 已设 PLANPILOT_ENV"改"Phase 5 将新增"（grep 证实两者均无）；env inventory 改 AST 扫描（regex 版确漏 clock.py:231 mapping 读取与 publisher FAULT_ENV_VAR 常量间接 :337/:363），19/21 降为基线快照、G4 落地后重生成，禁写死；EVAL 验收删幻影 BLOCKED: 行、按 run_evals.py:72-73 真实两行+exit 1；smoke 输出改回其真实默认 tests/evidence/compact-smoke（rev.4 误写成正式 EVAL 目录，脚本本就禁写 runtime-eval）；wheelhouse 统一 deploy/wheelhouse/MANIFEST.json + tools/build_wheelhouse.py + 仓外 wheel 目录三件套、manifest 跟踪 .whl 不跟踪；design §10 "×3 phases" 残留改 six kill points × {resume, rollback}。
- 非阻塞建议全部吸收：ready 措辞收紧为 opens-RW+writer-reservation（非 durability/free-space 证明）；九表=有意的 full-runtime-only 政策，verdict 输出缺失表名、Phase 4 fixture 走完整 runtime 路径；backup_database() 保持 -> None，manifest 由 scheduled_backup 工具层组装，不动公共 API。
- 版本头 rev.3→rev.5 同步（自查发现的标题漂移）；§11 增 REV.5 撤回登记（ACK env、锁序、8731、BLOCKED: 幻影、smoke 目录、三套 wheelhouse 名、PLANPILOT_ENV 现状误报、regex inventory）。README/START_PROMPT/tasks 同步。checkboxes 35 不变（grep 现测）。
- 守卫：词表 PASS；git diff --check 0；HEAD 855a5c0、合同 b92e53f4…fe639 未动；零测试执行（评审：一轮小修即可进 Phase 0/1）。

## 2026-09-23 — G4 spec rev.5b：round-5 五处提交前修正（文档级，架构未动）

- #1 backup_database() 矛盾清除：tasks 4.3 旧文"returns a real result +
  clock stamp"删除，与 design V3 统一为保持 -> None、scheduled_backup
  自行推导确定性文件名+算 SHA+调 V1 验证+写 manifest。
- #2 START_PROMPT/README 快照措辞：改为"启动配置字段只快照一次；
  Bedrock 六变量保留 per-call 重读以支持凭据轮换（唯一例外）"。
- #3 ACK 篡改声明收窄（采纳评审推荐项）：安全绑定字段=receipt SHA/
  generation/backup SHA/target_db 四项，门禁=格式非法/缺失/过期代际/
  四字段不匹配；operator/timestamp 明示仅溯源、同格式编辑不承诺可检测。
  design R4、§10 矩阵行、tasks 4.6/4.7、START_PROMPT 同步；§11 增补
  REV.5 撤回登记一条（首次写入时曾误重复两份+两处 V3 段落重影，已去重
  并 grep 复核单份）。
- #4 backup_root 未定义修正：tasks 2.1 改为 development=ROOT/backups
  （与 backup.py 现默认一致）、production 必须显式配置、compose 容器内
  /backups。
- #5 EVAL 验收路径化：design [LP-2] 与 tasks 0.3 改为 line1 断言
  ROOT 前缀+runtime-eval 后缀（run_evals.py:17 拼绝对路径），line2 精确
  匹配 cases=30 行+exit 1。
- 自查追加：START_PROMPT Phase-0 条目仍残留 rev.3 的"[LP] clean-close
  -wal 残留"旧说法与旧四步流程，重写为 rev.5b 探针四件套（WAL 条件形态/
  ro-rw/EVAL 两行/六 kill 点），并补"日志先落仓外 staging"。
- 复核：tasks 35 checkbox；词表 PASS；git diff --check 0；HEAD 855a5c0、
  合同 b92e53f4…fe639 未动。纯文档，未跑测试。

## 2026-09-24 — G4 Phase 0 完成（spec commit c7e6398 之后）：事实复验四项全过，AST inventory 实证 20/22

- 0.1 `855a5c0` 为 HEAD `c7e6398` 祖先 ✓；合同 SHA `b92e53f4ff05…` ✓；
  tag g2-baseline-5bf299a 解引用 `5bf299a` ✓。
- 0.2 §0 事实表逐行复核 ✓：clock.py 277 行 / persistence.py 231 行 /
  api_server.py 425 行；clock_session 三列 + singleton；触发器仅
  audit_chain_* 3 个；repair-at-init(INSERT OR IGNORE head) 在 :134；
  backup_database -> None；scheduled_backup 返回 {backup,removed}、
  无 status.json；auth secret <32 直接 ValueError；/publish 为实际路由；
  Dockerfile/compose 8080、healthcheck /health、compose backups:/backups；
  smoke OUT=tests/evidence/compact-smoke；合同 EVAL-* 唯一 id=30
  （裸 grep 曾报 31，经 JSON-walk 复核实为同一集合，差异是 grep 模式）。
- 0.3 [LP] 探针重跑（日志在仓外 staging E:/PlanPilot-Hackathon/
  g4-probe-staging/，Phase 6 拷入 evidence）：
  probeA WAL：construct+close(无事务)→目录只剩 probe.db；开放写事务未
  close→probe.db/-shm/-wal 三件齐；close 后归一。条件形态确认。
  probeB EVAL：line1 绝对路径(E:\…\runtime-eval)、line2
  cases=30 passed=0 failed=0 blocked=30、EXIT=1，与 §0 逐字一致。
  probeC ro/rw：ro begin=ok→insert 拒(attempt to write a readonly
  database)；rw begin=ok→insert ok。P0-1 语义复证。
- 0.4 tools/probes/inventory_env.py 交付（AST 扫描，含模块常量间接解析+
  mapping 参数跟随；os.environ.get / environ[...] 两类）。实测
  ALL vars=20 points=22（正则法 19/21 漏了 publisher FAULT_ENV_VAR 常量
  间接，AST 正确捕获 PLANPILOT_PUBLISHER_FAULT:363 即 +1 var/+1 point
  ——这 1 差正是 tasks 0.4 要求 AST 的理由）；PROD 子集 vars=15
  points=17。19/21 已按 round-4 要求降级为对比基线。
- tasks.md 勾 0.1-0.4（35→31 未勾）。六 kill 点矩阵探针属 Phase 4 交付
  物，不在 Phase 0 空跑（诚实登记）。

- 复跑事故登记：probeB 让 runner 重写了已封口的
  tests/evidence/runtime-eval/EVIDENCE.json（仅 generated_at 字段变化，
  其余 SHA 内容一致），已 `git checkout` 复原——封口证据不因复跑刷新；
  Phase 0 探针证据一律以仓外 staging 日志为准。后续复跑 run_evals 的
  任务（如 Phase 6）须先确认是否允许刷新该文件。

## 2026-09-24 — G4 Phase 1 完成：clock_session 防御纵深（A1 fail-closed + A2 三触发器 + shape guard + A4 negctl）

- A1 clock.py::_persist：row-None fail-open（return stamp）改 raise
  RuntimeError；诚实路径不可达（attach 先 INSERT 后绑 _db），仅篡改可达。
- A2 persistence.py executescript 追加三触发器（真 4 列；no_delete /
  anchor_immutable(scenario_anchor,real_wall_started_at) / no_rewind
  (WHEN NEW<OLD last_issued，前向 UPDATE 保持合法)）；RAISE 实测抛
  IntegrityError（非 OperationalError），测试按实测断言。
- A2' shape guard：_CLOCK_TRIGGERS 规范文本单点声明；构造后逐 trigger
  比对 sqlite_master 存储体（逐行折叠+去 IF NOT EXISTS 后 sha256）。
  实测钉住：IF NOT EXISTS 对同名空壳静默 no-op → 预植 hollow shell 被
  digest 拒绝构造。残留洞诚实登记：out-of-band DROP TRIGGER 后重开等同
  pre-G4 resume 文件（拒绝它会砖化合法升级路径），重开自愈重装规范体，
  test_out_of_band_dropped_defence_self_heals_on_reopen 钉住该语义。
- rename 探针：ALTER TABLE/RENAME COLUMN 不被 BEFORE 触发器拦截，但触发器
  随表迁移继续拦截 DELETE/UPDATE（digest 会因 ON "新名" 失配而拒开）——
  行为已验证，未额外造轮。
- A4 tests/negative_control/test_clock_defence_negctl.py：两变异
  （strip 三触发器 / _persist 回 fail-open），socket-free focused 子集
  （test_clock_session_defence + test_plan_store_persistence），
  caught=2 escaped=0；real tree 字节哈希复原断言。
- 测试适配 1 处：test_pending_approval_cannot_be_revived_by_restart 原靠
  回写 real_wall_started_at 模拟停机——恰是新防线拦截的篡改形；改为平移
  观察器（clock_mod.datetime 包 _ShiftedDatetime 覆盖 Database+_start 的
  attach 窗口），语义等价（downtime=REAL elapsed）且不再自我篡改。
- 数字：unit 750 passed（+8 defence 文件，21 clock-policy 全绿不变）；
  negctl 8→9 passed（333.64s 全量）；定向 43 passed；词表 PASS；
  git diff --check 干净；tasks 勾 1.1-1.4（未勾 31→27）。

## 2026-09-25 — G4 Phase 1.0.1：Batch-A 审查五缺口收口（fast-track，Reviewer 指令直接授权）

- #1 missing-trigger 语义对齐：design §2 + tasks 1.4 + persistence 注释
  改为诚实三态（launch 前缺失=IF NOT EXISTS 自愈安装，不拒开；同名异体
  =digest fail-closed；out-of-band 删 trigger+行=特权编辑器已知限制）。
  删除"missing→RuntimeError"失实措辞；A3 异常类型按实测钉成
  IntegrityError（原文误写 OperationalError）。
- #2 rename 勾账：实测 raw `ALTER TABLE ... RENAME COLUMN` 成功且
  SQLite 同步改写 trigger 存储体 → 下一次 Database() 打开因 digest
  失配 RuntimeError。新测试 test_rename_column_is_not_aborted_but_next_
  open_fails_closed 钉真实语义；tasks 1.4 不再声称 rename 当场 abort。
- #3 第三路 negctl：mutation "shape guard neutered to pass-through"
  （沙箱副本把 guard 条件改成 if False）→ hollow pre-plant 测试必须
  失败。caught=3 escaped=0 broken=0；真实树前后逐文件 sha256 一致。
- #4 构造失败连接泄漏：__init__ 主体移入 _open()，BaseException 统一
  close-if-opened 后 re-raise 原异常（不吞不 mask）。回归
  test_failed_open_releases_handle_no_gc：refused open 后立即
  os.rename 成功，不依赖 gc.collect()（Windows 实测）。
- #5 fingerprint 收紧：废除全局 IF NOT EXISTS 正则删除（会误删 RAISE
  字符串内同文，两个不同体可碰撞）；只规范空白+单个尾分号；re import
  已删。sqlite_master 实测本就不存结构位 IF NOT EXISTS，无需 strip。
  test_fingerprint_does_not_strip_text_inside_strings 钉双指纹不同。
- 数字：定向 defence 11 passed（8→11，+3 pins）；clock-policy 21；
  unit 750→753 passed；negctl 9 passed（clock_defence 文件内
  caught=3/0/0）；词表 PASS；git diff --check 0；合同 SHA
  b92e53f4ff054105… 不变。tasks 无新勾（1.1-1.4 本已勾，语义已改对）。

## 2026-09-25 — G4 Batch A 收口：Phase 2 startup config + Phase 3 health split + ready 探针只读文件假绿实证

- Phase 2（design §3）：新 `src/planpilot/startup_config.py`。frozen `StartupConfig`，secret 持真值但
  `field(repr=False, compare=False)`；`PLANPILOT_ENV` 闭集 ENVS=("development","production")；默认
  host 127.0.0.1 / port 8080 / ready_timeout_ms 1500 / clock wall；backup 默认 ROOT/backups（无
  Path(home) 幽灵变量）。`parse_startup_env` 纯函数（dict 进，不读 os.environ 不触 fs），preflight 独揽
  I/O；`startup_or_die` 组装；`config_provenance()` 只输出布尔/计数/路径，main() 在 Database 构造前记录一次。
  `api_server.main()` 只 `dict(os.environ)` 一次，Server 从 cfg 构造；Bedrock 六变量保持 per-call 重读
  （测试双向钉死：字段不冻结 + 轮换即时生效）。定向 21 passed。
- Phase 3（design §4）：/health 逐字不变（含 db.lock 下 SELECT 1，消费者测试零改动）；/health/live 零 DB
  零时钟；/health/ready 每请求独立 plain connect（默认 RW）+ 单一预算 ready_timeout_ms + BEGIN IMMEDIATE
  + ROLLBACK，无 checked_at；/health/deep `_auth("plan")`→403 仓库惯例，恒 200 + overall_ok，backup 年龄
  只认 last_verified_backup.json（缺→no_verified_manifest，绝不猜 mtime）；automation 结构测试证明无人
  把 deep 当门禁。探针钉住勾账两处失实并改正：(1) tasks 3.3 的 X-PlanPilot-Token 头在本仓库不存在
  （grep 全仓零命中，design §4 本来就是 _auth）；(2) 只读文件上 BEGIN IMMEDIATE 竟然被接受、只有写被拒
  （Windows 探针，delete journal 与 WAL 双模式实证）——reservation-only 探针会在 chmod-444 库上假绿，
  故探针补一条 in-txn `CREATE TABLE _pp_ready_probe`（ROLLBACK 零残留，测试对账 sqlite_master 集合），
  design §4 同步 [IMPL NOTE]。negctl 升至五路（新增 #4 去掉写锁预约 / #5 去掉 in-txn falsifier），
  caught=5 escaped=0 broken=0，真树前后逐文件 SHA-256 一致。
- Batch A 冻结验证：见下一条提交注释中的原始数字。

## 2026-09-25 — G4 Batch A 硬化（评审 P0 + 4×P1 + P2 落修）

评审结论「Batch A 暂不批准进入 Phase 5」逐条现场核实后全部修复：

- **P0 合法启动崩溃**：`tools/api_server.py` main() 传 `backup_root=cfg.backup_root`
  ——StartupConfig 字段实名 `backup_dir`（startup_config.py），AttributeError 在
  Database 已构造、try/finally 之前抛出。改为 `cfg.backup_dir`；新增
  `test_main_with_valid_env_assembles_and_closes_cleanly`：合法快照驱动真实
  main()（ServerSpy 替换 serve_forever，捕获接线后 handle_request 服务一次真实
  /health/live 再返回），证明 (1) Database 在快照路径真实构造 (2) Server 收到
  cfg.backup_dir (3) main() finally 关闭 server（socket 再连必拒）与 DB (4) 全程
  零 AttributeError。此前 main() 测试全部走非法配置提前退出，合法路径从未被装配
  测试覆盖——评审属实。
- **P1-1 readiness 假绿幽灵库**：普通 `sqlite3.connect(path)` 在文件丢失时创建
  零字节 DB 并让 BEGIN/CREATE/ROLLBACK 全成功 → 503 变 200。改
  `file:<resolved>?mode=rw` URI（本批复测：missing→CANTOPEN 且**不创建文件**；
  既有文件等价默认 RW；chmod-444 仍可开、in-txn CREATE falsifier 独立兜底）。
  新增 `test_ready_503_when_db_file_missing_and_never_creates_it`（含 -wal/-shm
  零副产物）；negctl 升至**六路**（#6 回退 plain connect → 缺文件测试必须红），
  caught=6 escaped=0 broken=0。
- **P1-2 production 不拒 loopback**：tasks 2.4 承诺未兑现，parse 现在
  LOOPBACK_HOSTS=(127.0.0.1/localhost/::1/::ffff:127.0.0.1) + production = error
  （与 clock.py 同一拼写族）；dev 默认不动。参数化测试 3 拼写全拒 +
  0.0.0.0 通过 + dev 保留 loopback；既有 2 个 production 用例改为显式非回环主机。
- **P1-3 deep overall_ok 未按任务聚合**：改为唯一公式 `not gate_failures`：
  audit 断链/head 异常、clock.status() 抛异常、idempotency 查询异常 = 任何环境
  皆 false；agent 未配置 / manifest 缺失 = production false、development warning
  （本地兜底是设计意图）；响应带 `gate_failures` 列表；HTTP 恒 200 不变。新增
  `test_deep_overall_ok_aggregates_every_gate`（含 DROP idempotency_registry
  真变异、_BoomClock 桩、server.env 生产翻转三段）。
- **P1-3 尾：manifest 年龄 RFC3339**：deep 只读 `verified_at_epoch/mtime_epoch`，
  真实 Phase 4 `verified_at` RFC3339 字段会被算成 age≈0 假绿。新增
  `_manifest_verified_epoch`：RFC3339 优先（Z→+00:00 兼容 3.11），显式 `*_epoch`
  浮点兼容，缺失/不可解析 = manifest_unreadable + gate failure，绝不 age 0。
  `test_deep_reads_rfc3339_verified_at_not_epoch_guess` 用 26h 前戳钉死
  （旧代码此处必 0）。
- **P1-4 Bedrock 六变量声明不实**：现场核实 credential(:109)/network switch
  (:124/:127) 真 per-call，region/model/daily(:78-90) 仅 __init__ 读。不重构
  Bedrock；startup_config 模块头、design §3 F2、README、START_PROMPT 全部收紧为
  「凭据与网络开关支持运行时轮换；region/model/daily 构造时读取」。测试从
  「源码含 os.environ」grep 改为真实行为：`test_bedrock_credential_rotation_is_a
  _real_per_call_reread`（真构造 Client→翻 env→_credential() 返回新 key）+
  `test_bedrock_runtime_reads_are_scoped_behaviourally`（FORBID 开关 per-call 翻
  转生效、converse 拒绝路径、region/daily 构造时读且**新构造**才见新值）。
- **P2 provenance 措辞**：实际记录含 host/port/三路径，「只输出布尔/计数」不实。
  摘要/文档统一改为「非敏感配置值与路径 + secret 仅 present/length」。
- 文档同步：design §3（loopback/F2/summary）§4（mode=rw 协议、deep 聚合语义、
  测试 (d)）、tasks 2.3/2.4/3.2/3.3、README、START_PROMPT 同步；negctl docstring
  五路→六路。Server 增加 `env` 参数（默认 development，fixture 兼容），deep 据此
  区分 dev/prod 语义。
