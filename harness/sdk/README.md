# Add a vertical

A vertical is the role the Organization agent is hired to perform in one kind of business. It supplies instructions, agent classes, roles, job recipes, tools, sources and displays over the same harness. OpenAI owns the core. The [bare vertical](../verticals/bare/vertical.json) is the smallest working example; [retail](../verticals/retail/README.md) adds the camera-based shop role.

## Source layout

Create `harness/verticals/<id>/` with its own manifest, prompts, domain modules and optional web assets. [layout.py](../layout.py) installs `runtime/`, `mechanics/`, `sdk/` and exactly the selected vertical. Module names stay unique across those folders. The layer gate rejects shared imports into a vertical; shared code consumes declared interfaces and installed callbacks.

`ROCOTO_VERTICAL=<id>` selects the folder. `python3 harness/run.py pack <id>` validates the manifest. The canonical file is `vertical.json`, with schema `rocoto.vertical.v1`. For one release, the loader reads `pack.json`, `rocoto.pack.v1`, `display_sets`, `agent_templates` and `agent_templates_retired` as fallbacks; the former product `segment` field becomes `business`. Canonical files and keys take precedence; a malformed canonical file stops loading. `ROCOTO_PACK` remains a fallback for `ROCOTO_VERTICAL`. The stable Python module remains `pack.py`. A private workspace's persisted binding fixes the vertical that owns its state.

## Manifest

[pack.py](pack.py) is the exact schema and loader. It validates bounds, relative paths, source origins and referenced bytes before installation. Changes to source-owned files contribute to the manifest digest. A referenced file stays inside its selected vertical.

| Key | Meaning |
| --- | --- |
| `schema`, `id`, `revision` | Wire version, folder identity and declared revision. |
| `product` | Name, business role, capability name and concise summary. |
| `prompts` | System instructions and optional common prefix for fixed conversations. |
| `fixed_chats` | Purposes, titles, icons, jobs, prompt paths and optional skills; includes `organization`. |
| `agent_classes` | Declared identifiers, labels, input kinds, one default and authority `participant_reach` or `granted_effects`. |
| `sub_agents` | Inline declarations or a JSON file of bounded roles. |
| `recipes` | Inline declarations or a JSON file of complete job recipes. |
| `tile_sets` | Installable collections of displays, their source file, installation tool and processor module. |
| `event_vocabulary` | Optional event types and groups, from a vertical JSON file or a display set. |
| `skills` | Optional revisioned skill seeds and required tools/connections. |
| `ready_agents` | Ready-made agents with exact class, input, instructions, mode, filters and allowance. |
| `ready_agents_retired` | Optional historical definitions used by existing saved chats. |
| `tool_families` | Module or `module:attribute` references resolved from the selected vertical. |
| `host_module` | Optional module implementing workspace installation, routes and lifespan. |
| `surfaces` | Pages and tabs, their content, producer and read tools. |
| `builtin_capabilities` | Optional selection of WhatsApp, Viber, email and voice; omitted entries are disabled. |
| `connections_expected` | Required business systems with their purpose and setup skill. |
| `data_sources` | Named sources, read tools and optional skills. |
| `wake_sources` | Declared source identities reported by the shared harness. |
| `flow` | The business workflow and its host, script or agent stages. |
| `demo` | Optional demo starter material. |
| `capabilities` | Optional facts extending the generated capability table with source evidence. |

The canonical ready-made-agent listing is `GET /api/ready-agents`, returning `ready_agents`. The previous `/api/agents/catalogue` route keeps its `templates` response for one release. Saved `agent_setup.template_id` values and `.workspace-template.json` bindings retain their existing identity. Python callers use `ready_agents()`, `retired_ready_agents()`, `tile_sets()` and `check_tile_sets()`; the former accessor names remain aliases. The earliest legacy `recipes` tile catalogue also loads, while complete job recipes keep the separate `recipes` key.

## Roles and job recipes

[job_recipes.py](job_recipes.py) validates both contracts. A role declares `id`, `title`, `job`, instruction file, `launched_by`, permitted `tools`, `model`, `effort`, `limit_cents`, `timeout_seconds` and `per_turn`. Launchers refer to actual chat purposes, class identifiers or ordinary `chat`. Its tools and budget remain subordinate to the parent's current authority. `delegate_job` executes it as a separate managed read session using an atomically preallocated slot; root and role spending caps together remain within $1. The saved parent request, call identity, actor and role deadline fence replay and restart.

A recipe declares its identity, revision, full job, instruction file, permitted launchers, ordered `steps` and final `done_when`. Each step has a unique identity, instructions, assigned role or `parent`, tools, earlier prerequisites and completion criteria. A recipe is the full job an agent follows; the display catalogue belongs in `tile_sets`.

