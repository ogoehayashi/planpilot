# Tasks — `tool-error-middleware`

**Design:** `./design.md`

**Contract:** `contract/planpilot_agent_contract_v1.8.json`
**Start prompt:** `./START_PROMPT.md`

Do tasks in order. A checked box means code plus tests exist and were run; it
does not mean “planned” or “partially implemented”.

## Phase 0 — baseline and contract extraction

- [ ] **0.1 Verify the starting repository**
      Require a clean worktree; record current HEAD; verify
      `approval-service-v1.0.1-hardening^{}` equals `0cf1060...` and is an
      ancestor; verify contract SHA; create branch `tool-error-middleware`.
- [x] **0.2 Run and record the inherited baseline**
      Run approval focused tests, all unit tests, full tests, closed vocabulary
      and workspace validation. Historical expected full count is 462, but the
      new implementer must record the actual output rather than copying it.
- [x] **0.3 Extract the contract registries in tests**
      Pin eight tool names, 18 error codes, retryability, details refs,
      correlation regex and all eight failure schemas directly against V1.8.

## Phase 1 — registry, correlation and serialization

- [x] **1.1 Implement the contract-derived runtime registry**
      Unknown tools/codes fail closed. Mirrored constants, if used, have exact
      drift tests against the pinned contract.
- [x] **1.2 Implement correlation context**
      Production UUIDv4 minting, injected deterministic test factory, exact
      lowercase/version/variant validation, and same-logical-call reuse.
- [x] **1.3 Implement exact JSON wire serialization**
      UTF-8, deterministic separators/order, no NaN/Infinity, immutable bytes.
- [x] **1.4 Enforce the 4096-byte failure-envelope limit**
      Replace oversize envelopes as a whole with a minimal schema-valid
      `INTERNAL_ERROR`; include CJK/emoji byte-boundary tests.

## Phase 2 — errors and trusted adapters

- [x] **2.1 Implement validated `FrameworkDomainError`**
      Accept only one of 18 codes; copy details; source retryability from the
      registry; validate details at construction.
- [x] **2.2 Implement trusted exception adapters**
      Explicitly support SchemaValidationError, StoreError and ApprovalError.
      Revalidate their copied fields before emission.
- [x] **2.3 Implement sanitized unexpected-error conversion**
      No raw exception text, traceback, path, payload, secret or untrusted text.
      Store/Approval invariant errors become `INTERNAL_ERROR`.
- [x] **2.4 Reject exception spoofing**
      An arbitrary exception with `.code/.details/.retryable` must not pass
      through as a domain error.
- [x] **2.5 Pin every failure envelope to both schemas**
      Validate code-specific details, `$defs.tool_error`, and the selected
      tool's `failure_schema`; invalid trusted errors fall back once.

## Phase 3 — transaction and execution boundary

- [x] **3.1 Implement staged/no-op transaction protocols**
      Define prepare/commit/rollback ownership; rollback is idempotent; do not
      refactor PlanStore or ApprovalService speculatively.
- [x] **3.2 Implement middleware execution order**
      known tool → correlation → input validation → prepare → deadline/output
      validation → serialization → commit → immutable outcome.
- [x] **3.3 Prove failure atomicity**
      For invalid input, handler failure, invalid output, deadline, serialization
      error and commit failure, assert business state before == after.
- [x] **3.4 Distinguish caller and server schema failures**
      Bad input is `INVALID_INPUT`; bad handler output is sanitized
      `INTERNAL_ERROR` and is never committed.
- [x] **3.5 Do not catch process-control BaseExceptions**
      Verify KeyboardInterrupt/SystemExit propagation and cleanup.

## Phase 4 — deadlines, retries and observer seam

- [x] **4.1 Implement injected monotonic deadline context**
      Correct pre/post checks and exact `DEADLINE_EXCEEDED` details, including
      required `profiles_completed` for `plan_generation`, and absent-or-empty
      handling for every other stage.
- [x] **4.2 Preserve solver exhaustion semantics**
      `SEARCH_ESCALATION_EXHAUSTED` remains non-retryable and is never converted
      into framework deadline failure.
- [x] **4.3 Implement retry-policy lookup without retries**
      Pin RATE_LIMITED, DEADLINE_EXCEEDED and INTERNAL_ERROR rules; prove the
      middleware invokes a handler at most once.
- [x] **4.4 Add the bounded completion-observer seam**
      Emit once after final outcome; do not invent decision trace or audit
      persistence. Pin behaviour when the observer fails after commit.

## Phase 5 — integration tests with completed foundations

- [x] **5.1 Round-trip existing PlanStore errors**
      INVALID_INPUT, STATE_NOT_FOUND, PLAN_VERSION_CONFLICT,
      PLAN_DIGEST_MISMATCH and IDEMPOTENCY_CONFLICT remain schema-valid and
      defensively copied.
- [x] **5.2 Round-trip existing ApprovalService errors**
      All seven current approval error shapes remain exact; invariant/conflict
      programming errors are sanitized INTERNAL_ERROR.
- [x] **5.3 Verify tool schema reuse**
      Use `validate_tool_payload`; no second schema compiler and no copied tool
      schemas in runtime code.
- [x] **5.4 Pin public-wire non-expansion**
      Internal outcome/transaction/observer fields never appear in success or
      failure payloads.

## Phase 6 — adversarial verification

- [x] **6.1 Complete the focused unit/adversarial suite**
      Cover every threat in design §16 and every minimum test in §17.
- [x] **6.2 Add disposable-copy mutation control**
      Skip validation, early commit, spoofed exception, retryability override,
      bytes-vs-chars, raw truncation, leaked exception, BaseException catch,
      hidden retry and observer rewrite mutations must all be caught.
- [x] **6.3 Verify negative-control hygiene**
      Zero escaped, zero broken anchors, sandbox restored, real repository bytes
      unchanged even if the child process is killed.
- [x] **6.4 Run repository-wide guards**
      Full pytest, closed vocabulary, its self-test, Kiro workspace validation,
      contract hash and `git diff --check`.

## Phase 7 — evidence and external review

- [x] **7.1 Generate timestamped evidence**
      Include exact commands/counts, subject hashes, reference wire bytes and
      scope statement. Generator and fact-checker hash themselves.
- [x] **7.2 Write a sceptical handoff and fact-check it**
      State what was built, what was not, decisions, known limitations and
      reviewer attack order. Fact-check must be fails=0.
- [x] **7.3 Append the development log**
      Follow `docs/devlog/README.md`; append, never rewrite history.
- [ ] **7.4 Create the clean review baseline**
      Commit only after evidence matches; create annotated tag
      `tool-error-middleware-v1-baseline`; verify it with `^{}`; never move old
      tags. Stop for independent review—do not begin publisher work.
