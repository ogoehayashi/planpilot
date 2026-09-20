# Start prompt for the implementing Codex/Kiro task

Copy everything below the divider into a new task opened at the root of the
PlanPilot repository.

---

你现在负责 PlanPilot 黑客松项目的 G2 独立 Part：`publisher-transaction`
（合同级发布事务：幂等注册表、`IDEMPOTENCY_CONFLICT`、真实 `audit_log_id`、
严格单事务发布、崩溃/并发证据）。请实际完成代码、测试、对抗性变异、证据和
外部审查 handoff，不要只给方案或伪代码。完成后停在外部审查点，不要继续
G3 或任何后续 Part。

## 零、开工门禁（必须先确认）

1. 本 Part 是 Design-First：先完整阅读 `.kiro/specs/publisher-transaction/`
   下 `README.md`、`design.md`、`tasks.md`，确认其中内容没有被后续讨论推翻，
   再动手。
2. 第一刀代码被明确限定为 `tasks.md` Phase 1：**两张表 + 规范化请求指纹 +
   幂等 probe（case A 重放 / case B 冲突）**。在 Phase 1 全部通过前，不得
   写 lifecycle/audit/receipt 的任何新路径。
3. 基线事实（开工时逐一验证，不得默认成立）：
   - 审计通过的功能基线是 annotated tag `g2-baseline-5bf299a` →
     `5bf299adb1e06c2f061db4086cc3bf183944ff56`。tag 不许移动、重写、
     也不许 checkout 后直接开工。
   - Phase 0 是**条件式**门禁（reviewer round-2 修正）：HEAD 必须是评审员
     批准的 publisher-spec 提交（即包含本目录四份文件的提交），且
     `c1e9274` 必须是它的祖先；G2 开发血统父提交 = `c1e9274`。禁止
     checkout `c1e9274` 开工——那会把本 spec 从工作树里丢掉。
   - 合同 `contract/planpilot_agent_contract_v1.8.json` SHA-256 前缀
     `b92e53f4…fe639`。任何 Phase 结束后它必须原样。禁止改合同。
   - 继承基线数字（重跑记录实测，禁止抄写）：unit 642、full 649、
     negctl 7 + `RESTORE-MISMATCH: none`。

## 一、仓库与权威来源

工作目录必须是 PlanPilot 实现仓库根目录，其中必须存在：

- `AGENTS.md`
- `contract/planpilot_agent_contract_v1.8.json`
- `.kiro/specs/publisher-transaction/design.md`
- `.kiro/specs/publisher-transaction/tasks.md`
- `src/planpilot/authority.py`（`publish_plan` 现状）
- `src/planpilot/persistence.py`（`Database`、`canonical`、`_append_audit_record`、
  `_audit` 返回真实 `AUD-…` 的事实）
- `src/planpilot/tools/middleware.py`（`ToolErrorMiddleware` +
  `PreparedToolCall` 分阶段协议）
- `tools/api_server.py`（`/publish` 现状：路由级 `validate_tool_payload` 输入
  校验 + `_auth("approve_publish")`；本 Part 将把输入校验唯一入口移进
  middleware，auth 留在路由——见 design.md §4/§6.2）

开始后按顺序完整阅读：

1. `AGENTS.md`
2. `.kiro/steering/contract-authority.md`
3. `.kiro/steering/guardrails.md`
4. `.kiro/steering/tech.md`
5. `.kiro/steering/product.md`
6. `.kiro/steering/structure.md`
7. `contract/planpilot_agent_contract_v1.8.json` 中的：
   `tools[publish_plan]` 的 `input_schema`/`output_schema`/`failure_schema`、
   `$defs.error_details_idempotency_conflict`、
   `$defs.error_details_policy_violation`（violated_policy 枚举含
   `approval_scope_exceeded`）、`$defs.error_details_validation_failed`、
   `tool_execution_contract.retryability_registry`（IDEMPOTENCY_CONFLICT
   非重试）、`$defs.tool_error`
