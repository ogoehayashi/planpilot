# Design — `tool-error-middleware`

**Mode:** Design-First

**Contract:** `contract/planpilot_agent_contract_v1.8.json`

**Contract SHA-256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`
**Starting implementation baseline:**
`approval-service-v1.0.1-hardening` → `0cf1060990e4668bc293744d0e44e9baf3aff20b`

> Note: this file must live under the real repository path
> `.kiro/specs/tool-error-middleware/design.md`. If a tool reports a different
> parent directory, stop rather than creating a second repository.

## 1. Purpose

Implement the single framework boundary through which every PlanPilot tool call
is validated, executed and serialized. The middleware makes the contract's
failure transport executable:

- validate each public input before the handler runs;
- validate each successful output before it can be returned or committed;
- convert trusted domain failures into the common `$defs.tool_error` shape;
- use the exact per-code `details` schema and retryability registry;
- mint and preserve an RFC 4122 UUIDv4 correlation ID;
- enforce the whole-envelope 4096-byte UTF-8 limit;
- sanitize unexpected implementation failures into a bounded
  `INTERNAL_ERROR` without traceback, secrets or untrusted text;
- provide a transaction/commit boundary so a failed call leaves no business
  mutation;
- expose an internal completion event for the later decision-trace part without
  implementing trace persistence here.

The middleware is deterministic Python framework code. It makes no LLM,
Bedrock, network, workbook or dataset call.

## 2. Contract authority and exact scope

Authoritative contract sections:

- `tool_execution_contract`;
- `schema_distribution`;
- `field_ownership`;
- `security_controls`;
- `workflow.framework_error_semantics`;
- `tools[*].input_schema`, `output_schema`, `failure_schema`;
- `$defs.tool_error`;
- all 18 registered `error_details_*` definitions;
- `$defs.decision_trace_record` only as a future integration boundary;
- `scheduling_engine.time_budget_policy` and
  `tool_execution_contract.generation_escalation_policy` only for distinguishing
  framework deadlines from the internal solver ladder.

The contract wins over this design. Do not edit the contract to make an
implementation easier and do not create a `requirements.md`.

### In scope

- a reusable execution boundary for all eight fixed tool names;
- contract-derived tool schema lookup;
- trusted exception adapters and a validated generic domain-failure type;
- success/failure result discrimination inside Python;
- canonical JSON serialization of the result that will be sent on the wire;
- correlation ID creation/reuse;
- retryability metadata and retry-policy lookup, but not an automatic retry
  loop;
- stage deadline representation with an injected monotonic clock;
- a transaction protocol that delays commit until success payload validation
  and deadline checks pass;
- no-op transaction support for reads;
- tests using synthetic handlers plus existing PlanStore and ApprovalService
  error classes;
- mutation controls, timestamped evidence and external-review handoff.

### Explicitly out of scope

- implementations of `load_factory_state`, `validate_factory_state`,
  `generate_plan_options`, `validate_plan`, `request_approval`,
  `check_approval_status`, `publish_plan`, or `log_security_event`;
- publish idempotency and the publisher transaction;
- audit hash-chain persistence;
- final `decision_trace_record` persistence;
- orchestration of the eleven workflow states;
- retries against Bedrock or any external service;
- solver escalation or fallback implementation;
- authentication, UI, dataset access and EVAL execution;
- changing the known `change_promised_due_date` validator-provenance boundary.

## 3. Existing foundations to reuse

Do not duplicate these components:

- `planpilot.validation.validate_tool_payload(...)` compiles all tool schemas
  with the root `$defs` bundled;
- `planpilot.validation.validate(...)` validates a `$defs` payload;
- `planpilot.validation.SchemaValidationError` already carries
  `code=INVALID_INPUT` and schema-valid details;
- `planpilot.store.StoreError` subclasses carry trusted registered
  `code/message/details/retryable` values;
- `planpilot.approval.ApprovalError` subclasses carry trusted registered
  `code/message/details/retryable` values;
- PlanStore and ApprovalService already implement their local atomicity and
  defensive-copy rules.

Invariant/programming errors such as `StoreInvariantError`,
`ApprovalInvariantError`, and `ContractIntegrityError` do not carry a truthful
user-facing domain code. If they escape a public handler, they are server faults
and become sanitized `INTERNAL_ERROR`; never force them into
`VALIDATION_FAILED`, `POLICY_VIOLATION`, or another unrelated code.

## 4. Proposed package structure

```text
src/planpilot/tools/
├── __init__.py
├── middleware.py        # execution order and ToolExecutionOutcome
├── errors.py            # FrameworkDomainError + safe INTERNAL_ERROR factory
├── registry.py          # 8 tools, 18 codes, detail refs, retry policies
├── correlation.py       # UUIDv4 factory and validation
├── transaction.py       # staged/no-op transaction protocol
└── serialization.py     # exact JSON bytes and 4096-byte enforcement

