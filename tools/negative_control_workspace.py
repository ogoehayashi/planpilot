#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Negative control for validate_kiro_workspace.py.

190/0 passing proves nothing if the leak-detection patterns can never match.
This injects each defect class into a COPY of a steering file and requires the
corresponding check to fire. Same discipline as negative_control_v18.py.
"""
from __future__ import annotations
import re, shutil, subprocess, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
STEER = ROOT / ".kiro" / "steering"
TARGET = STEER / "product.md"
BACKUP = TARGET.with_suffix(".md.negctl")
VALIDATOR = ROOT / "tools" / "validate_kiro_workspace.py"

MUTATIONS = [
    ("unrendered subscript", lambda t: t.replace("# Product Overview", "# Product Overview\n\nsee {bc['hackathon_track']} for details")),
    ("unrendered chr(10)", lambda t: t.replace("# Product Overview", "# Product Overview\n{chr(10).join(items)}")),
    ("unrendered len()", lambda t: t.replace("# Product Overview", "# Product Overview\n{len(doc['tools'])} tools")),
    ("unrendered json.dumps", lambda t: t.replace("# Product Overview", "# Product Overview\n{json.dumps(x)}")),
    ("python repr leaked", lambda t: t.replace("# Product Overview", "# Product Overview\n<class 'collections.OrderedDict'>")),
    ("OrderedDict repr leaked", lambda t: t.replace("# Product Overview", "# Product Overview\nOrderedDict([('a', 1)])")),
    ("bare None in prose", lambda t: t.replace("# Product Overview", "# Product Overview\nvalue: None.")),
    ("NaN leaked", lambda t: t.replace("# Product Overview", "# Product Overview\nscore nan here")),
    ("empty braces", lambda t: t.replace("# Product Overview", "# Product Overview\n{} placeholder")),
    ("mis-transcription leaked", lambda t: t.replace("Production Planning", "Product Planning")),
    ("front matter removed", lambda t: re.sub(r"^---\n.*?\n---\n", "", t, count=1, flags=re.S)),
    ("inclusion mode removed", lambda t: t.replace("inclusion: always", "something: else")),
    ("truncated body", lambda t: t[:400]),
    ("secret material", lambda t: t.replace("# Product Overview", "# Product Overview\nAKIAIOSFODNN7EXAMPLE")),
]


def run_validator() -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(VALIDATOR)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    return p.returncode, p.stdout + p.stderr


shutil.copy2(TARGET, BACKUP)
try:
    rc, out = run_validator()
    if rc != 0:
        print("BASELINE NOT CLEAN — aborting")
        print(out[-2000:])
        raise SystemExit(1)
    base_ok = re.search(r"ok=(\d+) fail=(\d+)", out)
    print(f"baseline: validator clean ({base_ok.group(0)})\n")

    caught, escaped = [], []
    for name, mutate in MUTATIONS:
        original = BACKUP.read_text(encoding="utf-8")
        mutated = mutate(original)
        if mutated == original:
            print(f"  SKIPPED  | {name} — mutation produced no change (bad fixture)")
            escaped.append(name + " (fixture no-op)")
            continue
        TARGET.write_bytes(mutated.replace("\r\n", "\n").encode("utf-8"))
        rc, out = run_validator()
        summary = re.search(r"ok=(\d+) fail=(\d+)", out)
        first_fail = next((l[7:].strip() for l in out.splitlines() if l.startswith("  FAIL |")), "")
        if rc != 0:
            caught.append(name)
            print(f"  CAUGHT   | {name:<28} {summary.group(0) if summary else ''}")
            print(f"           | -> {first_fail[:96]}")
        else:
            escaped.append(name)
            print(f"  ESCAPED  | {name:<28} validator still returned 0")
finally:
    shutil.copy2(BACKUP, TARGET)
    BACKUP.unlink()

# confirm restoration
rc, out = run_validator()
restored = rc == 0
print(f"\nrestored: validator clean again = {restored}")

print(f"\nNEGATIVE CONTROL | caught={len(caught)} escaped={len(escaped)} of {len(MUTATIONS)}")
if escaped:
    print("VACUOUS CHECKS:", escaped)
if not restored:
    print("WARNING: target file was not restored cleanly")
    raise SystemExit(1)
raise SystemExit(1 if escaped else 0)
