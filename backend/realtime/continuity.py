"""Host-receipted continuity of stock tracker state across camera file rotations.

This service records tracker output; it does not perform detection or association.
One source is one camera. Source artifacts keep their bytes and original clocks.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from realtime.contracts import SourceWindow, TrackingContinuity
from realtime.store import (
    AccessDenied,
    CapacityExceeded,
    Conflict,
    RealTimeStore,
    _digest,
    _encoded,
    _identifier,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_ids(self):
        for name in type(self).model_fields:
            if name.endswith("_id") or name == "tracker_namespace":
                _identifier(getattr(self, name))
        return self


class TrackerEvidence(Strict):
    reference: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class HistoricalTrackerRun(Strict):
    """Host-attested complete run; reports do not establish physical visibility."""

    basis: Literal["complete_recording_kitti"]
    source_asset_id: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_window: SourceWindow
    frame_count: int = Field(ge=1, le=100_000, strict=True)
    fps: Decimal = Field(gt=0, le=120)
    application_config: TrackerEvidence
    execution_log: TrackerEvidence
    tracker_sidecar: TrackerEvidence
    source_mapping: TrackerEvidence
    producer: TrackerEvidence
    configuration_basis: Literal["retained_application_config"]
    visibility_basis: Literal["unclassified_tracker_report"]
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def complete_clock(self):
        duration = self.source_window.end_s - self.source_window.start_s
        if duration > 3600 or duration * self.fps != self.frame_count:
            raise ValueError("Historical run requires its complete bounded frame clock")
        return self


class EpochStart(Strict):
    epoch_id: str
    camera_subject_id: str
    tracker_namespace: str
    implementation: Literal["nvidia_deepstream_nvtracker"]
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    started_s: Decimal = Field(ge=0)
    evidence: TrackerEvidence
    historical_run: HistoricalTrackerRun | None = None

    @model_serializer(mode="wrap")
    def preserve_existing_receipts(self, handler):
        value = handler(self)
        if value["historical_run"] is None:
            value.pop("historical_run")
        return value

    @model_validator(mode="after")
    def retained_configuration(self):
        if self.historical_run and self.configuration_sha256 != self.historical_run.application_config.sha256:
            raise ValueError("Historical configuration hash identifies the retained application config")
        return self


class EpochEnd(Strict):
    epoch_id: str
    ended_s: Decimal = Field(ge=0)
    reason: Literal["tracker_reset", "process_restart", "source_clock_discontinuity", "eos", "stopped"]
    evidence: TrackerEvidence


class ArtifactMapping(Strict):
    mapping_id: str
    epoch_id: str
    source_asset_id: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_window: SourceWindow
    stream_window: SourceWindow
    evidence: TrackerEvidence

    @model_validator(mode="after")
    def same_elapsed_time(self):
        a, b = self.source_window, self.stream_window
        if a.end_s - a.start_s != b.end_s - b.start_s or a.end_s - a.start_s > 3600:
            raise ValueError("Map equal bounded source and stream durations")
        return self


class TrackerInterval(Strict):
    tracker_id: str
    birth_id: str
    source_window: SourceWindow
    state: Literal["observed", "shadow", "reported"]
    first_reported_frame: int | None = Field(default=None, ge=0, le=99_999, strict=True)

    @model_serializer(mode="wrap")
    def preserve_existing_intervals(self, handler):
        value = handler(self)
        if value["first_reported_frame"] is None:
            value.pop("first_reported_frame")
        return value


class TrackerSnapshot(Strict):
    epoch_id: str
    tracker_namespace: str
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: TrackerEvidence
    tracks: list[TrackerInterval] = Field(max_length=500)


class TrackSegment(Strict):
    segment_id: str
    epoch_id: str
    mapping_id: str
    visit_subject_id: str
    receipt_id: str
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tracker_id: str
    birth_id: str
    source_window: SourceWindow
    state: Literal["observed", "shadow", "reported"]


# One canonical frame clock for hosts and this store, whatever the ambient decimal context.
_FRAME_CLOCK = Context(prec=28, rounding=ROUND_HALF_EVEN)


def reported_frame_s(run, frame):
    """Source time of one frame in a retained run: its start plus frame index over frame rate."""
    return _FRAME_CLOCK.add(run.source_window.start_s, _FRAME_CLOCK.divide(Decimal(frame), run.fps))


def reported_birth_id(namespace, sidecar_sha256, tracker_id, first_frame):
    """Identify the first report in one retained run, independently of physical entry."""
    return "reported:" + _digest(
        ["rocoto.tracker-reported-birth.v1", namespace, sidecar_sha256, tracker_id, first_frame]
    )


def _contains(outer, inner):
    return outer.start_s <= inner.start_s < inner.end_s <= outer.end_s


def _stream_window(mapping, local):
    if not _contains(mapping.source_window, local):
        raise Conflict("Source interval falls outside its artifact mapping")
    offset = mapping.stream_window.start_s - mapping.source_window.start_s
    return SourceWindow(start_s=local.start_s + offset, end_s=local.end_s + offset)


class ContinuityStore:
    def __init__(self, store=None):
        self.store = store or RealTimeStore()

    async def _epoch(self, con, context, source_id, epoch_id):
        row = await con.fetchrow(
            "select e.body,x.ended_s from realtime.tracker_epoch e left join realtime.tracker_epoch_end x "
            "using(tenant_id,source_id,epoch_id) where e.tenant_id=$1 and e.source_id=$2 and e.epoch_id=$3",
            context.company_id,
            source_id,
            epoch_id,
        )
        if not row:
            raise AccessDenied("Tracker epoch requires its current source grant")
        return EpochStart.model_validate(row["body"]), row["ended_s"]

    async def _mapping(self, con, context, source_id, mapping_id):
        body = await con.fetchval(
            "select body from realtime.source_artifact_mapping "
            "where tenant_id=$1 and source_id=$2 and mapping_id=$3",
            context.company_id,
            source_id,
            mapping_id,
        )
        if not body:
            raise AccessDenied("Artifact mapping requires its current source grant")
        return ArtifactMapping.model_validate(body)

    async def _room(self, con, context, body):
        limits = await self.store._lock_budget(con, context)
        usage = await self.store._usage(con, context)
        self.store._check_bytes(
            limits, usage, await con.fetchval("select octet_length($1::jsonb::text)+2048", body)
        )
        count = await con.fetchval(
            "select record_count from realtime.continuity_usage($1)", context.company_id
        )
        if count >= min(limits["max_events"] * 8, 400000):
            raise CapacityExceeded("Retained tracker continuity budget reached")

    async def _retry(self, con, context, table, key, source_id, value, body):
        # Table/key are constants selected by this service, never request fields.
        prior = await con.fetchval(
            f"select digest from realtime.{table} where tenant_id=$1 and source_id=$2 and {key}=$3",
            context.company_id,
            source_id,
            value,
        )
        if prior is not None and prior != _digest(body):
            raise Conflict("Continuity receipt conflicts with its retained identity")
        return prior is not None

    async def start_epoch(self, context, source_id, record):
        spec = EpochStart.model_validate(record)
        body = spec.model_dump(mode="json")
        async with self.store._transaction(context, "realtime:ingest") as con:
            source = await self.store._source(con, context, source_id, writing=True)
            if source["kind"] != "camera":
                raise Conflict("Stock tracking epochs belong to camera sources")
            await self.store._lock_budget(con, context)
            if await self._retry(con, context, "tracker_epoch", "epoch_id", source_id, spec.epoch_id, body):
                return body
            from realtime.subjects import SubjectStore

            camera = await SubjectStore(self.store)._read(con, context, spec.camera_subject_id)
            if (
                camera["source_id"] != source_id
                or camera["kind"] != "camera"
                or camera["current_status"] != "active"
            ):
                raise Conflict("Tracker epoch requires its active same-source camera")
            if await con.fetchval(
                "select 1 from realtime.tracker_epoch e where tenant_id=$1 and source_id=$2 "
                "and not exists(select 1 from realtime.tracker_epoch_end x where "
                "x.tenant_id=e.tenant_id and x.source_id=e.source_id and x.epoch_id=e.epoch_id)",
                context.company_id,
                source_id,
            ):
                raise Conflict("Close the prior tracker epoch before starting fresh state")
            if await con.fetchval(
                "select 1 from realtime.tracker_epoch where tenant_id=$1 and source_id=$2 "
                "and tracker_namespace=$3",
                context.company_id,
                source_id,
                spec.tracker_namespace,
            ):
                raise Conflict("A fresh tracker epoch requires its own namespace")
            await self._room(con, context, body)
            await con.execute(
                "insert into realtime.tracker_epoch(tenant_id,source_id,epoch_id,camera_subject_id,"
                "tracker_namespace,digest,body) values($1,$2,$3,$4,$5,$6,$7)",
                context.company_id,
                source_id,
                spec.epoch_id,
                spec.camera_subject_id,
                spec.tracker_namespace,
                _digest(body),
                body,
            )
        return body

    async def end_epoch(self, context, source_id, record):
        spec = EpochEnd.model_validate(record)
        body = spec.model_dump(mode="json")
        async with self.store._transaction(context, "realtime:ingest") as con:
            await self.store._source(con, context, source_id, writing=True)
            await self.store._lock_budget(con, context)
            if await self._retry(
                con, context, "tracker_epoch_end", "epoch_id", source_id, spec.epoch_id, body
            ):
                return body
            epoch, _ = await self._epoch(con, context, source_id, spec.epoch_id)
            if epoch.historical_run:
                historical = epoch.historical_run
                expected_end = (
                    epoch.started_s + historical.source_window.end_s - historical.source_window.start_s
                )
                if (
                    spec.ended_s != expected_end
                    or spec.reason != "eos"
                    or spec.evidence != historical.execution_log
                ):
                    raise Conflict("Historical closure preserves the retained full-run EOS receipt")
            last = await con.fetchval(
                "select max(stream_end_s) from realtime.source_artifact_mapping "
                "where tenant_id=$1 and source_id=$2 and epoch_id=$3",
                context.company_id,
                source_id,
                spec.epoch_id,
            )
            if spec.ended_s < epoch.started_s or last is not None and spec.ended_s < last:
                raise Conflict("Epoch closure preserves every retained source interval")
            await self._room(con, context, body)
            await con.execute(
                "insert into realtime.tracker_epoch_end(tenant_id,source_id,epoch_id,ended_s,digest,body) "
                "values($1,$2,$3,$4,$5,$6)",
                context.company_id,
                source_id,
                spec.epoch_id,
                spec.ended_s,
                _digest(body),
                body,
            )
        return body

    async def map_artifact(self, context, source_id, record):
        spec = ArtifactMapping.model_validate(record)
        body = spec.model_dump(mode="json")
        async with self.store._transaction(context, "realtime:ingest") as con:
            await self.store._source(con, context, source_id, writing=True)
            await self.store._lock_budget(con, context)
            if await self._retry(
                con, context, "source_artifact_mapping", "mapping_id", source_id, spec.mapping_id, body
            ):
                return body
            epoch, end = await self._epoch(con, context, source_id, spec.epoch_id)
            if epoch.historical_run:
                historical = epoch.historical_run
                if (
                    spec.source_asset_id != historical.source_asset_id
                    or spec.source_sha256 != historical.source_sha256
                    or spec.source_window != historical.source_window
                    or spec.stream_window.start_s != epoch.started_s
                ):
                    raise Conflict("Historical tracking is limited to its exact single recording")
                if spec.evidence != historical.source_mapping:
                    raise Conflict("Historical artifact clock preserves the retained source sample mapping")
            if (
                spec.stream_window.start_s < epoch.started_s
                or end is not None
                and spec.stream_window.end_s > end
            ):
                raise Conflict("Artifact clock must fit its recorded tracker epoch")
            if await con.fetchval(
                "select 1 from realtime.source_artifact_mapping where tenant_id=$1 and source_id=$2 "
                "and (source_asset_id=$3 or (epoch_id=$4 and stream_start_s<$6 and stream_end_s>$5))",
                context.company_id,
                source_id,
                spec.source_asset_id,
                spec.epoch_id,
                spec.stream_window.start_s,
                spec.stream_window.end_s,
            ):
                raise Conflict("Artifact identity and epoch intervals require an unambiguous mapping")
            if await con.fetchval(
                "select 1 from realtime.input_receipt where tenant_id=$1 and source_id=$2 "
                "and body->>'source_asset_id'=$3",
                context.company_id,
                source_id,
                spec.source_asset_id,
            ):
                raise Conflict("Register artifact mapping before its actual input receipts")
            await self._room(con, context, body)
            await con.execute(
                "insert into realtime.source_artifact_mapping(tenant_id,source_id,mapping_id,epoch_id,"
                "source_asset_id,stream_start_s,stream_end_s,digest,body) values($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                context.company_id,
                source_id,
                spec.mapping_id,
                spec.epoch_id,
                spec.source_asset_id,
                spec.stream_window.start_s,
                spec.stream_window.end_s,
                _digest(body),
                body,
            )
        return body

    async def _validate_clock(self, con, context, source_id, asset, local, lineage):
        mapping = await self._mapping(con, context, source_id, lineage.mapping_id)
        epoch, end = await self._epoch(con, context, source_id, lineage.epoch_id)
        if mapping.epoch_id != lineage.epoch_id or mapping.source_asset_id != asset:
            raise Conflict("Continuity preserves its exact epoch and source artifact")
        actual = _stream_window(mapping, local)
        if (
            actual != lineage.stream_window
            or actual.start_s < epoch.started_s
            or end is not None
            and actual.end_s > end
        ):
            raise Conflict("Continuity clock differs from its host mapping")
        return mapping, epoch

    async def validate_input(self, con, context, source_id, body):
        lineage = body.get("continuity")
        if lineage is None:
            if body.get("tracker_snapshot") is not None or await con.fetchval(
                "select 1 from realtime.source_artifact_mapping where tenant_id=$1 and source_id=$2 "
                "and source_asset_id=$3",
                context.company_id,
                source_id,
                body["source_asset_id"],
            ):
                raise Conflict("Mapped tracker input requires its typed continuity receipt")
            return
        lineage = TrackingContinuity.model_validate(lineage)
        local = SourceWindow.model_validate(body["source_window"])
        mapping, epoch = await self._validate_clock(
            con, context, source_id, body["source_asset_id"], local, lineage
        )
        snapshot = TrackerSnapshot.model_validate(body.get("tracker_snapshot"))
        if (
            body.get("source_sha256") != mapping.source_sha256
            or snapshot.epoch_id != epoch.epoch_id
            or snapshot.tracker_namespace != epoch.tracker_namespace
            or snapshot.configuration_sha256 != epoch.configuration_sha256
        ):
            raise Conflict("Actual input must preserve its source bytes and stock tracker lineage")
        for track in snapshot.tracks:
            if not _contains(local, track.source_window):
                raise Conflict("Tracker observations stay inside the actual input interval")
        historical = epoch.historical_run
        if historical is None:
            if any(t.state == "reported" or t.first_reported_frame is not None for t in snapshot.tracks):
                raise Conflict("Reported tracker output requires a historical full-run receipt")
            return
        if (
            local != historical.source_window
            or snapshot.evidence != historical.tracker_sidecar
            or _digest(snapshot.model_dump(mode="json")) != historical.snapshot_sha256
        ):
            raise Conflict("Historical tracker snapshot must match its complete retained report")
        births = {}
        for track in snapshot.tracks:
            frame = track.first_reported_frame
            if track.state != "reported" or frame is None or frame >= historical.frame_count:
                raise Conflict("Historical reports retain unknown physical visibility")
            first = reported_frame_s(historical, frame)
            if (
                track.birth_id
                != reported_birth_id(
                    epoch.tracker_namespace, historical.tracker_sidecar.sha256, track.tracker_id, frame
                )
                or track.source_window.start_s < first
            ):
                raise Conflict("Historical visit birth preserves its first reported frame")
            prior = births.setdefault(track.tracker_id, (frame, track.source_window.start_s))
            if prior[0] != frame:
                raise Conflict("One historical raw tracker ID retains one report boundary")
            births[track.tracker_id] = (frame, min(prior[1], track.source_window.start_s))
        for frame, first_interval in births.values():
            if first_interval != reported_frame_s(historical, frame):
                raise Conflict("Historical snapshot must contain each first reported frame")

    async def event_reason(self, con, context, source_id, event, replay_context=None):
        p = event.provenance
        if p.continuity is not None and replay_context is not None:
            return "replay_continuity_not_commissioned"
        try:
            if p.continuity is not None:
                await self._validate_clock(
                    con, context, source_id, p.source_asset_id, p.source_window, p.continuity
                )
            for ref in p.actual_receipt_refs:
                body = await con.fetchval(
                    "select body from realtime.input_receipt "
                    "where tenant_id=$1 and source_id=$2 and receipt_id=$3",
                    context.company_id,
                    source_id,
                    ref,
                )
                if bool(body.get("continuity")) != bool(p.continuity):
                    return "continuity_receipt_required"
                if p.continuity is not None:
                    lineage = TrackingContinuity.model_validate(body["continuity"])
                    if (
                        lineage.epoch_id != p.continuity.epoch_id
                        or lineage.mapping_id != p.continuity.mapping_id
                        or not _contains(lineage.stream_window, p.continuity.stream_window)
                    ):
                        return "continuity_receipt_mismatch"
        except (Conflict, AccessDenied, ValueError):
            return "invalid_continuity_mapping"
        return None

    async def _track_evidence(
        self, con, context, source_id, receipt_id, receipt_sha, tracker_id, birth_id, window, state
    ):
        row = await con.fetchrow(
            "select body,digest from realtime.input_receipt "
            "where tenant_id=$1 and source_id=$2 and receipt_id=$3",
            context.company_id,
            source_id,
            receipt_id,
        )
        if not row or row["digest"] != receipt_sha or not row["body"].get("continuity"):
            raise Conflict("Track segment requires its exact actual-input receipt")
        body = row["body"]
        await self.validate_input(con, context, source_id, body)
        snapshot = TrackerSnapshot.model_validate(body["tracker_snapshot"])
        if not any(
            t.tracker_id == tracker_id
            and t.birth_id == birth_id
            and t.state == state
            and _contains(t.source_window, window)
            for t in snapshot.tracks
        ):
            raise Conflict("Track segment must be observed in the retained stock tracker receipt")
        return body, snapshot

    async def validate_visit(self, con, context, record, attrs):
        p = record.provenance
        reported = attrs.get("basis") == "first_reported"
        body, snapshot = await self._track_evidence(
            con,
            context,
            record.source_id,
            p.receipt_id,
            p.sha256,
            attrs["tracker_id"],
            attrs["visit_boundary_id"],
            SourceWindow(start_s=p.source_start_s, end_s=p.source_end_s),
            "reported" if reported else "observed",
        )
        if reported and p.source_start_s != min(
            t.source_window.start_s
            for t in snapshot.tracks
            if t.tracker_id == attrs["tracker_id"] and t.birth_id == attrs["visit_boundary_id"]
        ):
            raise Conflict("Historical visit begins at its first reported frame")
        epoch, _ = await self._epoch(con, context, record.source_id, attrs["tracker_epoch_id"])
        if (
            snapshot.epoch_id != epoch.epoch_id
            or attrs["camera_subject_id"] != epoch.camera_subject_id
            or attrs["tracker_namespace"] != epoch.tracker_namespace
            or record.namespace != epoch.tracker_namespace
            or attrs["source_asset_id"] != body["source_asset_id"]
        ):
            raise Conflict("Visit identity preserves its exact tracker epoch and birth artifact")

    async def record_segment(self, context, source_id, record):
        spec = TrackSegment.model_validate(record)
        body = spec.model_dump(mode="json")
        async with self.store._transaction(context, "realtime:ingest") as con:
            await self.store._source(con, context, source_id, writing=True)
            await self.store._lock_budget(con, context)
            if await self._retry(
                con, context, "track_segment", "segment_id", source_id, spec.segment_id, body
            ):
                return body
            evidence, snapshot = await self._track_evidence(
                con,
                context,
                source_id,
                spec.receipt_id,
                spec.receipt_sha256,
                spec.tracker_id,
                spec.birth_id,
                spec.source_window,
                spec.state,
            )
            lineage = TrackingContinuity.model_validate(evidence["continuity"])
            if spec.epoch_id != lineage.epoch_id or spec.mapping_id != lineage.mapping_id:
                raise Conflict("Track segment keeps its original epoch and artifact mapping")
            from realtime.subjects import SubjectStore

            visit = await SubjectStore(self.store)._read(con, context, spec.visit_subject_id)
            attrs = visit["attributes"]
            if (
                visit["kind"] != "visit"
                or visit["source_id"] != source_id
                or visit["current_status"] == "retracted"
                or attrs.get("tracker_epoch_id") != spec.epoch_id
                or attrs["tracker_id"] != spec.tracker_id
                or attrs["visit_boundary_id"] != spec.birth_id
                or attrs["tracker_namespace"] != snapshot.tracker_namespace
            ):
                raise Conflict("Track segment belongs to the exact retained visit lineage")
            mapping = await self._mapping(con, context, source_id, spec.mapping_id)
            stream = _stream_window(mapping, spec.source_window)
            await self._room(con, context, body)
            await con.execute(
                "insert into realtime.track_segment(tenant_id,source_id,segment_id,epoch_id,mapping_id,"
                "visit_subject_id,receipt_id,stream_start_s,stream_end_s,state,digest,body) "
                "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                context.company_id,
                source_id,
                spec.segment_id,
                spec.epoch_id,
                spec.mapping_id,
                spec.visit_subject_id,
                spec.receipt_id,
                stream.start_s,
                stream.end_s,
                spec.state,
                _digest(body),
                body,
            )
        return body

    async def validate_binding(self, con, context, source_id, event, subject):
        p = event["envelope"]["provenance"]
        if not p.get("continuity"):
            raise Conflict("Epoch visit requires an epoch-qualified event")
        lineage = TrackingContinuity.model_validate(p["continuity"])
        if (
            event["replay_context"] is not None
            or lineage.epoch_id != subject["attributes"]["tracker_epoch_id"]
        ):
            raise Conflict("Visit binding preserves its live tracker epoch")
        await self._validate_clock(
            con,
            context,
            source_id,
            p["source_asset_id"],
            SourceWindow.model_validate(p["source_window"]),
            lineage,
        )
        # A first-reported visit has unknown visibility by its own identity, whatever rows exist.
        if subject["attributes"].get("basis") == "first_reported" or not await con.fetchval(
            "select 1 from realtime.track_segment where tenant_id=$1 and source_id=$2 and "
            "visit_subject_id=$3 and epoch_id=$4 and mapping_id=$5 and receipt_id=any($6::text[]) "
            "and state='observed' and stream_start_s<=$7 and stream_end_s>=$8",
            context.company_id,
            source_id,
            subject["subject_id"],
            lineage.epoch_id,
            lineage.mapping_id,
            p["actual_receipt_refs"],
            lineage.stream_window.start_s,
            lineage.stream_window.end_s,
        ):
            raise Conflict("Visit binding requires observed tracker coverage for this event interval")

    async def _playback_rows(self, con, context, source_id, epoch_id, playhead, table):
        # Table is chosen by this service; values always remain query parameters.
        if table not in {"source_artifact_mapping", "track_segment"}:
            raise ValueError("Unsupported playback record")
        active = (
            "and (select status from realtime.subject_revision r where "
            "r.tenant_id=t.tenant_id and r.source_id=t.source_id "
            "and r.subject_id=t.visit_subject_id order by revision desc limit 1)='active' "
            if table == "track_segment"
            else ""
        )
        limit = min(context.bounds.max_events, 1000)
        rows = await con.fetch(
            "with selected as (select body,stream_start_s,stream_end_s "
            f"from realtime.{table} t where tenant_id=$1 and source_id=$2 and epoch_id=$3 "
            "and ($4::numeric is null or stream_start_s<=$4) "
            + active
            + "order by stream_start_s,stream_end_s,body::text limit $5), sized as "
            "(select *,sum(octet_length(body::text)+128) over() as bytes from selected) "
            "select case when bytes<=$6 then body end as body,bytes from sized "
            "order by stream_start_s,stream_end_s,body::text",
            context.company_id,
            source_id,
            epoch_id,
            playhead,
            limit + 1,
            context.bounds.max_output_bytes,
        )
        if len(rows) > limit or any(row["bytes"] > context.bounds.max_output_bytes for row in rows):
            raise CapacityExceeded("Tracker playback exceeds its bounded record or byte budget")
        return [row["body"] for row in rows]

    async def _playback(self, con, context, source_id, epoch_id, playhead=None):
        """Fresh tracker-only descriptor under current RLS and subject lifecycle.

        Artifact extents are immutable playback metadata. Segment history is
        clipped to elapsed time; current state includes the exact first frame.
        A first-reported visit takes its first report from its sealed boundary.
        """
        from realtime.subjects import SubjectStore

        await self.store._source(con, context, source_id)
        epoch, _ = await self._epoch(con, context, source_id, epoch_id)
        subjects = SubjectStore(self.store)
        camera = await subjects._read(con, context, epoch.camera_subject_id)
        if camera["kind"] != "camera" or camera["current_status"] != "active":
            raise AccessDenied("Tracker playback requires its active camera")
        mappings = {
            row["mapping_id"]: ArtifactMapping.model_validate(row)
            for row in await self._playback_rows(
                con, context, source_id, epoch_id, playhead, "source_artifact_mapping"
            )
        }

        def intervals(mapping, local):
            stream = _stream_window(mapping, local)
            if playhead is None or playhead >= stream.end_s:
                # An elapsed interval is the retained source window itself.
                source_end, stream_end = local.end_s, stream.end_s
            else:
                offset = mapping.stream_window.start_s - mapping.source_window.start_s
                source_end, stream_end = playhead - offset, playhead
            return {
                "source_window": {"start_s": str(local.start_s), "end_s": str(source_end)},
                "stream_window": {"start_s": str(stream.start_s), "end_s": str(stream_end)},
            }

        artifacts = [
            {
                "mapping_id": m.mapping_id,
                "source_asset_id": m.source_asset_id,
                "source_sha256": m.source_sha256,
                # Full immutable media metadata supports seeking at its first frame.
                "source_window": m.source_window.model_dump(mode="json"),
                "stream_window": m.stream_window.model_dump(mode="json"),
            }
            for m in mappings.values()
        ]
        visitors = {}
        for raw in await self._playback_rows(con, context, source_id, epoch_id, playhead, "track_segment"):
            segment = TrackSegment.model_validate(raw)
            mapping = mappings.get(segment.mapping_id)
            if mapping is None:
                raise Conflict("Track playback requires its retained artifact mapping")
            if segment.visit_subject_id not in visitors:
                visit = await subjects._read(con, context, segment.visit_subject_id)
                attrs = visit["attributes"]
                if visit["current_status"] != "active":
                    continue
                if (
                    visit["kind"] != "visit"
                    or visit["source_id"] != source_id
                    or attrs.get("tracker_epoch_id") != epoch_id
                    or attrs["tracker_namespace"] != epoch.tracker_namespace
                    or attrs["tracker_id"] != segment.tracker_id
                    or attrs["visit_boundary_id"] != segment.birth_id
                ):
                    raise Conflict("Playback preserves its exact retained visit lineage")
                first_reported = None
                if attrs["basis"] == "first_reported":
                    # The sealed visit boundary names the first report, whichever
                    # retained interval was delivered or selected first.
                    birth = next(
                        (m for m in mappings.values() if m.source_asset_id == attrs["source_asset_id"]),
                        None,
                    )
                    first = Decimal(visit["provenance"]["source_start_s"])
                    if birth is None or not (
                        birth.source_window.start_s <= first < birth.source_window.end_s
                    ):
                        raise Conflict("Playback preserves the sealed first reported frame")
                    offset = birth.stream_window.start_s - birth.source_window.start_s
                    first_reported = {
                        "source_asset_id": birth.source_asset_id,
                        "source_s": str(first),
                        "stream_s": str(first + offset),
                    }
                visitors[segment.visit_subject_id] = {
                    "visit_subject_id": segment.visit_subject_id,
                    "subject_revision": visit["revision"],
                    "tracker_id": segment.tracker_id,
                    "birth_id": segment.birth_id,
                    "basis": attrs["basis"],
                    "first_observed": None,
                    "first_reported": first_reported,
                    "segments": [],
                    "tracking_state_at_playhead": None if playhead is None else "absent",
                }
            person = visitors[segment.visit_subject_id]
            if (person["tracker_id"], person["birth_id"]) != (segment.tracker_id, segment.birth_id):
                raise Conflict("Playback segments preserve one exact tracker birth")
            if (segment.state == "reported") != (person["basis"] == "first_reported"):
                raise Conflict("Playback keeps reported intervals with their first-reported visit")
            stream = _stream_window(mapping, segment.source_window)
            if segment.state == "observed" and person["first_observed"] is None:
                person["first_observed"] = {
                    "source_asset_id": mapping.source_asset_id,
                    "source_s": str(segment.source_window.start_s),
                    "stream_s": str(stream.start_s),
                }
            if (
                playhead is not None
                and stream.start_s <= playhead < stream.end_s
                and (segment.state == "observed" or person["tracking_state_at_playhead"] == "absent")
            ):
                person["tracking_state_at_playhead"] = segment.state
            # At the exact first frame the visitor is current, but elapsed
            # history is empty. Never emit a zero-length SourceWindow.
            if playhead is not None and stream.start_s == playhead:
                continue
            person["segments"].append(
                {
                    "segment_id": segment.segment_id,
                    "mapping_id": segment.mapping_id,
                    "source_asset_id": mapping.source_asset_id,
                    "source_sha256": mapping.source_sha256,
                    "state": segment.state,
                    **intervals(mapping, segment.source_window),
                }
            )
        result = {
            "schema": "rocoto.epoch-playback.v1",
            "source_id": source_id,
            "epoch_id": epoch_id,
            "camera_subject_id": epoch.camera_subject_id,
            "tracker_namespace": epoch.tracker_namespace,
            "clock": "stream",
            "playhead_s": None if playhead is None else str(playhead),
            "artifacts": artifacts,
            "visitors": list(visitors.values()),
            "geometry_status": "unavailable",
        }
        _encoded(result, context.bounds.max_output_bytes)
        return result

    async def playback(self, context, source_id, epoch_id, *, at_s=None):
        """Read receipt-backed selectable visitors, even with zero admitted activities."""
        _identifier(source_id)
        _identifier(epoch_id)
        playhead = None if at_s is None else Decimal(str(at_s))
        if playhead is not None and (not playhead.is_finite() or playhead < 0):
            raise ValueError("Playback requires a finite nonnegative stream playhead")
        async with self.store._transaction(context, "realtime:read") as con:
            return await self._playback(con, context, source_id, epoch_id, playhead)

    async def segments(self, context, source_id, visit_subject_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Select up to 1000 retained track segments")
        async with self.store._transaction(context, "realtime:read") as con:
            await self.store._source(con, context, source_id)
            rows = await con.fetch(
                "select body,stream_start_s,stream_end_s from realtime.track_segment where tenant_id=$1 "
                "and source_id=$2 and visit_subject_id=$3 order by stream_start_s,segment_id limit $4",
                context.company_id,
                source_id,
                visit_subject_id,
                limit,
            )
            result = [
                dict(row)
                | {"stream_start_s": str(row["stream_start_s"]), "stream_end_s": str(row["stream_end_s"])}
                for row in rows
            ]
            _encoded(result, context.bounds.max_output_bytes)
            return result
