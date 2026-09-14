# Start prompt for the implementing Codex/Kiro task

Copy everything below the divider into a new task opened at the root of the
PlanPilot repository.

---

你现在负责 PlanPilot 黑客松项目的下一个独立 Part：
`tool-error-middleware`。请实际完成代码、测试、对抗性变异、证据和外部审查
handoff，不要只给方案或伪代码。完成后停在外部审查点，不要继续实现
publisher 或后续 Part。

## 一、仓库与权威来源

工作目录应为 PlanPilot 实现仓库根目录，其中必须存在：

- `AGENTS.md`
- `contract/planpilot_agent_contract_v1.8.json`
- `.kiro/specs/tool-error-middleware/design.md`
- `.kiro/specs/tool-error-middleware/tasks.md`
- `src/planpilot/store/`
- `src/planpilot/approval/`

开始后按顺序完整阅读：

1. `AGENTS.md`
2. `.kiro/steering/contract-authority.md`
3. `.kiro/steering/guardrails.md`
4. `.kiro/steering/tech.md`
5. `.kiro/steering/product.md`
6. `.kiro/steering/structure.md`
7. `contract/planpilot_agent_contract_v1.8.json` 中的：
   `tool_execution_contract`、`schema_distribution`、`field_ownership`、
   `security_controls`、`workflow.framework_error_semantics`、八个 `tools`、
   `$defs.tool_error` 和全部 `error_details_*`
8. `.kiro/specs/tool-error-middleware/README.md`
9. `.kiro/specs/tool-error-middleware/design.md`
10. `.kiro/specs/tool-error-middleware/tasks.md`
11. `docs/PLANPILOT_AGENT_DESIGN_ZH.md`
12. `docs/devlog/README.md` 和 `docs/devlog/DEVELOPMENT_LOG.md`
13. `src/planpilot/validation/schema.py`
14. `src/planpilot/store/errors.py`
15. `src/planpilot/approval/errors.py`

契约是唯一需求源。Design-First spec 是实施设计，不是第二份需求。禁止创建
`requirements.md`，禁止因为代码方便而修改契约，禁止发明新的 tool、参数、
error code、validation code、state、KPI、profile 或 event ID。

## 二、基线检查

任何修改前执行并记录：

```powershell
git status --short
git branch --show-current
git log -5 --oneline --decorate
git rev-parse "approval-service-v1.0.1-hardening^{}"
git merge-base --is-ancestor 0cf1060990e4668bc293744d0e44e9baf3aff20b HEAD
```

要求：

- worktree 必须干净；如果有不属于你的改动，不要删除、覆盖、reset 或 stash，
  先向用户报告；
- hardening tag 必须 peel 到
  `0cf1060990e4668bc293744d0e44e9baf3aff20b`；
- 当前 HEAD 可以包含该 tag 之后的团队文档/devlog commits，但必须以
  `0cf1060` 为祖先；
- 创建新分支 `tool-error-middleware`；
- 不移动、不删除、不 force-update 任何已有 tag。

使用仓库已存在的 Python 3.11 环境。此机器如果 `python` 指向 WindowsApps
假入口，使用 `.venv-review\Scripts\python.exe`。不要凭命令没有输出就宣称
测试通过；必须核对退出码和 pytest summary。

先重跑继承基线：

```powershell
python -m pytest tests/unit -q
python -m pytest tests -q
python tools/check_closed_vocabularies.py
python tools/check_closed_vocabularies.py --self-test
python tools/validate_kiro_workspace.py
```

历史参考值是 full suite 462 passed、approval unit 40 passed、approval
mutation 14/14；这些只是异常检测参考，必须以你亲自跑出的结果为证据。

旧的 `factcheck_impl_handoff.py` 和 `factcheck_approval_handoff.py` 分别锚定
历史 baseline tag，不应在后续 HEAD 上用旧行数/旧 subject hash 判断新 Part。
不要为了让旧 fact-check 在新分支通过而改写历史 handoff 或旧 evidence。

## 三、本 Part 的唯一目标

实现一个所有八个公共工具共用的、确定性的 tool execution middleware：

- 输入在 handler 前按对应 `input_schema` 验证；
- 成功输出在 commit/返回前按 `output_schema` 验证；
- 可信 domain exceptions 映射为公共 `$defs.tool_error`；
- 精确执行 18 个 error code 的 details schema 和 retryability；
- framework mint lowercase RFC 4122 UUIDv4 correlation ID，同一逻辑 retry 复用；
- 最终错误 JSON 的 UTF-8 bytes 必须 ≤4096；
- 未知异常必须清洗为安全、有限的 `INTERNAL_ERROR`，不得泄漏 traceback、
  路径、secret、raw payload、customer note 或 event instruction；
- 失败调用不得留下业务状态修改；
- 提供 later decision-traces 使用的 observer seam，但本 Part 不实现 trace/audit
  持久化。

完整设计和边界以 `design.md` 为准。严格按 `tasks.md` 从 Phase 0 做到
Phase 7；只有代码和测试真正完成并亲自运行后才能勾选。

## 四、不可妥协的实现语义

1. 不新增 public success envelope。内部 dataclass/protocol 不得出现在 wire
   schema。
2. 不使用 duck typing 信任异常。一个任意异常即使伪造 `.code/.details`，也只能
   变成安全 `INTERNAL_ERROR`。
