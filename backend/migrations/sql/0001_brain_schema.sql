-- seed/live separation on the existing tables (idempotent, additive).
alter table whatsapp_messages add column if not exists seeded boolean not null default false;
alter table orders            add column if not exists seeded boolean not null default false;
alter table calls             add column if not exists seeded boolean not null default false;
-- recurring/standing orders (auto-approved, no review needed) + scheduled future deliveries.
alter table orders add column if not exists recurring boolean not null default false;
alter table orders add column if not exists recurring_label text default '';
-- live intake stage shown on the "Rocoto is taking the order" loader (listening|reading|matching|deciding).
alter table orders add column if not exists stage text default '';
-- ── THE TENANT ON THE ORDER (2026-07-27, multi-tenancy pass — the customer-#2 wall).
-- `orders` is the one commerce table that was never tenant-keyed: `order_events`, `catalogue_skus`,
-- `customer_profiles`, `ingest_queue` and `medusa_sync` all carry `tenant_id` and `orders` did not,
-- so every read of the projection (the Pedidos tab, the desk day view, the dispatch Excel batch, the
-- 0.3% meter) was global. One tenant made that invisible; two make it a data-isolation failure.
--
-- NULLABLE, never `not null` — that is what makes it safe under OLD code after a healthz rollback
-- (the schema is not rolled back with the code; `db.order_insert` allowlists its columns, so old
-- code simply never names this one). The DEFAULT is set separately, at boot, from the box's own
-- `config.BRAIN_TENANT` rather than being written here as the literal 'rocoto' — see
-- `db._backfill_order_tenants`, which explains why a default is needed at all (raw `insert into
-- orders (...)` statements in brain/seed.py) and why it must not be a hardcoded slug.
alter table orders add column if not exists tenant_id text;
create index if not exists idx_orders_tenant_time on orders (tenant_id, created_at desc);
create index if not exists idx_orders_tenant_status on orders (tenant_id, status, created_at desc);
-- The same key on the raw inbound row. Nothing READS it on the text path (the webhook threads the
-- tenant in memory), but the MEDIA path crosses an `asyncio.create_task` boundary carrying only the
-- message id — so `_extract_for_msg` re-resolves the tenant from here rather than guessing.
alter table whatsapp_messages add column if not exists tenant_id text;
create index if not exists idx_wa_msgs_tenant on whatsapp_messages (tenant_id, created_at desc);
-- live in-call order draft + caller location (Stage 7), additive on the existing calls table.
alter table calls add column if not exists order_draft jsonb not null default '{}'::jsonb;
alter table calls add column if not exists location jsonb not null default '{}'::jsonb;
-- M1M2 WP-4 (eng F1, critical): the dispatch-time VALUE SNAPSHOT. Lines mostly carry NULL prices
-- (extraction only records STATED prices), so the 0.3% meter can never re-derive value later —
-- the resolved euro value is FROZEN onto the order row AT dispatch (rule: line.price → tenant
-- catalogue price → flag unpriced). SET-ONCE (write only where null): a re-dispatch or price edit
-- never rewrites history the meter already billed. value_snapshot carries the per-line audit
-- ({lines:[{item,qty,unit_price_eur,source,value_cents}], unpriced_lines, at}).
alter table orders add column if not exists value_snapshot_cents bigint;
alter table orders add column if not exists value_snapshot jsonb;
-- «Paga con tarjeta» on the customer portal (brain/order_payment.py) — a SEPARATE, isolated payment
-- channel from the dispatch-time value_snapshot above and from reparto's delivery_settlements ledger:
-- a customer may want to pay the moment an order is confirmed, long before it is dispatched or a
-- driver is anywhere near it, and value_snapshot_cents is null until dispatch. SET-ONCE (the webhook
-- writes only where card_paid_at is null) so a redelivered Stripe event can never double-record.
-- card_invoice_* are the REAL Stripe-issued invoice on the distributor's own connected account
-- (invoice_creation on a direct charge — see order_payment.py) — populated only once payment lands.
alter table orders add column if not exists card_checkout_session_id text;
alter table orders add column if not exists card_paid_at timestamptz;
alter table orders add column if not exists card_paid_cents bigint;
alter table orders add column if not exists card_invoice_hosted_url text;
alter table orders add column if not exists card_invoice_pdf_url text;
-- §5.8a/WP-5 · THE PER-ORDER DELIVERY-WINDOW OVERRIDE («hoy tráemelo antes, que tengo boda»). The
-- window belongs to the CUSTOMER (their receiving hours, on customer_profiles below); THIS is the
-- exception for one delivery, and it wins over the default for that order only. NULL = no override,
-- which is not the same as "no window" — the customer's default still applies. Same four columns as
-- the customer default so exactly one shape is read on both sides (brain/window.py).
alter table orders add column if not exists window_from   text;   -- 'HH:MM' local, null = open at that end
alter table orders add column if not exists window_to     text;   -- 'HH:MM' local, null = open at that end
alter table orders add column if not exists window_source text;   -- said | set | habit (see brain/window.py)
alter table orders add column if not exists window_said   text;   -- their OWN words, verbatim, when they gave words not digits

-- Grounding catalogue. One row per SKU; dense embedding + FTS search_text drive the matcher.
create table if not exists catalogue_skus (
  id text primary key,
  tenant_id text not null default 'rocoto',
  code text,
  name text not null,
  family text default '',
  base_unit text not null default 'kg',     -- kg | unit | pack | case
  pack_factor numeric default 1,            -- units per pack/case (count<->weight)
  variable_weight boolean not null default false,
  price numeric,                            -- per base_unit (EUR); null = unknown
  in_assortment boolean not null default true,
  embedding vector(1024),
  search_text tsvector,
  attrs jsonb not null default '{}'::jsonb,
  seeded boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists idx_cat_skus_tenant  on catalogue_skus (tenant_id);
create index if not exists idx_cat_skus_search  on catalogue_skus using gin (search_text);
create index if not exists idx_cat_skus_embed   on catalogue_skus using hnsw (embedding vector_cosine_ops);

-- WP-4 (IN-CALL-RUNTIME-DESIGN §6): the distilled per-tenant product DIRECTORY — Tier-0 pinned
-- knowledge for every fold. Rebuilt debounced on catalogue change; idempotent on sku_hash.
create table if not exists catalogue_directory (
  tenant_id    text primary key,
  body         text not null,
  alias_map    jsonb not null default '{}'::jsonb,
  sku_hash     text not null,
  n_skus       int not null default 0,
  generated_at timestamptz not null default now()
);

-- Learned + seeded mention->SKU aliases. phone_number null = global; else customer-scoped.
-- ── §17.45 PER-CUSTOMER PRICES (Escofruit · Dana, Mercabarna 2026-08-05) ─────────────────────
-- «Per customer, per product, TWO modes: limit to his list, or limit + a fixed price. A fixed
-- price beats the list price — for that customer alone; different customers can hold different
-- fixed prices for the same product.»
--
-- ONE ROW IS ONE DECISION about one customer and one article, and the two modes are one column
-- apart on purpose: they are the same idea (what this customer's words resolve to, and at what
-- price), which is why §17.45 puts them in the same card in the dossier. `price` NULL = "his list,
-- at my prices"; `price` set = "his list, at his price".
--
-- KEYED (tenant, phone, sku): the customer is identified by phone because that is the identity the
-- runtime resolves (customer_profiles is keyed the same way), and the tenant leads because a
-- restaurant that buys from two distributors holds a separate arrangement with each — the same law
-- WP-A1 phase C wrote into `endpoint` and `customer_profiles`.
create table if not exists customer_sku_prices (
  tenant_id    text not null default 'rocoto',
  phone_number text not null,
  sku_id       text not null,
  price        numeric,                       -- EUR per the SKU's base_unit; NULL = list price
  only_his_list boolean not null default false, -- the limit half of §17.45
  note         text default '',               -- why this arrangement exists, in the operator's words
  updated_at   timestamptz not null default now(),
  updated_by   text default '',               -- §17.34.6: every settings change answers "who changed this?"
  primary key (tenant_id, phone_number, sku_id)
);
create index if not exists ix_csp_tenant_phone on customer_sku_prices (tenant_id, phone_number);

-- §17.23 AVISOS — who gets told what, on which rail. One row per MOMENT the runtime produces;
-- an absent row means the spec's default (defaults-first, §17.14.2). An aviso is a PROJECTION
-- of a spine event, never a second stream — this table only routes, it never records.
create table if not exists aviso_routing (
  tenant_id  text not null default 'rocoto',
  moment     text not null,                    -- revision | duda | canal_caido | pedido_nuevo
  rail_app   boolean not null default false,
  rail_wa    boolean not null default false,
  rail_email boolean not null default false,
  updated_at timestamptz not null default now(),
  updated_by text default '',                  -- §17.34.6: who changed this?
  primary key (tenant_id, moment)
);

-- §17.4c/§17.36 DISPATCH SCHEDULES — many named sends per tenant (spec §2901's table, plus the
-- purpose tile, the external-consent CHECK — §17.36(5): NO path may create an unconsented
-- external send — and the per-row watermark the once-a-day law needs).
create table if not exists dispatch_schedule (
  id           bigserial primary key,
  tenant_id    text not null,
  name         text not null,
  purpose      text not null default 'otro',   -- compras|direccion|almacen|facturacion|incidencias|otro
  format_id    text not null,                  -- standard | flat | sage50 (§17.4c vocabulary)
  cadence      jsonb not null,                 -- {mode:'scheduled', at:'13:30', days:[1..7]}
  send_window  text not null default 'today',
  categories   text[] not null default '{}',   -- empty = everything
  recipients   text[] not null,
  columns      jsonb,
  enabled      boolean not null default true,
  external     boolean not null default false,
  consent_by   text,
  consent_at   timestamptz,
  last_run_at  timestamptz,
  last_run_note text default '',
  created_by   text default '',
  created_at   timestamptz not null default now(),
  constraint dispatch_external_needs_consent check (not external or consent_by is not null)
);
create index if not exists ix_dispatch_sched_tenant on dispatch_schedule (tenant_id) where enabled;

-- §17.47b INBOUND MAIL — there is no inbox: what arrives becomes a ROW on the surface it
-- belongs to (pedidos/facturas per tenant, unknown = the findable dead-letter). email_id
-- unique makes Resend's webhook retries idempotent.
create table if not exists inbound_mail (
  id          bigserial primary key,
  tenant_id   text not null default '',
  purpose     text not null,                       -- pedidos | facturas | unknown
  from_addr   text not null default '',
  to_addr     text not null default '',
  subject     text not null default '',
  body_text   text not null default '',
  attachments jsonb not null default '[]'::jsonb,
  email_id    text unique,
  received_at timestamptz not null default now()
);
create index if not exists ix_inbound_mail_tenant on inbound_mail (tenant_id, purpose, received_at desc);

-- §17.48c THE MCP AUTHORISATION SERVER's three stores. Public clients only (RFC 7591 dynamic
-- registration, no secrets); codes are one-shot PKCE-bound; tokens are OPAQUE rows (revocation
-- is a delete-adjacent update, never a JWT you cannot recall). Caps on the token are the
-- INTERSECTION rule's output — never wider than the granting user's membership (§17.34).
create table if not exists mcp_client (
  client_id     text primary key,
  client_name   text not null default '',
  redirect_uris jsonb not null default '[]'::jsonb,
  created_at    timestamptz not null default now(),
  created_ip    text default '',
  last_used_at  timestamptz
);

create table if not exists mcp_code (
  code            text primary key,
  client_id       text not null,
  redirect_uri    text not null,
  code_challenge  text not null,                  -- S256, base64url of sha256(verifier)
  resource        text not null default '',
  scope           text not null default '',
  user_email      text not null,
  tenant_slug     text not null,
  caps            jsonb not null default '[]'::jsonb,
  created_at      timestamptz not null default now(),
  used_at         timestamptz                     -- one-shot: set on exchange, refused after
);

create table if not exists mcp_token (
  id             bigserial primary key,
  access_hash    text not null unique,            -- sha256 of the opaque access token
  refresh_hash   text unique,                     -- sha256 of the rotating refresh token
  client_id      text not null,
  client_name    text not null default '',
  user_email     text not null,
  tenant_slug    text not null,
  caps           jsonb not null default '[]'::jsonb,
  scope          text not null default '',
  resource       text not null default '',
  created_at     timestamptz not null default now(),
  access_expires timestamptz not null,
  last_used_at   timestamptz,
  revoked_at     timestamptz
);
create index if not exists ix_mcp_token_tenant on mcp_token (tenant_slug) where revoked_at is null;

-- every MCP call, append-only — the cr_audit pattern (actor = the token, never a person's word)
create table if not exists mcp_audit (
  id         bigserial primary key,
  ts         timestamptz not null default now(),
  token_id   bigint,
  tenant_slug text not null,
  verb       text not null,
  args_hash  text not null default '',
  outcome    text not null default '',            -- ok | draft:<id> | error:<code>
  detail     jsonb not null default '{}'::jsonb
);

-- §17.48b THE DRAFT STORE — a change verb staged by ANY frontend (panel copilot, MCP) and
-- decided in the panel. Never deleted: accepted/rejected rows are the copilot's own history.
create table if not exists config_draft (
  id         bigserial primary key,
  tenant_id  text not null default 'rocoto',
  verb       text not null,
  args       jsonb not null default '{}'::jsonb,
  summary    text not null default '',
  state      text not null default 'pending',    -- pending | accepted | rejected
  created_by text not null default '',
  created_at timestamptz not null default now(),
  decided_by text,
  decided_at timestamptz
);
create index if not exists ix_config_draft_pending on config_draft (tenant_id, state) where state = 'pending';

create table if not exists sku_aliases (
  id bigserial primary key,
  tenant_id text not null default 'rocoto',
  alias text not null,                      -- normalized mention text
  sku_id text not null references catalogue_skus(id) on delete cascade,
  phone_number text,
  source text not null default 'seed',      -- seed | edit | learned
  weight int not null default 1,
  created_at timestamptz not null default now()
);
create index if not exists idx_sku_alias_lookup on sku_aliases (tenant_id, alias);
create index if not exists idx_sku_alias_phone  on sku_aliases (phone_number);

-- ── §5.8a · THE TWO REVIEW REASONS THAT NEEDED REAL STATE (WP-3/WP-4, 2026-07-26) ──────────────
-- The review rule (§5.8) is a CLOSED set of three sources, but two of its triggers had nowhere to
-- read from: "out of their range" and "no stock". Both are STATE, not screens — so they are tables,
-- next to the catalogue they talk about.

-- WHERE THEY DELIVER, STORED AS A RULE (§5.8a item 2; decision doc
-- decisions/delivery-range-radius-2026-07-26.md in Rocoto-Labs/company). LOCKED: a radius in km
-- from the depot. The row is a RULE and never a bare number — `range_kind`
-- names WHICH rule applies, so adding `range_kind='zones'` later reads `zones` instead of `range_km`
-- and needs NO migration of the tenants already on 'radius'. `range_km` NULL = never set: we invent
-- no limit the tenant never gave us (brain/range.py returns known=False, servable=True).
create table if not exists tenant_range (
  tenant_id   text primary key,
  range_kind  text not null default 'radius',    -- radius (shipped) | zones (the likely refinement)
  range_km    numeric,                           -- null = NOT SET. never defaulted, never guessed
  depot_lat   double precision,
  depot_lon   double precision,
  depot_label text default '',                   -- «nave de Mercamadrid» — what the boss calls it
  zones       jsonb default '[]'::jsonb,         -- unused on 'radius'; the additive seam, present now
  updated_at  timestamptz not null default now()
);

-- STOCK LEVELS (§5.8a row 3: "no hay stock" becomes a real reason instead of a screen). One row per
-- (tenant, sku) — the level as last COUNTED, never inferred. `low_at` is the boss's own «me queda
-- poco» threshold; NULL is seeded from the count at capture time (brain/stock.py LOW_AT_FRACTION),
-- so the threshold is DATA per SKU and the fraction is only its seed. Inventory is OFF by default
-- per tenant (tenants.inventory_enabled), so an empty table can never make an order short.
create table if not exists stock_levels (
  tenant_id   text not null,
  sku_id      text not null references catalogue_skus(id) on delete cascade,
  qty         numeric default 0,
  unit        text default 'kg',
  low_at      numeric,                           -- null = no threshold set for this SKU
  source      text default 'capture',            -- capture | voice | manual | import
  captured_at timestamptz default now(),
  note        text default '',
  primary key (tenant_id, sku_id)
);
create index if not exists idx_stock_levels_tenant on stock_levels (tenant_id);

-- THE CORRECTION LOOP (§5.8b Inventario — the feature Cal AI never gave you). A capture records
-- WHAT ROCOTO READ (`read_json`) before anything is written to the levels; the boss corrects it by
-- talking; only then is it APPLIED. `corrected` remembers that he had to fix the read — that is the
-- signal that tells us whether the reading is any good, and it is lost the moment we overwrite the
-- levels first and ask later.
create table if not exists stock_captures (
  id         bigserial primary key,
  tenant_id  text not null,
  image_url  text,
  transcript text,
  read_json  jsonb default '[]'::jsonb,          -- [{sku_id,name,qty,unit,note}] as READ
  corrected  boolean default false,              -- the boss changed the read before applying it
  applied    boolean default false,              -- levels written from this capture (set-once)
  created_at timestamptz default now()
);
-- THE HALF WE WERE THROWING AWAY (2026-07-27). `brain/stock.read_capture` returns
-- {read, unmatched, note}: `read` is what grounded to a SKU and `unmatched` is the honest «esto no lo
-- encuentro» — the crate whose name is real but matched nothing in the catalogue. Until this column
-- existed the capture row stored only `read`, so the unmatched half lived exactly as long as the HTTP
-- response: an operator who put the phone down before dealing with it could never come back to it, and
-- nothing could ever count how often the reading fails to ground (which is the same product signal
-- `corrected` was added for). It is stored SEPARATELY from `read_json` on purpose — a line with no
-- `sku_id` must never be able to drift into the list `apply_capture` writes levels from.
-- `note` rides along because an empty read with «no he sabido leerlo» and an empty read with nothing
-- to say are different events, and only the note tells them apart after the fact.
alter table stock_captures add column if not exists unmatched_json jsonb not null default '[]'::jsonb;
alter table stock_captures add column if not exists note text not null default '';
create index if not exists idx_stock_captures_tenant on stock_captures (tenant_id, created_at desc);

-- Precomputed customer profile (the "always" scale + Customer DNA source).
create table if not exists customer_profiles (
  -- A PROFILE BELONGS TO A TENANT (WP-A1 phase C). The real per-tenant key is the unique index
  -- `customer_profiles_tenant_phone_key` declared in schema_runtime.sql, and the PK below is
  -- PROMOTED to (tenant_id, phone_number) by ..._customer_profiles_tenant_contract.sql once the new
  -- code is live. It is not written here because `create table if not exists` is a no-op on a live
  -- database — editing it would change fresh clones and never reach prod (the drift
  -- vital_schema_drift catches). Why it matters: one row per phone BOX-WIDE meant the lat/lon
  -- writers, keyed on phone alone, put one supplier's coordinates on the row labelled with
  -- another's tenant — which GET /api/sup/orders then returned as customer_lat/customer_lon.
  -- See A1-TENANT-SCOPE-CUTOVER §7.2.
  phone_number text not null,
  tenant_id text not null default 'rocoto',
  display_name text default '',
  typical_days jsonb not null default '[]'::jsonb,           -- [0..6] Mon..Sun
  cadence_days numeric,
  streak_weeks int default 0,
  top_products jsonb not null default '[]'::jsonb,           -- [{sku_id,name,share}]
  typical_basket jsonb not null default '[]'::jsonb,         -- [{sku_id,name,qty,unit}]
  usual_personalisations jsonb not null default '[]'::jsonb,
  phone_numbers jsonb not null default '[]'::jsonb,          -- linked staff numbers
  shared_media_refs jsonb not null default '[]'::jsonb,
  autopilot boolean not null default false,
  narrative text default '',
  stats jsonb not null default '{}'::jsonb,                  -- lifetime_value, order_count, volatility, first_seen
  seeded boolean not null default false,
  updated_at timestamptz not null default now(),
  primary key (phone_number)
);
-- the real per-tenant key (WP-A1 phase C). Declared here as well as in schema_runtime.sql because
-- db.init() is what creates this table on a fresh database, and `on conflict (tenant_id,
-- phone_number)` needs a matching unique index to resolve against from the very first write.
create unique index if not exists customer_profiles_tenant_phone_key
  on customer_profiles (tenant_id, phone_number);
create index if not exists ix_customer_profiles_phone on customer_profiles (phone_number);
-- WP-3 (their codes): the customer's OWN client code in the supplier's ERP (Sage etc.) — the
-- `codigo_cliente` column of the daily Excel. Nullable, captured conversationally or via
-- POST /api/customer/{phone}/erp-code (L2-style data, not a wizard step).
alter table customer_profiles add column if not exists erp_client_code text;
-- §5.8a · WHERE the delivery point IS, as first-class columns (WP-4). Geo lived only inside
-- `stats.location` (a jsonb blob the routes surface digs through, keyed `lng`); a RULE cannot be
-- evaluated against a blob it has to guess the shape of. `lat`/`lon` NULL = we do not know where
-- they are, and brain/range.py says so honestly rather than claiming a distance (known=False).
-- Note the spelling: `lon` here, `lng` in the legacy `stats.location`/`tenants` — range.py reads the
-- column first and falls back to the blob, so nothing that shipped has to move.
alter table customer_profiles add column if not exists address text not null default '';
alter table customer_profiles add column if not exists lat double precision;
alter table customer_profiles add column if not exists lon double precision;
-- §5.8a item 1 · the ORDER-LEVEL norm (brain/validators.validate_order). Per-line `qty_off_norm`
-- misses fifteen individually-normal lines that add to four times the usual basket. Cached here so a
-- future reader need not re-derive the median from the order history on every turn; `typical_n` = how
-- many PRICED orders it was computed from, and below validators.ORDER_VALUE_MIN_N there is no norm.
alter table customer_profiles add column if not exists typical_order_value_cents bigint;
alter table customer_profiles add column if not exists typical_order_n int not null default 0;
-- §5.8a/WP-5 · THE DELIVERY WINDOW — the customer's own RECEIVING HOURS, which is why it lives on the
-- customer and not on the order (the order carries the exception; see `orders.window_*` above).
-- Modelled honestly, and every part of that sentence is load-bearing:
--   · a window is a from/to on a LOCAL CLOCK ('HH:MM', Europe/Madrid) — never an instant, never a
--     duration, and never anything a van's position could be computed from. ROUTE OPTIMISATION IS
--     OUT OF SCOPE: this is what the CUSTOMER told us, not what we predict.
--   · it may be OPEN-ENDED — «a partir de las 8» is `from='08:00', to=null` and is a real window,
--     not a half-filled one. A window exists iff at least one bound (or `said`) is present.
--   · ABSENT IS A REAL STATE. All-NULL means nobody ever stated one, and every surface must keep
--     reading «sin franja» rather than defaulting to a shift we invented. A fabricated window is a
--     promise nobody made — which is exactly why /api/reparto shipped these fields blank until now.
--   · `said` holds their OWN WORDS when they gave words and not digits («por la mañana»). We do NOT
--     turn a phrase into clock digits: digits when they gave digits, their words when they gave
--     words, «sin franja» when they gave nothing.
--   · `source` is the PROVENANCE, and it is not decoration: a window a driver acts on must be
--     traceable to who promised it. Closed set — said | set | habit — with 'habit' present as the
--     additive seam and NOTHING WRITING IT TODAY (there is no observed delivery-time signal in this
--     product: `customer_habits` has never had a reader or a writer, and `orders.dispatched_at` is
--     when WE dispatched, not when we ARRIVED). Same construction as `tenant_range.range_kind='zones'`.
alter table customer_profiles add column if not exists delivery_window_from   text;
alter table customer_profiles add column if not exists delivery_window_to     text;
alter table customer_profiles add column if not exists delivery_window_source text;
alter table customer_profiles add column if not exists delivery_window_said   text;
alter table customer_profiles add column if not exists delivery_window_at timestamptz;  -- when it was captured (the provenance's timestamp half)

-- Per-(customer,sku) habit table — typical qty, delivery day-of-week, conditional timing.
create table if not exists customer_habits (
  id bigserial primary key,
  tenant_id text not null default 'rocoto',
  phone_number text not null,
  sku_id text,
  habit_key text not null,                  -- typical_qty | delivery_dow | conditional_timing | ...
  habit_value jsonb not null default '{}'::jsonb,
  support int not null default 1,
  updated_at timestamptz not null default now(),
  unique (tenant_id, phone_number, sku_id, habit_key)
);

-- Event-sourced, bi-temporal order log. Orders table is the deterministic projection of this.
create table if not exists order_events (
  id bigserial primary key,
  tenant_id text not null default 'rocoto',
  order_id text not null,
  seq int not null,                         -- per-order monotonic
  event_type text not null,                 -- created|line_set|line_edited|qty_changed|sku_changed|note_set|submitted|undone|status_changed|dispatched
  payload jsonb not null default '{}'::jsonb,
  valid_from timestamptz not null default now(),   -- business time
  recorded_at timestamptz not null default now(),  -- system time
  invalidated boolean not null default false,
  actor text not null default 'system',     -- system | operator
  seeded boolean not null default false
);
create index if not exists idx_order_events_order on order_events (order_id, seq);

-- Learned patterns (aliases, qty-norms, delivery-dow, personalisations) — the L5 reinforcement store.
create table if not exists learned_patterns (
  id bigserial primary key,
  tenant_id text not null default 'rocoto',
  phone_number text,                        -- null = global
  pattern_type text not null,               -- alias | qty_norm | delivery_dow | personalisation | ...
  pattern_key text not null,
  pattern_value jsonb not null default '{}'::jsonb,
  support int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_learned_patterns_lookup on learned_patterns (tenant_id, pattern_type, phone_number);

-- Durable ingest queue: write-before-process, ~2-min debounce, retry, dead-letter.
create table if not exists ingest_queue (
  id bigserial primary key,
  tenant_id text not null default 'rocoto',
  message_id text not null,                 -- whatsapp_messages.id (idempotency)
  phone_number text not null,
  status text not null default 'queued',    -- queued | processing | done | dead
  attempts int not null default 0,
  not_before timestamptz not null default now(),   -- debounce gate
  urgent boolean not null default false,
  last_error text,
  enqueued_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (message_id)
);
create index if not exists idx_ingest_queue_drain on ingest_queue (status, not_before);
create index if not exists idx_ingest_queue_phone on ingest_queue (phone_number);

-- ═══════════════════════════════════════════════════════════════════════════════════════════════
-- §5.11 EL REPARTO — the second half of an order's life.
-- Until now an order ran needs_review → approved → sent and then left the product: who took it,
-- when it arrived, whether anyone paid and what was said at the door lived in a van and in one
-- driver's head. These five tables are that half. Everything here is ADDITIVE (§ migration law).
-- ═══════════════════════════════════════════════════════════════════════════════════════════════

-- THE FRANJA, PER DAY OF THE WEEK (§5.11.1). The standing window lives on customer_profiles; real
-- receiving hours are not one number («entre semana de 8 a 11, los sábados hasta las 13»). A row
-- exists ONLY for a day the customer said DIFFERS — there are never seven rows by default, because
-- absence here means "same as always", which is the truth and not a gap. Same honesty rules as the
-- parent columns: digits only when they gave digits, `said` keeps their words, `source` is the
-- provenance (said | set | habit) and a window with no bound and no words is not a window.
create table if not exists customer_window_dow (
  tenant_id     text not null,
  phone_number  text not null,
  dow           smallint not null check (dow between 0 and 6),   -- 0 = Monday (ISO), matches typical_days
  window_from   text,
  window_to     text,
  window_source text,
  window_said   text,
  updated_at    timestamptz not null default now(),
  primary key (tenant_id, phone_number, dow)
);

-- LA RUTA — one day, one van (§5.11.0). The stop ORDER is persisted the moment it is optimised
-- because a driver is FOLLOWING it: a sequence that silently re-shuffles under him is worse than no
-- sequence at all. `driver_operator_id` null = nobody assigned yet (the very common day-one case is
-- the boss driving his own van, and he is already an operator).
create table if not exists delivery_routes (
  id            text primary key,                    -- 'rt_<tenant>_<date>'
  tenant_id     text not null,
  route_date    date not null,
  driver_operator_id text,                           -- → operators.id; null = unassigned
  state         text not null default 'planificada', -- planificada | en_curso | cerrada
  optimised_at  timestamptz,
  total_distance_m int,
  total_duration_s int,
  unassigned    jsonb not null default '[]'::jsonb,  -- order_ids VROOM could not fit in their franja
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (tenant_id, route_date, driver_operator_id)
);

-- LA PARADA — one order, at one address, on one route. THE ONLY PLACE DELIVERY STATE LIVES.
-- `pendiente → entregada → liquidada`, plus the terminal `no_entregada`. A stop is never deleted:
-- a removed one is `descartada` with a reason, because "it vanished from the route" is exactly the
-- failure this table exists to make impossible. `proof_url` is a photo taken AT the tap; `lat/lon`
-- are recorded once, at the tap, as proof of PRESENCE — never a live trail, never tracking.
create table if not exists delivery_stops (
  id           bigserial primary key,
  tenant_id    text not null,
  route_id     text not null references delivery_routes(id) on delete cascade,
  order_id     text not null,
  position     int,                                 -- VROOM's driving order; null until optimised
  state        text not null default 'pendiente',   -- pendiente | entregada | no_entregada | descartada
  failed_reason text not null default '',           -- cerrado | no_estaba | rechazado | no_llego
  delivered_at timestamptz,
  delivered_by text,                                -- → operators.id
  lat          double precision,
  lon          double precision,
  proof_url    text,
  note         text not null default '',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (route_id, order_id)
);
create index if not exists idx_delivery_stops_order on delivery_stops (tenant_id, order_id);

-- LO PENDIENTE — the money ledger, and it is APPEND-ONLY (§5.11.0 noun 4). This product has no
-- invoices; it has delivered orders, and a delivered order is either settled or it is owed. One row
-- per settlement EVENT, so a partial payment is representable and the history can never be edited
-- into a different past. `owed = orders.total − Σ settlements`, aged from delivery.
--   · THIS TABLE IS THE ONLY SOURCE OF ANY AMOUNT SHOWN ANYWHERE in the reparto surface, the credit
--     gate, or a payment link. There is no screen in this feature where a human types what is owed.
--   · `method` is a closed set. `credito` is a settlement of AMOUNT ZERO — it records that the money
--     was not taken and who agreed to that, which is a fact worth keeping and not the absence of one.
--   · `external_ref` carries the Stripe object when a rail was involved; empty for cash.
create table if not exists delivery_settlements (
  id           bigserial primary key,
  tenant_id    text not null,
  order_id     text not null,
  stop_id      bigint references delivery_stops(id) on delete set null,
  amount_cents bigint not null default 0,
  method       text not null,                       -- efectivo | bizum | tarjeta | credito
  credit_kind  text not null default '',            -- '' | billie (a third party carries it) | confianza (you do)
  external_ref text not null default '',            -- Stripe checkout session / payment intent
  recorded_by  text,                                -- → operators.id
  recorded_at  timestamptz not null default now(),
  note         text not null default ''
);
create index if not exists idx_settlements_order on delivery_settlements (tenant_id, order_id);
-- IDEMPOTENCY IS A CONSTRAINT, NOT A READ. Stripe redelivers webhooks, sometimes concurrently, so
-- «select 1 where external_ref=…» followed by an insert is a race: two deliveries can both read
-- «not settled yet» and both write the full amount, and the ledger is append-only — the customer is
-- then recorded as having paid twice with no way back. The unique index makes the second write fail
-- instead. Partial, because cash settlements legitimately have no ref and there are many of them.
create unique index if not exists idx_settlements_ref
  on delivery_settlements (tenant_id, external_ref) where external_ref <> '';

-- EL AVISO — the message that arrives at the right place (§5.11.5). Two kinds, ONE object:
-- `auto` is computed from data and cannot be dismissed (it goes away when the fact does); `nota` is
-- written by a person and is resolved through a generated questionnaire. Resolution is a
-- CONVERSATION: resolving one may raise the next, and `parent_id` is the thread.
create table if not exists avisos (
  id           bigserial primary key,
  tenant_id    text not null,
  phone_number text not null,                       -- the customer it belongs to
  kind         text not null,                       -- auto | nota
  audience     text not null default 'repartidor',  -- repartidor | jefe
  band         text not null default 'nota',        -- nota | subrayado (the attention band, not a colour)
  body         text not null,
  state        text not null default 'abierto',     -- abierto | resuelto
  created_by   text,                                -- → operators.id; null for auto
  created_at   timestamptz not null default now(),
  resolved_at  timestamptz,
  resolved_by  text,
  resolved_choice text not null default '',         -- the option they picked, verbatim
  resolved_note   text not null default '',         -- their free text / dictation
  parent_id    bigint references avisos(id) on delete set null,
  auto_key     text not null default ''             -- dedupe key for `auto` (one overdue aviso per customer)
);
create index if not exists idx_avisos_open on avisos (tenant_id, phone_number, state);
create unique index if not exists idx_avisos_auto_key
  on avisos (tenant_id, phone_number, auto_key) where kind = 'auto' and state = 'abierto';

-- THE TENANT'S CREDIT TERMS (§5.11.7). A RULE row, never bare numbers on the tenant — same
-- construction as `tenant_range`. `on_breach` DEFAULTS TO 'avisar' and that default is a decision,
-- not timidity: an employee that chases a restaurant for money without the supplier having said so
-- would be acting on the customer relationship, which §5.8's law reserves for the human. 'pedir_pago'
-- is the version Peter described and it is one tap away in Ajustes.
create table if not exists tenant_credit (
  tenant_id     text primary key,
  credit_days   int not null default 30,            -- past this, an owed delivery is overdue
  credit_limit_cents bigint,                        -- null = no ceiling set (we invent none)
  on_breach     text not null default 'avisar',     -- avisar (brief the boss) | pedir_pago (ask the customer)
  offer_terms   boolean not null default false,     -- ask «¿lo necesitas a 30 días?» before delivery
  updated_at    timestamptz not null default now()
);
