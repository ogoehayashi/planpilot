#!/usr/bin/env python3
"""G4 4.5/4.6 - the ONLY path that restores a DB or acks a restore.

Mutually exclusive modes:
  <backup>            run/continue the intent ledger for one backup
  --rollback          pre-restore world, byte-exact, or refuse
  --ack OPERATOR      write the per-restore ack (only ack path)
  --status            offline diagnosis: ledger/gate view, zero writes
  --show-receipt      print the receipt JSON, zero writes

Offline-only guarantee (4.6): this module imports json/os/sys/argparse
+ planpilot.restore/backup/db_lock ONLY. No Server, no Database, no
RuntimeAuthority, no ScenarioClock is ever constructed here; --status/
--show-receipt are pure reads. Exit codes: 0 ok, 1 refused/aborted,
2 usage, 3 a restore is pending operator attention (status mode).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planpilot.restore import (  # noqa: E402
    RestoreAborted, ack_path, load_ledger, marker_path, receipt_path,
    restore_database, rollback_ledger, evaluate_recovery_gate, write_ack,
    current_phase, reconcile_ledger,
)
from planpilot.db_lock import DbLockHeld  # noqa: E402


def _default_db() -> Path:
    env = os.environ.get("PLANPILOT_DB")
    if env:
        return Path(env)
    return Path("data") / "planpilot.db"


def _resolve_db(args) -> Path:
    return Path(args.db) if args.db else _default_db()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="restore_database.py",
        description="G4 crash-safe DB restore (intent ledger).")
    ap.add_argument("backup", nargs="?", help="verified backup file")
    ap.add_argument("--db", help="target database (default PLANPILOT_DB)")
    ap.add_argument("--rollback", action="store_true",
                    help="return to the pre-restore world, byte-exact")
    ap.add_argument("--ack", metavar="OPERATOR",
                    help="write the per-restore ack (ONLY ack path)")
    ap.add_argument("--status", action="store_true",
                    help="offline ledger/gate view (zero writes)")
    ap.add_argument("--show-receipt", action="store_true",
                    help="print the receipt JSON (zero writes)")
    args = ap.parse_args(argv)

    modes = [bool(args.backup), args.rollback, args.ack is not None,
             args.status, args.show_receipt]
    if sum(modes) != 1:
        print("exactly one of: <backup> | --rollback | --ack OPERATOR | "
              "--status | --show-receipt", file=sys.stderr)
        return 2
    db = _resolve_db(args)
    try:
        return _dispatch(args, db)
    except RestoreAborted as exc:
        print(f"RESTORE REFUSED:\n{exc}", file=sys.stderr)
        return 1
    except DbLockHeld as exc:
        print(f"RESTORE REFUSED (server holds the DB lock):\n{exc}",
              file=sys.stderr)
        return 1


def _dispatch(args, db: Path) -> int:
    if args.status:
        return _status(db)
    if args.show_receipt:
        r = receipt_path(db)
        if not r.is_file():
            print(f"no receipt at {r}", file=sys.stderr)
            return 1
        print(r.read_text(encoding="utf-8"))
        return 0
    if args.ack is not None:
        payload = write_ack(db, args.ack)
        print("ACK WRITTEN:", json.dumps(payload, sort_keys=True))
        return 0
    if args.rollback:
        rollback_ledger(db)
        print(f"ROLLED BACK to the pre-restore world at {db}")
        return 0
    ledger = restore_database(db, args.backup)
    print("RESTORED generation", ledger["generation"],
          "phase", current_phase(ledger))
    return 0


def _status(db: Path) -> int:
    marker = marker_path(db)
    ledger = load_ledger(marker)
    gate_ok, reasons = evaluate_recovery_gate(db)
    print(f"db: {db}")
    print(f"receipt: {'yes' if receipt_path(db).is_file() else 'no'}")
    print(f"ack: {'yes' if ack_path(db).is_file() else 'no'}")
    if ledger is None:
        print("ledger: (no marker)")
    else:
        rec = reconcile_ledger(ledger)
        print(f"ledger: generation={ledger['generation']} "
              f"phase={current_phase(ledger)}")
        print("facts-derived ops:", json.dumps(rec["ops"], sort_keys=True))
        for c in rec["conflicts"]:
            print("CONFLICT:", c)
    print("gate:", "OPEN (server may start)" if gate_ok
          else "BLOCKED -> " + "; ".join(reasons))
    return 0 if gate_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())