tests/unit/
└── test_tool_error_middleware.py

tests/negative_control/
└── test_tool_error_middleware_negctl.py

tools/
├── write_evidence_tool_error_middleware.py
└── factcheck_tool_error_handoff.py
```

Names may be consolidated when a file would otherwise be trivial, but the
responsibilities and one-way dependencies must remain visible. Modules under
`tools/` in the repository root are development/evidence scripts; runtime code
belongs under `src/planpilot/tools/`.

## 5. Exact tool and error registries

The only public tool names are, in contract order:

1. `load_factory_state`
2. `validate_factory_state`
3. `generate_plan_options`
4. `validate_plan`
5. `request_approval`
6. `check_approval_status`
7. `publish_plan`
8. `log_security_event`

Every one has `failure_schema = {"$ref":"#/$defs/tool_error"}`. Production
startup later must require all eight handlers; this part may exercise a partial
registry with synthetic handlers, but it must reject an unknown tool name.

The error registry is derived from the contract and pinned by drift tests:

| Error code | Retryable | Details definition |
|---|---:|---|
| `INVALID_INPUT` | false | `error_details_invalid_input` |
| `STATE_NOT_FOUND` | false | `error_details_state_not_found` |
| `VALIDATION_FAILED` | false | `error_details_validation_failed` |
| `NO_FEASIBLE_PLAN` | false | `error_details_no_feasible_plan` |
| `DEADLINE_EXCEEDED` | true | `error_details_deadline_exceeded` |
| `SEARCH_ESCALATION_EXHAUSTED` | false | `error_details_search_escalation_exhausted` |
| `RATE_LIMITED` | true | `error_details_rate_limited` |
| `APPROVAL_REQUIRED` | false | `error_details_approval_required` |
| `APPROVAL_REJECTED` | false | `error_details_approval_rejected` |
| `APPROVAL_EXPIRED` | false | `error_details_approval_expired` |
| `APPROVAL_WINDOW_CLOSED` | false | `error_details_approval_window_closed` |
| `APPROVAL_SET_INVALIDATED` | false | `error_details_approval_set_invalidated` |
| `APPROVAL_SET_INCOMPLETE` | false | `error_details_approval_set_incomplete` |
| `PLAN_VERSION_CONFLICT` | false | `error_details_plan_version_conflict` |
| `PLAN_DIGEST_MISMATCH` | false | `error_details_plan_digest_mismatch` |
| `IDEMPOTENCY_CONFLICT` | false | `error_details_idempotency_conflict` |
| `POLICY_VIOLATION` | false | `error_details_policy_violation` |
| `INTERNAL_ERROR` | true | `error_details_internal_error` |

Do not maintain an untested handwritten registry. Either derive it from the
loaded pinned contract or mirror it for runtime speed and assert exact equality
to the contract, following the current store/approval policy pattern.

## 6. Internal execution types — not new wire schemas

The contract defines success payloads and failure payloads, but not a success
envelope. Do not add fields to the public wire format. An internal immutable
result type may discriminate the two paths:

```python
@dataclass(frozen=True)
class ToolExecutionOutcome:
    tool_name: str
    correlation_id: str
    is_error: bool
    payload: Mapping[str, object]
    wire_bytes: bytes
