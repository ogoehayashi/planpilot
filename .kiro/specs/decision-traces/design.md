# Design — decision-traces

## Authority and scope

Every P0-5 middleware invocation emits one server-authored
`$defs.decision_trace_record` before its outcome returns. Tools and the LLM
cannot supply or edit traces. Trace persistence shares the audit hash chain.

## Record construction

`trace_id` is `correlation_id:sequence_no`; sequence is monotonic for a reused
correlation id. The writer receives only bounded middleware facts and explicit
workflow metadata. Summaries never contain full plans, raw factory rows,
credentials or untrusted event text. Success and failure records carry elapsed
time, stage budget, stable entity references and registered error semantics.

The complete record is validated against the pinned V1.8 definition before the
single transaction appends its chain row and typed trace index. Observer failure
cannot rewrite a committed tool result; the security service verifies that its
mandatory trace exists and marks the service unhealthy when it does not.

