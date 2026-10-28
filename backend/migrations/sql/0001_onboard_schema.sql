-- CRM leads. One row per prospect (deduped on canonical E.164 phone). status walks
-- signed_up → call_booked → called → done → wizard_sent → waitlisted.
create table if not exists leads (
  id              text primary key,
  phone           text not null,
  name            text default '',
  business        text default '',
  email           text default '',
  status          text not null default 'signed_up',
  cal_booking_ref text,
  ref             text default '',           -- attribution (utm/source)
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create unique index if not exists idx_leads_phone on leads (phone);
-- Supabase identity link (2026-07-09): marries a lead to its authenticated Supabase user, so the
-- customer can sign in from the landing and resume onboarding at their furthest step, and so payments
-- can later be tied to a stable user id. Nullable + additive (existing leads bind on first sign-in).
alter table leads add column if not exists supabase_user_id text;
create index if not exists idx_leads_supabase_user on leads (supabase_user_id);
-- One lead per Supabase user (partial → many phoneless/NULL leads still coexist). Arbiter for the
-- ON CONFLICT in auth_state's identity-lead insert: guards the concurrent-double-sign-in race.
create unique index if not exists idx_leads_supabase_user_uq on leads (supabase_user_id) where supabase_user_id is not null;

-- Auth v2 (2026-07-09): phoneless identity-first leads. A Google/email sign-in creates the lead the
-- moment the user authenticates — before we know their phone. Drop the NOT NULL and make phone
-- uniqueness apply ONLY to non-null phones, so many phoneless identity-leads can coexist while a real
-- phone still dedupes to one lead. Idempotent + additive; existing rows all have phones (zero impact).
alter table leads alter column phone drop not null;
drop index if exists idx_leads_phone;
create unique index if not exists idx_leads_phone on leads (phone) where phone is not null;
-- Mirror the cal.com booking start time so the booking-status modal can show "Tu llamada está
-- reservada para {fecha}" without a round-trip to cal.com. Nullable + additive.
alter table leads add column if not exists cal_booking_start timestamptz;
-- What they answered on /precios, and what it priced out at (2026-08-14, POST /api/leads/pricing).
-- Peter: «Email is the alert, not the record» — so the alert email is not allowed to be the only
-- copy of ten answers somebody spent two minutes giving us. It lands HERE, on the lead the CRM
-- already lists, so a conversation three weeks later opens with what they told us.
--
-- ONE COLUMN, NOT A TABLE, and not a second store: the shape is the quiz's, the quiz's shape moves
-- whenever the page moves, and there is exactly one row of it per lead — which is a jsonb column,
-- not a schema. `brain/onboarding.log_event` still records the submission in `events` as it does for
-- every other lead action, so the audit trail keeps its timeline; this column is the CURRENT answer
-- set, the one the CRM's `select l.*` already picks up with no join.
--
-- The ALTER lives in THIS file rather than schema_runtime.sql because this file is where the leads
-- table is created and where its other sixteen ALTERs live — the migration law's rule is that a new
-- column arrives as its own explicit `add column if not exists` statement and never as an edit to a
-- `create table if not exists` block (which is a silent no-op on a box that already has the table),
-- and that is satisfied here. Splitting one table's DDL across two files would cost more than it buys.
alter table leads add column if not exists pricing_quiz jsonb;

-- Ingested call notes (Markdown born in /home/founder/calls/*.md). note_path is the idempotency
-- key: a note is ingested at most once. transcript = the cleaned '## Transcript' section, raw = the
-- '## Raw transcript' section (or the whole body when the sections are absent).
create table if not exists calls_onboard (
  id             text primary key,
  lead_id        text,
  phone          text default '',
  note_path      text unique,
  transcript     text default '',
  raw_transcript text default '',
  duration_sec   int,
  kind           text default '',
  created_at     timestamptz not null default now()
);
create index if not exists idx_calls_onboard_lead  on calls_onboard (lead_id);
create index if not exists idx_calls_onboard_phone on calls_onboard (phone);

-- Structured facts extracted from a call transcript by the (later) Sonnet P0 stage. Left empty
-- for now — the extractor is a stub; this table just holds the shape.
create table if not exists transcript_facts (
  id         text primary key,
  call_id    text,
  lead_id    text,
  field      text not null,
  value      jsonb not null default '{}'::jsonb,
  confidence text default '',
  mentions   jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_tfacts_call on transcript_facts (call_id);
create index if not exists idx_tfacts_lead on transcript_facts (lead_id);

-- Opaque tokens that gate the personalized wizard link (/bienvenida/<token>). One active token per
-- minted invite; validated non-leaking (unknown/used → null, never a distinguishable error).
create table if not exists onboarding_tokens (
  token       text primary key,
  lead_id     text,
  tenant_slug text,
  status      text not null default 'active',   -- active | used
  created_at  timestamptz not null default now(),
  used_at     timestamptz
);
create index if not exists idx_onboard_tokens_lead on onboarding_tokens (lead_id);

-- ── §17.41 · THE INVITATION, WHOSE TOKEN IS THE LINK (WP-A3) ─────────────────────────────────
-- «invitation — a row (email · role · org · inviter · expires_at 7d · token) whose TOKEN IS THE
--  LINK. States per §17.33 (pending → active; resend; revoke kills the link immediately).»
--
-- SEPARATE FROM `onboarding_tokens`, which is the LEAD's door (a prospect finishing a wizard). This
-- is a MEMBER's door into an org that already exists, and conflating them would mean one revoke
-- path, one expiry policy and one status vocabulary for two different acts.
--
-- §17.33's honesty rule is why `accepted_at` and `revoked_at` are columns rather than a status
-- string: «nothing about the person changes until they accept» — an invitation is a state a person
-- is IN, and the roster must be able to say when it was sent, whether it was opened, and that
-- revoking killed the link immediately. `opened_at` exists for exactly that sentence in the UI.
create table if not exists invitation (
  token       text primary key,                  -- THE LINK. Unguessable, minted server-side.
  tenant_slug text not null,
  email       text not null,
  role        text not null default 'staff',     -- owner | staff | repartidor (§17.34's three)
  invited_by  text not null default '',          -- the actor, for «María te ha invitado»
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null,              -- 7 days (§17.41); enforced on ACCEPT, not by a sweep
  opened_at   timestamptz,                       -- «sent 2 days ago · not opened yet»
  accepted_at timestamptz,
  revoked_at  timestamptz                        -- set = the link is dead THIS INSTANT
);
create index if not exists ix_invitation_tenant on invitation (tenant_slug);
-- one LIVE invitation per (org, email): re-inviting someone re-sends rather than accumulating
-- links that all still work. Partial, so the history of accepted/revoked rows is kept.
create unique index if not exists ix_invitation_live on invitation (tenant_slug, lower(email))
  where accepted_at is null and revoked_at is null;

-- ── THE-JOURNEY-CANON §2.1 · THE ACCOUNT CLAIM — step 1 of the mono-account journey ──────────
-- «Peter sends their link; they claim the account (real auth, email).» Peter mints ONE link for
-- ONE address at ONE house; claiming it proves the address, provisions the workspace and mints the
-- panel's own session. The rail is `claim.py`; the discipline is `demo_auth.py`'s, borrowed whole.
--
-- SEPARATE FROM `invitation` ABOVE, which is a MEMBER's door into an org that already exists
-- (§17.41). This is the door that MINTS one. Conflating them would mean one expiry policy and one
-- status vocabulary for two different acts — and only one of them may create a world.
--
-- SEPARATE FROM `demo_magic_links`, which is a SIGN-IN: that table carries an address and nothing
-- else, because /entrar opens the workspace the address already owns. A claim names the HOUSE on
-- the link (§2.1 property 5: «no field on the page can change which house is claimed»), so the
-- house is a column here and is never read off a request body.
--
-- Only the token's HASH is stored, for the reason `demo_magic_links` states: a database dump must
-- not be a pile of working accounts. `claimed_ip` / `claimed_ua` / `consumed_at` / `minted_by` ARE
-- §2.1 property 7's audited row — who, when, from what agent, against which invite.
create table if not exists account_claim (
  token_hash   text primary key,
  address      text not null,
  house        text not null default '',
  minted_by    text not null default '',
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  nonce_hash   text,                              -- set when the claim PAGE renders; matched on POST
  opened_at    timestamptz,
  consumed_at  timestamptz,                       -- the single-use gate, written by the UPDATE
  claimed_ip   text,
  claimed_ua   text,
  tenant_slug  text,                              -- the house this claim actually opened
  revoked_at   timestamptz                        -- set = the link is dead THIS INSTANT
);
create index if not exists ix_account_claim_address on account_claim (lower(address), created_at desc);
-- one LIVE claim per address: re-minting supersedes rather than accumulating links that all work.
create unique index if not exists ix_account_claim_live on account_claim (lower(address))
  where consumed_at is null and revoked_at is null;

-- Per-token wizard progress. wizard_state is a verbatim jsonb blob the React wizard owns; version
-- bumps on every save (optimistic-concurrency hook for later).
create table if not exists onboarding_progress (
  token        text primary key,
  wizard_state jsonb not null default '{}'::jsonb,
  step         int not null default 0,
  version      int not null default 0,
  updated_at   timestamptz not null default now()
);

-- Referral-only waitlist. position = row_number() over (invited_count desc, created_at asc),
-- recomputed on read. invited_count bumped ATOMICALLY (never read-modify-write).
create table if not exists waitlist (
  id            text primary key,
  lead_id       text,
  phone         text default '',
  email         text default '',
  first_name    text default '',
  referral_code text,
  referred_by   text,
  invited_count int not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create unique index if not exists idx_waitlist_refcode on waitlist (referral_code) where referral_code is not null;
create index if not exists idx_waitlist_referred_by on waitlist (referred_by);
create index if not exists idx_waitlist_rank on waitlist (invited_count desc, created_at asc);

-- Append-only audit/event log for a lead (status changes, mark-done, wizard submit, etc.).
create table if not exists events (
  id         bigserial primary key,
  lead_id    text,
  type       text not null,
  payload    jsonb not null default '{}'::jsonb,
  actor      text default '',
  created_at timestamptz not null default now()
);
create index if not exists idx_events_lead on events (lead_id);

-- Per-tenant chosen ElevenLabs voice (the wizard's 'elige la voz' step). Global TTS voice is env
-- today; this makes it per-supplier. Additive on the existing tenants table.
alter table tenants add column if not exists voice_id text;

-- WP-2 golive (M1M2): the tenant's OWN WhatsApp number + the one-shot activation claim.
-- `number` is bound at go-live from the CR «Números» surface (E.164; the partial unique index makes
-- two tenants sharing a line structurally impossible — the race-safe belt under the friendly
-- number_taken pre-check). `golive_at` is the ATOMIC claim: welcome/bind/activation fire exactly once
-- (update … where golive_at is null); a second golive is a friendly already_live no-op.
alter table tenants add column if not exists number text;
alter table tenants add column if not exists golive_at timestamptz;
create unique index if not exists uq_tenants_number on tenants (number) where number is not null;

-- Wave-3 (supplier web app): the per-supplier «Tienda online» toggle — capability-ladder tier 2
-- (SEPARATED) surfacing as an OPT-IN. Turning it ON records INTENT only (`ecommerce_requested_at`)
-- and surfaces to Peter via ops.record_alert; the Medusa revival stays the human-run runbook
-- (ECOMMERCE-MOTHBALL-REVIVAL-RUNBOOK.md) — nothing auto-provisions. Distinct from
-- employee_subscriptions.ecommerce_enabled (Carmen's 0.3% application-fee/billing flag).
alter table tenants add column if not exists ecommerce_enabled boolean not null default false;
alter table tenants add column if not exists ecommerce_requested_at timestamptz;

-- §5.8a/§5.8b · «Inventario» — OFF BY DEFAULT, per tenant, exactly the ecommerce_enabled pattern
-- above (an opt-in boolean on the tenant row; the only per-tenant capability flag this codebase has).
-- It gates the ONE thing that matters outside the app: whether a stock level may ever hold an order
-- for review (brain/stock.py). A distributor who never counts stock must never see an order flagged
-- «no hay stock» — silence, not a guess. Lives in ONBOARD_SCHEMA and not BRAIN_SCHEMA on purpose:
-- `tenants` is created by SHOP_SCHEMA, which runs AFTER BRAIN_SCHEMA, and BRAIN_SCHEMA is one
-- multi-statement execute — an `alter table tenants` in there would abort the whole brain schema on
-- a fresh box.
alter table tenants add column if not exists inventory_enabled boolean not null default false;

-- first-sign-in onboarding (Peter, 2026-08-07): NULL = the typeform wizard has not run for this
-- tenant. Backfill is DATE-FENCED, not open-ended — an open `where onboarded_at is null` would
-- re-mark every future fresh tenant on each boot and the wizard would never fire for anyone.
alter table tenants add column if not exists onboarded_at timestamptz;
update tenants set onboarded_at = created_at
  where onboarded_at is null and created_at < timestamptz '2026-08-07';

-- ── WHICH DOOR MINTED THIS WORLD (2026-08-09, the /demo retention fix — demo_flow.reap) ──────────
-- `provisioning.provision` is the ONE writer of a workspace and it has TWO doors: the alta CTA and
-- the /demo sign-in. `soyrocoto.com/privacy.html` and `/datos-seguridad.html` both publish «los
-- datos de una demo se conservan un máximo de 90 días si no llegamos a trabajar juntos», and the
-- reaper could not perform that sentence because it had no way to tell the two apart: it matched a
-- `demo_`-prefixed slug that was also marked `seeded`, and the slug provision derives is the
-- EMAIL's handle and is never seeded. Zero rows, forever, against a live published promise.
--
-- SO THE DOOR IS RECORDED AT CREATION, as a column, instead of being inferred from the SHAPE OF A
-- SLUG — slug-shape coupling is exactly what broke, and a marker written by the code that knows the
-- answer cannot drift from a derivation that guesses it. NULL for every row that predates this line
-- (legacy: never reapable by the new rung, which is the safe direction — the old rung still names
-- the legacy anonymous worlds explicitly).
--   'demo'  minted by the /demo sign-in    → reapable ONCE it is past the window AND never went live
--   'alta'  minted by the panel's alta CTA → never reapable
alter table tenants add column if not exists provisioned_via text;
create index if not exists idx_tenants_provisioned_via on tenants (provisioned_via)
  where provisioned_via is not null;

-- ── §17.2 THE ENTITLEMENT ROW — one table, replacing the ad-hoc booleans (WP-A2) ──────────────
-- «Today entitlement is three unrelated flags (tenants.inventory_enabled,
--  tenants.ecommerce_enabled, employee_subscriptions.ecommerce_enabled). That does not survive
--  five products.» — §17.2
--
-- `state` MIRRORS `tenants.status` on purpose: a product is never live while the tenant is in test
-- (§17.9's wall). The three-value shape is what lets a supplier configure Reparto in test and arm
-- it later without a second concept.
--
-- READ THROUGH brain/entitlement.py, never directly: while the legacy flags still exist they remain
-- AUTHORITATIVE, and that module is where the two are reconciled. A surface that reads this table
-- raw would disagree with the runtime the day someone flips the old boolean.
create table if not exists tenant_product (
  tenant_id    text not null,
  product      text not null,             -- pedidos | reparto | cobros | inventario | inteligencia | tienda
  state        text not null default 'off' check (state in ('off','test','live')),
  settings     jsonb not null default '{}'::jsonb,
  stripe_item  text,                      -- the subscription item / meter this product bills on
  enabled_at   timestamptz,
  disabled_at  timestamptz,
  enabled_by   text default '',           -- the actor; every flip is ALSO a spine event (§17.2)
  primary key (tenant_id, product)
);
create index if not exists ix_tenant_product_live on tenant_product (tenant_id) where state <> 'off';

-- Tiny key/value flag store (durable, survives restarts). Used by the self-terminating WhatsApp-
-- template-approval watcher (brain/wa_watch.py): once wa_message_enabled flips true it stays true
-- and the watcher never runs again. Generic enough for future one-shot runtime flags.
create table if not exists system_flags (
  key        text primary key,
  value      text not null default '',
  updated_at timestamptz not null default now()
);

-- ── §17.47b.4 · CUSTOM INBOUND PURPOSES — «an address is a purpose; a purpose is an agent».
-- The two built-ins (pedidos, facturas) are code; anything else the supplier DESCRIBES to the
-- copilot lands here, so a tenant can mint «nominas.<slug>@…» and say where its rows go. The
-- routing recipe is prose on purpose: the agent reads it, the schema does not constrain it.
create table if not exists inbound_purpose (
  tenant_slug text not null,
  purpose     text not null,                 -- the local-part prefix: [a-z0-9-]+
  label       text not null default '',      -- what the supplier calls it
  recipe      text not null default '',      -- what should happen to what arrives, in their words
  created_by  text not null default '',
  created_at  timestamptz not null default now(),
  primary key (tenant_slug, purpose)
);

-- ── R6 · doc_prefs: the print modal's options, remembered per tenant (§17.31 / SOTA R6).
-- Additive + tiny: one jsonb of {lot,tick,price,sig}; the modal seeds from it and writes back.
create table if not exists doc_prefs (
  tenant_slug text primary key,
  opts        jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

-- ── Re-engagement drip: one enrolment row per lead, walks the send ladder (brain/drip.py) ──
-- Additive + isolated (relates by lead_id, no hard FK: fail-soft). A lead is ENROLLED at auth/signup
-- time; the drip cron advances `step` and stamps `last_sent_at` as each email goes out, and sets
-- `status` terminal (completed | stopped | unsubscribed) so it is never dripped again.
create table if not exists drip_state (
  lead_id       text primary key,                         -- relates to leads.id (no hard FK: fail-soft)
  status        text not null default 'active',           -- active | completed | stopped | unsubscribed
  step          int  not null default 0,                  -- last send step COMPLETED (0 = none yet; 4 = final sent)
  enrolled_at   timestamptz not null default now(),       -- t0 for the whole ladder
  last_sent_at  timestamptz,                              -- when the most recent email went out
  next_send_key text,                                     -- 't2h'|'d1'|'d3'|'d7' — the send we owe next (nullable)
  completed_at  timestamptz,                              -- set when the lead booked (drip stop reason)
  unsub_token   text unique,                              -- opaque one-click unsubscribe token
  sends         jsonb not null default '[]'::jsonb,       -- audit: [{key,at,ok,resend_id?}]
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists idx_drip_state_due on drip_state (status, last_sent_at);
create index if not exists idx_drip_state_unsub on drip_state (unsub_token) where unsub_token is not null;

-- ── Test-mode isolation (Stripe `livemode` pattern) + Owner/RBAC (TEST-MODE-AND-ACCESS spec) ──
-- Additive, idempotent, DARK: default false → every existing row is LIVE, every existing user a
-- customer. Isolation is APP-LAYER (Supabase holds no app data → Postgres RLS protects nothing); these
-- columns are the flag the central query chokepoint (brain/onboard_q.py) filters on. A missed WHERE
-- would leak test data to a real customer, so every onboarding/CRM query routes through onboard_q.
alter table leads               add column if not exists is_test boolean not null default false;
alter table onboarding_tokens   add column if not exists is_test boolean not null default false;
alter table onboarding_progress add column if not exists is_test boolean not null default false;
alter table waitlist            add column if not exists is_test boolean not null default false;
alter table drip_state          add column if not exists is_test boolean not null default false;
alter table calls_onboard       add column if not exists is_test boolean not null default false;
-- Peter's own notes on the call (WP-6 intake). The call note markdown carries a `## Notes` (or
-- `## Notas`) section alongside the transcript; ingest parses it here so the ficha extractor reasons
-- over BOTH the transcript AND Peter's notes. Additive + nullable-ish (default '' → existing rows unaffected).
alter table calls_onboard       add column if not exists notes text default '';
alter table transcript_facts    add column if not exists is_test boolean not null default false;
alter table events              add column if not exists is_test boolean not null default false;
-- Partial/compound indexes so the common "live only" and "test only" scans stay cheap. (NB: a
-- non-concurrent CREATE INDEX takes a brief ACCESS EXCLUSIVE lock at boot — trivial at current row
-- counts; switch to CREATE INDEX CONCURRENTLY out-of-band if these tables ever grow large.)
create index if not exists idx_leads_is_test    on leads (is_test, created_at desc);
create index if not exists idx_tokens_is_test   on onboarding_tokens (is_test);
create index if not exists idx_waitlist_is_test on waitlist (is_test);

-- ── Setup fee (€50 'alta') payment record + email idempotency (brain/setupfee.py) ──
-- The wizard's final gate: a Stripe Checkout charge that finalizes onboarding. Stored per token so
-- BOTH the browser return (?paid=1) AND the Stripe webhook resolve to the same row. paid_at is set
-- once payment is confirmed; invoice_email_sent_at is the atomic guard that sends the branded invoice
-- email exactly once across the redirect + webhook (+ invoice.paid backstop) paths.
alter table onboarding_progress add column if not exists paid_at               timestamptz;
alter table onboarding_progress add column if not exists stripe_session_id     text;
alter table onboarding_progress add column if not exists stripe_payment_intent text;
alter table onboarding_progress add column if not exists stripe_invoice_id     text;
alter table onboarding_progress add column if not exists amount_total          int;
alter table onboarding_progress add column if not exists currency              text;
alter table onboarding_progress add column if not exists invoice_hosted_url    text;
alter table onboarding_progress add column if not exists invoice_pdf_url       text;
-- M1M2 WP-4 (eng F7): the card that pays the €500 alta IS the card the 0.3% meter charges.
-- Checkout runs with customer_creation='always' + setup_future_usage='off_session'; the resulting
-- Stripe Customer (cus_…) is persisted here and the commission remainder invoice bills it.
alter table onboarding_progress add column if not exists stripe_customer_id    text;
alter table onboarding_progress add column if not exists refunded_at           timestamptz;
alter table onboarding_progress add column if not exists refunded_amount       integer not null default 0;
alter table onboarding_progress add column if not exists invoice_email_sent_at timestamptz;

-- Owner/RBAC: a lead already == a Supabase user (supabase_user_id), so the role lives on the lead.
-- role in (customer|admin|owner); exactly one is_master_owner. owner_actor stamps WHICH owner minted a
-- TEST lead — test leads carry supabase_user_id=NULL so they can never collide with, or hijack, the
-- owner's LIVE identity-lead on idx_leads_supabase_user_uq, and a wipe is scoped to their creator.
alter table leads add column if not exists role text not null default 'customer';
alter table leads add column if not exists is_master_owner boolean not null default false;
alter table leads add column if not exists owner_actor text;
create index if not exists idx_leads_owner_actor on leads (owner_actor) where owner_actor is not null;

-- is_test IMMUTABILITY (defense-in-depth): once a row is written test/live it can never flip. One
-- BEFORE UPDATE trigger fn attached to every onboarding/CRM table; normal updates (that don't touch
-- is_test) pass straight through. Idempotent (drop-if-exists + recreate).
create or replace function rocoto_is_test_immutable() returns trigger as $rocoto_imm$
begin
  if new.is_test is distinct from old.is_test then
    raise exception 'is_test is immutable (table %)', tg_table_name;
  end if;
  return new;
end;
$rocoto_imm$ language plpgsql;
do $rocoto_imm_do$
declare t text;
begin
  foreach t in array array['leads','onboarding_tokens','onboarding_progress','waitlist',
                           'drip_state','calls_onboard','transcript_facts','events'] loop
    execute format('drop trigger if exists trg_is_test_immutable on %I', t);
    execute format('create trigger trg_is_test_immutable before update on %I '
                   'for each row execute function rocoto_is_test_immutable()', t);
  end loop;
end
$rocoto_imm_do$;
