# OpenAI agent setup

The backend and page run on your machine. Agents run through OpenAI's documented managed Agents API with OpenAI-hosted execution. The Harness supplies durable workspace state, tools, permissions, recipes, connections and the browser interface.

## Your project key

Create an application API key in **your** OpenAI Platform project with `api.agents.read`, `api.agents.write` and `api.responses.write` scopes. The [official Agents API quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart), checked 28 October 2026, documents these permissions and the `OpenAI-Beta: agents=v1` header. The existing transport sets that header and sends credentials only to `api.openai.com`.

Supply the key to the server through private environment configuration. For an interactive Bash session, hidden input keeps it out of command history:

```bash
read -r -s -p 'OpenAI project key: ' ROCOTO_OPENAI_API_KEY
printf '\n'
export ROCOTO_OPENAI_API_KEY
export ROCOTO_AGENT_AUTH_MODE=openai_api
export ROCOTO_MANAGED_SPENDING_MODE=enforced
export ROCOTO_OPERATOR_ID=workspace-owner
```

The source retains `ROCOTO_` environment names and the managed MCP namespace for compatibility. Set provider billing limits in your own Platform project. The adapter currently selects `gpt-6-astra`; its versioned pricing assumptions live in `harness/runtime/openai_gateway.py` and require review against your provider's current pricing when commissioning execution.

## Explicit workspace allowance

Paid execution requires both your provider credential and a company-bound funded allowance. The page does not create funds. For a fresh local development workspace, explicitly authorize an internal operator allowance through the existing ledger:

```bash
export ROCOTO_VERTICAL=bare
export AGENT_WORKSPACE_STATE="$HOME/.local/state/agent-workspace"
mkdir -p -m 700 "$AGENT_WORKSPACE_STATE"
.venv/bin/python tools/authorize_local_budget.py \
  --state "$AGENT_WORKSPACE_STATE" --amount-usd 2 --days 7 \
  --acknowledge-provider-billing
```

This records a $2 development allowance for the existing managed model. It makes zero API requests and records an authorization rather than a customer deposit or provider balance. Existing reconciliation, pool scope, expiry and monthly limits still apply. The tool refuses state inside the source checkout. Production/customer funding uses the existing host-only verified-funding interfaces.

The managed policy reserves a maximum $1 allocation per turn shared by the parent and declared roles. Default `enforced` mode requests the provider's session spending control. If your project rejects that control, execution reports the failure; it remains unverified until commissioned. An operator may expressly select `monitored` mode, whose estimated-cost/deadline cancellation is a spending target with possible overshoot. The historical project's monitored authorization grants nothing to a new installation; select the mode for your own account.

## Public MCP callback

The managed hosted agent must reach the workspace's HTTPS MCP endpoint. Set `ROCOTO_PUBLIC_ORIGIN` to your own externally reachable origin and reverse-proxy it to the loopback server, preserving streaming requests. The application mounts `/runtime/mcp` with short-lived, scoped, revocable credentials for managed children. Localhost is useful for running the page and offline development; it is not reachable from a hosted agent.

Use your own origin, TLS, workspace membership and private state. Existing hostnames, provider accounts, raw footage and budgets are deliberately outside this release. Connected providers and optional messaging/custom-compute services require separate operator configuration.

## First live proof

After commissioning the origin, key, owner access and allowance, start the bare server and use Organization for one short read task. Check the explicit runtime status, files, reload, Stop and accounting receipts. Provider execution is billable. This source candidate was verified with local/fake transports; preparing it performed zero provider/model/GPU calls.
