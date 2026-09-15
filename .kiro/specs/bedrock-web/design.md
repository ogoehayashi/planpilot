# Bedrock web integration

Use the regional Bedrock Converse HTTPS API with a server-only bearer key. Default
region is ap-southeast-1 and default model is Amazon Nova Pro
(amazon.nova-pro-v1:0), selected following the user's explicit request to change
models after Anthropic geographic access failed in both the API and Playground.
The original competition contract still pins Sonnet and is unchanged. This
user-authorized configuration deviation must not be described as satisfying that
model requirement. Nova access remains unverified in the user's AWS account.
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
failure, and script syntax. AWS availability remains deployment verification.
