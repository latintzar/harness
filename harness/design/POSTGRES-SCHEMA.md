# PostgreSQL target data contracts

Dated design checkpoint, 9–11 October 2026, retained from the original Vitrina Harness. This is a retail target contract. The current public implementation and managed OpenAI runtime are described in [Architecture](../../docs/ARCHITECTURE.md), [MCP](../mechanics/MCP.md) and the [SDK](../sdk/README.md). Historical “current” statements below refer to their original checkpoint; each implementation or deployment needs its own present verification.

This is the target persistence contract for the video product. PostgreSQL is the durable system of
record for Company metadata, authorization, processing state and admitted facts. Media, crops,
track geometry and large raw receipts use private object storage; PostgreSQL keeps immutable
references, hashes, content type, dimensions, retention and access scope.

## Company boundary and schema groups

The canonical Company authority is the maintained backend's `public.tenants.slug`. An imported
video workspace uses its exact persisted `.workspace-identity.json.company_scope` as that slug.
The SQL key is text and accepts the existing 64-character identity; the importer validates that
identity explicitly rather than regenerating it from a new path or changing ordinary provisioning
rules. Environment selectors such as `retail-demo` are explicit aliases bound to the tenant; their
display names and state paths confer no authority. Linking an import to an existing tenant requires
a host-verified identity mapping and collision check, never a name match.

New domain records live in the `realtime` schema. Every tenant-owned table has `tenant_id` referencing
`public.tenants(slug)`; parent-child references include `tenant_id` in their composite foreign keys.
This prevents a row from referring to another tenant's source, profile, conversation or event even
when opaque record IDs coincide. The SDK's `company_id` names this same tenant key. Existing shared
tables retain their established column names; the one-to-one Organization business profile is
business context, not another tenancy registry.

Host authorization resolves the tenant, member/service actor and current resource grants before
opening the existing `db.scoped_tx(scope)` transaction. It sets transaction-local `rocoto.scope`;
FORCE RLS policies on new tenant-owned tables use that same context and fail closed when it is
absent. Imported hexadecimal identities map exactly to `t.<company_scope>`. Scope mappings for
other IDs must be unique and collision checked rather than relying on lossy sanitization.
Application predicates and composite foreign keys reinforce RLS. The runtime uses the restricted,
non-owner `rocoto_rt` role with `ROCOTO_REQUIRE_RLS=true`; owner/bypass roles are reserved for
explicit migrations or audited maintenance. Existing cross-scope worker scans use the control
pool, then return to scoped runtime transactions for tenant writes.

Membership reuses `public.operators` and `public.team_members`, preserving opaque principal IDs
and verified identity bindings. Video `owner`, `operator` and `browse` permissions and the current
location/function grants remain explicit: translating `operator` or `browse` to legacy `staff`
would grant unrelated action/delivery privileges. A migration supplies the exact capability mapping
and tests it before enabling a carrier. Organization conversation virtual owners remain tenant-bound
service actors with their original IDs; they do not become human operators or gain membership.

