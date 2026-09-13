# PlanPilot `plan-store-and-digest` 第四轮审查整改报告

> **历史报告：** 第五轮只读审查随后发现了 direct-SUPERSEDED route 缺陷。
> 当前结论与验证数字以 `FIFTH_AUDIT_REMEDIATION_REPORT.md` 为准。

**日期：** 2026-09-13  
**整改基线：** `fc908d7`  
**契约：** `contract/planpilot_agent_contract_v1.8.json`  
**契约 SHA-256：** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

## 结论

本轮发现的 store-local P0/P1 语义缺陷已经修改并补上对抗性回归。
`plan-store-and-digest` 可以作为后续模块的实现基础，但 **PlanPilot 整体仍不能
进入真实生产或 pilot**：approval service、调度器、数据迁移、工具层和
EVAL-001..030 尚未完成。

最重要的边界是：本轮建立的是可持久化、可重试的 supersede outbox，
不是对尚未实现的 approval service 声称端到端 exactly-once。

## 审查发现与修改

| 优先级 | 已复现问题 | 修改方案 | 永久验证 |
|---|---|---|---|
| P0 | 旧的 `drain_superseded()` 会删除唯一事件；消费后 dump 再 load 被拒，消费者在副作用与删除之间崩溃也无法安全恢复 | 事件历史改为 append-only；新增 `pending_superseded()` 与 `acknowledge_superseded()`；ack 单独持久化并支持幂等重试 | `test_audit4_regressions.py` 的 retry、crash-before-ack、ack round-trip、idempotency 用例 |
| P0 | stale v1 在 v2 存在时仍能进入 `AWAITING_APPROVAL` | `AWAITING_APPROVAL`、`APPROVED`、`PUBLISHED` 统一要求目标为当前 active version | stale-authority 回归 + mutation |
| P0 | `DRAFT -> PUBLISHED` 无审批可成功；`published_version` 可与目标版本不一致 | PUBLISHED 必须从 APPROVED 进入、存在 approval binding、且 `published_version == plan_version` | direct-publish 与 wrong-binding 回归 + mutation |
| P1 | 已绑定的 `approval_set_id` 可被替换；PUBLISHED 可回退到可变态 | approval binding 改为 write-once；SUPERSEDED 为终态，PUBLISHED 仅允许在重生成时转为 SUPERSEDED | replacement 与 successor-rule 回归 + mutation |
| P1 | dump 可注入错误 `invalidation_cause` 或与 lifecycle 不一致的事件时间 | load 时要求 contract cause 精确匹配，并要求 `superseded_at == lifecycle.updated_at` | forged-cause / forged-time 回归 + mutation |
| P1 | dump 中版本 `[1, 42]` 可绕过连续递增规则 | 每个 plan 的内容版本在 load 时必须 pairwise contiguous | version-gap 回归 + mutation |

## 持久化与兼容性

状态 envelope 新增可选的 `superseded_acks`：

- 老 dump 没有该字段时按空列表读取，保持向后兼容；
- event 与 ack 都使用 closed schema；
- 拒绝重复事件、孤儿事件、重复 ack、孤儿 ack、错误 digest、错误 approval
  binding，以及早于 supersede event 的 ack 时间；
- 每个 `SUPERSEDED` lifecycle 必须恰好有一个历史事件，即使它没有绑定
  approval set；
- `pending_superseded()` 只隐藏已确认事件，不删除历史，并返回 defensive
  copy，调用者不能修改 store 内部状态。

## 代码与测试变更

主要实现：

- `src/planpilot/store/plan_store.py`
- `src/planpilot/store/persistence_schema.py`
- `src/planpilot/store/errors.py`
- `src/planpilot/validation/schema.py`

主要新增/扩展验证：

- `tests/unit/test_audit4_regressions.py`
- `tests/unit/test_plan_store_invariants.py`
- `tests/unit/test_plan_store_persistence.py`
- `tests/negative_control/test_plan_store_negctl.py`

负控从 44 增至 55 个 mutation：53 个必须使 suite 失败，2 个由独立防线继续
拦截；要求 `escaped=0`、`broken_fixtures=0`，并验证 mutation 后源文件逐字节
恢复。

## 最终验证记录

以下命令均从仓库根目录、使用 Python 3.11 运行：

```text
python -m pytest tests/unit -q
python -m pytest tests/negative_control -q -s
python -m pytest tests/ -q
python tools/check_closed_vocabularies.py --self-test
python tools/check_closed_vocabularies.py
python tools/validate_kiro_workspace.py
python tools/negative_control_workspace.py
python tools/factcheck_impl_handoff.py
```

最终应与重建后的 `tests/evidence/plan-store-and-digest/` 一致：

| 门槛 | 结果 |
|---|---|
| unit suite | **399 passed** |
| full suite | **402 passed** |
| store negative control | **53 caught / 0 escaped / 0 broken fixtures of 55**，另有 2 个 defence-in-depth |
| closed vocabulary guard | **PASS**，21 vocabularies / 135 members / 32 modules |
| guard self-test | **PASS**，17 cases |
| Kiro workspace validation | **ok=190 fail=0** |
| workspace negative control | **caught=14 escaped=0 of 14** |
| handoff fact-check | **fails=0** |
| contract hash | **未改变** |

## 尚未完成与下一步

1. 实现 approval service：幂等执行 invalidation，副作用成功提交后才调用
   `acknowledge_superseded()`；补 crash-point integration tests。
2. 明确 F-STORE-01：契约 18 个 error code 没有适合表达非法 lifecycle transition
   的 code。目前把它视为程序错误并抛 `StoreInvariantError`，不伪造成 tool error。
3. 完成 dataset migration、material reservation、deterministic scheduler 和 tool
   layer 后，再执行 EVAL-001..030；在此之前不得声称 pilot-ready。
4. 当前 store 仍是单进程内存实现，没有锁、WAL 或多进程并发保证；demo 可用，
   生产不可用。

## 交付状态

本报告对应的修改当前位于工作树中，**尚未提交**。外部接收方应先运行上述
验证命令，再决定是否合并；不要仅依据本报告中的数字验收。
