# Product tour

Harness is a Codex-based, vertical-adaptable agent harness with generative UI. A business supplies its domain, tools and interfaces; the harness supplies conversations, files, jobs, approvals, spending records and an SDK for agent-built displays.

A business agent needs a place to work: persistent context, scoped tools, credentials, approvals and interfaces for its results. Rebuilding that framework for every vertical makes the domain work harder to maintain.

## An agent builds a dispatch view

1. **Start in Organization.** A team member asks for a view of orders ready for dispatch. The conversation keeps the request and its subsequent work together.
2. **Read the permitted records.** The vertical supplies an order-reading tool or source. A bounded reader role gets its declared tools, scope, deadline and share of the parent’s allowance.
3. **Follow the job.** A recipe records prerequisites, steps and completion evidence. The member can see what the agent is doing; connected writes use the relevant action-bound approval.
4. **Show a useful interface.** The display SDK turns the result into a domain view: for example, a ready-order count, chart and underlying records beside the conversation. The vertical owns those records and labels.
5. **Keep the work.** Conversation history, scoped files, saved job progress and usage receipts remain in the workspace. Stop and revocation retain their execution and reconciliation contracts.

This is the connected product flow for a vertical that supplies order tools and display data. The bare starter exposes the reusable workspace; its public visitor conversation is an authored example about the display SDK. Managed execution uses your OpenAI project, explicit allowance and reachable MCP service.

## Product areas

| Area | What you do | Result |
| --- | --- | --- |
| Organization chat | Ask for work in the business’s own terms. | The request, its context and the resulting discussion share one conversation. |
| Files | Open the files associated with a conversation. | The agent and member can refer to the same scoped artifacts. |
| Agents and jobs | Define a role, inputs, tools, schedule and limits. | A reusable job can retain its progress and completion evidence. |
| Displays and tabs | Present domain results as a useful interface. | Counts, charts and records appear through the shared display SDK. |
| Connections and approvals | Connect a business system and approve a particular action. | Tools use current member authority and the selected account. |
| Usage and spending | Inspect execution and its saved usage. | Each paid turn reserves funds and keeps its cost record. |

## Design decisions

- **The vertical owns the business.** Prompts, tools, sources, roles and optional pages belong to the selected vertical. The framework supplies the common work environment.
- **Recipes retain the full job.** Recipes retain their original definition, step prerequisites and evidence. Skills teach a source or connection; displays present results.
- **Interfaces accompany the conversation.** The display SDK gives structured results a place to live alongside the request that produced them.
- **Authority travels through execution.** Membership, scope, approvals, stop state and spending are checked across tool work and replay.

## Reached stage

Harness retains its Codex-based lineage; the current source adapter uses OpenAI’s managed Agents API for the agent loop and hosted execution. The local starter opens the real workspace, access and file stores with provider-off state. The hosted visitor sees authored example content. Build a vertical with one useful tool, one bounded job and one display, then configure and test its managed runtime.

## Modules

| Source | Responsibility |
| --- | --- |
| [Bare manifest](../harness/verticals/bare/vertical.json) | Declare the Organization conversation, default Agent class, roles and recipe. |
| [Vertical SDK](../harness/sdk/README.md) | Add domain instructions, tools, sources, host hooks and displays. |
| [Roles and recipes](../harness/sdk/job_recipes.py) | Validate bounded roles and complete jobs with prerequisites. |
| [Display presentation](../harness/sdk/display_presentation.py) | Validate and publish the structured display result. |
| [Skills](../harness/mechanics/skills.py) | Keep revisioned source and connection instructions. |
| [MCP access](../harness/mechanics/mcp_access.py) | Expose reads under declared scope and grants. |
| [Managed runtime](../harness/runtime/managed_runtime.py) | Connect durable workspace work to the managed agent core. |
| [Shared browser shell](../harness/web/) | Present conversations, files, agents, connections and optional vertical pages. |

## Setup and implementation

[Native local workspace](LOCAL-WORKSPACE.md) · [OpenAI setup](OPENAI-SETUP.md) · [Architecture](ARCHITECTURE.md) · [Engineering decisions](ENGINEERING.md) · [Vertical SDK](../harness/sdk/README.md) · [Mechanics reference](../harness/mechanics/README.md) · [Setup skill](../.agents/skills/agent-workspace-setup/SKILL.md) · [Developer guide](developer-guide.md)

[made by atelier neyra](https://atelierneyra.com)
