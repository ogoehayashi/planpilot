"""Build & verify the PlanPilot linux wheelhouse (G4 Phase 5.1, design §6).

SINGLE LAYOUT (round-4 P1-5 — the three competing names are retired):
  - `deploy/wheelhouse/MANIFEST.json`  — TRACKED in Git (filenames +
    sha256 + sizes + the exact download recipe)
  - the wheel dir — genuinely OUT OF THE REPO (task wording): default
    `<repo-dir's parent>/planpilot-wheelhouse`, i.e. a sibling of the
    clone, never inside Git. Any --wheel-dir override is allowed;
    .gitignore refuses `*.whl` under the repo as a backstop.

The manifest is GENERATED, never hand-written: `download` runs pip and
hashes whatever landed; `verify` re-hashes the dir against the tracked
manifest and fails closed on ANY mismatch (missing file, extra file,
byte drift). The Docker build (5.3, BLOCKED until docker exists)
installs from this wheelhouse with `pip install --no-index
--find-links`, so `verify` passing is the precondition for trusting an
offline image build.

Exit codes: 0 ok / 2 verification failed / 3 environment refuses
(no pip, bad args).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "deploy" / "wheelhouse" / "MANIFEST.json"
DEFAULT_WHEEL_DIR = ROOT.parent / "planpilot-wheelhouse"

# The linux target for the container image (python:3.11-slim,
# manylinux x86_64). Pinned here so download and review see ONE recipe.
PLATFORM_ARGS = [
    "--only-binary=:all:",
    "--platform", "manylinux2014_x86_64",
    "--python-version", "3.11",
]
REQUIREMENTS_FILES = ["requirements.txt", "requirements-import.txt"]


def requirement_pins() -> list[str]:
    """Direct pins from the tracked requirements files (exact versions)."""
    pins = []
    for name in REQUIREMENTS_FILES:
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pins.append(line)
    return sorted(pins)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(wheel_dir: Path) -> dict:
    files = sorted(
        ({"name": p.name, "sha256": sha256_of(p), "size": p.stat().st_size}
         for p in wheel_dir.glob("*.whl")),
        key=lambda w: w["name"])
    if not files:
        raise ValueError(f"no wheels found under {wheel_dir}")
    return {
        "manifest_version": 1,
        "generated_at": datetime.now(
            timezone.utc).isoformat(timespec="seconds"),
        "target": {
            "os": "linux", "arch": "x86_64", "python_version": "3.11",
            "pip_platform_args": PLATFORM_ARGS,
        },
        "requirements_files": {
            name: sha256_of(ROOT / name) for name in REQUIREMENTS_FILES
        },
        "direct_pins": requirement_pins(),
        "wheels": files,
        "wheel_count": len(files),
        "total_bytes": sum(w["size"] for w in files),
    }


def cmd_download(args) -> int:
    wheel_dir = Path(args.wheel_dir)
    # 5.2.1 (reviewer P2): `download` used to write into a directory
    # that already contained wheels, and build_manifest() would then
    # bless every stale/unrelated .whl into the new tracked manifest.
    # The destination must be EMPTY of wheels (fresh dir, or one whose
    # content we provably produced) or the run refuses.
    preexisting = sorted(wheel_dir.glob("*.whl")) if wheel_dir.exists() else []
    if preexisting:
        print(f"REFUSING download into non-empty wheel dir: {wheel_dir} "
              f"already holds {len(preexisting)} .whl file(s). "
              "Empty the dir or pass a fresh --wheel-dir.", file=sys.stderr)
        return 3
    wheel_dir.mkdir(parents=True, exist_ok=True)
    cmd = [args.python, "-m", "pip", "download", *PLATFORM_ARGS,
           "-d", str(wheel_dir), *requirement_pins()]
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(f"pip download failed (exit {proc.returncode})",
              file=sys.stderr)
        return 3
    manifest = build_manifest(wheel_dir)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"manifest written: {MANIFEST_PATH} "
          f"({manifest['wheel_count']} wheels, "
          f"{manifest['total_bytes']} bytes)")
    return 0


def cmd_verify(args) -> int:
    wheel_dir = Path(args.wheel_dir)
    if not MANIFEST_PATH.exists():
        print(f"FAIL: tracked manifest missing at {MANIFEST_PATH}",
              file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []

    # 1) requirements drift: the manifest was generated from specific
    #    bytes of the tracked requirements files; if they moved, the
    #    wheel set no longer provably covers the app.
    for name, want in manifest["requirements_files"].items():
        tracked = ROOT / name
        if not tracked.exists():
            problems.append(f"requirements file missing: {name}")
        elif sha256_of(tracked) != want:
            problems.append(f"requirements file changed since manifest: {name}")

    # 2) every manifest wheel present with byte-exact hash + size
    expected = {w["name"]: w for w in manifest["wheels"]}
    actual = {p.name: p for p in wheel_dir.glob("*.whl")}
    for fname in sorted(expected):
        p = actual.get(fname)
        if p is None:
            problems.append(f"missing wheel: {fname}")
            continue
        w = expected[fname]
        if p.stat().st_size != w["size"]:
            problems.append(f"size drift: {fname}")
        elif sha256_of(p) != w["sha256"]:
            problems.append(f"sha256 mismatch: {fname}")
    # 3) no silent extras (an unlisted wheel smuggles into the image)
    for fname in sorted(set(actual) - set(expected)):
        problems.append(f"unlisted extra wheel: {fname}")

    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 2
    print(f"wheelhouse OK: {len(expected)} wheels, "
          f"{manifest['total_bytes']} bytes at {wheel_dir}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["download", "verify"])
    ap.add_argument("--wheel-dir", default=str(DEFAULT_WHEEL_DIR))
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter whose pip performs the download")
    args = ap.parse_args(argv)
    if args.command == "download":
        return cmd_download(args)
    return cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())