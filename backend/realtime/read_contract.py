"""Bounded public projection wire shared by authenticated Rocoto hosts."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from realtime.store import CapacityExceeded

MAX_OUTPUT_BYTES = 1 * 1024 * 1024
MAX_PLAYHEAD_S = Decimal(2**53 - 1)


def bounded_playhead(value: str | None) -> str | None:
    """Accept finite nonnegative source seconds within the public wire bounds."""
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValueError("Use bounded source seconds")
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Use finite nonnegative source seconds") from exc
    if not seconds.is_finite() or seconds < 0 or seconds > MAX_PLAYHEAD_S:
        raise ValueError("Source playhead exceeds the supported numeric range")
    return value


def public_projection(result: dict[str, Any], *, epoch_id: str | None = None) -> dict[str, Any]:
    """Expose business data and playback evidence, keeping private lineage inside the SDK."""
    public = {
        "schema": "rocoto.realtime-snapshot.v1",
        "status": result.get("status"),
        "source_id": result.get("source_id"),
        "source_asset_id": result.get("source_asset_id"),
        "original_source_id": result.get("original_source_id"),
        "lane": result.get("lane"),
        "playhead_s": result.get("playhead_s"),
        "snapshot": result.get("snapshot"),
        "freshness": result.get("freshness") or {},
    }
    if epoch_id is not None:
        public.update(epoch_id=epoch_id, clock="stream", source_asset_id=None)
    if "playback" in result:
        public["playback"] = result["playback"]
    encoded = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise CapacityExceeded("response byte budget exceeded")
    return public
