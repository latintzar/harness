---
name: agent-workspace-setup
description: Set up this Agent workspace source release, verify its bare vertical locally, and guide an explicitly requested managed-agent installation.
---

# Set up Agent workspace

Work from the repository containing this skill. Read [README](../../../README.md), [dependencies](../../../DEPENDENCIES.md) and [architecture](../../../docs/ARCHITECTURE.md). Use Python 3.12, `requirements-dev.txt` and `ROCOTO_VERTICAL=bare` for the minimal workspace.

## Local workspace

Follow the README's local startup commands with fresh owner-selected private state outside the checkout. Generate the owner code into that private directory and hand the operator its path. Keep the code and credentials in private input. Start the loopback server, check `/health`, open the page and verify owner sign-in. Report local startup separately from provider execution.

Run the exact lean offline suite under “Verify locally” in the README. It uses fake transports. The [release record](../../../provenance/release-checks.json) preserves the two inherited failures in the broader original suite. Use [engineering](../../../docs/ENGINEERING.md) for source changes and the [SDK](../../../harness/sdk/README.md) for a new vertical.

## Managed agent execution

When the operator requests live execution, follow [OpenAI setup](../../../docs/OPENAI-SETUP.md). Confirm the operator's project and billing choice, collect its key through private environment input, configure its reachable HTTPS MCP origin and authorize its chosen local allowance through `tools/authorize_local_budget.py`. The allowance is an internal authorization record; actual provider billing uses the operator's account.

Review the helper's amount, expiry and billing acknowledgment with the operator before recording a new allowance. Start a billable proof only within their requested scope. Check the saved result, files, reload, Stop and accounting receipts. Report uncertainty and retained holds accurately.

The bare local page, API access, funded admission, hosted MCP reachability and an actual managed turn are separate checks. Use the existing scope, approval and accounting paths. Original deployment records describe their original installation; [current public guides and historical source paths](../../../docs/HISTORICAL-RECORDS.md) identify that boundary.
