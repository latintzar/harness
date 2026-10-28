# Harness mechanics

Current source guide with dated original operating examples. [Public architecture](../../docs/ARCHITECTURE.md) describes this release; the [historical index](../../docs/HISTORICAL-RECORDS.md) identifies original deployment receipts cited below.

Rocoto's harness gives a workspace its agents, records, connections and working surfaces. OpenAI runs the agent core; the selected vertical supplies its business role. Start at the [front door](../README.md), then use [the SDK](../sdk/README.md) to add a vertical.

## Workspace and Organization agent

A workspace owns its business profile, members, collections, files, connections, usage and durable jobs. Each member acts under their own identity. Shared data ownership and the acting member travel separately through every tool, saved receipt and provider request.

[organization_chats.py](organization_chats.py) maintains the canonical Organization conversation for each workspace and collection. The selected manifest can add fixed purposes. Canonical chats preserve their identity while different authorized members contribute ordinary messages. The Organization agent reads current setup and readiness, carries the workspace's instructions and can launch its declared job recipes.

[scope_host.py](scope_host.py) captures one admission for a turn. Nested tools and descendants retain the original actor and resource view. Current membership, conversation visibility and the original grant are rechecked before and after awaited work. A vertical contributes resource-specific scope checks through its installed host extension.

## Agent classes

A manifest declares class identifiers, labels, input kinds and one of two authorities: `participant_reach` or `granted_effects`. Class names belong to the vertical. [agent_kind_policy.py](agent_kind_policy.py) applies the authority through saved definitions and both parent and provider-root ancestry. Unknown declarations and incomplete ancestry retain restricted effect access.

A participant-reach agent reaches its own current participants through the messaging host. A granted-effects agent can use a connected operation only under its actual account, scope, approval and budget. Retail adds the live-observation condition in its own host callback.

An ordinary chat can become a saved agent in place. A ready-made agent creates an inactive draft with its declared class, inputs, filters and allowance in that same chat; activation is a separate current-authority decision.

## Rules

Workspace and vertical instructions describe the work. Typed admission, grants, budgets and tool schemas enforce its authority. An instruction requesting a permission or limit changes neither the host grant nor the available money. The host preserves actual author, source and evidence fields when a model proposes content.

## Skills

