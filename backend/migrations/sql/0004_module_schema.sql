-- Source: portal.py / _SCHEMA
create table if not exists portal_tokens (
  token         text primary key,
  tenant_id     text not null,
  phone_number  text not null,
  status        text not null default 'active',   -- active | revoked
  created_at    timestamptz not null default now(),
  last_used_at  timestamptz,
  expires_at    timestamptz not null,
  uses          int not null default 0,
  seeded        boolean not null default false
);
-- ONE active link per customer per tenant — the stable-bookmark + one-thing-to-revoke property.
create unique index if not exists ux_portal_token_active
  on portal_tokens (tenant_id, phone_number) where status = 'active';
create index if not exists idx_portal_tokens_customer
  on portal_tokens (tenant_id, phone_number, created_at desc);

-- Source: consent.py / CONSENT_SCHEMA
create table if not exists consent_events (
  id             bigserial primary key,
  operator_email text        not null,
  kind           text        not null,
  granted        boolean     not null,
  text_shown     text        not null default '',
  text_version   text        not null default '',
  ip             text,
  user_agent     text,
  method         text        not null default '',
  created_at     timestamptz not null default now()
);
create index if not exists consent_events_lookup
  on consent_events(lower(operator_email), kind, id desc);
-- Every column explicitly ALTERed as well as declared: the table is created lazily on first use, so
-- an EDIT to the create-block above would be a silent no-op wherever the table already exists —
-- exactly the drift DEPLOY-RUNBOOK.md's migration LAW exists to kill. Adding a column later means
-- adding a line here, not editing the block.
alter table consent_events add column if not exists text_shown   text not null default '';
alter table consent_events add column if not exists text_version text not null default '';
alter table consent_events add column if not exists ip           text;
alter table consent_events add column if not exists user_agent   text;
alter table consent_events add column if not exists method       text not null default '';
-- APPEND-ONLY AT THE DB LEVEL (the cr_audit/sup_audit discipline): the trigger fires for EVERY role
-- including the owner the backend connects as, so «a withdrawal writes a new row, never edits the
-- old one» is enforced by Postgres rather than by everyone remembering it.
create or replace function consent_events_immutable() returns trigger language plpgsql as $ce$
begin
  raise exception 'consent_events is append-only (% blocked)', tg_op;
end $ce$;
drop trigger if exists consent_events_no_mutate on consent_events;
create trigger consent_events_no_mutate before update or delete on consent_events
  for each row execute function consent_events_immutable();

-- Source: api_v1.py / DDL
create table if not exists api_key (
  id           bigserial primary key,
  tenant_id    text        not null,
  label        text,
  prefix       text        not null,
  key_hash     text        not null unique,
  caps         text[]      not null default '{read}',
  created_by   text,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at   timestamptz
);
create index if not exists api_key_tenant_idx on api_key (tenant_id) where revoked_at is null;

-- Source: directory.py / SCHEMA
create table if not exists catalogue_directory (
  tenant_id    text primary key,
  body         text not null,
  alias_map    jsonb not null default '{}'::jsonb,
  sku_hash     text not null,
  n_skus       int not null default 0,
  generated_at timestamptz not null default now()
);
alter table catalogue_directory add column if not exists group_map       jsonb not null default '{}'::jsonb;
alter table catalogue_directory add column if not exists tier            text;
alter table catalogue_directory add column if not exists names_complete  boolean;
alter table catalogue_directory add column if not exists prices_complete boolean;
alter table catalogue_directory add column if not exists budget_bytes    int;

-- Source: demo_phone.py / PHONE_SCHEMA
create table if not exists demo_phone_link (
  sid          text primary key,
  nonce        text not null,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  hard_until   timestamptz not null,
  claim        text,
  claimed_at   timestamptz,
  phone_seen   timestamptz,
  desk_seen    timestamptz,
  revoked      boolean not null default false
);
alter table demo_phone_link add column if not exists hard_until timestamptz;
alter table demo_phone_link add column if not exists claim      text;
alter table demo_phone_link add column if not exists claimed_at timestamptz;
alter table demo_phone_link add column if not exists phone_seen timestamptz;
alter table demo_phone_link add column if not exists desk_seen  timestamptz;
alter table demo_phone_link add column if not exists revoked    boolean not null default false;

