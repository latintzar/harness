-- ── THE CONNECTION — one authorisation, one external account (a WABA for WhatsApp) ─────────────
create table if not exists channel_connection (
  id             bigserial primary key,
  tenant_id      text not null,
  kind           text not null check (kind in ('whatsapp','email','voice')),
  provider       text not null check (provider in ('meta_cloud','twilio','resend','ses')),
  -- Meta's WhatsApp Business Account id. The connection's identity ON THE PLATFORM, so a
  -- reconnect finds the same row instead of minting a second one.
  external_id    text not null default '',
  display_name   text not null default '',
  status         text not null default 'connecting'
                 check (status in ('connecting','connected','error','revoked')),
  -- COEXISTENCE IS A PROPERTY OF THE CONNECTION, not a separate table: the same WABA can hold a
  -- number the business still answers by hand on their phone. The runtime has to know, because
  -- what it may assume about "who replied" changes.
  coexistence    boolean not null default false,
  -- WHY THE TOKEN IS NOT IN THIS TABLE. A customer's business token is the only credential here we
  -- cannot store as a hash — we have to USE it to send. It lives in `channel_secret`, encrypted,
  -- with its own grants, so an accidental `select * from channel_connection` in a log, a support
  -- query or a pg_dump excerpt cannot leak the ability to message somebody's customers.
  scopes         text not null default '',
  last_error     text not null default '',
  connected_by   text not null default '',
  connected_at   timestamptz,
  revoked_at     timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
-- One row per (tenant, kind, external account). A re-run of Connect UPDATES rather than duplicates.
create unique index if not exists ux_channel_conn_ext
  on channel_connection (kind, external_id) where external_id <> '';
create index if not exists ix_channel_conn_tenant on channel_connection (tenant_id, kind)
  where revoked_at is null;

-- ── THE NUMBER — what "Add number" produces, and the INBOUND ROUTING KEY ───────────────────────
create table if not exists channel_number (
  id             bigserial primary key,
  connection_id  bigint not null references channel_connection(id) on delete cascade,
  tenant_id      text not null,
  kind           text not null default 'whatsapp',
  -- Meta's phone_number_id. THE key every inbound webhook carries as
  -- value.metadata.phone_number_id, so this is what the router reads. Globally unique BY PLATFORM
  -- RULE, and the unique index below is what makes a mis-routed inbound impossible rather than
  -- unlikely — it is the same fence `tenants.wa_phone_number_id`'s unique index used to give.
  external_id    text not null,
  e164           text not null default '',
  label          text not null default '',           -- «Pedidos Barcelona» — the supplier's word
  -- cloud       — a number Rocoto answers on, full stop.
  -- coexistence — the business ALSO answers it by hand in the WhatsApp Business app. The runtime
  --               must never assume silence means "nobody replied".
  mode           text not null default 'cloud' check (mode in ('cloud','coexistence')),
  status         text not null default 'pending'
                 check (status in ('pending','registering','live','suspended','removed')),
  is_primary     boolean not null default false,
  verified_name  text not null default '',
  quality_rating text not null default '',
  throughput     text not null default '',
  last_synced_at timestamptz,
  registered_at  timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
-- WHO THIS NUMBER BELONGS TO (2026-08-19). Peter: «the company being able to create new users for
-- the different salespeople… and then allocating a number to them. The main thing would be
-- allocating a number.»
--
-- A distributor is not one person with one WhatsApp: it is a boss and some comerciales, and a
-- customer usually has «their» person. NULL means the house — the number nobody in particular owns,
-- which is both the default and the honest answer for a one-person business. It is an EMAIL rather
-- than a user id because that is what `member` is keyed on and what an invitation carries, so a
-- number can be allocated to somebody the day they are invited rather than the day they first
-- sign in.
alter table channel_number add column if not exists owner_email text;

create unique index if not exists ux_channel_number_ext on channel_number (kind, external_id);
create index if not exists ix_channel_number_tenant on channel_number (tenant_id, kind)
  where status <> 'removed';
-- Exactly ONE primary per (tenant, kind) — enforced by the DATABASE, because "which number do we
-- answer from when the conversation does not say" is a question that must have one answer.
create unique index if not exists ux_channel_number_primary
  on channel_number (tenant_id, kind) where is_primary and status <> 'removed';

-- ── THE SECRET — separated on purpose, encrypted at rest, never selected by accident ───────────
create table if not exists channel_secret (
  connection_id  bigint primary key references channel_connection(id) on delete cascade,
  tenant_id      text not null,
  -- AES-256-GCM, key from ROCOTO_SECRET_KEY, nonce prefixed. `channels.py` is the ONLY reader.
  ciphertext     bytea not null,
  key_version    int not null default 1,
  -- The last 4 characters, for a human to recognise a token without it being usable.
  hint           text not null default '',
  rotated_at     timestamptz,
  created_at     timestamptz not null default now()
);

-- ── ROUTING — which number a conversation belongs to, and who inside the tenant owns it ────────
-- Peter: «we need to be able to route and have a selector for who to route shit to in the customer
-- panel for each customer». Two different questions, so two columns and neither is guessed:
--   channel_number_id — which of OUR numbers this customer talks to (so the reply leaves from the
--                       number they wrote to, which is the whole point of the layer)
--   assigned_to       — which PERSON on the team owns them, for the shared queue
create table if not exists customer_routing (
  tenant_id         text not null,
  phone_number      text not null,
  channel_number_id bigint references channel_number(id) on delete set null,
  assigned_to       text not null default '',        -- operator email, '' = the shared pool
  note              text not null default '',
  updated_by        text not null default '',
  updated_at        timestamptz not null default now(),
  primary key (tenant_id, phone_number)
);
create index if not exists ix_customer_routing_assigned on customer_routing (tenant_id, assigned_to)
  where assigned_to <> '';

-- ── META'S 24-HOUR CLOCK, MADE DURABLE ────────────────────────────────────────────────────────
-- Coexistence onboarding starts a deadline: both history-sync calls must run within 24 hours or the
-- customer is offboarded and has to redo the ENTIRE Embedded Signup flow — the phone taps included.
-- That is not a retry we can paper over afterwards, so the deadline is a ROW written when the
-- connection opens, not a timer in a process that a deploy restarts. The worker sweeps it.
create table if not exists channel_sync_deadline (
  connection_id bigint not null references channel_connection(id) on delete cascade,
  kind          text not null,                      -- coexistence_history
  due_at        timestamptz not null,
  done_at       timestamptz,
  attempts      int not null default 0,
  last_error    text not null default '',
  primary key (connection_id, kind)
);
create index if not exists ix_channel_sync_due on channel_sync_deadline (due_at)
  where done_at is null;

-- ── HISTORY, CAPTURED BEFORE IT IS UNDERSTOOD ──────────────────────────────────────────────────
-- A single `history` webhook can describe THOUSANDS of messages, and Meta says plainly: capture the
-- payload, process asynchronously. So the webhook stores the raw chunk and returns.
--
-- RAW, and deliberately so: the history sync is a ONE-SHOT. Meta will not send it again — re-running
-- means offboarding the customer and putting them back through the whole Embedded Signup flow,
-- taps on their phone included. So the bytes are kept exactly as they arrived, and how we GROUND
-- them into orders and customers can be rewritten as often as we like without asking anyone.
-- Chunks arrive OUT OF ORDER (`chunk_order` is the sequence) and completion is `progress == 100`,
-- never the phase number — Meta's own text contradicts itself on that one.
create table if not exists wa_history_chunk (
  id          bigserial primary key,
  tenant_id   text not null,
  payload     jsonb not null,
  progress    int not null default 0,
  processed_at timestamptz,
  received_at timestamptz not null default now()
);
create index if not exists ix_wa_history_tenant on wa_history_chunk (tenant_id, received_at);
create index if not exists ix_wa_history_unprocessed on wa_history_chunk (tenant_id)
  where processed_at is null;

-- ── CALLING, PER NUMBER (2026-08-17) ───────────────────────────────────────────────────────────
-- WhatsApp calling is not a tenant setting, it is a NUMBER setting, and it is gated on a threshold
-- the number has to climb to: Meta requires a messaging limit of ≥2.000 unique recipients/day
-- (below it, enabling returns error 138015) plus a credit line on the WABA. That gate is per
-- PHONE NUMBER, so every new distributor starts ineligible and becomes eligible with use — which is
-- why this is state we track and re-check rather than a flag somebody ticks.
alter table channel_number add column if not exists calling_status text not null default 'unknown';
alter table channel_number add column if not exists calling_checked_at timestamptz;
alter table channel_number add column if not exists calling_detail jsonb not null default '{}'::jsonb;
