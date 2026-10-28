# OpenAI core adapter

Public source release: use the root README and docs/OPENAI-SETUP.md for a new installation. Historical commissioning/deployment references describe the original project; source footage, account grants and private receipts are outside this export.
OpenAI's managed Agents API is the required agent core. OpenAI runs and updates it; Rocoto's `runtime/` contains its API adapter, authorization boundaries and result projection. The three levels are core, harness and vertical. [Decision 0013](../../docs/decisions/0013-managed-agent-core.md) records the managed-runtime choice.

## Sessions and turns

[managed_codex.py](managed_codex.py) implements the official managed API transport. [managed_runtime.py](managed_runtime.py) binds a provider session to its durable Rocoto conversation through [managed_sessions.py](../mechanics/managed_sessions.py). The session configuration selects OpenAI-hosted execution and the commissioned spending policy: a finite provider cap by default, or the expressly approved monitored target. A turn records its original actor, request identity, input, provider session and progress before dispatch.

The shared bridge calls the configured managed runtime. The runtime receives credentials from the workspace's private server environment. Provider keys and MCP authorization values remain in private custody; chat history and public status carry resource identities and safe receipts.

Restart recovery resumes the saved request and assistant row. It reconciles durable tool effects and provider state before accepting replacement work. Stop and withdrawn permission interrupt the original root and descendants; concurrency remains reserved until their quiescence is confirmed. Provider sessions with changed immutable tools, plugins or environment receive a new binding after the previous work is retired.

## Tools, children and approvals

The [SDK registry](../sdk/tool_families.py) builds the parent's admitted function tools and the read tools visible to stock children. [managed_turn.py](managed_turn.py) journals each function call under its provider action identity before executing it, checks current admission around effects and returns its saved outcome on replay.

Stock multi-agent execution stays inside OpenAI's core. Its children share a revocable managed MCP lease rooted in the original conversation and actor. The stable private session credential points to a server-held generation; each turn captures its own admission, deadline and cancellation signal. Restart withdraws tool authority until the same funded request is reconciled.

The common approval router ties a decision to the requesting action, workspace, actor and current revision. Connected-account writes require their current grant and any applicable human approval at dispatch. A class declared with `participant_reach` retains that restriction through descendants; its permitted recipients are resolved by the host.

