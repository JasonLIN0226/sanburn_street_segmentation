"""Offline integration check; synthetic data only, no API calls or old outputs."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("street_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class IndependentWorkflowTest(unittest.TestCase):
    def test_setup_sandbox_image_and_renderer(self):
        with tempfile.TemporaryDirectory(prefix="street_test_", dir=ROOT) as d:
            root = Path(d)
            source = root / "source.png"
            im = Image.new("RGB", (256, 256), "white")
            draw = ImageDraw.Draw(im)
            draw.line([(8, 80), (248, 80)], fill="black", width=2)
            draw.line([(8, 170), (248, 170)], fill="black", width=2)
            im.save(source)
            runner.MAP_NAME = "TEST"
            runner.MAP_SOURCE = source
            runner.CASE = root / "case"
            runner.setup()
            config = json.loads((runner.CASE / "config.json").read_text())
            self.assertEqual(config["mode"], "initial")
            prompt = (runner.CASE / "prompt.txt").read_text()
            self.assertIn("Initial Mask mode ends", prompt)
            self.assertNotIn("3 FIRST_REVIEW:", prompt)
            runner.sandbox_test()
            batches = json.loads((runner.CASE / "source_batches.json").read_text())
            self.assertEqual(sum(len(x["tiles"]) for x in batches), 1)
            tile = batches[0]["tiles"][0]
            runner.append(runner.CASE / "stages.jsonl", {"stage": "source_review"})
            out = runner.image_output([tile["image"]])
            self.assertEqual(out[1]["type"], "input_image")
            with self.assertRaises(ValueError):
                runner.image_output([str(ROOT / "workflow/conditional.py")])
            inv = {
                "tiles": {
                    tile["id"]: {
                        "roads": ["synthetic"],
                        "observation": "synthetic test",
                        "nonroad": "test",
                        "edge": "test",
                    }
                },
                "roads": [
                    {
                        "id": "synthetic",
                        "name": "synthetic",
                        "polygon": [[10, 80], [245, 80], [245, 170], [10, 170]],
                        "fixed_edges": [1, 3],
                        "radius": 4,
                        "exits": [],
                        "obstacles": [],
                    }
                ],
                "exclusions": [],
                "unresolved": [],
            }
            (runner.CASE / "inventory_r1.json").write_text(json.dumps(inv))
            command = (
                f"{runner.PYTHON} conditional.py mark initial_build && "
                f"{runner.PYTHON} conditional.py build inventory_r1"
            )
            response = {
                "id": "mock-initial",
                "status": "completed",
                "output": [{
                    "type": "function_call",
                    "name": "run_command",
                    "call_id": "build",
                    "arguments": json.dumps({"command": command}),
                }],
            }
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch.object(
                runner, "post", return_value=response
            ) as post:
                runner.run()
            post.assert_called_once()
            mask = runner.CASE / "run/maps/TEST/tools/inventory_r1/mask.png"
            self.assertTrue(mask.exists())
            self.assertEqual(mask.read_bytes(), (runner.CASE / "mask.png").read_bytes())
            self.assertTrue((runner.CASE / "overlay.jpg").is_file())
            self.assertFalse((runner.CASE / "qa_r1.json").exists())
            execution = json.loads((runner.CASE / "execution.json").read_text())
            self.assertEqual(execution["status"], "completed")
            self.assertEqual(execution["mode"], "initial")
            self.assertEqual(execution["coverage"]["source_missing"], [])
            self.assertFalse(execution["coverage"]["first_review_required"])
            with Image.open(mask) as m:
                self.assertEqual(m.size, (256, 256))
                self.assertEqual(m.getpixel((128, 125)), 255)

    def test_max_keeps_full_review_prompt(self):
        with tempfile.TemporaryDirectory(prefix="street_test_", dir=ROOT) as d:
            root = Path(d)
            source = root / "source.png"
            Image.new("RGB", (30, 30), "white").save(source)
            with patch.multiple(runner, MAP_NAME="TEST", MAP_SOURCE=source, CASE=root / "case"):
                runner.setup("max")
                config = json.loads((runner.CASE / "config.json").read_text())
                prompt = (runner.CASE / "prompt.txt").read_text()
            self.assertEqual(config["mode"], "max")
            self.assertIn("3 FIRST_REVIEW:", prompt)
            self.assertIn("5 CONDITIONAL_FINAL:", prompt)


if __name__ == "__main__":
    unittest.main()
