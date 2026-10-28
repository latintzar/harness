# TILES AND TABS

The SDK gives every vertical the same tabs, tiles, canvases, saved presentation and bounded script runtime. A display belongs to its actual owning agent. A tile and its full canvas show one published output revision.

## Display envelope and ownership

[display_presentation.py](display_presentation.py) supplies `PresentationKit`, schemas, safe bindings and page projection. `PresentationKit.display(id, owner_agent_id, output, spec)` creates an envelope containing `display_id`, `owner_agent_id`, `revision`, `tile`, `canvas` and `presentation`. The host admits source data and checks the owning agent's visibility before serving it.

`revise(current, change, output=..., expected_revision=...)` validates a saved change and advances its revision inside the adapter's atomic write transaction. The adapter persists current presentation and history alongside its publication. Deleting a display removes its saved presentation in that same transaction.

Core or unlinked displays resolve their owner through the canonical Organization conversation. Linked displays use their actual saved agent. Legacy records retain the actual principal when an agent identity has yet to be established.

## Kit and rendering

The shared kit contains charts, timelines and instances. A vertical extends a kit with authored family declarations, schema validators, recipe aliases and trusted scalar row fields. Each instance keeps its own vocabulary. Model and script text render as inert text; admitted evidence references retain their host provenance and current-origin URLs.

Load `/display-charts.css`, `/display-canvas.css`, `/display-charts.js` and `/display-canvas.js` in resource order. `RocotoDisplayCanvas.mountDisplay({display, ...options})` returns `element`, `output`, `update({display})` and `destroy()`. `tileDisplay(...)` draws the compact form. Updates preserve the selected period, scroll, focus and phone slot. `onOpenAgent(id)` opens the owning agent.

`RocotoDisplayCanvas.create(extend)` accepts a vertical's output/event adapters, block renderers, optional family canvases and selection callbacks. Retail's tracker-canvas adapter contributes shelf and product vocabulary. Shared rendering is independently exercised with another business's display at desktop and phone widths.

## Scripts and schedules

[display_scripts.py](display_scripts.py) runs a script through the commissioned custom compute broker. A `ScriptContract` supplies input packing, immutable snapshot, input digest and domain faults. The shared runtime owns job admission, bounded execution, concurrency, cancellation and broker cleanup. `opened` persists the original broker request before dispatch; `closed` clears it only after release is confirmed.

[display_runs.py](display_runs.py) stores policies, optimistic revisions, leased passes, run history and unfinished cleanup. The host supplies owner, collection and an opaque scope identifier. `claim`, `renew` and `finish` retain the original lease token. `due_reading` evaluates manual, interval, changed-input and daily triggers against the admitted input stamp.

A calculation can run as a registered step in the [common signal chain](../mechanics/README.md#wakes-and-the-signal-chain), complete on its own or deliver its saved result to an agent. The producer checks exact source incarnation, revision and current scope. The original input snapshot and external job identity remain available while the signal or cleanup is unfinished. Recovery reconciles that job's receipts before another calculation is admitted.

Retail retains its installed ledger names and publication transaction through a thin adapter. Its source/camera checks, location time and output families remain in the vertical.

## Tabs

The shared shell provides the primary workspace navigation and `RocotoWorkspaceTabs`. A vertical declares its pages and contributes mount, show, hide and unmount behavior through the SDK. Workspace and identity changes invalidate pending asynchronous work before any later response can render. Hiding a tab releases its active polling or media work; opening it reads current admitted state.

The server injects only the selected vertical's source-owned `page_assets` into the shared page. A model can change display data and validated presentation; executable page assets belong to the reviewed source.

## UI extension

`RocotoWorkspaceExtensions.register(verticalId, factory)` installs one vertical factory. Its frozen host context exposes live shared-state accessors and explicit operations. The extension owns domain state, source DOM, styles, listeners and safe wire adapters. The bare extension provides the shared fallback.

| Concern | Representative hooks |
| --- | --- |
| Lifecycle | `applyHarness`, `initializeRoute`, `resetWorkspace`, `resumeWorkspace`, `cancelPending` |
| Navigation | `selectCollection`, `openCollection`, `renderNavigation`, `renderView`, `surfaceLayout` |
| Composer | `draftData`, `restoreDraft`, `requestData`, `composerData`, `renderComposerAttachments` |
| Messages | `messageData`, `renderMessageContent`, `renderUserAttachments`, `receiveEvent` |
| Sources | `collections`, `normalizeCollection`, `collectionLabel`, `updateStatus`, `resolveReference`, `resolveMedia` |
| Agent input | `openAgentEditor`, `agentEditorData`, `agentDefinitionData`, `renderAgentDetails` |

Shared messages, generated files, approvals, questions, identity, grants and run history remain common mechanics. Domain edits preserve exact input identifiers and filters. Reference and media hooks receive a current-scope predicate and recheck it after asynchronous resolution.

The shared wake controls list public metadata and reveal a signing secret through the private CSRF-protected endpoint. Their identity fence aborts stale requests, clears transient secret values and preserves ordinary history and browser drafts as public metadata only.

## Evidence

[Display SDK tests](../tests/test_display_sdk.py), [run-ledger tests](../tests/test_display_runs.py) and [browser display tests](../tests/ui_display_sdk.cjs) cover owner separation, optimistic revisions, binding safety, replacement leases and the shared renderer. Retail script, presentation and signal-adapter tests preserve its existing product behavior through these same implementations.
