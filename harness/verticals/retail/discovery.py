"""Corpus-wide Cosmos scoring and CPU grouping for evidence-led overviews.

Groups are visual-similarity candidates without semantic labels. The agent can
inspect a few diverse representatives with the actual video reasoner, then drill
into useful groups. Adjacent windows merge into source-time candidate intervals;
these counts do not establish distinct incidents, identities, or exhaustive recall.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import math
import time

import numpy as np


def _unit(rows):
    rows = np.asarray(rows, dtype=np.float32)
    norm = np.linalg.norm(rows, axis=-1, keepdims=True)
    if not np.isfinite(rows).all() or (norm < 1e-8).any():
        raise ValueError("Discovery requires finite, nonzero Cosmos vectors.")
    return rows / norm


def _distribution(scores):
    if len(scores) == 0:
        return {"count": 0}
    values = np.quantile(scores, [0, .05, .25, .5, .75, .95, 1])
    return {"count": len(scores), **{key: round(float(value), 6) for key, value in zip(
        ("min", "p05", "p25", "median", "p75", "p95", "max"), values)}}


def _cluster(vectors, groups):
    """Deterministic spherical k-means, bounded fitting sample; assign every row."""
    if len(vectors) <= 1 or groups == 1:
        return np.zeros(len(vectors), dtype=np.int32)
    sample = vectors[np.linspace(0, len(vectors) - 1, min(4096, len(vectors)), dtype=int)]
    centers = [sample[0]]  # Input intervals are ranked by their best query score.
    while len(centers) < min(groups, len(sample)):
        similarity = sample @ np.asarray(centers).T
        distance = 1 - similarity.max(axis=1)
        chosen = int(np.argmax(distance))
        if distance[chosen] <= 1e-6:
            break
        centers.append(sample[chosen])
    centers = np.asarray(centers, dtype=np.float32)
    for _ in range(24):
        labels = np.argmax(sample @ centers.T, axis=1)
        updated = centers.copy()
        for group in range(len(centers)):
            members = sample[labels == group]
            if len(members):
                mean = members.mean(axis=0)
                norm = np.linalg.norm(mean)
                if norm > 1e-8:
                    updated[group] = mean / norm
        if np.max(np.abs(updated - centers)) < 1e-5:
            centers = updated
            break
        centers = updated
    assignments = np.empty(len(vectors), dtype=np.int32)
    for start in range(0, len(vectors), 8192):
        assignments[start:start + 8192] = np.argmax(vectors[start:start + 8192] @ centers.T, axis=1)
    return assignments


def _merge_intervals(records, vectors, scores, selected):
    ordered = sorted(selected, key=lambda i: (records[i]["video_id"], float(records[i]["start"]),
                                               float(records[i]["end"]), records[i]["id"]))
    intervals = []
    for i in ordered:
        clip = records[i]
        start, end = float(clip["start"]), float(clip["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Source windows require finite, increasing timestamps.")
        if (intervals and intervals[-1]["video_id"] == clip["video_id"]
                and start <= intervals[-1]["end"] + 1e-6):
            intervals[-1]["end"] = max(intervals[-1]["end"], end)
            intervals[-1]["members"].append(i)
        else:
            intervals.append({"video_id": clip["video_id"], "dataset": clip["dataset"],
                              "camera": clip["camera"], "start": start, "end": end, "members": [i]})
    for interval in intervals:
        members = interval["members"]
        interval["max_score"] = float(scores[members].max())
        interval["mean_score"] = float(scores[members].mean())
        interval["window_count"] = len(members)
        interval["representative"] = min(members, key=lambda i: (-float(scores[i]), records[i]["id"]))
        identity = "|".join(records[i]["id"] for i in members)
        interval["interval_id"] = "interval-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        mean = vectors[members].mean(axis=0)
        interval["vector"] = mean / max(float(np.linalg.norm(mean)), 1e-8)
    return sorted(intervals, key=lambda interval: (-interval["max_score"], interval["interval_id"]))


def _representatives(members, records, vectors, scores, count):
    ranked = sorted(members, key=lambda i: (-float(scores[i]), records[i]["id"]))
    chosen = [ranked[0]]
    while len(chosen) < min(count, len(ranked)):
        remaining = [i for i in ranked if i not in chosen]
        cameras = {records[i]["video_id"] for i in chosen}
        diverse_sources = [i for i in remaining if records[i]["video_id"] not in cameras]
        pool = diverse_sources or remaining
        similarity = vectors[pool] @ vectors[chosen].T
        diversity = 1 - similarity.max(axis=1)
        query_scores = scores[pool]
        span = float(query_scores.max() - query_scores.min())
        relevance = (query_scores - query_scores.min()) / span if span > 1e-8 else np.zeros(len(pool))
        # Highest-ranked first; subsequent clips add appearance/source diversity.
        picked = int(np.argmax(.75 * diversity + .25 * relevance))
        chosen.append(pool[picked])
    return chosen


def _clip_result(record, score):
    result = {key: record[key] for key in ("id", "video_id", "dataset", "camera", "start", "end")}
    result.update(score=round(float(score), 6), verification="candidate",
                  source_url=f"/?clip={record['id']}", video_url=f"/clips/{record['id']}.mp4",
                  thumbnail_url=f"/thumbnails/{record['id']}.jpg", playback_start=0)
    return result


def _interval_result(interval, records, scores):
    return {key: interval[key] for key in ("interval_id", "group_id", "video_id", "dataset", "camera", "start", "end", "window_count")} | {
        "max_score": round(interval["max_score"], 6), "mean_score": round(interval["mean_score"], 6),
        "representative": _clip_result(records[interval["representative"]], scores[interval["representative"]])}


def discover_patterns(index, query, dataset=None, groups=8, candidate_limit=None, score_threshold=None,
                      representatives_per_group=3, group_id=None, interval_id=None, offset=0, limit=20):
    """Score every scoped window; group selected candidates and page the evidence.

    The default groups every scoped window. An explicit candidate_limit selects
    a ranked subset. With a threshold, every scoped score at or above it
    participates (candidate_limit is ignored). Thresholds are uncalibrated.
    group_id pages that group's intervals; interval_id pages its source clip IDs.
    """
    started = time.perf_counter()
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("Provide a discovery question of one to 2000 characters.")
    for value, name, minimum, maximum in [(groups, "groups", 1, 8),
                                          (representatives_per_group, "representatives_per_group", 1, 8),
                                          (offset, "offset", 0, 10**9), (limit, "limit", 1, 100)]:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if candidate_limit is not None and (type(candidate_limit) is not int or not 1 <= candidate_limit <= 1000000):
        raise ValueError("candidate_limit must be an integer from 1 to 1000000, or omitted for the full scope.")
    if score_threshold is not None:
        if isinstance(score_threshold, bool) or not isinstance(score_threshold, (int, float)) or not math.isfinite(score_threshold) or not -1 <= score_threshold <= 1:
            raise ValueError("Choose a finite cosine threshold between -1 and 1.")
    with getattr(index, "_lock", nullcontext()):
        all_records = list(index.clips)
        matrix = np.asarray(index.embeddings)
        if len(matrix) != len(all_records):
            raise ValueError("Corpus metadata and embeddings must have equal coverage.")
        eligible = [i for i, clip in enumerate(all_records) if dataset is None or clip["dataset"] == dataset]
        records = [all_records[i] for i in eligible]
        if eligible:
            vectors = _unit(matrix[eligible])
            text = _unit(np.asarray(index._encode_text(query.strip())).reshape(1, -1))[0]
            if vectors.shape[1] != len(text):
                raise ValueError("Query and video vectors must share a model space.")
            scores = np.clip(vectors @ text, -1, 1)
        else:
            vectors, scores = np.empty((0, 0)), np.empty(0)
    ranked = np.argsort(-scores, kind="stable")
    selected = ranked[:candidate_limit] if score_threshold is None else ranked[scores[ranked] >= score_threshold]
    # Cluster windows before merging time ranges. A long continuous recording
    # can contain changing activity; averaging it first would erase patterns.
    scope_id = hashlib.sha256((query.strip() + f"|{groups}|" + "|".join(records[i]["id"] for i in selected)).encode()).hexdigest()[:12]
    labels = _cluster(vectors[selected], groups) if len(selected) else np.empty(0, dtype=int)
    buckets = [list(selected[labels == label]) for label in sorted(set(labels.tolist()))]
    buckets.sort(key=lambda bucket: -max(float(scores[i]) for i in bucket))
    summaries, by_group, intervals = [], {}, []
    for number, bucket in enumerate(buckets, 1):
        gid = f"group-{scope_id}-{number}"
        grouped_intervals = _merge_intervals(records, vectors, scores, bucket)
        for interval in grouped_intervals:
            interval["group_id"] = gid
        intervals.extend(grouped_intervals)
        members = bucket
        reps = _representatives(members, records, vectors, scores, representatives_per_group)
        by_group[gid] = grouped_intervals
        summaries.append({"group_id": gid, "label": None, "candidate_window_count": len(members),
                          "candidate_interval_count": len(grouped_intervals),
                          "source_video_count": len({records[i]["video_id"] for i in members}),
                          "score_distribution": _distribution(scores[members]),
                          "representatives": [_clip_result(records[i], scores[i]) for i in reps],
                          "review_status": "Inspect representatives to propose a label; extend review to test it."})
    intervals.sort(key=lambda item: (-item["max_score"], item["interval_id"]))
    if group_id is not None and group_id not in by_group:
        raise ValueError("Use a group_id returned for this question and selection settings.")
    scoped = by_group[group_id] if group_id is not None else intervals
    if interval_id is not None:
        interval = next((item for item in scoped if item["interval_id"] == interval_id), None)
        if interval is None:
            raise ValueError("Use an interval_id returned within the selected group.")
        members = sorted(interval["members"], key=lambda i: (float(records[i]["start"]), records[i]["id"]))
        page = [_clip_result(records[i], scores[i]) for i in members[offset:offset + limit]]
        total, page_kind = len(members), "source_windows"
    else:
        page = [_interval_result(item, records, scores) for item in scoped[offset:offset + limit]]
        total, page_kind = len(scoped), "contiguous_candidate_intervals"
    return {"query": query.strip(), "dataset": dataset, "model": (getattr(index, "report", {}) or {}).get("model"),
            "coverage": {"corpus_window_count": len(all_records), "scored_window_count": len(records),
                         "selected_candidate_windows": len(selected), "unselected_windows": len(records) - len(selected),
                         "candidate_interval_count": len(intervals), "scoring_coverage": "all windows in selected dataset scope",
                         "selection": ("uncalibrated_score_threshold" if score_threshold is not None else
                                       "entire_scope" if candidate_limit is None else "ranked_review_budget"),
                         "candidate_limit": candidate_limit if score_threshold is None else None,
                         "score_threshold": score_threshold, "scored_distribution": _distribution(scores)},
            "groups": summaries,
            "drilldown": {"group_id": group_id, "interval_id": interval_id, "kind": page_kind, "offset": offset,
                          "limit": limit, "total": total, "next_offset": offset + limit if offset + limit < total else None,
                          "items": page},
            "interpretation": "Groups reflect visual similarity. Merged source-time intervals are candidate coverage, not validated incidents. Representative inspection supports hypotheses; counts need broader review.",
            "cost": {"provider_spend_usd": 0, "device": "cpu", "elapsed_seconds": round(time.perf_counter() - started, 4)}}
