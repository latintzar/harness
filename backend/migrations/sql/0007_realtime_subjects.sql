-- Explicit source-backed identities; canonical company/catalog authorities stay intact.
create table realtime.subject (
 tenant_id text not null,
 source_id text not null,
 subject_id text not null check(length(subject_id) between 1 and 256),
 kind text not null check(kind in ('camera','zone','visit','observed_item','catalog_product')),
 namespace text not null check(length(namespace) between 1 and 256),
 identity_digest text not null check(identity_digest ~ '^[a-f0-9]{64}$'),
 parent_subject_id text,
 original_source_id text,
 original_subject_id text,
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 created_at timestamptz not null default now(),
 primary key(tenant_id,subject_id),
 unique(tenant_id,source_id,subject_id),
 unique(tenant_id,identity_digest),
 foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id),
 foreign key(tenant_id,source_id,parent_subject_id) references realtime.subject(tenant_id,source_id,subject_id),
 foreign key(tenant_id,original_source_id,original_subject_id) references realtime.subject(tenant_id,source_id,subject_id),
 check((original_source_id is null)=(original_subject_id is null))
);
create table realtime.subject_revision (
 tenant_id text not null,
 source_id text not null,
 subject_id text not null,
 revision integer not null check(revision between 1 and 10000),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 status text not null check(status in ('active','closed','retracted')),
 attributes jsonb not null check(octet_length(attributes::text)<=8192),
 provenance jsonb not null check(octet_length(provenance::text)<=4096),
 replay_context jsonb check(replay_context is null or octet_length(replay_context::text)<=8192),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 created_at timestamptz not null default now(),
 primary key(tenant_id,subject_id,revision),
 foreign key(tenant_id,source_id,subject_id) references realtime.subject(tenant_id,source_id,subject_id)
);
create table realtime.event_subject_set (
 binding_count integer not null check(binding_count between 0 and 32),
 tenant_id text not null,
 source_id text not null,
 event_id text not null,
 event_revision integer not null,
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 created_at timestamptz not null default now(),
 primary key(tenant_id,event_id,event_revision),
 unique(tenant_id,source_id,event_id,event_revision),
 foreign key(tenant_id,source_id,event_id,event_revision) references realtime.event(tenant_id,source_id,event_id,event_revision)
);
create table realtime.event_subject_binding (
 tenant_id text not null,
 source_id text not null,
 event_id text not null,
 event_revision integer not null,
 role text not null check(role in ('camera','zone','actor','recipient','item','product_candidate','product_confirmed')),
 subject_id text not null,
 subject_revision integer not null,
 primary key(tenant_id,event_id,event_revision,role,subject_id),
 foreign key(tenant_id,source_id,event_id,event_revision) references realtime.event_subject_set(tenant_id,source_id,event_id,event_revision),
 foreign key(tenant_id,subject_id,subject_revision) references realtime.subject_revision(tenant_id,subject_id,revision)
);
create index realtime_subject_source on realtime.subject(tenant_id,source_id,kind,subject_id);
create index realtime_subject_event on realtime.event_subject_binding(tenant_id,subject_id,event_id,event_revision);

create function realtime.subject_visible(company text,subject text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select exists(select 1 from realtime.subject s where s.tenant_id=company and s.subject_id=subject
   and realtime.source_visible(company,s.source_id)
   and (s.original_source_id is null or realtime.source_visible(company,s.original_source_id)))
$$;
revoke all on function realtime.subject_visible(text,text) from public;
grant execute on function realtime.subject_visible(text,text) to rocoto_rt;

-- Existing catalog IDs keep their tenant association; this grants no catalog listing.
create function realtime.catalogue_subject_visible(company text,sku text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.company_visible(company) and exists(
   select 1 from public.catalogue_skus p where p.tenant_id=company and p.id=sku)
$$;
revoke all on function realtime.catalogue_subject_visible(text,text) from public;
grant execute on function realtime.catalogue_subject_visible(text,text) to rocoto_rt;

do $$ declare tab text; begin
 foreach tab in array array['subject','subject_revision','event_subject_set','event_subject_binding'] loop
  execute format('alter table realtime.%I enable row level security',tab);
  execute format('alter table realtime.%I force row level security',tab);
  execute format('create policy migration_read on realtime.%I for select to %I using(true)',tab,current_user);
  execute format('grant select,insert on realtime.%I to rocoto_rt',tab);
 end loop;
end $$;
create policy subject_read on realtime.subject for select using(realtime.subject_visible(tenant_id,subject_id));
create policy subject_revision_read on realtime.subject_revision for select using(realtime.subject_visible(tenant_id,subject_id));
create policy subject_insert on realtime.subject for insert with check(
 realtime.source_writable(tenant_id,source_id)
 and realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true)) && array['realtime:ingest','realtime:configure']
 and (original_source_id is null or realtime.source_visible(tenant_id,original_source_id)));
create policy subject_revision_insert on realtime.subject_revision for insert with check(
 realtime.source_writable(tenant_id,source_id) and realtime.subject_visible(tenant_id,subject_id)
 and realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true)) && array['realtime:ingest','realtime:configure']);
create policy binding_set_read on realtime.event_subject_set for select using(realtime.source_visible(tenant_id,source_id));
create policy binding_read on realtime.event_subject_binding for select using(
 realtime.source_visible(tenant_id,source_id) and realtime.subject_visible(tenant_id,subject_id));
create policy binding_set_insert on realtime.event_subject_set for insert with check(
 realtime.source_writable(tenant_id,source_id)
 and 'realtime:ingest'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));
create policy binding_insert on realtime.event_subject_binding for insert with check(
 realtime.source_writable(tenant_id,source_id) and realtime.subject_visible(tenant_id,subject_id)
 and 'realtime:ingest'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));

create function realtime.subject_usage(company text) returns table(revision_count bigint,binding_count bigint,retained_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with s as(select count(*) n from realtime.subject where tenant_id=company),
 r as(select count(*) n,coalesce(sum(octet_length(attributes::text)+octet_length(provenance::text)+coalesce(octet_length(replay_context::text),0)+512),0)::bigint b from realtime.subject_revision where tenant_id=company),
 b as(select count(*) n from realtime.event_subject_binding where tenant_id=company),
 e as(select count(*) n from realtime.event_subject_set where tenant_id=company)
 select r.n,b.n,r.b+s.n*1024+b.n*512+e.n*512 from s,r,b,e where realtime.company_visible(company)
$$;
revoke all on function realtime.subject_usage(text) from public;
grant execute on function realtime.subject_usage(text) to rocoto_rt;
alter function realtime.retained_usage(text) rename to retained_usage_replay;
revoke all on function realtime.retained_usage_replay(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+s.retained_bytes,
 b.source_count,b.profile_count from realtime.retained_usage_replay(company) b cross join realtime.subject_usage(company) s
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