```

`payload` is a defensive immutable/copy view. `wire_bytes` is the exact UTF-8
JSON to send to the caller. Keeping the measured bytes prevents a later gateway
from reserializing the dict with different escaping and accidentally breaking
the 4096-byte guarantee.

This class is internal implementation structure. It must never appear in the
tool input/output schemas.

## 7. Execution order

The order is security-critical:

```text
1. Resolve a known tool registration.
2. Create or validate the framework-owned correlation_id.
3. Validate input against that tool's input_schema.
4. Start/inherit the declared stage deadline using an injected monotonic clock.
5. Ask the handler/transaction to prepare, but do not commit business state.
6. If a trusted domain failure occurs, build and validate tool_error.
7. If an unexpected Exception occurs, build a sanitized INTERNAL_ERROR.
8. For a candidate success, enforce the deadline and validate output_schema.
9. Serialize the exact success bytes.
10. Commit the prepared business mutation atomically.
11. Return success bytes, or a validated bounded error if commit fails.
12. Emit one internal completion notification for later trace persistence.
```

`KeyboardInterrupt`, `SystemExit`, `GeneratorExit` and other `BaseException`
conditions are not converted into user-facing tool errors. Catch `Exception`,
not `BaseException`; cleanup remains the transaction/context manager's duty.

### Important atomicity limitation

A middleware `try/except` cannot undo a handler that already wrote state. The
handler protocol must stage work or execute inside a rollback-capable unit of
work. Tests that only assert “an error dict was returned” without checking state
before/after are insufficient.

For this part, implement and prove the protocol with synthetic state. Real tool
adapters later must wrap PlanStore/ApprovalService operations at their existing
atomic service boundaries. Do not refactor those reviewed modules speculatively.

## 8. Input and output validation semantics

### Invalid input

If public input fails its tool `input_schema`:

- the handler is not called;
- the transaction is not opened or committed;
- return `INVALID_INPUT` with details from `SchemaValidationError`;
- use the framework correlation ID;
- validate the complete failure against both the registered detail definition
  and the tool's `failure_schema`.

### Invalid server output

If a handler returns a payload that fails its `output_schema`, that is not the
user's invalid input. It is an implementation fault:

- do not commit;
- do not leak validator internals;
- return sanitized `INTERNAL_ERROR`;
- `diagnostic_class` may name a safe implementation class such as
  `ToolOutputValidationFailure`;
- `safe_detail` must not contain the invalid raw payload.

### Tool exceptions

Only explicitly registered trusted roots may be passed through:

- `SchemaValidationError`;
- `StoreError`;
- `ApprovalError`;
- the middleware's validated `FrameworkDomainError` for future modules.

An arbitrary exception object that merely defines `.code`, `.details` or
`.retryable` is untrusted and becomes `INTERNAL_ERROR`. This blocks exception
spoofing by a handler or dependency.

Before pass-through, rebuild/copy the fields and revalidate them. Never return
the exception object's mutable details dict directly.

## 9. Correlation ID

Contract shape:

```text
^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$
```

Rules:

- the framework, not the Agent or workbook, mints the ID;
- production uses lowercase `uuid.uuid4()`;
- a new logical call receives a new ID;
- retry of the same logical call receives the existing internal execution
  context and therefore reuses the ID;
- the ID is not added to public tool inputs;
- tests inject a deterministic factory; do not monkeypatch global randomness;
- an invalid internal context is a programming/deployment fault and is replaced
  with a newly minted valid ID for the safe `INTERNAL_ERROR` response; the
  handler must not execute.

The later decision-trace and audit layers must store exactly this same value.

## 10. Error construction and sanitization

Every emitted error has exactly:

```json
{
  "error_code": "...",
  "message": "1..500 characters",
  "retryable": false,
  "correlation_id": "lowercase UUIDv4",
  "details": {}
}
```

Rules:

- `retryable` always comes from the contract registry, never the exception;
- `details` is validated against the code-specific definition;
- the entire object is validated against `$defs.tool_error` and the selected
  tool's `failure_schema`;
- trusted domain messages are copied only after length and safety checks;
- unknown exceptions never use raw `str(exc)`, `repr(exc)`, traceback, local
  path, SQL, payload, API key or customer/event text;
- unknown failures use `INTERNAL_ERROR` with bounded `diagnostic_class` and a
  generic `safe_detail`;
- an invalid trusted error is itself an implementation failure and is replaced,
  not recursively wrapped forever.

The fallback `INTERNAL_ERROR` constructor must be small, deterministic,
schema-valid, and independently tested. If the first error fails schema,
serialization or size checks, build the fallback once; if that impossible
constant fails, raise a framework invariant exception rather than emit invalid
JSON.

## 11. UTF-8 wire-size enforcement

The limit applies to the fully serialized error envelope, not to Python
characters or one field:

```python
wire = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
```

If `len(wire) > 4096`, replace the whole envelope with bounded
`INTERNAL_ERROR`; do not truncate arbitrary JSON bytes, because that can split a
UTF-8 sequence or produce invalid JSON. Tests must include multi-byte CJK and
emoji content where character count is far below byte count.

The 4096-byte rule is for failures. Success payload bounds are enforced by each
output schema and the non-LLM data-path design; do not invent a global success
limit in this part.

## 12. Deadline semantics

Use an injected monotonic clock for measurements. Wall-clock timestamps are not
used for elapsed duration. A `DEADLINE_EXCEEDED` error carries:

- stage;
- elapsed seconds;
- budget seconds;
- `profiles_completed` is required when stage is `plan_generation`; for every
  other stage it is absent or an empty array, exactly as the contract describes.

Critical distinction:

- framework stage overrun → retryable `DEADLINE_EXCEEDED`;
- CP-SAT/fallback shared generation ladder exhausted → non-retryable
  `SEARCH_ESCALATION_EXHAUSTED` authored by the future engine.

Middleware must not convert an engine `SEARCH_ESCALATION_EXHAUSTED` into
`DEADLINE_EXCEEDED` and must not start a fresh 40-second budget on retry.

A post-handler deadline failure can remain atomic only when the handler has not
committed yet. This is why deadline checking occurs before transaction commit.

## 13. Retry policy

This part reports and exposes policy; it does not retry handlers automatically.

- `RATE_LIMITED`: retry the same read only after `retry_after_seconds` and only
  if the original caller deadline has enough time;
- `DEADLINE_EXCEEDED`: preserve workflow state; do not repeat inside the same
  expired deadline; a later request requires changed budget/configuration;
- `INTERNAL_ERROR`: at most one retry after the named dependency passes a
  health check;
- every other error: non-retryable.

Automatic retries belong to the future workflow/orchestration integration. A
hidden retry in middleware could duplicate mutations and reset the SLA clock.

## 14. Transaction protocol

Define the smallest protocol that makes failure atomicity testable. One viable
shape is:

```python
class PreparedToolCall(Protocol):
    def prepare(self, payload, context) -> Mapping[str, object]: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

