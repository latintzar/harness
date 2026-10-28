-- Camera tracker lifecycle and immutable artifact clocks; no identity inference.
create table realtime.tracker_epoch (
 tenant_id text not null, source_id text not null, epoch_id text not null,
 camera_subject_id text not null, tracker_namespace text not null,
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,source_id,epoch_id),
 unique(tenant_id,source_id,tracker_namespace),
 foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id),
 foreign key(tenant_id,source_id,camera_subject_id) references realtime.subject(tenant_id,source_id,subject_id)
);
create table realtime.tracker_epoch_end (
 tenant_id text not null, source_id text not null, epoch_id text not null,
 ended_s numeric not null check(ended_s>=0),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,source_id,epoch_id),
 foreign key(tenant_id,source_id,epoch_id) references realtime.tracker_epoch(tenant_id,source_id,epoch_id)
);
create table realtime.source_artifact_mapping (
 tenant_id text not null, source_id text not null, mapping_id text not null, epoch_id text not null,
 source_asset_id text not null,
 stream_start_s numeric not null check(stream_start_s>=0),
 stream_end_s numeric not null check(stream_end_s>stream_start_s and stream_end_s-stream_start_s<=3600),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,source_id,mapping_id),
 unique(tenant_id,source_id,source_asset_id),
 unique(tenant_id,source_id,epoch_id,mapping_id),
 foreign key(tenant_id,source_id,epoch_id) references realtime.tracker_epoch(tenant_id,source_id,epoch_id)
);
create table realtime.track_segment (
 tenant_id text not null, source_id text not null, segment_id text not null,
 epoch_id text not null, mapping_id text not null, visit_subject_id text not null, receipt_id text not null,
 stream_start_s numeric not null check(stream_start_s>=0),
 stream_end_s numeric not null check(stream_end_s>stream_start_s and stream_end_s-stream_start_s<=3600),
 state text not null check(state in ('observed','shadow')),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null check(octet_length(body::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,source_id,segment_id),
 foreign key(tenant_id,source_id,epoch_id,mapping_id)
   references realtime.source_artifact_mapping(tenant_id,source_id,epoch_id,mapping_id),
 foreign key(tenant_id,source_id,visit_subject_id) references realtime.subject(tenant_id,source_id,subject_id),
 foreign key(tenant_id,source_id,receipt_id) references realtime.input_receipt(tenant_id,source_id,receipt_id)
);
create index realtime_track_segment_visit on realtime.track_segment(
 tenant_id,source_id,visit_subject_id,stream_start_s,segment_id);

do $$ declare tab text; begin
 foreach tab in array array['tracker_epoch','tracker_epoch_end','source_artifact_mapping','track_segment'] loop
  execute format('alter table realtime.%I enable row level security',tab);
  execute format('alter table realtime.%I force row level security',tab);
  execute format('create policy migration_read on realtime.%I for select to %I using(true)',tab,current_user);
  execute format('create policy scoped_read on realtime.%I for select using(realtime.source_visible(tenant_id,source_id))',tab);
  execute format('create policy scoped_insert on realtime.%I for insert with check(realtime.source_writable(tenant_id,source_id) and ''realtime:ingest''=any(realtime.actor_capabilities(tenant_id,current_setting(''rocoto.principal'',true))))',tab);
  execute format('grant select,insert on realtime.%I to rocoto_rt',tab);
 end loop;
end $$;
create function realtime.continuity_usage(company text) returns table(record_count bigint,retained_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with receipts as (
 select body from realtime.tracker_epoch where tenant_id=company
 union all select body from realtime.tracker_epoch_end where tenant_id=company
 union all select body from realtime.source_artifact_mapping where tenant_id=company
 union all select body from realtime.track_segment where tenant_id=company
 ) select count(*),coalesce(sum(octet_length(body::text)+2048),0)::bigint
 from receipts where realtime.company_visible(company)
$$;
revoke all on function realtime.continuity_usage(text) from public;
grant execute on function realtime.continuity_usage(text) to rocoto_rt;
alter function realtime.retained_usage(text) rename to retained_usage_modules;
revoke all on function realtime.retained_usage_modules(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+c.retained_bytes,
 b.source_count,b.profile_count from realtime.retained_usage_modules(company) b
 cross join realtime.continuity_usage(company) c
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
