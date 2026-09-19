# Design — model-binding

## Authority

The V1.8 `platform_binding.llm_inference` object pins AWS Bedrock Claude Sonnet
4.5. The deployed source region remains `ap-southeast-1`; the authoritative
inference profile is
`global.anthropic.claude-sonnet-4-5-20250929-v1:0`. AWS documents that profile
for global cross-Region inference and lists Singapore as a supported source
region. This specification does not revise the contract or infer live account
permission.

## Runtime rule

`planpilot.inference.bedrock_client` is the only provider I/O module. It owns the
region, profile, credential handling, request transport and usage accounting.
Runtime configuration may repeat the pinned values but may not silently select
another model. A mismatched model fails during client construction, before any
network or usage reservation.

Local development and tests retain the network-deny default. Tests use an
injected transport and assert the exact encoded request URL; status inspection
never probes AWS. Live permission, response quality and cost reconciliation are
deployment evidence, not offline implementation evidence.

## Delivery surfaces

The Python default, PowerShell launcher, `.env.example`, Docker image, Compose
service, package manifest and operator documentation carry the same profile and
source region. A regression scan rejects the former Nova identifier on those
surfaces. The compatibility intent validator under `agent/` contains no provider
client or SDK construction.
