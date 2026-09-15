"""Create a source handoff ZIP without local credentials, databases or caches."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {'src', 'tools', 'tests', 'docs', 'examples', 'data', 'contract', 'deploy', '.kiro'}
EXCLUDED = {'.git', '__pycache__', '.pytest_cache', 'secrets', 'dist', 'node_modules', '_scratch'}
SUFFIXES = {'.py', '.md', '.json', '.txt', '.log', '.html', '.css', '.js', '.cjs', '.ps1', '.yaml', '.yml',
            '.service', '.timer', '.xlsx', '.ini', '.cfg', '.toml', '.bat', '.sh', '.svg', '.png', '.jpg'}
SPECIAL = {'.env.example', '.gitignore', '.dockerignore', '.gitattributes', 'Dockerfile'}


def included(relative):
    if relative.as_posix() == 'PACKAGE_MANIFEST.json':
        return False
    if any(p in EXCLUDED or p.startswith(('.venv', '.pycache')) for p in relative.parts):
        return False
    if relative.name.startswith('.env') and relative.name != '.env.example':
        return False
    if relative.name.lower() in {'claudeapi.txt', 'bedrock-api-key.txt', 'credentials', 'token.txt'}:
        return False
    if len(relative.parts) > 1 and relative.parts[0] not in DIRECTORIES:
        return False
    return relative.suffix.lower() in SUFFIXES or relative.name in SPECIAL


def check_secrets(data, known, name):
    if any(secret in data for secret in known):
        raise ValueError(f'Known credential found in {name}; packaging stopped')
    patterns = (rb'\bABSK[A-Za-z0-9+/=_-]{40,}', rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
                rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    if any(re.search(pattern, data) for pattern in patterns):
        raise ValueError(f'Credential-shaped content found in {name}; packaging stopped')


def collect_files(secret_files):
    known = []
    for filename in secret_files:
        secret = filename.read_text(encoding='utf-8-sig').strip()
        if secret:
            known.append(secret.encode('utf-8'))
    files = []
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if not included(relative) or not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f'External or linked file is not distributable: {relative}')
        raw = path.read_bytes()
        check_secrets(raw, known, relative.as_posix())
        if path.suffix == '.xlsx':
            with zipfile.ZipFile(io.BytesIO(raw)) as workbook:
                for member in workbook.infolist():
                    if member.file_size > 20_000_000:
                        raise ValueError('Workbook member exceeds package safety limit')
                    check_secrets(workbook.read(member), known, relative.as_posix())
        files.append((relative.as_posix(), raw))
    required = {'START_HERE.md', 'tools/setup_local.ps1', 'tools/start_local.ps1',
                'tools/api_server.py', 'tools/web/index.html', 'examples/factory_demo.json',
                'contract/planpilot_agent_contract_v1.8.json'}
    if not required.issubset({name for name, _ in files}):
        raise ValueError('Required handoff files are missing')
    return files


def make_manifest(files):
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'format': 'source-handoff',
            'python': '3.11', 'default_region': 'ap-southeast-1', 'default_model': 'amazon.nova-pro-v1:0',
            'files': [{'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                      for name, raw in files]}


def refresh_manifest(secret_files):
    manifest = make_manifest(collect_files(secret_files))
    target = ROOT / 'PACKAGE_MANIFEST.json'
    temporary = ROOT / 'PACKAGE_MANIFEST.json.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(target)
    print(json.dumps({'manifest': str(target), 'files': len(manifest['files']),
                      'integrity': 'refreshed'}, ensure_ascii=False))


def build(destination, secret_files):
    destination = destination.resolve()
    if destination.suffix.lower() != '.zip' or destination.exists():
        raise ValueError('Output must be a new .zip file; existing archives are never overwritten')
    files = collect_files(secret_files)
    manifest = make_manifest(files)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files:
            archive.writestr('PlanPilot/' + name, raw)
        archive.writestr('PlanPilot/PACKAGE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP integrity verification failed')
        for entry in manifest['files']:
            raw = archive.read('PlanPilot/' + entry['path'])
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Manifest verification failed')
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(checksum + '  ' + destination.name + '\n', encoding='ascii')
    print(json.dumps({'archive': str(destination), 'files': len(files), 'bytes': destination.stat().st_size,
                      'sha256': checksum, 'integrity': 'verified'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path)
    mode.add_argument('--refresh-manifest', action='store_true')
    parser.add_argument('--secret-file', type=Path, action='append', default=[],
                        help='Additional external credential file to check for accidental inclusion; never archived')
    args = parser.parse_args()
    if args.refresh_manifest:
        refresh_manifest(args.secret_file)
    else:
        build(args.output, args.secret_file)
