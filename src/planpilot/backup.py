from __future__ import annotations
import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def backup_database(source: str | Path, destination: str | Path) -> None:
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source == destination or (destination.exists() and source.samefile(destination)):
        raise ValueError('backup destination must differ from source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.planpilot-backup-', suffix='.db', dir=destination.parent)
    os.close(fd)
    try:
        source_conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)
        target_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(target_conn, pages=256, sleep=0.05)
            if target_conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup integrity check failed')
        finally:
            target_conn.close()
            source_conn.close()
        with open(temporary, 'r+b') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256_file(path: Path) -> str:
    """Full-file SHA-256, chunked — used for the before/after invariant
    of the verification battery (G4 4.2)."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


# G4 4.2 (design §5 V1, grep-verified at 855a5c0): every table a FULL
# RUNTIME backup must carry. A bare-Database fixture that never ran
# AuditTrail/FactoryStateRegistry init is intentionally NOT restorable
# (round-4 policy) — the verdict text names which tables were missing.
REQUIRED_TABLES = (
    'audit_chain', 'audit_chain_head', 'authority_state', 'clock_session',
    'publication_receipt', 'idempotency_registry', 'security_events',
    'decision_traces', 'factory_states',
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class BackupVerification:
    """Result of the G4 4.2 read-only battery. `ok` is the AND of every
    check; `entry_count`/`event_hash` are the verified audit tip when
    the chain walk ran, else None."""
    path: str
    sha256: str
    ok: bool
    checks: tuple[Check, ...]
    entry_count: int | None = None
    event_hash: str | None = None

    def failure_text(self) -> str:
        return ' | '.join(f'{c.name}: {c.detail}' for c in self.checks if not c.ok)


def verify_backup_file(path: str | Path) -> BackupVerification:
    """G4 4.2 (design §5 V1) — the FULL backup battery, SEPARATE and
    read-only, never repairing what it verifies:
      1. refuse a stranded -wal/-shm sibling (copy is not self-contained);
      2. open file:...?mode=ro&immutable=1 + PRAGMA query_only=ON;
      3. PRAGMA integrity_check must be exactly 'ok';
      4. REQUIRED_TABLES all present (SELECT from sqlite_master — read);
      5. verify_audit_connection chain+head walk (V1, F5 verbatim rule);
      6. the file's SHA-256 MUST be byte-identical after every outcome,
         pass or fail — asserted here AND pinned per outcome in tests.
    Raises FileNotFoundError/OSError for an unusable path; expected
    corruption is reported via checks, never by raising."""
    from .audit import AuditChainError, verify_audit_connection

    p = Path(path).resolve(strict=True)
    sha_before = sha256_file(p)
    checks: list[Check] = []
    entry_count = event_hash = None

    wal, shm = Path(str(p) + '-wal'), Path(str(p) + '-shm')
    stranded = [q.name for q in (wal, shm) if q.exists()]
    checks.append(Check(
        'self_contained', not stranded,
        f'stranded sidecar(s) beside the copy: {stranded}; refuse — a '
        'backup with live WAL/SHM siblings is not a complete file'
        if stranded else 'no -wal/-shm sibling beside the copy'))

    conn = None
    walkable = False
    try:
        if stranded:
            checks.append(Check('integrity_check', False,
                                'skipped — file refused as not self-contained'))
            checks.append(Check('required_tables', False,
                                'skipped — file refused as not self-contained'))
            checks.append(Check('audit_chain', False,
                                'skipped — file refused as not self-contained'))
        else:
            conn = sqlite3.connect(p.as_uri() + '?mode=ro&immutable=1', uri=True)
            conn.execute('PRAGMA query_only=ON')
            conn.row_factory = sqlite3.Row

            integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
            checks.append(Check('integrity_check', integrity == 'ok',
                                f'returned {integrity!r}' if integrity != 'ok'
                                else "returned 'ok'"))

            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            checks.append(Check(
                'required_tables', not missing,
                f'missing table(s): {missing}; only a FULL runtime backup '
                'is restorable (design §5 policy)' if missing
                else f'all {len(REQUIRED_TABLES)} core tables present'))

            walkable = integrity == 'ok' and not missing
            if walkable:
                try:
                    tip = verify_audit_connection(conn)
                    entry_count = tip['entry_count']
                    event_hash = tip['event_hash']
                    checks.append(Check(
                        'audit_chain', True,
                        f'{entry_count} entries, tip {event_hash[:16]}…'))
                except AuditChainError as exc:
                    checks.append(Check('audit_chain', False,
                                        f'{exc.kind}: {exc}'))
            else:
                checks.append(Check(
                    'audit_chain', False,
                    'skipped — earlier battery check(s) failed; never walked'))
    finally:
        if conn is not None:
            conn.close()

    sha_after = sha256_file(p)
    checks.append(Check(
        'sha_unchanged', sha_before == sha_after,
        f'verification mutated the file ({sha_before[:12]}… != {sha_after[:12]}…)'
        if sha_before != sha_after else f'SHA-256 {sha_after} identical before/after'))

    ok = all(c.ok for c in checks)
    return BackupVerification(path=str(p), sha256=sha_after, ok=ok,
                              checks=tuple(checks),
                              entry_count=entry_count, event_hash=event_hash)
