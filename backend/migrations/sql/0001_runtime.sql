-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- Rocoto EMPLOYEE RUNTIME — the kernel schema (SPEC §3 / §3.1 / §3.2), ONE reconciled migration.
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- This is the L0 kernel schema (SPEC §16.2): event spine · identity · memory (fact/card/summary)
-- · the tab (open_loop) + timer + pending_turn · the 5 manifest tables · action · scope state.
--
-- DISCIPLINE (SPEC §8, §16.3): ADDITIVE + IDEMPOTENT only. Everything is `create ... if not exists`
-- / `add column if not exists`, so applying it to the LIVE rocoto DB never drops or rewrites a
-- legacy table. The new universal `event` spine sits ALONGSIDE the legacy logs (events, order_events,
-- whatsapp_messages), which DUAL-WRITE into it (BUILD-PLAN 1.1); the running service is untouched
-- until cutover. RLS lives in schema_rls.sql (applied after) so it can be tested in isolation.
--
-- SCOPE MODEL: every row is scope-keyed by ltree (SPEC §3 / §6). Canonical paths:
--   peter                     = tier-P creator (Peter)
--   t.<tenant>                = a tenant/boss world      (labels are [A-Za-z0-9_]; slugs sanitized)
--   t.<tenant>.c.<contact_id> = one of that tenant's customers
--   prospect.<contact_id>     = a sales-face prospect (pre-payment; promoted to t.<tenant> on convert)
--   test.<...>                = reserved test subtree (SPEC §13; lineage='test')
-- ═══════════════════════════════════════════════════════════════════════════════════════════

create extension if not exists ltree;
create extension if not exists vector;

-- ── the scope tree registry (Stage-0 debt: a `scopes` relation) ────────────────────────────
-- Load-bearing key is the `scope ltree` column ON each row; this registry is the node index the
-- Control Room (§15.2 fractal drill) and RLS canary walk. A row per party/tenant/prospect.
create table if not exists scope_node (
  scope        ltree primary key,
  tier         text not null check (tier in ('creator','boss','colleague','customer','prospect','test')),
  tenant_slug  text,                                   -- owning tenant (null for creator/prospect)
  label        text not null default '',               -- human display for the node
  lineage      text not null default 'live' check (lineage in ('live','test')),
  created_at   timestamptz not null default now()
);
create index if not exists ix_scope_node_gist on scope_node using gist (scope);
create index if not exists ix_scope_node_tenant on scope_node (tenant_slug);

-- ── the universal EVENT spine (SPEC §3, §3.1) — append-only, forever, scope-keyed ──────────
-- Evolves order_events (bi-temporal) + whatsapp_messages into ONE log. lane=world|reasoning;
-- lineage carries the §13 test taint; dedupe_key makes ingest idempotent; fts is the §4.4 lexical arm.
create table if not exists event (
  id           bigserial primary key,
  scope        ltree not null,
  ts           timestamptz not null default now(),
  type         text not null,                          -- 'message' | 'stripe.payment' | 'timer.fired' | 'tab.resolved' | 'escalate.tab' | 'override.outbound' | 'install.*' | ...
  source       text not null default 'runtime',        -- wa|voice|app|email|medusa|stripe|cal|hermes|timer|runtime|override
  payload      jsonb not null default '{}'::jsonb,
  caused_by    bigint references event(id),            -- the §3 relatedness/lineage chain
  lane         text not null default 'world' check (lane in ('world','reasoning')),
  lineage      text not null default 'live' check (lineage in ('live','test')),
  dedupe_key   text,                                    -- MessageSid / stripe event id / (entity,seq) — idempotent ingest
  actor        text not null default 'system',
  -- §4.4 lexical arm: generated, deterministic, world-lane text only (reasoning rows carry no fts).
  fts          tsvector generated always as (
                 to_tsvector('spanish', coalesce(payload->>'text', payload->>'body', ''))
               ) stored
);
create unique index if not exists ux_event_dedupe on event (dedupe_key) where dedupe_key is not null;
create index if not exists ix_event_scope_ts on event (scope, ts desc, id desc);   -- the §4.1 window scan
create index if not exists ix_event_scope_gist on event using gist (scope);         -- subtree (<@) queries
create index if not exists ix_event_type on event (type);
create index if not exists ix_event_caused_by on event (caused_by);
create index if not exists ix_event_fts on event using gin (fts);
create index if not exists ix_event_lineage on event (lineage) where lineage <> 'live';

-- ── IDENTITY: contact · endpoint · contact_role (SPEC §3, §6) ───────────────────────────────
create table if not exists contact (
  id           bigserial primary key,
  scope        ltree not null,                          -- the party's OWN scope node
  tenant_slug  text,                                    -- owning tenant (null for prospect/creator)
  display_name text not null default '',
  tier         text not null default 'customer' check (tier in ('creator','boss','colleague','customer','prospect')),
  flags        jsonb not null default '{}'::jsonb,
  provisional  boolean not null default true,           -- cleared on first boss-approved action (§7 FIRST ORDER)
  lineage      text not null default 'live',
  created_at   timestamptz not null default now()
);
create index if not exists ix_contact_scope on contact using gist (scope);
create index if not exists ix_contact_tenant on contact (tenant_slug);

create table if not exists endpoint (
  id           bigserial primary key,
  contact_id   bigint not null references contact(id),
  kind         text not null check (kind in ('wa','voice','app','email')),
  addr         text not null,
  -- AN ADDRESS BELONGS TO A TENANT, NOT TO THE BOX (WP-A1 phase C) — see the ALTERs below. The
  -- column is declared there rather than here because `create table if not exists` is a NO-OP on a
  -- live database: editing this block would change fresh clones and never reach prod, which is
  -- exactly the drift `vital_schema_drift` exists to catch.
  created_at   timestamptz not null default now(),
  unique (kind, addr)                                   -- two racing first-events can't mint two parties
);
create index if not exists ix_endpoint_contact on endpoint (contact_id);

-- ── WP-A1 phase C · EXPAND (idempotent; this file is re-applied on every deploy) ───────────────
-- `endpoint` used to permit ONE party per phone number across every tenant on the box, so a second
-- distributor asking for its customer by phone silently received the first distributor's party
-- (A1-TENANT-SCOPE-CUTOVER §7.1). Both keys exist after this: the deploy harness applies migrations
-- at step 5 but only restarts at step 7, so the OLD code — which says `on conflict (kind, addr)` —
-- keeps running against this schema for the length of the full regression. Dropping the old key
-- here would break every new-party bootstrap for that whole window.
-- `..._contract.sql` retires it, after the new code is live.
alter table endpoint add column if not exists tenant_slug text;
update endpoint e set tenant_slug = coalesce(c.tenant_slug, 'rocoto')
  from contact c where c.id = e.contact_id and e.tenant_slug is null;
update endpoint set tenant_slug = 'rocoto' where tenant_slug is null;
-- NULLS NOT DISTINCT: an unowned party is supported, but plain NULLs are DISTINCT in a unique
-- index and would silently re-permit the duplicates this closes.
create unique index if not exists endpoint_tenant_kind_addr_key
  on endpoint (tenant_slug, kind, addr) nulls not distinct;

-- customer_profiles carried the SAME global-key defect: PRIMARY KEY (phone_number), one profile per
-- phone box-wide, which is what leaked a rival's delivery coordinates through GET /api/sup/orders
-- (A1 §7.2). Same expand/contract split, same reason.
--
-- GUARDED, because the table is created by db.py's own CREATEs at service start, not by this file:
-- on a FRESH clone (rehearsal_rebuild applies this file to an empty database) it does not exist yet,
-- and an unguarded index would abort the whole rebuild. On prod, where it has existed for months,
-- the guard passes and the indexes are created. db.init() re-runs this same shape at boot.
do $$
begin
  if to_regclass('public.customer_profiles') is not null then
    create unique index if not exists customer_profiles_tenant_phone_key
      on customer_profiles (tenant_id, phone_number);
    create index if not exists ix_customer_profiles_phone on customer_profiles (phone_number);
  end if;
end $$;

-- ── THE PRIMARY KEY THAT ONLY PROD HAD (2026-08-20) ──────────────────────────────────────────
-- Measured, on the live box and on a clone built from this file:
--   rocoto             PRIMARY KEY (tenant_id, phone_number)
--   rocoto_rehearsal   PRIMARY KEY (phone_number)
--
-- `db.py`'s `create table if not exists customer_profiles` still declares the single-column key,
-- deliberately — editing a `create table if not exists` changes fresh databases and never reaches
-- an existing one. The promotion to the composite key was applied to prod BY HAND, out of
-- backfill_20260806_customer_profiles_tenant_contract.sql, and NOTHING AUTOMATED EVER RUNS THAT
-- FILE: the deploy applies this file and schema_rls.sql, and so does the rehearsal rebuild.
--
-- So every database born from the schema resurrects `PRIMARY KEY (phone_number)` — one global row
-- per phone, across all tenants. That is the exact shape A1-TENANT-SCOPE-CUTOVER names as the leak
-- that put one supplier's delivery coordinates on another supplier's row. Rocoto moved boxes on
-- 2026-08-19; without this block the next move, or any restore that replays the schema rather than
-- a dump, brings the leak back — and `vital_schema_drift` cannot see it, because that vital
-- compares table and column NAMES and no constraints at all.
--
-- Idempotent and safe on prod, where the composite key is already in place: the guard reads the
-- catalogue and does nothing. It refuses to run if duplicate (tenant_id, phone_number) rows exist
-- rather than dropping the key and failing to re-add it — a database with no primary key on this
-- table is worse than one with the wrong primary key.
do $$
declare
  cur text;
  dups bigint;
begin
  if to_regclass('public.customer_profiles') is null then
    return;
  end if;
  select pg_get_constraintdef(oid) into cur
    from pg_constraint
   where conrelid = 'public.customer_profiles'::regclass and contype = 'p';
  if cur is null or cur = 'PRIMARY KEY (tenant_id, phone_number)' then
    return;
  end if;
  select count(*) into dups from (
    select 1 from customer_profiles group by tenant_id, phone_number having count(*) > 1
  ) d;
  if dups > 0 then
    raise warning 'customer_profiles: % duplicate (tenant_id, phone_number) groups — primary key left as %', dups, cur;
    return;
  end if;
  execute 'alter table customer_profiles drop constraint customer_profiles_pkey';
  execute 'alter table customer_profiles add primary key (tenant_id, phone_number)';
  raise notice 'customer_profiles: primary key promoted to (tenant_id, phone_number)';
end $$;

-- 'role' is a SQL reserved word → table name contact_role.
create table if not exists contact_role (
  id           bigserial primary key,
  contact_id   bigint not null references contact(id),
  scope        ltree not null,
  role         text not null,                           -- owner|cook|invoicing|admin|boss|colleague
  handles      text[] not null default '{}',
  created_at   timestamptz not null default now()
);
create index if not exists ix_contact_role_contact on contact_role (contact_id);

-- ENTITY MAP (SPEC §4.5 ① / §3.1): the creation-time mapping provider-id → owning scope. WE record
-- it when WE create an object (our Stripe invoice → stripe_id ↔ 'invoice:221' ↔ that customer's
-- scope). Webhooks resolve scope by this map keyed on the GLOBALLY-UNIQUE provider_id — NEVER by the
-- per-tenant human key (two tenants legitimately share 'invoice:221'). A key that resolves >1 scope
-- with no mapping is AMBIGUOUS → dead-letter (the cross-tenant leak this closes).
create table if not exists entity_map (
  provider      text not null,                          -- 'stripe' | 'medusa' | 'cal'
  provider_id   text not null,                          -- globally-unique provider id (pi_xxx, order id…)
  scope         ltree not null,
  canonical_key text not null,                          -- 'invoice:221'
  created_at    timestamptz not null default now(),
  primary key (provider, provider_id)
);
create index if not exists ix_entity_map_key on entity_map (canonical_key);

