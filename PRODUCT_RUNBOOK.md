# PlanPilot production runbook

## Local run

最简单的本地启动方式是运行：

```powershell
.\tools\start_local.ps1
```

脚本会提示输入数字密钥，自动设置环境变量、签发 planner Token、复制
Token 到剪贴板并启动 API。打开网页后按 `Ctrl+V` 填入“访问令牌”字段即可。
Manager Token 使用：

```powershell
.\tools\start_local.ps1 -Role manager -User manager-demo
```

The one-command local launcher prompts for a numeric secret, issues a signed
planner token, copies it to the clipboard, and starts the API. The secret must
contain at least 32 digits. Use `-Role manager` when testing manager approvals.

```powershell
$env:PYTHONPATH="src"
python tools/api_server.py
```

Open `http://localhost:8080/`. To plan against a factory file, post JSON with
`factory_file`. The default `data/factory_demo_v18.json` explicitly declares
products and BOM rows, max lot sizes, inventory source buckets, routing operation
types, worker skills, stable calendar windows, changeovers and KPI assumptions.
Missing authority data is rejected rather than replaced with defaults.

Excel workbooks require `pip install -r requirements-import.txt`. The production
path accepts the contract's 17-sheet V1.8 workbook, normalizes timestamps to
Asia/Singapore and quantities to integer base units, and stores an immutable
SQLite factory state. Record defects are reported and quarantined transitively;
structural corruption is `INVALID`. The old three-sheet compact workbook remains
available only through the legacy adapter for compatibility tests.

Set `PLANPILOT_AUTH_SECRET` before starting the API. Approval and publish calls
must send `Authorization: Bearer <token>`; tokens are signed with the same
secret and carry planner or manager permissions.

The web UI now reads authoritative plans by ID/version, binds actions to the
selected candidate's digest, shows every server-derived approval requirement,
and offers role-gated Approve / Reject controls. Planner and Manager tokens are
separate inputs. `GET /approval/status` refreshes expiry and invalidation;
`GET /audit/status` verifies the append-only chain and exposes the head hash and
recent event IDs. A regenerated version invalidates its previous approval set;
an old version or digest cannot be published. The UI never decides approval
policy itself.

## Bedrock configuration

Set `PLANPILOT_BEDROCK_REGION=ap-southeast-1` and
`PLANPILOT_BEDROCK_MODEL=global.anthropic.claude-sonnet-4-5-20250929-v1:0`,
then provide the per-team Bedrock API key through `PLANPILOT_BEDROCK_KEY_FILE`
or `PLANPILOT_BEDROCK_API_KEY`. `BedrockClient` rejects any different model
binding and uses `temperature=0`; tool execution remains allowlisted and
deterministic. Keep `PLANPILOT_FORBID_LLM_NETWORK=1` for local development and
tests, and set it to `0` only for an authorized deployment or demo invocation.

## Persistence and operations

`planpilot.persistence.Database` owns SQLite WAL transactions, the audit chain
and inference metering only. `planpilot.authority.RuntimeAuthority` persists
canonical snapshots of the audited `PlanStore` and `ApprovalService` in one
optimistic SQLite transaction. The former parallel `plans`, `revisions`,
`bound_approvals` and `publications` implementation is no longer created or
called by the runtime.

`POST /schedule` expands fixed max-size lots, runs the deterministic solver,
builds contract-valid V1.8 `plan_content`, then invokes a separately implemented
validator. The validator reloads factory state, recomputes digest, reservations,
HC-001..HC-013 and KPIs, validates the `validate_plan` output, and only then calls
`install_validated_plan`. Digest, KPI, schema or hard-constraint mismatch fails
before an authority write. Workbook and normalized JSON inputs both enter
planning through a stored `state_id`; generation resolves that immutable
reference instead of accepting a mutable workbook payload.

Store the database on durable block storage and use the SQLite online backup.
Expose it behind TLS and an authenticated reverse proxy before any network use.

## Evaluation gate (P0-1 correction)

Run `python tools/run_evals.py` for the formal readiness inventory. Currently
all 30 cases are BLOCKED (0 PASS / 0 FAIL), and exit code 1 is expected.
The runner does not execute formal scenarios yet. A workbook, unit-test success
or an old PASS file cannot unlock it. Each case needs a specific executor,
complete pass-condition verification, inputs/outputs, reconstructable traces,
canonical digest (or verified no-plan outcome), measured timings, approval
records (or verified non-applicability) and a verified audit chain.

Run `python tools/run_smoke_harness.py` for compact demo/utility assertions.
Its independent `tests/evidence/compact-smoke/SMOKE_EVIDENCE.json` is not formal
acceptance evidence; exit code 0 means only the named smoke checks passed.
Do not use Python `-O` for assertion-based checks.

The old green evidence is withdrawn and wrapped as historical-only data in
`tests/evidence/retracted/`. See `EVAL_EVIDENCE_RETRACTION_REPORT_20260916.md`.

Create a consistent SQLite backup with:

```powershell
python tools/backup_database.py planpilot.db backups/planpilot.db
```

`GET /health` is a liveness check and `GET /metrics` returns in-process request
counts and accumulated schedule latency. Export these values to the deployment
monitoring system rather than relying on process memory for long-term metrics.
