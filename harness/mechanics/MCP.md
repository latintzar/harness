# MCP

Rocoto exposes the active workspace's reviewed reads to a customer's own agent through `/mcp`. The same [tool registry](../sdk/tool_families.py) supplies the hosted parent, stock children and local command, with an explicit allowlist for each surface. The selected vertical contributes its own reads. Tool names and counts are derived from that registry.

## Public access

[mcp_host.py](mcp_host.py) serves Streamable HTTP MCP and the browser's key-management routes. A key binds workspace, owner and collection, with expiry and revocation. [mcp_access.py](mcp_access.py) stores its hash. Settings → MCP access creates and revokes keys; the raw credential is shown for the private handoff.

[public_mcp.py](public_mcp.py) obtains an opaque admission for each request. Membership and current resource access are checked before and after the read. Results pass the shared size bound and public sanitizer. The caller's key selects a collection; it supplies neither arbitrary filesystem paths nor a wider owner.

The public surface currently grants reviewed reads. Conversation writes and subscriptions remain the [MCP writes design](../design/MCP-WRITES.md), whose status separates implemented primitives from the proposed external contract.

## Managed children

`/runtime/mcp` is the managed runtime's session-bound read door. Its credential and lifecycle are separate from customer-issued keys. [managed_mcp.py](managed_mcp.py) binds a stable private token to one durable session and grants a fresh server-held generation for each admitted turn. The generation captures original actor, scope, deadline and cancellation. An in-flight callback retains its own generation even if a later turn begins.

Between turns and after restart, tool authority is withdrawn. Recovery can reinstate only the original funded request under current permission; stop or revocation retires that request. Root and stock children share the same narrow reads and original admission.

## Local command

[mcp_server.py](mcp_server.py) composes the selected workspace's local services through the same registry. A host-authenticated principal and collection establish private access; retail can contribute its explicitly commissioned local operations. Local construction avoids scheduler recovery and messaging providers.

Run `python3 harness/run.py mcp_server --help` from the repository root for the current state, principal and collection flags. Set `ROCOTO_VERTICAL` to the intended vertical before starting the process. The bootstrap installs shared code and that vertical alone.

## Evidence

The public, local and managed MCP tests cover tool-surface parity, owner and collection binding, revoked membership, bounded results and retained admission across awaits. The [October 5 transport receipt](../../docs/HISTORICAL-RECORDS.md#record-7) records the earlier published tool set; a new release records its exact current tools and revocation proof. Deployments and origins are listed in [the deployment source](../../README.md#run-the-bare-workspace).