[skills.py](skills.py) stores numbered instruction revisions with their required tools and connection scopes. A workspace starts with the selected vertical's seeds. Installing or selecting a skill checks current availability; unavailable tools remain explicit readiness gaps. Installed OpenAI packages use the [runtime's verified package selection](../runtime/README.md#openai-plugins-and-capabilities).

## Sub-agents and recipes

A declared role specifies a job, instructions, permitted launchers, tools, model, effort, money, duration and per-turn count. `delegate_job` executes a named read role in its own bounded managed session, funded from a slot preallocated with the parent turn. Root and role caps total at most $1. The role retains the original parent request and actor, narrows its tools to declared reads and carries a durable deadline through restart. OpenAI's stock subagents remain within-session core orchestration and share the root's MCP admission.

A recipe describes a complete job: ordered steps, prerequisites, responsible roles, instructions, tools, evidence and completion criteria. [recipe_runs.py](recipe_runs.py) saves the exact recipe revision and conversation-scoped progress. Completing a step requires its prerequisites and recorded evidence; an optimistic revision check protects concurrent updates. Reopening a running job returns its original record. Display collections are `tile_sets`, a separate manifest field.

## Wakes and the signal chain

[signal_chain.py](signal_chain.py), [agent_wakes.py](agent_wakes.py) and [signal_dispatcher.py](signal_dispatcher.py) use the same `background-agents.sqlite` ledger for authenticated messages, schedules, timers, hooks, source events and registered calculation jobs. Every signal keeps its original ingress identity and payload. Processed output is stored separately; it changes the delivered content while the original source remains the authority.

The transparent default delivers the signal. A reviewed chain can add bounded rules, a registered script, an optional cheap classifier or a registered effect before delivery. [wake_tools.py](wake_tools.py) exposes the host's declared handler schemas in `list_wakes`, `set_wake` and `create_webhook`; executable source comes from the host's registered adapter. Hooks have private signing secrets, deduplicated event identities and current destination grants. Their copy-once secret view clears on closure or identity change.

[signal_screening.py](signal_screening.py) provides one bounded Responses classification step against the workspace's dedicated API pool. It reserves money before dispatch, records the original request, and settles actual usage from the retained provider response. A lost paid response remains uncertain and held for reconciliation. Classifier output decides whether to deliver; original event data and authority remain intact.

A calculation-only signal can complete without an agent turn. A registered producer proves its current source and exact incarnation; optional delivery promotes that same signal to its saved agent destination. Every asynchronous step journals its external identity before dispatch and retains cleanup evidence. Restart reconciles the original operation instead of repeating uncertain work.

## Long-running agents

[background_agents.py](background_agents.py) stores definitions, revisions, runs, standing grants and scheduler recovery. The selected input provider supplies readiness, admitted inputs and resource accounting before recovery starts. Bare uses the SDK's source-free provider; retail installs its observation provider.

Standing authority preserves the original actor, definition revision, scope and expiry across restart. A paused, revoked or expired grant withdraws new execution. An already admitted hosted request remains recovering until the original provider work is cancelled or settled. Legacy session-bound activations retain their expiry. Definition edits invalidate the prior revision's execution authority.

Schedules, source triggers and manual requests meet the same queue admission and concurrency checks. A run is an ordinary turn in its saved agent conversation, with durable history and original request identity. [CHANNELS](CHANNELS.md) explains how verified replies return to that conversation.

## Sandbox

OpenAI-hosted execution belongs to the core. Rocoto's commissioned custom compute broker runs bounded display scripts and custom connector reads. [The display SDK](../sdk/TILES-AND-TABS.md#scripts-and-schedules) owns input snapshots, job limits, cancellation and cleanup receipts; a vertical supplies the source contract and publication check. Shared services own the ledger and sandbox rather than copying a vertical implementation.

## Limits and identity

The common ledger bounds each workspace to 48 agent wake turns, 500 screen steps and 200 outbound sends per UTC day. A wake grants at most $1 across its root and children. Wake concurrency is one per workspace and two per host. Stable effect identities make quota reservations idempotent; an uncertain external request retains its reservation.

Stop, expiry, participant removal, workspace revocation and source-scope changes withdraw current authority. A worker keeps its slot while remote cleanup remains uncertain. Provider money, message-count admission, transfer bytes and source resources are separate enforceable limits. [Cost safety](../../docs/COST-SAFETY.md) records their exact commissioning boundaries.

Access sessions, public MCP keys and managed-session MCP leases are distinct grants. Private credentials stay outside model-visible histories. [MCP](MCP.md) describes the external read door; [CONNECTIONS](CONNECTIONS.md) describes accounts the workspace uses.

## Self-description

`get_harness` derives its vertical, declared roles, recipes, data sources, tool lists and capability facts from the installed manifest, sealed registry and current workspace services. The generated table in [README](../../README.md#existing-framework) is the shared currentness index. A capability's source proof and a host's publication receipt remain separate facts.

## Questions and approvals

Clarification requests keep their saved request identity, answer schema and owning conversation. Provider-action approvals and ordinary questions use the shared router while retaining their distinct authority. Each completion rechecks the actual actor, current request and scope; a stale browser response cannot approve a replacement action. Private credential requests use their own protected input path.

## Chat page

The shared page provides Organization, recent chats, files, agent classes, settings, message history, clarification and approval controls. Messages retain author and channel; a browser reload reconnects to the saved run. The SDK mounts a vertical's tabs and assets through explicit lifecycle hooks. Source-specific drafts, evidence and viewers live with their vertical. [TILES AND TABS](../sdk/TILES-AND-TABS.md) owns that contract.

## Takeover progress

The takeover’s source work and publication acceptance are complete. Both public origins passed acceptance on managed release `99430164d577d95261eb7d07e1d8a0bb62ca495a`; `/healthz` exposes each current release. The hosted protocol proof, ordinary Organization turns and saved files pass; both pages pass browser acceptance at 1600 and 390 pixels. Retail retains all nine tiles and its canvas, and its transient weather failure recovered. An actual stock child completed `get_harness` through the deployed managed MCP door; Stop withdrew that access and left the root and child quiescent. The [rollout receipt](../../docs/HISTORICAL-RECORDS.md#record-15) records the live acceptance. CI passes for release `99430164`; the final release commit still requires CI and approval for the protected-main merge. [Deployment](../../README.md#run-the-bare-workspace) owns current host facts.

The dated builder history below records the order of verified source and host changes; the original handover and follow-up wording remain in [the frozen record](../../docs/HISTORICAL-RECORDS.md#record-26).


The ownership gate is open. The three landed builds are `27366068` (scripts), `f65ef4cd`
(presentations) and `dc2819fe` (self-description), followed by handover receipt `4636ca92`.
The first work package connected their routes, prompts, ownership, MCP reads and write locks,
then verified and published the retail development service. Subsequent work followed the handover
order; the completed source and host checkpoints are retained below.

The staging sandbox retention upgrade passed its isolated proof with zero provider and model calls,
then installed at 17:34:39 UTC on 11 October. Its [receipt](../../docs/HISTORICAL-RECORDS.md#record-8)
records the exact source closure, cleanup checks, rollback location and unchanged application/media
service processes. Script runs on new events were enabled with the subsequent application publication.

The first visual pass covers the shared sidebar and retail tiles/canvases at 1600 and 390 pixels.
Long mobile tile titles use two lines. The shared setup notice and disabled-action tooltip use
the selected agent's own readiness; unrelated workspace observations leave its notice visible.
The repository gates passed with the exact locked frontend dependencies and isolated MCP 1.30.
The full repository test target reached an inherited backend test-double failure and a legacy live-provider test whose model download exhausted disk. The disposable database and generated download were cleaned up; an offline backend rerun excludes that live-provider test. Harness integration and UI verification are separate and passed.

The [tracker script proof](../../docs/HISTORICAL-RECORDS.md#record-9) ran the final source closure on staging: 2,000 synthetic events, 839 KiB across seven input parts and four periods in 1.070 seconds. All sixteen recorded source hashes match the tested checkout; broker, container and temporary state cleanup are confirmed. Model and provider calls: zero.

Step 1 published `bbec6136` at the existing retail origin. The [served receipt](../../docs/HISTORICAL-RECORDS.md#record-12) verifies current script bytes, all nine tiles and a canvas at 1600 and 390 pixels through a revoked-after-use read-only session. Event-driven script runs are enabled after the retention upgrade. Step 2 moved all 1,020 tracked files with byte and mode verification; import, packaging and link repairs are complete. Focused Python, storage, broker-custody, desktop/mobile browser and documentation checks passed before the retail release migration.

Step 2 keeps the retail service, state, origin and installed artifact paths stable. The release builder selects shared code and only the active vertical, including the maintained retail read closure. Fresh subprocesses use the same bootstrap as the host. Installed broker and worker implementation bytes remain identical to the first published work package.

The move preserves the existing repository lint/type baseline scope. A separate whole-harness Ruff check now catches syntax errors and undefined or unbound names without rewriting SHA-pinned helpers or absorbing inherited formatting findings into the baseline.

Step 2 validation: `make gates` passed, including the unchanged Ruff/mypy baselines and the new harness syntax/name check. Browser regression checks cover both retail and a different vertical, all published canvas families, and desktop/mobile fit. Historical commissioning tests use the exact old broker fixture while the current broker remains unchanged.

### Required runtime — Peter’s follow-up, 11 October

The product runtime is the managed OpenAI Agents API with OpenAI-hosted execution. This supersedes the handover’s conditional core choice. Rocoto owns its workspace mechanics, authorization and SDK, while OpenAI owns the agent core and its updates. The former app-server runtime is retained as migration material; both deployments now use the managed core. Peter confirmed proceeding with managed Agents API using its current regional availability; EU access does not gate this work. The earlier request remains a prepared record, and further submission work is deferred. Peter supplied the application key through Rocoto’s designated vault. Read-only checks of managed sessions and the configured model both returned 200; these checks created zero sessions and made zero model calls. The 13 October managed protocol and ordinary Organization/file checks establish live hosted execution; the completed application acceptance is listed above.

Step 2 published `a1a7990f` after Peter’s explicit staging approval. The [served relocation receipt](../../docs/HISTORICAL-RECORDS.md#record-13) records the exact artifact, unchanged runtime identity, source hash, nine tiles and viewed Visitors canvas at desktop/mobile sizes. The temporary read-only QA session was revoked.


Step 3 checkpoint: shared stores, tool registry, input providers, HTTP host composition and opaque admission are separated from retail. The layer audit now enforces **zero runtime/shared imports into a vertical**. A copied artifact containing runtime, mechanics, SDK and bare—but physically no retail folder—starts the real backend and exercises Organization, files, membership revocation and MCP. Independent review found and fixed nested participant admission losing its original actor after an await. The [extraction receipt](../../docs/HISTORICAL-RECORDS.md#record-11) records the tests and remaining page/mechanics work; it is a source checkpoint, not a managed-runtime publication.


Managed migration checkpoint: the combined offline suite passes 78 tests, including the
actual conversation bridge, real workspace accounting, MCP authority, recovered child
history and a full fake-HTTP protocol proof. The immutable synthetic live proof was
prepared on rocoto-staging and passed its dry-run. Automatic approval review required
Peter’s explicit spending approval and private vault-key transfer; those steps were completed before the 13 October monitored proof.
The browser shell now accepts arbitrary declared agent classes and mounts retail Home
through the shared tab SDK. Sixteen navigation cases passed, including the bare manifest;
the later physically bare artifact and browser proofs verified the completed retail UI extraction.


Shared mechanics checkpoint: the bare artifact and shared page load without retail files;
capabilities are selected by the vertical, and disabled capabilities construct no provider,
poller or store. Verified WhatsApp/Viber replies enter the common signal dispatcher and
outgoing messages use its shared daily send limit. Durable standing grants, timers and
signed webhooks retain original actor authority through restart. Shared displays carry their
owning agent and use the SDK's sandbox, ledger, scheduler and renderer. The managed core
adapter passed offline protocol, accounting and authority proofs before the approved live proof. Optional email and voice use the same conversation, current-account and spend admission; signed reply correlation, voice completion and message delivery have focused integration proofs. Their live providers remain governed by deployment commissioning.

Source verification (11 October): the managed runtime, saved Organization authority, optional email/voice admission, signal sources, scoped message spending, role funding and artifact recovery have focused offline proofs. The maintained player passes its real browser/database checks with canonical retail addresses. The manufacturer fixture builds and starts from one vertical folder, without retail or the separate orders checkout; its company-bound SQLite reads and Orders tab are exercised. Repository backend tests passed 849 cases initially; the four renamed-route/fixture failures were repaired and their 17-case maintained-read/player suite passed across the focused reruns. The prototype API passed 460 tests and the vertical contracts passed 141. The repository gates pass; Copper (249 tests and 49 subtests), camera commissioning (85) and Cedar connection proofs (162) pass. The broader repository command exposed seven fiscal-store fixture failures when its fixed due date passed; a pinned fixture clock preserves the dated scenario and all nine fiscal tests pass. Live managed execution and publication were subsequently verified on 13 October, with final application acceptance tracked separately. Legacy harness tests that exercise already-retired archive workflows are tracked separately from these current contracts.


Final boundary checkpoint (11 October): canonical manifests, shared page headers and settings, retail-owned business profiles and resource scope, and saved route/asset aliases are integrated. The final composition selection passes 61 tests; retail scope mutation tests pass 33, including current-member withdrawal after a committed write. All 65 application tool-test files and 169 repository-tool tests pass. `make gates` passes. The eight commissioned helper modules match their published bytes. Bare Home and Settings were viewed at 1600 and 390 pixels with the retail folder absent. These source and local browser receipts preceded the approved live managed proof and both target publications on 13 October.

The $1 hosted proof was approved and staging access restored. OpenAI rejected session creation on `spend_control` with “Session budget configuration is not enabled”; [the receipt](../../docs/HISTORICAL-RECORDS.md#record-14) records that rejected enforced-mode attempt. Peter’s subsequent monitored-spending approval enabled the successful replacement proof. Seven obsolete execution adapters are now retained under deployment migration material and excluded from both application artifacts; every historical test/assertion/proof is preserved.

First bare commissioning on 11 October published `20e7d782` at `harness.soyrocoto.com`, with dedicated service/state, valid TLS, the installed Rocoto managed API credential and desktop/phone workspace checks. [The receipt](../../docs/HISTORICAL-RECORDS.md#record-10) retains that artifact, DNS and revoked QA access. Both origins subsequently moved to the managed release listed above. Support case 17021543 retains the session-budget request; Peter’s 13 October approval permits monitored execution with possible overshoot while that provider feature remains unavailable. The internal demo allocations record spending authority; provider billing and retained reservations remain separate accounting facts.

## What the builders needed


The builders are using the same mechanics a customer's long-running agent needs. This record
keeps the observed need, the harness requirement and the evidence of its implementation together.

| Observed need | Harness requirement | Current evidence |
| --- | --- | --- |
| Source names changed while saved company identities and older integrations remained in use. | Prefer canonical contracts, retain explicit one-release readers and prove existing state reopens unchanged. | The [vertical compatibility tests](../tests/test_vertical_compatibility.py) cover new-file precedence, old schemas and keys, and byte-preserved workspace bindings; the [manufacturer artifact proof](../tests/test_manufacturer_fit.py) builds both manifest generations. |
| Another vertical already had company records and needed to join the same host. | Folder-only tools, event vocabulary and tabs use the shared runtime with current company admission. | The [manufacturer fit proof](../tests/test_manufacturer_fit.py) builds and starts a synthetic second business, isolates two company databases and withdraws reads after membership revocation; its [SDK adaptation map](../sdk/README.md#manufacturer-fit) records the remaining image-worker contract. |
| A predecessor was still landing work in the shared checkout. | An explicit ownership handover and a durable completion receipt before another worker writes. | The [handover landing log](../../docs/HISTORICAL-RECORDS.md#record-32) released the checkout at 17:30 UTC. |
| Independent readers mapped different parts before work began. | A recipe can save a map and hand it to a sub-agent with an exclusive file or record scope. | Three read-only audits became three disjoint integration assignments. |
| A conversation resumed after its context was compacted. | Keep ordered checkpoints, decisions, unfinished work and verification evidence in the workspace. | The nine [handover steps](../../docs/HISTORICAL-RECORDS.md#record-32) retain source, deployment and final live acceptance checkpoints; CI and merge approval remain explicit. |
| Slow background work needed an honest status. | A wait identifies the actual run and its latest receipt, rather than inferring completion from silence. | The predecessor's running test process and the landing log were checked separately. |
| Several agents share a small machine and disk. | Admit work against shared CPU, memory, disk and provider budgets before it starts; classify tests that download large models or call paid services. | Heavy verification is serialized; an older backend test exhausted disk through an unexpected 2.2 GB model download, and its scratch was reclaimed after process cleanup. Browser checks pause below 1.5 GB free while other host jobs run; only owned completed scratch is reclaimed. |
| A user's message steers a job while workers are running. | Preserve background work and deliver steering to its owner. | Each delegated integration has a named owner and a retained result. |
| A child's prompt can request a limit without enforcing it. | Bind each sub-agent's tools, model and cost limit to its actual execution grant and shared parent budget. | Named managed roles reserve their declared slots with the parent, enforce read tools and model selection, and keep the original deadline through restart. |
| A failed send can have succeeded at the provider. | Persist admission and outcome receipts; reconcile uncertain effects before retrying. | The messaging outbox and shared signal chain retain uncertain sends for reconciliation. |
| A shared chat belongs to the workspace while a real member starts its turn. | Carry workspace data ownership and the acting member as separate identities through readiness and every tool. | Fixed-chat integration tests cover reads, new agents, edits and revoked members. |
| Installed dependencies differed from the repository pins. | Verify against an isolated runtime with the declared versions and exercise actual transport boundaries. | The isolated environment uses the declared dependencies; managed start, resume and original-request cancellation have transport proofs. |
| A script failed before any observation arrived. | A valid script wake can reach its agent with an empty input stream while current scope and grant checks still apply. | Script-wake regressions cover empty input, consumed observations, expiry and revoked access. |
| An agent was configured before its first matching source existed. | Keep its declared event selection through empty initial data and later edits. | Ready-made agent tests preserve event types and reject unrelated observations. |
| An import and a scheduled job both write a location. | One location lease covers every write in each pass. | Import tests cover observations, media, page data and tracked visits under that lease. |

| Lazy workspace construction could recover jobs before their providers existed. | Install stores, declared vertical providers and the sealed tool registry before scheduler recovery; failed construction cleans up and remains uncached. | The bare lifecycle proof starts without the retail folder and exercises files, Organization and current membership. |
| A nested tool scope could accidentally widen a captured grant after an await. | Carry one opaque admission through every transport and recheck the original actor and grant at effects and completion. | Independent review found the nested-admission case; the scope integration records its regression proof. |
| A provider application reached a human verification challenge. | Save the exact request, distinguish prepared/submitted/accepted states and require a submission receipt before claiming success. | The EU access request is prepared; there is no submission receipt. |
| A managed session keeps an immutable tool configuration across many turns. | Keep its opaque credential stable while separately granting and revoking each turn’s actor, scope and deadline; recover only the original funded request. | The shared managed MCP adapter persists bindings and begins each restart with withdrawn authority. |
| A display outlives the page and the business role that first used its renderer. | Give extensions isolated schemas and renderers, and bind every display to its actual owning agent. | Shared display tests render a second vertical at phone and desktop sizes without retail imports. |
| A provider reports approximate cumulative spending before final billing. | Reserve only additional session authority in the existing workspace ledger; keep uncertain charges held until a final provider receipt. | Fifteen accounting cases cover replay, concurrency, actor/session binding, terminal estimates, final multi-hold settlement and rejected foreign receipts and operator-funded charges. |

| Managed session spend is cumulative and its reported consumption is approximate. | Reserve the workspace allowance first; use a finite provider ceiling where available or explicitly approved monitored cancellation, and retain uncertain charges for reconciliation. | The live monitored proof passes with a 100-cent target and original deadline; provider billing remains unknown and its reservation stays held. |

| A phone reply may be saved before a worker or process stops. | Use the ordinary chat message as the delivery receipt; reconcile the inbox without starting another turn. | The shared message-to-signal adapter covers duplicate events, contact revocation, queue admission failure, restart reconciliation and mutable display names in nine tests, including send revocation during quota admission and fair receipt reconciliation. |
| A queued message can be admitted while its agent is still working. | Preserve the original claimed turn's live authority after marking the message delivered, then end it with the signal. | Reply authority checks the verified event and current contact, participant and workspace throughout the claimed run. |

| A later accounting receipt can be hidden behind older unresolved work. | Advance bounded reconciliation scans fairly; preserve unresolved records for later review. | A ninth saved chat receipt settles after two scans while eight unresolved receipts remain held. |
| A send can lose permission while its quota reservation waits. | Recheck contact, account, spend and host delivery authority after quota admission and before the provider request. | A revocation inside the quota callback cancels the send; 103 focused message and accounting tests pass. |
| A paused agent can still have an admitted hosted request after a restart. | Separate permission to execute from permission to cancel; retain concurrency until the original provider work is quiescent. | Restart regressions cover paused, revoked and expired grants, missing identities and shared concurrency. |

| A verified provider reply arrives after restart before its workspace is loaded. | Verify the signature with only bound provider settings, then recover the workspace; discover persisted inbox work independently of agent jobs. | Three HTTP integration cases cover Viber enrollment, cold signed ingress and restart discovery of pending messages. |
| A catalogue advances while a workspace keeps an installed skill. | Resolve installed capabilities from their exact verified files and immutable receipt; keep catalogue browsing separate from installed state. | Seventy-five package, skill and route tests cover retained revisions, owner scope, hostile archives and installation without a local agent core. |


| Builder need | Harness requirement | Evidence or implementation |
| --- | --- | --- |
| An external step may outlive a process or lose its response. | Retain its durable effect identity and cleanup receipt; reconcile that original operation before replacement work. | Signal steps, endpoint effects and display-script recovery preserve uncertain requests and concurrency. |
| A later source generation can change the meaning of a saved job. | Capture the original input bytes, source incarnation, revision and scope, and retain them while the job remains unfinished. | Retail script signals preserve immutable snapshots through publication and cleanup. |
| A complete job spans interruptions and changes to its instructions. | Save the recipe revision, prerequisites, progress and evidence in the owning conversation. | Recipe runs retain the original job and use optimistic revisions for step completion. |
| An approval response can arrive after identity or action changes. | Route it through the shared current-actor admission and exact saved request identity. | Question and provider approval routes share the conversation fence while preserving their distinct grants. |
| A hosted file can arrive after the originating turn resumes. | Persist the provider session and artifact receipt, then recheck current owner and conversation scope at download and publication. | Managed files retain remote identity and host publication receipts. |
| A reply and its cost can outlive a provider connection attempt. | Keep a durable provider message identity and account-bound money reservation so the original chat resumes and uncertain sends reconcile. | Email/voice composition uses the same inbox, outbox, signal ledger and private capability-spend holds. |
| Several workers edit the same system and read different descriptions. | Keep one declared living-document index, dated evidence, explicit file ownership and a test for unindexed source documents. | The twelve-source document gate holds the common reading path. |

| A process can restart during a bounded delegated job. | Persist the first wall-clock deadline with its funded request so recovery preserves its original time allowance. | Named managed read jobs retain their deadline, prepaid slot and original session across restart. |

| An old test can require a workflow retired before the current change. | Compare failures with the recorded source revision and keep replacement behavior under an explicit contract. | A clean `039442fd` snapshot reproduces six legacy control/archive failures; current managed and maintained-player boundaries have separate proofs. |

| A renamed page header worked in the shell while several modular routes still required its predecessor. | Define one request contract and use it at every route boundary, with explicit compatibility precedence. | Seventy-four canonical header and route checks cover new/legacy headers, duplicate rejection, current sessions and scope. |
| A deployment can carry both canonical and legacy settings during migration. | Read the canonical key first, preserve deliberate empty values, and remove every credential alias before execution. | Retail settings tests cover precedence and subprocess environment filtering; generic configuration lives in the shared example. |
| A host session or human approval expires during a long job. | Retain prepared work and its exact next action, distinguish access from money authority, and resume only after the original gate is satisfied. | The restored Tailscale identity resumed bare commissioning; the approved managed proof retains its bounded request across the provider-feature rejection. |

| Moving a vertical can hide its UI while shared state or middleware still carries its rules. | Verify physically absent vertical bundles, exact aliases through real middleware, and current authority after a mutation changes its own resource revision. | Bare, canonical scope-mutation and saved-asset regressions caught and fixed the remaining route and ownership leaks. |
| A managed conversation retains provider history after its visible resource selection changes. | Bind history to the opaque admitted scope, preserve that identity through every descendant and filter replay by the same identity. | Independent review reproduced scope-switch and grandchild history gaps; the managed boundary records their targeted regressions. |

| A documented provider control is unavailable to the commissioned account. | Keep the exact rejected request and reserved funds; retain the support request and resume under provider enablement or a separately recorded money decision. | OpenAI rejected the 100-cent session cap before returning a session. The original receipt remains intact; Peter approved monitored spending and the replacement live protocol proof passed. |

| A restricted subprocess inherits the executable but loses its matching interpreter libraries. | Preserve the runtime’s required library search path while keeping application credentials outside the child environment. | CI used Python 3.12.14 with its configured library path and 3.12.3 without it; the corrected environment and packaged retail fixture closure pass five focused database-backed checks. |

| A provider feature blocked the approved managed core, and the owner accepted a spending target with possible overshoot. | Persist the exact money decision, distinguish target from provider-enforced ceiling, and preserve original funding, deadline and cancellation authority through recovery. | Peter’s 13 October instruction authorizes monitored managed execution; token estimates remain separate from settled provider charges. |
| A second vertical’s provider workflow was mistaken for a missing shared primitive. | Test the declared workflow against existing folder-owned tools and separate shared authority from provider commissioning. | Manufacturer generation is a host step; scoped image reads and reviewed parent effects fit existing families, while direct multimodal child inputs remain an explicit future extension. |
| A hosted session provisions asynchronously, waits for tools, and distinguishes an absent cursor from a null cursor. | Keep the original request while waiting, recognize provider states, omit absent fields and reconcile cleanup before retrying. | The 13 October V5 protocol proof verifies durable effect replay, stock-child history, artifact delivery and cancellation; earlier failures and V3 cleanup reconciliation remain recorded, with final billing held as unknown. |
| A hosted session fails after an awaited operation and its public error carries little context. | Keep bounded private diagnostics tied to the original request and retain safe operator failure fields. | The managed runtime retains stage and provider context privately so a live failure can be diagnosed without copying credentials or private records into chat. |
| A provider's session-update fields differ from its creation schema. | Validate each operation against its own contract and prove a follow-up on the same session. | The follow-up configuration fix uses the managed update schema; Organization/file and browser checks pass, and the actual stock child completed its managed MCP read before Stop withdrew access. |
| A retained workspace's demo allowance expires before a new runtime is commissioned. | Renew the original total through a private, scoped authorization event after every old request is reconciled; preserve all prior costs and receipts. | Retail's original pool now has a USD 5 total managed-chat allowance, including USD 0.000512 historical cost; 82 focused checks cover replay, expired authority, scope/model, concurrent renewal and closed-request preservation. |

| A stock child attempted a coordinator function instead of its inherited MCP read. | Name the advertised child tool namespace in delegated work and return unavailable reads promptly. | A real stock child completed `mcp__rocoto__get_harness`; shared coordinator instructions now distinguish parent function tools from inherited MCP reads. |
