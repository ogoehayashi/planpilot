#!/usr/bin/env python3
"""G4 4.2 - verify_backup.py: run the full battery on ONE backup file.

Read-only by construction. Exit 0 only when ok=True; 1 = battery
failure (never restorable); 2 = usage/path error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planpilot.backup import verify_backup_file  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_backup.py",
        description="Read-only G4 backup battery (never repairs).")
    ap.add_argument("backup", help="path to a planpilot backup .db")
    args = ap.parse_args(argv)
    try:
        result = verify_backup_file(Path(args.backup))
    except FileNotFoundError:
        print("VERIFY | PATH-ERROR | no such file:", args.backup)
        return 2
    print(result.failure_text() if not result.ok else
          f"VERIFY | PASS | {result.path} | entries={result.entry_count} "
          f"| tip={result.event_hash[:16]}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