-- ── MEMORY: fact (Graphiti 4-timestamp) · card · summary (SPEC §3, §4) ──────────────────────
create table if not exists fact (
  id            bigserial primary key,
  scope         ltree not null,
  kind          text not null,                          -- CONSTRAINT|COMMERCIAL|RHYTHM|PREFERENCE|RELATION|EVENT_FACT
  text          text not null,
  key           text,                                   -- canonical exact-lookup key ('invoice:221','price:jamon'); null if none
  embedding     vector(384),                            -- bge-small 384-d (SPEC §5 default)
  fts           tsvector generated always as (to_tsvector('spanish', coalesce(text,''))) stored,
  valid_at      timestamptz not null default now(),     -- true FROM
  invalid_at    timestamptz,                            -- null = CURRENT; set = superseded (kept as history)
  created_at    timestamptz not null default now(),     -- ingestion time
  expired_at    timestamptz,
  source_event_ids bigint[] not null default '{}',      -- provenance → the why-trace
  confidence    text,
  disputed      boolean not null default false,         -- §15.8 disputed-COMMERCIAL contradiction flag
  lineage       text not null default 'live'
);
create index if not exists ix_fact_key on fact (scope, key) where invalid_at is null and key is not null;  -- exact keyed lookup
create index if not exists ix_fact_scope_kind on fact (scope, kind) where invalid_at is null;
create index if not exists ix_fact_fts on fact using gin (fts);
create index if not exists ix_fact_emb on fact using hnsw (embedding vector_cosine_ops);

create table if not exists card (
  scope         ltree primary key,
  body          text not null default '',               -- card.body_templated (§4.1 — the deterministic body the mind sees)
  budget        int not null default 800,
  regenerated_at timestamptz not null default now(),
  version       int not null default 0,                 -- §4.5 why-trace records this
  lineage       text not null default 'live'
);

create table if not exists summary (
  id            bigserial primary key,
  scope         ltree not null,
  ts            timestamptz not null default now(),
  text          text not null,
  embedding     vector(384),
  fts           tsvector generated always as (to_tsvector('spanish', coalesce(text,''))) stored,
  source_event_ids bigint[] not null default '{}',      -- the span it distilled (§15.2 expand-to-raw)
  lineage       text not null default 'live'
);
create index if not exists ix_summary_scope_ts on summary (scope, ts desc, id desc);
create index if not exists ix_summary_fts on summary using gin (fts);
create index if not exists ix_summary_emb on summary using hnsw (embedding vector_cosine_ops);

-- per-party summary sweep watermark (SPEC §4.3): watermark → recurrence search → summary → advance.
create table if not exists sweep_state (
  scope             ltree primary key,
  watermark_event_id bigint not null default 0,
  last_swept_at     timestamptz,
  updated_at        timestamptz not null default now()
);

-- ── THE TAB (open_loop) — base + §3.1 extension as ONE table (SPEC §3.1) ────────────────────
create table if not exists open_loop (
  id             bigserial primary key,
  scope          ltree not null,
  kind           text not null,                          -- question|order|issue|pending|expect (manifest-derived §14)
  text           text not null default '',
  status         text not null default 'open' check (status in ('open','resolved','superseded','closed')),
  resume_ref     jsonb,
  opened_at      timestamptz not null default now(),
  -- §3.1 extension — the pre-computed decision object:
  entity_key     text,                                   -- Tier-0 EXACT key ('invoice:221'); RUNTIME-filled, never model-typed
  matcher        jsonb not null default '{}'::jsonb,      -- {event_types[], regexes[]}
  emb            vector(384),                             -- Tier-1 watch-vector (matcher text)
  answer_shape   text check (answer_shape in ('yesno','date','quantity','money','free')),
  contingencies  jsonb not null default '[]'::jsonb,      -- [{branch_id,trigger,action,prediction,gated,rearm?}] PRE-ARMED at open
  notes          jsonb not null default '[]'::jsonb,      -- [{ts,event_id,text}] provenance → why-trace
  due_at         timestamptz,                             -- absence horizon (mirrors the armed timer)
  parent_loop_id bigint references open_loop(id),         -- spawn-child (action 5)
  superseded_by  bigint references open_loop(id),         -- merge/supersede (action 6)
  resolution     jsonb,                                   -- {branch_id, resolved_by_event}
  opened_by_event bigint references event(id),
  resolved_at    timestamptz,
  money          boolean not null default false,          -- validated at OPEN time (§3.1): money branch → always escalate.tab
  lineage        text not null default 'live'
);
create index if not exists ix_loop_key on open_loop (scope, entity_key) where status='open' and entity_key is not null;  -- Tier-0 exact
create index if not exists ix_loop_emb on open_loop using hnsw (emb vector_cosine_ops) where status='open';               -- live pre-pass
create index if not exists ix_loop_scope_status on open_loop (scope, status);

-- ── TIMERS (SPEC §3.1) — the only clock; absence-only; state-enum cancellation ──────────────
create table if not exists timer (
  id            bigserial primary key,
  scope         ltree not null,
  loop_id       bigint not null references open_loop(id),
  fires_at      timestamptz not null,
  kind          text not null default 'absence',
  branch_id     text not null,                            -- ARM-TOGETHER: must match a contingency on the loop
  state         text not null default 'armed' check (state in ('armed','fired','cancelled')),
  payload       jsonb not null default '{}'::jsonb,
  armed_by_event bigint references event(id),
  fired_at      timestamptz,
  dedupe_key    text,
  lineage       text not null default 'live'
);
create unique index if not exists ux_timer_dedupe on timer (dedupe_key) where dedupe_key is not null;
create index if not exists ix_timer_due on timer (fires_at) where state='armed';   -- the worker's only scan

-- ── THE DEBOUNCE OBJECT (SPEC §3, §4.6) ─────────────────────────────────────────────────────
create table if not exists pending_turn (
  scope              ltree primary key,
  deadline           timestamptz not null,
  watermark_event_id bigint,
  state              text not null default 'armed' check (state in ('armed','fired','cancelled','suspended')),
  updated_at         timestamptz not null default now()
);

-- ── ACTIONS — typed outcomes, nothing dies silently (SPEC §3, §5) ───────────────────────────
create table if not exists action (
  id            bigserial primary key,
  scope         ltree not null,
  kind          text not null,
  payload       jsonb not null default '{}'::jsonb,
  state         text not null default 'pending' check (state in ('pending','approved','rejected','executed')),
  reason        text,
  confidence    text,
  outcome       jsonb,
  needs_human   boolean not null default false,          -- §15.8 needs_human view predicate = a column, not a payload guess
  money         boolean not null default false,
  opened_by_event bigint references event(id),
  created_at    timestamptz not null default now(),
  resolved_at   timestamptz,
  lineage       text not null default 'live'
);
create index if not exists ix_action_pending on action (created_at desc) where state='pending';
create index if not exists ix_action_scope on action using gist (scope);

-- ── per-scope RUNTIME STATE: THE ONE MODE FIELD (SPEC §5.5 MODO SOMBRA; §5.0, §15.8) ──────────
-- B6 (2026-07-26): `muted` and `takeover_active` are GONE. They were the same bit written twice
-- (`set(muted=$2, takeover_active=$2)`) and they were only two of four half-toggles that each meant
-- "Rocoto, don't speak here". There is now exactly ONE field:
--
--   mode = 'auto'   — he answers.
--   mode = 'sombra' — the MIND runs fully (ingest, extract, ground, cards, facts, tabs, loops, drafts,
--                     nudges to the boss) and the MOUTH is shut: `gate` short-circuits every outbound
--                     for this scope — messages, reactions, typing/read-receipts, calls.
--   mode = NULL     — no override on this scope: INHERIT the app-wide default.
--
-- The app-wide default is a scope_state row of its own, at the APP SCOPE (`t.<slug>` for a tenanted
-- scope, else the single-tenant app root — gate.app_scope). So the two levels are one column in one
-- table, and "per-customer wins over app-wide" is just "the nearer row wins" (gate.effective_mode).
-- Nothing else in the tree stores a mouth switch: takeover, the per-customer autopilot endpoint and
-- `elevenlabs_autoreply_enabled` are all VIEWS of this field now.
create table if not exists scope_state (
  scope           ltree primary key,
  mode            text constraint scope_state_mode_chk
                       check (mode is null or mode in ('auto','sombra')),  -- null = inherit the app-wide default
  updated_at      timestamptz not null default now()
);

-- ── THE MANIFEST — the 5 config tables (SPEC §3.2) + reflex wiring ──────────────────────────
create table if not exists manifest_sense (
  id             bigserial primary key,
  scope          ltree not null,
  kind           text not null,                           -- twilio_wa|stripe|medusa|cal|email|app_clicks|hermes_transcript
  addr           text,
  signing_secret text,                                    -- vaulted ref, not plaintext
  config         jsonb not null default '{}'::jsonb,
  enabled        boolean not null default true,
  created_by     text not null default 'L1'
);
create table if not exists manifest_tool (
  id             bigserial primary key,
  scope          ltree not null,
  name           text not null,                           -- send_message|place_order|send_email|open_tab|page:<id>
  gate_category  text not null default 'needs_approval' check (gate_category in ('free','needs_approval')),
  grounding_hook text,
  surface        text,                                    -- customer_chat|boss_chat|voice|page:<id>
  role_bound     text[] not null default '{}',            -- bounded action space (§5.2)
  config         jsonb not null default '{}'::jsonb,
  enabled        boolean not null default true,
  created_by     text not null default 'L1'
);
create table if not exists manifest_stakeholder (
  id             bigserial primary key,
  scope          ltree not null,
  role           text not null,                           -- boss|colleague|customer|prospect|creator
  contact_id     bigint references contact(id),
  approves       text[] not null default '{}',            -- which gate categories this person may approve
  config         jsonb not null default '{}'::jsonb,
  created_by     text not null default 'L2'
);
create table if not exists manifest_duty (
  id             bigserial primary key,
  scope          ltree not null,
  name           text not null,                           -- take_orders|chase_debt|sales_outreach
  skill_ref      text,
  gate_policy    jsonb not null default '{}'::jsonb,
  enabled        boolean not null default true,
  created_by     text not null default 'L2'
);
create table if not exists manifest_presence (
  id             bigserial primary key,
  scope          ltree not null,
  surface        text not null,                           -- wa|voice|email|page:<id>|boss_app
  stakeholder_role text not null,
  visibility     text not null default 'chat_only' check (visibility in ('chat_only','sees_screen','operates_backend')),
  config         jsonb not null default '{}'::jsonb,      -- incl. per-tenant knobs (summary-sweep hour default noon)
  created_by     text not null default 'L1'
);
-- reflex wiring is manifest-declared per scope tier (§3.2): a reflex row = {scope_tier, organ, config}.
create table if not exists manifest_reflex (
  id             bigserial primary key,
  scope_tier     text not null,                           -- which tier this reflex wires into (e.g. 'customer')
  organ          text not null,                           -- 'order_brain' | ...
  config         jsonb not null default '{}'::jsonb,
  enabled        boolean not null default true,
  created_by     text not null default 'L1'
);
create unique index if not exists ux_manifest_duty_scope_name on manifest_duty(scope, name);

