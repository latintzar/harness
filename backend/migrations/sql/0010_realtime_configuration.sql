-- Typed requests and actual application are separate, source-scoped receipts.
create table realtime.configuration_proposal (
 tenant_id text not null, source_id text not null, proposal_id text not null,
 expected_revision integer not null check(expected_revision>=1),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=65536),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 conversation_id text not null,
 primary key(tenant_id,source_id,proposal_id),
 foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create table realtime.configuration_revision (
 tenant_id text not null, source_id text not null, revision integer not null check(revision>=2),
 proposal_id text not null, profile_revision_id text not null,
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=131072),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 conversation_id text not null,
 primary key(tenant_id,source_id,revision),
 unique(tenant_id,source_id,proposal_id),
 foreign key(tenant_id,source_id,proposal_id) references realtime.configuration_proposal(tenant_id,source_id,proposal_id),
 foreign key(tenant_id,source_id,profile_revision_id) references realtime.profile_revision(tenant_id,source_id,revision_id)
);
create table realtime.configuration_head (
 tenant_id text not null, source_id text not null, revision integer not null,
 desired_profile_id text not null,
 public_body jsonb not null check(octet_length(public_body::text)<=65536),
 primary key(tenant_id,source_id),
 foreign key(tenant_id,source_id,revision) references realtime.configuration_revision(tenant_id,source_id,revision),
 foreign key(tenant_id,source_id,desired_profile_id) references realtime.profile_revision(tenant_id,source_id,revision_id)
);
create table realtime.configuration_ack (
 tenant_id text not null, source_id text not null, ack_id text not null,
 revision integer not null, profile_revision_id text not null, receipt_id text not null,
 clock_kind text not null check(clock_kind in ('asset','epoch')), clock_id text not null,
 boundary_s numeric not null check(boundary_s>=0),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 conversation_id text not null,
 primary key(tenant_id,source_id,ack_id),
 unique(tenant_id,source_id,clock_kind,clock_id,boundary_s),
 foreign key(tenant_id,source_id,revision) references realtime.configuration_revision(tenant_id,source_id,revision),
 foreign key(tenant_id,source_id,profile_revision_id) references realtime.profile_revision(tenant_id,source_id,revision_id),
 foreign key(tenant_id,source_id,receipt_id) references realtime.input_receipt(tenant_id,source_id,receipt_id)
);
create index realtime_configuration_ack_clock on realtime.configuration_ack(tenant_id,source_id,clock_kind,clock_id,boundary_s);

create function realtime.configuration_worker(company text, source text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.source_visible(company,source) and exists(
  select 1 from realtime.service_actor a join realtime.resource_grant g
   on g.tenant_id=a.tenant_id and g.principal_id=a.actor_id
  where a.tenant_id=company and a.actor_id=current_setting('rocoto.principal',true)
   and a.active and 'realtime:acknowledge'=any(a.capabilities)
   and 'realtime:ingest'=any(a.capabilities) and g.source_id=source and g.can_ingest)
$$;
revoke all on function realtime.configuration_worker(text,text) from public;
grant execute on function realtime.configuration_worker(text,text) to rocoto_rt;

do $$ declare tab text; begin
 foreach tab in array array['configuration_proposal','configuration_revision','configuration_head','configuration_ack'] loop
  execute format('alter table realtime.%I enable row level security',tab);
  execute format('alter table realtime.%I force row level security',tab);
  execute format('create policy migration_read on realtime.%I for select to %I using(true)',tab,current_user);
  -- Read grants on revisions are private: ordinary readers receive a whitelisted view through the SDK.
  if tab in ('configuration_proposal','configuration_revision') then
   execute format('create policy scoped_read on realtime.%I for select using(realtime.source_visible(tenant_id,source_id) and (''realtime:configure''=any(realtime.actor_capabilities(tenant_id,current_setting(''rocoto.principal'',true))) or realtime.configuration_worker(tenant_id,source_id)))',tab);
  else
   execute format('create policy scoped_read on realtime.%I for select using(realtime.source_visible(tenant_id,source_id))',tab);
  end if;
  if tab='configuration_ack' then
   execute format('create policy scoped_insert on realtime.%I for insert with check(realtime.configuration_worker(tenant_id,source_id))',tab);
  else
   execute format('create policy scoped_insert on realtime.%I for insert with check(realtime.source_visible(tenant_id,source_id) and ''realtime:configure''=any(realtime.actor_capabilities(tenant_id,current_setting(''rocoto.principal'',true))))',tab);
  end if;
  execute format('grant select,insert on realtime.%I to rocoto_rt',tab);
 end loop;
end $$;
create policy scoped_update on realtime.configuration_head for update
 using(realtime.source_visible(tenant_id,source_id) and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))))
 with check(realtime.source_visible(tenant_id,source_id) and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));
grant update on realtime.configuration_head to rocoto_rt;

create function realtime.configuration_usage(company text) returns table(record_count bigint,retained_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with records as (
 select body from realtime.configuration_proposal where tenant_id=company
 union all select body from realtime.configuration_revision where tenant_id=company
 union all select body from realtime.configuration_ack where tenant_id=company
 union all select to_jsonb(h) from realtime.configuration_head h where tenant_id=company
 ) select count(*),coalesce(sum(octet_length(body::text)+2048),0)::bigint
 from records where realtime.company_visible(company)
$$;
revoke all on function realtime.configuration_usage(text) from public;
grant execute on function realtime.configuration_usage(text) to rocoto_rt;
alter function realtime.retained_usage(text) rename to retained_usage_continuity;
revoke all on function realtime.retained_usage_continuity(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+c.retained_bytes,
 b.source_count,b.profile_count from realtime.retained_usage_continuity(company) b
 cross join realtime.configuration_usage(company) c
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
