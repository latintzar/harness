# Retail vertical

Public source release: use the root README and docs/OPENAI-SETUP.md for a new installation. Historical commissioning/deployment references describe the original project; source footage, account grants and private receipts are outside this export.
Retail is Rocoto's first vertical: the Organization agent helps a shop connect its cameras, catalog and till data, then keeps useful trackers, displays and alerts current. It runs on the [shared harness](../../mechanics/README.md) and [managed OpenAI core](../../runtime/README.md). The video demo is this vertical with the demo company's seeded data.

[vertical.json](vertical.json) declares its prompts, fixed conversations, classes, tools, vocabulary, sources, displays and setup jobs. [retail_host.py](retail_host.py) installs its real services and scope checks. Business-specific Python and web assets live in this folder.

## Conversations and classes

Organization holds business setup, locations, catalog, till and required coverage. Cameras handles source connection, privacy, view geometry, clocks and readiness. Messaging setup belongs to each member's verified channel. These are the shared persistent conversation mechanics with retail instructions.

Retail declares Action agents with `granted_effects` and Trackers with `participant_reach`. Both consume committed tracker events through the retail input provider. A Tracker's permitted phone reach ends at its current participants; live delivery additionally requires the retail live-observation lane. Action effects require the connected system's actual account, grant, approval and spending authority.

Saved archive-camera definitions retain history and their retirement status. Current execution follows committed observation inputs and exact event selection.

## Core Trackers

[core_trackers/retail.json](core_trackers/retail.json) is retail's declared display set. It installs nine maintained Core Trackers for a location: Visitors, Product interactions, Picked up, Put back, Basket, Queue time, Empty shelves, Sales and Conversion. Their processors read the same admitted location records and publish one output revision for each tile and canvas.

Core Trackers compute deterministically and belong to the canonical Organization agent. A shop's own linked tracker retains its actual agent identity, script, saved presentation and separate run and wake policies. [PAGES](PAGES.md) explains projection and viewing; [DATA](DATA.md) explains source provenance.

The runner responds to admitted input changes, local-day changes and explicit requests. Its location lease covers derived records and publication; seed commands, imports and scheduled passes use that same lease. Script calculation passes through the common signal chain, preserving one signal through optional agent delivery. The policies are never, on failure, on change and every run; current source, script revision and camera scope remain required.

## Ready-made agents and skills

[ready_agents.json](ready_agents.json) offers Queue watch, Empty shelf watch and Shop summary. Selecting one saves an inactive draft in the same chat, preserving declared class, input, mode, interval, run allowance and event types. An empty initial profile match keeps those event types so later unrelated data cannot broaden the job. Earlier definitions remain in [the retired catalogue](retired/ready_agents.json) for existing conversations.

Queue watch follows queue events; Empty shelf watch follows shelf state changes; Shop summary combines admitted observations with current Core Tracker outputs. Each saved draft still requires readiness and activation. Phone readiness depends on the commissioned channel, verified recipient and current spending grant.

[skills.json](skills.json) seeds retail instructions. The live registry determines which seed tools are available; `get_harness` reports the actual gaps. A seed's presence is an instruction and dependency declaration, rather than a source or connection commissioning receipt.

## Bringing a shop on board

The Organization setup recipe reads existing workspace facts, confirms the shop and locations, establishes catalog and till sources, and records actual source readiness. Camera setup preserves vendor/source metadata, normalized regions, time basis, overlap and privacy decisions. A camera's connection, recording availability and admitted observation stream are separate readiness facts.

Catalog imports retain source, currency and whether a price is observed or estimated. A visible product interaction is evidence about an observed item; linking it to a catalog entry requires its own provenance. Sales and conversion state their transaction basis and coverage.

Setup conversations use the shared private credential requests and clarification controls. Recipes retain their original revision, prerequisite completion and observed evidence through interruption. [The SDK](../../sdk/README.md) owns the generic job contract.

## Product boundaries and verification

The existing published retail service is `https://video-demo.soyrocoto.com`; [deployment](../../../README.md#run-the-bare-workspace) distinguishes that release from current source. The seeded demo uses preserved observations and generated history, labelled in its data. Live camera acquisition and provider delivery need their own commissioned service receipts.

Retail tests cover exact event filters, source visibility, retained camera admission, scope changes during awaits, script incarnation and cancellation, location write leases, presentation deletion and the nine maintained outputs. Browser proofs exercise Home, canvases and camera evidence at desktop and phone sizes.


## Settings and page ownership

[.env.example](.env.example) declares retail settings alongside the [shared harness example](../../.env.example). [retail_settings.py](retail_settings.py) is the compatibility table: each canonical name wins whenever present, including an empty value; otherwise its installed alias supplies the value. The map adapter removes both spellings of provider keys before child processes can inherit them. Existing deployment files remain valid through the aliases.

| Setting | Owner and behavior |
|---|---|
| `ROCOTO_RETAIL_REALTIME_READS` | Retail reader, fixed at service startup: `legacy` or `maintained`. |
| `ROCOTO_RETAIL_LOCATION_CONTEXT` | Retail weather and maps, `on` or `off`, within the commissioned ledger. |
| `ROCOTO_RETAIL_MAPBOX_TOKEN`, `ROCOTO_RETAIL_GOOGLE_MAPS_API_KEY` | Retail map provider credentials, consumed once by the adapter. |
| `ROCOTO_RETAIL_ITEM_INSPECTION_ENABLED`, `ROCOTO_RETAIL_ITEM_INSPECTION_WORKSPACES` | Optional funded item inspection and its exact workspace list. |
| `ROCOTO_RETAIL_SHELF_CATALOG_INSPECTION_ENABLED`, `ROCOTO_RETAIL_SHELF_CATALOG_WORKSPACES` | Optional funded shelf inspection and its exact workspace list. |
| `ROCOTO_RETAIL_MODAL_PYTHON`, `ROCOTO_RETAIL_MODAL_CONFIG_PATH` | Company-owned retail Modal execution paths. |
| `ROCOTO_RETAIL_QDRANT_URL`, `ROCOTO_RETAIL_SOURCE_DERIVATIVES_ENABLED` | Retail archive-reader address and optional source derivatives. |
| `ROCOTO_TILE_RUNNER`, `ROCOTO_TILE_SCRIPT_EVENT_RUNS` | Shared display scheduling controls, consumed here by the retail adapter. |
| `ROCOTO_PUBLIC_ORIGIN` | Shared public origin used by retail setup links. |

The maintained database keeps its existing `RUNTIME_DATABASE_URL` contract. Setting names and switches select already commissioned services; provider grants, source admission, cost limits and receipts still govern execution.

[web/retail-ui.js](web/retail-ui.js) owns recording-folder setup, SFTP import status, approval review, polling and transfer progress. The shared connector page supplies live workspace context and generic lifecycle hooks. Storage receipts retain workspace, detail-generation and request-epoch checks; review-dialog ownership and focus restoration survive refreshes. A bare page loads only the generic connector form and shared assets.
