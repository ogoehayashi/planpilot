"""PlanPilot AI — production planning agent for LionCity Precision Pte. Ltd.

Architecture (contract `runtime_principle`): the LLM orchestrates — it interprets
intent, selects tools, compares validated results, explains trade-offs and
requests approval. All scheduling, constraint enforcement, KPI computation and
plan validation are performed by deterministic tools. The LLM never computes.

Nothing in this package may call an LLM for arithmetic, scheduling, constraint
checking, KPI computation or plan validation. See
`.kiro/steering/contract-authority.md`.
"""

__version__ = "0.1.0"

# Contract this implementation is written against. Tests assert the on-disk
# contract hashes to this value, so a swapped contract cannot pass silently.
CONTRACT_SHA256 = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
CONTRACT_SCHEMA_VERSION = "1.8.0"
