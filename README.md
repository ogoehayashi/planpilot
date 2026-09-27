# PlanPilot

**A production-planning agent with an explicit permission boundary.**

> **The LLM never computes.** Every number this system produces — every schedule,
> every KPI, every constraint check, every approval state — comes from deterministic
> code. The language model interprets intent, selects tools, compares **validated**
> results, explains trade-offs and requests human approval. It has no arithmetic
> authority, no write authority, and no ability to approve its own work.

**Team Fa1c0ns_** · Team Code **ZQHCZEKA** · NUS-ISS *"Show Me Your Agents"* Hackathon · Public Category

**Live deployment:** <http://54.254.231.101/> — AWS Lightsail (Singapore, `ap-southeast-1`), Docker, port 80
**Model:** AWS Bedrock · Claude Sonnet 4.5 (`global.anthropic.claude-sonnet-4-5-20250929-v1:0`)

---

## The problem

> *Production planners in manufacturing SMEs coordinate customer orders, available
> inventory, machine capacity and workforce schedules. Planning is often performed
> manually using spreadsheets and historical experience.*
> — official problem statement

Manual planning is slow, unauditable, and irreproducible: when a planner leaves, the
reasoning leaves with them. But an LLM that *invents* a schedule is worse than a
spreadsheet, because it produces confident numbers you cannot check.

PlanPilot's answer is an architectural boundary rather than a prompt convention.

---

## The boundary, in two lists

The contract splits responsibilities into two explicit, non-overlapping roles:

| The language model may | The deterministic tools must |
|---|---|
| interpret user intent | calculate the schedule |
| select tools | enforce constraints |
| compare **validated** results | calculate KPIs |
| explain trade-offs | validate the plan |
| request human approval | author **all** structural plan fields |

**Zero overlap.** The model can *ask* for a schedule; it cannot *be* one. This is not a
policy the model is asked to respect — it is the set of tools it is able to reach.

---

## Architecture

```
HTTP API ──▶ RuntimeAuthority ──▶ PlanStore (immutable, versioned, digest-bound)
                   │                      │
                   │                      └──▶ audit hash chain (append-only)
                   ├──▶ CP-SAT scheduling engine (deterministic, seeded)
                   ├──▶ Independent validator  (recomputes everything, fails closed)
                   └──▶ ApprovalService        (role-checked, server-owned expiry)
                                   ▲
        AWS Bedrock ───────────────┘ coordinates and explains only
```

Three components carry the weight:

- **The deterministic solver** — CP-SAT, driven through five lexicographic stages,
  pinned to a **single search worker with a fixed seed of 42** and a deterministic time
  budget. A plan you cannot reproduce is a plan you cannot approve.
- **`RuntimeAuthority`** — the only path that writes a plan. It commits the audited plan
  store and the approval service **atomically**, so an approved plan without an audit
  trail is not a reachable state.
- **The independent validator** — re-computes the plan digest, material balance,
  **thirteen hard constraints (HC-001 … HC-013)** and every KPI **from scratch**. If its
  numbers disagree with the solver's, it **fails closed**. *The solver is not trusted to
  grade its own homework.*

### Why the service level is guaranteed before cost is negotiated

The solver minimises, in this order, locking each stage's optimum before moving on:

```python
stages = (unscheduled_count, sum(late_flags), sum(lateness), sum(secondary_terms), tier3)
```

`unscheduled work` → `number of late orders` → `minutes of lateness` →
`secondary-skill assignments` → **only then** the profile-weighted cost trade-off
(`tier3`).

The profile weights exist **only in the fifth stage**. The first four are solved and
frozen first, so no profile can buy itself a cheaper plan by quietly missing a promise
date. This is why all three shipped profiles hit 8/8 on time on the demo dataset.

### On `FEASIBLE` (an honest note we surface on screen)

The deployed instance reports **`CP-SAT · FEASIBLE`**. Precisely:

- The first four stages must each be **proven optimal** — if any of them returned merely
  feasible, the engine would **refuse the answer** and fall back to a documented priority
  rule. So seeing `CP-SAT` at all means the **service level is proven optimal**.
- `FEASIBLE` describes only the **fifth** stage: the profile-weighted cost trade-off was
  solved **inside its deterministic time budget** rather than proven optimal to the last
  minute.

