"""G1.0.2 re-review P1: evidence packs must be self-verifying.

Review round 2 caught recorded SHA-256 values that did not match the
committed log blobs (two were CRLF pre-normalisation hashes despite
`.gitattributes text eol=lf`; one matched neither representation). This
walks every tests/evidence/**/EVIDENCE.json and re-hashes the referenced
log against the bytes git will check out (LF). If a log ships stale or a
hash is transcribed from the wrong representation, this fails.
"""
import hashlib
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _repo_root(pack: Path):
    """The git repo the pack physically lives in (the negative-control
    harness re-executes unit tests from a temp copy where our HEAD is
    unreachable and irrelevant)."""
    for cand in (pack.parent, *pack.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _git_blob(repo_root: Path, rel: str):
    r = subprocess.run(
        ["git", "cat-file", "blob", f"HEAD:{rel}"],
        cwd=repo_root, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _shipped_bytes(pack: Path, log_name: str, repo_root):
    """The bytes a reviewer checks: committed HEAD blob when the pack is
    in git history; otherwise the working file, which MUST already be LF
    (`.gitattributes text eol=lf` would silently rewrite the blob and
    stale any hash recorded from a CRLF working copy). The negative-
    control harness copies the repo without .git — there the working
    bytes ARE the shipped bytes, still checked."""
    path = pack.parent / log_name
    if repo_root is not None:
        rel = path.relative_to(repo_root).as_posix()
        blob = _git_blob(repo_root, rel)
        if blob is not None:
            return blob
    else:
        rel = str(path)
    data = path.read_bytes()
    assert b"\r\n" not in data, (
        f"{rel}: not in HEAD yet and working copy has CRLF — normalise "
        "before commit or the blob hash will not match")
    return data


def _artifacts(doc: dict):
    for run in doc.get("runs", []):
        yield run["log"], run["sha256"]
    for name, meta in (doc.get("raw_stdout", {})
                       .get("artifacts", {})).items():
        yield name, meta["sha256"]


def test_every_evidence_log_hash_matches_committed_blob():
    packs = sorted((REPO / "tests" / "evidence").glob("*/EVIDENCE.json"))
    assert packs, "evidence packs disappeared"
    checked = 0
    for pack in packs:
        doc = json.loads(pack.read_text(encoding="utf-8"))
        root = _repo_root(pack)
        for log_name, recorded in _artifacts(doc):
            blob = _shipped_bytes(pack, log_name, root)
            if blob is None:  # harness copy outside any git repo
                continue
            assert b"\r\n" not in blob, (
                f"{pack.parent.name}/{log_name}: shipped bytes have CRLF "
                "despite eol=lf — recorded hashes are representation-bound")
            actual = hashlib.sha256(blob).hexdigest()
            assert actual == recorded, (
                f"{pack.parent.name}/{log_name}: EVIDENCE.json says "
                f"{recorded[:12]}… but shipped blob is {actual[:12]}…")
            checked += 1
    assert checked >= 6, f"only {checked} evidence logs verified"
