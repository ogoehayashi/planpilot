from __future__ import annotations
import os
import sqlite3
import tempfile
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
