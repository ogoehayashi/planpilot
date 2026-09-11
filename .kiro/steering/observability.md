---
inclusion: fileMatch
fileMatchPattern: ["src/planpilot/store/**", "src/planpilot/audit/**", "src/planpilot/tools/**", "src/planpilot/workflow/**"]
---

# Observability — decision traces and the audit chain

Rubric dimension 6: "logging/tracing of decisions, golden-path + adversarial
eval cases".

## Purpose

Make every Agent decision inspectable after the fact. The contract already had correlation ids and an append-only audit hash chain; this section adds the decision-trace record that ties a user intent to the tools it caused, the deterministic results it relied on, and the explanation it produced.

## Trace model

- **unit**: one decision_trace_record per tool invocation
- **writer**: The framework tool-invocation middleware writes every trace record server-side, in the same layer that mints correlation_id and appends to the audit hash chain. Traces are therefore not an LLM-authored artefact and no tool exposes them as an output: emitting one is a framework duty, not a tool capability. This is why decision_trace_record appears in no tool output_schema.
- **emission_rule**: Exactly one record per tool invocation, written before the invocation result is returned to the Agent, including failed invocations. A tool_error produces a trace record carrying error_code and retryable, so failures are traceable too.
- **identity**: correlation_id from tool_execution_contract.correlation_id_policy; retries of the same logical call reuse it
- **trace_id_rule**: trace_id is derived, never minted independently: it is correlation_id followed by ':' followed by sequence_no. Because correlation_id is a framework-assigned UUIDv4 and sequence_no is monotonic within a workflow run, the composition is globally unique without any new identifier authority. The schema enforces this shape, so a hand-written or colliding trace_id cannot validate.
- **ordering**: monotonic sequence within a workflow run
- **storage**: append-only alongside the audit log; never mutable, never rewritten
- **integrity**: each record is covered by the audit hash chain described in security_controls.audit_log_integrity
- **genesis_rule**: The first record of a chain sets audit_chain_prev_hash to 64 ASCII '0' characters. That sentinel is the only permitted non-digest value, it is distinguishable from any real SHA-256 output, and it makes the required field satisfiable at chain start. Every later record carries the previous record's event_hash. Verification therefore walks from the genesis sentinel forward and fails on the first break.
- **chain_verification**: An auditor recomputes each event_hash over the canonical record and compares it to the next record's audit_chain_prev_hash. A rewritten, reordered or deleted record breaks the chain at a detectable position.

## Record schema — `22` properties, `11` required

Required: `trace_id`, `correlation_id`, `sequence_no`, `recorded_at`, `workflow_state_before`, `tool_name`, `input_summary`, `output_summary`, `elapsed_ms`, `decision_reason_codes`, `audit_chain_prev_hash`

| field | constraint |
|---|---|
| `trace_id` | {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}:[1-9][0-9] |
| `correlation_id` | {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"} |
| `sequence_no` | {"type": "integer", "minimum": 1} |
| `recorded_at` | {"type": "string", "format": "date-time"} |
| `workflow_state_before` | {"type": "string", "minLength": 1} |
| `workflow_state_after` | {"type": ["string", "null"]} |
| `tool_name` | {"type": "string", "minLength": 1} |
| `input_summary` | {"type": "string", "maxLength": 2048} |
| `output_summary` | {"type": "string", "maxLength": 2048} |
| `state_id` | {"type": ["string", "null"]} |
| `plan_id` | {"type": ["string", "null"]} |
| `plan_version` | {"type": ["integer", "null"], "minimum": 0} |
| `plan_digest` | {"type": ["string", "null"]} |
| `elapsed_ms` | {"type": "integer", "minimum": 0} |
| `stage_budget_seconds` | {"type": ["number", "null"], "minimum": 0} |
| `decision_reason_codes` | {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 64}, "maxItems": 32, "uniqueItems": |
| `error_code` | {"type": ["string", "null"]} |
| `retryable` | {"type": ["boolean", "null"]} |
| `approval_request_id` | {"type": ["string", "null"]} |
| `approval_status_observed` | {"enum": ["PENDING", "APPROVED", "REJECTED", "EXPIRED", null]} |
| `security_event_ref` | {"type": ["string", "null"]} |
| `audit_chain_prev_hash` | {"type": "string", "pattern": "^(?:0{64}|[0-9a-f]{64})$"} |

`additionalProperties: false` — an unknown field is a schema violation.

### trace_id is derived, never minted

trace_id is derived, never minted independently: it is correlation_id followed by ':' followed by sequence_no. Because correlation_id is a framework-assigned UUIDv4 and sequence_no is monotonic within a workflow run, the composition is globally unique without any new identifier authority. The schema enforces this shape, so a hand-written or colliding trace_id cannot validate.

### The genesis sentinel

The first record of a chain sets audit_chain_prev_hash to 64 ASCII '0' characters. That sentinel is the only permitted non-digest value, it is distinguishable from any real SHA-256 output, and it makes the required field satisfiable at chain start. Every later record carries the previous record's event_hash. Verification therefore walks from the genesis sentinel forward and fails on the first break.

An auditor recomputes each event_hash over the canonical record and compares it to the next record's audit_chain_prev_hash. A rewritten, reordered or deleted record breaks the chain at a detectable position.

## What is captured

- workflow state before and after the call
- tool name, declared input summary and output summary (bounded, never full plan JSON)
- deterministic result references: plan_id, plan_version, plan_digest, state_id
- elapsed milliseconds and the stage budget the call was charged to
- decision_reason_codes drawn from the controlled vocabulary, never free text
- framework tool_error code, retryability and details when the call failed
- approval_request_id and observed status when the call touched an approval
- security events already routed to log_security_event, referenced not duplicated

## What is NEVER captured

- credentials or API keys
- full operation arrays (they stay on the non-LLM data path)
- untrusted customer note or event payload text verbatim beyond the bounded, quarantined form already defined

## Evaluation use

EVAL runtime evidence is assembled from decision traces, not from ad hoc logs. A passing EVAL case must be reconstructable from its traces alone: which state was loaded, which plans were generated, which validations ran, what KPIs resulted, and which approvals gated publication.

**Practical consequence:** when you implement an EVAL case, write it so the
pass/fail verdict can be recomputed from the trace bundle alone. If a verdict
needs a human to look at a log line, the trace is incomplete.

## Retention

- **scope**: the hackathon prototype retains traces for the evaluation window
- **rule**: traces are evidence for EVAL-001..030; they are retained with timestamped inputs and outputs per implementation_migration.evidence
- **production_note**: a retention and purge policy is production work, out of prototype scope

## Implementation notes

- The writer is framework middleware in `store/trace.py`, invoked around every
  tool call. Tools do not write their own traces and must not.
- Failed invocations are traced too, carrying `error_code` and `retryable`.
- `security_event_ref` is an opaque reference to a `log_security_event` record —
  never a copy of untrusted text. Copying untrusted payload into a trace would
  defeat the quarantine.
- Traces join the same append-only hash chain as the audit log
  (`audit_log_integrity`: append-only SHA-256 hash chain over canonical event records; the prototype exposes previous_event_hash and event_hash for verification).