| Area | Core tables | Required properties |
| --- | --- | --- |
| Company | Existing `public.tenants`, `public.operators`, `public.team_members`; tenant-bound environment aliases, resource grants, `organization_profiles`, `audit_log` | One Company authority, preserved role semantics, one-to-one business profile and immutable member/service-actor audit identity. |
| Setup | `locations`, `rooms`, `cameras`, `camera_revisions` | Topology, privacy, timezone and setup revision; camera identity is stable. |
| Operational contacts | Existing `public.contact`, `public.contact_role`, `public.endpoint`; `channel_addresses` (member verification per channel), `agent_participants`, `recipient_bindings`, `contact_requests` | Reuse tenant-scoped contacts and endpoints; preserve member verification separately per channel (`viber`, `sms`, `whatsapp`). A recipient binding names its holder: a Tracking Agent for its own participants, or an Action Agent's purpose-specific revocable grant for another person. |
| Sources | `source_connections`, `source_assets`, `source_windows`, `source_clock_revisions`, `blob_refs`, `source_transform_receipts` | Credential references, source hashes, clock mapping, input geometry and lifecycle. |
| Worker observation | `tracker_definitions`, `tracker_bindings`, `worker_profile_revisions`, `worker_pipeline_revisions`, `worker_runs`, `worker_checkpoints` | Core/Add-on observation interests, official camera worker revisions, leases and admission. |
| Processing | `module_definitions`, `module_installations`, `processing_revisions`, `processing_runs`, `processing_checkpoints` | Maintained post-event business processors, customer scripts/LLM rules and resumable runs. |
| Presentation | `canvas_revisions`, `tile_projections`, `artifact_refs` | Customer-facing dashboard presentation, minimized tiles and governed packages. |
| Visits | `visits`, `track_segments`, `track_points` | Camera-scoped IDs, source intervals, geometry and tracker revision. |
| Observations | `observations`, `observation_revisions`, `domain_events`, `event_revisions`, `event_evidence` | Raw receipt, validation state, source interval, profile revision and admission history. |
| Catalog | `catalog_products`, `catalog_product_revisions`, `prices`, `observed_items`, `item_links` | Source-backed identity and price basis separated from visual observations. |
| Commerce | `pos_transactions`, `pos_lines`, `basket_transitions`, `basket_transaction_links` | POS facts remain separate; basket evidence is temporal and visit-scoped. |
| Agents | `setup_agents`, `tracking_agents`, `action_agents`, `agent_revisions`, `agent_alerts`, `action_attempts` | Existing chat IDs, versioned scripts/instructions and scoped effects. |
| Alerts and delivery | `alert_envelopes`, `alert_revisions`, `alert_subscriptions`, `delivery_grants`, `outbox`, `delivery_attempts`, `phone_delivery_policies`, `phone_deliveries`, `phone_spend_counters` | Shared processed-alert identity, provenance, internal/external grants and provider outcome. One phone delivery per participant and alert revision under a stable ID, its state from actual provider acknowledgments, and daily message counters per company and per agent. |

New domain `id` values use UUIDv7. Existing Company, principal, conversation and imported-record
identifiers remain opaque and retain their exact values; migration never requires reissuing them as
UUIDs. Explicit aliases record verified links where an existing backend record has a different key.
External identifiers use a `(tenant_id, source_system, external_id)` unique key. Existing shared
contact/endpoint integer IDs retain their native type and are exposed as opaque references. Durable
service timestamps are UTC `timestamptz`; source-relative intervals remain numeric fields with an
explicit source clock revision.

## Time, geometry and admission

A source asset has `captured_start`, `captured_end`, `source_time_basis`, `source_clock_revision`
and `clock_uncertainty_ms`. A source window stores exact source-relative bounds and the selected
frame sampling receipt. A `source_transform_receipt` binds actual input bytes and frame hashes to
decode, crop, resize, letterbox, tile and coordinate mappings. It is the authority for mapping
worker evidence back to original source pixels; presentation tiles have a separate contract.

Every Observation Event stores:

- `source_start_s`, `source_end_s`: the half-open `[start, end)` source interval;
- `source_asset_id` and `source_window_id`: immutable input identity;
- `ingested_at`: when the host received the envelope;
- `processed_at`: when a worker or post-event processor completed;
- `admitted_at`: when validation allowed the fact into product projections;
- `occurred_at_utc`: optional UTC `timestamptz`, only with a valid clock revision; wire payloads
  use an RFC3339 ISO-8601 UTC string. Source intervals remain decimal seconds with explicit `_s`
  units.

Legacy `start_s`/`end_s` and `event_start`/`event_end` names are accepted only at migration or
adapter boundaries and are normalized to `source_start_s`/`source_end_s`.

