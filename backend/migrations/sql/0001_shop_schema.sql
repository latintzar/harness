-- the dual-write outbox: one row per (sancho_order_id, seq) projection intent
create table if not exists medusa_sync (
  id              bigserial primary key,
  tenant_id       text not null default 'rocoto',
  sancho_order_id text not null,
  sancho_seq      int  not null,
  op              text not null,              -- create_order | edit_order | fulfill | cancel
  status          text not null default 'pending',  -- pending | in_flight | done | dead
  medusa_order_id text,
  idem_key        text not null,             -- '{sancho_order_id}:{seq}:{op}'
  attempts        int  not null default 0,
  not_before      timestamptz not null default now(),
  last_error      text,
  payload         jsonb not null default '{}'::jsonb,
  seeded          boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (idem_key)
);
create index if not exists idx_medusa_sync_drain on medusa_sync (status, not_before);
create index if not exists idx_medusa_sync_order on medusa_sync (sancho_order_id, sancho_seq);

-- catalogue_skus additive columns (Medusa = product master; this stays the derived read-model)
alter table catalogue_skus add column if not exists medusa_variant_id text;
alter table catalogue_skus add column if not exists medusa_product_id text;
create unique index if not exists uq_cat_skus_variant on catalogue_skus (tenant_id, medusa_variant_id)
  where medusa_variant_id is not null;
alter table catalogue_skus add column if not exists live_on_ecom boolean not null default false;
alter table catalogue_skus add column if not exists image_url text;
alter table catalogue_skus add column if not exists customizations jsonb not null default '[]'::jsonb;
-- per-product ASAP / out-of-hours fulfilment envelope (the storefront shows "order now, ready by …").
-- Additive: null/[] means "no special envelope" (the default storefront copy applies).
alter table catalogue_skus add column if not exists asap_envelope jsonb not null default '{}'::jsonb;

-- e-commerce-source flag on the order projection: a storefront-originated order (basket → submit →
-- outbox) carries from_storefront so the supplier's "e-commerce orders" view can filter to its channel.
-- Folded in events.py from the 'created' event; persisted here so the read-model can query it.
alter table orders add column if not exists from_storefront boolean not null default false;
create index if not exists idx_orders_storefront on orders (from_storefront, created_at desc)
  where from_storefront;

