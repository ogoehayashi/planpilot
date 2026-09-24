#!/usr/bin/env python3
"""Phase 0 (task 0.4) — AST-based environment-variable read-point inventory.

Spec: .kiro/specs/production-gates/tasks.md 0.4 / design.md §3 F5.

Walks `src/`, `tests/`, `tools/` with the `ast` module (NOT regex) and
counts ACTUAL read CALLS of environment mappings:

  * `os.environ["X"]`, `os.environ.get("X", ...)`
  * bare `environ.get(...)` / `environ[...]` where `environ` is a
    module-global or a function PARAMETER (mapping-parameter reads,
    e.g. clock.py `def clock_from_env(environ, ...)`)
  * module-CONSTANT indirection: `environ.get(FAULT_ENV_VAR)` resolves
    FAULT_ENV_VAR to its module-level string value
    (publisher.py:337 -> PLANPILOT_PUBLISHER_FAULT at :363)

Excludes, by construction: comments, docstrings, string examples,
dict literals in fixtures (only mappings named `os.environ` /
`environ` / `env_snapshot` are followed), and any `.get` on dicts not
reaching one of those names.

Output: per-file table + totals, dual scope:
  scope=ALL        every read point found
  scope=PROD       the production-startup subset (vars that can affect
                   a `python tools/api_server.py` boot or the runtime
                   it constructs)

The 855a5c0 baseline snapshot (19 vars / 21 points, regex-measured in
round-4) is a COMPARISON BASELINE ONLY. Re-run this after G4 lands —
new ENV/backup/ready/recovery vars will appear; never hard-code the
totals as a permanent test invariant.
"""
import ast
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
TREES = ["src", "tests", "tools"]

# Production-startup scope: variables read on the path
# `python tools/api_server.py` -> Server -> Database/clock/publisher,
# plus contract loading. Anything else (test-only, TEMP helpers) is ALL-scope.
PROD_VARS = {
    "PLANPILOT_AUTH_SECRET", "PLANPILOT_ENV", "PLANPILOT_FACTORY_ROOT",
    "PLANPILOT_DB", "PLANPILOT_CONTRACT_PATH", "PLANPILOT_CONTRACT_SHA256",
    "PLANPILOT_CLOCK_MODE", "PLANPILOT_SCENARIO_NOW", "PLANPILOT_DATASET",
    "PLANPILOT_ALLOW_PUBLIC_SCENARIO", "PLANPILOT_PUBLISHER_FAULT",
    "PLANPILOT_BEDROCK_API_KEY", "PLANPILOT_BEDROCK_KEY_FILE",
    "PLANPILOT_BEDROCK_REGION", "PLANPILOT_BEDROCK_MODEL",
    "PLANPILOT_BEDROCK_DAILY_TOKENS", "PLANPILOT_FORBID_LLM_NETWORK",
    "PLANPILOT_BEDROCK_MAX_ATTEMPTS",
}

ENV_NAMES = {"environ", "env_snapshot"}  # mapping params/globals we follow


class Visitor(ast.NodeVisitor):
    def __init__(self, module):
        self.module = module
        self.reads = []          # (lineno, var-or-expr)
        self.consts = {}         # module-level NAME -> str value
        self.func_param_names = {}  # funcdef lineno -> set(param names)
        self.stack_params = []

    def visit_Module(self, node):
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
               and isinstance(stmt.targets[0], ast.Name) \
               and isinstance(stmt.value, ast.Constant) \
               and isinstance(stmt.value.value, str):
                self.consts[stmt.targets[0].id] = stmt.value.value
        self.generic_visit(node)

    # ---- name resolution helpers -------------------------------------
    def _is_environ_attr(self, node):
        """os.environ"""
        return (isinstance(node, ast.Attribute) and node.attr == "environ"
                and isinstance(node.value, ast.Name) and node.value.id == "os")

    def _is_environ_name(self, node):
        """bare `environ` / `env_snapshot` (param or global)"""
        return (isinstance(node, ast.Name) and node.id in ENV_NAMES) or \
               (node in ())  # placeholder; globals checked at module consts

    def _var_of(self, arg):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name) and arg.id in self.consts:
            return self.consts[arg.id]
        if isinstance(arg, ast.Name):
            return f"<unresolved:{arg.id}>"
        return "<expr>"

    # ---- read points ---------------------------------------------------
    def visit_Subscript(self, node):
        v = node.value
        if self._is_environ_attr(v) or self._is_environ_name(v) or \
           (isinstance(v, ast.Name) and v.id in self.consts):
            # environ["X"]
            self.reads.append((node.lineno, self._var_of(node.slice)))
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("get", "setdefault"):
            base = f.value
            followed = (self._is_environ_attr(base) or
                        self._is_environ_name(base) or
                        (isinstance(base, ast.Name) and
                         base.id in ("os") and False))
            if followed and node.args:
                self.reads.append((node.lineno, self._var_of(node.args[0])))
        self.generic_visit(node)


def scan_file(path: pathlib.Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    v = Visitor(path)
    v.visit(tree)
    # dedupe (same line + same var counted once)
    return sorted(set(v.reads))


def main():
    rows = []
    for tree in TREES:
        for py in sorted((ROOT / tree).rglob("*.py")):
            for lineno, var in scan_file(py):
                rows.append((str(py.relative_to(ROOT)), lineno, var))
    print("== ENV READ-POINT INVENTORY (AST; task 0.4) ==")
    print(f"repo HEAD: see git log line in this same log file")
    for f, ln, var in rows:
        scope = "PROD" if var in PROD_VARS else "ALL"
        print(f"{scope:4} {f}:{ln}  {var}")
    vars_all = {v for _, _, v in rows}
    pts_all = len(rows)
    prod = [(f, l, v) for f, l, v in rows if v in PROD_VARS]
    print(f"-- totals: ALL vars={len(vars_all)} points={pts_all}")
    print(f"-- totals: PROD vars={len({v for _,_,v in prod})} points={len(prod)}")
    print(f"-- unresolved constant refs: "
          f"{sorted({v for _,_,v in rows if v.startswith('<')}) or 'none'}")


if __name__ == "__main__":
    main()
