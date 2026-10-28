"""Resolve host-published media against current maintained camera authority."""

from realtime.continuity import ContinuityStore
from realtime.store import AccessDenied, CapacityExceeded, Conflict, RealTimeStore, _encoded, _identifier
from realtime.subjects import SubjectStore

MAX_ARTIFACTS = 64


class PlaybackMediaStore:
    def __init__(self, store=None):
        self.store = store or RealTimeStore()

    async def bindings(self, context, references):
        """Join each reference to its current source grant, active camera, epoch and mapping.

        One scoped read transaction answers the whole catalog. Every call reads current
        rows, so a revoked grant or retracted camera refuses the next call.
        """
        if not 1 <= len(references) <= MAX_ARTIFACTS:
            raise CapacityExceeded("Choose up to 64 playback artifacts")
        continuity, subjects = ContinuityStore(self.store), SubjectStore(self.store)
        result, epochs = [], {}
        async with self.store._transaction(context, "realtime:read") as con:
            for reference in references:
                source_id, epoch_id, mapping_id = (
                    _identifier(reference[k]) for k in ("source_id", "epoch_id", "mapping_id")
                )
                if (source_id, epoch_id) not in epochs:
                    await self.store._source(con, context, source_id)
                    epoch, _ = await continuity._epoch(con, context, source_id, epoch_id)
                    camera = await subjects._read(con, context, epoch.camera_subject_id)
                    if camera["kind"] != "camera" or camera["current_status"] != "active":
                        raise AccessDenied("Playback requires its active camera")
                    epochs[source_id, epoch_id] = epoch
                epoch = epochs[source_id, epoch_id]
                mapping = await continuity._mapping(con, context, source_id, mapping_id)
                if mapping.epoch_id != epoch_id:
                    raise Conflict("Playback preserves the commissioned epoch mapping")
                result.append({
                    "source_id": source_id,
                    "epoch_id": epoch_id,
                    "tracker_namespace": epoch.tracker_namespace,
                    "camera_subject_id": epoch.camera_subject_id,
                    **mapping.model_dump(mode="json", exclude={"evidence", "epoch_id"}),
                })
            _encoded(result, context.bounds.max_output_bytes)
        return result