-- ── Acceptability engine: the auto-accept allow-list (acceptability-engine.md §2) ──
create table if not exists accept_rules (
  id           text primary key,
  tenant_id    text not null default 'rocoto',
  kind         text not null,                 -- asap | qty | customization
  scope        text not null default 'tenant',-- tenant | customer
  phone_number text,                          -- E.164 (region ES); required when scope='customer'
  pattern      jsonb not null default '{}'::jsonb,
  enabled      boolean not null default true,
  created_from text not null default 'manual',-- recommendation:<id> | manual | seed
  hit_count    integer not null default 0,    -- audit-only; never feeds the decision
  last_hit_at  timestamptz,
  seeded       boolean not null default false,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists idx_accept_rules_lookup on accept_rules (tenant_id, kind, enabled);
create index if not exists idx_accept_rules_customer on accept_rules (tenant_id, phone_number)
  where scope='customer';

-- ── Modular scheduled tasks per employee (employee-scheduled-tasks.md) ──
create table if not exists agent_tasks (
  id          text primary key,              -- 'atk_<task_key>__<tenant>'
  tenant_id   text not null default 'rocoto',
  agent_id    text not null,                 -- ecom | deliveries | orders
  task_key    text not null,                 -- e.g. 'ecom.standardize_customizations'
  enabled     boolean not null default true,
  cadence     text not null default 'weekly',-- daily | weekly
  watermark   jsonb not null default '{}'::jsonb,  -- {cursor_ts, last_id, version}
  last_run_at timestamptz,
  last_result jsonb,                          -- ALWAYS written: {status, scanned, produced, ...}
  running     boolean not null default false, -- UI hint; advisory lock is the real guard
  seeded      boolean not null default false,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (tenant_id, task_key)
);

-- ── Owner-accepted recommendation cards (the scheduled-task output) ──
create table if not exists agent_recommendations (
  id          text primary key,              -- 'rec_<random>'
  tenant_id   text not null default 'rocoto',
  agent_id    text not null,
  task_key    text not null,
  kind        text not null,                 -- customization | asap | qty | consolidation | autoaccept
  dedupe_key  text not null default '',
  payload     jsonb not null default '{}'::jsonb,
  status      text not null default 'open',  -- open | accepted | dismissed | superseded
  result      jsonb,                          -- what accept() did: {accept_rule_id?, option_id?, ...}
  seeded      boolean not null default false,
  created_at  timestamptz not null default now(),
  decided_at  timestamptz,
  unique (tenant_id, task_key, dedupe_key)
);
create index if not exists idx_agent_recs_open on agent_recommendations (tenant_id, agent_id, status);

-- ── Storefront custom-order capture: a draft basket that lands in the supplier's WhatsApp thread ──
-- The customer composes a non-standard custom on the storefront (composer + mic); it is captured here
-- as a DRAFT (a person must decide it) and surfaced in the supplier's conversation as a BasketRequestCard.
-- On accept it converts to a placed order (submit_order → outbox → Medusa) + the warm pay-link is sent.
create table if not exists basket_sessions (
  id              text primary key,            -- 'bsk_<random>'
  tenant_id       text not null default 'rocoto',
  phone_number    text not null,               -- E.164 (region ES) — the conversation it pins to
  customer_name   text not null default '',
  lines           jsonb not null default '[]'::jsonb,   -- [{item,qty,unit,price,custom}]
  custom_note     text not null default '',    -- the composer text / dictation transcript
  total           numeric,
  status          text not null default 'submitted',    -- submitted | accepted | paid | cancelled
  pay_link        text not null default '',
  medusa_order_id text,                          -- filled on accept (the placed order)
  version         int  not null default 0,       -- optimistic concurrency (accept-vs-edit race)
  wa_message_id   text,
  seeded          boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists idx_basket_sessions_phone on basket_sessions (tenant_id, phone_number, status);
-- Additive hardening (Stage 5a): an unguessable, TTL'd access token gates every PUBLIC read/pay route
-- (the bsk_ id is short + appears on wa.me / pay surfaces → IDOR risk). Minted at create, carried in the
-- wa.me link + widget payload, required on public read/pay, expired on accept/pay. `phone_verified` records
-- whether the basket's phone passed OTP — the toll-fraud gate for outbound paid SMS (no SMS before verify).
alter table basket_sessions add column if not exists access_token text;
alter table basket_sessions add column if not exists access_token_at timestamptz;
alter table basket_sessions add column if not exists phone_verified boolean not null default false;
create index if not exists idx_basket_sessions_token on basket_sessions (access_token)
  where access_token is not null;

-- ── Stripe Connect: the supplier's payout (Express) account, so card payments settle to THEM ──
create table if not exists shop_connect (
  tenant_id         text primary key,
  stripe_account_id text,                          -- acct_…
  charges_enabled   boolean not null default false,
  payouts_enabled   boolean not null default false,
  details_submitted boolean not null default false,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

-- ── Stripe SUBSCRIPTIONS: OUR SaaS billing of the supplier for their AI employees (brain/stripe_subs.py).
-- DISTINCT from shop_connect above (that pays the supplier OUT; this bills the supplier). One row per
-- tenant. `stripe_customer_id` is the platform-side Customer (cus_…) the Marco/Hugo subscriptions hang
-- off; `ecommerce_enabled` is Carmen's 0.3%-application-fee enable toggle (NOT a recurring price) with a
-- one-month lock-in noted from `ecommerce_enabled_at`. The active-subscription truth lives in Stripe —
-- subscription_status() reads it live; this table only holds the customer id + the Carmen flag.
create table if not exists employee_subscriptions (
  tenant_id           text primary key,
  stripe_customer_id  text,                          -- cus_…
  ecommerce_enabled   boolean not null default false,-- Carmen's 0.3% application-fee enable
  ecommerce_enabled_at timestamptz,                  -- when it was last turned on (one-month lock-in clock)
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
-- Additive guards (re-running on an older DB is safe; mirrors the alter-table-add-column-if-not-exists style).
alter table employee_subscriptions add column if not exists stripe_customer_id text;
alter table employee_subscriptions add column if not exists ecommerce_enabled boolean not null default false;
alter table employee_subscriptions add column if not exists ecommerce_enabled_at timestamptz;

-- ── Tenant registry: the canonical list of supplier shops (= teams). The customer directory reads
-- this; `tenant_id` on every other table is this `slug`. Rocoto is the first row — test-marked,
-- seeded — and onboarding a real company INSERTS another the exact same way (nothing hardcoded).
-- The Medusa scoping ids (publishable key + sales channel + region) give each shop native catalogue
-- isolation; lat/lng drive the directory's distance sort; `plan` is a forward hook for per-team pricing.
create table if not exists tenants (
  slug                     text primary key,
  name                     text not null,
  tagline                  text not null default '',
  status                   text not null default 'draft',   -- draft | test | live
  seeded                   boolean not null default false,
  address                  text not null default '',
  city                     text not null default '',
  region                   text not null default '',
  country                  text not null default 'ES',
  lat                      double precision,
  lng                      double precision,
  hours_open               int not null default 8,
  hours_close              int not null default 20,
  hero_image               text,
  medusa_publishable_key   text,
  medusa_sales_channel_id  text,
  medusa_region_id         text,
  medusa_stock_location_id text,
  currency_code            text not null default 'eur',
  plan                     text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);
create index if not exists idx_tenants_status on tenants (status);
-- Shop info sections (the clickable hero rows → modal/subpage): every shop can show its own About,
-- allergens, and delivery/payment info. Additive so existing tenants rows gain them.
alter table tenants add column if not exists about text;
alter table tenants add column if not exists allergens text;
alter table tenants add column if not exists delivery_info text;
-- ── THE INBOUND ROUTING KEY (2026-07-27, multi-tenancy pass). Meta's NUMERIC phone-number id — the
-- one that rides every Cloud-API webhook as `value.metadata.phone_number_id`. It is NOT the E.164 in
-- `number` (that is the human-readable line; Meta never sends it on an inbound), which is why the
-- routing needs its own column rather than reusing `number`. `tenancy.tenant_for_wa_number_id` reads
-- exactly this. UNIQUE where present: two tenants cannot claim the same business line, and a NULL
-- simply means "this tenant has no WhatsApp line bound yet" (the fallback then serves it, loudly).
alter table tenants add column if not exists wa_phone_number_id text;
-- §17.6 / §17.34 — the Behaviour page's tenant-level knobs (hold_new_customers, off-list posture…).
-- One additive jsonb; OFF-by-default semantics live in the readers, never in a backfill.
alter table tenants add column if not exists behaviour jsonb not null default '{}'::jsonb;
create unique index if not exists idx_tenants_wa_phone_id on tenants (wa_phone_number_id)
  where wa_phone_number_id is not null;

-- ── Operator teams: the people who run shops + their memberships. An operator belongs to one or more
-- teams (= tenants) and operates as ONE active team at a time (the founder toggles between them in
-- settings). This is the typed contract behind multi-team operation and the seam for per-team pricing.
create table if not exists operators (
  id            text primary key,            -- 'op_<handle>'
  email         text unique,
  name          text not null default '',
  is_founder    boolean not null default false,
  active_tenant text,                          -- the team currently being operated (→ tenants.slug)
  seeded        boolean not null default false,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create table if not exists team_members (
  operator_id text not null,
  tenant_slug text not null,
  role        text not null default 'staff',  -- owner | founder | staff
  seeded      boolean not null default false,
  created_at  timestamptz not null default now(),
  primary key (operator_id, tenant_slug)
);
create index if not exists idx_team_members_op on team_members (operator_id);
