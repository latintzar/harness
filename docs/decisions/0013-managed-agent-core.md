# Decision 0013: Managed agent core

Public-release summary prepared 28 October 2026 from the existing runtime, SDK and mechanics documentation. The original private project's dated deployment receipts remain separate.

The framework delegates the agent loop, session execution, hosted environment and stock sub-agents to OpenAI's managed Agents API. It owns the workspace backend/page, durable conversations, scoped tools, jobs, approvals, connections, files, funding and vertical interfaces.

This boundary lets upstream execution evolve behind an explicit adapter while the application's business authority and saved effects remain under the workspace's control. The runtime persists request/session/tool identities, verifies current admission, reconciles recovery and confirms descendant quiescence before releasing authority or accounting holds.

Local founder adapters are historical migration material, excluded from built runtime artifacts. Custom connector/display execution is a separate optional broker capability and does not replace the managed agent core.

Consequences: the page runs locally, agent execution needs the operator's own OpenAI project/access/funding, and hosted children need a reachable scoped MCP endpoint. A locally passing test suite establishes host behavior with fake transports; a live provider proof remains a distinct commissioning result.