3. `retryable` 由契约 registry 决定，不相信 exception 自报值。
4. caller input 不合法是 `INVALID_INPUT`；server handler output 不合法是
   `INTERNAL_ERROR`，不能把服务器 bug 归咎于用户。
5. 错误必须同时通过 code-specific details、`$defs.tool_error` 和当前工具
   `failure_schema`。
6. 4096 是完整 JSON 的 UTF-8 byte limit，不是字符数。禁止截断原始 JSON
   bytes；超限时整体替换为最小安全 `INTERNAL_ERROR`。
7. middleware 只能 catch `Exception`，不能吞掉 KeyboardInterrupt、SystemExit
   等 `BaseException`。
8. middleware 的 try/except 不能冒充事务。必须有 prepare/commit/rollback 或
   等效 staged boundary，并用状态 before/after 证明失败原子性。
9. output schema 和 deadline 检查通过前不能 commit。
10. middleware 不自动 retry handler。它只报告 contract retry metadata/policy。
11. CP-SAT + deterministic fallback 是一次 generate 调用内部的 solver ladder；
    `SEARCH_ESCALATION_EXHAUSTED` 不得被改成 `DEADLINE_EXCEEDED`。
12. 不重构已经审计的 PlanStore/ApprovalService 来迁就尚不存在的 public
    adapters；使用显式 adapter 和 synthetic transaction tests。
13. 本地开发和测试不得调用 Bedrock、网络或消耗赛事额度。

## 五、已有组件必须复用

- 工具 schema：`planpilot.validation.validate_tool_payload`
- `$defs` 校验：`planpilot.validation.validate`
- 可信 store root：`planpilot.store.StoreError`
- 可信 approval root：`planpilot.approval.ApprovalError`
- schema input root：`planpilot.validation.SchemaValidationError`

`StoreInvariantError`、`ApprovalInvariantError`、`ContractIntegrityError` 等是
内部程序/部署问题，没有可诚实套用的用户 domain error code；若逃到工具边界，
清洗为 `INTERNAL_ERROR`。不要把它们硬塞进 POLICY_VIOLATION 或
VALIDATION_FAILED。

建议 runtime package 位于 `src/planpilot/tools/`，测试和证据文件按
`design.md` §4 布局。可以合理合并极小模块，但不要让 registry、serialization、
transaction 和 middleware 的职责混成不可测试的大函数。

## 六、工作方法

- 先读代码和契约，再写实现；不要依赖 handoff 里的数字代替事实。
- 使用小步、可验证改动；每完成一层先跑 focused tests。
- 文件编辑保留用户已有变化，不使用 `git reset --hard` 或覆盖式 checkout。
- 所有 mutation 在临时仓库副本执行，永远不改真实 `src/`。
- mutation anchor 必须恰好命中一次；0 次或多次都算 broken fixture 并失败。
- 对失败路径同时断言 error shape 和 state unchanged。
- 测试 UUID 时注入 factory，不依赖随机碰巧；测试 deadline 时注入 monotonic
  clock，不 sleep。
- 不在测试中调用 LLM 或网络。
- 若发现契约本身缺少表达能力，停止该分支实现、记录证据并报告，不自行造词。

## 七、完成前的对抗检查

至少主动攻击：

- unknown/extra input field；
- handler 未被调用；
- schema-invalid success；
- commit-before-validation；
- exception spoofing；
- trusted error details 被篡改；
- exception-provided retryable 造假；
- raw secret/攻击文本泄漏；
- 4095/4096/4097 byte 边界；
- CJK/emoji 字符数与 byte 数差异；
- NaN/Infinity；
- correlation uppercase、错误 UUID version/variant、retry 不复用；
- prepare 后 deadline；
- rollback 和 commit failure；
- BaseException；
- observer failure；
- hidden double invocation/retry；
- search escalation 被误映射。

测试不能只证明对象能构造，还要验证 exact serialized bytes 能重新 parse，且完整
payload 通过对应 tool `failure_schema`。

## 八、证据、日志与 Git

完成代码后：

1. 跑 focused suite；
2. 跑 disposable-copy mutation control，并输出 caught/escaped/broken；
3. 跑 full repository suite；
4. 跑 closed vocabulary + self-test；
5. 跑 Kiro workspace validation；
6. 核对 contract SHA；
7. 跑 `git diff --check`；
8. 生成 `tests/evidence/tool-error-middleware/EVIDENCE.json` 和原始日志；
9. 写 `TOOL_ERROR_MIDDLEWARE_HANDOFF.md`；
10. 写并运行 `tools/factcheck_tool_error_handoff.py`，要求 fails=0；
11. 按 `docs/devlog/README.md` 向主日志末尾追加条目，禁止改写历史；
12. 确认证据 subject hash 与最终文件一致；
13. 提交干净 baseline；
14. 创建 annotated tag `tool-error-middleware-v1-baseline`；
15. 用 `git rev-parse "tool-error-middleware-v1-baseline^{}"` 验证，而不是比较
    annotated tag object SHA；
16. 确认工作树干净。

Evidence 和 handoff 必须明确写：这是 middleware unit/integration evidence，
不是 EVAL-001～030，不代表 publisher、workflow 或 pilot-ready。

完成后向用户汇报：

- commit 和 peeled tag；
- focused/full/mutation/guard 的实际数字；
- 实现的关键保证；
- 仍未实现的范围；
- 外部审查入口和建议攻击顺序。

然后停止，等待独立审查。不要自行开始 `audit-hash-chain`、
`decision-traces` 或 publisher。
