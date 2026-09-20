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
- **P1-3**：scenario 模式必须显式 `PLANPILOT_SCENARIO_ANCHOR`（严格 +08:00
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
