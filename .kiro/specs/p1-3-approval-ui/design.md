# P1-3 Approval UI — Design-First implementation

The V1.8 contract and server-owned `ApprovalService` remain authoritative.
The web client neither computes approval requirements nor changes a decision
without a role-bound server call.

## Workflow

1. Generate three independently validated plan versions with `POST /schedule`.
   Choosing a profile loads that option's immutable `plan_content` and mutable
   lifecycle with authenticated `GET /plans`; the selected digest and version
   become the only binding for subsequent actions.
2. The first `POST /approval/request` atomically creates every required action.
   The UI renders each returned request, approver role, expiry, order impact,
   reason code and Approve / Reject controls. Planner and Manager tokens remain
   distinct; `POST /approval/decide` enforces the role on the server.
3. `GET /approval/status` rechecks expiry and invalidation and returns the
   contract's aggregate snapshot. `POST /publish` again verifies version,
   digest, complete approvals and Planner role. The UI displays the server's
   structured stale, expired or rejected error code.
4. `GET /audit/status` verifies the append-only chain and gives the UI its
   head digest and recent event IDs. The UI also shows the selected plan digest
   and any genuine Agent step traces.

## Scope

This is one local authenticated UI workflow. It is not a claim that AWS, a
deployed reverse proxy, MES/ERP integration, or formal EVAL cases have passed.
The browser never stores a Manager token beyond its current page lifetime.