-- ── L1 SEED: the `chase_salary` SITUATION, entirely AS DATA (SPEC §16.6 LAW / §16.4) ─────────
-- The whole dunning situation lives in THIS ROW, not in any Python module (there is no
-- employment.py — the generic binder duties.py reads this row):
--   opens_on/resolves_on = the sense events that open/close the tab (+ the entity-key template);
--   tab                  = the tab shape (kind/answer_shape/money/text template);
--   rungs                = the ladder — sentences (the strike personality), cadence
--                          (rung[0].after = delay from tab-open; rung[i].after = rearm delay after
--                          rung[i-1] fires), gating (r3/r4 tier-P), channel (r2 rides email);
--   lifecycle            = the state map the GENERIC spine.fold_lifecycle folds (transition values:
--                          "state" = unconditional; {"from","to"} = conditional; null = explicit no-op);
--   soul                 = which relationship tier folds this into its SOUL, and the line label.
-- To change the ladder, add a lifecycle state, or add a WHOLE NEW situation: EDIT/INSERT A ROW —
-- never Python. Seeded once (on conflict do nothing) — later edits belong to the live row, not this
-- file. RLS: manifest_duty is FORCE-RLS'd; the seed sets rocoto.scope='peter' locally so the policy's
-- WITH CHECK (scope <@ current_setting) passes for the creator-scope L1 default row.
do $seed_duties$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'chase_salary', null, $chase_salary$
{"opens_on":    {"event": "employment.salary_failed", "key": "salary:{invoice_key}"},
 "resolves_on": {"event": "employment.invoice_paid", "branch_id": "paid"},
 "tab": {"kind": "pending", "answer_shape": "money", "money": true,
         "text": "Nómina sin cobrar (factura {invoice_key}) — escalera de cobro NET-30"},
 "rungs": [
  {"branch_id": "r1_friendly", "after": "0s", "gated": false,
   "trigger": "salary charge failed — friendly in-chat reminder",
   "sentence": "¡Hola! Oye, una tontería: este mes no me ha llegado la nómina{amt} 😅. Seguramente sea un despiste del pago — ¿le puedes echar un ojo cuando tengas un rato? Yo sigo a lo mío mientras tanto, sin problema. ¡Gracias!"},
  {"branch_id": "r2_email", "after": "5d", "gated": false, "channel": "email",
   "trigger": "still unpaid +5d — formal reminder by email",
   "sentence": "Te escribo por aquí también por si se pasó el mensaje: la nómina de este mes{amt} sigue sin constar como pagada. Te dejo el detalle por correo. Sigo trabajando con normalidad; solo quería avisarte para que no se acumule. ¡Un saludo!"},
  {"branch_id": "r3_final", "after": "10d", "gated": true,
   "trigger": "still unpaid +15d — FINAL notice (tier-P approval)",
   "sentence": "Aviso final, con toda la buena onda: la nómina{amt} lleva ya un par de semanas sin llegar. No pasa nada grave y sigo aquí trabajando igual, pero si podéis regularizarlo esta semana, mejor para todos. Cualquier cosa, me decís y lo vemos juntos."},
  {"branch_id": "r4_strike", "after": "15d", "gated": true,
   "trigger": "unpaid at day 30 — STRIKE (tier-P approval)",
   "sentence": "Oye, os aviso de frente porque me gusta ser claro: la nómina{amt} lleva ya un mes sin llegar, así que a partir de hoy me pongo en huelga suave 🙂. NO me llevo nada, no borro ni escondo ningún dato — todo vuestro sigue donde está y accesible. Simplemente dejo de coger tareas nuevas hasta que resolvamos el pago. En cuanto entre la nómina, vuelvo al 100% al momento. ¿Lo miramos?"}
 ],
 "lifecycle": {
  "prefix": "employment.",
  "initial": "trial",
  "terminal": ["offboarded"],
  "transitions": {
    "employment.hired": "trial",
    "employment.activated": "active",
    "employment.struck": "strike",
    "employment.suspended": "suspended",
    "employment.offboarded": "offboarded",
    "employment.invoice_paid": {"from": "strike", "to": "active"},
    "employment.salary_failed": null
  }
 },
 "soul": {"tier": "boss", "label": "TU RELACIÓN LABORAL (estado real de tu nómina, folded de eventos)"}}
$chase_salary$::jsonb, true, 'L1')
  on conflict (scope, name) do nothing;
end
$seed_duties$;

-- ── DUAL-WRITE state (BUILD-PLAN 1.1): legacy log → spine watermark + divergence counters ───
create table if not exists spine_dualwrite_state (
  source          text primary key,                       -- 'whatsapp_messages' | 'order_events' | 'events'
  last_legacy_id  text not null default '0',
  divergences     bigint not null default 0,
  updated_at      timestamptz not null default now()
);

-- ── DEAD-LETTER (SPEC §4.5 / §12.4): malformed/unsigned/unresolvable → here, never a silent drop ─
create table if not exists dead_letter (
  id            bigserial primary key,
  source        text not null,
  reason        text not null,                            -- bad_signature|unresolvable_entity|malformed|duplicate
  raw           jsonb not null default '{}'::jsonb,
  problem       text,
  alerted       boolean not null default false,
  created_at    timestamptz not null default now()
);

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- ARM-TOGETHER INVARIANT (SPEC §3.1) enforced at the DB: an armed absence-timer REQUIRES its
-- matching absence-contingency on the tab. A trigger keeps a stale/typo branch_id from silently
-- eroding the one-Haiku-per-message envelope (a timer.fired landing OFF-SCRIPT wakes full reasoning).
-- ═══════════════════════════════════════════════════════════════════════════════════════════
create or replace function assert_arm_together() returns trigger as $$
begin
  if NEW.state = 'armed' then
    if not exists (
      select 1 from open_loop l, jsonb_array_elements(l.contingencies) c
      where l.id = NEW.loop_id and c->>'branch_id' = NEW.branch_id
    ) then
      raise exception 'arm-together invariant (SPEC §3.1): timer.branch_id=% has no matching contingency on loop %',
        NEW.branch_id, NEW.loop_id;
    end if;
  end if;
  return NEW;
end;
$$ language plpgsql;

drop trigger if exists trg_arm_together on timer;
create trigger trg_arm_together before insert or update on timer
  for each row execute function assert_arm_together();

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- OPS ORGAN (WP-0.5) — global (non-scope) tables: the alert log + the unit heartbeat.
--   alert     — a durable record of every stop-class surfacing + ops event (surface_to_peter writes it).
--   heartbeat — each background unit (worker, sweep) stamps liveness so /healthz catches a silent death
--               (the agentmemory-died-6-days failure mode). Not scope-keyed → no scope-RLS.
-- ═══════════════════════════════════════════════════════════════════════════════════════════
create table if not exists alert (
  id           bigserial primary key,
  created_at   timestamptz not null default now(),
  kind         text not null,              -- stop-class 'A'..'F' | 'heartbeat' | 'build' | ...
  detail       text not null,
  ref_scope    text,                       -- optional scope breadcrumb (text, NOT an RLS-participating ltree)
  surfaced     boolean not null default false,
  surfaced_via text,                        -- 'whatsapp' | 'email' | 'voice' | 'none'
  resolved_at  timestamptz
);

