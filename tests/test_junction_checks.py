"""Regression checks for the scoped workflow's local, read-only diagnostics."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

SCOPED = Path(__file__).resolve().parents[1] / "workflow"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = load("junction_checks", SCOPED / "src/junction_checks.py")
submission = load("scoped_initial_tools", SCOPED / "initial_tools.py")
review = load("apply_junction_review", SCOPED.parent / "apply_junction_review.py")


class JunctionTest(unittest.TestCase):
    def test_review_can_only_add_within_the_selected_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base, other = root / "base", root / "alternate"
            original = np.zeros((100, 160), np.uint8)
            original[5, 5] = 255
            alternate = np.full_like(original, 255)
            alternate[5, 5] = 0
            for folder, pixels in [(base, original), (other, alternate)]:
                folder.mkdir()
                Image.fromarray(original).save(folder / "source.png")
                Image.fromarray(pixels).save(folder / "mask.png")
                summary = dict(
                    map="test",
                    selected_revision="inventory_r1",
                    mask_sha256=review.digest(folder / "mask.png"),
                    review_locations=[
                        dict(
                            kind="possible_open_junction",
                            roads=["a", "b"],
                            native_triangle=[[20, 20], [20, 80], [140, 50]],
                        )
                    ],
                )
                (folder / "summary.json").write_text(json.dumps(summary))
            revision = base / "run/maps/test/tools/inventory_r1"
            revision.mkdir(parents=True)
            inventory = dict(
                roads=[dict(id=k, radius=2) for k in ("a", "b")],
                exclusions=[dict(polygon=[[38, 45], [46, 45], [46, 55], [38, 55]], radius=1)],
            )
            geo = revision / "agent_input.json"
            geo.write_text(json.dumps(inventory))
            (revision / "proposal.json").write_text(
                json.dumps(dict(agent_input_sha256=review.digest(geo)))
            )
            dest = root / "corrected.png"
            review.apply_review(base, other, 1, dest)
            with Image.open(dest) as im:
                result = np.asarray(im)
            self.assertEqual(result[50, 80], 255)
            self.assertEqual(result[5, 5], 255)
            self.assertEqual(result[4, 4], 0)
            self.assertEqual(result[50, 22], 0)
            self.assertEqual(result[50, 42], 0)
            with self.assertRaises(FileExistsError):
                review.apply_review(base, other, 1, dest)

    def test_only_observable_gap_between_distinct_streets_is_flagged(self):
        first = [[35, 50], [190, 80], [190, 20], [35, 20]]
        second = [[35, 112], [190, 80], [190, 140], [35, 140]]
        roads = [
            dict(id=name, polygon=ring, fixed_edges=[0], radius=3)
            for name, ring in [("r1", first), ("r2", second)]
        ]
        obstacle = [dict(polygon=[[70, 74], [77, 74], [77, 82], [70, 82]])]
        mask = np.zeros((160, 220), np.uint8)
        for ring in (first, second):
            cv2.fillPoly(mask, [np.int32(ring)], 255)
        source = np.full_like(mask, 225)
        cv2.line(source, (35, 50), (35, 112), 40, 2)
        before = mask.copy()
        self.assertEqual(len(checks.possible_junction_gaps(mask, source, roads, obstacle)), 1)
        np.testing.assert_array_equal(mask, before)
        self.assertFalse(checks.possible_junction_gaps(mask, source, roads, []))
        filled = np.full_like(mask, 255)
        self.assertFalse(checks.possible_junction_gaps(filled, source, roads, obstacle))
        cv2.line(source, (35, 112), (190, 80), 40, 2)
        self.assertFalse(checks.possible_junction_gaps(mask, source, roads, obstacle))

    def test_closure_alias_requires_both_adjacent_edges(self):
        item = dict(polygon=[[0, 0], [8, 0], [8, 8], [0, 8]], fixed_edges=[0, 3, 4])
        submission.validate_fixed_edges(item)
        self.assertEqual(item["fixed_edges"], [0, 3])
        for bad in ([3, 4], [0, 4], [0, 3, 5], [-1], [True], [0, 3, 4.0]):
            with self.subTest(edges=bad), self.assertRaises(ValueError):
                submission.validate_fixed_edges(dict(item, fixed_edges=bad))
