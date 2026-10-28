create table if not exists pb_account (
  id           bigserial primary key,
  name         text not null,
  contact      text not null default '',
  role         text not null default '',
  stage        text not null default 'contacted',
  next_action  text not null default '',
  next_date    date,
  last_touch   timestamptz,
  volumes      jsonb not null default '{}'::jsonb,   -- the calculator's INPUTS (screen 2)
  meddpicc     jsonb not null default '{}'::jsonb,   -- the eight qualification fields (screen 4)
  map_rows     jsonb not null default '[]'::jsonb,   -- the mutual action plan: [{date,who,what}]
  archived     boolean not null default false,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
-- the list is sorted by what is due next, and a row with no date sorts last rather than first
create index if not exists idx_pb_account_next on pb_account (archived, next_date nulls last, id);

create table if not exists pb_note (
  id          bigserial primary key,
  account_id  bigint not null references pb_account(id) on delete cascade,
  body        text not null,
  author      text not null default '',
  created_at  timestamptz not null default now()
);
create index if not exists idx_pb_note_acct on pb_note (account_id, created_at desc);
