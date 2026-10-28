# Engineering workflow

Use Python 3.12 and an isolated environment. Install `requirements-dev.txt`, select `ROCOTO_VERTICAL=bare` and keep fresh private state outside the checkout. [Local setup](../README.md#run-the-bare-workspace) opens the workspace; [OpenAI setup](OPENAI-SETUP.md) commissions paid execution.

## Design choices

| Experience to preserve | Engineering choice | Source |
|---|---|---|
| A conversation continues after reload or restart. | Save conversation, provider-session and original-request identities before execution; reconcile recovery against that request. | [Managed sessions](../harness/mechanics/managed_sessions.py), [runtime](../harness/runtime/managed_runtime.py) |
| A permission change takes effect during a tool call. | Check current admission around awaited reads, effects and result projection. | [Tool context](../harness/sdk/tool_families.py), [managed MCP](../harness/mechanics/managed_mcp.py) |
| A retry returns the saved effect. | Journal each provider action identity before dispatch and reuse its durable outcome. | [Tool dispatch](../harness/runtime/managed_turn.py) |
| A helper has a bounded job. | Declare its tools, deadline and allocation; reserve parent and role slots within one turn allowance. | [Roles and recipes](../harness/sdk/job_recipes.py), [policy](../harness/runtime/managed_policy.py) |
| An uncertain provider outcome remains visible. | Retain the reservation until attributable usage and quiescence evidence settle it. | [Spending policy](../harness/runtime/managed_policy.py), [accounting](../harness/mechanics/company_usage.py) |
| One business role can ship independently. | Install shared layers and one declared vertical; test the bare backend with retail absent. | [Layout](../harness/layout.py), [bare proof](../harness/tests/test_bare_deployment.py) |

## Offline checks

[The README command](../README.md#verify-locally) and [CI](../.github/workflows/offline.yml) run the same lean suite with local fake transports. The suite covers the SDK/layer boundary, portable bare artifact, managed protocol, sessions, turns, files, spending and explicit local allowance. CI actions are pinned to commits and receive read-only repository access.

Retail adds its own declared requirements. Original migration and cross-product suites also depend on their separate source closures. The [release record](../provenance/release-checks.json) names the two inherited broader-suite failures and records the original archive's Git-index skip. A separate fresh Git copy checks the selected files without changing the release candidate's history.

## Source release

Update [source provenance](../provenance/source-export.json), the reviewed inventory and file hashes when preparing an adapted export. Keep keys, runtime data, provider grants and original deployment receipts in their owning private records. The [historical index](HISTORICAL-RECORDS.md) identifies the original records referenced by retained guides. [Third-party notices](../THIRD_PARTY.md) travel with selected assets and built artifacts.
