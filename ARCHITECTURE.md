# OpenCoding Architecture

OpenCoding is a complete AI coding governance workflow for turning a beginner's idea into an agent-executed, verified, recoverable delivery. APG remains a compatibility identifier for historical scripts, tests, ledgers, receipts, snapshots, and change IDs.

## Module boundaries
Project types: AI coding governance workflow and agent orchestration layer
Operational dependencies: selected agent/provider integrations are optional and must be verified per host

## Public contracts
The public contracts cover requirement facts, recommendations, project documents, TaskPlans, previews, agent runs, verification, review, recovery, receipts, and delivery summaries. Offline preview remains one execution mode, not the product boundary。

## Invariants
- The canonical policy remains authoritative at `.governance/policy.toml`。
- Historical APG identifiers remain valid compatibility aliases。
- Agents are execution engines; OpenCoding owns decision, orchestration, authorization, evidence, and acceptance boundaries。
- Docker Desktop GUI, MCP, and a specific IDE plugin are optional integration choices, not product prerequisites。
