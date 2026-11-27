"""Apply a visually confirmed local addition from a second Initial Mask."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image


def read(path):
    return json.loads(path.read_text())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def apply_review(base, alternate, location, output, review=None):
    summary = read(base / "summary.json")
    second = read(alternate / "summary.json")
    findings = read(review) if review else summary
    if any(s.get("selected_revision") != "inventory_r1" for s in (summary, second)):
        raise ValueError("A reviewed initial junction requires two Initial Masks at inventory_r1")
    if digest(base / "source.png") != digest(alternate / "source.png"):
        raise ValueError("The masks must use identical source images")
    for folder, s in ((base, summary), (alternate, second)):
        if digest(folder / "mask.png") != s["mask_sha256"]:
            raise ValueError("The mask has changed since export")
    if (findings["mask_sha256"], findings["map"]) != (summary["mask_sha256"], summary["map"]):
        raise ValueError("Review locations belong to a different mask")
    locations = findings.get("review_locations", [])
    if not 1 <= location <= len(locations):
        raise ValueError("Choose a location listed in the base mask's review")
    selected = locations[location - 1]
    if selected["kind"] != "possible_open_junction":
        raise ValueError("Expected a reviewed street merge")

    revision = base / "run" / "maps" / summary["map"] / "tools/inventory_r1"
    geometry = revision / "agent_input.json"
    if digest(geometry) != read(revision / "proposal.json")["agent_input_sha256"]:
        raise ValueError("The inventory no longer matches the rendered geometry")
    inventory = read(geometry)
    roads = {r["id"]: r for r in inventory["roads"]}
    with Image.open(base / "mask.png") as im, Image.open(alternate / "mask.png") as alt:
        if im.mode != "L" or alt.mode != "L" or im.size != alt.size:
            raise ValueError("Initial Mask pixel formats or dimensions differ")
        result = np.array(im)
        triangle = np.asarray(selected["native_triangle"], np.int32)
        x, y, w, h = cv2.boundingRect(triangle)
        area = result[y : y + h, x : x + w]
        local = triangle - np.int32([x, y])
        stencil = np.zeros(area.shape, np.uint8)
        cv2.fillConvexPoly(stencil, local, 255)
        radius = min(roads[k].get("radius", 32) for k in selected["roads"])
        cv2.line(stencil, tuple(local[0]), tuple(local[1]), 0, 4 * radius + 1)
        for item in inventory["exclusions"]:
            ring = np.asarray(item["polygon"], np.int32) - [x, y]
            ring = ring.astype(np.int32)
            cv2.fillPoly(stencil, [ring], 0)
            cv2.polylines(stencil, [ring], True, 0, 2 * item.get("radius", 10) + 1)
        other = np.asarray(alt.crop((x, y, x + area.shape[1], y + area.shape[0])))
        area[(stencil > 0) & (other > 0)] = 255

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as folder:
        staged = Path(folder) / "mask.png"
        Image.fromarray(result).save(staged)
        os.link(staged, output)  # Existing results and hand edits are preserved.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("alternate", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--location", type=int, required=True, help="One-based index in review_locations"
    )
    parser.add_argument(
        "--review", type=Path, help="Matching review record from an offline replay, if needed"
    )
    args = parser.parse_args()
    apply_review(args.base, args.alternate, args.location, args.output, args.review)
