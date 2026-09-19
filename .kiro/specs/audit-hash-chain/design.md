# Design — audit-hash-chain

## Authority and scope

The V1.8 contract is authoritative. This module implements one canonical
append-only SHA-256 chain shared by infrastructure audit records, security
events and decision traces. It does not implement publisher policy or invent a
second audit schema.

## Write protocol

Records use deterministic UTF-8 JSON (`sort_keys`, compact separators,
`ensure_ascii=false`, `allow_nan=false`). The genesis previous hash is 64 zeroes;
every later hash is `sha256(previous_hash + canonical_record)`. One SQLite
transaction appends the chain row and its typed index row. UPDATE and DELETE are
rejected by database triggers. Verification checks ids, links, record hashes and
the persisted chain head.

`log_security_event` is a staged P0-5 middleware handler. Its candidate output is
schema-validated before commit; commit rechecks the expected head and atomically
adds the record. Untrusted excerpts are normalized, secret/PII-redacted and
bounded before the tool input is constructed.

## Failure rules

A missing logger fails closed. A chain race, invalid schema, invalid output or
commit failure never produces a successful security reference. Raw untrusted
text and credentials never enter traces or public responses.

