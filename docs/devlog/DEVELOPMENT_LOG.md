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