-- Source: demo_auth.py / MAGIC_SCHEMA
create table if not exists demo_magic_links (
  token_hash   text primary key,
  email        text not null,
  ip           text,
  user_agent   text,
  consent      jsonb not null default '{}'::jsonb,
  text_version text not null default '',
  lang         text not null default 'es',
  company      text not null default '',
  demo_token   text,                       -- VESTIGIAL: the anonymous session it named is gone
  nonce_hash   text,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  consumed_at  timestamptz
);
create index if not exists demo_magic_links_email on demo_magic_links(lower(email), created_at desc);
alter table demo_magic_links add column if not exists user_agent   text;
alter table demo_magic_links add column if not exists consent      jsonb not null default '{}'::jsonb;
alter table demo_magic_links add column if not exists text_version text not null default '';
alter table demo_magic_links add column if not exists lang         text not null default 'es';
alter table demo_magic_links add column if not exists company      text not null default '';
alter table demo_magic_links add column if not exists demo_token   text;
-- §F3: only the HASH of the requester nonce, for the same reason only the hash of the token is
-- stored — a database dump must not be a pile of working sign-ins.
alter table demo_magic_links add column if not exists nonce_hash   text;

-- Source: demo_flow.py / DEMO_SCHEMA
create table if not exists demo_sessions (
  token         text primary key,
  sid           text unique not null,
  email         text,
  persona       jsonb not null default '{}'::jsonb,
  intake_id     text,
  catalog_ready boolean not null default false,
  turns         int not null default 0,
  closed        boolean not null default false,
  referrer      text,
  created_at    timestamptz default now(),
  last_seen     timestamptz default now(),
  expires_at    timestamptz
);
-- §17.56c — IDENTITY MOVED OUT OF THE TOKEN. `operator_email` + `tenant_slug` are what a session is
-- keyed to, and since 2026-08-09 BOTH ARE REQUIRED: a row with either one null is a legacy anonymous
-- session, refused at every door and dropped by `reap()`. The token survives as the flow's handle —
-- it names WHICH session, the cookie says WHOSE. Explicit ALTERs, not an edit to the create-block
-- above: this table is
-- created lazily, so editing the block is a silent no-op wherever it already exists — the exact
-- drift DEPLOY-RUNBOOK.md's migration LAW exists to kill.
alter table demo_sessions add column if not exists operator_email text;
alter table demo_sessions add column if not exists tenant_slug    text;
create index if not exists demo_sessions_operator
  on demo_sessions(lower(operator_email), last_seen desc);

-- Source: join.py / JOIN_SCHEMA
create table if not exists join_applicants (
  id          text primary key,
  created     timestamptz not null default now(),
  lang        text not null default 'en',
  name        text default '',
  link        text default '',
  why         text default '',
  location    text default '',
  email       text default '',
  transcript  jsonb not null default '[]'::jsonb,
  ip_hash     text default '',
  ua          text default '',
  dover_id    text,
  dover_error text
);
create index if not exists idx_join_applicants_created on join_applicants (created desc);
alter table join_applicants add column if not exists github   text default '';
alter table join_applicants add column if not exists linkedin text default '';
alter table join_applicants add column if not exists other    text default '';
alter table join_applicants add column if not exists phone    text default '';
alter table join_applicants add column if not exists voices   jsonb not null default '{}'::jsonb;