A model narrative time is accepted only when it matches the prepared window and clock contract.
Out-of-window, ambiguous-clock and unbound-input results are retained as `quarantined`. The raw
response, prompt/config hash, input frame trace, source transform receipt, tracker sidecar/OSD
receipt and validator decision are separate immutable references.

An `observation` has a lifecycle such as `received`, `validated`, `quarantined`, `admitted`,
`superseded` or `retracted`. An Observation Event points to an admitted observation and has an
append-only revision chain. A Business Domain Event instead points to its validated connector/source
receipt and external identity. A correction creates a new event revision with
`supersedes_event_id` and a reason; a retraction records who/why/when and removes the event from
current projections while retaining the original evidence.

An Observation Event requires an admitted Observation and its source/evidence lineage. A Business
Domain Event instead stores `origin_kind` and a validated connector/source receipt, external event
identity, effective time and source revision. POS transactions, catalog imports, stock receipts and
connection state changes use that business lineage and do not invent a video source interval or clock.
The shared Domain Event projection preserves the origin kind and exact lineage for each event.

## Worker runs and post-event processing runs

A `worker_pipeline_revision` pins the official decoder, person tracker, Cosmos model/plugin,
sampling, privacy processing, input transform and output transport. A `worker_profile_revision` pins
the active Core/Add-on TrackerDefinitions and bindings for the camera profile. A `worker_run` starts
with the planned source window, worker pipeline/profile revisions and any prepared receipts available
at admission. The run produces the actual `source_transform_receipt`, frame trace and OSD receipt;
these receipts are required before event admission. The run owns bounded source windows, leases,
inference checkpoints, input/output hashes and quality validation. It is the camera observation path.

A `processing_revision` pins one or more maintained `ModuleDefinition`/`ModuleInstallation`
revisions, customer-approved scripts or LLM settings, the canonical output/alert schema,
presentation bindings and the Tracker event dependencies it consumes. A `processing_run` executes that revision over admitted
events, with its own trigger, cursor, lease, budget, checkpoint and output revisions. A Tracking
Agent can select automatic post-event processing, wake processing or a scheduled batch; the chat
identity remains separate from each processing run.

A processing result emits at most one idempotent `alert_envelope` per emission key and revision.
A correction creates a new envelope revision referencing the superseded output and input event IDs;
subscriptions consume the canonical envelope rather than a script-specific payload. The `outbox`
row is inserted with a committed fact, processing result or alert envelope and contains
`aggregate_type`, `aggregate_id`, `aggregate_revision`, `event_type`, `payload_schema`, `payload`,
`idempotency_key`, `available_at` and delivery state. A worker claims rows with `FOR UPDATE
SKIP LOCKED`, publishes to the selected internal transport and records attempts. Publication is
at-least-once; consumers are idempotent. Kafka is optional transport, not required infrastructure.

## Execution, wake and delivery axes

The hosted harness records configuration turns separately from execution. A configuration turn may
validate and activate a Tracker or Processing revision. Observation execution is a `WorkerRun`;
custom post-event script execution is a `ProcessingRun`; agent wake is an explicit subscription
choice; and event delivery is an outbox/delivery attempt. Editing a configuration or script never
implicitly targets or wakes a Tracking Agent.

The shared `alert_envelope` carries `event_id`, `event_revision`, canonical input event references, bounded
input artifact references, output provenance, emission idempotency key and status. Internal agents
consume it through `alert_subscriptions`; external webhooks and external Action Agents use explicit
`delivery_grants`. Subscription revisions preserve the exact event scope, output schema and grant.

