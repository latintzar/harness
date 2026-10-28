-- Preserve historical tracker output without asserting visibility or activity.
alter table realtime.track_segment drop constraint track_segment_state_check;
alter table realtime.track_segment add constraint track_segment_state_check
 check (state in ('observed','shadow','reported'));
