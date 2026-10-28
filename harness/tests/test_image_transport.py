"""Exercise the rendering recipe against the native app-server string transport."""
import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent_bridge as bridge


class ImageTransportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "The native rendering recipe uses JavaScript")
    def test_source_images_are_rendered_separately_from_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            frame = state / "source.jpg"
            frame.write_bytes(b"source-image-bytes")
            record = {"id":"clip-a", "dataset":"cafe", "start":0, "end":6}
            retrieval = SimpleNamespace(
                get_clip=lambda _:record,
                inspect_clip=lambda _: {"clip":record, "frames":[{"path":str(frame), "time":1.2}]},
            )
            agent = bridge.CodexVideoAgent(state, retrieval, lambda:[])
            content, _, success = asyncio.run(agent._tool("inspect_clip", {"clip_id":"clip-a"}, "cafe", "proof"))
            self.assertTrue(success)
            # Codex app-server 0.153.4 code mode flattens dynamic contentItems into this string.
            flattened = "\n".join(item.get("text", item.get("imageUrl", "")) for item in content)
            recipe = getattr(bridge, "FRAME_OUTPUT_RENDER_JS", "")
            self.assertTrue(recipe, "inspect_clip must document the actual code-mode image transport")
            spec = next(item for item in bridge.tool_specs() if item["name"] == "inspect_clip")
            self.assertIn(recipe, spec["description"])
            program = "const r=" + json.dumps(flattened) + ";const out=[];const image=v=>out.push({image:v});const text=v=>out.push({text:v});" + recipe + ";console.log(JSON.stringify(out));"
            output = json.loads(subprocess.check_output(["node", "-e", program], text=True))
            images = [item["image"] for item in output if "image" in item]
            texts = [item["text"] for item in output if "text" in item]
            self.assertEqual(len(images), 1)
            self.assertEqual(base64.b64decode(images[0].split(",", 1)[1]), frame.read_bytes())
            self.assertEqual(json.loads(texts[0])["sampled_frame_times"], [1.2])
            self.assertNotIn("data:image/", "\n".join(texts))


if __name__ == "__main__":
    unittest.main()
