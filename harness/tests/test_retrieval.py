"""Small, offline checks for evidence boundaries and retrieval filtering."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

MODULE_PATH = Path(__file__).parents[1] / "verticals/retail/retrieval.py"
spec = importlib.util.spec_from_file_location("video_retrieval", MODULE_PATH)
retrieval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retrieval)


class RetrievalTests(unittest.TestCase):
    def test_dataset_filter_and_public_evidence_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = [
                {"id": "cafe-a-0", "video_id": "cafe-a", "dataset": "cafe", "camera": "A", "start": 0, "end": 2, "path": "/private/a.mp4", "thumbnail_path": "/private/a.jpg", "sample_time": 1},
                {"id": "warehouse-a-0", "video_id": "warehouse-a", "dataset": "warehouse", "camera": "A", "start": 0, "end": 2, "path": "/private/b.mp4", "thumbnail_path": "/private/b.jpg", "sample_time": 1},
                {"id": "cafe-b-0", "video_id": "cafe-b", "dataset": "cafe", "camera": "B", "start": 0, "end": 2, "path": "/private/c.mp4", "thumbnail_path": "/private/c.jpg", "sample_time": 1},
            ]
            (root / "clips.json").write_text(json.dumps(records))
            np.save(root / "embeddings.npy", np.array([[0.6, 0.8], [1, 0], [0, 1]], dtype=np.float32))
            index = retrieval.RetrievalIndex(root)
            index._encode_text = lambda query: np.array([1, 0], dtype=np.float32)
            matches = index.search("a person", dataset="cafe")
            self.assertEqual([m["id"] for m in matches], ["cafe-a-0", "cafe-b-0"])
            self.assertAlmostEqual(matches[0]["score"], 0.6, places=6)
            self.assertNotIn("path", matches[0])
            self.assertNotIn("thumbnail_path", matches[0])
            self.assertEqual(matches[0]["source_url"], "/media/cafe-a#t=0,2")
            self.assertEqual(index.get_video("cafe-a")["path"], "/private/a.mp4")
            self.assertEqual(index.search("   "), [])
            self.assertEqual(index.search("person", dataset="missing"), [])

    def test_partial_last_window_and_precise_frame_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            import cv2
            root = Path(tmp)
            video = root / "short.mp4"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
            for i in range(25):
                writer.write(np.full((48, 64, 3), i * 5, dtype=np.uint8))
            writer.release()
            index = retrieval.RetrievalIndex(root / "index")
            index._encode_images = lambda images: np.tile(np.array([[0.6, 0.8]], dtype=np.float32), (len(images), 1))
            report = index.build([{"path": str(video), "id": "short", "dataset": "fixture", "camera": "camera-1"}], interval=2)
            self.assertEqual(report["clip_count"], 2)
            clips = json.loads((root / "index/clips.json").read_text())
            self.assertEqual([(c["start"], c["end"]) for c in clips], [(0.0, 2.0), (2.0, 2.5)])
            self.assertTrue(all(c["start"] <= c["sample_time"] < c["end"] for c in clips))
            self.assertEqual(np.load(root / "index/embeddings.npy").shape, (2, 2))
            inspection = index.inspect_clip(clips[1]["id"])
            self.assertTrue(inspection["frames"])
            for frame in inspection["frames"]:
                self.assertTrue(Path(frame["path"]).is_file())
                self.assertLess(frame["time"], 2.5)

    def test_corrupted_index_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips.json").write_text("[]")
            np.save(root / "embeddings.npy", np.ones((1, 2)))
            with self.assertRaises(ValueError):
                retrieval.RetrievalIndex(root)


if __name__ == "__main__":
    unittest.main()
