-- Additive foundation for Rocoto Real-time. public.tenants remains the company authority.
create schema realtime;
revoke all on schema realtime from public;
grant usage on schema realtime to rocoto_rt, rocoto_ctrl;

create table realtime.company_binding (
  tenant_id text primary key references public.tenants(slug),
  scope ltree not null unique,
  identity_digest text not null check(identity_digest ~ '^[0-9a-f]{64}$'),
  import_reference text not null,
  max_events integer not null default 50000 check(max_events between 1 and 1000000),
  max_outbox integer not null default 10000 check(max_outbox between 1 and 100000),
  created_at timestamptz not null default now(),
  check(nlevel(scope)=2 and subpath(scope,0,1)='t'::ltree)
);
create table realtime.environment_alias (
  tenant_id text not null references realtime.company_binding(tenant_id),
  alias text not null,
  primary key(tenant_id,alias)
);
create table realtime.service_actor (
  tenant_id text not null references realtime.company_binding(tenant_id),
  actor_id text not null,
  capabilities text[] not null,
  active boolean not null default true,
  primary key(tenant_id,actor_id)
);
create table realtime.source (
  tenant_id text not null references realtime.company_binding(tenant_id),
  source_id text not null,
  kind text not null check(kind in ('camera','connector','processing','replay')),
  metadata jsonb not null default '{}'::jsonb check(octet_length(metadata::text)<=65536),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  primary key(tenant_id,source_id)
);
create table realtime.resource_grant (
  tenant_id text not null,
  principal_id text not null,
  source_id text not null,
  can_ingest boolean not null default false,
  can_process boolean not null default false,
  primary key(tenant_id,principal_id,source_id),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create table realtime.profile_revision (
  tenant_id text not null,
  source_id text not null,
  revision_id text not null,
  event_types text[] not null check(cardinality(event_types) between 1 and 256),
  created_at timestamptz not null default now(),
  primary key(tenant_id,source_id,revision_id),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create table realtime.input_receipt (
  tenant_id text not null,
  source_id text not null,
  receipt_id text not null,
  digest text not null check(digest ~ '^[0-9a-f]{64}$'),
  body jsonb not null check(octet_length(body::text)<=262144),
  created_at timestamptz not null default now(),
  primary key(tenant_id,source_id,receipt_id),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create table realtime.event (
  tenant_id text not null,
  source_id text not null,
  event_id text not null,
  event_revision integer not null check(event_revision>0),
  sequence bigint generated always as identity,
  event_type text not null,
  occurred_at timestamptz,
  external_identity text,
  replay_context jsonb check(replay_context is null or octet_length(replay_context::text)<=80000),
  digest text not null check(digest ~ '^[0-9a-f]{64}$'),
  envelope jsonb not null check(octet_length(envelope::text)<=300000),
  committed_at timestamptz not null default now(),
  primary key(tenant_id,event_id,event_revision),
  unique(tenant_id,sequence),
  unique(tenant_id,source_id,external_identity,event_revision),
  unique(tenant_id,source_id,event_id,event_revision),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create index realtime_event_source_clock on realtime.event(tenant_id,source_id,occurred_at,sequence);
create table realtime.outbox (
  tenant_id text not null,
  source_id text not null,
  outbox_id bigint generated always as identity,
  event_id text not null,
  event_revision integer not null,
  lane text not null default 'live' check(lane in ('live','replay')),
  state text not null default 'pending' check(state in ('pending','leased','complete','failed')),
  attempts integer not null default 0,
  lease_token text,
  lease_until timestamptz,
  available_at timestamptz not null default now(),
  completed_at timestamptz,
  primary key(tenant_id,outbox_id),
  unique(tenant_id,event_id,event_revision),
  foreign key(tenant_id,source_id,event_id,event_revision) references realtime.event(tenant_id,source_id,event_id,event_revision),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create index realtime_outbox_due on realtime.outbox(tenant_id,available_at,outbox_id)
  where state in ('pending','leased');
create table realtime.quarantine (
  tenant_id text not null,
  source_id text not null,
  digest text not null,
  reason text not null,
  envelope jsonb not null check(octet_length(envelope::text)<=300000),
  created_at timestamptz not null default now(),
  primary key(tenant_id,source_id,digest),
  foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);

-- A narrow membership lookup avoids broad runtime SELECT grants on the legacy registry.
-- The principal is selected by the authenticated host, never by a request body.
create function realtime.actor_capabilities(company text, actor text) returns text[]
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select coalesce((
   select case when m.role in ('owner','founder') then
       array['realtime:read','realtime:configure','realtime:ingest','realtime:process']
     when m.role in ('operator','browse') then array['realtime:read']
     else array[]::text[] end
   from public.team_members m join public.operators o on o.id=m.operator_id
   where m.tenant_slug=company and m.operator_id=actor
 ), (select capabilities from realtime.service_actor where tenant_id=company and actor_id=actor and active), array[]::text[])
$$;
revoke all on function realtime.actor_capabilities(text,text) from public;
grant execute on function realtime.actor_capabilities(text,text) to rocoto_rt,rocoto_ctrl;

-- The helper is owned by the migration owner; workers cannot edit bindings or memberships.
create function realtime.company_visible(company text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select exists(select 1 from realtime.company_binding b where b.tenant_id=company
   and b.scope=nullif(current_setting('rocoto.scope',true),'')::ltree
   and realtime.actor_capabilities(company,current_setting('rocoto.principal',true)) &&
     array['realtime:read','realtime:configure','realtime:ingest','realtime:process'])
$$;
revoke all on function realtime.company_visible(text) from public;
grant execute on function realtime.company_visible(text) to rocoto_rt,rocoto_ctrl;

create function realtime.source_visible(company text, source text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.company_visible(company) and exists(
   select 1 from realtime.source s where s.tenant_id=company and s.source_id=source and s.active
   and (
     'realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true)))
     or exists(select 1 from realtime.resource_grant g where g.tenant_id=company
         and g.source_id=source and g.principal_id=current_setting('rocoto.principal',true)))
   and (s.kind!='replay' or exists(
     select 1 from realtime.source original where original.tenant_id=company
       and original.source_id=s.metadata->>'original_source_id' and original.kind='camera' and original.active
       and (
         'realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true)))
         or exists(select 1 from realtime.resource_grant g where g.tenant_id=company
             and g.source_id=original.source_id and g.principal_id=current_setting('rocoto.principal',true)))
   )))
$$;
revoke all on function realtime.source_visible(text,text) from public;
grant execute on function realtime.source_visible(text,text) to rocoto_rt,rocoto_ctrl;

create function realtime.source_writable(company text, source text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.source_visible(company,source) and (
   'realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true)))
   or exists(select 1 from realtime.resource_grant g where g.tenant_id=company
       and g.source_id=source and g.principal_id=current_setting('rocoto.principal',true) and g.can_ingest))
$$;
revoke all on function realtime.source_writable(text,text) from public;
grant execute on function realtime.source_writable(text,text) to rocoto_rt;

create function realtime.source_processable(company text, source text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.source_visible(company,source) and (
   'realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true)))
   or exists(select 1 from realtime.resource_grant g where g.tenant_id=company
       and g.source_id=source and g.principal_id=current_setting('rocoto.principal',true) and g.can_process))
$$;
revoke all on function realtime.source_processable(text,text) from public;
grant execute on function realtime.source_processable(text,text) to rocoto_rt;

-- Company-scoped read access; only explicit provisioning can mutate security tables.
do $$ declare tab text; begin
 foreach tab in array array['company_binding','environment_alias','service_actor','resource_grant'] loop
   execute format('alter table realtime.%I enable row level security',tab);
   execute format('alter table realtime.%I force row level security',tab);
   execute format('create policy company_scope on realtime.%I for select using (realtime.company_visible(tenant_id))',tab);
   execute format('grant select on realtime.%I to rocoto_rt',tab);
   execute format('create policy migration_authority on realtime.%I to %I using (true) with check (true)',tab,current_user);
 end loop;
end $$;
-- The locked binding serializes per-company admission and its retained-row budgets.
grant update (max_events) on realtime.company_binding to rocoto_rt;
create policy binding_lock on realtime.company_binding for update
  using(realtime.company_visible(tenant_id)) with check(false);

do $$ declare tab text; begin
 foreach tab in array array['source','profile_revision','input_receipt','event','outbox','quarantine'] loop
   execute format('alter table realtime.%I enable row level security',tab);
   execute format('alter table realtime.%I force row level security',tab);
   execute format('create policy source_scope on realtime.%I for select using (realtime.source_visible(tenant_id,source_id))',tab);
   execute format('grant select on realtime.%I to rocoto_rt',tab);
 end loop;
end $$;
create policy source_insert on realtime.source for insert with check(
 realtime.company_visible(tenant_id) and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));
create policy profile_insert on realtime.profile_revision for insert with check(
 realtime.source_visible(tenant_id,source_id) and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));
grant insert on realtime.source,realtime.profile_revision to rocoto_rt;

do $$ declare tab text; begin
 foreach tab in array array['input_receipt','event','outbox','quarantine'] loop
   execute format('create policy ingest_insert on realtime.%I for insert with check (realtime.source_writable(tenant_id,source_id) and ''realtime:ingest''=any(realtime.actor_capabilities(tenant_id,current_setting(''rocoto.principal'',true))))',tab);
   execute format('grant insert on realtime.%I to rocoto_rt',tab);
 end loop;
end $$;
create policy outbox_update on realtime.outbox for update using(
 realtime.source_processable(tenant_id,source_id) and 'realtime:process'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))))
 with check(realtime.source_visible(tenant_id,source_id));
grant update(state,attempts,lease_token,lease_until,available_at,completed_at) on realtime.outbox to rocoto_rt;
grant usage,select on all sequences in schema realtime to rocoto_rt;

-- Budget reads count the whole company, including sources a given worker cannot read.
-- The fixed migration-owner SELECT policy makes the helper work with FORCE RLS even
-- when the migration owner is a non-superuser. Runtime roles cannot assume that role.
do $$ declare tab text; begin
 foreach tab in array array['source','profile_revision','input_receipt','event','outbox','quarantine'] loop
   execute format('create policy migration_read on realtime.%I for select to %I using (true)',tab,current_user);
 end loop;
end $$;
alter table realtime.company_binding add column max_bytes bigint not null default 134217728
  check(max_bytes between 1048576 and 10737418240);
create function realtime.retained_usage(company text) returns table(
 event_count bigint, receipt_count bigint, pending_count bigint, quarantine_count bigint, retained_bytes bigint,
 source_count bigint, profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with e as(select count(*) n,coalesce(sum(octet_length(envelope::text)+coalesce(octet_length(replay_context::text),0)+1024),0)::bigint b from realtime.event where tenant_id=company),
 r as(select count(*) n,coalesce(sum(octet_length(body::text)+512),0)::bigint b from realtime.input_receipt where tenant_id=company),
 q as(select count(*) n,coalesce(sum(octet_length(envelope::text)+512),0)::bigint b from realtime.quarantine where tenant_id=company),
 w as(select count(*) n from realtime.outbox where tenant_id=company and state!='complete'),
 s as(select count(*) n,coalesce(sum(octet_length(metadata::text)+512),0)::bigint b from realtime.source where tenant_id=company),
 p as(select count(*) n,coalesce(sum(octet_length(event_types::text)+512),0)::bigint b from realtime.profile_revision where tenant_id=company)
 select e.n,r.n,w.n,q.n,e.b+r.b+q.b+s.b+p.b,s.n,p.n from e,r,q,w,s,p where realtime.company_visible(company)
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;


-- Immutable records retain the authenticated host operation that admitted them.
do $$ declare tab text; begin
 foreach tab in array array['source','profile_revision','input_receipt','event','quarantine'] loop
   execute format('alter table realtime.%I add column principal_id text not null default current_setting(''rocoto.principal'',true)',tab);
   execute format('alter table realtime.%I add column request_id text not null default current_setting(''rocoto.request_id'',true)',tab);
 end loop;
end $$;