The [bare onboarding recipe](../verticals/bare/recipes/onboarding.json) first reads the workspace, then confirms the business and connects the required work. Its [workspace-reader role](../verticals/bare/sub_agents.json) demonstrates a bounded helper. `begin_recipe` saves the original definition; `record_recipe_step` keeps revisioned progress and evidence in the owning conversation. A later manifest update leaves an existing job's saved instructions intact.

## Host installation

An optional host module exports `install_workspace(host, selected)`, `register_routes(app, host, http)` and `lifespan(app, host)`. [host.py](host.py) supplies the shared host interface and the bare extension. Installation attaches real domain services and input providers before the tool registry is sealed and scheduler recovery begins.

[agent_inputs.py](agent_inputs.py) defines the input-provider contract. Its default handles source-free work. A domain provider supplies definition validation, readiness, input preparation, claiming, recovery, completion and resource accounting through the existing agent store transaction. It creates neither a competing scheduler nor a copied queue.

[tool_families.py](tool_families.py) builds immutable registries from common families and the selected manifest. `ToolContext` separates actor, data owner, collection, conversation and opaque admission. Every invocation checks current admission. An extension can adapt its domain context and validated result fields while transports remain shared.

The extension supplies resource-specific admission, retained history visibility, result projection and optional source/service factories. Current authority is checked across awaited work. Retail's [host implementation](../verticals/retail/retail_host.py) is a complete example; the bare process starts with the retail folder physically absent.

## Displays and page assets

The shared page accepts `page_assets` from the selected host and mounts its declared tabs through the SDK. Domain DOM, source fields and media behavior stay in that vertical's assets. The [tiles and tabs contract](TILES-AND-TABS.md) covers display schemas, ownership, saved presentation, script execution and browser lifecycle.

## Build and prove a vertical

1. Begin with the bare manifest and write the role, fixed conversations and one default class.
2. Add prompts, roles, recipes, skills and ready-made definitions. Validate every reference with the loader.
3. Install actual inputs, tool families and services through explicit host hooks. Declare optional capabilities and their readiness.
4. Add displays and tabs through the shared SDK. Keep business vocabulary and resource access in the vertical.
5. Start a fresh private state directory under `ROCOTO_VERTICAL=<id>` and prove membership, revocation, restart, declared effects and artifact selection.

The loader, layer, tool-family, job-recipe, recipe-run, display and bare-startup tests are executable contracts. The bare deployment proof imports and starts the real backend while retail dependencies and files are unavailable. Use [the deployment runbook](../../README.md#run-the-bare-workspace) for local startup and the selected target's package builder.

## Manufacturer fit

The synthetic [manufacturer fixture](../tests/verticals/manufacturer/vertical.json) supplies one SQLite order table, a company-scoped read tool, Organization and Office chats, a custom Desk class and an Orders tab. Its [artifact test](../tests/test_manufacturer_fit.py) packages the ordinary shared runtime with that folder, starts the real backend with retail and the external orders checkout unavailable, and checks separate company data and membership revocation. The [tab test](../tests/manufacturer_tabs.cjs) runs the actual vertical asset through the shared mount and cancellation lifecycle.

A source vocabulary may declare `{"path":"events.json","groups":["orders"]}`. That vertical-local JSON object maps event keys to entries with required `group` and optional `label`, `scope` and `description`. Event and scope names are lowercase keys; labels and descriptions hold up to 400 characters. A vocabulary holds up to 512 types and 64 distinct groups. File bytes enter the vertical digest and package selection. Existing `{"ref":"tile_sets/retail#vocabulary","groups":[...]}` declarations use the same entry validation. Each declaration chooses one source; paths remain inside the vertical.

The existing manufacturer product owns its catalogue, pricing, private uploads and order database. Its integration moves that domain package under `verticals/manufacturer/`, adds `tile_sets`, installs real input providers when needed, registers page assets/tabs and uses the shared managed runtime and selected-vertical builder. Its present scheduled maintenance can use `input_kind: "none"` and the existing tools; whole onboarding stages become recipes with saved prerequisites and evidence. Company-specific crawling, parsing, publication and customer receipt rules stay with the vertical.

Named read roles select a model and share the parent’s bounded allowance; `delegate_job` accepts text and declared read tools. The manufacturer draft assigns image generation to a host step and declares its image model as a provider connection. Its folder can supply scoped image-inspection reads and an asynchronous parent write tool for reviewed generation, using the shared current-actor admission and durable managed-call receipt. That vertical owns image provenance, provider credentials, funded call limits and output publication. Direct image attachments to named child sessions would extend the shared delegation contract; the declared host-generation path uses the existing tool-family seam. The company order-desk fixture makes zero model or provider calls; the manufacturer’s image adapter and its commissioned-provider proof remain with that vertical.
