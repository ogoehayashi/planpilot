# Bedrock web offline verification

Timestamp: 2026-09-15T10:37:29.9562257+08:00

Scope: compact adapter intent/explanation integration, not full V1.8 tool-wire
certification and not live AWS validation.

Results:

- `python -m pytest tests/unit -q`: 516 passed in 8.34 seconds.
- `python tools/check_closed_vocabularies.py`: PASS.
- Real solver output from `data/factory_demo_v18.json` piped to
  `node tools/check_web_render.cjs`: rendering checks and Agent chat DOM
  simulation checks PASS.
- Windows PowerShell parser for `tools/start_local.ps1`: PASS.

The new 20 Python cases cover mock Converse transport, safe error handling,
credential-file parsing, network prohibition, run/day/input limits, real solver
generation, explanation failure preservation, invalid intent rejection,
non-generating replies, path confinement, authenticated local HTTP generation
and subsequent explanation. No Bedrock request was made. No live AWS credential
is embedded in test data or this report. Real browser behavior remains unverified.
