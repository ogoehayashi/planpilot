# Bedrock web integration

Use the regional Bedrock Converse HTTPS API with a server-only bearer key. The
source region is ap-southeast-1 and the enforced inference profile is Bedrock
Claude Sonnet 4.5
(`global.anthropic.claude-sonnet-4-5-20250929-v1:0`). Runtime defaults, startup
scripts, environment examples, deployment metadata and package metadata share
that one binding. A different model fails closed unless a future contract
revision changes the authoritative binding. Account access remains unverified
until the deployment check; local verification never calls AWS.
Credentials are read from an environment variable or an external file, never sent
to the browser. No live inference occurs during local verification.

This is the compact adapter integration, not an implementation of all eight V1.8
tool wire schemas or AWS managed Agents. The model selects a bounded intent:
generate, explain, or reply. The server runs the existing deterministic pipeline
for generation and persists candidates. Only KPI/risk summaries enter inference;
operations and raw workbook text stay on the non-LLM path. The model cannot write
factory data, choose arbitrary files, approve, or publish. Unsupported requests
are explained rather than silently changing production constraints.

Each request is stateless and can reference a specific current plan version.
Two model calls at most: intent, then explanation. Persist usage reservations and
actual usage in SQLite, including failed/unknown calls conservatively. Enforce a
daily token ceiling, input byte ceiling, output token ceiling, bounded responses,
network timeout and no automatic retry or redirect. Audit real execution steps
with request id and references; never record prompts, keys or provider bodies.

Frontend displays configured versus verified state honestly. The existing manual
generation button stays deterministic. Agent messages use /agent/chat and show
model errors without pretending that the local pipeline was a model response.
If explanation fails after saving a plan, return the saved plan with a warning.

Verification: mocked transport and HTTP API tests; genuine local solver output;
auth rejection, no-secret errors, usage limits, invalid model output, no raw
operations in prompts, explanation-only no writes, persisted plan on explanation
failure, exact model/profile binding, absence of an alternate default, and script
syntax. AWS availability remains deployment verification.