Participant phone delivery is a further axis with its own record, `phone_delivery_policies`: on by
default for a Tracking Agent and independent of worker, script, wake and external delivery state.
The host fans one alert revision out to `phone_deliveries`, one row per participant, keyed by the
stable ID defined in [Channels](../../docs/HISTORICAL-RECORDS.md#record-24). Each row pins the binding,
address and policy generations it was admitted under, the channel, the reserved units and the
provider message ID. An edit-only configuration change, a paused policy and a replay run or loop
write zero phone deliveries. Core Tracking Module alerts have no phone delivery.

## Idempotency, visits and commercial facts

Every command/envelope has a `request_id`, `company_id`, `idempotency_key` where the operation
writes, `command_hash` where applicable and `created_at`. A live worker execution adds an opaque
`run_id`; a replay execution adds an opaque `run_id` and `loop_id`, plus `source_asset_id` and
`source_window_id`. Setup, profile, configuration and canvas commands do not require those
execution or source fields. The
database enforces unique idempotency per Company and operation. A retry with the same hash returns
its stored result; a conflicting hash is an explicit conflict.

`track_segments` binds a camera-scoped tracker ID to a visit, source interval, source frame
geometry and worker revision. A tracker ID can be reused after a reset or track birth; the intended
composite includes `(company_id, camera_id, tracker_epoch_id, tracker_id, track_birth_id)` and its
explicit visit binding. `tracker_epochs` retains the worker/configuration and lifecycle receipt.
`source_clock_mappings` maps each immutable file's local PTS into that epoch's source clock without
changing its source SHA. A file cut preserves identity only with this verified mapping and live
tracker lineage. Migration `0009_realtime_continuity` implements these concepts as
`realtime.tracker_epoch`, `tracker_epoch_end`, `source_artifact_mapping` and `track_segment`.
The maintained admission/subject services require an actual tracker snapshot with matching epoch,
configuration, full tracker ID and birth identity before joining an observed segment to a visit.
Artifact and stream intervals preserve equal elapsed time and immutable source hashes. Worker
emission and replay compilation of this continuity contract remain separate integration work.

Cross-camera `visit_associations` retain local visit IDs, revisioned stock MV3DT adoption/correction
evidence and the calibration/synchronization revision. Observations preserve whether the person was
directly visible, shadow-tracked or projected from a peer view. Shared journey identity does not
supply face/biometric recognition or a returning-shopper profile.

`observed_items` stores `instance_id`, visit, first/last source interval, description, quantity,
quantity basis, identity status and evidence. `item_links` records candidate/confirmed catalog
links with reviewer or matcher provenance and revision. The reducer consumes ordered
`basket_transitions`, each tied to an admitted event and evidence. Repeated sightings of one
instance do not create another unit; unresolved association stays visible as uncertainty.

Stock uses explicit claims with `state` in `unknown`, `empty`, `available` or `replenished`, plus
`quantity` only when the source supports a count and `observed_at`. An `empty` claim is sourced;
an absent row means the system has no stock measurement. Observation Events never manufacture
`pos_transactions`, payment amounts or sales totals. A POS import creates a separate transaction
and line with source receipt, currency and effective time. A `basket_transaction_link` records
evidence and confidence without changing either source fact.

## POS reconciliation

A custom POS reconciliation `ProcessingRun` declares a valid connector, a bounded coverage window
and a source watermark. Each candidate has status `pending`, `unmatched`, `matched` or `unknown`.
`unmatched` requires a closed matching window, sufficient connector coverage and no qualifying
transaction under the recorded rule. A POS outage or incomplete feed keeps the result `unknown`
or `pending`. Late connector arrivals resolve or correct the same occurrence through a new revision.
POS source facts, video Observation Events and the derived reconciliation result retain separate
records and exact lineage; an unmatched result is not a confirmed unpaid sale.

## Configuration and public mapping

A shared internal `ConfigurationService` accepts trusted business-intent requirements from the
Organization agent or a registered deterministic operation invoked by delegated MCP through the
hosted API, validates dependencies and permissions, compiles Tracker bindings and worker/processing
revisions, and activates them only after readiness checks and an acknowledgement receipt. A dormant
configuration-agent state is a lifecycle state of the hosted conversation harness while an ambiguity
is waiting for input; it is not an independent passive product. MCP and host callers use this same
ConfigurationService and cannot bypass it. Deterministic reads and changes do not require an LLM turn.

Sourced estimates remain allowed when their source and basis are explicit, such as a catalog-derived
estimated price. An observed action never becomes a paid transaction, POS amount or inferred sale.
Activation stores the canonical output schema and explicit delivery subscriptions. Standing
authority is checked at action execution; configuration edits do not require per-edit approval when
inside that authority. The hosted conversation API is a core product boundary. The Rocoto UI and a user's delegated
external agent through MCP use the same persistent threads, agent revisions, runs and results, with
delegated Company authorization at each request. Monitoring and processing remain server-side and
transport-independent. The customer-facing label for the live observation experience is **Rocoto Real-time**; Cosmos,
worker-plugin and prompt names remain internal metadata.

## Operational contacts

`ContactService` reuses the maintained backend's `public.contact`, `public.contact_role` and
`public.endpoint` through tenant-scoped repositories. Contacts already support operational parties,
including customers and prospects, without requiring operator membership. Preserve their IDs, scope
and `tenant_slug`; endpoint identity is unique by `(tenant_slug, kind, addr)`. Every endpoint lookup
and mutation verifies its contact and tenant because the existing endpoint table has no independent
RLS boundary. A member link is explicit and host verified, never inferred from a matching name or
phone number.

Private channel verification/receipt, revision, account generation and revocation records augment
these established records. A `channel_addresses` row holds one verified address per member and
channel, with channel in `viber`, `sms` or `whatsapp`; the existing endpoint kinds gain `sms` and
`viber` beside `wa` through a maintained migration. Host forms collect recipient details; chats
receive the scoped contact reference and completion state. Sending requires both an authorized
binding and a ready channel account.

Recipient bindings have two holders. A **Tracking Agent** holds one binding per participant in
`recipient_bindings` (agent or chat, member, channel, purpose `tracking_alerts`, enabled,
generation), created by default at promotion to Tracking mode and when its creator adds a company
member; `agent_participants` lists those members and `contact_requests` holds the contact card
raised in the chat for a participant with no verified number. An **Action Agent** holds a binding
for a person outside its participants only through a purpose-specific grant naming the connection,
operation, audience and expiry. A Tracking Agent's bindings can name its participants alone.
[Channels](../../docs/HISTORICAL-RECORDS.md#record-24) owns the field lists.

Migration inventories `messaging.sqlite` separately. Its existing `contacts` rows are member-bound
channel verification records, not a replacement operational contact registry. Preserve member IDs,
verified challenges, account/channel generations, revocations and conversation bindings. Any link
to an operational contact requires an explicit verified mapping. The migration adds a `whatsapp`
channel to every existing contact, binding, outbox, inbox and delivery-event row. External
recipients use the same ContactService with their own delivery grants and provider-readiness checks.

## SQLite migration boundary

The current prototype stores are migration inputs. This documentation pass leaves them untouched.
The backend service/repository boundary belongs under `backend/realtime/`; video host adapters use
the internal SDK rather than becoming a second persistence authority. Add a new Alembic revision
after the checkpoint's `0004` head (the retained migration chain now extends through `0012`) through the maintained migration workflow; applied revisions remain
immutable and application startup performs a read-only schema check.

| Migration input | Durable destination | Authority and preservation guard |
| --- | --- | --- |
| Workspace identity; `access.sqlite` environments/principals/memberships | `public.tenants`, `operators`, `team_members`; environment aliases and resource grants | Exact company/principal IDs; verified collisions and identity links; preserve browse/observer limits and existing role semantics. |
| `business-scope.sqlite` graph/history/chat scopes; `business-context.sqlite` revisions | Tenant topology, business-profile revisions, grants and scope audit | Keep collection/revision keys, camera view/source bindings and revocation history; profile remains within the existing tenant. |
| Chat JSON; `agent-chats.json`; native scope/toolset mappings and questions | Conversations, messages, native-thread bindings, service actors and questions | Preserve chat/thread IDs, sender, lineage and question generation/turn/item correlation; native runtime state remains separately retained. |
| `background-agents.sqlite` agents/runs/audit/wakes | Agent revisions, immutable run snapshots, wake queue and cursors | Preserve saved definitions, active grants, bounded wakes and provider-event deduplication; import does not execute an agent. |
| Source-intake registry; `monitoring.sqlite` sources/profiles/events | Source assets, profile revisions and observations | Preserve source hashes, ownership, exact profile revision, event fingerprint and original event key; enforce tenant-composite references. |
| `stream-ingestion.sqlite` windows/model outputs/adapter receipts/work | Admission receipts and transactional outbox/work | Retain source and arrival clocks, raw hashes, attempts and deduplication; admit facts and their outbox entries in one transaction. |
| `location-intelligence.sqlite` records; catalog/basket receipts | Catalog, visits, observed-item histories and derived projections | Preserve evidence/price basis and recording-scoped tracker identities; source topology remains authoritative. |
| `messaging.sqlite` verification/bindings/inbox/outbox, participants, recipient bindings and phone deliveries | Existing operational contacts/endpoints plus channel addresses, agent participants, recipient bindings, phone deliveries and spend counters | Explicit member-to-contact links; preserve channel, channel generations, provider IDs, request keys, delivery IDs and revocations. Import sends nothing. |
| `company-usage.sqlite` ledger/settings/provider bindings | Tenant-bound immutable usage/budget records | Preserve money event IDs, purpose-scoped allowances and permanent pool binding; import confers no new spending grant. |

The target migration is append-only and staged:

1. Freeze new SQLite writes for a selected Company and record each store path, schema checksum and
   export time.
2. Bind the exact Company identity to `public.tenants.slug`, import preserved memberships and
   one-to-one Organization profile, then locations, rooms, cameras and source grants. Resolve
   existing-tenant/principal collisions through an explicit host-verified mapping before import.
   Record `legacy_store`, `legacy_table`, `legacy_pk` and source hash in `legacy_mappings`.
3. Import source assets/blobs by checksum, preserving source-relative time, clock receipts,
   transform receipts and private ACLs. A missing blob blocks admission of its dependent event.
4. Import Tracker/profile revisions, worker receipts, agent revisions, monitoring events, visits,
   catalog records and basket evidence. Dataset reference labels remain evaluation provenance.
5. Recompute projections from admitted event revisions, compare counts and hashes, then run shadow
   reads against SQLite. Resolve mismatches through recorded corrections.
6. Switch the host read/write boundary to PostgreSQL. The read-only SQLite copy remains an audit
   and migration reference. A pre-write snapshot can support rollback before PostgreSQL writes; after
   writes begin, recovery uses a recorded reverse delta/reconciliation or a forward repair revision,
   never a stale snapshot restore.

No migration step turns an unknown price, quarantined model output or reference label into a
commercial fact.

## GPU-free compiled-event replay

A replay source adapter reads preserved compiled tracker/event envelopes and calls the same source
admission, event transaction, post-event processing, outbox and browser subscription services used
by live input. It uses a distinct replay `worker_run`/`processing_run`, `run_id` and `loop_id`,
retains the original source asset/window, and carries a separate simulation arrival clock. Replay
output is a candidate until the normal validator admits it; quarantined model events stay
unadmitted. A database queue, process channel or future broker carries the same envelope.

Replay identity cannot collide with live checkpoints, event IDs, processing cursors, agent
watermarks or outbox keys. Temporary decoded files, intermediate artifacts and replay receipts have
bounded retention and deletion receipts. External effects are disabled by default and require a
separately authorized test sink. The replay lane writes zero phone deliveries and reaches zero
channel providers.
