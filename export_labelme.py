"""Export a street workflow run for manual review using LabelMe 7.11."""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image


def review_image(run: Path) -> Path:
    config = json.loads((run / "config.json").read_text())
    original = Path(config.get("source_original") or "")
    if original.is_absolute() and original.is_file():
        setup = run / "setup.json"
        digest = json.loads(setup.read_text()).get("original_sha256") if setup.exists() else None
        with Image.open(original) as image:
            # The runner segments raw pixels, while LabelMe applies EXIF rotation.
            same_orientation = image.getexif().get(274, 1) == 1
        if same_orientation and digest:
            with original.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                    return original
    return run / "source.png"


def export_run(run: Path, output_dir: Path | None = None) -> Path:
    run = run.resolve()
    image_path = review_image(run)
    directory = (output_dir or run / "labelme").resolve()
    target = directory / image_path.with_suffix(".json").name
    return write_mask_json(run / "mask.png", image_path, target)


def write_mask_json(mask_path: Path, image_path: Path, target: Path) -> Path:
    if target.exists():
        raise FileExistsError(f"Review already exists: {target}")
    shapes = []
    with Image.open(mask_path) as mask, Image.open(image_path) as image:
        if mask.size != image.size:
            raise ValueError(f"Mask size {mask.size} differs from image size {image.size}")
        if mask.mode not in ("1", "L") or any(v not in (0, 1, 255) for _, v in mask.getcolors()):
            raise ValueError("Expected a binary grayscale workflow mask")
        width, height = mask.size
        bounds = mask.getbbox()
        if bounds:
            left, top, right, bottom = bounds
            encoded = io.BytesIO()
            # LabelMe's native PNG stores one byte per pixel, with foreground equal to 1.
            mask.crop(bounds).convert("L").point([0] + [1] * 255).save(encoded, format="PNG")
            shapes.append(
                dict(
                    label="st1",
                    shape_type="mask",
                    group_id=None,
                    description="",
                    points=[[left, top], [right - 1, bottom - 1]],
                    flags={},
                    mask=base64.b64encode(encoded.getvalue()).decode("ascii"),
                )
            )
    document = dict(
        version="7.11.0",
        flags={},
        shapes=shapes,
        imagePath=os.path.relpath(image_path, target.parent),
        imageData=None,
        imageHeight=height,
        imageWidth=width,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent) as tmp:
        staged = Path(tmp) / target.name
        staged.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        os.link(staged, target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Workflow run directory containing mask.png")
    parser.add_argument("--output-dir", type=Path, help="Defaults to RUN/labelme")
    parser.add_argument("--open", action="store_true", help="Open the exported JSON in LabelMe")
    args = parser.parse_args()
    try:
        target = export_run(args.run, args.output_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(target, flush=True)
    if args.open:
        sys.exit(subprocess.call([sys.executable, "-m", "labelme", str(target)]))