Required semantics:

- `prepare` may compute and stage but cannot expose committed business state;
- middleware validates/deadline-checks the prepared output before `commit`;
- a failure before commit invokes idempotent rollback;
- commit is atomic in the backing service; a commit failure must leave the
  prior state observable;
- rollback failure is never appended to user details; it becomes internal
  telemetry and a safe `INTERNAL_ERROR`;
- read-only tools use an explicit no-op implementation;
- success is returned only after commit succeeds;
- failure responses are never passed to commit.

If implementation discovers that this protocol cannot truthfully wrap a future
tool, record the mismatch as a design finding. Do not weaken failure atomicity
or claim a generic `try/except` provides rollback.

## 15. Completion observer and provenance boundary

The middleware is the only component allowed to convert a handler outcome into
wire bytes. Callers cannot submit a prebuilt “successful tool result” for the
middleware to bless. Success comes only from a registered handler invocation,
then passes output validation.

After the final outcome is fixed, notify an injected observer with bounded
internal facts:

- tool name and correlation ID;
- success/error discriminator and error code when applicable;
- elapsed duration and deadline metadata;
- payload byte length;
- commit status;
- stable entity references supplied by the handler context.

The observer interface is a seam for `decision-traces`; it must not implement or
invent the final trace schema, audit hashes or persistence in this part. Observer
failure must not turn a successfully committed business action into a reported
failure. It should fail closed at deployment health/startup or write a separate
internal diagnostic according to the later trace design. Pin this unresolved
integration choice in the external handoff rather than guessing.

The repository devlog mentions a gateway/tool-result failure mode. Treat it as
an observed engineering risk, not a new public requirement. The contract-backed
controls here are registered-handler execution, schema validation, correlation,
digest verification in stateful tools, and later trace/audit linkage. Do not add
an undocumented signature or field to public tool results.

## 16. Security and mutation threats to test

| Attack or defect | Required result |
|---|---|
| Unknown input field | `INVALID_INPUT`; handler and commit untouched |
| Handler returns unknown success field | `INTERNAL_ERROR`; no commit |
| Exception spoofs valid `.code/details` | sanitized `INTERNAL_ERROR` |
| Trusted exception carries wrong details shape | fallback `INTERNAL_ERROR` |
| Exception message contains a secret or raw attack text | no secret/text in wire bytes |
| Error `retryable` disagrees with registry | registry wins |
| UUID is uppercase, wrong version or caller-authored | handler does not run; valid internal failure correlation |
| Envelope is 4097 bytes | bounded `INTERNAL_ERROR` ≤4096 bytes |
| CJK/emoji crosses byte limit | measured on UTF-8 bytes, not characters |
| NaN/Infinity in details | no invalid JSON; bounded `INTERNAL_ERROR` |
| Deadline expires after prepare | rollback; `DEADLINE_EXCEEDED`; no commit |
| Solver ladder returns final exhaustion | preserve `SEARCH_ESCALATION_EXHAUSTED` |
| Handler raises `KeyboardInterrupt` | propagate; cleanup, no fabricated tool error |
| Caller mutates returned payload/details | middleware or exception internal state unchanged |
| Observer fails after a committed success | no false report that business action failed |

