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
- 发布事务、审计链(append-only hash chain)、调度器
- 数据集 + EVAL-001~030(官方评分的实际战场;9/28 截止前须留 ≥5 整天)
- git 远程 / bundle 备份(三个审计 tag 目前全在 E 盘单点)