8. `.kiro/specs/publisher-transaction/README.md` → `design.md` → `tasks.md`
9. `docs/devlog/DEVELOPMENT_LOG.md` 最后五条（尤其 G1.0.2 批准与 P2 遗留登记）
10. `src/planpilot/authority.py` 的 `publish_plan`/`_mutate`/`_audit` 调用链
11. `src/planpilot/store/errors.py` 的 `IdempotencyConflictError` 注释与
    details 形状（同一 code，禁止另造错误码）

## 二、必须钉死的顺序（design.md §4 原文，不得调换）

首次发布在一个 SQLite `BEGIN IMMEDIATE` 事务里完成（跨阶段状态机见
design.md §4.5；输入校验唯一入口在 middleware，见 design.md §6.2）：

1. 校验认证角色（路由层，事务外）。
2. middleware 校验 publish 输入 schema（唯一入口；路由不得先行
   `validate_tool_payload`，否则绕过合同 `tool_error` 形状）。
3. 事务内查询 idempotency key。
4. 相同 key、不同请求指纹 → `IDEMPOTENCY_CONFLICT`（Case B），零业务变更。
4a. 新 key 但已发布计划绑定了**不同** approval set → `POLICY_VIOLATION` /
   `violated_policy: "approval_scope_exceeded"`（Case D，fail-closed；
   不是幂等冲突，禁止用 `IDEMPOTENCY_CONFLICT` 冒充）。
5. 重验当前版本、digest、validator evidence。
6. 加载完整审批集并检查通过状态。
7. 转移 lifecycle 到 `PUBLISHED`。
8. 插入真实 `plan_published` audit-chain 记录，捕获 `_audit` 返回的真 id。
9. 用真实 `audit_log_id` 构建成功响应。
10. 提交前完成 publish output schema 校验。
11. 保存请求指纹 + 完整原始成功响应（receipt + registry 两张表）。
12. 一次性 commit。

输出校验失败 → 发布状态、审计记录、幂等记录一起回滚。
`tools/api_server.py:140` 的 `AUD-` 来自 `audit_chain` 主键，是有效审计 ID；
真正的缺口是 `_mutate()` 丢弃 `_audit()` 返回值 + `/publish` 无成功输出校验，
本 Part 在源头补上，不在任何地方拼装新 ID。

## 三、禁止事项

- 禁止修改 `contract/planpilot_agent_contract_v1.8.json`。
- 禁止把 publisher 实现混进 `tool-error-middleware` spec 的目录或文件。
- 禁止 `requirements.md`（Design-First 惯例）。
- 禁止移动或重写 `g2-baseline-5bf299a`；禁止在其上做 checkout -b 开工。
- 禁止引入第二套 `canonical()`、第二个 `audit_log_id` 生成函数。
- 禁止把 `clock_session` P2 trigger 顺手做进来（那是已登记的独立遗留项）。
- 禁止用 `print` 代替 logging；禁止 `os.path` 拼路径（本仓统一 `pathlib`）。
- 未获批准不得自动 commit/push 之外的 git 历史操作（不 rebase、不 amend）。

## 四、完成定义

- `tasks.md` Phase 0–4 全绿，勾选 = 代码 + 测试真实存在且跑过。
- 崩溃矩阵 design §8 全部 18 案（5 个注入点 + 并发 2 + restart 重放 +
  输出校验回滚 + 审计身份 2 + 负向零变更 + post-COMMIT 内存同步失败
  （当前调用仍 200 + 真 receipt，poison 只拦后续调用，case 13）+
  **两个真·硬杀子进程**（`os._exit`，case 14/15，禁止只拿异常回滚冒充）+
  alias 不可变 + validator evidence 负测 + observer trace 记账），
  并发矩阵、restart 重放、负向零变更矩阵，全部以原始
  stdout + `EVIDENCE.json`（hashes 按最终 HEAD blob）落盘，
  `test_evidence_integrity.py` 在主仓与 harness 双路径通过。
- 全套回归：targeted → clock/evidence → unit → full → negctl + 恢复哈希。
- devlog append-only 一条：记录实测数字、case C alias 决策与理由、case D
  fail-closed 决策。
- 停在外部审查点：bundle + `bundle verify` 自临时 clone 复验，一页报告，
  不开始 G3。
