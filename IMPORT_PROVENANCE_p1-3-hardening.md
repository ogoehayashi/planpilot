# Import provenance — p1-3-hardening branch

- Imported: 2026-09-19, by Hermes agent, from teammate snapshot on local disk.
- Source tree: C:\Users\Lawrence\Desktop\Hackton\qin\outputs\PlanPilot-Mac-Migration-20260916-v2\PlanPilot-Team-20260915\PlanPilot
  (package "PlanPilot-Team-20260915", delivered via PlanPilot-Mac-Migration-20260916-v2;
  source has NO .git — this commit is the first git lineage for this tree.)
- Target: this repo (branch p1-3-hardening from team-runtime-integration @ 018674d).
- Contract verification: planpilot_agent_contract_v1.8.json SHA-256 = b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639
  (must equal b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639 — single authority).
- Merge policy: whole-tree import overwriting tracked+new files; docs/devlog/DEVELOPMENT_LOG.md
  three-way merged (kept round-10 review entry unique to lineage repo + teammate newer entries).
  Parent-branch draft src/planpilot/authority.py preserved under docs/reference/ for
  cross-check against teammate RuntimeAuthority (src/planpilot/authority.py).
- Known-defect register carried in (to be fixed by G1/G2, see champion review):
  P0-1 approval-window clock (planning_start 2026-09-14), non-contractual publisher
  (idempotency key dropped, no output-schema/audit_log_id binding), Windows UTF-8/negctl issues.
- Delivery hygiene (2026-09-19): PACKAGE_MANIFEST.json at repo root is a STALE
  2026-09-15 snapshot (143 files, Nova default model) and does NOT represent this
  branch (git currently tracks 266+ files). tools/build_team_package.py regenerates
  it inside every export and excludes the checked-in copy, so shipped packages are
  unaffected; the root copy stays only as historical evidence until G3 regenerates
  or retires it. Do not cite it as a description of the current tree.
