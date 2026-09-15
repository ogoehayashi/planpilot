"""Run the local acceptance gates used before a demo or handoff."""
from pathlib import Path
import subprocess, sys
root=Path(__file__).resolve().parents[1]
commands=[[sys.executable,'-m','pytest','tests/unit','-q'],[sys.executable,'tools/check_closed_vocabularies.py']]
for command in commands:
    result=subprocess.run(command,cwd=root)
    if result.returncode: raise SystemExit(result.returncode)
print('LOCAL ACCEPTANCE | PASS')