-- Source: demo_media.py / MEDIA_SCHEMA
create table if not exists demo_media (
  id          text primary key,
  sid         text not null,
  client_id   text not null,
  kind        text not null,
  origin      text not null default 'desk',
  state       text not null default 'recibida',
  mime        text not null default '',
  bytes       int  not null default 0,
  seconds     real,
  text        text not null default '',
  confidence  jsonb,
  result      jsonb,
  error       text not null default '',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
-- IDEMPOTENCY IS THIS INDEX, not a check-then-insert. §17.20: «every image carries a client-generated
-- client_id, so retries are idempotent — a phone that dies mid-POST and retries can never duplicate a
-- file». A check-then-insert loses that race the one time it matters (a flaky phone retrying fast).
create unique index if not exists demo_media_client on demo_media(sid, client_id);
create index if not exists demo_media_sid on demo_media(sid, created_at desc);
-- Explicit ALTERs, never an edit to the create-block: the table is created lazily, so editing the
-- block above is a silent no-op wherever it already exists (DEPLOY-RUNBOOK.md's migration LAW).
alter table demo_media add column if not exists origin     text  not null default 'desk';
alter table demo_media add column if not exists seconds    real;
alter table demo_media add column if not exists confidence jsonb;
alter table demo_media add column if not exists result     jsonb;
alter table demo_media add column if not exists error      text  not null default '';
alter table demo_media add column if not exists updated_at timestamptz not null default now();

-- Source: claim.py / CLAIM_SCHEMA
create table if not exists account_claim (
  token_hash   text primary key,             -- only the HASH: a database dump is not a pile of
                                             --  working claims (demo_magic_links' own law)
  address      text not null,                -- the ONE address this link opens. §2.1 property 5.
  house        text not null default '',     -- the house's NAME, as Peter typed it on the mint
  minted_by    text not null default '',     -- the founder who sent it — «Invited by Peter»
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  nonce_hash   text,                         -- set when the claim PAGE is opened, matched on POST
  opened_at    timestamptz,                  -- «sent 2 days ago · not opened yet»
  consumed_at  timestamptz,                  -- the single-use gate, written by the UPDATE itself
  claimed_ip   text,                         -- ── the audited row (§2.1 property 7) ──
  claimed_ua   text,
  tenant_slug  text,                         -- the house this claim actually opened
  revoked_at   timestamptz                   -- set = the link is dead THIS INSTANT
);
create index if not exists ix_account_claim_address on account_claim (lower(address), created_at desc);
-- one LIVE claim per address: re-minting supersedes rather than accumulating links that all work.
create unique index if not exists ix_account_claim_live on account_claim (lower(address))
  where consumed_at is null and revoked_at is null;

-- Source: brain/onboarding.py / INTAKE_SCHEMA
create table if not exists catalogue_intake (
  id           text primary key,
  tenant_id    text not null,
  created_by   text not null default '',
  source_kind  text not null check (source_kind in ('file','paste')),
  source_name  text not null default '',
  source_rows  int,
  raw_ref      text,
  state        text not null default 'parsing'
               check (state in ('parsing','ready','failed','committed','discarded')),
  parsed       jsonb not null default '[]'::jsonb,
  edits        jsonb not null default '{}'::jsonb,
  warnings     jsonb not null default '[]'::jsonb,
  colmap       jsonb,
  destination  text,
  committed_at timestamptz,
  result       jsonb,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null default now() + interval '7 days'
);
create index if not exists idx_catalogue_intake_tenant on catalogue_intake (tenant_id, created_at desc);
-- §17.56: the column-confirmation payload skuparse now surfaces on tabular sources. Its OWN column
-- (not folded into `parsed`) because `parsed` is a jsonb ARRAY that list_intakes counts with
-- jsonb_array_length — wrapping it in an object to smuggle a sibling in would break every reader
-- for the sake of avoiding one additive migration. Null = non-tabular / pre-existing rows.
alter table catalogue_intake add column if not exists colmap jsonb;

-- Source: brain/onboarding.py / INTAKE_LINK_SCHEMA
create table if not exists intake_links (
  token        text primary key,
  tenant_id    text not null,
  minted_by    text not null default '',
  label        text not null default '',
  sent_to      text not null default '',
  status       text not null default 'active',    -- active | revoked
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  expires_at   timestamptz not null,
  uses         int not null default 0,
  intakes      jsonb not null default '[]'::jsonb
);
-- ONE active link per founder per tenant — the idempotent-«mándamelo» + one-thing-to-revoke property.
create unique index if not exists ux_intake_link_active
  on intake_links (tenant_id, minted_by) where status = 'active';

-- Source: brain/seed.py / _ensure_schema
create table if not exists seed_profile (
                     id         text primary key,
                     name       text not null default '',
                     body       jsonb not null default '{}'::jsonb,
                     created_at timestamptz not null default now(),
                     updated_at timestamptz not null default now()
                   );

-- Source: brain/seed.py / _ensure_schema
alter table tenant_range add column if not exists seeded boolean not null default false;
