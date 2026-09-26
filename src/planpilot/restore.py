"""G4 4.5 (design R2/R3): intent-ledger crash-safe DB restore.

The marker `<db>.restore-state` is NOT a phase label - it is an intent
ledger: a fixed op list with per-op pending/done state, plus a per-file
fact table {path -> location/sha256/size/mtime_ns} covering the source
backup, the staging copy and the target main/-wal/-shm trio. Every move
is preceded by a marker write that DECLARES it and followed by a rewrite
that refreshes the facts, so at any hard-kill instant marker and file
positions are consistent enough to RECONCILE. A rerun NEVER trusts the
label: reconcile scans all locations, hashes what exists, re-derives
state from facts only.

Single-process discipline: restore takes the SAME cross-process OS lock
the server holds (db_lock helper), non-blocking. A denial means the
server still holds it and we fail immediately - no online mutation path
exists.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .backup import sha256_file, verify_backup_file
from .db_lock import acquire_server_lock

TOOL_VERSION = "g4-phase4-4.5"

MARKER_SUFFIX = ".restore-state"
RECEIPT_SUFFIX = ".restore-receipt.json"
ACK_SUFFIX = ".restored-acked"
QUAR_ROOT_SUFFIX = ".quarantine"
STAGE_SUFFIX = ".restore-staging"

# Phases, in ledger order. FACTS (per-file location+sha), not labels,
# decide what a rerun does.
PHASE_ORDER = ["PREPARED", "QUARANTINED", "REPLACED", "RECEIPTED"]


class RestoreAborted(RuntimeError):
    """Refusal that must never look like progress (operator message)."""


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def marker_path(db_path):
    return Path(str(db_path) + MARKER_SUFFIX)


def receipt_path(db_path):
    return Path(str(db_path) + RECEIPT_SUFFIX)


def ack_path(db_path):
    return Path(str(db_path) + ACK_SUFFIX)


def quarantine_root(db_path):
    return Path(str(db_path) + QUAR_ROOT_SUFFIX)


def stage_path(db_path):
    return Path(str(db_path) + STAGE_SUFFIX)


def _atomic_write_json(dest: Path, payload: dict) -> None:
    """temp -> fsync -> replace -> parent-dir fsync. The ONLY writer form
    here: a kill at any point leaves either the old bytes or the new
    bytes, never a torn file."""
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fsync_path(p: Path) -> None:
    """Flush disk caches for a file we own. Opened O_RDWR because the
    Windows CRT _commit behind os.fsync REJECTS read-only fds (EBADF);
    every file fsynced here was written by this very process, so it is
    writable by construction."""
    fd = os.open(str(p), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_fact(p: Path) -> dict:
    """Reality probe for ONE path: does it exist, and if so its identity
    bytes. Facts - never the phase label - drive reconciliation."""
    if not p.exists() or p.is_dir():
        return {"exists": False, "sha256": None, "size": None,
                "mtime_ns": None}
    st = p.stat()
    return {"exists": True, "sha256": sha256_file(p), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns}


# Fixed op ledger. `phase` groups ops: a phase advances ONLY when all
# its ops are done. Order IS the execution order when finishing.
OP_DEFS = [
    ("ack-invalidate", "PREPARED"),
    ("stage-copy", "PREPARED"),
    ("stage-verify", "PREPARED"),
    ("quarantine-main", "QUARANTINED"),
    ("quarantine-wal", "QUARANTINED"),
    ("quarantine-shm", "QUARANTINED"),
    ("replace-staging", "REPLACED"),
    ("receipt-write", "RECEIPTED"),
    ("marker-clear", "RECEIPTED"),
]

PHASE_OF_OP = dict(OP_DEFS)


def new_ledger(tool_version=TOOL_VERSION):
    return {
        "tool_version": tool_version,
        "intent_phase": None,          # what the tool is ABOUT to do
        "pending_move": None,          # op name while a move is declared
        "ops": {name: "pending" for name, _ in OP_DEFS},
        "target_db": None,
        "backup_path": None,
        "backup_sha256": None,
        "generation": None,            # <UTCts>-<seq> quarantine dir
        "facts": {},                   # path-string -> fact dict
        "verified_tip": None,          # set by stage-verify (or re-anchored)
        "stage_verified_sha": None,
        "receipt_sha256": None,
    }


def load_ledger(marker: Path):
    """None when absent; raises RestoreAborted when unreadable — a torn
    or corrupt marker is never guessed past."""
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreAborted(f"restore-state marker is unreadable: {exc}")
    ledger = new_ledger()
    ledger.update(data)
    return ledger


def save_ledger(marker: Path, ledger: dict) -> None:
    """Declare-first discipline: this runs BEFORE the op it declares."""
    _atomic_write_json(marker, ledger)


def refresh_facts(ledger: dict, paths) -> None:
    """Record post-move reality — this runs AFTER the op completes."""
    for p in paths:
        ledger["facts"][str(p)] = _file_fact(Path(p))


def _phase_done(ledger: dict, phase: str) -> bool:
    return all(ledger["ops"][n] == "done" for n, ph in OP_DEFS if ph == phase)


def current_phase(ledger: dict) -> str:
    for phase in PHASE_ORDER:
        if not _phase_done(ledger, phase):
            return phase
    return "COMPLETE"


def _absent_fact() -> dict:
    return {"exists": False, "sha256": None, "size": None, "mtime_ns": None}


def reconcile_ledger(ledger: dict) -> dict:
    """The ONLY entry logic (design §5 R2): scan every location, hash
    what is there, and derive per-op done/pending from FACTS. A phase
    label is never trusted — including our own `ops` dict, which is
    overwritten here by what the filesystem actually says.

    Returns {ops: {name: 'done'|'pending'}, conflicts: [str]} — a
    non-empty conflicts list means reality matches NEITHER 'fully done'
    nor 'not started' for some declared move; the caller must abort,
    never guess.
    """
    target, backup, stage, qroot, qdir, marker, receipt = _paths(ledger)
    conflicts: list[str] = []
    ops: dict[str, str] = {}
    stored = ledger["facts"]

    def fact(p: Path) -> dict:
        return _file_fact(Path(p))

    backup_now = fact(backup)
    stage_now = fact(stage)
    main_now = fact(target)
    wal_now = fact(Path(str(target) + "-wal"))
    shm_now = fact(Path(str(target) + "-shm"))
    pre_main = stored.get(str(target)) or _absent_fact()
    pre_wal = stored.get(str(target) + "-wal") or _absent_fact()
    pre_shm = stored.get(str(target) + "-shm") or _absent_fact()

    # --- PREPARED -----------------------------------------------------
    ack_now = fact(ack_path(target))
    ops["ack-invalidate"] = "done" if not ack_now["exists"] else "pending"

    if stage_now["exists"]:
        if stage_now["sha256"] == ledger["backup_sha256"]:
            ops["stage-copy"] = "done"
        elif backup_now["exists"] and stage_now["sha256"] == backup_now["sha256"]:
            # stage matches the CURRENT backup but not the anchored one:
            # the backup changed after the intent — abort, never bless.
            conflicts.append(
                f"staging matches backup bytes {stage_now['sha256'][:12]}... but "
                f"ledger anchored {str(ledger['backup_sha256'])[:12]}...: source "
                "changed mid-restore")
            ops["stage-copy"] = "pending"
        else:
            # torn partial copy from a kill DURING the copy: not a result
            # of the declared move, so the move is 'not started' — the
            # handler deletes and re-copies. Never trusted as staging.
            ops["stage-copy"] = "pending"
    else:
        ops["stage-copy"] = "pending"

    if ops["stage-copy"] == "done":
        tip_ok = (ledger.get("stage_verified_sha") == stage_now["sha256"]
                  and ledger.get("verified_tip"))
        ops["stage-verify"] = "done" if tip_ok else "pending"
    else:
        ops["stage-verify"] = "pending"

    # --- REPLACED, evaluated FIRST ------------------------------------
    # Design §5 verbatim: after os.replace "the ledger's fact
    # target.main_sha == staging_sha proves REPLACED EVEN IF the phase
    # field still reads QUARANTINED". Staging was consumed by design, so
    # the fact is anchored via the backup sha staging verified equal to.
    # replace-proven therefore implies every earlier op completed — and
    # it SUPPRESSES the quarantine conflict detector, which would else
    # read the post-replace bytes at target as "matching no pre-move
    # fact" (kill point 5, hard-kill matrix).
    replace_proven = (main_now["exists"]
                      and main_now["sha256"] == ledger["backup_sha256"])

    # --- QUARANTINED (one file at a time; a half-moved trio is fine) --
    for name, now, pre, qname in (
            ("quarantine-main", main_now, pre_main, target.name),
            ("quarantine-wal", wal_now, pre_wal, target.name + "-wal"),
            ("quarantine-shm", shm_now, pre_shm, target.name + "-shm")):
        ops[name] = _reconcile_quarantine_op(
            name, now, pre, qdir / qname if qdir else None, conflicts,
            replace_proven)

    if replace_proven and stage_now["exists"]:
        ops["replace-staging"] = "pending"     # leftover copy: idempotent redo
    elif replace_proven:
        ops["replace-staging"] = "done"
        ops["stage-copy"] = "done"
        ops["stage-verify"] = "done"
    elif main_now["exists"]:
        ops["replace-staging"] = "pending"
    else:
        # main absent, replace not proven: stage-copy re-runs from the
        # anchored backup if staging is missing (ops run IN ORDER), so
        # this converges rather than conflicting. Only a lost/mismatched
        # backup itself is unsalvageable — the executor refuses then.
        ops["replace-staging"] = "pending"

    # --- RECEIPTED ----------------------------------------------------
    # An OLD receipt bound to a DIFFERENT generation is evidence of a
    # PREVIOUS restore, not of this ledger's receipt-write: this run's
    # handler overwrites it on completion (design §5 R4: every new
    # restore acks separately; second restore of the SAME backup is the
    # spec'd flow). A receipt that no longer matches what THIS ledger
    # recorded after writing it IS tampering — a conflict.
    ops["receipt-write"] = "pending"
    if receipt.exists():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        bound = (isinstance(data, dict)
                 and data.get("generation") == ledger["generation"]
                 and data.get("backup_sha256") == ledger["backup_sha256"]
                 and data.get("target_db") == ledger["target_db"])
        if bound:
            recorded = (ledger["facts"].get(str(receipt)) or {}).get("sha256")
            actual = sha256_file(receipt)
            if recorded is not None and recorded != actual:
                conflicts.append(
                    "receipt bytes no longer match the ledger facts recorded "
                    f"when it was written ({recorded[:12]}... != {actual[:12]}...)")
            else:
                ops["receipt-write"] = "done"
    ops["marker-clear"] = "pending" if marker.exists() else "done"

    return {"ops": ops, "conflicts": conflicts}


def _reconcile_quarantine_op(name, now, pre, qfile, conflicts, replace_proven):
    """One trio member: DONE iff the quarantine copy matches the pre-move
    fact of its target slot (identity moved, bytes untouched); NOT
    STARTED iff the target still matches the pre-move fact (or the slot
    was empty before and is empty now). Anything else is a conflict —
    UNLESS the replace is fact-proven, which retroactively proves every
    earlier move finished (design §5: target bytes == staged/backup sha).
    """
    if replace_proven:
        return "done"
    if now["exists"] and pre["exists"] and now["sha256"] == pre["sha256"]:
        return "pending"                       # still at target, untouched
    if not now["exists"] and not pre["exists"]:
        return "done"                          # slot was empty, nothing to move
    if qfile is not None:
        qf = _file_fact(qfile)
        if qf["exists"] and pre["exists"] and qf["sha256"] == pre["sha256"]:
            return "done"                      # identity sits in quarantine
    if now["exists"] or (qfile is not None and _file_fact(qfile)["exists"]):
        conflicts.append(
            f"{name}: target slot {'holds' if now['exists'] else 'gave way to'} "
            "bytes matching NO recorded pre-move fact — refusing to guess")
    return "pending"


def _paths(ledger: dict):
    """Re-derive every path from the two anchored facts (target, backup).
    Locations are deterministic; the ledger never stores them per-file
    beyond the fact-table keys."""
    target = Path(ledger["target_db"])
    backup = Path(ledger["backup_path"])
    stage = Path(str(target) + STAGE_SUFFIX)
    qroot = Path(str(target) + QUAR_ROOT_SUFFIX)
    qdir = qroot / ledger["generation"] if ledger["generation"] else None
    marker = Path(str(target) + MARKER_SUFFIX)
    receipt = Path(str(target) + RECEIPT_SUFFIX)
    return target, backup, stage, qroot, qdir, marker, receipt


def _fsync_dir(p: Path) -> None:
    """POSIX dir fsync for true durability. Windows has no directory
    fsync primitive at all (os.open on a dir raises PermissionError), so
    the platform check SKIPS it there rather than swallowing real POSIX
    errors behind a blanket try/except."""
    if os.name == "nt":
        return
    fd = os.open(str(p), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _trio(target: Path):
    return (target, Path(str(target) + "-wal"), Path(str(target) + "-shm"))


def _new_generation(target: Path) -> str:
    """`<UTCts>-<seq>` unique by CREATION: mkdir the final name with
    exist_ok=False, bump seq on collision. Returns the dir uncreated?
    No — CREATED, that is the point: two runs in the same microsecond
    cannot share a generation."""
    root = quarantine_root(target)
    root.mkdir(parents=True, exist_ok=True)
    ts = _now_ts()
    seq = 1
    while True:
        try:
            (root / f"{ts}-{seq}").mkdir()
            return f"{ts}-{seq}"
        except FileExistsError:
            seq += 1


# ------------------------------------------------------------------ ops
def _op_ack_invalidate(ledger):
    """Every NEW restore invalidates the previous ack atomically BEFORE
    touching target files, ledgered so the invalidation survives a kill
    (design §5 R4)."""
    ack = ack_path(Path(ledger["target_db"]))
    if ack.exists():
        ack.unlink()
        _fsync_dir(ack.parent)


def _op_stage_copy(ledger):
    target, backup, stage, _q, _qd, _m, _r = _paths(ledger)
    if stage.exists() and sha256_file(stage) != ledger["backup_sha256"]:
        stage.unlink()                     # torn partial: move never started
    if not stage.exists():
        if not backup.is_file():
            raise RestoreAborted(f"anchored backup is gone: {backup}")
        if sha256_file(backup) != ledger["backup_sha256"]:
            raise RestoreAborted(
                "backup bytes changed since the intent was anchored — "
                "refusing to guess which generation to restore")
        shutil.copyfile(backup, stage)
        _fsync_path(stage)


def _op_stage_verify(ledger):
    _t, _b, stage, _q, _qd, _m, _r = _paths(ledger)
    v = verify_backup_file(stage)
    if not v.ok:
        raise RestoreAborted(
            "staging copy refused by the read-only battery (4.2):\n"
            + v.failure_text())
    if v.sha256 != ledger["backup_sha256"]:
        raise RestoreAborted("staging sha drifted from the anchored backup")
    ledger["verified_tip"] = {"entry_count": v.entry_count,
                              "event_hash": v.event_hash}
    ledger["stage_verified_sha"] = v.sha256


def _quarantine_op(index: int):
    def handler(ledger):
        target = Path(ledger["target_db"])
        src = _trio(target)[index]
        if not src.exists():
            return                          # empty slot: vacuously done
        qdir = quarantine_root(target) / ledger["generation"]
        qdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(qdir / src.name))
    return handler


def _op_replace_staging(ledger):
    target, _b, stage, _q, _qd, _m, _r = _paths(ledger)
    if not stage.exists():
        if sha256_file(target) == ledger["backup_sha256"]:
            return                          # replace already proven by facts
        raise RestoreAborted("staging vanished and target does not match "
                             "the anchored backup — no source to replace from")
    if stage.parent != target.parent:
        raise RestoreAborted("staging is not on the target volume — "
                             "os.replace would not be atomic")
    os.replace(stage, target)
    _fsync_path(target)
    _fsync_dir(target.parent)


def _op_receipt_write(ledger):
    _t, _b, _s, _q, _qd, _m, receipt = _paths(ledger)
    tip = ledger.get("verified_tip")
    if tip is None:
        # Inferred-done path (replace proven, staging already consumed):
        # no stage-verify ran THIS process, so walk the chain read-only
        # off the target itself — safe because target bytes == anchored
        # backup sha (replace_proven) and we hold the OS lock.
        v = verify_backup_file(Path(ledger["target_db"]))
        if not v.ok or v.sha256 != ledger["backup_sha256"]:
            raise RestoreAborted(
                "receipt needs a verified audit head; the target no longer "
                "matches the anchored backup — refusing to write a receipt "
                "over unverified bytes")
        tip = {"entry_count": v.entry_count, "event_hash": v.event_hash}
        ledger["verified_tip"] = tip
    payload = {
        "backup_sha256": ledger["backup_sha256"],
        "backup_path": ledger["backup_path"],
        "target_db": ledger["target_db"],
        "audit_head_entry_count": tip["entry_count"],
        "event_hash": tip["event_hash"],
        "quarantine_dir": str(quarantine_root(Path(ledger["target_db"]))),
        "generation": ledger["generation"],
        "restored_at": _now_ts(),
        "tool_version": ledger["tool_version"],
        "restored_db_sha256": sha256_file(Path(ledger["target_db"])),
    }
    _atomic_write_json(receipt, payload)
    ledger["receipt_sha256"] = sha256_file(receipt)


def _op_marker_clear(ledger):
    """Last op, only after the receipt is durable (design §5 R2:
    marker cleared AFTER receipt fsynced)."""
    marker = marker_path(Path(ledger["target_db"]))
    if marker.exists():
        marker.unlink()
        _fsync_dir(marker.parent)


OP_HANDLERS = {
    "ack-invalidate": _op_ack_invalidate,
    "stage-copy": _op_stage_copy,
    "stage-verify": _op_stage_verify,
    "quarantine-main": _quarantine_op(0),
    "quarantine-wal": _quarantine_op(1),
    "quarantine-shm": _quarantine_op(2),
    "replace-staging": _op_replace_staging,
    "receipt-write": _op_receipt_write,
    "marker-clear": _op_marker_clear,
}


def _kill_point(op_name: str) -> None:
    """Test seam (4.7): PLANPILOT_RESTORE_KILL_AT names an op; when set
    to THIS op we os._exit mid-sequence — a hard kill, no cleanup, no
    finally, exactly the crash the ledger must survive."""
    point = os.environ.get("PLANPILOT_RESTORE_KILL_AT")
    if point == op_name:
        os._exit(9)


def _touch_ledger_facts(ledger):
    target, backup, stage, _qroot, qdir, marker, receipt = _paths(ledger)
    paths = [backup, stage, marker, receipt, ack_path(target)]
    paths.extend(_trio(target))
    if qdir:
        paths.extend(qdir / p.name for p in _trio(target))
    refresh_facts(ledger, paths)


def execute_ledger(marker: Path, ledger: dict) -> dict:
    """Run reconcile FIRST, then every still-pending op in ledger order.
    Each op: declare (pending_move persisted) -> kill point -> handler ->
    mark done -> refresh facts -> persist. The on-disk marker and the
    file positions are therefore consistent at every kill instant."""
    while True:
        rec = reconcile_ledger(ledger)
        if rec["conflicts"]:
            raise RestoreAborted(
                "reconciliation found a world no rule can explain:\n- "
                + "\n- ".join(rec["conflicts"]))
        ledger["ops"] = rec["ops"]
        pending = [name for name, _ph in OP_DEFS
                   if ledger["ops"][name] != "done"]
        if not pending:
            break
        for name in pending:
            ledger["intent_phase"] = PHASE_OF_OP[name]
            ledger["pending_move"] = name
            save_ledger(marker, ledger)          # DECLARE first
            _kill_point(name)                    # test seam: hard exit
            OP_HANDLERS[name](ledger)
            ledger["ops"][name] = "done"
            ledger["pending_move"] = None
            if name == "marker-clear":
                # The marker IS the ledger file — after clearing it there
                # is nothing left to persist (and persisting would RESURRECT
                # it). This must be the last write-free exit.
                return ledger
            _touch_ledger_facts(ledger)
            save_ledger(marker, ledger)          # facts refreshed after
        # loop: re-reconcile what the handlers actually achieved
    return ledger


def plan_restore(db_path, backup_path, fresh: bool = True):
    """Build/anchor the ledger for THIS restore. Never trusts a stale
    marker bytes: fresh=True (a new restore) rewrites the ledger from
    scratch after anchoring backup facts; resuming (CLI without a new
    backup arg) passes an existing dict through."""
    target = Path(db_path)
    backup = Path(backup_path)
    if not backup.is_file():
        raise RestoreAborted(f"backup file not found: {backup}")
    v = verify_backup_file(backup)
    if not v.ok:
        raise RestoreAborted(
            f"backup refused by the read-only battery BEFORE any intent "
            f"was written (never restorable):\n{v.failure_text()}")
    ledger = new_ledger()
    ledger["target_db"] = str(target)
    ledger["backup_path"] = str(backup)
    ledger["backup_sha256"] = v.sha256
    ledger["generation"] = _new_generation(target)
    _touch_ledger_facts(ledger)
    return ledger


def restore_database(db_path, backup_path, lock_handle_owner=None):
    """Single-process discipline: take the SAME cross-process OS lock the
    server holds, non-blocking; a denial means a live server — abort,
    online mutation never exists. Returns the finished ledger."""
    target = Path(db_path)
    holder = acquire_server_lock(target)   # DbLockHeld propagates
    try:
        marker = marker_path(target)
        existing = load_ledger(marker)
        if existing is not None and existing["target_db"] == str(target) \
                and existing["backup_path"] == str(Path(backup_path)):
            ledger = existing                  # resume: reconcile decides
        else:
            ledger = plan_restore(target, backup_path)
            save_ledger(marker, ledger)
        return execute_ledger(marker, ledger)
    finally:
        holder.release()


def rollback_ledger(db_path) -> dict:
    """`--rollback`: return the world to the pre-restore state byte-exact.
    Quarantined trio moves back to the target slots (reverse order:
    shm, wal, main — each individually ledgered by reconcile), the
    staging copy is removed, the marker is deleted. Requires a marker
    with a recorded generation; a receipt for a COMPLETED restore is
    left alone untouched (rollback of a finished restore is not the
    contract — the operator restores a different backup instead)."""
    target = Path(db_path)
    marker = marker_path(target)
    ledger = load_ledger(marker)
    if ledger is None:
        raise RestoreAborted("no restore-state marker: nothing to roll back")
    holder = acquire_server_lock(target)
    try:
        qdir = quarantine_root(target) / ledger["generation"] \
            if ledger["generation"] else None
        if qdir is None or not qdir.is_dir():
            raise RestoreAborted(
                f"quarantine generation dir is gone ({qdir}): refusing to "
                "guess where the old bytes went")
        # Refuse to roll back over bytes the ledger cannot explain.
        # BOTH-present is LEGAL only for main-after-os.replace: the target
        # holds the replace's new bytes (== anchored backup sha) while our
        # original waits in quarantine — discard the new bytes and move
        # the original back. Anything else at both slots = a third party.
        for idx, p in enumerate(_trio(target)):
            now = _file_fact(p)
            qf = _file_fact(qdir / p.name)
            if now["exists"] and qf["exists"]:
                if idx == 0 and now["sha256"] == ledger["backup_sha256"]:
                    p.unlink()               # replace wrote these, ours
                else:
                    raise RestoreAborted(
                        f"rollback: {p.name} exists at BOTH target and "
                        "quarantine with bytes this ledger never produced; "
                        "refusing")
            elif now["exists"]:
                pre = ledger["facts"].get(str(p)) or _absent_fact()
                if not (pre["exists"] and pre["sha256"] == now["sha256"]) \
                        and now["sha256"] != ledger["backup_sha256"]:
                    raise RestoreAborted(
                        f"rollback: {p.name} at target matches neither the "
                        "pre-move fact nor the anchored backup — refusing "
                        "to clobber unknown bytes")
        for p in reversed(_trio(target)):
            src = qdir / p.name
            if src.exists():
                shutil.move(str(src), str(p))
        stage = Path(str(target) + STAGE_SUFFIX)
        if stage.exists():
            stage.unlink()
        # Same-generation receipt/ack are evidence of THIS interrupted
        # ledger — a rolled-back restore leaves none behind (a later
        # gate check would reject with an unsalvageable reason).
        r = receipt_path(target)
        if r.exists():
            try:
                rdata = json.loads(r.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rdata = None
            if isinstance(rdata, dict) and rdata.get("generation") == \
                    ledger["generation"]:
                r.unlink()
        a = ack_path(target)
        if a.exists():
            try:
                adata = json.loads(a.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                adata = None
            if isinstance(adata, dict) and adata.get("generation") == \
                    ledger["generation"]:
                a.unlink()
        _touch_ledger_facts(ledger)
        save_ledger(marker, ledger)          # facts before delete: a kill
        #  here leaves a marker whose facts say 'rolled back' (target trio
        #  present, staging gone) — reconcile reads it, ops all pending
        #  against a pre-restore world; a later restore or rollback works.
        if qdir.exists() and not any(qdir.iterdir()):
            qdir.rmdir()
        marker.unlink()
        _fsync_dir(marker.parent)
        return ledger
    finally:
        holder.release()


# ------------------------------------------------------------------ ack / gate
def write_ack(db_path, operator: str) -> dict:
    """The ONLY ack path (design §5 R4): binds the four security fields —
    receipt file's own sha256, generation, backup_sha256, target_db —
    plus provenance (operator, timestamp) and ack-time evidence
    (restored_db_sha256_at_ack, never re-compared at boot)."""
    target = Path(db_path)
    receipt = receipt_path(target)
    if not receipt.is_file():
        raise RestoreAborted(
            "no restore-receipt to ack: a restore must complete first")
    rdata = json.loads(receipt.read_text(encoding="utf-8"))
    payload = {
        "receipt_sha256": sha256_file(receipt),
        "generation": rdata["generation"],
        "backup_sha256": rdata["backup_sha256"],
        "target_db": str(target),
        "operator": operator,
        "acked_at": _now_ts(),
        "restored_db_sha256_at_ack": sha256_file(target),
    }
    ack = ack_path(target)
    _atomic_write_json(ack, payload)
    return payload


def evaluate_recovery_gate(db_path) -> tuple[bool, list[str]]:
    """R4 gate, read-only: is this database allowed to bind a socket?
    (ok=False => startup MUST abort, dev included.) A restore happened
    when the receipt exists OR the marker exists (marker != cleared:
    its mere presence means an intent never finished being receipted).
    The ack must then match ALL FOUR security-binding fields; operator
    and timestamp are provenance only — same-format edits to them are
    NOT claimed detectable (round-5)."""
    target = Path(db_path)
    receipt = receipt_path(target)
    marker = marker_path(target)
    if not receipt.exists() and not marker.exists():
        return True, []                      # no restore ever happened here
    reasons: list[str] = []
    if marker.exists():
        reasons.append("restore-state marker present: an intent ledger "
                       "never reached receipt (interrupted or unfinished "
                       "restore)")
    if not receipt.is_file():
        reasons.append("no restore-receipt on disk: nothing to ack against")
        return False, reasons
    try:
        receipt_sha = sha256_file(receipt)
        rdata = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, [f"receipt is unreadable: {exc}"]
    ack = ack_path(target)
    if not ack.is_file():
        reasons.append("no ack file: run restore_database.py --ack <operator>")
        return False, reasons
    try:
        adata = json.loads(ack.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, [f"ack file is malformed/unparseable: {exc}"]
    checks = (("receipt_sha256", adata.get("receipt_sha256"), receipt_sha),
              ("generation", adata.get("generation"), rdata.get("generation")),
              ("backup_sha256", adata.get("backup_sha256"),
               rdata.get("backup_sha256")),
              ("target_db", adata.get("target_db"), str(target)))
    for field, got, want in checks:
        if got != want:
            reasons.append(f"ack field {field} does not match the receipt"
                           if got is not None else f"ack field {field} missing")
    if reasons:
        return False, reasons
    return True, []
