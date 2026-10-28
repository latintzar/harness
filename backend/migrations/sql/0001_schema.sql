create table if not exists whatsapp_messages (
  id text primary key,
  phone_number text not null,
  role text not null,
  message_text text,
  message_type text default 'text',
  media_urls jsonb not null default '[]'::jsonb,
  media_types jsonb not null default '[]'::jsonb,
  wa_message_id text,
  reply_to_wa_id text,
  reply_to_text text,
  transcript text,
  transcript_status text default 'none',
  transcript_meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_wa_msgs_phone_time on whatsapp_messages (phone_number, created_at desc);
create table if not exists whatsapp_user_profiles (
  phone_number text primary key,
  display_name text,
  updated_at timestamptz not null default now()
);
-- Per-customer voice-call toggle. Default FALSE → every existing customer keeps the
-- current Polly + Deepgram-live behaviour until explicitly flipped on for a test number.
alter table whatsapp_user_profiles
  add column if not exists elevenlabs_autoreply_enabled boolean not null default false;
-- When Rocoto auto-answers calls: a list of windows [{days:[0..6 Mon..Sun], start:min, end:min}].
-- Empty = always (when the toggle is on). start>end means the window wraps past midnight.
alter table whatsapp_user_profiles
  add column if not exists auto_windows jsonb not null default '[]'::jsonb;
create table if not exists device_tokens (
  token text primary key,
  platform text default 'android',
  updated_at timestamptz not null default now()
);
-- Live call lifecycle. One row per voice call, keyed to the `call_transcript`
-- whatsapp_messages row (id == msg_id) so it ties cleanly to the conversation log.
-- 'status': active | ended.  'mode': 'eleven' (ConvAI bridge) | 'live' (Polly+Deepgram).
-- 'operator_state': agent | takeover  (agent = EL/Polly is handling; takeover = operator joined).
-- 'lines' is the running live transcript (jsonb array of {speaker,text,start,end}).
create table if not exists calls (
  id text primary key,
  phone_number text not null,
  status text not null default 'active',
  mode text not null default 'live',
  operator_state text not null default 'agent',
  spectatable boolean not null default false,
  call_sid text,
  recording_sid text,
  recording_url text,
  lines jsonb not null default '[]'::jsonb,
  subtitle text default '',
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists idx_calls_status_time on calls (status, started_at desc);
create index if not exists idx_calls_call_sid on calls (call_sid);
-- Phase-A orders. One draft order per order-bearing message; `lines` jsonb holds the
-- ungrounded extracted lines [{raw_text,item,qty,unit,price,note,confidence}].
-- status: needs_review | approved | sent | failed.
create table if not exists orders (
  id text primary key,
  phone_number text not null,
  customer_name text default '',
  source_message_ids jsonb not null default '[]'::jsonb,
  status text not null default 'needs_review',
  lines jsonb not null default '[]'::jsonb,
  note text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  dispatched_at timestamptz,
  delivery_note text,
  delivery_date text
);
create index if not exists idx_orders_status_time on orders (status, created_at desc);
-- Migration for tables created before delivery_date existed (idempotent).
alter table orders add column if not exists delivery_date text;
-- Per-agent config (the Orders agent's dispatch cadence etc.). Single jsonb blob per agent.
create table if not exists agent_settings (
  agent_id text primary key,
  settings jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
-- Landing "See it live" demo leads (see demo_call.py). A visitor requests a call/WhatsApp; we
-- store the number + what discovery learned so Peter can follow up. Purge after 90 days.
create table if not exists demo_leads (
  id text primary key,
  token text unique not null,          -- opaque browser token (never expose call_sid)
  channel text not null default 'call',-- 'call' | 'whatsapp'
  phone text not null,
  locale text default 'es',
  ip text, user_agent text,
  consent boolean not null default false,
  status text not null default 'requested', -- requested|ringing|answered|completed|no_answer|failed|voicemail|sent
  call_sid text,
  name text default '', business text default '', summary text default '',
  answers jsonb not null default '{}'::jsonb,
  cta_text text default '', wa_text text default '',
  lines jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  ended_at timestamptz
);
create index if not exists idx_demo_leads_created on demo_leads (created_at desc);
create index if not exists idx_demo_leads_phone on demo_leads (phone, created_at desc);