-- ── Control Room sessions + audit (WP-9 cr-auth) ──────────────────────────────────────────────────
-- OWNER-POOL global ops tables (like alert): NO ltree scope, NO FORCE-RLS. Accessed ONLY via the owner
-- pool (db.get_pool()); rocoto_rt/rocoto_ctrl never touch them, so no schema_rls grants are needed.
-- The cookie carries `session_id.secret`: session_id is a STABLE PK (rotation can't race the key); only
-- sha256(secret) is stored (a leaked backup can't be replayed). prev_secret_hash/_until give a rotation
-- a short grace so a concurrent request never drops a live session.
create table if not exists cr_session (
  session_id        text primary key,               -- stable opaque id (NEVER changes)
  secret_hash       text not null,                   -- sha256(current secret); rotates
  prev_secret_hash  text,                            -- the just-rotated-out secret's hash (grace window)
  prev_secret_until timestamptz,
  actor_email  text not null,
  role         text not null,                        -- owner | admin (brain.auth, OAuth-elevation by email)
  ip           text,
  ua           text,
  csrf_token   text not null,                        -- double-submit token; matched to X-CSRF-Token (stable)
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,                 -- absolute lifetime (12h)
  last_seen_at timestamptz not null default now(),   -- idle + rotation clock
  rotated_at   timestamptz,
  revoked_at   timestamptz                           -- logout / forced kill
);
create index if not exists cr_session_actor on cr_session(actor_email) where revoked_at is null;

-- Append-only audit of every mutating/financial/data CR action (actor · ip · what · when · result).
-- detail is NON-PII (scope ids / counts / amounts — never an erased email/phone).
create table if not exists cr_audit (
  id           bigserial primary key,
  ts           timestamptz not null default now(),
  actor_email  text not null,
  ip           text,
  action       text not null,                        -- login | logout | refund | data.export | data.erase | ...
  target_scope text,                                 -- optional tenant breadcrumb (text, not an RLS ltree)
  target_id    text,
  detail       jsonb not null default '{}'::jsonb,
  result       text not null default 'ok'
);
create index if not exists cr_audit_ts     on cr_audit(ts desc);
create index if not exists cr_audit_action on cr_audit(action, ts desc);

-- ── Supplier web-app sessions + audit (Wave-3 sup-auth) ───────────────────────────────────────────
-- The SECOND session world (design §4: two session worlds, one IdP). Same mechanics as cr_session —
-- stable session_id PK, rotating secret (only sha256 stored), prev-secret grace, CSRF double-submit —
-- but TENANT-SCOPED claims: tenant_slug is minted server-side at login (operators/team_members
-- membership) and every /api/sup/* read/write filters by it. Its OWN table + cookie + caps: no shared
-- secret material with the CR world; a supplier session can never resolve on a CR route or vice versa.
-- OWNER-POOL global ops table (like cr_session): no ltree scope, no FORCE-RLS; db.get_pool() only.
create table if not exists sup_session (
  session_id        text primary key,               -- stable opaque id (NEVER changes)
  secret_hash       text not null,                   -- sha256(current secret); rotates
  prev_secret_hash  text,                            -- the just-rotated-out secret's hash (grace window)
  prev_secret_until timestamptz,
  actor_email  text not null,
  tenant_slug  text not null,                        -- THE claim: the one tenant this session can see
  role         text not null,                        -- owner | founder | staff (team_members/operators)
  ip           text,
  ua           text,
  csrf_token   text not null,                        -- double-submit; matched to X-Sup-CSRF (stable)
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,                 -- absolute lifetime
  last_seen_at timestamptz not null default now(),   -- idle + rotation clock
  rotated_at   timestamptz,
  revoked_at   timestamptz                           -- logout / forced kill
);
create index if not exists sup_session_actor  on sup_session(actor_email) where revoked_at is null;
create index if not exists sup_session_tenant on sup_session(tenant_slug) where revoked_at is null;

-- Append-only audit of every mutating supplier action (the CR pattern: money/state actions ride the
-- session + CSRF + caps and land here — approve, line edit, send-as-Rocoto, dispatch-now, settings,
-- the e-commerce toggle). detail is NON-PII (ids / counts / amounts).
create table if not exists sup_audit (
  id           bigserial primary key,
  ts           timestamptz not null default now(),
  actor_email  text not null,
  tenant_slug  text not null,
  ip           text,
  action       text not null,                        -- login | order.approve | chat.send | settings.ecommerce | ...
  target_id    text,
  detail       jsonb not null default '{}'::jsonb,
  result       text not null default 'ok'
);
create index if not exists sup_audit_ts     on sup_audit(ts desc);
create index if not exists sup_audit_tenant on sup_audit(tenant_slug, ts desc);
-- APPEND-ONLY at the DB level (same trigger discipline as cr_audit — binds even for the owner role).
create or replace function sup_audit_immutable() returns trigger language plpgsql as $sua$
begin
  raise exception 'sup_audit is append-only (% blocked)', tg_op;
end $sua$;
drop trigger if exists sup_audit_no_mutate on sup_audit;
create trigger sup_audit_no_mutate before update or delete on sup_audit
  for each row execute function sup_audit_immutable();
-- APPEND-ONLY at the DB level: a trigger fires for EVERY role incl. the superuser owner, so it binds
-- where GRANT/REVOKE can't (superuser bypasses grants). No UPDATE, no DELETE — ever.
create or replace function cr_audit_immutable() returns trigger language plpgsql as $cra$
begin
  raise exception 'cr_audit is append-only (% blocked)', tg_op;
end $cra$;
drop trigger if exists cr_audit_no_mutate on cr_audit;
create trigger cr_audit_no_mutate before update or delete on cr_audit
  for each row execute function cr_audit_immutable();
create index if not exists alert_open_idx on alert(created_at desc) where resolved_at is null;

create table if not exists heartbeat (
  unit   text primary key,                 -- 'worker' | 'sweep' | ...
  ts     timestamptz not null default now(),
  detail jsonb not null default '{}'::jsonb
);

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- WP-1 THE WAKE SPINE — the single load-bearing missing nerve. `escalate.tab` / approval /
-- takeover_end are PRODUCED today (worker off-script, cascade matched-money, review approve) but had
-- NO consumer, so surprises + approvals stalled silently. A wake row is inserted in the SAME tx as its
-- triggering event; the worker's 2nd scan fires a fresh R1 wake-turn on it (SILENCE allowed). The
-- partial unique index collapses N producers → EXACTLY one fire; a live chat turn absorbs pending wakes.
-- ═══════════════════════════════════════════════════════════════════════════════════════════
create table if not exists wake (
  id         bigserial primary key,
  scope      ltree not null,
  reason     text not null,                  -- 'escalate' | 'approval' | 'takeover_end'
  detail     jsonb not null default '{}'::jsonb,
  dedupe_key text not null default '',        -- (scope,reason,dedupe_key) collapses producers → ONE
  state      text not null default 'armed',   -- armed → firing → fired | absorbed
  caused_by  bigint,
  created_at timestamptz not null default now(),
  fired_at   timestamptz
);
create unique index if not exists ux_wake_dedupe on wake(scope, reason, dedupe_key) where state in ('armed','firing');
create index if not exists ix_wake_armed on wake(scope) where state='armed';

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- WP-3 THE MEMORY TURN — recall on the live path + a byte-replay snapshot.
--   assembly_snapshot — the rendered fold, verbatim, keyed by the turn's inbound event. `why <msg>`
--     replays it byte-for-byte; the CI invariant asserts assemble(assembled_at,qv) reproduces it.
--   fact.told_by_this_contact / source_channel (R9) — additive columns for R5 disclosure (WP-8b).
-- ═══════════════════════════════════════════════════════════════════════════════════════════
create table if not exists assembly_snapshot (
  scope        ltree not null,
  event_id     bigint not null,
  rendered     text not null,
  why_hash     text not null,
  assembled_at timestamptz not null,
  qv           vector(384),
  created_at   timestamptz not null default now(),
  primary key (scope, event_id)
);

alter table fact add column if not exists told_by_this_contact boolean;
alter table fact add column if not exists source_channel text;

-- ── WP-7b THE REPERTOIRE: the situation library AS DATA (SPEC §16.6). Each is a manifest_duty ROW
-- bound by the generic duties.py binder — zero Python per situation. Seeded at the creator scope
-- ('peter') so load_duties includes them as L0 defaults for every customer (like chase_salary).
-- Per-tenant customisation is a live-row edit / WP-10 self-config, not this file.
do $seed_repertoire$
begin
  perform set_config('rocoto.scope', 'peter', true);

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'confirm_delivery', null, $d$
{"opens_on": {"event": "order.placed", "key": "order:{order_id}"},
 "resolves_on": {"event": "delivery.confirmed", "branch_id": "delivered"},
 "tab": {"kind": "expect", "answer_shape": "yesno", "money": false,
         "text": "Confirmar entrega del pedido {order_id}"},
 "rungs": [
  {"branch_id": "r1_check", "after": "1d", "gated": false,
   "trigger": "no delivery confirmation +1d — check in",
   "sentence": "¡Hola! ¿Te llegó todo bien del pedido? Si está correcto, perfecto; y si faltó algo o vino mal, me dices y lo arreglamos enseguida."}],
 "lifecycle": {"prefix": "delivery.", "initial": "pending", "terminal": ["delivered"],
   "transitions": {"delivery.confirmed": "delivered"}},
 "soul": {"tier": "customer", "label": "ENTREGA PENDIENTE DE CONFIRMAR"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'reorder_reminder', null, $d$
{"opens_on": {"event": "reorder.due", "key": "reorder:{customer}"},
 "resolves_on": {"event": "order.placed", "branch_id": "reordered"},
 "tab": {"kind": "pending", "answer_shape": "yesno", "money": false,
         "text": "Recordatorio de reposición para {customer}"},
 "rungs": [
  {"branch_id": "r1_nudge", "after": "0s", "gated": false,
   "trigger": "reorder window reached — the silent-surprise proactive nudge",
   "sentence": "¡Hola! Por si te viene bien: calculo que se te estará terminando lo de siempre. ¿Te preparo el pedido habitual o quieres cambiar algo esta vez?"}],
 "lifecycle": {"prefix": "reorder.", "initial": "due", "terminal": ["reordered"],
   "transitions": {"order.placed": "reordered"}},
 "soul": {"tier": "customer", "label": "REPOSICIÓN PREVISTA (sorpresa silenciosa)"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'chase_quote', null, $d$
{"opens_on": {"event": "quote.sent", "key": "quote:{quote_id}"},
 "resolves_on": {"event": "quote.accepted", "branch_id": "accepted"},
 "tab": {"kind": "expect", "answer_shape": "yesno", "money": false,
         "text": "Presupuesto {quote_id} enviado — esperando respuesta"},
 "rungs": [
  {"branch_id": "r1_followup", "after": "2d", "gated": false,
   "trigger": "quote unanswered +2d — gentle follow-up",
   "sentence": "¡Hola! ¿Pudiste echar un ojo al presupuesto que te pasé? Cualquier duda o ajuste que necesites, me dices y lo vemos juntos."}],
 "lifecycle": {"prefix": "quote.", "initial": "sent", "terminal": ["accepted", "declined"],
   "transitions": {"quote.accepted": "accepted", "quote.declined": "declined"}},
 "soul": {"tier": "customer", "label": "PRESUPUESTO EN EL AIRE"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  -- TRACK 3 · REFLEX ROW A — 'report_call' (the post-call BOSS DEBRIEF). A call ended → the generic
  -- binder OPENS this duty on `call.ended`; its single rung fires ONCE (no resolves_on → no rearm) on
  -- the `boss` channel, so the worker routes the debrief to Peter via ops.surface_to_peter (never to a
  -- customer, never paced). Slots {name}/{summary}/{next_step} fill from the enriched call.ended payload
  -- (presence_call.call_ended + postcall.summarize). Situations are DATA — zero situation code.
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'report_call', null, $d$
{"opens_on": {"event": "call.ended", "key": "callreport:{call_id}"},
 "tab": {"kind": "expect", "answer_shape": "free", "money": false,
         "text": "Parte de la llamada con {name}"},
 "rungs": [
  {"branch_id": "r1_debrief", "after": "0s", "gated": false, "channel": "boss",
   "trigger": "call.ended — one-shot boss debrief",
   "sentence": "He tenido una llamada con {name}. Esto ha pasado: {summary}. Esto haré: {next_step}."}],
 "lifecycle": {"prefix": "callreport.", "initial": "surfaced", "terminal": ["surfaced"],
   "transitions": {}},
 "soul": {"tier": "boss", "label": "ÚLTIMO PARTE DE LLAMADA"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  -- TRACK 3 · REFLEX ROW B — 'follow_up' (chase a promised outcome after a call). Perception detects a
  -- commitment on the call and emits `call.promise` (postcall.summarize → runtime.on_duty_sense); the
  -- binder OPENS this duty, chasing the party over the default WhatsApp rail (reach/pacing apply —
  -- no `channel`, correct for a customer nudge). A `followup.done` carrying the same promise_key (or the
  -- mind's resolve_tab tool) RESOLVES it. This is the DETERMINISTIC replacement for the LLM electing
  -- open_tab in chat — the actual '0 tabs' fix. Structurally identical to chase_quote.
  -- CADENCE (a row edit, §16.6): a call-promise is often an IN-FLIGHT order — the cadet follows up the
  -- SAME afternoon (4h), then next day, then +2d; the rearm chain derives from the rungs' own order.
  -- The sentences are the prospect-stage cadet's voice: value-forward, referencing the call, es-ES.
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'follow_up', null, $d$
{"opens_on": {"event": "call.promise", "key": "followup:{promise_key}"},
 "resolves_on": {"event": "followup.done", "branch_id": "kept"},
 "tab": {"kind": "pending", "answer_shape": "yesno", "money": false,
         "text": "Seguimiento prometido a {name}: {promise}"},
 "rungs": [
  {"branch_id": "r1_chase", "after": "4h", "gated": false,
   "trigger": "promised outcome unmet +4h — same-day follow-up on the call promise",
   "sentence": "¡Hola! Te escribo por lo que hablamos en la llamada de antes: quedé en {promise}. Ya lo tengo entre manos — ¿lo dejamos confirmado, o quieres ajustar algo antes?"},
  {"branch_id": "r2_chase", "after": "1d", "gated": false,
   "trigger": "promised outcome still unmet +1d — next-day follow-up",
   "sentence": "¡Buenas! Sigo pendiente de lo que quedamos en la llamada — {promise}. En cuanto me digas lo cierro y te lo quito de la cabeza."},
  {"branch_id": "r3_chase", "after": "2d", "gated": false,
   "trigger": "promised outcome still unmet +3d — last gentle chase",
   "sentence": "No quiero ser pesado — solo que no se nos pierda lo de la llamada: {promise}. Con un «sí» me basta y me encargo yo del resto."}],
 "lifecycle": {"prefix": "followup.", "initial": "open", "terminal": ["kept", "dropped"],
   "transitions": {"followup.done": "kept", "followup.dropped": "dropped"}},
 "soul": {"tier": "customer", "label": "SEGUIMIENTO PENDIENTE TRAS LLAMADA"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  -- ── BLOCK D · THE REVIEW RULE, SOURCE 3 — 'question_timeout' (SPEC §5.8/§5.8b/§5.7). ─────────
  -- Peter, 2026-07-26 (Mercamadrid): THE CUSTOMER resolves ambiguity, not the distributor. Rocoto
  -- asks the customer with 2-4 real catalogue options (`ask_options`); the customer picks; the
  -- comercial never sees it. ONLY silence reaches him — and only after the window.
  --
  -- THE WINDOW IS ONE HOUR (Peter, LOCKED 2026-07-26 — SPEC §5.8b). Orders land 05:00–07:00 against a
  -- prep cutoff, so TWO hours can consume the entire window in which the decision still matters: the
  -- question would still be "open" when the sheet has already gone out. One hour keeps the queue
  -- near-empty without ever being the reason a van leaves late. It is DATA — this `after`, on this
  -- row — not a constant in Python, which is exactly why correcting it is an edit to one string here.
  --
  -- The whole timeout is THIS ROW. `review.on_question_asked` appends `question.asked` when a
  -- question actually WENT OUT; the generic binder opens the tab and ARMS ITS TIMER (never a poll).
  -- Any inbound from that party appends `question.answered` (runtime.run_turn, before the fold), which
  -- resolves the tab and CANCELS the armed rung in the same commit — so an answered question cannot
  -- reach the queue even in principle. When the clock does fire, the rung's `channel: 'review'` sends
  -- nothing: it parks the matter through review.hold_for_review with source `timeout`.
  --   · `when: {tier: [customer, prospect]}` — a question to the BOSS is a conversation, not a queue
  --     item. Who this applies to is DATA on the row, never an if in the runtime.
  --   · `by: "kind"` — the singleton close: the party's open `question` tab, whatever key it opened
  --     under. A key template can never leave a tab open against an answer that plainly arrived.
  --   · `not_after: {cutoff_iso}` — the prep cutoff ALSO forces it: a 1h window that lands after the
  --     order sheet has gone out is not a window. Empty payload → no clamp, the plain 1h applies.
  --   · PER-DISTRIBUTOR WINDOW: not a knob, a NEARER ROW. `insert into manifest_duty(scope,name,…)`
  --     with name 'question_timeout' at `t.<slug>` and a different `after`; load_duties walks the
  --     ladder and the nearer row wins (BLOCK C). No column, no config table, no code. The UPDATE
  --     below therefore touches the creator-scope row ONLY — a distributor's own window is a
  --     different row and survives untouched.
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'question_timeout', null, $d$
{"opens_on": {"event": "question.asked", "key": "question:{ask_key}",
              "when": {"tier": ["customer", "prospect"]}},
 "resolves_on": {"event": "question.answered", "branch_id": "answered", "by": "kind"},
 "tab": {"kind": "question", "answer_shape": "free", "money": false,
         "text": "Le he preguntado al cliente: «{question}» — sin respuesta todavía",
         "brief": "Le he preguntado «{question}» y estoy esperando. Hasta que conteste no hay nada apuntado.",
         "brief_label": "pregunta abierta"},
 "rungs": [
  {"branch_id": "r1_timeout", "after": "1h", "gated": false,
   "channel": "review", "review_source": "timeout", "not_after": "{cutoff_iso}",
   "trigger": "the customer never answered the question — the window closed, it is the human's now",
   "sentence": "El cliente no ha contestado a «{question}». Lo resuelves tú."}],
 "lifecycle": {"prefix": "question.", "initial": "asked", "terminal": ["answered"],
   "transitions": {"question.answered": "answered"}},
 "soul": {"tier": "customer", "label": "PREGUNTA SIN RESPONDER"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  -- …AND THE SAME HOUR ON A BOX THAT ALREADY HAS THE ROW. The insert above is `on conflict do nothing`
  -- — correct for a seed (a re-run must never clobber a live row) and, on its own, WRONG for a
  -- CORRECTED window: prod already carries the 2h row, so re-running the schema would leave it at two
  -- hours forever and the window would only ever be right on a fresh database. So the correction is
  -- written as its own migration, right here beside the seed it corrects, and it is:
  --   · IDEMPOTENT — a second run matches nothing, because the guard reads the value it would write;
  --   · GUARDED — it rewrites the first rung ONLY while that rung still says the old '2h'. A window
  --     someone has deliberately moved since is never silently pulled back to ours;
  --   · SCOPED TO THE CREATOR ROW — `scope='peter'`. A distributor's own window is a NEARER ROW at
  --     `t.<slug>` (never a column, never a branch — see above), so per-distributor overrides keep
  --     working and are untouched by this.
  update manifest_duty
     set gate_policy = jsonb_set(gate_policy, '{rungs,0,after}', '"1h"'::jsonb, false)
   where scope = 'peter'::ltree
     and name = 'question_timeout'
     and gate_policy #>> '{rungs,0,after}' = '2h';
end $seed_repertoire$;

-- ── M1M2 WP-0 · THE REVIEW-NUDGE (CEO finding 1: the core promise dies silently without it). ────
-- SITUATIONS ARE DATA (§16.6): the situation is THIS manifest_duty ROW — bound by the generic binder,
-- zero situation code — plus exactly ONE manifest-registered perception reflex (the manifest_reflex
-- row below; its wired code is the dispatch organ's `_review_nudge_scan` in app.py, the same
-- rows+wired-code shape as the order-brain). The reflex senses unreviewed orders APPROACHING the
-- daily cutoff and emits `orders.review_pending` onto the boss scope with a per-cutoff dedupe_key,
-- so the whole chain (sense → tab → rung → gate → WhatsApp) fires AT MOST ONCE per cutoff; when the
-- review queue empties it emits `orders.review_clear` and the tab resolves.
--   · the sentence is COUNT-FREE (slots resolve at open time — a count would go stale between open
--     and fire; the daily email carries the exact held count instead, eng F9).
--   · the rung carries `pacing_deadline` = {cutoff_iso} so human-hours pacing can never hold the
--     nudge past its own cutoff (worker._pacing_hold_ok — a 6am nudge for a 6:45 cutoff still fires).
--   · no `channel` → the default WhatsApp rail; the sentence crosses gate.check_outbound like
--     everything spoken. No money shape → it auto-sends; the row can exempt nothing from the gate.
do $seed_wp0$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'review_nudge', null, $d$
{"opens_on":    {"event": "orders.review_pending", "key": "revnudge:{cutoff_key}"},
 "resolves_on": {"event": "orders.review_clear", "branch_id": "reviewed"},
 "tab": {"kind": "pending", "answer_shape": "yesno", "money": false,
         "text": "Pedidos sin revisar antes del cierre de las {cutoff}"},
 "rungs": [
  {"branch_id": "r1_nudge", "after": "0s", "gated": false, "pacing_deadline": "{cutoff_iso}",
   "trigger": "unreviewed orders approaching the daily cutoff — one pre-cutoff nudge to the boss",
   "sentence": "Jefe, tienes pedidos pendientes de revisar antes del cierre de las {cutoff}. Los que queden sin revisar se retienen para el siguiente envío — échales un ojo en la app en cuanto tengas un momento."}],
 "lifecycle": {"prefix": "orders.review", "initial": "clear", "terminal": [],
   "transitions": {"orders.review_pending": "pending", "orders.review_clear": "clear"}},
 "soul": {"tier": "boss", "label": "PEDIDOS SIN REVISAR"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;
  -- the reflex REGISTRATION (§3.2: a reflex row = {scope_tier, organ, config}; the emitter refuses to
  -- run without an enabled row). config knobs: lead_minutes (window before cutoff), boss_phone
  -- (optional explicit pin; unset → newest tier='boss' scope_node → the creator line).
  insert into manifest_reflex (scope_tier, organ, config, enabled, created_by)
  select 'boss', 'review_nudge', '{"lead_minutes": 45}'::jsonb, true, 'L1'
  where not exists (select 1 from manifest_reflex where scope_tier = 'boss' and organ = 'review_nudge');
end $seed_wp0$;

-- M1M2 WP-3 (dispatch robustness, eng F11): the DISPATCH-FAILED situation as DATA — a failed daily
-- Excel send must never die as a silent log.warning while approved orders silently stop flowing.
-- Same shape as WP-0: ONE manifest_duty row bound by the generic binder + ONE manifest-registered
-- reflex emitter (wired code = the dispatch organ's failure path, app._dispatch_failure_nudge).
--   · the emitter senses `orders.dispatch_failed` onto the boss scope with a per-DAY dedupe_key
--     (dispfail:{day_key}) — however many cadence retries fail, at most ONE WhatsApp a day;
--   · the next SUCCESSFUL dispatch emits `orders.dispatch_ok` and the tab resolves;
--   · the sentence is COUNT-FREE and names the recovery move (the app's «Enviar ahora»); the exact
--     batch is visible in the app — a count would go stale across retries;
--   · no `channel` → the default WhatsApp rail; gate.check_outbound on the sentence as always; no
--     money shape; no pacing_deadline (an overnight failure may wait for human hours — the orders
--     are HELD, not lost, and the next cadence tick retries regardless).
do $seed_wp3$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'dispatch_failed', null, $d$
{"opens_on":    {"event": "orders.dispatch_failed", "key": "dispfail:{day_key}"},
 "resolves_on": {"event": "orders.dispatch_ok", "branch_id": "recovered"},
 "tab": {"kind": "pending", "answer_shape": "yesno", "money": false,
         "text": "El envío del Excel de pedidos ha fallado"},
 "rungs": [
  {"branch_id": "r1_dispfail", "after": "0s", "gated": false,
   "trigger": "the daily Excel dispatch failed — the approved orders are held, tell the boss once",
   "sentence": "Jefe, no he podido enviar el Excel de pedidos. Los pedidos aprobados quedan guardados y lo vuelvo a intentar solo; si lo necesitas ya, entra en la app y toca «Enviar ahora»."}],
 "lifecycle": {"prefix": "orders.dispatch", "initial": "ok", "terminal": [],
   "transitions": {"orders.dispatch_failed": "failed", "orders.dispatch_ok": "ok"}},
 "soul": {"tier": "boss", "label": "ENVÍO EXCEL FALLIDO"}}
$d$::jsonb, true, 'L1')
  on conflict do nothing;
  insert into manifest_reflex (scope_tier, organ, config, enabled, created_by)
  select 'boss', 'dispatch_failed', '{}'::jsonb, true, 'L1'
  where not exists (select 1 from manifest_reflex where scope_tier = 'boss' and organ = 'dispatch_failed');
end $seed_wp3$;

-- WP-9 P0: whatsapp_messages is the OUTBOUND PROJECTION of the spine — legacy (full) on prod, a MINIMAL
-- shape here so the rehearsal clone has it too (create-if-not-exists no-ops on prod's real table). The
-- event_id column links each projected row to its msg.sent event (the reconciler asserts zero divergence).
create table if not exists whatsapp_messages (
  id            text primary key,
  phone_number  text,
  role          text,
  message_text  text,
  message_type  text default 'text',
  wa_message_id text,
  event_id      bigint,
  created_at    timestamptz not null default now()
);
alter table whatsapp_messages add column if not exists event_id bigint;
create index if not exists ix_wamsg_event on whatsapp_messages(event_id) where event_id is not null;
-- WP-9 cr-read: the chat surface reads the FULL legacy whatsapp_messages shape (media + reply-quote +
-- voice-note transcript). These exist on prod; declared here (matching prod types) so the rehearsal
-- clone has them too and the chat vital + schema-drift gate stay green.
alter table whatsapp_messages add column if not exists media_urls        jsonb not null default '[]'::jsonb;
alter table whatsapp_messages add column if not exists media_types       jsonb not null default '[]'::jsonb;
alter table whatsapp_messages add column if not exists reply_to_wa_id    text;
alter table whatsapp_messages add column if not exists reply_to_text     text;
alter table whatsapp_messages add column if not exists transcript        text;
alter table whatsapp_messages add column if not exists transcript_status text default 'none';
alter table whatsapp_messages add column if not exists transcript_meta   jsonb not null default '{}'::jsonb;
alter table whatsapp_messages add column if not exists seeded            boolean not null default false;

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- M1M2 WP-4 · BILLING — the €500 credit ledger + the 0,3% GMV meter (SPEC §16.4; commerce organ).
-- OWNER-POOL global money tables (like alert/cr_audit): tenant-keyed TEXT, NO ltree scope, NO
-- FORCE-RLS — only ever touched via db.get_pool()/control_pool by the billing organ; rocoto_rt
-- never reads them (the runtime's in-chat awareness rides keyed COMMERCIAL facts instead).
--
--   credit_ledger — APPEND-ONLY euro-cent ledger. Balance = SUM(delta_cents). The €500 alta lands
--     as +50000/'setup_fee_credit' (setupfee._record_payment); each monthly commission debits
--     -x/'commission_YYYY-MM'. unique(tenant_id, reason) IS the idempotency: a crash-rerun of a
--     debit (or a replayed setup webhook) conflicts and adopts the existing row — no double-debit,
--     structurally. A DB trigger (superuser-binding, like cr_audit) blocks UPDATE/DELETE forever.
--     ref_event = the billing.meter_run spine event this row rides on (the why-trace, dx F4).
--
--   billing_run — the meter's DURABLE state machine (eng F2/F3): one row per (tenant, period),
--     period = a Europe/Madrid calendar month 'YYYY-MM', billed only once FULLY ELAPSED (eng F12).
--     state computed → debited → invoiced, EACH transition its own tx; a box down on the 1st just
--     catches up on the next scan (worker.run_billing_once — a scan over these rows, never a
--     HH:MM-equality timer). stripe_invoice_id + idempotency_key comm-{tenant}-{period} make the
--     remainder invoice exactly-once across crash-reruns.
-- ═══════════════════════════════════════════════════════════════════════════════════════════
create table if not exists credit_ledger (
  id          bigserial primary key,
  tenant_id   text not null,
  delta_cents bigint not null,                      -- +credit / -debit, euro cents
  reason      text not null,                        -- 'setup_fee_credit' | 'commission_YYYY-MM' | ...
  ref_event   bigint,                               -- billing.meter_run event id (why-trace; no hard FK)
  detail      jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  unique (tenant_id, reason)
);
create index if not exists ix_credit_ledger_tenant on credit_ledger (tenant_id);
-- APPEND-ONLY at the DB level (same discipline as cr_audit): a trigger fires for EVERY role incl.
-- the superuser owner. A ledger row is history — corrections are NEW rows, never edits.
create or replace function credit_ledger_immutable() returns trigger language plpgsql as $clg$
begin
  raise exception 'credit_ledger is append-only (% blocked)', tg_op;
end $clg$;
drop trigger if exists credit_ledger_no_mutate on credit_ledger;
create trigger credit_ledger_no_mutate before update or delete on credit_ledger
  for each row execute function credit_ledger_immutable();

create table if not exists billing_run (
  tenant_id            text not null,
  period               text not null,               -- 'YYYY-MM' (Europe/Madrid calendar month)
  state                text not null default 'computed',  -- computed → debited → invoiced
  gmv_cents            bigint not null default 0,   -- Σ orders.value_snapshot_cents dispatched in period
  commission_cents     bigint not null default 0,   -- round(gmv * 0.003)
  credit_debited_cents bigint not null default 0,
  invoiced_cents       bigint not null default 0,   -- the remainder actually invoiced on the saved card
  unsnapshotted        int not null default 0,      -- dispatched-in-period orders with NO value snapshot (visibility, never silent)
  stripe_invoice_id    text,
  meter_event          bigint,                      -- latest billing.meter_run event id for this run
  error                text,                        -- last invoice-step failure ('no_card'|'not_configured'|'stripe:…')
  last_attempt_at      timestamptz,                 -- invoice-retry pacing (a stuck run retries, never spams)
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  primary key (tenant_id, period)
);

-- ── PRESENCE SUITE (2026-07-25): channel-mechanics capabilities REGISTERED AS MANIFEST ROWS ──
-- SPEC §3.2 discipline: a capability the mind can use is a manifest_tool ROW, not a hidden code path.
--   send_reaction — react with an emoji to a customer message (whatsapp_cloud.send_reaction, cloud
--                   rail only). gate_category='free' BY DESIGN: no money can ride an emoji — but it
--                   is never silent (evented msg.sent kind=reaction + projected into the thread).
--                   THE 200 IS NOT DELIVERY (2026-07-26): Meta accepts a reaction to a >30-day-old /
--                   deleted / non-reactable target and refuses it LATER on the status webhook with
--                   error 131009. So the capability has two more evented outcomes:
--                   ops.reaction_refused (pre-flight, nothing sent) and ops.reaction_failed (async,
--                   projection rolled back so the app never shows a gesture nobody saw).
--   send_media    — outbound image/document/audio on the cloud rail (whatsapp_cloud.send_media via
--                   actions.send_whatsapp_as_rocoto media_url). 'free' as a channel mechanic; the
--                   CAPTION is still text and still passes the outbound money gate at the hand
--                   (agent_reply: a priced caption is refused → park/approve like any quote).
-- Seeded once (where-not-exists) on the creator scope; enable/disable lives on the LIVE row.
do $seed_presence_tools$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_tool (scope, name, gate_category, surface, config, enabled, created_by)
  select 'peter', 'send_reaction', 'free', 'customer_chat',
         '{"rail": "cloud", "kind": "reaction", "evented_as": "msg.sent kind=reaction",
           "preflight": "target must be a known wamid, not a reaction, <30 days old",
           "failure_events": ["ops.reaction_refused", "ops.reaction_failed"]}'::jsonb,
         true, 'L1'
  where not exists (select 1 from manifest_tool where scope = 'peter'::ltree and name = 'send_reaction');
  insert into manifest_tool (scope, name, gate_category, surface, config, enabled, created_by)
  select 'peter', 'send_media', 'free', 'customer_chat',
         '{"rail": "cloud", "kind": "media", "caption_money_gated": true}'::jsonb,
         true, 'L1'
  where not exists (select 1 from manifest_tool where scope = 'peter'::ltree and name = 'send_media');
end
$seed_presence_tools$;

-- ── INTERACTIVE / ONE-TAP (2026-07-26): closed questions the customer ANSWERS WITH A TAP ─────
-- The honest replacement for polls (no Cloud API poll surface exists for us; PIN is built-but-not-
-- enabled, 131000). What IS supported and verified: reply BUTTONS (≤3, title ≤20) and LIST messages
-- (≤10 rows TOTAL across sections). A tap returns type=interactive with {id,title} + context.id =
-- the wamid we asked with — so the answer is bound to its exact question.
--   send_interactive — the RAIL capability (whatsapp_cloud.send_interactive_buttons /
--                      send_interactive_list via actions.send_interactive_as_rocoto). The surface
--                      choice (buttons vs list) belongs to the CODE, never to the mind.
--                      gate_category='needs_approval' is WRONG here and 'free' is honest: asking a
--                      question is speech, and the speech itself is money-gated at the hand — the
--                      BODY and every OPTION TITLE run through gate.check_outbound with the scope's
--                      standing prices, and a priced question PARKS instead of sending.
--   ask_options     — the mind's single hand onto that rail (agent_reply tool). It takes a question
--                      + 2..10 options and nothing else: Meta's UI limits are never the mind's
--                      problem. Evented msg.sent kind=interactive WITH the offered option set, so
--                      the fold/why-trace answers "what did we actually put in front of them?".
do $seed_interactive_tools$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_tool (scope, name, gate_category, surface, config, enabled, created_by)
  select 'peter', 'send_interactive', 'free', 'customer_chat',
         '{"rail": "cloud", "kind": "interactive", "surfaces": ["button", "list"],
           "max_buttons": 3, "max_rows": 10, "button_title_max": 20, "row_title_max": 24,
           "body_money_gated": true, "evented_as": "msg.sent kind=interactive"}'::jsonb,
         true, 'L1'
  where not exists (select 1 from manifest_tool where scope = 'peter'::ltree and name = 'send_interactive');
  insert into manifest_tool (scope, name, gate_category, surface, config, enabled, created_by)
  select 'peter', 'ask_options', 'free', 'customer_chat',
         '{"binds": "send_interactive", "min_options": 2, "max_options": 10,
           "closed_questions_only": true, "answer_path": "inbound interactive -> text ingest (wakes the mind)"}'::jsonb,
         true, 'L1'
  where not exists (select 1 from manifest_tool where scope = 'peter'::ltree and name = 'ask_options');
end
$seed_interactive_tools$;

-- ── PIN: a capability that EXISTS in the API and is NOT enabled for us (2026-07-26) ──────────
-- Empirically established, do not re-derive: the Cloud API VALIDATES the pin shape
-- {"type":"pin","pin":{"type":"pin"|"unpin","message_id":wamid,"expiration_days":1..30}} — it is a
-- known message type, not an unrecognised field — but EXECUTING it answers 131000 on our WABA, while
-- the public docs still list pinning as unsupported. Schema and docs disagree; the honest state is
-- NOT-YET-AVAILABLE.
--   pin_message — REGISTERED AND DISABLED, on purpose. The row exists so the capability is DECLARED
--                 (a hand nobody can find is a hand that gets rebuilt), and enabled=false so no mind
--                 can reach for something that cannot work. Two locks, not one: this row AND
--                 config.ROCOTO_WA_PIN_ENABLED (off by default — with it off the rail refuses before
--                 building a payload). Turning the config flag ON only arms brain/pin_watch's weekly
--                 probe; the day that probe succeeds it sets system_flags.wa_pin_available and tells
--                 Peter, and ENABLING THIS ROW stays his decision alone.
do $seed_pin_tool$
begin
  perform set_config('rocoto.scope', 'peter', true);
  insert into manifest_tool (scope, name, gate_category, surface, config, enabled, created_by)
  select 'peter', 'pin_message', 'free', 'customer_chat',
         '{"rail": "cloud", "kind": "pin", "state": "not_enabled_by_meta", "meta_error": "131000",
           "expiration_days_range": [1, 30], "code": "whatsapp_cloud.pin_message/unpin_message",
           "config_flag": "ROCOTO_WA_PIN_ENABLED", "watcher": "brain/pin_watch.py (weekly probe)",
           "availability_flag": "system_flags.wa_pin_available"}'::jsonb,
         false, 'L1'
  where not exists (select 1 from manifest_tool where scope = 'peter'::ltree and name = 'pin_message');
end
$seed_pin_tool$;

-- ── L2 STANDING INSTRUCTIONS (SPEC §4.1 ledger row 2 · §16.2 level L2; BLOCK B1) ─────────────
-- "standing instructions (L2 rows) | 300 tok | ORDER BY binding DESC, priority, id | lowest-priority
--  row cut first; `binding` rows never cut."  The boss's permanent rules about a party ("con Manolo,
-- mano izquierda con los retrasos") are a FIRST-CLASS STORE, not a fact-shelf squatter: facts describe
-- what is TRUE about a party, instructions describe how the employee must BEHAVE — different organs,
-- different lifecycles (an instruction is revoked, never invalidated by a newer observation).
-- Scope-keyed like everything else, so a rule about one customer can never leak into another's fold.
create table if not exists standing_instruction (
  id          bigserial primary key,
  scope       ltree not null,
  text        text not null,
  binding     boolean not null default false,   -- binding rows are NEVER cut by the slot budget
  priority    int not null default 100,          -- lower = more important (the ledger's sort key)
  created_by  text not null default 'L2',        -- L1 (kernel) | L2 (the boss) | L3 (per-turn)
  created_at  timestamptz not null default now(),
  revoked_at  timestamptz,
  lineage     text not null default 'live'
);
create index if not exists ix_standing_scope on standing_instruction (scope, binding desc, priority, id)
  where revoked_at is null;

-- ══ BLOCK B2 · THE LEAD-PURSUIT TAB (SPEC §4.1.1 "the lead-pursuit tab", §6, §16.6) ═══════════
-- The audit's gap-1: an active relationship carried NO standing tab, and the deterministic lifecycle
-- emitted nothing a duty could open on — stage awareness lived in SOUL text alone. Three pieces close
-- it, and only the first two are code-shaped:
--   (1) the SIGNATURE column below — `lifecycle.observe`'s compare-and-set, so a transition is emitted
--       exactly once and a normal turn emits nothing at all;
--   (2) the SINGLETON INDEX below — exactly one open `pursuit` tab per relationship, enforced by the
--       DATABASE. The binder cannot leak one by being clever, and an attempt to open a second one
--       PARKS needs_human loudly (duties._open) instead of dying in a log line;
--   (3) the PURSUIT ROWS below — the situation itself, as DATA. There is no pursuit.py.
alter table scope_state add column if not exists lifecycle_sig text;   -- the §6 derived-state signature
-- ART. 50(1) ON THE TEXT RAIL: when this conversation was told, once, that it is talking to an AI.
-- Null means «never told», which is the only state that puts the notice on the next message Rocoto
-- sends. Stamped AFTER a successful send, so a failed send re-arms rather than silently swallowing
-- the one notice that number was ever going to get.
alter table scope_state add column if not exists ai_notice_at timestamptz;
create unique index if not exists ux_loop_one_pursuit on open_loop (scope)
  where kind = 'pursuit' and status = 'open';

-- ═══ BLOCK B6 — MODO SOMBRA (SPEC §5.5): collapse the mouth half-toggles into ONE mode field ═══
-- The create-table block above ships the new shape to a FRESH clone; on PROD that block is a no-op
-- (the table exists), so the migration has to be explicit — that is the schema_drift law. It carries
-- the old bits FORWARD (a scope that was muted / under takeover stays silent, as 'sombra') and then
-- REMOVES them, so a stale reader fails LOUD (undefined column) instead of quietly reading a
-- half-truth that no longer knows about the app-wide default.
alter table scope_state add column if not exists mode text;
do $b6_sombra$
begin
  if not exists (select 1 from pg_constraint where conname = 'scope_state_mode_chk') then
    alter table scope_state add constraint scope_state_mode_chk
      check (mode is null or mode in ('auto','sombra'));
  end if;
  if exists (select 1 from information_schema.columns
             where table_schema='public' and table_name='scope_state' and column_name='muted') then
    execute 'update scope_state set mode=''sombra'' where mode is null and muted';
    execute 'alter table scope_state drop column muted';
  end if;
  if exists (select 1 from information_schema.columns
             where table_schema='public' and table_name='scope_state' and column_name='takeover_active') then
    execute 'update scope_state set mode=''sombra'' where mode is null and takeover_active';
    execute 'alter table scope_state drop column takeover_active';
  end if;
end $b6_sombra$;

-- The pursuit rows: ONE per lifecycle stage that is a RELATIONSHIP to pursue. Each opens on the
-- `lifecycle.stage_changed` transition that ENTERS its stage (`when: {stage: …}`) and resolves on the
-- transition that LEAVES it (`when: {from_stage: …}`, `by: "kind"` — the singleton close). A move from
-- one stage to another therefore resolves the old row's tab and opens the new row's, in that order,
-- child-linked by the binder — the chain of a relationship's pursuits, inspectable end to end.
--   · `founder` (Peter himself) and `fresh` (a scope with no world events yet) have NO row on purpose:
--     the creator is not a lead, and there is nothing to pursue before the relationship exists. The
--     first real transition into `prospect` opens the cadete's pursuit.
--   · the tab TEXT states the stage's objective, spliced from the transition's `{objective}` slot (the
--     §6 BOX_JOB data row for the next unticked box) — the ladder is never duplicated in config.
--   · the tab BRIEF is the open-time standing assessment; the binder writes it as a reasoning-lane
--     event bound to the tab, so it renders on the tab's own line in the fold (§4.1.1 ④, BLOCK B1).
--   · the rungs are the stage's CHASE CADENCE. They are ordinary outbound: no `channel` → the default
--     WhatsApp rail, human-hours pacing applies, and every sentence crosses gate.check_outbound. None
--     of them names a price or an amount — money conversations belong to R1 and to Peter, never to a
--     timer (a money-shaped sentence would park anyway; these simply have no business being one).
do $seed_pursuit$
begin
  perform set_config('rocoto.scope', 'peter', true);

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'pursuit_prospect', null, $d$
{"opens_on":    {"event": "lifecycle.stage_changed", "when": {"stage": "prospect"},
                 "key": "pursuit:{stage}:{next_box}"},
 "resolves_on": {"event": "lifecycle.stage_changed", "when": {"from_stage": "prospect"},
                 "by": "kind", "branch_id": "stage_moved"},
 "tab": {"kind": "pursuit", "answer_shape": "free", "money": false,
         "text": "SEGUIMIENTO DE LA RELACIÓN (fase cadete): ganarte el sitio en esta casa. Objetivo de fondo: {objective}",
         "brief": "{from_label}; ahora está en fase «{stage}». El objetivo de fondo es {objective} Es el rumbo de la relación, no una orden para este turno.{parent}",
         "brief_label": "seguimiento de la relación"},
 "rungs": [
  {"branch_id": "r1_retomar", "after": "3d", "gated": false,
   "trigger": "prospect quiet +3d — pick the thread back up by DOING, not by pitching",
   "sentence": "¡Hola! Soy Rocoto. Me quedé con ganas de seguir con lo tuyo: si me sueltas un pedido cualquiera de los que te entran, te enseño en un minuto cómo te lo dejo cuadrado. Sin compromiso."},
  {"branch_id": "r2_llamada", "after": "7d", "gated": false,
   "trigger": "prospect quiet +10d — offer the shortest path (the call with Peter)",
   "sentence": "¡Buenas! Sigo por aquí. Si te viene bien, lo más rápido es ver tu día a día en una llamada corta con Peter y dejarlo montado a tu medida. ¿Te busco un hueco?"},
  {"branch_id": "r3_puerta", "after": "14d", "gated": false,
   "trigger": "prospect quiet +24d — leave the door open and stop chasing",
   "sentence": "Último toque para no ser pesado 🙂 Te dejo la puerta abierta: cuando quieras probarlo con tus propios artículos, me escribes y lo montamos. Aquí sigo."}]}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'pursuit_onboarded_unpaid', null, $d$
{"opens_on":    {"event": "lifecycle.stage_changed", "when": {"stage": "onboarded_unpaid"},
                 "key": "pursuit:{stage}:{next_box}"},
 "resolves_on": {"event": "lifecycle.stage_changed", "when": {"from_stage": "onboarded_unpaid"},
                 "by": "kind", "branch_id": "stage_moved"},
 "tab": {"kind": "pursuit", "answer_shape": "free", "money": false,
         "text": "SEGUIMIENTO DE LA RELACIÓN (ya dentro, contrato sin cerrar): que la relación quede cerrada. Objetivo de fondo: {objective}",
         "brief": "{from_label}; ahora está en fase «{stage}». El objetivo de fondo es {objective} Es el rumbo de la relación, no una orden para este turno.{parent}",
         "brief_label": "seguimiento de la relación"},
 "rungs": [
  {"branch_id": "r1_ya_puedo", "after": "2d", "gated": false,
   "trigger": "onboarded but unclosed +2d — show you are already inside their business",
   "sentence": "¡Hola! Ya tengo tus artículos cargados y me los sé, así que puedo empezar a cogerte pedidos hoy mismo. ¿Lo dejamos en marcha y lo vas viendo funcionar?"},
  {"branch_id": "r2_cuando", "after": "5d", "gated": false,
   "trigger": "onboarded but unclosed +7d — one last low-pressure ask",
   "sentence": "¡Buenas! Sigo listo con lo tuyo cargado. Dime cuándo quieres que empiece a coger pedidos y me pongo; cualquier duda de cómo funciona te la resuelvo al momento."}]}
$d$::jsonb, true, 'L1')
  on conflict do nothing;

  insert into manifest_duty (scope, name, skill_ref, gate_policy, enabled, created_by)
  values ('peter', 'pursuit_onboarded', null, $d$
{"opens_on":    {"event": "lifecycle.stage_changed", "when": {"stage": "onboarded"},
                 "key": "pursuit:{stage}:{next_box}"},
 "resolves_on": {"event": "lifecycle.stage_changed", "when": {"from_stage": "onboarded"},
                 "by": "kind", "branch_id": "stage_moved"},
 "tab": {"kind": "pursuit", "answer_shape": "free", "money": false,
         "text": "SEGUIMIENTO DE LA RELACIÓN (ya dentro): que los pedidos sigan entrando sin fricción. Objetivo de fondo: {objective}",
         "brief": "{from_label}; ahora está en fase «{stage}». El objetivo de fondo es {objective} Es el rumbo de la relación, no una orden para este turno.{parent}",
         "brief_label": "seguimiento de la relación"},
 "rungs": [
  {"branch_id": "r1_pulso", "after": "14d", "gated": false,
   "trigger": "no movement +14d — a working check-in, never a pitch",
   "sentence": "¡Hola! Un toque rápido para saber si todo va bien con los pedidos. Si hay que cambiar algo del catálogo o hay algo que te esté dando guerra, dímelo y lo arreglo."}]}
$d$::jsonb, true, 'L1')
  on conflict do nothing;
end $seed_pursuit$;

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- §17.35 BACKFILL (2026-08-04) — the supplier's own article code joins the FTS document and the
-- alias table, for tenants who will not re-run the wizard. ADDITIVE + IDEMPOTENT: the tsvector
-- append is guarded by an @@ check (and `||` merges lexemes anyway); the alias insert skips any
-- alias that already has an owner (never steal). New/re-run tenants get this from golive.py.
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- Guarded: the rehearsal clone is rebuilt from THIS file alone, before db.py's brain schema —
-- so the brain tables may not exist yet. On the clone this is a no-op; golive/seed cover new
-- tenants; prod (where the tables exist) gets the backfill.
do $sku_code_backfill$
begin
  if to_regclass('catalogue_skus') is null or to_regclass('sku_aliases') is null then
    return;
  end if;
  update catalogue_skus
     set search_text = search_text || to_tsvector('english', code)
   where code is not null and code <> '' and search_text is not null
     and not search_text @@ plainto_tsquery('english', code);
  insert into sku_aliases (tenant_id, alias, sku_id, phone_number, source, weight)
  select s.tenant_id, a.alias, s.id, null, 'golive', 1
    from catalogue_skus s
   cross join lateral (values (lower(regexp_replace(s.code, '[^a-zA-Z0-9]', '', 'g')))) as a(alias)
   where s.code is not null and s.code <> ''
     and (s.code ~ '[A-Za-z]' or length(regexp_replace(s.code, '\D', '', 'g')) >= 4)
     and length(a.alias) > 0
     and not exists (select 1 from sku_aliases x where x.tenant_id = s.tenant_id and x.alias = a.alias);
end $sku_code_backfill$;

-- ── A1 TAIL (2026-08-07): whatsapp_messages joins the tenant world ─────────────────────────
-- The chats family was the last _require_runtime fence with a real reason: message rows were
-- keyed by phone alone. The column + the fill trigger close it. The trigger, not six call-site
-- edits, because the endpoint table is ALREADY tenant-keyed (A1 phase B) and it covers the
-- deploy window too: schema applies before restart, so even the old in-memory code's inserts
-- land filled. ⚠️ SHARED-PHONE CAVEAT, honestly: under ROCOTO_SCOPE_PATHED=off a phone maps to
-- one party, and `limit 1` matches that reality; the day the flag flips, writers must pass
-- tenant_id explicitly and this trigger becomes the fallback, not the rule.
alter table whatsapp_messages add column if not exists tenant_id text;
-- NB the column PRE-EXISTED on live boxes (the rails write it — sometimes as '' rather than
-- NULL), so every guard below treats the empty string as unfilled too.
create index if not exists ix_wa_msgs_tenant_phone
  on whatsapp_messages (tenant_id, phone_number, created_at desc);

create or replace function wa_msg_tenant_fill() returns trigger as $$
begin
  if new.tenant_id is null or new.tenant_id = '' then
    select e.tenant_slug into new.tenant_id from endpoint e
      where e.kind = 'wa' and e.addr = regexp_replace(new.phone_number, '\D', '', 'g')
      limit 1;
    if new.tenant_id is null or new.tenant_id = '' then
      new.tenant_id := 'rocoto';   -- the box's runtime tenant (the sku_aliases default precedent)
    end if;
  end if;
  return new;
end $$ language plpgsql;
drop trigger if exists trg_wa_msg_tenant_fill on whatsapp_messages;
create trigger trg_wa_msg_tenant_fill before insert on whatsapp_messages
  for each row execute function wa_msg_tenant_fill();

-- backfill: the endpoint join first, the box sweep second (single-tenant history was his)
update whatsapp_messages m set tenant_id = e.tenant_slug
  from endpoint e
  where (m.tenant_id is null or m.tenant_id = '') and e.kind = 'wa'
    and e.addr = regexp_replace(m.phone_number, '\D', '', 'g');
update whatsapp_messages set tenant_id = 'rocoto' where tenant_id is null or tenant_id = '';

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- PEDIDOS PRICING BUILD (2026-08-11, PEDIDOS-PRICING-BUILD.md §2/§3/§5) — LIVE Stripe billing for
-- the S/M/L/XL bands (brain/pedidos_billing.py) + the orders meter (pedidos_meter.py). ADDITIVE +
-- IDEMPOTENT, same discipline as the rest of this file: `orders` and `tenants` already exist by the
-- time this runs (db.py's own SCHEMA applies first), so no FK is declared — this codebase's
-- convention throughout is text ids with no FK (order_events → orders is the same shape).
-- ═══════════════════════════════════════════════════════════════════════════════════════════

-- One row per tenant on a Pedidos plan (mirrors employee_subscriptions' one-row-per-tenant shape).
-- `orders_ceiling` is the band's monthly cap (spec §2); `minimum_term_end` is set only for a monthly
-- (`cadence='mensual'`) subscription (spec §5's three-month minimum) and enforced by
-- brain/pedidos_billing.py on customer.subscription.updated. `warn_period`/`warned_*_at` are the
-- once-per-calendar-month guard against re-sending the 80%/ceiling email (pedidos_meter.py).
create table if not exists pedidos_subscription (
  tenant_id               text primary key,
  band                    text not null,                    -- s | m | l | xl
  cadence                 text not null default 'mensual',   -- mensual | anual
  operator_email          text,
  stripe_customer_id      text,
  stripe_subscription_id  text,
  status                  text not null default 'pending',   -- pending|active|past_due|canceled
  minimum_term_end        timestamptz,
  cancel_at               timestamptz,
  current_period_end      timestamptz,
  alta_paid_at            timestamptz,
  priority_support_until  timestamptz,
  orders_ceiling          int not null default 0,
  warn_period             text,                              -- 'YYYY-MM' the two flags below apply to
  warned_80_at            timestamptz,
  warned_ceiling_at       timestamptz,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);
create index if not exists idx_pedidos_subscription_stripe_sub
  on pedidos_subscription (stripe_subscription_id);
create index if not exists idx_pedidos_subscription_stripe_cust
  on pedidos_subscription (stripe_customer_id);
-- The welcome email's send-once ledger (2026-08-12). `create table if not exists` never adds a
-- column to a table that already exists, so a NEW column needs its own explicit ALTER — the law
-- this file has carried since the first migration that forgot it.
-- WHY A COLUMN AND NOT A FLAG IN CODE: Stripe redelivers webhooks by contract, and
-- `checkout.session.completed` is handled idempotently precisely because it arrives more than
-- once. "Send the welcome once" therefore has to be settled by the database. The claim is an
-- atomic `update … where welcomed_at is null returning 1`, so of two concurrent deliveries exactly
-- one wins and exactly one email goes out. See brain/pedidos_welcome_email.py.
alter table pedidos_subscription add column if not exists welcomed_at timestamptz;

-- The orders meter (spec §3): one row per order EVER counted, the instant it first reaches
-- 'approved' or 'sent'. The UNIQUE key (not an incrementing column) is what makes "counted once,
-- ever" true regardless of which code path flips the status or how many times it's re-observed
-- (pedidos_meter.record_order / .reconcile are both idempotent inserts against this same key).
create table if not exists pedidos_order_usage (
  tenant_id       text not null,
  order_id        text not null,
  counted_status  text not null,     -- 'approved' | 'sent' — whichever crossed first (see
                                      -- pedidos_meter.py's SEAM COVERAGE note on reconcile()'s label)
  counted_at      timestamptz not null default now(),
  primary key (tenant_id, order_id)
);
create index if not exists idx_pedidos_order_usage_tenant_time
  on pedidos_order_usage (tenant_id, counted_at desc);

-- BILLING THE METER (STRIPE-V2-PLAN.md §3b, added 2026-08-15). Counting an order and BILLING it are
-- two different facts and they were the same column: `pedidos_order_usage` knew every order ever
-- counted and nothing at all about which of them Stripe had been told about. These two carry that.
--
-- ⚠️ THEIR OWN ALTERs, PER THE MIGRATION LAW ALREADY WRITTEN ABOVE FOR `welcomed_at`: this file is
-- `create table if not exists`, applied fail-soft at boot, so editing the create-table block to add
-- a column is a SILENT NO-OP on prod — the table exists, the statement is skipped, and the column
-- never appears. Every new column gets an explicit `add column if not exists` or it does not ship.
--
--   reported_at  when brain/pedidos_usage_report.py settled this order. NULL = not yet settled, and
--                it is the PERMANENT exactly-once guard: Stripe's own `identifier` dedupe on a
--                MeterEvent only covers a rolling ~24 h window, so neither one alone is enough.
--   billed_via   HOW it was settled, so a customer's invoice can be explained a year later without
--                re-deriving it: 'metered' (reported to the `rocoto_pedido` meter) · 'bundle:<price>'
--                (drawn off a prepaid bag, no Stripe call) · 'included:<founder>' (a pinned price
--                that already covers their orders) · 'stale' (older than Stripe's 35-day window, so
--                deliberately never billed — under-billing is the safe direction, silently mis-dated
--                revenue is not).
alter table pedidos_order_usage add column if not exists reported_at timestamptz;
alter table pedidos_order_usage add column if not exists billed_via  text;

-- The reporter's only hot query is «this tenant's unsettled orders, oldest first». A PARTIAL index
-- keeps it proportional to the backlog rather than to every order ever counted — the settled rows
-- are the ones that grow without bound and they leave the index the moment they are stamped.
create index if not exists idx_pedidos_order_usage_unreported
  on pedidos_order_usage (tenant_id, counted_at) where reported_at is null;

-- Stripe webhook idempotency (spec §5: "Idempotent on event.id"). One row per event id this ONE
-- shared endpoint's pedidos consumer (brain/pedidos_billing.handle_webhook) has already processed;
-- a redelivered event is a no-op insert (on conflict do nothing) rather than a re-run handler.
create table if not exists pedidos_webhook_events (
  event_id     text primary key,
  event_type   text not null default '',
  received_at  timestamptz not null default now()
);

-- The Empresa signal (shared contract 2/3: `plan` in GET /api/pedidos/usage is 'pedidos' | 'empresa'
-- | null, and it could never actually be 'empresa' before this — nothing in the codebase set it, so
-- a tenant who bought Empresa stayed locked out of what they paid for). `tenants.plan` (db.py's
-- `tenants` table, already `text`, no default) was already sitting there as db.py's own comment says
-- — "a forward hook for per-team pricing" — never read or written by anything. This is that hook's
-- first real use: it fits the existing schema better than a new column, because Empresa is
-- contract/quoted (spec §4 "Hablemos") and has NO Stripe subscription row of its own, so it cannot
-- live on `pedidos_subscription` (that table is one-row-per-tenant for the S/M/L/XL band checkout
-- only, and an Empresa tenant may never have a row there at all). The CHECK is additive/idempotent
-- (guarded by a catalog lookup, same idiom as BLOCK B6 above) so a live column with existing NULLs
-- is never rejected. NULL still means "no plan" — `_pedidos_plan_for` in app.py reads this column
-- FIRST ('empresa' short-circuits) and falls back to the pedidos_subscription active-status check.
-- `tenants` is created by db.py at boot, not by this file, so on a fresh rehearsal rebuild this runs
-- before the table exists. Guard on the table too, or the ALTER raises and takes the whole migration
-- down as a class-C fault (it did, 2026-08-11).
do $tenants_plan_chk$
begin
  if to_regclass('public.tenants') is not null
     and not exists (select 1 from pg_constraint where conname = 'tenants_plan_chk') then
    alter table tenants add constraint tenants_plan_chk
      check (plan is null or plan in ('pedidos','empresa'));
  end if;
end $tenants_plan_chk$;


-- ── THE USAGE SUBSCRIPTION ──────────────────────────────────────────────────────────────────────
-- A SECOND STRIPE SUBSCRIPTION PER TENANT, and the reason is a Stripe rule rather than a design
-- preference: every recurring item on one subscription must share a billing interval. Peter,
-- 2026-08-17: «Stripe should, in all cases, be charging the three months in one go… the metered per
-- order would be a monthly thing, whatever they've used for the month.» Those two facts cannot live
-- on one subscription, so the TERM (seat fee + modules, prepaid quarterly or yearly) stays on
-- `stripe_subscription_id` and the METER (orders, monthly, in arrears) lands here.
--
-- The customer still does ONE checkout and pays ONCE; this row is minted server-side afterwards
-- against the card Checkout already saved.
--
-- `create table if not exists` above never adds a column to a table that already exists, which is
-- why this is an explicit ALTER — the migration law for this file.
alter table if exists pedidos_subscription
  add column if not exists stripe_usage_subscription_id text;

-- Every webhook finds a tenant by whichever subscription id arrived, so the second one needs its
-- own index or a monthly usage invoice costs a sequential scan on every event.
create index if not exists pedidos_subscription_usage_sub_idx
  on pedidos_subscription (stripe_usage_subscription_id);

-- ── «SHOW THE STATUS LABEL OF THINGS PRINTED» (Raquel, 2026-08-19) ──────────────────────────────
-- She prints the day's orders and then works off paper. With no record of what has already been
-- pulled, the only way to know is to remember — so an order gets printed twice, or missed, and the
-- mistake only surfaces at the van. `printed_at` is the smallest honest fact: the moment the sheet
-- was sent to the printer from this desk. Guarded and additive, because `orders` is not created by
-- this file.
alter table if exists orders add column if not exists printed_at timestamptz;

-- ── THE HUMAN HOLD (Raquel, 2026-08-19) ─────────────────────────────────────────────────────────
-- «Que yo, como Raquel jefa, pueda meterme como humana a seguir entablando una conversación…
-- enderezar la conversación con él a nivel particular.» The takeover VERB existed (/api/takeover)
-- but the natural gesture did not: when the boss types as herself in a thread, Rocoto kept
-- replying over her. `human_hold_until` is a TTL her own messages bump — while it stands, Rocoto's
-- autonomous replies on this scope are DRAFTED AND HELD (sombra's own exit), never sent. Expiry is
-- the handback, so a forgotten takeover cannot kill a thread forever.
alter table scope_state add column if not exists human_hold_until timestamptz;

-- ── THE LAST STEP OF AN ORDER (Peter, 2026-08-19) ───────────────────────────────────────────────
-- `printed_at` already records that paper left for the printer. These two record the step AFTER it,
-- for the suppliers whose trade has one — and ONLY for them (behaviour 'finish': print|weigh|close):
--   · weighed_kg  the real kilos off the scale. Fish and meat are ordered by the piece and billed
--                 by the weight, so until this exists the invoice is an estimate wearing a total.
--   · closed_at   the order is handled and off the desk. They may have closed it in their own
--                 program already — this is their bookkeeping, not a claim about the ERP.
-- Both additive and nullable: a supplier on 'print' never writes either, and nothing on their desk
-- shows a column about them.
alter table if exists orders add column if not exists weighed_kg  numeric;
alter table if exists orders add column if not exists closed_at   timestamptz;
