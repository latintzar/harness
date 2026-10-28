# Agent workspace engineering

Read README.md, DEPENDENCIES.md and docs/ARCHITECTURE.md. This is the reusable Vitrina software Harness. For installation work, read [.agents/skills/agent-workspace-setup/SKILL.md](.agents/skills/agent-workspace-setup/SKILL.md).

Keep runtime, mechanics, SDK and verticals separate. Shared code consumes declared interfaces. Start the bare vertical with fresh private state outside the checkout; keep keys, recordings, customer data and funded authorizations in their owning private runtime.

Run the relevant offline tests for source changes. Tests use synthetic state and fake provider transports. Real OpenAI execution, paid integrations, GPU work, publishing and deployment are separate actions. Preserve original-code provenance and third-party notices. Update provenance/release-files.json and provenance/file-sha256.json when preparing a new reviewed source export.
