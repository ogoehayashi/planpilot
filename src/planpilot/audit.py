"""Contract-valid security events and decision traces on one append-only chain."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from copy import deepcopy
from typing import Any, Mapping

from .persistence import Database, canonical
from .tools import ToolErrorMiddleware
from .validation import validate, validate_tool_payload


GENESIS = "0" * 64
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|secret|session[_ -]?token)\s*[:=]\s*\S+"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{80,}\b"),
    re.compile(r"\b\d{12}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


def redact_untrusted_text(value: object) -> tuple[str, bool]:
    """Normalize, redact and byte-safely bound an untrusted excerpt."""
    text = " ".join(str(value).replace("\x00", "").split())
    changed = text != str(value)
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn("[redacted]", text)
        changed = changed or bool(count)
    text = text[:500]
    return text or "[empty untrusted text]", changed


class AuditChainError(RuntimeError):
    """G4 4.1 (design §5 V1): chain/head verification failure raised by
    verify_audit_connection. `kind` is a closed vocabulary:
    missing_table | missing_head | chain_break | head_mismatch."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(f"{kind}: {detail}" if detail else kind)


def verify_audit_connection(conn) -> dict:
    """G4 4.1 (design §5 V1) — the read-only audit verifier over an
    ALREADY-OPEN connection. Deliberately NOT a method of Database and
    NOT AuditTrail: the verifier must not repair what it verifies, so
    this function contains ZERO schema changes and ZERO writes (an AST
    guard test rejects any forbidden write-statement word here, docstring
    included — keep this prose free of those words or the guard fires).

    F5 verbatim rule (mirrors Database._append_audit_record,
    persistence.py:310): the hash is sha256(previous + record) over the
    STORED record STRING exactly as read — it is NEVER re-canonicalized
    from parsed JSON. A whitespace-only mutation of a stored record is
    therefore a chain_break here, whereas a canonicalizing verifier
    would falsely bless it.

    Raises AuditChainError(kind=...) on the first violation; returns
    {"entry_count": int, "event_hash": str} of the verified tip.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "audit_chain" not in tables:
        raise AuditChainError("missing_table", "audit_chain")
    if "audit_chain_head" not in tables:
        raise AuditChainError("missing_table", "audit_chain_head")

    previous = GENESIS
    count = 0
    for row in conn.execute(
            "SELECT id, record, previous_hash, event_hash "
            "FROM audit_chain ORDER BY id"):
        row_id, record, previous_hash, event_hash = row
        count += 1
        expected = hashlib.sha256((previous + record).encode("utf-8")).hexdigest()
        if (row_id != count or previous_hash != previous
                or event_hash != expected):
            raise AuditChainError(
                "chain_break",
                f"at entry id={row_id} (position {count}); stored bytes "
                "were not re-canonicalized before hashing (F5)")
        previous = event_hash

    head = conn.execute(
        "SELECT entry_count, event_hash FROM audit_chain_head "
        "WHERE singleton=1").fetchone()
    if head is None:
        raise AuditChainError("missing_head", "no singleton=1 row")
    if head[0] != count or head[1] != previous:
        raise AuditChainError(
            "head_mismatch",
            f"head says entry_count={head[0]} event_hash={head[1]!r:.16}… "
            f"but walk found entry_count={count} tip={previous!r:.16}…")
    return {"entry_count": count, "event_hash": previous}


class AuditTrail:
    """Append-only typed records sharing the infrastructure audit chain."""

    def __init__(self, database: Database):
        self.database = database
        self._sequence_lock = threading.RLock()
        with database.transaction():
            database.conn.executescript("""
                CREATE TABLE IF NOT EXISTS security_events (
                    security_event_id TEXT PRIMARY KEY,
                    audit_log_id TEXT NOT NULL UNIQUE,
                    record TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS decision_traces (
                    trace_id TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    record TEXT NOT NULL,
                    audit_log_id TEXT NOT NULL UNIQUE,
                    event_hash TEXT NOT NULL UNIQUE,
                    UNIQUE(correlation_id,sequence_no)
                );
                CREATE TRIGGER IF NOT EXISTS security_events_no_update
                BEFORE UPDATE ON security_events BEGIN
                    SELECT RAISE(ABORT, 'security_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS security_events_no_delete
                BEFORE DELETE ON security_events BEGIN
                    SELECT RAISE(ABORT, 'security_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS decision_traces_no_update
                BEFORE UPDATE ON decision_traces BEGIN
                    SELECT RAISE(ABORT, 'decision_traces is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS decision_traces_no_delete
                BEFORE DELETE ON decision_traces BEGIN
                    SELECT RAISE(ABORT, 'decision_traces is append-only');
                END;
            """)

    def head(self) -> str:
        row = self.database.conn.execute(
            "SELECT event_hash FROM audit_chain ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS

    def append_security(
        self, security_event_id: str, record: dict, *, expected_head: str,
        expected_audit_log_id: str, expected_event_hash: str,
    ) -> dict:
        with self.database.transaction():
            if self.head() != expected_head:
                raise RuntimeError("audit chain advanced before security event commit")
            appended = self.database._append_audit_record(record)
            if (appended["audit_log_id"] != expected_audit_log_id
                    or appended["event_hash"] != expected_event_hash):
                raise RuntimeError("prepared security event identity changed at commit")
            self.database.conn.execute(
                "INSERT INTO security_events VALUES(?,?,?,?)",
                (security_event_id, appended["audit_log_id"], appended["record"], appended["event_hash"]),
            )
        return appended

    def append_trace(self, record: dict) -> dict:
        validate(record, "decision_trace_record", "decision_trace_record", record.get("trace_id"))
        with self.database.transaction():
            if record["audit_chain_prev_hash"] != self.head():
                raise RuntimeError("audit chain advanced before trace commit")
            appended = self.database._append_audit_record(record)
            self.database.conn.execute(
                "INSERT INTO decision_traces VALUES(?,?,?,?,?,?)",
                (record["trace_id"], record["correlation_id"], record["sequence_no"],
                 appended["record"], appended["audit_log_id"], appended["event_hash"]),
            )
        return appended

    def next_sequence(self, correlation_id: str) -> int:
        with self.database.lock:
            row = self.database.conn.execute(
                "SELECT COALESCE(MAX(sequence_no),0)+1 FROM decision_traces WHERE correlation_id=?",
                (correlation_id,),
            ).fetchone()
        return int(row[0])

    def security_event(self, security_event_id: str) -> dict:
        with self.database.lock:
            row = self.database.conn.execute(
                "SELECT record FROM security_events WHERE security_event_id=?", (security_event_id,)
            ).fetchone()
        if row is None:
            raise KeyError("security event not found")
        return json.loads(row[0])


class DecisionTraceWriter:
    """P0-5 observer that writes exactly one schema-valid record per call."""

    def __init__(self, trail: AuditTrail):
        self.trail = trail

    @staticmethod
    def _text(value: object, fallback: str) -> str:
        if not isinstance(value, str) or not value:
            return fallback
        return value[:2048]

    def __call__(self, event: Mapping[str, Any]) -> None:
        metadata = dict(event.get("trace_metadata") or {})
        refs = dict(event.get("entity_references") or {})
        correlation_id = event["correlation_id"]
        with self.trail._sequence_lock:
            sequence = self.trail.next_sequence(correlation_id)
            error_code = event.get("error_code")
            record = {
                "trace_id": f"{correlation_id}:{sequence}",
                "correlation_id": correlation_id,
                "sequence_no": sequence,
                "recorded_at": self.trail.database.clock.now(),
                "workflow_state_before": self._text(metadata.get("workflow_state_before"), "UNKNOWN"),
                "workflow_state_after": metadata.get("workflow_state_after"),
                "tool_name": event["tool_name"],
                "input_summary": self._text(metadata.get("input_summary"), "validated contract input"),
                "output_summary": self._text(
                    metadata.get("output_summary"),
                    f"{'error '+str(error_code) if event['is_error'] else 'validated success'}; bytes={event['payload_bytes']}",
                ),
                "state_id": refs.get("state_id"),
                "plan_id": refs.get("plan_id"),
                "plan_version": int(refs["plan_version"]) if refs.get("plan_version") is not None else None,
                "plan_digest": refs.get("plan_digest"),
                "elapsed_ms": max(0, round(float(event["elapsed_seconds"]) * 1000)),
                "stage_budget_seconds": (event.get("deadline") or {}).get("budget_seconds"),
                "decision_reason_codes": list(metadata.get("decision_reason_codes") or []),
                "error_code": error_code,
                "retryable": event.get("retryable"),
                "approval_request_id": refs.get("approval_request_id"),
                "approval_status_observed": metadata.get("approval_status_observed"),
                "security_event_ref": refs.get("security_event_ref"),
                "audit_chain_prev_hash": self.trail.head(),
            }
            self.trail.append_trace(record)


class _PreparedSecurityEvent:
    def __init__(self, trail: AuditTrail, security_event_id: str):
        self.trail = trail
        self.security_event_id = security_event_id
        self.payload = None
        self.output = None
        self.record = None
        self.expected_head = None
        self.committed = False

    def prepare(self, payload, context):
        self.payload = deepcopy(dict(payload))
        self.expected_head = self.trail.head()
        logged_at = self.trail.database.clock.now()
        with self.trail.database.lock:
            next_id = self.trail.database.conn.execute(
                "SELECT COALESCE(MAX(id),0)+1 FROM audit_chain"
            ).fetchone()[0]
        audit_log_id = f"AUD-{next_id:012d}"
        self.record = {
            "record_type": "security_event",
            "security_event_id": self.security_event_id,
            "correlation_id": context.correlation_id,
            "logged_at": logged_at,
            "event": self.payload,
        }
        import hashlib
        event_hash = hashlib.sha256(
            (self.expected_head + canonical(self.record)).encode("utf-8")
        ).hexdigest()
        self.output = {
            "security_event_id": self.security_event_id,
            "audit_log_id": audit_log_id,
            "logged_at": logged_at,
            "previous_event_hash": None if self.expected_head == GENESIS else self.expected_head,
            "event_hash": event_hash,
        }
        return deepcopy(self.output)

    def commit(self):
        self.trail.append_security(
            self.security_event_id, self.record,
            expected_head=self.expected_head,
            expected_audit_log_id=self.output["audit_log_id"],
            expected_event_hash=self.output["event_hash"],
        )
        self.committed = True

    def rollback(self):
        self.payload = None
        self.record = None


class SecurityEventService:
    """The sole runtime implementation of the public log_security_event tool."""

    def __init__(self, database: Database, *, uuid_factory=uuid.uuid4):
        self.trail = AuditTrail(database)
        self.trace_writer = DecisionTraceWriter(self.trail)
        self.uuid_factory = uuid_factory
        self.middleware = ToolErrorMiddleware(uuid_factory=uuid_factory, observer=self.trace_writer)

    def log_untrusted_instruction(self, text: object, *, context: str, workflow_state: str) -> dict:
        excerpt, redacted = redact_untrusted_text(text)
        security_event_id = f"SEC-{self.uuid_factory()}"
        payload = {
            "trigger_source": "prompt_injection_detected",
            "untrusted_text_excerpt": excerpt,
            "sanitization_applied": True,
            "redaction_applied": redacted,
            "context": context[:500],
            "action_taken": "BLOCKED",
        }
        validate_tool_payload(payload, "log_security_event", "input_schema", "log_security_event_input")
        outcome = self.middleware.execute(
            "log_security_event", payload,
            _PreparedSecurityEvent(self.trail, security_event_id),
            entity_references={"security_event_ref": security_event_id},
            trace_metadata={
                "workflow_state_before": workflow_state,
                "workflow_state_after": workflow_state,
                "input_summary": "sanitized untrusted instruction security event",
                "output_summary": "security event committed to append-only audit chain",
            },
        )
        value = json.loads(outcome.wire_bytes.decode("utf-8"))
        if outcome.is_error:
            raise RuntimeError(f"security event logging failed: {value['error_code']}")
        with self.trail.database.lock:
            trace_count = self.trail.database.conn.execute(
                "SELECT COUNT(*) FROM decision_traces WHERE correlation_id=?",
                (outcome.correlation_id,),
            ).fetchone()[0]
        if trace_count != 1:
            raise RuntimeError("security event committed without its required decision trace")
        validate_tool_payload(value, "log_security_event", "output_schema", "log_security_event_output")
        return value

    def log_factory_quarantine(self, record: Mapping[str, Any]) -> list[dict]:
        """Persist every prompt-injection quarantine from a normalized workbook."""
        events = {
            row.get("event_id"): row
            for row in record.get("source_tables", {}).get("Events", [])
            if isinstance(row, dict)
        }
        logged = []
        for issue in record.get("validation", {}).get("quarantined_entities", []):
            if issue.get("code") != "PROMPT_INJECTION":
                continue
            event_id = issue.get("entity_id")
            source = events.get(event_id, {})
            logged.append(self.log_untrusted_instruction(
                source.get("Payload_JSON", "[quarantined instruction]"),
                context=f"Events.Payload_JSON event_id={event_id}",
                workflow_state="INPUT_VALIDATED",
            ))
        return logged


__all__ = [
    "AuditTrail", "DecisionTraceWriter", "GENESIS", "SecurityEventService",
    "redact_untrusted_text",
]