## 17. Test and evidence strategy

### Focused unit tests

At minimum pin:

1. exact eight tool names and exact 18-code registry;
2. every code's retryability and details ref against the contract;
3. all eight failure schemas resolve to `$defs.tool_error`;
4. valid success for each tool schema through synthetic fixtures where feasible;
5. input validation happens before handler invocation;
6. output validation happens before commit;
7. trusted StoreError, ApprovalError and SchemaValidationError round-trip;
8. invariant and unknown exceptions are sanitized;
9. exception spoofing is rejected;
10. correlation mint/reuse/shape;
11. exact UTF-8 wire bytes and size boundary;
12. failure envelope self-validation;
13. transaction rollback and commit ordering;
14. pre/post deadline behaviour;
15. retry policy has no hidden retry loop;
16. observer sees the final immutable outcome once;
17. `BaseException` is not converted;
18. defensive-copy/immutability properties.

### Negative control

Run mutations only in a disposable repository copy. Include mutations that:

- skip input validation;
- skip output validation;
- commit before output validation;
- trust any object with `.code`;
- use exception-provided retryability;
- omit details validation;
- count characters instead of UTF-8 bytes;
- truncate raw bytes at 4096;
- reuse/mint correlation incorrectly;
- expose `str(unknown_exception)`;
- catch `BaseException`;
- convert search exhaustion to deadline exceeded;
- automatically retry a mutating handler;
- let observer failure rewrite committed success.

A mutation counts as caught only when the real focused suite fails. Missing
anchors are broken fixtures and fail the control. Real sources are never a
mutation target; use the existing temporary-copy pattern.

### Evidence

Create `tests/evidence/tool-error-middleware/` with:

- `EVIDENCE.json` containing timestamp, contract hash, subject hashes, Python
  version, commands, exit codes and `all_green`;
- focused unit log;
- mutation log with caught/escaped/broken counts;
- full-suite log;
- vocabulary and workspace-validation logs;
- reference serialized errors, including exact byte lengths for ASCII and
  multi-byte boundary cases;
- a scope statement that EVAL-001..030 remain pending.

The evidence generator and fact-checker must hash themselves, as the approval
part does. Do not edit generated evidence by hand.

## 18. Acceptance gates

The part is ready for external review only when all are true:

- the contract hash is unchanged;
- every `tasks.md` item except the final external-review verdict is checked;
- focused tests are green;
- all middleware mutations are caught with zero escapes and zero broken anchors;
- the full existing repository suite remains green;
- closed vocabulary and workspace validation pass;
- `git diff --check` passes;
- no Bedrock/network call occurred;
- timestamped evidence matches current subject hashes;
- a fact-check reports zero failures;
- a handoff describes implementation, findings, limitations and exact counts;
- the worktree is clean;
- an annotated `tool-error-middleware-v1-baseline` tag peels to the final commit;
- existing tags are unchanged;
- `docs/devlog/DEVELOPMENT_LOG.md` has the corresponding append-only entry.

Passing these gates approves only this middleware part. It does not establish
publisher, workflow, end-to-end or pilot readiness.

## 19. Decisions that must remain explicit

1. **No success envelope is invented.** Internal outcome types do not change
   public payload schemas.
2. **No duck-typed trust.** A random exception carrying a valid-looking code is
   not a trusted domain error.
3. **Output invalidity is INTERNAL_ERROR.** It is not blamed on caller input.
4. **Registry owns retryability.** Exceptions cannot override it.
5. **No automatic retry.** Retry execution belongs to later orchestration.
6. **Wire bytes are authoritative.** Size is measured after exact UTF-8
   serialization.
7. **No arbitrary truncation.** Oversize or unserializable error is replaced as
   a whole.
8. **Commit comes last.** A caught exception after an eager mutation is not
   failure atomicity.
9. **Invariant errors remain internal.** Do not fabricate a domain code.
10. **Trace/audit remain separate parts.** Provide a seam; do not pre-implement
    or fake their guarantees.
