-- Internal support grants are explicit actor/environment/account capabilities. Empty at install.
create table operator_access (
  operator_id text not null references operators(id) on delete cascade,
  environment text not null check (environment in ('development','staging','production')),
  tenant_slug text not null references tenants(slug) on delete cascade,
  capability text not null check (capability in ('accounts.read','accounts.edit','sessions.revoke','audit.read')),
  granted_by text not null,
  reason text not null check (length(reason) between 3 and 500),
  granted_at timestamptz not null default now(),
  expires_at timestamptz not null,
  primary key (operator_id, environment, tenant_slug, capability),
  check (expires_at > granted_at)
);

create table operator_audit (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  actor_email text not null,
  session_id text not null,
  environment text not null,
  tenant_slug text not null,
  action text not null,
  target_id text not null,
  outcome text not null,
  request_id text not null,
  detail jsonb not null default '{}'::jsonb
);
create index operator_audit_target on operator_audit(environment,tenant_slug,id desc);
create trigger operator_audit_no_mutate before update or delete on operator_audit
  for each row execute function sup_audit_immutable();

-- Exact request identity makes retries repeatable; successful audit and mutation share a transaction.
create table operator_request (
  actor_email text not null,
  environment text not null,
  tenant_slug text not null,
  request_id text not null,
  fingerprint text not null,
  status integer not null,
  response jsonb not null,
  created_at timestamptz not null default now(),
  primary key(actor_email,environment,tenant_slug,request_id)
);

-- Product adapters may attach a safe receipt summary after verifying native resource ownership.
-- This table is a pointer to the company lifecycle, not a second execution engine.
create table operator_synthetic_run (
  run_id text primary key,
  tenant_slug text not null references tenants(slug) on delete restrict,
  environment text not null check(environment in ('development','staging','production')),
  scenario_id text not null,
  source_sha256 text not null check(source_sha256 ~ '^[a-f0-9]{64}$'),
  phase text not null check(phase in ('created','populated','verified','failed','deleted')),
  effects text not null check(effects in ('capture','allowlisted')),
  updated_at timestamptz not null default now()
);
revoke all on operator_access,operator_audit,operator_request,operator_synthetic_run from public;
