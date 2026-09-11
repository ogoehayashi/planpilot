#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the generated Kiro workspace.

Generation by f-string is convenient and fragile: an unresolved expression, a
mis-escaped brace, or a None leaking into prose produces a steering file that
still LOOKS like documentation. This checks the output mechanically.
"""
from __future__ import annotations
import json, re, sys, io, hashlib, subprocess
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
STEER = ROOT / ".kiro" / "steering"
HOOKS = ROOT / ".kiro" / "hooks"
CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"
doc = json.loads(CONTRACT.read_bytes().decode("utf-8"))

fails, oks = [], []
def ck(cond, label):
    (oks if cond else fails).append(label)
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}")

files = sorted(STEER.glob("*.md")) + [ROOT / "AGENTS.md", ROOT / ".kiro" / "specs" / "README.md"]

print("=== A. every expected file exists and is non-trivial ===")
expected = ["product.md", "tech.md", "structure.md", "contract-authority.md",
            "guardrails.md", "engine-rules.md", "observability.md"]
for name in expected:
    p = STEER / name
    ck(p.exists() and p.stat().st_size > 1500, f"{name} exists and >1.5 KB ({p.stat().st_size if p.exists() else 0} B)")
ck((ROOT / "AGENTS.md").exists(), "AGENTS.md exists")
ck((ROOT / ".kiro" / "specs" / "README.md").exists(), "specs/README.md exists")

print("\n=== B. front matter is valid YAML and inclusion mode is correct ===")
FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
MODES = {}
for p in sorted(STEER.glob("*.md")):
    text = p.read_text(encoding="utf-8")
    m = FM.match(text)
    ck(m is not None, f"{p.name}: has a front matter block at the very top")
    if not m:
        continue
    fm = m.group(1)
    try:
        import yaml
        meta = yaml.safe_load(fm)
        parsed = True
    except ImportError:
        # minimal parse: key: value lines only
        meta = {}
        for line in fm.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        parsed = True
    ck(parsed and "inclusion" in meta, f"{p.name}: declares an inclusion mode")
    MODES[p.name] = meta.get("inclusion")
    body = text[m.end():]
    ck(len(body.strip()) > 500, f"{p.name}: has a substantive body after front matter")
print(f"     inclusion modes: {MODES}")
ck(MODES.get("engine-rules.md") == "fileMatch", "engine-rules.md is fileMatch (context-heavy, not always-on)")
ck(MODES.get("observability.md") == "fileMatch", "observability.md is fileMatch")
always = [k for k, v in MODES.items() if v == "always"]
ck(len(always) == 5, f"exactly 5 always-on files (actual {len(always)}): {always}")

print("\n=== C. no unresolved f-string artefacts leaked into the prose ===")
LEAK = [
    (r"\{[a-z_]+\[", "unrendered subscript expression"),
    (r"\{chr\(10\)", "unrendered chr(10)"),
    (r"\{len\(", "unrendered len()"),
    (r"\{str\(", "unrendered str()"),
    (r"\{json\.dumps", "unrendered json.dumps"),
    (r"\{', '\.join", "unrendered join"),
    (r"\bNone\b(?! )", "bare None in prose"),
    (r"\bnan\b", "NaN leaked"),
    (r"\{\}", "empty braces"),
    (r"<class '", "python repr leaked"),
    (r"OrderedDict\(", "OrderedDict repr leaked"),
]
for p in files:
    text = p.read_text(encoding="utf-8")
    for pat, label in LEAK:
        hits = re.findall(pat, text)
        ck(not hits, f"{p.name}: no {label}" + (f" -> {hits[:3]}" if hits else ""))

print("\n=== D. escaped braces rendered correctly ===")
ca = (STEER / "contract-authority.md").read_text(encoding="utf-8")
ck(r"^EVT-\d{3}$" in ca, "the event_id regex rendered as ^EVT-\\d{3}$ (not ^EVT-\\d{{3}}$)")
ck("{{3}}" not in ca, "no doubled braces survived")

print("\n=== E. key numbers in the generated text match the contract ===")
alltext = "\n".join(p.read_text(encoding="utf-8") for p in files)
checks = [
    (f"EVAL-001..{len(doc['acceptance_tests']):03d}", f"all {len(doc['acceptance_tests'])} EVAL cases referenced"),
    ("HC-013", "last hard constraint referenced"),
    ("SEARCH_ESCALATION_EXHAUSTED", "the V1.7 final error code referenced"),
    ("MATERIAL_RESERVATION_MISMATCH", "the newest validation code referenced"),
    ("Claude Sonnet 4.5", "model pinned in generated text"),
    ("AWS Lightsail", "host pinned in generated text"),
    ("AWS Bedrock", "inference service pinned"),
    ("ortools==9.11.4210", "solver pin present"),
    ("jsonschema[format]==4.23.0", "jsonschema pin present"),
    ("Production Planning", "official track title present"),
    ("Product Planning", None),  # must NOT appear - see below
    ("2026-09-28T09:00:00+08:00", "shortlisting deadline present"),
    ("2026-10-10T08:30:00+08:00", "finale date present"),
    ("TO_BE_RECORDED_BEFORE_SUBMISSION", "team code placeholder present"),
    ("decision_trace_record", "trace record referenced"),
    ("Design-First", "spec workflow guidance present"),
]
for token, label in checks:
    if label is None:
        ck(token not in alltext, "the mis-transcription 'Product Planning' did NOT leak into steering")
    else:
        ck(token in alltext, label)

# every closed vocabulary must actually be enumerated in contract-authority.md
print("\n=== F. closed vocabularies are fully enumerated for the agent ===")
vocabs = {
    "hard constraints": [h["id"] for h in doc["hard_constraints"]],
    "workflow states": doc["workflow"]["states"],
    "error codes": doc["$defs"]["tool_error"]["properties"]["error_code"]["enum"],
    "plan profiles": [k for k, v in doc["plan_profiles"].items() if isinstance(v, dict)],
    "KPIs": doc["$defs"]["kpis"]["required"],
    "decision reason codes": doc["$defs"]["decision_reason_code"]["enum"],
}
for label, items in vocabs.items():
    missing = [i for i in items if i not in ca]
    ck(not missing, f"all {len(items)} {label} enumerated" + (f" (missing: {missing})" if missing else ""))
nonhc = [c for c in doc["$defs"]["validation_issue_code"]["enum"] if not c.startswith("HC-")]
missing = [c for c in nonhc if c not in ca]
ck(not missing, f"all {len(nonhc)} non-HC validation codes enumerated" + (f" (missing: {missing[:5]})" if missing else ""))
events = sorted({e for v in doc["data_source"]["official_disruption_coverage"].values() for e in v})
missing = [e for e in events if e not in ca]
ck(not missing, f"all {len(events)} events enumerated" + (f" (missing: {missing})" if missing else ""))
sheets = list(doc["data_source"]["required_sheets"])
missing = [s for s in sheets if s not in ca]
ck(not missing, f"all {len(sheets)} dataset sheets enumerated" + (f" (missing: {missing})" if missing else ""))

print("\n=== G. hooks are valid JSON with real trigger names ===")
VALID_TRIGGERS = {"PostFileSave", "PostFileCreate", "PostFileDelete", "PreToolUse",
                  "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop",
                  "PreTaskExec", "PostTaskExec"}
for p in sorted(HOOKS.glob("*.json")):
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        ck(True, f"{p.name} parses as JSON")
    except json.JSONDecodeError as e:
        ck(False, f"{p.name} parses as JSON ({e})")
        continue
    ck(cfg.get("version") == "v1", f"{p.name} declares version v1")
    for h in cfg.get("hooks", []):
        ck(h.get("trigger") in VALID_TRIGGERS, f"{p.name}: trigger '{h.get('trigger')}' is a real Kiro trigger")
        ck(h.get("action", {}).get("type") in ("command", "prompt"), f"{p.name}: action type is command or prompt")
        ck(bool(h.get("name")), f"{p.name}: hook is named")
        if h["action"]["type"] == "command":
            ck(bool(h["action"].get("command")), f"{p.name}: command is non-empty")

print("\n=== H. no credentials or secrets present ===")
# Runs BEFORE the idempotency section on purpose: that section re-runs the
# generator, which restores any mutated file and would silently wipe an
# injected secret, making the scan below pass for the wrong reason.
for p in files + [ROOT / ".env.example", ROOT / ".gitignore"]:
    text = p.read_text(encoding="utf-8")
    ck(not re.search(r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|-----BEGIN)", text),
       f"{p.name} contains no secret material")
gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
ck(".env" in gi, ".gitignore excludes .env")
env = (ROOT / ".env.example").read_text(encoding="utf-8")
ck("PLANPILOT_BEDROCK_API_KEY=\n" in env or env.rstrip().endswith("=") or "PLANPILOT_BEDROCK_API_KEY=" in env,
   ".env.example has an EMPTY key placeholder")
ck(re.search(r"PLANPILOT_BEDROCK_API_KEY=\s*$", env, re.M) is not None,
   ".env.example key value is blank, not a fabricated credential")
ck("PLANPILOT_FORBID_LLM_NETWORK=1" in env, ".env.example defaults to forbidding LLM network in dev")

print("\n=== I. generation is idempotent (re-run produces identical bytes) ===")
before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
subprocess.run([sys.executable, str(ROOT / "tools" / "generate_kiro_workspace.py")],
               capture_output=True, cwd=ROOT)
after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
diff = [p.name for p in files if before[p] != after[p]]
ck(not diff, f"re-running the generator changes nothing (changed: {diff})")

print(f"\nWORKSPACE VALIDATION | ok={len(oks)} fail={len(fails)}")
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
