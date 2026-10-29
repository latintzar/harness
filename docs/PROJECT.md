# A Codex-based agent harness with generative UI

Harness is a Codex-based, vertical-adaptable agent harness with generative UI. A business supplies its domain, tools and interfaces; the harness supplies conversations, files, jobs, approvals, spending records and an SDK for agent-built displays.

A business agent needs a place to work: persistent context, scoped tools, credentials, approvals and interfaces for its results. Rebuilding that framework for every vertical makes the domain work harder to maintain.

| Feature | What it does |
| --- | --- |
| Vertical SDK | Declare roles, tools, recipes, data classes and optional displays for a business. |
| Generative UI | Use the display SDK to render domain-specific views from an agent’s work. |
| Persistent work | Keep conversations, scoped files and jobs with the workspace. |
| Supervision | Connect approvals, identity and spend accounting to agent actions. |
| Runtime adapters | The current source includes managed OpenAI execution; the project retains its Codex-based lineage and architecture. |

## Workflow

Define a vertical → Give a role tools and recipes → Run supervised agent work → Render a domain display → Retain work and evidence

## Design decisions

- The framework owns shared infrastructure; the vertical owns business meaning.
- Tools and displays have declared contracts, so a new industry can build on the same harness.

## Status and next steps

The source includes the original harness and SDK. The hosted bare workspace is a read-only visitor example with an authored display fixture; connected agent execution uses the operator’s own provider setup. Build a vertical with one role, one useful tool and one display, then commission its managed runtime.

Source: [harness/sdk/README.md](../harness/sdk/README.md).
