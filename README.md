# Harness

A Codex-based agent harness with generative UI

Harness is a Codex-based, vertical-adaptable agent harness with generative UI. A vertical supplies its domain, tools and interfaces; agents work through conversations, scoped files, jobs, recipes, approvals and spend accounting.

[Website](https://atelierneyra.com/harness) · [Open demo](https://preview.atelierneyra.com/apps/agent-workspace/) · [Product tour](docs/product-tour.md) · [Documentation](https://docs.atelierneyra.com/agent-workspace/overview/)

[![Harness Organization chat discussing a ready-order display, with the agent sidebar](https://atelierneyra.com/assets/product-motion-v5/agent-workspace-0e3fe195346c.jpg)](https://preview.atelierneyra.com/apps/agent-workspace/)

## A dispatch view from a conversation

1. **Ask for a useful result.** An Organization conversation keeps the team’s request and context together: for example, “show the orders ready for dispatch.”
2. **Run the domain work.** The selected vertical supplies order tools; declared roles and recipes give the job its steps, scope, limits and evidence.
3. **Show the result.** A generative display can present the count, chart and individual orders alongside the conversation. Files and job progress remain in the workspace.

## Product areas

| Area | What people do |
| --- | --- |
| Organization chat | Ask for work, retain context and discuss the result. |
| Files | Inspect the artifacts associated with the conversation. |
| Agents and jobs | Define a role, inputs, tools, schedule and limits. |
| Displays and tabs | Give structured domain results a useful interface. |
| Connections and approvals | Connect a business system and approve a particular action. |
| Usage and spending | See what the agent did and what its work cost. |

The public bare workspace contains an authored example conversation about the display SDK. Connected execution uses your OpenAI project, allowance and reachable MCP service. The current managed-runtime adapter retains the project’s Codex-based lineage and vertical architecture.

[Product tour](docs/product-tour.md) follows that experience and maps the framework modules. [Vertical SDK](harness/sdk/README.md) explains how to supply the business-specific tools and displays.

## Run the bare workspace

Use Python 3.12:

```sh
git clone https://github.com/latintzar/harness.git
cd harness
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/dev.py
```

Open **http://127.0.0.1:8780** and enter the one-use code saved in the private file printed by the launcher. The local workspace includes its real conversation and file stores. [Native local workspace](docs/LOCAL-WORKSPACE.md) explains the included files and public visitor mode.

For connected agents, follow [OpenAI setup](docs/OPENAI-SETUP.md) with your own project, allowance and MCP origin.

## Guides

[Product tour](docs/product-tour.md) · [Build a vertical](docs/developer-guide.md#build-a-vertical) · [Vertical SDK](harness/sdk/README.md) · [Architecture](docs/ARCHITECTURE.md) · [Developer guide and checks](docs/developer-guide.md) · [Setup skill](.agents/skills/agent-workspace-setup/SKILL.md)

## Licence

Peter Neyra’s original code is [MIT licensed](LICENSE). Dependencies, fonts, artwork and integration marks retain their [own notices](THIRD_PARTY.md). The [developer guide](docs/developer-guide.md#source-and-licence) retains source-export and verification details.

[made by atelier neyra](https://atelierneyra.com)
