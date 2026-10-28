# Agent workspace framework

This is the reusable Vitrina Harness implementation. The [public front page](../README.md) owns setup, current verification and release boundaries. OpenAI runs the managed core; this code owns persistent workspace behavior and vertical interfaces.

- [SDK](sdk/README.md): create one vertical folder with prompts, classes, tools, recipes, roles, tabs and displays.
- [Mechanics](mechanics/README.md): durable jobs, conversations, authority, signals, approvals and connections.
- [Runtime](runtime/README.md): managed API adapter, recovery and tool/file projections.
- [Architecture](../docs/ARCHITECTURE.md): current layers, request flow and state ownership.
- [Target designs](design/SERVICE-BOUNDARY.md): dated retail service, data and MCP-write contracts.
- [Own-key setup](../docs/OPENAI-SETUP.md): project scopes, private key, allowance and reachable MCP origin.
- [Bare vertical](verticals/bare/README.md): minimal workspace example.
- [Retail](verticals/retail/README.md): optional source extension; camera/media assets require the operator's own authorized inputs.

Historical source names and environment variables remain compatible. Dated deployment receipts, provider grants, imported source media and private runtime state remain outside this export. The [historical index](../docs/HISTORICAL-RECORDS.md) identifies original operating records cited by retained guides.
