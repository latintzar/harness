# MCP writes target design

Dated design checkpoint, 9–11 October 2026, retained from the original Vitrina Harness. This is a retail target contract. The current public implementation and managed OpenAI runtime are described in [Architecture](../../docs/ARCHITECTURE.md), [MCP](../mechanics/MCP.md) and the [SDK](../sdk/README.md). Historical “current” statements below refer to their original checkpoint; each implementation or deployment needs its own present verification.

Status: **target design**, 2026-10-09 planning checkpoint. This document authorizes
no additional public tools or execution. [PUBLIC-MCP.md](../../docs/HISTORICAL-RECORDS.md#record-29) owns the
current seven-tool, read-only transport and historical HTTPS proof. The expanded
interface below requires implementation, migration, tests and a release receipt.
[Architecture](SERVICE-BOUNDARY.md) owns the host and worker design;
[Objects](../../docs/HISTORICAL-RECORDS.md#record-27) owns the shared business vocabulary.

## Product boundary

The customer-facing service is **Rocoto Real-time**. MCP is another access channel
to the same Rocoto hosted harness. A customer can type in Rocoto or ask their own
agent to talk to Rocoto on their behalf. Both channels reach the same persistent
Organization, Camera, Tracking and Action Agent conversations, questions,
proposals, configuration and business results. Hosted conversation access is an
essential target capability, not an optional adapter for a separate product.

The external agent delivers the customer's request and authorized replies, then
returns Rocoto's results. Rocoto remains responsible for its hosted agent turns,
monitoring, processing, subscriptions and authorized actions. Saved monitoring
continues server-side independently of MCP connections and external-agent uptime.
An idle conversation is a durable record, not a continuously running model
process. Its next admitted message, schedule or subscription starts only the work
permitted by its durable execution policy. An edit-only request can change a
Tracker or script without waking the target Tracking Agent. A configuration
conversation may use a bounded turn of its own to help make that edit.

Rocoto Skills teach the external client the available business concepts, required
inputs and how to continue the right hosted conversation. Messages can carry text
or supported typed business intent. The harness routes ordinary reads and fully
specified operations to deterministic services when appropriate; using the same
harness does not require a model turn for every read or operation. Internal
`ConfigurationService` validates and activates changes for every channel. It is
an implementation boundary, not a separate customer-facing configuration bypass.

Customers can use their own external Action Agents to act on processed alerts.
Rocoto's harness helps configure effective Trackers and custom post-tracker
scripts, then delivers their admitted results. Hosted Action Agents are an
available execution choice, not a dependency or required fee tier for external
alert delivery. Usage for actual Rocoto configuration, observation, processing
and delivery remains separately metered and bounded.

Participant phone chat is another consumer of the same canonical processed alert.
Every Tracking Agent delivers its alerts to its own participants' verified phones
by default, over Viber, SMS or WhatsApp where commissioned, and a phone reply
continues the same hosted conversation an MCP caller reads. The host owns that
delivery; it needs no hosted Action Agent and no external webhook.
[Channels](../../docs/HISTORICAL-RECORDS.md#record-24) owns its records, routes and spend bound.

Public tool names, schemas, descriptions, UI messages, errors and customer Skill
outputs use **Rocoto Real-time** and business terms. Provider/model/hardware and
execution-infrastructure names stay in internal engineering records. Customers
describe their business and desired outcomes; the harness owns technical
configuration. Public responses are explicit allowlisted DTOs, not serialized
internal records with a few fields removed. This naming/data boundary requires
tests before the expanded API ships.

The internal architecture uses shared PostgreSQL-backed typed services, an outbox
and isolated execution. MCP is the access interface, not the datastore or message
broker. Kafka remains optional internal transport. Webhooks, polling or negotiated
notifications deliver updates to external agents within their actual client
capabilities; MCP alone guarantees neither a permanent unsolicited stream nor an
autonomous client wake. Deeper historical analysis is a future optional capability
and is not a dependency of this real-time product or its MCP conversation access.

## Records exposed to customers

| Record | External meaning and authority |
|---|---|
| Tracker Core | Host-maintained mandatory observation capabilities and their effective revision. The host protects required capabilities against removal or disabling through prompts, skills, scripts or client edits. |
| Tracker Add-on | Versioned business interests layered onto Core, with observation vocabulary, criteria, source scope, schedule, budget and activation state. Requirements discovery supplies the supported business fields. |
| Event / processed alert | Canonical event ID and revision, source lineage, processing revision where applicable, time and evidence. Origins distinguish observation, business-source fact, processed alert, review and explicit dataset-reference evaluation. Internal agents/subagents and external subscriptions reference the same admitted alert, with recipient-specific field filtering. |
| Visit / observed item / product | Exact source-scoped track and object relationships, catalog-link status and price provenance. Candidate identity, estimated basket value and settled transaction value remain separate fields. |
| Core Tracking Module | Maintained business processing after admitted Rocoto Real-time events, with its fixed tile/canvas contract. Product-owned implementation and mandatory capabilities remain protected. |
| ModuleInstallation | Tenant/location deployment of an allowed maintained module revision, including source bindings, effective settings and health. |
| CanvasRevision | Dashboard UI and its typed data bindings. A Core module's maintained canvas is protected; a customer Tracking Agent can author/revise its own canvas through the isolated validated publisher. |
| Real-time configuration / analysis run | Public configuration revision, desired/active status, business scope, coverage and admitted result references. Technical execution settings remain internal. |
| Business / location / camera context | Current scoped configuration, capture-clock assumptions, zones, privacy/calibration records and source health. Private camera credentials remain in the secret store. |
| Agent / conversation / run / question | Persistent operating objective, conversation history, execution attempt and durable input request. Each remains addressable after MCP reconnection. |
| Processor / subscription / delivery | Versioned post-tracker computation, event selection and delivery records with grants, execution limits, retry state and destinations. Processing activation, target-agent wake policy, participant phone delivery and external delivery activation are independently recorded. |
| Participant / recipient binding | A Tracking Agent's participants (its creator plus the company members that creator adds) and, per participant, the channel, enabled switch, masked verified address and last delivery state. Full numbers and security codes stay in the private host form. |
| Execution policy | Revisioned interaction mode and independent observation, script-processing, target-agent wake, participant phone delivery and external delivery settings. Edit-only access does not imply a target run or a phone message, or override mandatory Core obligations. |
| Usage / permission | Host-reserved and settled usage, remaining granted budget and effective capability scope. Read access to usage does not grant budget changes. |

Mandatory Core coverage is reported per camera and capability: `ready`,
`degraded`, `unknown`, or `unavailable`, with reason, last valid observation,
coverage interval and missing prerequisites. Missing or stale input yields that
state, not a numeric zero or an inferred successful observation. A customer can
change permitted settings through the shared configuration service while Core
obligations remain enforced. Versioned capability definitions live in host policy, not in a
client-provided prompt.

## Tool surface

The names below are proposed application tools, distinct from MCP protocol RPCs.
Company, environment and principal come from authentication; client arguments
select already-authorized entities. The current read names, listed in
[Public MCP](../../docs/HISTORICAL-RECORDS.md#record-29), remain an explicit compatibility
surface until a versioned migration is commissioned.

| Surface | Proposed tools | Behavior |
|---|---|---|
| Discovery | `get_realtime_capabilities`, `get_realtime_requirements` | Enabled features, business requirements, required Core coverage, schemas, effective grants, limits and actual readiness. |
| Hosted conversations | `list_agent_conversations`, `open_agent_conversation`, `get_agent_conversation`, `send_agent_message` | Find, create or continue the same persistent Organization/Camera/Tracking/Action conversation used in Rocoto. Text and supported typed business intent enter the hosted harness with attributable caller identity and an explicit interaction mode. Edit requests default to no target-agent wake. |
| Runs, results and questions | `get_agent_run`, `get_agent_result`, `list_agent_questions`, `answer_agent_question`, `cancel_agent_run` | Durable status/results, nonsecret questions, authorized answers and cooperative cancellation of a specific run. These are application records, independent of an MCP session. |
| Proposals and configuration | `get_agent_proposal`, `respond_agent_proposal`, `get_realtime_configuration`, `get_realtime_operation` | Review the harness's exact proposed change, supply an authorized approval/decline or requested correction, and read saved versus active state, including separate processing/wake/delivery policies. The harness performs validation/activation through internal ConfigurationService; approving an edit does not implicitly run its target. |
| Existing reads | `list_cameras`, `get_business_profile`, `get_business_context`, `list_camera_context`, `list_monitoring_profiles`, `list_tracker_events`, `get_tracker_event` | Current read compatibility; future DTO versions expose business fields and bounded source-time/event queries. |
| Tracker reads | `list_trackers`, `get_tracker`, `list_tracker_runs` | Core/Add-on classification, observation interests, health, scope and revision. **Current:** all three are listed over public MCP beside the existing reads. Each row carries the tracker's definition, its schedule with the state of the tracker service, its current output (tile and canvas with their revision) and its last run; `list_tracker_runs` reads a location's run rows. They read the grant principal's own tracker records inside the principal's present access: a definition with its location, an output with the cameras it read, a row of a recording with that recording. |
| Tracker changes | `save_tracker`, `remove_tracker`, `set_tracker_schedule`, `run_trackers`, `install_core_trackers` | **Current:** tools of the app's hosted parent conversation, and that conversation is their one road today. Public MCP lists none of them, so a customer's own agent reads trackers and changes none. Its road to these changes is the proposed `send_agent_message` of this contract (a message into that conversation under its proposal and grant policy), which has no implementation yet; the decision between that road and a write scope of its own is open. A Core Tracker key answers that Core Trackers are locked; a location's schedule and the recipe install belong to the Organization conversation. |
| Module and canvas reads | `list_modules`, `get_module`, `get_canvas` | Maintained business modules, dashboard tiles/canvas and typed data bindings, with Core/customer ownership explicit. |
| Business projections | `get_visit`, `get_product`, `get_usage` | Saved results, provenance and bounded history/as-of queries. Media access retains its source grant. |
| Processing and delivery | `list_processors`, `get_processor`, `list_subscriptions`, `get_subscription`, `get_delivery` | Read the same hosted post-event definitions and delivery health, including each participant's phone delivery state for a Tracking Agent's alert. Creation, revision, pause or removal enters the responsible conversation and its proposal/grant policy. External consumers can subscribe to processed alerts while hosted target-agent wakes are disabled. |

Capability-specific helpers can support harness workflows without starting a new
conversation turn for each read. They operate on the same canonical records and
authorization services; a second standalone rules/configuration product is outside
this contract. These tool names do not imply MCP protocol `resources/*` handlers
are already registered. Business resources are reachable through the typed reads;
optional resource bindings require their own advertised capability and tests.

### Public DTOs and schema versions

These are **target schema contracts**, not responses already served by the current
endpoint. Each public DTO declares a version such as
`rocoto.realtime.requirements.v1`; changes have an explicit compatibility policy.
Unsupported fields/versions produce bounded business-readable validation errors.
Opaque existing company/conversation IDs retain their registered identity.

| DTO | Public fields and states |
|---|---|
| Capabilities | `service_name: "Rocoto Real-time"`, `schema_version`, supported schema versions, feature availability, enabled conversation kinds, enabled phone channels (`viber`, `sms`, `whatsapp`, each with its actual readiness), effective grants, limits and polling/delivery guidance. Capability presence describes actual enabled behavior, not the roadmap. |
| Requirements | `requirements_revision`, authorized scope, business-profile revision, required/supported interests and outputs, required fields, allowed values, missing inputs, coverage prerequisites, approval and budget requirements. |
| Message / business intent | `conversation_id`, expected revision, idempotency key, interaction mode (`edit_only` or an explicitly allowed operational mode), optional target reference and text or supported typed business intent: authorized location/camera references, business goals, observation types/criteria, requested outputs, schedule and granted budget reference. The harness binds caller/delegation identity. Technical implementation settings and secrets stay outside the public intent. |
| Conversation / run / question | Stable canonical IDs, conversation kind and revision, attributable messages, run state/results, durable question schema/revision and allowed response actions. Each message names the channel it arrived on: the app, a delegated MCP caller, Viber, SMS or WhatsApp. A Tracking conversation lists its participants with channel and delivery state, with addresses masked. Current scope controls every read and answer. |
| Proposal | `proposal_id`, originating conversation/run, revision/hash, normalized intent, expected configuration revision, exact changes, source/output/destination scope, dependencies, estimated cost with basis, granted ceiling, expiry and required approvals. State: `ready`, `needs_input`, `needs_approval`, `unsupported` or `expired`. |
| Validation | `validation_id`, proposal hash/revision, requirements/policy revisions, checks, remaining inputs/approvals and expiry. Validation evaluates existing grants and grants no new authority. |
| Configuration | `configuration_id`, desired revision, active revision, business scope/interests, execution-policy revision, separate observation/processor/wake/delivery state, output bindings and per-capability readiness. Activation state: `pending`, `active`, `partially_active`, `needs_attention` or `paused`; reasons use business terms. |
| Operation | `operation_id`, state, revision, requested/started/completed times, progress/coverage, question/output references, cost/usage and `poll_after_ms`. Actual state is distinct from a successful configuration save. |
| Processed alert / delivery | Canonical `event_id`, `event_revision`, alert type/state, processing revision, scoped input/evidence references, relevant source/business times and any correction/resolution link. Each recipient gets a separate stable delivery ID, policy revision and filtered payload; transport metadata and private fields need not be identical. A participant phone delivery is keyed by participant and alert revision and carries text and a link into Rocoto; its state comes from actual provider acknowledgments. |
| Error | Stable code, readable message, field reference where relevant, retryability and a public support reference. Internal exception text, paths, provider identifiers and credentials remain in restricted audit logs. |

### Edit-only access and execution policy

Interaction and execution are separate durable settings. The host records the
caller/conversation's allowed interaction mode and the target's policy revision.
An edit request defaults to **no implicit target-agent wake**. An `edit_only`
setting permits authorized Tracker, script, output and subscription configuration;
it does not grant “run now” or enable event/scheduled target-agent execution.

The target policy records observation activation, post-tracker script activation,
agent wake selection, participant phone delivery and external delivery activation
independently. A supported setup is: observation active, custom script active,
target Tracking Agent wake disabled, participant phone delivery on, external alert
subscription active. Participant phone delivery is on by default for a Tracking
Agent. An edit-only request leaves it as saved and produces zero phone sends. Core-required observation and maintained
processing remain protected by host policy; selecting edit-only cannot remove
mandatory capabilities. Actual spending still requires current budgets/grants.

Configuration help may use its own bounded run in the same hosted harness. That
run can author or revise a script and produce a validated proposal; it is not a
background reasoning run of the configured Tracking Agent. Its audit distinguishes
configuration run, target, resulting revision and the absence of target wake.
Deterministic typed edits require no interpretation turn. Promotion, saving,
validation and permitted activation each retain this distinction.

Turning target-agent wakes on is a separate explicit policy change with its own
scope and budget check. Dispatch and scheduled claims recheck the latest policy;
queued wakes created under an older enabled revision cannot start after disable.
An already running target turn retains its recorded state and explicit Stop
behavior; an edit must not silently cancel it or claim that it never ran.

### Harness configuration transaction

1. The external client discovers capabilities/requirements and resolves or opens
   the responsible Rocoto conversation. `send_agent_message` supplies the user's
   text or typed business intent with an expected conversation revision and
   idempotency key. It creates an attributable entry in the same hosted history
   a Rocoto UI request would use; it does not create an external shadow thread.
   For edits, the effective interaction mode prevents implicit target-agent wake.
2. The harness chooses the bounded work needed. Supported explicit operations
   can use deterministic handlers; interpretation or ambiguous intent can use an
   admitted configuration turn. This authoring/help turn is distinct from the
   target Tracking Agent's wake policy. Internal ConfigurationService resolves scope, preserves
   required Core bindings and builds the normalized, reviewable change. Missing
   facts become durable questions in that conversation. Read-only requirements
   discovery and deterministic validation do not themselves start paid analysis.
3. Validation binds the proposal hash, requirements/policy revision, source
   permissions, privacy/readiness prerequisites, dependencies, resource ceiling
   and required approvals. The user reviews meaningful changes/cost when policy
   requires it; already-delegated routine changes remain within their grant.
   `get_agent_proposal` exposes the same proposal as the Rocoto UI.
4. `respond_agent_proposal` records an authorized decision or requested correction.
   The harness applies an approved change through ConfigurationService using a
   current validation receipt, expected configuration and execution-policy revisions,
   idempotency key and required approval receipt. It rechecks authority and atomically saves the
   revision, audit and activation intent. A changed proposal, requirements or
   permissions invalidates prior validation; a tool caller's affirmative answer
   never creates approval authority it lacks.
5. Conversation results and `get_realtime_configuration`/`get_realtime_operation`
   distinguish saved, pending, partially active and active state. The last working
   revision remains available while the change is prepared. Activation reports
   acknowledged capability coverage, with unavailable inputs explicit. Budget
   reservation precedes paid analysis or independently authorized external effects.
   Completing an edit emits configuration/audit results, not a target-agent wake.
   Observation/script activation and destination delivery follow their separately
   approved policies.

The UI, external-agent inbox and scheduled hosted turns all enter this same
harness and policy boundary. Routine deterministic work does not require a second
model to restate the request. When interpretation is needed, its usage belongs to
the admitted Rocoto run and is visible in that run's budget and result.

A Tracking Agent can author its own script/tile/canvas through the isolated
validated publisher, including optional design-subagent help. An external agent
with the corresponding authoring grant may deliver a supported artifact into the
same conversation/publication proposal. Product-owned Core module code and canvas
remain protected. Ordinary business configuration changes supported parameters;
script/code publication is a separate scoped capability.

## Hosted conversations and durable operations

`open_agent_conversation` accepts an authorized target and `agent_kind`
(`organization`, `camera`, `tracking`, `action`). It resolves the canonical
permitted conversation or creates one under the same host creation policy as the
UI. A new MCP connection resumes that conversation's history. Promoting an
ordinary chat retains its chat ID/history. The harness remains responsible for
context, durable questions, tools, schedules and current source permissions.
Dormant conversations hold durable records rather than a running model process.

An initial work response carries `conversation_id`, `state`, `revision`,
`poll_after_ms` and question/output references, plus `run_id` when an actual
hosted turn is admitted and `operation_id` for configuration/processing work.
A deterministic edit does not fabricate a target-agent run; a configuration turn
identifies its own purpose and target separately. Application IDs persist independently
of MCP request IDs and connection sessions. Bounded run/operation polling is the
compatibility baseline; modern protocol tasks can adapt it when negotiated.

The target application lifecycle is `queued → running → completed|failed`, with
`waiting_input`, `waiting_approval`, `cancel_requested`, `cancelled` and `expired`
where applicable. These are Rocoto states; protocol-task adapters map them
explicitly. Polling resumes after a network break. Execution recovery records
interrupted work and actual commits before retrying; reconnecting the transport
never independently creates a duplicate paid run.

Every message, answer or proposal decision that can create work supplies a
client-generated idempotency key. The host scopes it to company, acting
principal/delegation, operation and canonical request hash. Same key/same body
returns the original result; same key/different body conflicts. Conversation
writes carry an expected revision. Concurrent turns are serialized or return a
clear busy/conflict state so the UI and external agents share an ordered history.

Questions retain `question_id`, `conversation_id`, `revision`, kind, schema,
options, expiry and answer state, bound to their configuration `operation_id` or
actual `run_id` as applicable. `answer_agent_question` validates current source access,
question revision, actor and expected answer type. Decline, cancel and expiry are
distinct outcomes. A human approval cannot be supplied by a tool caller lacking
the required approval authority.

Cancellation of a run records durable intent and propagates to its descendants,
queued jobs, charged reservations and delivery admission. Commit/effect fences
prevent new unauthorized work after cancellation or revocation. Already admitted
external work can still finish and incur usage; status reports actual usage and
completed effects. Cancelling an HTTP request or closing an MCP session does not
cancel a durable run. Cancelling a conversation turn does not implicitly disable
saved monitoring/subscriptions: pausing those requires an explicit authorized
configuration change through the harness.

## Private input and questionnaires

Camera passwords, credential-bearing RTSP URLs, API keys and connector tokens
are collected through an owner-scoped, expiring HTTPS secure-input URL on the
Rocoto origin. The request binds company, actor, purpose, target and the actual
configuration operation or run; the private browser verifies the current signed-in identity before accepting values.
Secrets remain outside MCP arguments/results, conversation history and logs.
Completion returns configured-field names and a receipt, rather than values.
URLs carry opaque request handles; URL possession alone does not establish owner
authority. Secret submission is single-use and protected against replay.

Supported clients may render **nonsecret** questionnaires using negotiated form
elicitation. Secure input may use negotiated URL elicitation, with a visible
destination. Clients without either feature receive the durable question and
private completion link as ordinary tool output, then poll the run. Protocol
elicitation adapts the presentation; the durable host question remains canonical.
This follows the official [form/URL boundary](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation).

## Scripts, subscriptions and effects

Rocoto's harness helps configure quality Trackers and custom post-tracker scripts.
After observations and business-source records are admitted, deterministic
processors handle counts, dwell/queue intervals, POS matching, state projections
and threshold checks. They can emit **processed alerts**, rather than merely
forwarding raw observations. Interpretation or composition can use an admitted
agent turn when selected. Scripts can remain active while target-agent wakes are
disabled, and saved processing/delivery continues after the client disconnects.

A processor revision records input schemas/filters, source scope, code or
registered function hash, output schema, resource ceilings, permitted connections
and authority. Custom code executes in the host's isolated bounded worker with
explicit network/filesystem grants. Saving a script creates a revision; executing
it requires its separate processing activation. Backfill requires a bounded source
interval and budget. Neither activation nor script output implicitly enables a
Tracking or Action Agent wake.

Internal agent/subagent inboxes, hosted schedulers and external subscriptions use
the same canonical domain-event/processed-alert contract: stable `event_id`,
`event_revision`, processing revision, origin, state and evidence lineage. A
subagent remains within its parent's grants. The host commits the alert/revision
and outbox atomically, then fans it out to permitted consumers. This is shared
business identity and semantics, not an assertion of identical private fields or
transport: each adapter filters/redacts to its recipient's current grant and adds
its own delivery metadata/signature.

An admitted subscription records its definition/revision, canonical event filter,
cursor, consumer (`script`, internal agent/subagent, `participant_phone`,
`external_webhook` or granted hosted action), scope, expiry,
rate/concurrency/byte/spend limits and destination grant. The `participant_phone`
consumer exists by default for every Tracking Agent: its destinations are that
agent's participant bindings, its delivery ID belongs to the participant plus alert
revision, and its spend limit is the per-company and per-agent daily message cap. A stable delivery ID belongs to the subscription plus event revision and
is reused on retries. Consumers deduplicate; a correction is a new revision of the
same alert, not a duplicate new incident. Delivery is at least once; external
effect reconciliation remains necessary where a recipient lacks idempotency.
Replay/backfill use bounded cursors. Source and destination authority are rechecked
before dispatch/retry. Publisher-reference labels remain evaluation-only.

### Processed POS mismatch alert

A post-tracker script may join a supported item/visit observation to authorized
POS records under a versioned matching rule and a bounded matching window. A
mismatch requires relevant POS-feed coverage, a valid completeness watermark past
the matching-window end, compatible clock mapping and the rule's item/quantity
association evidence. While the feed is incomplete, late or its coverage/clock is
unknown, the state stays pending/unknown; absence of a matching row is insufficient.
Where completeness cannot be established, the script reports that coverage state
rather than a mismatch. A mismatch reports the rule's discrepancy and evidence;
it does not itself infer theft, intent or an observed transaction amount.

Late or corrected POS evidence recomputes the same case under its scoped stable
correlation key. It appends a correction or resolution revision linked to the
prior alert, with its new evidence and coverage watermark. Internal inboxes and
external webhooks receive that same canonical revision once per logical delivery;
transport retries reuse the delivery ID. Corrected alerts retain history and do
not create a second unresolved incident.

External webhooks use registered HTTPS destinations, SSRF-safe address/redirect
validation, signed timestamped payloads, bounded retries and retained delivery
receipts. Payloads carry event IDs and permitted summaries/evidence references;
private video is fetched separately under its own authorization. Delivery failure
and recovery are inspectable. MCP notifications can serve an active negotiated
client connection; they are not an autonomous wake guarantee. A webhook is useful
only when the customer's registered endpoint actually handles it.

The customer's external Action Agent is a supported, encouraged consumer of
processed alerts and can perform its own authorized reporting or business actions.
External alert delivery requires no hosted Rocoto Action Agent and no hosted Action
tier dependency. Rocoto's published observation/processing/delivery usage remains
accounted separately. A hosted Action Agent is an optional choice when the customer
wants Rocoto to perform that work; it can inspect permitted surrounding evidence,
compose a report or request a separately granted connector action.
Effect grants constrain connection, action, recipients, duration and budget.
Those recipients are people outside a Tracking Agent's participants; an agent's
own participants are reached by participant phone chat.
Visible cash/card actions remain visual observations; settled amount/revenue
requires transaction evidence. Delivery, refunds or purchases never inherit
permission merely because an event, script or conversation mentions them.

## Rocoto Skills for external clients

A versioned **Rocoto Real-time** Skill teaches the external client how to resolve
and continue the right hosted Organization, Camera, Tracking or Action Agent
conversation. It discovers business requirements, gathers permitted context,
delivers text or supported typed intent, answers durable questions within its
authority and returns results/proposals to the customer. The Skill identifies
which canonical thread to reuse, how to select edit-only access and how to poll
configuration work without waking the target agent; it does not ask
the client to reproduce Rocoto's harness, generate technical prompts or maintain
a separate configuration history.

The Skill helps the customer use Rocoto's harness to build and refine Trackers and
custom scripts that emit useful processed alerts. It explains independent
observation, script, target-agent wake and delivery settings, including the
edit-only configuration with active processing and external delivery. It prefers
the customer's existing external Action Agent when that agent can handle the
requested action; a hosted Action Agent is an explicit option, not a prerequisite.

An external agent with a commissioned receiving endpoint can request signed
webhooks for the same canonical alerts used internally. Otherwise it polls or uses
negotiated notifications while connected. The Skill preserves alert IDs/revisions,
coverage and correction/resolution state, including pending POS coverage instead
of claiming mismatch from missing rows. Monitoring and processing continue within
their grants/budgets independently of the chosen delivery channel.

Skills declare dependencies and pinned revisions and may be distributed as
readable instructions plus supported client skill packaging. Installation changes
instructions, not permissions. Customer outputs use the public DTOs, explain
uncertainty and coverage, and distinguish estimated values from settled
transactions. Existing [Skills](../mechanics/skills.py) are host-side instructions; external
packaging/discovery and this conversation-access Skill remain target work.

## Authorization, cost and data limits

- **Identity:** bind the existing company tenant, acting principal, membership
  and delegated client at the host. An agent inbox can accept a scoped service
  identity only with an explicit customer/tenant delegation, allowed conversation
  targets and capabilities. Service identity never substitutes for that grant or
  silently impersonates the owner. Recheck scope on reads, run resume, questions,
  commits and deliveries.
  Opaque IDs carry no authority. Revoking a source also removes its history and
  evidence from subsequent scoped responses and projections.
- **Grants:** separate read, conversation execution, configuration proposal/apply/approval,
  processor execution, target-agent wake/run, subscription/destination use, external
  effects and budget management. Edit-only grants cannot enable target-agent wakes
  or disable mandatory Core capabilities. A descendant can only narrow its parent grant and expiry. Existing
  `video:read` keys retain exactly their current authority.
- **Audit:** retain actor and delegation chain, request/idempotency IDs, definition
  and input hashes, admitted source scope, question/approval receipts, cost
  reservations, effective interaction/execution-policy revisions, configuration-run
  versus target-run attribution, actual completion and effects. Redact secrets and private paths;
  store model evidence separately from administrative authority.
- **Budgets:** reserve before charged analysis/agent work and charged delivery.
  Company, agent, run and service limits compose; retries reuse the execution
  record and ledger. Missing/expired budget or unavailable input gives an explicit
  state. Report estimates separately from provider-reported and settled usage.
- **Data:** cursor-pagination and field selection for histories; explicit as-of
  time and bounded ranges; source-time versus capture-time versus commit-time
  fields; versioned schemas; revocable private media links. Default event reads
  exclude publisher-reference evaluation records. Retention applies to runs,
  questions, evidence, logs, cached projections and export grants.
- **Transfer:** page/result/media byte limits, concurrency and poll intervals
  bound both server and client work. [Current transfer policy](../../docs/HISTORICAL-RECORDS.md#record-31)
  records the existing application budget and its scope. A response count is not
  a provider billing guarantee; quotas require admission at every charged path.

The present transport's concrete limits remain in [PUBLIC-MCP.md](../../docs/HISTORICAL-RECORDS.md#record-29).
Target conversation, configuration and subscription limits must be published and enforced before
those capabilities are enabled. A schema/tool annotation such as `readOnlyHint`
is descriptive; host policy performs authorization.

## Protocol compatibility

Verified from official sources and the installed package on 2026-10-09:

| Area | Current SDK/application | Modern MCP target |
|---|---|---|
| Version | Pinned Python `mcp==1.30.0`; installed latest protocol constant `2025-11-25`; October 5 HTTPS receipt negotiated that version. | Official current revision `2026-07-28`; migration needs compatible SDK/transport and conformance tests. |
| Discovery and state | Stateful `initialize` handshake and key-bound SDK sessions; JSON HTTP POST; GET 405. | Per-request version/capabilities and `server/discover`; application handles retain cross-call state. |
| Durable execution | No public task/conversation handlers. SDK has deprecated experimental 2025 task helpers. | Poll Rocoto operation tools as baseline. Optional `io.modelcontextprotocol/tasks` requires both peers' declared support; 2026 `tasks/get` includes terminal result, `tasks/update` carries input. |
| Questions | SDK defines form/URL elicitation, unused by public transport. | Supported elicitation uses 2026 input-required/retry flow; durable application questions also work without it. |
| Notifications | Current GET 405 and no public subscription stream. | Optional negotiated 2026 `subscriptions/listen`; application webhooks remain a separate delivery service. |

The [official versioning page](https://modelcontextprotocol.io/specification/versioning)
and [2026 changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
establish the version/transport changes. The [Tasks extension](https://modelcontextprotocol.io/extensions/tasks/overview)
defines its opt-in polling/input behavior. The installed SDK's capability types
are evidence of library schemas, not enabled Rocoto features. Its experimental
`tasks/result` era must not be treated as the 2026 extension.

Keep clients on a verified mutually supported protocol. Migration tests cover
legacy initialization, modern discovery, incompatible-version errors, capability
absence, malformed input, reconnection, idempotent retries and cancellation.
Never require task/elicitation/notification support for ordinary operation polling.
OAuth discovery and token validation are an independent migration from today's
preconfigured keys; audience, issuer, current grants and redirects must satisfy
the chosen flow. See [official authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization).
No SDK upgrade, OAuth deployment or protocol migration is made by this document.

## Internal implementation mapping

This subsection is for engineering and is excluded from public tool descriptors,
DTOs, customer Skills and UI/error output.

| Public concept | Internal record/service mapping |
|---|---|
| Hosted conversation access | The MCP adapter resolves the same persistent Organization/Camera/Tracking/Action thread, enters the Rocoto harness with host-bound caller/delegation authority, and projects its runs/questions/results. Deterministic capability handlers can operate without an LLM turn. |
| Rocoto Real-time configuration | Internal ConfigurationService compiles TrackerDefinitions/TrackerBindings into a WorkerProfileRevision; a WorkerPipelineRevision separately pins official decoder/person-tracker/Cosmos/NVIDIA implementation and input processing. |
| Real-time analysis operation | WorkerRun executes bounded source windows. Actual SourceTransformReceipts and exact source clocks/ID evidence bind results before admission. Internal GPU/provider details stay in restricted cost/operation records. |
| Business processor operation | ProcessingRevision pins post-event module/script rules, output schemas and event dependencies; ProcessingRun consumes admitted observations and business facts and can emit canonical processed-alert events. It is distinct from camera inference and WorkerProfileRevision. |
| Edit-only and dispatch | Configuration/interaction policy and activation/wake/delivery policies are separately revisioned. Host admission identifies any authoring run; scheduler/dispatcher checks target wake policy at claim. The outbox projects the same canonical alert revision to scoped internal/external delivery adapters. |
| Canvas | CanvasRevision/TileProjection are dashboard presentation and typed output bindings. They are distinct from video geometry or source transforms. |
| Public state and support reference | Allowlisted projection over configuration/run/coverage records; internal detailed diagnostics are retrieved only through operator tooling. |

The official NVIDIA `nvvllmvlm` path supplies segmented-frame processing,
per-stream prompts and `vlm-result`. The candidate `cosmos_stream_bridge.py`
binds original output to source clocks, input traces and exact rendered IDs.
[Official integration](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_vllm_plugin.html),
[canonical data contracts](POSTGRES-SCHEMA.md). These facts describe the engineering
implementation, not public customer terminology or a claim of newly proven quality.

## Migration and release gates

| Phase | Deliverable | Proof required before enabling |
|---|---|---|
|1. Read and requirements | Seven current reads plus Rocoto Real-time DTO/discovery/requirements contracts. | Current allowlist HTTPS receipt; bounded/source-scoped results; schema versions; public naming/diagnostic-field boundary; revoked-source/key denial. |
|2. Same hosted conversations | Organization/Camera/Tracking/Action thread discovery/creation, messages, durable runs/results/questions and secure input through the existing harness. This is essential MCP access, not an optional product layer. | UI/MCP resume the same chat; promotion preserves history; scoped service delegation; one run per retry; durable/stale-answer checks; polling/reconnect and cancellation; scope/budget admission; idle threads retain no active model process. |
|3. Harness configuration and publication | Review/respond to the same proposals, internal deterministic validation/activation, and permitted script/canvas publication. | Protected Core; current validation/approval and idempotency; changed permissions/revisions invalidate apply; acknowledged readiness; actual budget admission; typed handlers avoid unnecessary model turns; edit-only configuration creates no target-agent run and cannot enable its wake policy or remove Core. |
|4. Processing/subscriptions | Hosted scripts, event cursors/outbox, participant phone delivery, webhook delivery and authorized Action Agent schedules/wake modes. | Monitoring survives MCP disconnection; bounded execution, duplicate delivery, replay, source revocation, destination/signature checks, retry exhaustion and budget enforcement; active scripts emit the same canonical alert externally with zero hosted target/Action runs when wakes are disabled. An alert reaches a participant's phone and the reply appears in the same conversation an MCP caller reads; duplicate, restart, paused, edit-only and replay cases send once or zero as specified; the daily cap stops the next send. |
|5. Effects | Explicit connector actions and grants for recipients outside the agent's participants, through hosted runs. | Real provider commissioning, permission/approval revocation, idempotent receipts, reconciliation and accounted partial/cancelled execution. |

Required integration cases include:

- An edit-only MCP request revises a Tracker/script through the same conversation
  and validation path, optionally records a bounded configuration turn, and
  creates zero target Tracking Agent runs or newly enabled wake jobs.
- An activated script emits a processed alert with target-agent wake disabled;
  the external webhook receives its canonical ID/revision, and no hosted Action
  Agent is created or executed. An independently allowed internal consumer reads
  the same ID/revision with its own permitted field projection.
- A queued target wake is denied after the policy is disabled. An edit-only caller
  cannot enable target execution, widen destination grants or remove Core.
- A processed alert of a Tracking Agent creates one phone delivery per participant
  and alert revision; a retry or duplicate claim reuses that delivery ID. An
  edit-only request, a paused phone delivery policy and the compiled-data replay
  lane each create zero phone sends.
- Incomplete/stale POS coverage keeps a case pending/unknown. A complete matching
  watermark can support a mismatch; late POS resolves/corrects the same case,
  and retry/replay does not duplicate the incident or its logical deliveries.

Conversation access remains in the target release scope even when its individual
tools are enabled incrementally. Deeper historical analysis has its own future
capability, data and cost commissioning; these phases do not depend on it.

The current demo's quarantine/reference-label limitations remain in
[Delivery status](../../docs/HISTORICAL-RECORDS.md#record-33). Contract fixtures prove schema and policy;
commissioned source analysis establishes actual observation quality. Each enabled
feature updates capability discovery and receives its own release receipt.
