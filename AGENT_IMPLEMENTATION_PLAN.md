# PlanPilot Agent MVP

## Demonstration objective

PlanPilot is a production-planning agent for a manufacturing SME. A planner can describe an urgent order or disruption in natural language; the agent selects deterministic planning and validation tools, compares candidate plans, explains trade-offs, and pauses for explicit approval before publication.

## Agent boundary

The model interprets intent, chooses tools, sequences calls, compares validated outputs, and explains results. Tools own all scheduling, constraint checks, KPI calculations, persistence, and approval enforcement. The model never invents capacity, computes a schedule, approves overtime, changes a customer commitment, or publishes a plan.

## MVP vertical slice

1. Load a small structured factory dataset (orders, inventory, machines, workers, shifts, skills).
2. Accept a request such as “insert this urgent order and avoid overtime”.
3. Generate three deterministic alternatives: delivery priority, overtime minimisation, and stability priority.
4. Independently validate hard constraints and calculate KPIs.
5. Explain affected orders, causes, KPI deltas, and required approvals.
6. Obtain planner/manager approval through `ApprovalService`.
7. Publish only after all required approvals; record the decision trace.

## Build sequence

- Repair and seal the four open `PlanStore` audit findings recorded in `SESSION_STATE.md`.
- Implement deterministic scheduling, validation, KPI, and comparison tools.
- Add an inference client and an allowlisted tool registry with JSON schemas.
- Implement the agent orchestration loop and structured tool-call trace.
- Add a thin web/API demo and the disruption walkthrough.
- Execute runtime EVAL cases and capture deployment evidence.

## Definition of done for the hackathon demo

One request must visibly produce: agent interpretation, tool calls, three feasible/ infeasible alternatives with reasons, human approval requests, and a published plan after explicit confirmation. Every result must be reproducible from the same input and seed.
