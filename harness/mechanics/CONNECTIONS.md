# CONNECTIONS

A connection lets the workspace read or act in a company's system through an identified account and current scope. The Organization agent can explain setup and readiness. The host owns authentication, destination policy, secrets, approval and effect receipts.

## OpenAI and reviewed providers

[mcp_connections.py](mcp_connections.py) manages reviewed provider connections and per-person OAuth or bearer credentials. Agents use `list_connections`, `list_connection_tools` and `read_connection` to discover actual available reads. Provider policy controls transport, schema, scope and result size. Choosing a connector or installing a plugin is distinct from authorizing a particular account operation.

[managed_connections.py](managed_connections.py) adapts these grants into the managed runtime. Current workspace admission, account identity and operation scope are rechecked at dispatch. Participant-reach classes retain their recipient restriction through child ancestry. Account writes pass the shared approval route and durable action identity; a repeated provider call returns the retained receipt rather than reapplying the effect.

OpenAI's installed plugins and their immutable package receipts are described in [the runtime source](../runtime/README.md#openai-plugins-and-capabilities). Reviewed local provider profiles and commissioning evidence stay beside [the connection runbooks](../../docs/HISTORICAL-RECORDS.md#record-6).

## Custom connections

[custom_connectors.py](custom_connectors.py) stores the proposed adapter, setup revision, credentials request and activation. A setup conversation identifies the system and operation, prepares a bounded adapter, obtains current owner authorization and tests the declared read. Credential values enter a private host request; chat stores the request's identity and safe status.

[custom_execution.py](custom_execution.py), [custom_coordinator.py](custom_coordinator.py) and [custom_http.py](custom_http.py) run admitted public-HTTPS reads through the commissioned broker. Each job binds workspace, connector, revision, destination and current authority. The host controls DNS/address validation, TLS, methods, redirects, time, rows and bytes. The sandbox receives its exact snapshot and permitted gateway instead of ambient account credentials.

The coordinator journals request identity before dispatch and records cleanup. Stop or grant loss cancels the original job. An uncertain external result stays attached to that job for reconciliation. Source-specific transports, including retail storage and media, are installed by their vertical extension.

## Results and files

Provider output retains its original provenance and current account scope. The host validates display fields, safe links and generated files before exposing them. [provider_outputs.py](provider_outputs.py) is the common projection boundary; a vertical adds its own evidence preview only for its registered host tools.

Output that belongs in a display uses the [display SDK](../sdk/TILES-AND-TABS.md). Provider file artifacts use [managed file receipts](../runtime/README.md#files). A successful connection test proves the tested read; readiness reports the actual operation and account still available.

## Registered outgoing endpoints

[signal_endpoints.py](signal_endpoints.py) stores an owner-authorized HTTPS endpoint bound to its workspace, actor, chat and collection. The host resolves and pins public addresses, verifies TLS and fixes the destination. Each request has a stable event identity, timestamp and HMAC signature. Its canonical data envelope carries the approved payload.

`send_webhook` executes under the managed function call's durable identity, and a registered chain effect uses its original signal identity. Both reserve the shared daily send quota before transport. Endpoint-specific limits can narrow that quota. A lost response remains uncertain; duplicate calls read their saved receipt. Revocation and current owner role are checked again before transport. The endpoint signing secret is a private copy-once value.

## Tests and operation

Connection policy, native approval, managed connection, provider output, custom coordinator and HTTPS gateway tests exercise these boundaries using local stand-ins. [Deployment](../../README.md#run-the-bare-workspace) links the commissioning runbooks and receipts. [CHANNELS](CHANNELS.md) owns messages to people; [MCP](MCP.md) owns an external agent reading Rocoto.
