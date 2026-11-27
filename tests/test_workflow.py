"""Offline integration check; synthetic data only, no API calls or old outputs."""

import base64
import importlib.util
import json
import os
import tempfile
import unittest
from io import BytesIO
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
            self.assertEqual(config["workflow_version"], "scoped_paths_20261122")
            prompt = (runner.CASE / "prompt.txt").read_text()
            self.assertIn("Initial Mask mode ends", prompt)
            self.assertNotIn("3 FIRST_REVIEW:", prompt)
            self.assertNotIn("review_banks", prompt)
            self.assertIn((ROOT / "workflow/street_policy.txt").read_text().strip(), prompt)
            runner.sandbox_test()
            batches = json.loads((runner.CASE / "source_batches.json").read_text())
            self.assertEqual(sum(len(x["tiles"]) for x in batches), 1)
            tile = batches[0]["tiles"][0]

            def tool(name, args):
                return {
                    "type": "function_call",
                    "name": name,
                    "call_id": name,
                    "arguments": json.dumps(args),
                }

            batch = tool("record_source_batch", {"batch": 0, "observations": []})
            out = runner.execute_tool_call(batch, 1)["output"]
            self.assertEqual(out[-1]["type"], "input_image")
            box = [7, 74, 115, 95]
            crop = tool("view_source_details", {"regions": [{"id": "bank", "box": box}]})
            returned = runner.execute_tool_call(crop, 2)["output"]
            self.assertEqual(json.loads(returned[0]["text"])["tiles"][0]["box"], box)
            encoded = returned[-1]["image_url"].split(",", 1)[1]
            with Image.open(BytesIO(base64.b64decode(encoded))) as pixels:
                self.assertEqual(pixels.tobytes(), im.crop(box).tobytes())
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
                        "evidence": "two horizontal banks",
                        "continuation": "across tile",
                        "polygon": [[0, 80], [245, 80], [245, 170], [0, 170]],
                        "fixed_edges": [1, 3],
                        "radius": 4,
                        "exits": [
                            {
                                "id": "left",
                                "side": "west",
                                "boundary": "scan",
                                "cap_edge": 3,
                                "evidence": "both sides meet the west scan cut",
                            }
                        ],
                        "obstacles": ["Overhead bridge."],
                    }
                ],
                "exclusions": [],
                "unresolved": [],
            }
            record = tool(
                "record_source_batch",
                {
                    "batch": 1,
                    "observations": [{"tile_id": tile["id"], **inv.pop("tiles")[tile["id"]]}],
                },
            )
            build = tool("submit_roads", inv)
            premature = runner.execute_tool_call(build, 2)["output"]
            self.assertIn("lacks observations", json.loads(premature)["error"])
            saved = runner.execute_tool_call(record, 3)["output"]
            self.assertIn("All tile notes saved", saved)
            response = {"id": "mock-initial", "status": "completed", "output": [build]}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch.object(
                runner, "post", side_effect=[response]
            ) as post:
                runner.run()
            self.assertEqual(post.call_count, 1)
            offered = {t["name"] for t in post.call_args.args[1]["tools"]}
            self.assertTrue(offered.isdisjoint({"review_banks", "assess_initial"}))
            self.assertFalse((runner.CASE / "bank_review_state.json").exists())
            self.assertFalse((runner.CASE / "initial_surface_receipt.json").exists())
            stored = json.loads((runner.CASE / "inventory_r1.json").read_text())
            self.assertEqual(stored["roads"][0]["obstacles"], ["Overhead bridge."])
            self.assertEqual(stored["roads"][0]["polygon"], inv["roads"][0]["polygon"])
            self.assertEqual(stored["roads"][0]["fixed_edges"], [1, 3])
            self.assertEqual(stored["roads"][0]["exits"][0]["cap_edge"], 3)
            self.assertNotIn("render_notes", stored)
            mask = runner.CASE / "run/maps/TEST/tools/inventory_r1/mask.png"
            self.assertTrue(mask.exists())
            self.assertEqual(mask.read_bytes(), (runner.CASE / "mask.png").read_bytes())
            self.assertTrue((runner.CASE / "overlay.jpg").is_file())
            labelme = json.loads((runner.CASE / "labelme.json").read_text())
            self.assertEqual(labelme["imagePath"], "source.png")
            [shape] = labelme["shapes"]
            self.assertEqual((shape["label"], shape["shape_type"]), ("st1", "mask"))
            with Image.open(BytesIO(base64.b64decode(shape["mask"]))) as crop:
                restored = Image.new("L", (labelme["imageWidth"], labelme["imageHeight"]))
                (x, y), (right, bottom) = shape["points"]
                self.assertEqual(crop.size, (right - x + 1, bottom - y + 1))
                restored.paste(crop.point(lambda value: 255 if value else 0), (x, y))
            with Image.open(mask) as expected:
                self.assertEqual(restored.tobytes(), expected.tobytes())
            self.assertFalse((runner.CASE / "qa_r1.json").exists())
            execution = json.loads((runner.CASE / "execution.json").read_text())
            self.assertEqual(execution["status"], "completed")
            self.assertEqual(execution["mode"], "initial")
            self.assertEqual(execution["coverage"]["source_missing"], [])
            self.assertFalse(execution["coverage"]["first_review_required"])
            with Image.open(mask) as m:
                self.assertEqual(m.size, (256, 256))
                self.assertEqual(m.getpixel((128, 125)), 255)

    def test_recording_four_tiles_returns_the_last_group_and_allows_retry(self):
        with tempfile.TemporaryDirectory(prefix="street_batch_", dir=ROOT) as d:
            root = Path(d)
            source = root / "source.png"
            Image.new("RGB", (9000, 70), "white").save(source)
            with patch.multiple(runner, MAP_NAME="TEST", MAP_SOURCE=source, CASE=root / "case"):
                runner.setup()

                def record(group, notes):
                    return runner.execute_tool_call(
                        {
                            "name": "record_source_batch",
                            "call_id": f"group{group}",
                            "arguments": json.dumps({"batch": group, "observations": notes}),
                        },
                        group + 1,
                    )["output"]

                first = record(0, [])
                tiles = json.loads(first[0]["text"])["tiles"]
                self.assertEqual(len(tiles), 4)
                notes = [
                    {
                        "tile_id": tile["id"],
                        "roads": [],
                        "observation": "Empty paper",
                        "nonroad": "Paper",
                        "edge": "No exit",
                    }
                    for tile in tiles
                ]
                rejected = record(1, notes[:-1])
                self.assertIn("exactly one", json.loads(rejected)["error"])
                self.assertFalse((runner.CASE / "draft_batch_1.json").exists())
                second = record(1, notes)
                self.assertEqual(json.loads(second[0]["text"])["batch"], 2)
                self.assertEqual([x["type"] for x in second].count("input_image"), 1)
                self.assertEqual(
                    set(json.loads((runner.CASE / "draft_batch_1.json").read_text())),
                    {t["id"] for t in tiles},
                )
                self.assertEqual(runner.audit()["source_missing"], [])

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
            self.assertFalse(config.get("bank_review_tool"))
            self.assertIn((ROOT / "workflow/street_policy.txt").read_text().strip(), prompt)
            self.assertIn("retain the full parent cross-section", prompt)
            self.assertIn("extend both banks", prompt)
            self.assertIn("3 FIRST_REVIEW:", prompt)
            self.assertIn("5 CONDITIONAL_FINAL:", prompt)


if __name__ == "__main__":
    unittest.main()
