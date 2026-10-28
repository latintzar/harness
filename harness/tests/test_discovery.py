import math
from pathlib import Path
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery import discover_patterns


class Index:
    def __init__(self, records, vectors):
        self.clips = records
        self.embeddings = np.asarray(vectors, dtype=np.float32)
        self._lock = threading.RLock()
        self.report = {"model": "test-vector-space"}
        self.encoded = 0

    def _encode_text(self, query):
        self.encoded += 1
        return np.array([1, 0], dtype=np.float32)

    def search(self, *args, **kwargs):
        raise AssertionError("Discovery must score the full matrix, not capped search results")


def record(number, *, video="one", start=None, dataset="shop"):
    start = number * 4 if start is None else start
    return {"id": f"{video}-{number}", "video_id": video, "dataset": dataset,
            "camera": video, "start": start, "end": start + 4}


class DiscoveryTest(unittest.TestCase):
    def test_default_overview_groups_the_entire_collection(self):
        index = Index([record(i) for i in range(120)], [[1, 0]] * 120)
        result = discover_patterns(index, "question")
        self.assertEqual(result["coverage"]["selected_candidate_windows"], 120)
        self.assertEqual(result["coverage"]["selection"], "entire_scope")
        self.assertIsNone(result["coverage"]["candidate_limit"])

    def test_full_scope_scores_before_budget_and_dataset_filter(self):
        records = [record(i) for i in range(60)] + [record(0, video="two", dataset="factory")]
        vectors = [[math.cos(i / 100), math.sin(i / 100)] for i in range(60)] + [[1, 0]]
        index = Index(records, vectors)
        result = discover_patterns(index, "question", dataset="shop", candidate_limit=3)
        self.assertEqual(result["coverage"]["corpus_window_count"], 61)
        self.assertEqual(result["coverage"]["scored_window_count"], 60)
        self.assertEqual(result["coverage"]["selected_candidate_windows"], 3)
        self.assertEqual(result["coverage"]["unselected_windows"], 57)
        self.assertEqual(result["coverage"]["selection"], "ranked_review_budget")

    def test_continuous_video_preserves_changing_patterns(self):
        index = Index([record(i) for i in range(12)], [[1, 0]] * 4 + [[0, 1]] * 4 + [[1, 0]] * 4)
        result = discover_patterns(index, "question", groups=2, score_threshold=-1)
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["coverage"]["candidate_interval_count"], 3)
        self.assertEqual(sorted(g["candidate_window_count"] for g in result["groups"]), [4, 8])
        self.assertTrue(all(g["label"] is None for g in result["groups"]))

    def test_overlapping_windows_merge_within_pattern_and_source_only(self):
        rows = [record(0, start=0), record(1, start=2), record(2, start=6), record(3, start=14), record(0, video="two", start=0)]
        result = discover_patterns(Index(rows, [[1, 0]] * 5), "question", groups=1)
        self.assertEqual(result["coverage"]["candidate_interval_count"], 3)
        merged = next(r for r in result["drilldown"]["items"] if r["window_count"] == 3)
        self.assertEqual((merged["start"], merged["end"]), (0, 10))

    def test_threshold_selects_all_matching_windows_not_budget(self):
        rows = [record(i, video=f"video-{i}") for i in range(7)]
        result = discover_patterns(Index(rows, [[1, 0]] * 5 + [[0, 1]] * 2), "question",
                                   score_threshold=.9, candidate_limit=1)
        self.assertEqual(result["coverage"]["selected_candidate_windows"], 5)
        self.assertEqual(result["coverage"]["selection"], "uncalibrated_score_threshold")
        self.assertIsNone(result["coverage"]["candidate_limit"])

    def test_pagination_and_interval_drilldown_preserve_evidence_ids(self):
        index = Index([record(i) for i in range(10)], [[1, 0]] * 10)
        overview = discover_patterns(index, "question", groups=1)
        interval = overview["drilldown"]["items"][0]
        first = discover_patterns(index, "question", groups=1, interval_id=interval["interval_id"], limit=3)
        second = discover_patterns(index, "question", groups=1, interval_id=interval["interval_id"], offset=3, limit=3)
        self.assertEqual(first["drilldown"]["next_offset"], 3)
        self.assertEqual([r["id"] for r in first["drilldown"]["items"]], ["one-0", "one-1", "one-2"])
        self.assertEqual([r["id"] for r in second["drilldown"]["items"]], ["one-3", "one-4", "one-5"])
        self.assertEqual(first["drilldown"]["total"], 10)

    def test_deterministic_groups_and_diverse_source_representatives(self):
        rows = [record(0, video="a"), record(1, video="a"), record(0, video="b"), record(0, video="c")]
        index = Index(rows, [[1, 0], [.99, .01], [.98, .02], [.97, .03]])
        one = discover_patterns(index, "question", groups=1, representatives_per_group=3)
        two = discover_patterns(index, "question", groups=1, representatives_per_group=3)
        self.assertEqual(one["groups"], two["groups"])
        self.assertEqual({r["video_id"] for r in one["groups"][0]["representatives"]}, {"a", "b", "c"})

    def test_empty_scope_and_invalid_threshold(self):
        index = Index([record(0)], [[1, 0]])
        empty = discover_patterns(index, "question", dataset="missing")
        self.assertEqual(empty["groups"], [])
        self.assertEqual(index.encoded, 0)
        with self.assertRaises(ValueError):
            discover_patterns(index, "question", score_threshold=float("nan"))
        with self.assertRaises(ValueError):
            discover_patterns(index, "question", group_id="invented-group")


if __name__ == "__main__":
    unittest.main()
