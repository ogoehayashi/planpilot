"""One online backup and bounded retention, intended for a systemd timer."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re

from planpilot.backup import backup_database


def run(source, directory, keep):
    if keep < 1:
        raise ValueError('keep must be positive')
    directory = Path(directory).resolve()
    target = directory / ('planpilot-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
    backup_database(source, target)
    backups = sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()
                      and re.fullmatch(r'planpilot-\d{8}T\d{12}Z\.db', p.name)), reverse=True)
    removed = []
    for old in backups[keep:]:
        old.unlink()
        removed.append(old.name)
    return {'backup': str(target), 'removed': removed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('directory')
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args()
    print(run(args.source, args.directory, args.keep))
