# DATA

Retail owns footage, camera context, committed observations, visits, items, catalog, sales and their display projections. Shared mechanics own workspace identity, messages, jobs, grants and usage. The maintained PostgreSQL event and playback authority remains in [backend/realtime](../../../backend/realtime/README.md).

Camera profile defaults and setup drafts belong to [retail_business.py](retail_business.py). Retail installs them over the shared profile revision store and exposes `/api/retail/business/settings` and `/api/retail/business/connections`; their existing `/api/business/…` aliases retain the same endpoint and admission checks. Saved profiles, camera settings and connection identities keep their original history. The common profile presents business reference fields to bare and other verticals.

## Source admission

A registered source identifies its workspace, collection, camera, recording, time basis and private media handle. Camera metadata holds location, view geometry, normalized zones, clocks and any verified synchronization group. Connection setup, source bytes, derivatives and admitted observations each have their own readiness and receipt.

Source intake and its worker bind exact source generation, host authority, byte allowance and compute grant before work. Optional SFTP and shared-link transports use their reviewed connector and acquisition policies. The source-media service owns private playback and derivatives; the application serves its admitted handles rather than exposing storage credentials or arbitrary paths.

[Source intake](../../../docs/HISTORICAL-RECORDS.md#record-18), [source media](../../../docs/HISTORICAL-RECORDS.md#record-19), [derivation](../../../docs/HISTORICAL-RECORDS.md#record-17) and [SFTP](../../../docs/HISTORICAL-RECORDS.md#record-16) have separate commissioning runbooks. Their installed state follows deployment evidence.

## Location access

[retail_business_scope.py](retail_business_scope.py) owns the location, function, room and camera graph, chat selections and retained history receipts. It keeps the existing `business-scope.sqlite` file and tables. Current account membership comes from the shared `WorkspaceAuthority`; retail layers graph revisions, selected locations and exact source cameras onto that authority.

[retail_scope_routes.py](retail_scope_routes.py) serves `/api/retail/scope` and `/api/retail/chats/{chat_id}/scope`. Saved `/api/business/scope` and `/api/chats/{chat_id}/scope` addresses remain aliases of the same authenticated endpoints. [The scope component](web/retail-scope.js) and its stylesheet load from `/static/retail/`; former scope asset addresses serve those exact bytes. The selected retail extension installs the business access controls, chat selector and Cameras settings row.

## Observations and records

An observation retains source interval, camera, profile revision, event type, original evidence and its admission provenance. Event types must belong to the declared retail vocabulary. Person references are checked against tracks visible during the event. Evaluation/reference labels remain separately identified and excluded from ordinary product wakes.

Tracked visits, observed items and baskets derive from admitted records. Gaps and uncertainty remain visible. A visible payment action is evidence of that action; transaction success and amount come from their authoritative business source. Catalog links preserve how an observed item was matched and the origin of its image and price.

Current membership and resource scope fence tools, page projections, playback and retained chat history. Script wakes capture their original camera set, tracker incarnation, published script revision and source hash. Later source moves or narrowed chats cannot widen that retained evidence.

## Processing and publication

The Core Tracker processors read one location's events, tracks, catalog and sales. A location lease covers derived writes, script work and page publication. It is shared by service passes, seed commands and imports. The current output revision carries tile, canvas, period readings and provenance; the browser reads that publication.

A custom script receives an immutable admitted snapshot. Its original broker request and cleanup receipt stay linked to the common signal. Pending or uncertain signals retain their snapshot; restart reconciles the original result before another pass. A promoted wake keeps that same signal's source authority and agent destination revision.

## Demo company

The demo is a thin seed over these product paths. [seed_retail_demo.py](seed_retail_demo.py) creates the reviewed location, installs Core Trackers, imports preserved model output and tracked clips, derives item types, imports reviewed catalog matches, adds labelled history and publishes through the normal processors. The seed itself makes zero model calls.

Preserved records carry `data_basis: model_output`; generated history carries `data_basis: generated_history`. Estimated catalog prices retain `basis: estimated`, currency and source URL. The source observation keeps its answer file and hash. These labels distinguish demonstration material from live customer evidence.

From the repository root, inspect the current command before running an authorized seed:

```bash
ROCOTO_VERTICAL=retail python3 harness/run.py seed_retail_demo --help
```

The command requires the actual private state, access state, workspace, owner and input archive. Optional catalog results and confirmed location/address data are explicit inputs. The dated [seed contract](../../../docs/HISTORICAL-RECORDS.md#record-25) preserves the original operation and data structures; its paths describe that historical setup.

## Imports, exports and retention

RetailAction and compiled-event imports pass through the same source and record admission. Repeating an import preserves stable identities and current published tracker data. Media imports retain custody and transfer receipts. Exported records and generated files are bounded by owner, conversation, current source permission and format validation.

Source bytes and derivatives follow their commissioned service's retention. Product history, private credentials and provider receipts remain in their own stores. The [schema design](../../design/POSTGRES-SCHEMA.md) describes the remaining migration work; deployed PostgreSQL authority and the prototype's local stores remain explicitly distinguished.

## Verification

Source, storage, imported-media, observation, event-filter, script-wake and maintained-playback tests cover scope and provenance. Seed and processor tests prove repeatable derived values and location lease ownership. Broker and source-service receipts prove only their recorded source closure and operations. [Deployment](../../../README.md#run-the-bare-workspace) records the source revision actually served.
