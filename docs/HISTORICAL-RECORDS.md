# Historical records

The original Vitrina Harness keeps dated design notes, operator procedures and deployment receipts. Retained module guides refer to that operating history. This source beta carries the reusable implementation and selected technical designs; its own verification is in [release checks](../provenance/release-checks.json) and [local startup](../provenance/local-startup.json).

## Current guides

| Task | Public guide |
|---|---|
| Start and sign in | [Local workspace](../README.md#run-the-bare-workspace) |
| Configure your own provider and public MCP origin | [OpenAI setup](OPENAI-SETUP.md) |
| Follow current runtime, authority and approval paths | [Architecture](ARCHITECTURE.md), [MCP](../harness/mechanics/MCP.md) |
| Change source and verify it | [Engineering](ENGINEERING.md), [SDK](../harness/sdk/README.md) |
| Review optional migration contracts | [Retail service design](../harness/design/SERVICE-BOUNDARY.md), [data contracts](../harness/design/POSTGRES-SCHEMA.md), [MCP writes design](../harness/design/MCP-WRITES.md) |

## Original source paths

Paths below identify private originals relative to the Vitrina Harness source root and extraction commit recorded in [source provenance](../provenance/source-export.json). They preserve the citation's identity. A historical receipt describes the original installation; a new installation establishes its own accounts, grants, target and proof. Public MCP tool names come from the retained [registry](../harness/sdk/tool_families.py), rather than the count in an older receipt.

| Original source path | Retained guide citing it |
|---|---|
| <a id="record-2"></a>`harness/HARNESS.md` | `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-3"></a>`harness/deploy/MAINTAINED-COMMISSIONING.md` | `backend/realtime/README.md` |
| <a id="record-4"></a>`harness/deploy/MANAGED-EXECUTOR.md` | `harness/runtime/README.md` |
| <a id="record-5"></a>`harness/deploy/MIGRATION.md` | `harness/deploy/migration/README.md` |
| <a id="record-6"></a>`harness/deploy/connections/provider-profiles/README.md` | `harness/mechanics/CONNECTIONS.md` |
| <a id="record-7"></a>`harness/deploy/mcp/PUBLIC-PROOF-2026-10-05.json` | `harness/mechanics/MCP.md` |
| <a id="record-8"></a>`harness/deploy/native-executor/CUSTOM-RETENTION-INSTALLED-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-9"></a>`harness/deploy/native-executor/TRACKER-SCRIPT-PROOF-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-10"></a>`harness/deploy/quality/BARE-COMMISSIONING-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-11"></a>`harness/deploy/quality/HARNESS-EXTRACTION-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-12"></a>`harness/deploy/quality/HARNESS-LANDING-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-13"></a>`harness/deploy/quality/HARNESS-LAYOUT-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-14"></a>`harness/deploy/quality/MANAGED-PROTOCOL-2026-10-11.json` | `harness/mechanics/README.md` |
| <a id="record-15"></a>`harness/deploy/quality/MANAGED-ROLLOUT-2026-10-13.json` | `harness/mechanics/README.md` |
| <a id="record-16"></a>`harness/deploy/sftp/README.md` | `harness/verticals/retail/DATA.md` |
| <a id="record-17"></a>`harness/deploy/source-derive/README.md` | `harness/verticals/retail/DATA.md` |
| <a id="record-18"></a>`harness/deploy/source-intake/demo/README.md` | `harness/verticals/retail/DATA.md` |
| <a id="record-19"></a>`harness/deploy/source-media/README.md` | `harness/verticals/retail/DATA.md` |
| <a id="record-21"></a>`harness/records/2026-10-04-runtime-options.md` | `harness/design/SERVICE-BOUNDARY.md`, `harness/runtime/README.md` |
| <a id="record-22"></a>`harness/records/2026-10-09-backlog.md` | `backend/realtime/README.md`, `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-23"></a>`harness/records/2026-10-09-internal-sdk.md` | `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-24"></a>`harness/records/2026-10-11-consolidation/CHANNELS.md` | `harness/design/MCP-WRITES.md`, `harness/design/POSTGRES-SCHEMA.md`, `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-25"></a>`harness/records/2026-10-11-consolidation/DEMO-SEED.md` | `harness/verticals/retail/DATA.md` |
| <a id="record-26"></a>`harness/records/2026-10-11-consolidation/HARNESS.md` | `harness/mechanics/README.md` |
| <a id="record-27"></a>`harness/records/2026-10-11-consolidation/OBJECTS.md` | `harness/design/MCP-WRITES.md` |
| <a id="record-28"></a>`harness/records/2026-10-11-consolidation/PRODUCT.md` | `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-29"></a>`harness/records/2026-10-11-consolidation/PUBLIC-MCP.md` | `harness/design/MCP-WRITES.md` |
| <a id="record-30"></a>`harness/records/2026-10-11-consolidation/RUNTIME.md` | `harness/design/SERVICE-BOUNDARY.md` |
| <a id="record-31"></a>`harness/records/2026-10-11-consolidation/TRANSFER-BUDGET.md` | `harness/design/MCP-WRITES.md` |
| <a id="record-32"></a>`harness/records/2026-10-11-handover.md` | `harness/mechanics/README.md` |
| <a id="record-33"></a>`harness/records/2026-10-delivery-ledger.md` | `harness/design/MCP-WRITES.md`, `harness/design/SERVICE-BOUNDARY.md` |
