# PAGES

Retail contributes Home, its tracker canvases and the camera view to the shared tab shell. [retail-ui.js](web/retail-ui.js) owns domain composer fields, source drafts, evidence cards and viewers. [The shared SDK](../../sdk/TILES-AND-TABS.md) owns tab lifecycle, display schemas and common rendering.

## Home

Home selects the current workspace, collection and location, then reads the location's published Core Tracker outputs. Its nine tiles and their canvases share the same output revisions. A period change selects the processor's admitted day/week/month/year readings and preserves the location's time basis.

Every display projection carries its actual owner. Core displays resolve the Organization conversation; custom linked trackers open their saved agent. Saved presentation changes apply against an expected revision, and a data refresh preserves the selected period, focus, scroll and participant phone block.

The location's confirmed place can supply weather and a neighbourhood map. Product rows retain observed crop and catalog-image provenance separately. Counts, prices, sales and conversion show the basis held in their records.

## Camera view

The camera view combines registered recordings, admitted events and visit-scoped tracks. Selecting an event or visit opens its actual source interval. Stream health, recording availability, current event inputs and live service readiness are separate facts in the source status UI.

The maintained player reads the selected camera and suspends hidden work. Optional person outlines use the admitted track geometry and original time range. Camera motion and overlap require the source's geometry and synchronization evidence; a visit reference is local to the admitted track history.

The temporal basket is a projection of item interactions and intervals linked to the visit. Product identity, price and completed payment each retain their own evidence. Derived basket and sales projections do not turn visible movement into an authoritative till transaction.

## Routes and data

[retail_http.py](retail_http.py) installs the retail route groups through the selected host. [control_room_routes.py](control_room_routes.py) serves page data and saved presentation; [location_intelligence_routes.py](location_intelligence_routes.py) serves visit and event projections; [realtime_read_routes.py](realtime_read_routes.py) provides the maintained playback/read boundary. Camera, catalog, source and storage routes remain in this vertical.

The adapter reads location records and page projections produced by the current lease holder. Imports preserve existing Core Tracker output, catalog and presentation fields. Private media uses the commissioned source-media service and current owner/source admission. Browser requests retain the workspace generation and recheck it before displaying an awaited result.

## Interaction and evidence

A desktop view and a phone view use the same records. Camera playback, mask decoding and background polling are bounded by visibility. Closing or switching a surface releases its work. Long tile titles retain readable wrapping at phone widths.

The UI proof covers the nine tiles, full canvases, source selection, period changes, current scope, agent navigation and participant controls. Media and byte-transfer proofs belong to [the data source](DATA.md) and [the maintained backend](../../../backend/realtime/README.md); publication receipts live under [deployment](../../../README.md#run-the-bare-workspace).
