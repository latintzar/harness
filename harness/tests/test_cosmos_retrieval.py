"""Offline fixtures check Cosmos import provenance and model separation."""
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("cosmos_retrieval", HERE / "verticals/retail/cosmos_retrieval.py")
cosmos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cosmos)


class CosmosRetrievalTests(unittest.TestCase):
    def fixture(self, root):
        import cv2
        video = root / "source.mp4"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
        for _ in range(20):
            writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
        writer.release()
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps([{"id": "fixture", "source": str(video), "start": 0, "end": 2, "clip": str(video), "dataset": "fixture", "camera": "cam-a"}]))
        output = root / "output.json"
        payload = {"model": cosmos.MODEL_ID, "model_revision": cosmos.MODEL_REVISION, "dimensions": 256, "frames_per_clip": 8,
                   "clips": [{"path": str(video), "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "duration_seconds": 2, "embedding": [1.0] + [0.0] * 255}], "timing": {"worker_seconds": 1}}
        output.write_text(json.dumps(payload))
        return manifest, output, payload

    def test_import_retains_original_timing_and_requires_text_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, _ = self.fixture(root)
            report = cosmos.prepare_index([output], [manifest], root / "index")
            self.assertEqual(report["clip_count"], 1)
            marker = json.loads((root / "index/index-generation.json").read_text())
            self.assertEqual(marker["schema_version"], 2)
            self.assertEqual(marker["model_revision"], cosmos.MODEL_REVISION)
            for filename, proof in marker["files"].items():
                content = cosmos.generation_paths(root / "index", marker)[filename].read_bytes()
                self.assertEqual(proof["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(proof["bytes"], len(content))
            index = cosmos.CosmosRetrievalIndex(root / "index")
            self.assertFalse(index.status()["ready"])
            self.assertEqual(index.clips[0]["camera"], "cam-a")
            self.assertEqual(index.clips[0]["end"], 2)
            self.assertTrue(Path(index.clips[0]["thumbnail_path"]).is_file())
            self.assertEqual(index.embeddings.shape, (1, 256))
            index._encode_text = lambda query: np.array([1.0] + [0.0] * 255, dtype=np.float32)
            found = index.search("fixture", dataset="fixture")
            self.assertEqual(found[0]["model"], cosmos.MODEL_ID)
            self.assertNotIn("path", found[0])

    def test_import_rejects_a_different_embedding_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, payload = self.fixture(root)
            payload["model_revision"] = "wrong-model-revision"
            output.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "revision"):
                cosmos.prepare_index([output], [manifest], root / "index")

    def test_import_rejects_changed_input_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, payload = self.fixture(root)
            payload["clips"][0]["sha256"] = "0" * 64
            output.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "SHA256"):
                cosmos.prepare_index([output], [manifest], root / "index")

    def test_import_rejects_nonfinite_vector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, payload = self.fixture(root)
            payload["clips"][0]["embedding"][0] = float("nan")
            output.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "finite"):
                cosmos.prepare_index([output], [manifest], root / "index")

    def test_cafe_previews_share_cafe_scope_and_retain_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, _ = self.fixture(root)
            entries = json.loads(manifest.read_text())
            entries[0].update(dataset="cafe-preview", provenance={"recording_kind": "staged public cafe preview"})
            manifest.write_text(json.dumps(entries))
            cosmos.prepare_index([output], [manifest], root / "index")
            record = cosmos.CosmosRetrievalIndex(root / "index").clips[0]
            self.assertEqual(record["dataset"], "cafe")
            self.assertEqual(record["source_dataset"], "cafe-preview")
            self.assertEqual(record["provenance"]["recording_kind"], "staged public cafe preview")

    def test_failed_import_never_commits_its_generation_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, payload = self.fixture(root)
            state = root / "index"
            cosmos.prepare_index([output], [manifest], state)
            index = cosmos.CosmosRetrievalIndex(state)
            committed = (state / "index-generation.json").read_bytes()
            payload["clips"][0]["embedding"] = [0.0, 1.0] + [0.0] * 254
            output.write_text(json.dumps(payload))
            original_write = cosmos._atomic_write
            def interrupt_report(path, content):
                if path.name == "index-report.json":
                    raise OSError("Fixture interruption before report publication")
                original_write(path, content)
            with patch.object(cosmos, "_atomic_write", interrupt_report), self.assertRaises(OSError):
                cosmos.prepare_index([output], [manifest], state)
            self.assertEqual((state / "index-generation.json").read_bytes(), committed)
            self.assertFalse(index.refresh())
            self.assertEqual(index.embeddings[0, 0], 1)
            self.assertEqual(cosmos.CosmosRetrievalIndex(state).embeddings[0, 0], 1)
            cosmos.prepare_index([output], [manifest], state)
            self.assertTrue(index.refresh())
            self.assertEqual(index.embeddings[0, 1], 1)


class CosmosGenerationTests(unittest.TestCase):
    def publish(self, state, generation="a", dimension=256, revision=cosmos.MODEL_REVISION):
        """Small offline artifacts, with every digest produced independently of the importer."""
        state.mkdir(exist_ok=True)
        records = [{"id": f"clip-{generation}", "video_id": "video", "dataset": "fixture", "camera": generation,
                    "start": 0, "end": 2, "sample_time": 1, "model": cosmos.MODEL_ID,
                    "model_revision": revision}]
        vectors = np.zeros((1, dimension), dtype=np.float32)
        vectors[0, ord(generation[0]) % dimension] = 1
        stream = io.BytesIO()
        np.save(stream, vectors, allow_pickle=False)
        report = {"model": cosmos.MODEL_ID, "model_revision": revision, "embedding_dimensions": dimension,
                  "clip_count": 1, "video_count": 1}
        files = {"clips.json": json.dumps(records).encode(), "embeddings.npy": stream.getvalue(),
                 "index-report.json": json.dumps(report).encode()}
        for filename, content in files.items():
            (state / filename).write_bytes(content)
        marker = {"schema_version": 1, "generation_id": generation, "model": cosmos.MODEL_ID,
                  "model_revision": revision, "dimensions": dimension, "clip_count": 1,
                  "files": {filename: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                            for filename, content in files.items()}}
        temporary = state / "marker.tmp"
        temporary.write_text(json.dumps(marker))
        temporary.replace(state / "index-generation.json")
        return files

    def test_refresh_swaps_complete_generation_and_keeps_encoder_warm(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.publish(state)
            index = cosmos.CosmosRetrievalIndex(state)
            encoder, tokenizer = object(), object()
            index._model, index._processor = encoder, tokenizer
            self.publish(state, "b")
            self.assertTrue(index.refresh())
            self.assertEqual(index.clips[0]["id"], "clip-b")
            self.assertEqual(index.embeddings[0, ord("b")], 1)
            self.assertEqual(index.report["clip_count"], 1)
            self.assertIs(index._model, encoder)
            self.assertIs(index._processor, tokenizer)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("Unchanged marker should skip file reads")):
                self.assertFalse(index.refresh())

    def test_mixed_generation_with_equal_row_counts_preserves_previous_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            old = self.publish(state)
            index = cosmos.CosmosRetrievalIndex(state)
            previous = (index.clips, index.embeddings, index.report)
            self.publish(state, "b")
            (state / "embeddings.npy").write_bytes(old["embeddings.npy"])
            with self.assertRaisesRegex(ValueError, "SHA256"):
                index.refresh()
            for actual, saved in zip((index.clips, index.embeddings, index.report), previous):
                self.assertIs(actual, saved)
            with self.assertRaisesRegex(ValueError, "SHA256"):
                cosmos.CosmosRetrievalIndex(state)

    def test_incomplete_generation_can_be_retried_after_files_arrive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.publish(state)
            index = cosmos.CosmosRetrievalIndex(state)
            current = self.publish(state, "b")
            (state / "index-report.json").unlink()
            with self.assertRaisesRegex(ValueError, "incomplete"):
                index.refresh()
            self.assertEqual(index.clips[0]["id"], "clip-a")
            (state / "index-report.json").write_bytes(current["index-report.json"])
            self.assertTrue(index.refresh())
            self.assertEqual(index.clips[0]["id"], "clip-b")

    def test_valid_digests_do_not_accept_wrong_revision_or_dimensions(self):
        for options in ({"revision": "different-checkpoint"}, {"dimension": 128}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                self.publish(state)
                index = cosmos.CosmosRetrievalIndex(state)
                self.publish(state, "b", **options)
                with self.assertRaises(ValueError):
                    index.refresh()
                self.assertEqual(index.clips[0]["id"], "clip-a")

    def test_legacy_index_keeps_working_until_a_generation_is_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.publish(state)
            (state / "index-generation.json").unlink()
            index = cosmos.CosmosRetrievalIndex(state)
            self.assertEqual(index.clips[0]["id"], "clip-a")
            self.assertFalse(index.refresh())
            self.publish(state, "b")
            self.assertTrue(index.refresh())

    def test_changed_marker_during_load_preserves_previous_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.publish(state)
            index = cosmos.CosmosRetrievalIndex(state)
            self.publish(state, "b")
            original_read = Path.read_bytes
            def change_after_read(path):
                content = original_read(path)
                if path.name == "index-report.json":
                    self.publish(state, "c")
                return content
            with patch.object(Path, "read_bytes", change_after_read), self.assertRaisesRegex(ValueError, "changed"):
                index.refresh()
            self.assertEqual(index.clips[0]["id"], "clip-a")
            self.assertTrue(index.refresh())
            self.assertEqual(index.clips[0]["id"], "clip-c")


if __name__ == "__main__":
    unittest.main()
