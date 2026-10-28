# Rocoto Real-time data services

This package supplies the shared product boundary for admitted observations and business facts.
The video control room, replay, maintained processing modules, hosted agents and MCP adapters target
this boundary. [Product architecture](../../harness/design/SERVICE-BOUNDARY.md) describes the
whole product; [delivery plan](../../docs/HISTORICAL-RECORDS.md#record-22) tracks integration.

## Implemented foundation

- `contracts.py`: versioned canonical events, exclusive video/business/processing provenance,
  source-relative windows, optional observed UTC, host execution bounds and independent execution
  switches. A validated context describes an already authenticated request; it grants no authority.
- `provision.py`: explicit transactional import of the saved video company identity into
  `public.tenants`, existing operators and memberships. Environment names remain aliases. The import
  preserves opaque identifiers, exact roles and revocations, and rejects ambiguous authority.
- `provision_host.py`: the host command for that import, `python -m realtime.provision_host`. It
  reads the saved workspace identity and the environment's principals from the preview's state,
  connects as the owner migration role named in `DATABASE_URL`, and runs the import in one
  transaction. Its dry run is the whole import rolled back; `--apply` names the dry run's digest
  and writes a private receipt.
- `store.py`: restricted-role PostgreSQL transactions, current membership and per-source grants,
  immutable profiles/input receipts, registered event admission, quarantine, revisions, connector
  identity deduplication, atomic event/outbox writes, leased work and bounded source-clock reads.
- Migration `0005_realtime_foundation` adds the `realtime` schema. Company-qualified foreign keys,
  FORCE RLS and dedicated runtime grants apply to every table. Migration-owner policies are explicit
  for provisioning and narrow security-definer membership/budget helpers.
- `replay.py` and `replay_store.py`, with migration `0006_realtime_replay`, compile admitted camera
  history into a bounded, durable replay. Preparation resumes after interruption; ticks commit the
  event, outbox and cursor together. The database clock drives pause/resume, and each loop retains
  distinct identities. Replay uses the shared admission path with agent wakes and external effects
  disabled. Source revocation also revokes access to copied history and compiled plans.

- `subjects.py` and migration `0007_realtime_subjects` preserve explicit camera, zone, visit,
  physical-item and catalog identities. Event bindings pin both event and subject revisions;
  an unbound event differs from a sealed empty set. Replay copies use run/loop namespaces and
  commit those bindings with the replay event and cursor. Catalog references retain the existing
  company's SKU authority. A visit's source identity supplies no recurring-person profile.
- `modules.py` implements maintained source-time reducers for journeys, possession, explicit
  examination/queue intervals, product interactions and observed stock transitions. Corrections
  replace prior revisions; retractions preserve evidence while removing its business effect.
  Quantity and identity estimates remain qualified; visible payment actions carry no sale amount.
- `module_store.py` and migration `0008_realtime_modules` persist versioned module snapshots from
  sealed bindings and exact input revisions. Explicit refresh is the current integration boundary;
  continuous dispatch, catalog prices/POS projections and frontend cutover remain delivery work.
- `replay_worker.py` performs one bounded durable replay tick and refreshes the saved plan’s module
  projections. A retry repairs a crash between admission and projection. Source lists come from
  the saved plan, and replay effects/wakes remain disabled. The host scheduler and frontend adapter
  still need commissioning.

- `continuity.py` and migration `0009_realtime_continuity` retain stock tracker epochs,
  immutable artifact-to-stream clock mappings and receipt-backed track segments. Complete opaque
  tracker IDs include an epoch and birth identity. A continuing observed track can bind one visit
  across rotated files; resets, reused IDs and unverified shadow intervals cannot invent a join.
  Admission and subject bindings validate the same actual-input receipts. Replay with epoch lineage
  awaits explicit compiler support and currently fails closed.
- `modules.py` and `module_store.py` also project one registered tracker epoch on its stream clock.
  `refresh_epoch`, `project_epoch_at` and `latest_epoch` use `retail-core.epoch.v1`, separate from
  recording snapshots. Chapter evidence keeps the original file and local time. Late corrections,
  repeated deliveries and out-of-order files preserve deterministic journey ordering. A new item first
  observed in a later file can belong to the continuing visit through a receipt-backed observed
  segment. Re-identifying an existing physical item across files and camera-to-camera associations
  require their own evidence and remain open. Cached reads recheck every catalog/subject grant.
- `configuration.py`, the versioned retail playbook and migration `0010_realtime_configuration`
  implement typed business requirements, protected Core coverage, immutable proposals, desired
  revisions and worker acknowledgements. Activation uses an exact actual-input configuration receipt
  and source-clock boundary. Supported Core families can operate while missing zone/tracker inputs
  remain pending; admission rechecks current zone validity. Observation, processing, agent wake and
  delivery policy are stored separately. Harness/tool routing, worker compilation execution and
  policy-controlled scheduling still need integration.
- `identity.py` explicitly reconciles one verified supplier login with an existing imported operator.
  The host must provide a verified platform-owner context and control transaction. The operation
  preserves IDs/memberships, audits retries and rejects ambiguous or cross-company bindings.

- `import_stream.py` reads a host-selected immutable export of admitted stream history. It
  validates the original company/owner/source identities, native input receipts and fingerprints,
  then imports source/profile/receipt phases idempotently through the maintained SDK. Event and
  subject bindings commit together. Publisher references, unreceipted history and quarantined
  responses remain excluded. An empty verified export imports zero events; it supplies no model
  quality evidence.
- `import_tracker_run.py`: the host command that imports retained complete tracker runs as
  [historical tracker reports](#historical-tracker-reports), as the restricted runtime role.
- `media.py` answers up to 64 host playback references in one scoped read: the current source
  grant, the active camera subject of the tracker epoch and the immutable artifact mapping recorded
  for that epoch. The native video host joins the answer to its sealed catalog.
- `routes.py` exposes bounded authenticated recording/continuous-session snapshot and saved replay-status reads through
  the maintained supplier session. It resolves an existing verified operator membership and lets
  the restricted SDK enforce source access. The returned contract excludes worker configuration,
  internal processing manifests and prompts. Preview authentication and frontend cutover remain
  separate integration steps.

- `locations.py` and migration `0012_realtime_locations` are the store of record for a retail
  location's trackers: the location's identity and cameras, tracker revisions of three parts, the
  current output with its ledger, retail records, the run schedule and the newest 200 runs.
  `LocationStore` is the SDK. The tracker pipeline and the page adopt it in their own integration
  step; [Location trackers](#location-trackers) describes the tables and their writers.

The live prototype still uses its commissioned stores until a verified snapshot migration and
adapter cutover. Having this package and passing database tests does not establish UI cutover,
worker commissioning or production readiness. Accepted observations remain distinct from dataset
labels, quarantined model responses and synthetic test records.

## Authenticated read integration

`GET /api/realtime/sources/{source_id}/assets/{asset_id}/snapshot` returns a source projection,
optionally at a bounded `at_s` playhead. `GET /api/realtime/replays/{run_id}` returns saved replay
progress. `GET /api/realtime/sources/{source_id}/epochs/{epoch_id}/snapshot` reads one registered
continuous session; its optional `at_s` is stream time. Its separate `playback` descriptor names
verified tracker-backed visits, tracker namespace/ID/birth bindings and source artifact mappings,
even when no action observation has been admitted. First observed is a camera measurement, not
store entry. The descriptor rechecks current camera/visit state on every read and contains source
identifiers for the private media resolver; stored interval evidence does not invent trail geometry. The response names the epoch and stream
clock, while each chapter retains its real recording asset and local source window. It does not
expose the synthetic storage key as a playable asset. This maintained HTTP router uses `sup_session`,
current team membership and source grants;
responses are private and uncached. Revocation applies to cached sessions and copied replay data.
Identity resolution and the SDK read share one five-second deadline, after the existing supplier
authentication dependency. Invalid or out-of-range playheads return a bounded validation error.

Identity import intentionally preserves opaque operator IDs with no invented email association.
The supplier-session router resolves a verified email to that durable operator. Its deployment
requires host invocation of `identity.reconcile_verified_login` after platform-owner and supplier
verification; an unbound imported principal returns `principal_unavailable`. The existing email-keyed
invite flow must not create a duplicate operator as a substitute.

The native video product has a separate authenticated adapter into these same SDK services. It
preserves its existing verified access-code session and exact company/principal IDs, without an
email reconciliation or a second login. Its explicit read-authority setting, restricted pool and
artifact dependency closure are implemented and tested; source/media commissioning and frontend
cutover remain open. The published preview retains its recorded authority.

## Historical tracker reports

A retained complete single-recording tracker run can supply selectable visitors through
`EpochStart.historical_run` and migration `0011_realtime_tracker_reports`. The host verifies the
exact media, original sidecar, execution/EOS log, application configuration, producer and source
sample mapping hashes. Its typed snapshot digest seals the complete report. The retained application
configuration is explicitly distinguished from a complete effective low-level tracker configuration.
The run accepts its exact recording clock and hash; rotation or cross-camera association uses the
separate commissioned continuity path.

The store binds each retained hash to the record that carries it: the application configuration to
the epoch's configuration hash, the media hash and source sample mapping to the single artifact
mapping, the sidecar and snapshot digest to every receipt on that recording, and the EOS log to the
closure. One recording keeps one mapping and one tracker epoch. Delivering the same report again,
including two deliveries at once or one after closure, returns the retained records. A delivery
with any changed sealed value conflicts.

Old KITTI records can mix current and retrospectively emitted tracker boxes. Their visibility stays
`reported`, with a deterministic first-reported identity scoped to the run namespace, sidecar hash,
raw tracker ID and first reported frame. One raw tracker ID keeps one report boundary, and the sealed
snapshot contains that frame. The visit basis is `first_reported`; playback exposes
`first_reported` separately from `first_observed`, including the first frame with empty elapsed history.
`first_reported` is the visit's sealed first reported frame for every delivery order and slicing of
its segments. Current source grants, camera/visit retractions and bounded reads apply equally to
these records.

Action-to-visit bindings continue to require explicitly `observed` coverage, and a `first_reported`
visit is refused by its basis alone. Observed items attach to observed visits under the same rule.
The historical adapter can register a `tracking.reported` profile and actual tracker-input receipt
while admitting zero action events. Its receipt does not attest a video-reasoning model call; an
action event citing it is quarantined.

Migration `0011` widens one check constraint on `realtime.track_segment.state`. Grants, policies and
FORCE RLS keep their `0009` shape, and the revision is declared backward compatible. The previous
release starts against the upgraded schema, keeps its own writes valid and rejects `historical_run`,
`reported` and `first_reported` records as validation errors. Downgrade is refused so reported
history is retained; rollback restores application code.

Frame times come from one canonical clock, `reported_frame_s`: recording start plus frame index over
frame rate, in decimal seconds at 28 significant digits. Hosts use that value for each first reported
frame; a differently rounded time is refused. The database fixtures prove 8 frames per second, where
every frame time is a finite decimal, and 30 frames per second with a stream offset, where playback
returns each retained source interval exactly.

The host audits the original completed run before creating these records. SDK support and database
tests establish the storage contract, and [`import_tracker_run.py`](import_tracker_run.py) is the
host command that performs the audit and the import. Importing the published preview's workspace,
private media resolution and raw box sidecar delivery are separate commissioned steps. Sparse
publisher-frame reconstructions preserve their encoded clock and original sample map, and retain
that footage qualification in the source metadata (`temporal_sampling`, `unique_source_frames`).

### Host import of a retained run

`python -m realtime.import_tracker_run` imports one retained, complete, single-recording
person-tracker run per camera view. It reads a run root in the producer's layout and writes through
`RealTimeStore`, `SubjectStore` and `ContinuityStore` only, as `rocoto_rt` under row security.

```sh
cd backend    # or a release's packaged _maintained_backend directory
python -m realtime.import_tracker_run --run-root RUN_ROOT --reference RUN_LABEL \
  --identity SAVED_WORKSPACE_IDENTITY.json --principal OWNER_PRINCIPAL
RUNTIME_DATABASE_URL=... python -m realtime.import_tracker_run --run-root RUN_ROOT \
  --reference RUN_LABEL --identity SAVED_WORKSPACE_IDENTITY.json --principal OWNER_PRINCIPAL \
  --apply --expected-plan-sha256 PLAN_SHA256 --receipt NEW_RECEIPT.json
```

The first form is the dry run. It opens no database, changes no file and prints the plan: per view
its identifiers, sealed hashes, frame, visit and segment counts and each visitor's first reported
frame, plus one `plan_sha256` over the whole run, the company and the principal. `--apply` requires
that reviewed checksum, the restricted connection in `RUNTIME_DATABASE_URL` and a new receipt file in
an existing folder outside the repository and the run root. `--view CLIP` selects views; the default
is every record of the run manifest. The run label names the retained run inside every evidence
reference, so one run keeps one label.

Evidence sits per view under `retail-action-nvidia/<clip>/`, with `retail_prepare_track.py`,
`tracker-template.txt` and `source-inventory-proof.json` at the run root:

| Evidence | Check | Sealed in |
| --- | --- | --- |
| `clean.mp4` | SHA-256 and byte length equal the source inventory | `historical_run.source_sha256`, the mapping, the receipt |
| `app.txt` | one enabled file source naming this view's `clean.mp4`, the NVIDIA multi-object tracker enabled, KITTI output into this view's `tracker-kitti`, `file-loop = 0`, the recording's frame size | `configuration_sha256`, `historical_run.application_config` |
| `tracking.log` | one `Received EOS. Exiting` line followed by `App run successful`; `tracking_exit` 0 in the run receipt | `historical_run.execution_log`, the closure |
| `tracker-kitti/` | exactly `00_000_000000.txt` onward, one file per frame; each row has 17 columns and class `Person`; a tracker ID appears once per frame | `historical_run.tracker_sidecar`, every birth identity |
| `source_mapping.json` | one entry per encoded frame on the encoded clock, each naming its publisher frame | `historical_run.source_mapping`, the mapping's evidence |
| `receipt.json` | equals its run-manifest record; duration equals frame count over an integer frame rate | the epoch's `evidence`, the camera's provenance |
| `tracks.json` | equals the sidecar row for row | the receipt (`retained_run.tracks_sha256`) |
| producer script, template, inventory | regular files of the run root | `historical_run.producer`, the receipt's `retained_run` |

Every path is a regular file or directory reached through real directories. A symbolic link below
the run root is refused.

The per-frame directory has one canonical digest, `rocoto.tracker-kitti-directory.v1`: the SHA-256 of
a UTF-8 listing whose first line is that schema name, followed by one line per file in ascending
byte order of its name, `<sha256 of the file bytes>  <file name>`, each line ending in a line feed.
A host reproduces it with standard tools:

```sh
cd RUN_ROOT/retail-action-nvidia/CLIP/tracker-kitti
{ printf 'rocoto.tracker-kitti-directory.v1\n'; LC_ALL=C sha256sum -- *; } | sha256sum
```

Identifiers come from the view alone, so changed bytes meet the retained record of the same name:
source and source asset `retailaction-<clip>` (kind `camera`, preserved), camera subject
`<source>-camera`, epoch and tracker namespace `<source>-tracker-report`, mapping `<epoch>-m1`,
receipt `<epoch>-r1`, profile revision `tracking-reported@1` with the single event type
`tracking.reported`, visit `<epoch>-v<raw tracker ID>` and segment
`<visit>-s<first frame of its interval>`.

Per view the command registers the source, the profile, the camera subject, the epoch with
`historical_run`, exactly one artifact mapping whose evidence equals `historical_run.source_mapping`,
the tracker-input receipt, one `first_reported` visit per raw tracker ID, one `reported` segment per
run of consecutive reported frames, and the `eos` closure with the execution log. A visit's boundary
is `reported_birth_id` over the namespace, sidecar digest, tracker ID and first reported frame; its
provenance window is that one frame from `reported_frame_s`. The command admits zero events and
registers zero observed items.

Before the first write of a run the command checks the database role, the company binding and the
principal's configure and ingest capabilities, then compares every record the run's views already
retain with the plan. One differing record refuses the whole run. An identical run returns the
retained records and adds no row, and an interrupted run completes on the next apply. A changed byte
in an evidence file changes `plan_sha256` and the sealed record that carries its hash, so it is
refused against the reviewed checksum and again against the retained records. The receipt lists per
view the identifiers, hashes, counts, record digests, rows before and after, and the playback
read-back. A failure after writing began leaves a receipt with status `failed`.

On 10 October 2026 the command imported the 40 retained RetailAction views into a disposable
database: 40 sources, 40 epochs, 40 closures, 40 mappings, 142 `reported` segments, 0 other segments,
142 `first_reported` visits and 0 events. The legacy projection counted the same 142
recording-scoped visits, one per raw tracker ID of each recording. A second apply added no row. That
rehearsal used a synthetic company; the published preview's import is its own commissioned step.

`python3 tools/test-database.py -q tests/test_realtime_historical_tracker.py tests/test_realtime_import_tracker_run.py`
runs the storage contract and the importer against a disposable database, including the upgrade from
`0010` with existing rows. The importer's fixtures are small synthetic run roots.

## Location trackers

Migration `0012_realtime_locations` adds eight tables and `LocationStore` (`locations.py`) is their
only writer, as `rocoto_rt` under row security. The schema holds 32 `realtime` tables at this
revision. A location's name, address, coordinates and business scope stay in the video preview's own
stores; PostgreSQL holds the identity the tracker tables hang from.

| Table | Holds | Written by (capability) |
| --- | --- | --- |
| `location` | identity (`location_id`, `collection`, `business_type`), `run_seq` (what a commit expects), `page_seq` (the page's revision), `through_sequence` (the highest event sequence a committed run read), the run policy with its revision, a manual request, back-off and the lease | `register_location`, `set_policy` (configure); `commit`, `request`, `claim`, `finish` (process); a tracker change moves both revisions (configure) |
| `location_source` | the cameras of a location: one `source` of kind `camera` per row with its `camera_id`; a camera belongs to one location | `bind_source` (configure) |
| `location_tracker` | one tracker: key, Core status, kind, chat, state and its head (the active revision of each part, moved one `head_revision` at a time) | `install_trackers`, `activate` (configure) |
| `location_tracker_revision` | append-only revisions of the parts `definition`, `script` and `presentation`, each with its digest and the conversation that wrote it | `install_trackers`, `save_revision` (configure) |
| `location_tracker_output` | the current output of a tracker, updated in place: `tile`, `canvas`, `periods`, `period`, `input`, `digest`, `revision` | `commit` (process) |
| `location_tracker_output_revision` | the ledger: one row per published revision with its digest, period, input and a summary of the tile | `commit` (process) |
| `location_record` | retail records by kind, each identified within its location; a row keeps the basis it was written with (`derived`, `ingested` or `configured`) and draws a higher `mark` at every write | `commit` for derived rows (process); `put_records` (ingest for ingested, configure for configured) |
| `location_run` | the newest 200 runs of a location: trigger, status, times, input stamp, local day, wall time, bounded outputs and errors, and the digest and answer of the run's one commit | `claim`, `commit`, `finish` (process) |

Rules the database holds, each with the SDK refusing first:

- **The wall.** Every table has FORCE row security. `realtime.location_visible` reads a location
  with its cameras: every bound camera is visible to the principal, and a location that holds no
  camera yet is read by the principals who configure the company. `realtime.location_reach` answers
  the principal's locations once per statement, so a policy checks each row against that answer.
- **Processing and ingestion follow each camera's grant.** `location_reach('realtime:process')`
  asks `realtime.source_processable` for every camera of the location and
  `location_reach('realtime:ingest')` asks `realtime.source_writable`, the rules the camera's own
  outbox and events follow. A grant that only reads a camera reads its location and writes nothing
  of it: `claim`, `request`, `commit`, `finish` and `put_records` answer `AccessDenied`, and the
  policies refuse the same statements as raw SQL.
- **One location row, two writers.** The trigger `protect_location` holds each part of the row to
  its capability wherever row security binds the caller: the run policy and its revision change
  with `realtime:configure`; the cursor, the lease, the back-off, the request and the run pointers
  change with `realtime:process`. `run_seq`, `page_seq` and `policy_revision` move forward for
  every writer. The check `location_policy_shape` (`realtime.schedule_policy_valid`) keeps the
  stored policy inside what `SchedulePolicy` accepts, so `state` and `finish` always read it.
- **Core Trackers are locked.** A row with `core = true` keeps its key, kind and chat, holds a
  definition only and stays active: the constraints `location_tracker_core_locked`,
  `location_tracker_core_kind`, `location_tracker_revision_core_locked` and
  `location_tracker_revision_core_flag` and the identity trigger refuse the rest. The SDK answers
  with `CoreLocked`. A Core definition changes through `install_trackers`, which appends the
  recipe's next definition revision and moves the head. Which definition a Core Tracker holds is
  configuration's to install: the tables take a definition revision from any principal with
  `realtime:configure`. The runner holds the other half of the lock. It runs a Core Tracker's
  `processor.script` in its own process only when that script equals the one the maintained recipe
  names ([`core_script`](../../harness/verticals/retail/core_trackers/__init__.py)); a runner on
  `LocationStore` keeps that check.
- **Revisions are append-only.** `save_revision` names the newest revision the caller read
  (`expected_revision`); `activate` names the head it read (`expected_head`). A stale value is a
  `Conflict`. A head points at saved revisions of its own tracker (deferred foreign keys).
- **Records keep their kind, basis and location.** The check `location_record_kind_basis` holds the
  pairs of `RECORD_KINDS`: a run derives observed items, baskets, shelf states, queue intervals and
  the page; evidence and the till are ingested; the catalog and the zones are configured.
  `put_records` writes ingested and configured rows, `commit` writes derived ones. A record, an
  output and a ledger row are identified within their location, so a writer meets rows of the
  locations it reaches. A derived row that leaves a rebuild turns inactive.
- **One holder, one commit.** `claim` gives a location one holder on database time. `load` answers
  the snapshot a run reads: cameras, trackers with their active revision bodies, current outputs,
  the latest active revision of each live event with its actor, and the active records, bounded by
  the context's event and byte limits. `commit` takes the claim's `run_id` and `lease_token` and the
  snapshot's `run_seq`. The pass that holds the location commits; a pass whose lease another pass
  took over gets a `Conflict` and changes nothing. `run_seq` moves with every commit and every
  tracker change, so a snapshot read before either commits nothing. The commit writes the derived
  records, the outputs, the period readings, the cursor and the finished outbox rows in one
  transaction under the company lock. An output whose digest equals the current one stays as it is;
  a new digest rewrites the current row and adds one ledger row. `through_sequence` is an event
  sequence of the location. A run commits once: an exact retry of its commit returns the first
  answer, and other content from the same run is a `Conflict`.
- **The page's revision follows the page.** `page_seq` moves when a commit or a tracker change
  altered what the page shows; a pass that arrives at the same outputs leaves it where it is.
- **Bounded sizes.** Table checks bound every document: definition, script and presentation 256 KiB,
  tile 256 KiB, canvas and period readings 1 MiB each, a record 64 KiB (the `page` record 1 MiB),
  input 8 KiB, ledger summary 4 KiB, policy 4 KiB, a run's outputs 32 KiB and errors 16 KiB. The SDK
  measures a document the way PostgreSQL prints jsonb, every number in full, and refuses text that
  holds NUL. A refusal of a byte check that reaches the tables arrives as `CapacityExceeded`, and a
  value PostgreSQL does not store as `ValueError`. The SDK also keeps 64 locations and 100,000
  records per company and 128 trackers and 64 cameras per location. `realtime.location_usage` adds
  every row to the company byte ledger.
- **Rows stay.** The runtime role holds `select`, `insert` and named column updates on these tables.
  Run number N lives in slot (N-1) mod 200 of its location, so run N takes the place of run N-200
  with an update in place and the table holds 200 rows per location.

`home` returns the page keys (`core_trackers`, `core_tracker_canvases`, `products`, `currency`)
with `page_seq` as `revision`, and a short `unchanged` answer for a caller that names the revision
`home` gave it; any other value gets the whole page. `claim_events` leases the live outbox rows of
location cameras, grouped by location, and `commit` finishes them. `input_stamp` is a digest of what
a run reads: the events of the location's cameras with their bindings, the ingested and configured
records, the cameras and the tracker heads. Every write of a record draws a higher `mark` from one
sequence (the trigger `mark_location_record`), and the stamp carries the count and the sum of marks
of the location's input rows, so it moves with each committed write in whatever order the writers
began and committed. `realtime.location_inputs` answers those counts and high-water marks in one
call for a principal who reads the location, so a probe can ask every few seconds.

The schedule methods carry the names and return shapes of the preview runner's `TrackerRuns`:
`policy`, `set_policy(expected_revision)`, `request`, `claim`, `finish`, `runs`, `state` and
`last_run`. An expired holder's run closes as `interrupted` at its lease's end when the next pass
takes over, and reads as `interrupted` from that moment in `runs`, `last_run` and `state`; its
holder can still commit and close it while every other pass stays away. `finish` acts for the
holder's token. The unattended runner acts as the company owner; a service actor with
`realtime:process` and a process grant on each camera fits the same policies when one is
commissioned.

`python3 tools/test-database.py -q tests/test_realtime_locations.py` runs the storage contract
against a disposable database, including the upgrade from `0011`, raw SQL as `rocoto_rt` with another
company's settings and one pass with a plain role owning the tables. On 11 October 2026, at the
seeded demo's volume (1,735 events, 7,500 records, nine outputs of about 100 KiB), `load` took 1.5 s,
an unchanged `commit` 0.15 s and `home` 0.02 s on the development box; with 300 events `load` took
0.4 to 0.6 s and `input_stamp` 0.01 s. The same script after the fenced commit, at 300 events and
7,500 records with the box under shared load (load average above 20 on four CPUs), gave an
unchanged `commit` of 0.35 to 0.66 s, an exact retry of a commit 0.13 s, `home` 0.04 s, a short
`unchanged` answer 0.01 s, `input_stamp` under 0.01 s and `claim` with `finish` 0.05 s. The Python
work of that commit, measured alone, went from 0.026 s to 0.037 s with the commit digest.

## Admission

The trusted host resolves company, principal, conversation, exact capabilities and deadline.
`RealTimeStore` rechecks current membership using the restricted database role on every operation.
Owners can configure sources/profiles; service workers additionally need explicit source grants.
Workers register actual input receipts before submitting canonical events. Admission verifies saved
profile/event names, receipt identity and source bounds, and commits each event and its processing
outbox row in one transaction. Invalid evidence is retained in a bounded quarantine. Conflicting
identities raise a conflict; retries of identical revisions reuse the original event.

Video timestamps preserve their original source window. An anonymized dataset can have a source
clock and an unknown UTC capture time. Ingestion time describes the host receiving the data. A
connector's effective timestamp describes its business fact. These clocks have separate fields.
Processing provenance names each input event revision; a later
correction cannot silently change the evidence a processing result claims.
A source's record/revision identifiers persist through retries and adjacent worker windows.

Leases use fresh tokens and database time. Expired work can be reclaimed, stale completions fail,
and five expired attempts leave a retained failed job. A consumer must make its own effects
idempotent using the event and revision; this outbox provides at-least-once work delivery.
Replay work uses an explicit separate lane. Derived events preserve that replay lineage; a
processing result cannot enter live delivery by discarding its replay context.

## Resource limits

Admission serializes the company budget check with the write. Retained usage includes sources,
profiles, receipts, admitted events and quarantine across all company sources, including those a
particular worker cannot read. The initial allowance is 128 MiB of accounted record bytes and row
caps; provider/disk/index overhead is covered by the host's separate operational reserve. Reads
apply the byte allowance in SQL before materializing event JSON in the process. These limits require
an explicit retention/archival policy before continuous production; exceeding a limit stops
admission rather than silently deleting evidence.

## Verification

From the repository root:

```sh
python3 tools/test-database.py -q tests/test_schema_migrations.py tests/test_realtime_contracts.py tests/test_realtime_provision.py tests/test_realtime_store.py tests/test_realtime_replay.py tests/test_realtime_replay_store.py tests/test_realtime_subjects.py tests/test_realtime_modules.py tests/test_realtime_module_store.py tests/test_realtime_replay_worker.py
```

The runner creates its own PostgreSQL cluster, applies the reviewed migration chain, supplies the
restricted runtime/control roles, runs the tests and removes only that disposable cluster. Fixtures
are marked test data and make no provider/model calls. The two host commands have their own files,
`tests/test_realtime_provision.py` and `tests/test_realtime_import_tracker_run.py`.

The runner's owner role is the container's superuser. The video preview's
[commissioning rehearsal](../../docs/HISTORICAL-RECORDS.md#record-3)
applies the same chain and both host commands as a plain owner role beside `rocoto_rt`, the way a
host creates them.

The combined schema, foundation, subjects, modules and replay-worker suite passed **166 tests** on
October 9, 2026. Proofs include transactional failure injection, copied-history revocation,
replay restart/concurrent ticks, two separately identified replay loops, pickup/examination/return,
empty/replenished stock and recovery after admission before projection. These are disposable-database
fixtures, independently of actual model results.

Visits begin with a source asset and may continue across files through a registered tracker epoch,
immutable clock mappings and receipt-backed observed segments. The continuous reducer uses the
stream clock while preserving each chapter’s original file and local time. Camera-to-camera
associations remain a separate commissioning requirement; equal raw tracker IDs are insufficient.