Named [roles and jobs](../sdk/README.md#roles-and-job-recipes) use `delegate_job` to start a separate managed session with the declared read tools, model, effort, deadline and cost allocation. The host reserves the parent and every permitted role slot atomically within the same $1 turn allowance before provider input. These read jobs use the same managed loop with their environment and further delegation disabled. Their original actor, parent request and saved call identity survive restart. Parent Stop confirms each job's provider quiescence before acknowledgement.

## OpenAI plugins and capabilities

[hosted_capabilities.py](hosted_capabilities.py) prepares reviewed installed packages for the managed session. Installed revisions, verified files and receipts determine the selection. An upstream catalogue is discovery material; the installed immutable package supplies execution bytes. Removing a capability revokes the previous session binding before replacement tools become available.

Shared connection policy governs provider accounts and scope; [CONNECTIONS](../mechanics/CONNECTIONS.md) owns those rules. OpenAI's plugin format and managed execution remain upstream interfaces. Local custom compute supports separately bounded display and connector jobs beneath the harness.

## Files

[managed_files.py](managed_files.py) imports provider-created artifacts through their original session and artifact identities. Durable receipts bind the remote file to the conversation and admitted owner. Download, publication and later reads retain current permission checks. The ordinary workspace Files surface serves the resulting host-owned artifact.

## Funding and evidence

[managed_policy.py](managed_policy.py) reserves the workspace's existing provider funds before starting or extending paid authority. Parent and children share a $1 turn allocation. The default `enforced` spending mode binds OpenAI’s session cap. Peter approved `ROCOTO_MANAGED_SPENDING_MODE=monitored` for the rollout on 13 October: token-based estimates and the original five-minute deadline trigger automatic cancellation, while charges can exceed the target. Approximate provider consumption retains its hold until a final attributable receipt settles it. [Cost safety](../../docs/COST-SAFETY.md) owns commissioning and account limits.

The managed adapter has offline protocol, restart, authority, tool-effect, file and accounting proofs. A live managed run and publication are separate receipts. [Deployment](../../README.md#run-the-bare-workspace) records their state; [managed API commissioning](../../docs/HISTORICAL-RECORDS.md#record-4) owns the concrete proof procedure. Earlier app-server choices and deployment proofs are retained in [dated records](../../docs/HISTORICAL-RECORDS.md#record-21).

## Local adapter retirement

The running page uses `ManagedRuntime`; the shared bridge accepts managed controls only. Local founder authentication reports inference disabled on every host. Historical local adapters now live in [deploy/migration/legacy](../deploy/migration/legacy/README.md), alongside the retained self-hosted executor. Their original tests and proof drivers remain in place.

The read-only operator export now owns its transport, public-history conversion and settings in [deploy/migration](../deploy/migration/app_server.py), [history.py](../deploy/migration/history.py) and [config.py](../deploy/migration/config.py). Its protocol proof invokes `initialize` and `thread/read`, preserves the saved owner, and never writes a model catalogue. The current provider relay imports [provider_tool_policy.py](provider_tool_policy.py); the old policy module only re-exports that shared effect classification.

The reversible relocation moved those six adapters plus `managed_executor.py` into the migration package. Every existing test, assertion and proof driver is retained; historical imports and proof source lookups name the new location. Both published artifact selections exclude the package. The [relocation record](../deploy/migration/legacy/README.md) identifies the original paths, validation and rollback. An earlier broad deletion proposal was withheld after automatic approval review identified possible active coverage loss; this change preserves that coverage in place.

The following behavior checks now execute the managed protocol, with local provider fixtures:

| Preserved behavior | Current regression |
|---|---|
| Exact user text, separate model context, readiness and collection fence | `test_chat_message_context.py::test_managed_input_keeps_context_separate_and_retains_host_scope` |
| Generated file event, persisted owner/chat and measured turn time | `test_native_files.py::test_managed_tool_dispatch_emits_created_file_and_measured_turn_boundaries` |
| HTTP reopening of the file and timing receipt | `test_native_files.py::test_http_chat_rehydrates_managed_timing_and_generated_document` |
| Private credential prompt reaches the page while the original function awaits its answer | `test_credential_requests.py::test_managed_dispatch_streams_private_prompt_while_same_function_waits` |
| Stop and restart retire the original private-input waiter | `test_credential_requests.py::test_managed_stop_and_restart_cancel_persisted_private_requests` |
| A message send waits for provider root/child quiescence, survives a lost tool-result reply, and withholds receipts after revoked access | `test_organization_chats.py::test_managed_message_receipt_stops_provider_and_preserves_sender` |
| Resource selection and later configuration changes keep earlier scoped messages private | `test_scope_native.py::test_managed_bootstrap_after_selector_change_keeps_prior_history_private` |
| Pending recovery keeps its original scope; saved grandchildren retain root history authority | `test_managed_runtime.py::test_changed_history_scope_cancels_original_recovery_before_new_funding`, `test_scope_host.py::test_managed_grandchild_history_keeps_root_scope_after_revocation` |

A successful message delivery is saved against its exact managed request and function call before returning the tool result. Recovery stops at that durable boundary, so it can cancel the original provider session without repeating the send. The public chat changes to `waiting_for_reply` after quiescence; accounting retains the provider's terminal cancellation receipt. A delivery needing attention retains that status instead. Generated host files emit ordinary artifact events, and provider-harvested files retain the existing managed artifact path.

The admission carries an opaque history-scope identity into the managed session fingerprint. A changed scope starts a new provider generation after quiescence; recovery first reconciles the original funded session. Once a conversation has any scoped generation, replacement sessions start with fresh input because older local messages lack individual scope receipts. The saved chat remains available under its cumulative history guard. Stock and named descendants resolve their full same-owner, same-collection ancestry before applying that root guard.

The remaining mixed local fixtures include inspection previews/outcomes, provider-resource projection, questions/deadlines, skill selection, scope partitioning, and fresh/resumed prompt assembly. Current managed suites cover their broader lifecycle, files, approvals and authority boundaries; that coverage is evidence for reviewing each old assertion, rather than permission to drop a unique behavior check. The eight commissioned broker/custom-compute helpers remain byte-identical.