So the claim is *three independently validated, reproducible plans with a proven service
level and a budgeted cost trade-off* — **not** three proven optima.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the Bedrock key; never commit .env

python tools/api_server.py    # or: docker compose up
python -m pytest tests/unit   # deterministic, makes NO model calls
```

Local development and the test suite make **no Bedrock calls by design**
(`PLANPILOT_FORBID_LLM_NETWORK=1`): the whole shortlisting round runs under a documented
cost ceiling, and it is enforced in the test suite rather than trusted to discipline.

**935 test cases (375 test functions) pass.** The unit suite is deterministic: fixed
seed, single search worker, no dict-order or wall-clock dependence.

---

## Honest limitations

These are stated here for the same reason they are stated on screen — a system you can
check is a system whose limits you can also see.

1. **The runtime evaluation suite is authored but not executed.** EVAL-001 … EVAL-030
   are in the contract; the current state is **0 pass / 0 fail / 30 blocked**, because
   each case needs its own dedicated run with timestamped evidence. An earlier internal
   note claimed 30/30 — **that claim was wrong and was retracted in writing.** See
   `EVAL_EVIDENCE_RETRACTION_REPORT_20260916.md`.
2. **`FEASIBLE` is not `OPTIMAL`.** Service level is proven; the cost trade-off is
   verified and reproducible but budgeted. See the section above.
3. **Material reservation is a deliberate single pass.** The contract explicitly
   prohibits claiming material allocation is optimal. It is a bounded algorithm, not a
   second optimisation.
4. **A defect we found in our own demo, and fixed.** An earlier dataset produced three
   *identical* plans. Root cause: that dataset declared zero overtime capacity and a
   saturated changeover reference, leaving the fifth stage with no free variable to
   trade. The dataset was rebuilt so the trade-off is real, the deterministic fallback
   was fixed to honour profile weights, and a **regression test** now fails if the
   profiles ever collapse into one plan again.
5. **The agent badge reports configuration, not a completed call.** The status line
   literally reads `AWS connectivity not yet verified`. The only evidence that a model
   ran is a `model_*` trace step with a real elapsed-millisecond value.

---

## Repository map

| Path | Contents |
|---|---|
| `contract/planpilot_agent_contract_v1.8.json` | **The authoritative specification.** Finished, not a draft. |
| `src/planpilot/domain/planning.py` | The CP-SAT lexicographic solver |
| `src/planpilot/independent_validator.py` | Re-computes digest, constraints and KPIs; fails closed |
| `src/planpilot/authority.py` | `RuntimeAuthority` — the only plan-writing path |
| `src/planpilot/approval/` | Approval policy, role routing, version+digest binding |
| `src/planpilot/audit.py` | Append-only hash-chained audit log |
| `src/planpilot/agent/` | Intent interpretation, tool orchestration, explanation |
| `src/planpilot/tools/`, `api_server.py` | HTTP surface: `/schedule`, `/plans`, `/approval/*`, `/publish`, `/agent/chat` |
| `tools/web/` | The operations console (single-file UI, i18n) |
| `tests/unit/` | 935 deterministic unit tests — no network, no model calls |
| `AGENTS.md` | Machine-readable rules for contributors and coding agents |

---

## Documentation

- `OPERATIONS_MANUAL.md` — operating the deployed instance
- `HOW_TO_READ_THE_SCHEDULE.md` — reading the Gantt, KPIs and risk panel
- `TECHNICAL_DOCUMENT` / `PROBLEM_STATEMENT` / `BUSINESS_PROPOSAL` — the submission write-up
- `docs/devlog/DEVELOPMENT_LOG.md` — the build history, including the defects we found

---

## Design rules this repository holds itself to

- **The contract wins.** It is a finished specification. No invented codes, tools,
  parameters, KPIs, profiles, states or event ids.
- **The LLM never computes.**
- **Determinism or it did not happen.** Fixed seed, single search worker, no
  dict-order or wall-clock dependence.
- **Never hide bad news.** Late orders, unscheduled operations, hard violations,
  required approvals and solver infeasibility are always surfaced — including when the
  answer is "none".
- **Nothing is "done" without timestamped runtime evidence.**
- **Never commit credentials.**
