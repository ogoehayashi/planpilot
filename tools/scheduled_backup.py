"""One online backup + V1 battery + bounded retention (systemd timer).

G4 4.3 (design §5 V3): a scheduled backup enters the verified set ONLY
through the read-only battery. `backup_database()` KEEPS its -> None
signature — this tool derives the deterministic name from its own
inputs, runs the battery right after create, and assembles the
manifest HERE (no public API change). Failure -> file moved to
<dir>/unverified/, never counted in the retention success set, never
advertised. Success -> atomic (temp->fsync->replace) write of
<dir>/last_verified_backup.json {path, sha256, verified_at,
chain_head:{entry_count,event_hash}} — the manifest deep-health
reads for backup age (P1-4: never a newest-mtime guess).
"""
import argparse
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from planpilot.backup import backup_database, sha256_file, verify_backup_file

NAME_RE = re.compile(r'planpilot-\d{8}T\d{12}Z\.db')
MANIFEST_NAME = 'last_verified_backup.json'


def _atomic_write_json(dest: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(source, directory, keep):
    if keep < 1:
        raise ValueError('keep must be positive')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('planpilot-'
                          + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                          + '.db')
    backup_database(source, target)

    # V3 gate: verification happens BEFORE anything is advertised.
    result = verify_backup_file(target)
    if not result.ok:
        quarantine = directory / 'unverified'
        quarantine.mkdir(exist_ok=True)
        moved = quarantine / target.name
        shutil.move(str(target), str(moved))
        return {'backup': None, 'quarantined': str(moved),
                'failure': result.failure_text(), 'removed': []}

    removed = []
    backups = sorted((p for p in directory.iterdir()
                      if p.is_file() and not p.is_symlink()
                      and NAME_RE.fullmatch(p.name)), reverse=True)
    for old in backups[keep:]:
        old.unlink()
        removed.append(old.name)
    quarantine = directory / 'unverified'
    if quarantine.is_dir():
        # quarantine gets its OWN bounded prune (same keep): failing
        # files must not accumulate unboundedly either.
        stale = sorted((p for p in quarantine.iterdir()
                        if p.is_file() and not p.is_symlink()
                        and NAME_RE.fullmatch(p.name)), reverse=True)
        for old in stale[keep:]:
            old.unlink()
            removed.append('unverified/' + old.name)

    _atomic_write_json(directory / MANIFEST_NAME, {
        'path': str(target),
        'sha256': sha256_file(target),
        'verified_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'chain_head': {'entry_count': result.entry_count,
                       'event_hash': result.event_hash},
    })
    return {'backup': str(target), 'removed': removed,
            'manifest': str(directory / MANIFEST_NAME)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('directory')
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args()
    print(run(args.source, args.directory, args.keep))
