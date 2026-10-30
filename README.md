# Harness

A Codex-based agent harness with generative UI

Harness is a Codex-based, vertical-adaptable agent harness with generative UI. A vertical supplies its domain, tools and interfaces; agents work through conversations, scoped files, jobs, recipes, approvals and spend accounting.

[Website](https://atelierneyra.com/harness) · [Open app](https://preview.atelierneyra.com/apps/agent-workspace/) · [Documentation](https://docs.atelierneyra.com/agent-workspace/overview/)

![Harness](https://atelierneyra.com/assets/product-shots-20261030/agent-workspace.jpg)

Build a business role as a **vertical** over the existing workspace backend and browser interface. The minimal **bare** vertical includes an Organization conversation, one general Agent class, a setup recipe and a bounded workspace-reader role. OpenAI's managed Agents API runs the agent loop and hosted execution; this repository owns the workspace framework and its vertical SDK.

Originally developed as the **Vitrina Harness**. This source beta includes the existing implementation, portable setup and retained third-party notices.

## How it works

A business agent needs a place to work: persistent context, scoped tools, credentials, approvals and interfaces for its results. Rebuilding that framework for every vertical makes the domain work harder to maintain.

| Feature | What it does |
| --- | --- |
| Vertical SDK | Declare roles, tools, recipes, data classes and optional displays for a business. |
| Generative UI | Use the display SDK to render domain-specific views from an agent’s work. |
| Persistent work | Keep conversations, scoped files and jobs with the workspace. |
| Supervision | Connect approvals, identity and spend accounting to agent actions. |
| Runtime adapters | The current source includes managed OpenAI execution; the project retains its Codex-based lineage and architecture. |

The source includes the original harness and SDK. The hosted bare workspace is a read-only visitor example with an authored display fixture; connected agent execution uses the operator’s own provider setup. Its guest entry opens an authored example conversation about the display SDK. Build a vertical with one role, one useful tool and one display, then commission its managed runtime. [Product guide](docs/PROJECT.md).

## Run the bare workspace

Python 3.12:

```bash
git clone https://github.com/latintzar/harness.git
cd harness
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
export ROCOTO_VERTICAL=bare
export AGENT_WORKSPACE_STATE="$HOME/.local/state/agent-workspace"
mkdir -p -m 700 "$AGENT_WORKSPACE_STATE"

# Save a one-use owner code to a private file.
.venv/bin/python harness/run.py access \
  --state "$AGENT_WORKSPACE_STATE" --owner \
  --principal-id owner --display-name Owner \
  --output "$AGENT_WORKSPACE_STATE/owner-code.txt"

.venv/bin/python harness/run.py server \
  --state "$AGENT_WORKSPACE_STATE" --port 8780
```

Open **http://127.0.0.1:8780**, read your local `owner-code.txt` and enter the one-use code. The code is valid for 24 hours. The page and durable workspace run locally; agent execution requires your own API project, explicit allowance and reachable managed MCP origin. [OpenAI setup](docs/OPENAI-SETUP.md) explains the exact steps and existing spending policy.

Keep private state outside the checkout. The `ROCOTO_` environment names and namespace remain compatible with the original implementation.

## Guides

| Start here | Result |
|---|---|
| [Run the bare workspace](#run-the-bare-workspace) | Open the local page and sign in as its owner. |
| [Native local workspace](docs/LOCAL-WORKSPACE.md) | Inspect the actual interface and included source files with local, provider-off state. |
| [OpenAI setup](docs/OPENAI-SETUP.md) | Connect your own project, allowance and public MCP origin. |
| [Architecture](docs/ARCHITECTURE.md) | Follow a request through the provider, tools and approvals. |
| [Engineering](docs/ENGINEERING.md) | Understand the design choices and run the offline checks. |
| [Vertical SDK](harness/sdk/README.md) | Build your business role over shared workspace behavior. |
| [Reference](harness/README.md) | Read the runtime, mechanics, manifests and dated target designs. |

For agent-assisted installation, use the bundled [setup skill](.agents/skills/agent-workspace-setup/SKILL.md).

## Build a vertical

Start with [harness/verticals/bare/](harness/verticals/bare/). Declare a role, fixed conversations, classes, prompts, tools, complete-job recipes and bounded sub-agent roles in `vertical.json`. Optional input providers, tabs and displays use the same SDK.

```bash
ROCOTO_VERTICAL=bare .venv/bin/python harness/run.py pack bare
.venv/bin/python harness/deploy/build_preview.py --vertical bare
```

The builder selects the declared source closure, placing a bare artifact under `harness/dist-preview/bare/`. It works in a Git checkout or a reviewed source archive through `provenance/release-files.json`.

[SDK guide](harness/sdk/README.md) · [Architecture](docs/ARCHITECTURE.md) · [Mechanics](harness/mechanics/README.md) · [Dependency contract](DEPENDENCIES.md)

## Existing framework

| Capability | Implementation |
|---|---|
| Durable workspace conversations, members and files | Shared backend, SQLite/file stores, owner and scope checks |
| Managed execution and recovery | Explicit session/turn adapter, tool journals, Stop and descendant quiescence |
| Complete jobs and bounded roles | Revisioned recipes, prerequisites, completion evidence, role tools/deadlines/budgets |
| Background work | Durable grants, timers, intervals, signed webhooks and screened signal chains |
| Tools and connected systems | Vertical registries, managed connections, MCP read grants and action-bound approvals |
| Usage and spend | Company-bound funds, atomic reservations and durable reconciliation |
| Optional extensions | Custom connector/display sandbox brokers; source-only retail camera/retrieval/tracker vertical |

The retail code is included as an extension example with separate requirements. Source footage, imported datasets, benchmark receipts and demo-company branding are outside the export. The client manufacturer application belongs to a separate Vitrina product release; a synthetic manufacturer SDK fixture remains in `harness/tests/verticals/`.

## Verify locally

The lean framework/protocol suite runs with the pinned shared environment and fake provider transports:

```bash
ROCOTO_VERTICAL=bare .venv/bin/python -m pytest -q \
  harness/tests/test_layout.py harness/tests/test_vertical_loading.py \
  harness/tests/test_bare_deployment.py harness/tests/test_public_release.py \
  harness/tests/test_managed_codex.py harness/tests/test_managed_spend.py \
  harness/tests/test_managed_sessions.py harness/tests/test_managed_turn.py \
  harness/tests/test_managed_files.py
```

[CI](.github/workflows/offline.yml) runs this same lean command on Python 3.12 with read-only repository access. The release record preserves test counts, dependency versions and two inherited failures from the broader original suite. Retail tests need their extra requirements; GPU/model packages and live services are separate installations. Preparing this candidate made zero model/provider/GPU calls. [Verification record](provenance/release-checks.json).

## Source and licence

Peter Neyra's original code is [MIT licensed](LICENSE). Vendored libraries, fonts, model assets and integration marks retain their own notices in [THIRD_PARTY.md](THIRD_PARTY.md). Authored UI artwork has retained provenance. The original source and historical operating case remain privately preserved; `provenance/source-export.json` identifies copied files and deliberate omissions.

[made by atelier neyra](https://atelierneyra.com)
