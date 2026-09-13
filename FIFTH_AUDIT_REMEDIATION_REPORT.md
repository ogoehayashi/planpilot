# PlanPilot `plan-store-and-digest` 第五轮审查整改报告

> **历史报告：** 本轮结论与数字已由
> `SIXTH_AUDIT_REMEDIATION_REPORT.md` 取代。以下保留为审计轨迹，不代表
> 当前交付状态。

**日期：** 2026-09-13  
**整改基线：** 第四轮未提交工作树（Git HEAD `fc908d7`）  
**契约：** `contract/planpilot_agent_contract_v1.8.json`  
**契约 SHA-256：** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

## 审查结论

第五轮指出的 P0 持久化自锁成立，现已修复并增加结构性回归测试。
`plan-store-and-digest` 可以提交给外部审查者判断是否推进下一个 part；
该结论仅针对本模块，不代表 PlanPilot 已达到 pilot-ready。

## P0 复现

第四轮代码存在两条进入 SUPERSEDED 的路径：

```text
supersede()/commit_new_version()
  -> lifecycle.status = SUPERSEDED
  -> append immutable supersede event
  -> dump/load 正常

transition(..., "SUPERSEDED")
  -> lifecycle.status = SUPERSEDED
  -> 不产生 event
  -> dump 成功，但 load_state() 永久拒绝
```

后者违反了“每个 SUPERSEDED lifecycle 恰好对应一个历史事件”的持久化
不变量。由于 SUPERSEDED 本身不可恢复，产生的 dump 无法通过公共 API 修复。

## 修复决策

采用审查建议的方案 (a)：SUPERSEDED 不是公共 lifecycle transition target。

- `transition()` 对所有起始状态拒绝直接进入 SUPERSEDED；
- 内部 `_transition(..., allow_superseded=True)` 仅供原子退役路径使用；
- `supersede()` 统一负责更新 lifecycle 并追加 immutable event；
- `commit_new_version()` 继续通过 `supersede()` 原子退役旧版本；
- PUBLISHED 不能回退至 DRAFT/PROPOSED/BLOCKED 等可变态，但可由
  `supersede()` 在版本替换时退役；
- 文档不再把 SUPERSEDED 描述为 `workflow.transitions` 的一条边。契约中的
  依据来自 `plan_store.versioning`：新版本重生成后，旧版本被 supersede。

没有让公共 `transition()` 自己创建 event，因为那会形成第二套事件构造逻辑，
重新引入入口漂移风险。

## 新增结构性防线

`tests/unit/test_audit4_regressions.py` 新增：

1. 构造 DRAFT、PROPOSED、BLOCKED、AWAITING_APPROVAL、APPROVED、
   PUBLISHED、SUPERSEDED 七种公共可达状态；
2. 对每种状态验证直接 `transition(..., "SUPERSEDED")` 被拒，且拒绝前后
   lifecycle 与 pending event 均不变化；
3. 对每种状态验证 dump → load → dump 字节一致；
4. 验证 PUBLISHED 版本通过 `commit_new_version()` 重生成后，旧版本变为
   SUPERSEDED、新版本为 DRAFT、恰有一个 pending event，且可正常 reload；
5. 验证低层 `supersede()` 仍保留已发布版本的 `published_version` 审计信息。

Store negative control 新增第 56 个 mutation：删除公共 SUPERSEDED route gate。
只有对应回归测试失败时才算 caught，防止仅验证 load 侧而遗漏 write 侧。

## 同步修正

- `tests/unit/test_audit3_regressions.py` 的陈旧 `_AUTHORITY_STATUSES` 注释已改为
  `_ACTIVE_ONLY_STATUSES`；
- `design.md`、`IMPLEMENTATION_NOTES.md`、`REVIEW_HANDOFF_IMPLEMENTATION.md`
  和 `REVIEW_PROMPT.md` 已同步第五轮语义与数字；
- 第四轮报告已标记为历史报告，避免外部审查者引用过期结论；
- 外部审查入口现包含五轮审计，不依赖仓库外 scratch 文件。

## 最终验证

| 门槛 | 结果 |
|---|---|
| unit suite | **414 passed** |
| full suite | **417 passed** |
| store negative control | **54 caught / 0 escaped / 0 broken fixtures of 56**，另有 2 个 defence-in-depth |
| source restoration | **true** |
| closed-vocabulary guard | **PASS**，21 vocabularies / 135 members / 32 modules |
| guard self-test | **PASS**，17 cases |
| Kiro workspace validation | **ok=190 fail=0** |
| workspace negative control | **caught=14 escaped=0 of 14** |
| handoff fact-check | **fails=0** |
| contract hash | **未改变** |

证据位置：`tests/evidence/plan-store-and-digest/`。

## 是否可以推进下一个 part

建议结论：**可以提交外部审查；外部审查确认本报告和证据后，可推进下一个
part。** 推荐下一个 part 为 `approval-service`，优先实现：

1. pending supersede event 的幂等 invalidation；
2. 只有外部副作用提交成功后才 acknowledge；
3. 在副作用前、提交后、ack 前三个 crash point 的恢复测试；
4. approval set 与 plan_id/version/digest 的不可变绑定。

仍不得声称整体 pilot-ready：scheduler、dataset migration、tool layer、audit
chain 和 EVAL-001..030 尚未完成，store 仍为无锁/WAL 的单进程内存实现。

## 交付状态

整改仍在工作树中，**尚未提交 Git**。接收方应先运行 `REVIEW_PROMPT.md`
中的验证命令，再决定是否合并及进入下一 part。
