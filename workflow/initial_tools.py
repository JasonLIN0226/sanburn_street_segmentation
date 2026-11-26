"""Save agent-authored observations and geometry inside the independent case."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    target = Path(path)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, target)


def viewed_tiles():
    path = Path("image_receipts.jsonl")
    return {
        image["path"]
        for line in (path.read_text().splitlines() if path.exists() else [])
        for row in [json.loads(line)] if row["stage"] == "source_review"
        for image in row["images"]
    }


def tile_path(tile):
    return str(Path(tile["image"]).resolve().relative_to(Path.cwd().resolve()))


def draft(group):
    return Path(f"draft_batch_{group}.json")


def batch_notes(args, batches):
    group = args["batch"]
    if type(group) is not int or not 0 <= group <= len(batches):
        raise ValueError("batch must be 0 to start, or the displayed group number")
    if group > 1 and not draft(group - 1).is_file():
        raise ValueError("previous batch observations missing")
    tiles = batches[group - 1]["tiles"] if group else []
    notes = args["observations"]
    if len(notes) != len(tiles) or {n["tile_id"] for n in notes} != {t["id"] for t in tiles}:
        raise ValueError("supply exactly one observation per displayed tile")
    if group:
        if not {tile_path(t) for t in tiles}.issubset(viewed_tiles()):
            raise ValueError("source pixels must be viewed before recording observations")
        values = {}
        for note in notes:
            if not all(isinstance(note.get(k), str) and note[k].strip()
                       for k in ("observation", "nonroad", "edge")):
                raise ValueError("each tile needs observation, nonroad and edge evidence")
            if not isinstance(note["roads"], list) or not all(isinstance(v, str) for v in note["roads"]):
                raise ValueError("roads should be an array of road IDs")
            values[note["tile_id"]] = {k: note[k] for k in ("roads", "observation", "nonroad", "edge")}
        write(draft(group), values)
    elif not Path("stages.jsonl").exists():
        import core
        core.mark("source_review")
    if group == len(batches):
        return {"message": "All tile notes saved. Inspect any needed native detail, then submit roads."}
    following = batches[group]
    return {"batch": following["batch"], "tiles": [
        {"id": t["id"], "box": t["native_box"], "sides": t["edge_sides"], "path": tile_path(t)}
        for t in following["tiles"]
    ]}


def source_details(args, _batches):
    # Use the same native cropper as MAX review and preserve each requested set.
    from conditional import views

    regions = args["regions"]
    if not 1 <= len(regions) <= 4:
        raise ValueError("request one to four native detail boxes at a time")
    key = hashlib.sha256(json.dumps(regions, sort_keys=True).encode()).hexdigest()[:16]
    request = Path(f"source_detail_{key}.json")
    write(request, {"regions": regions})
    views(request)
    manifest = read(Path("detail_batches") / request.stem / "manifest.json")
    return {"tiles": [
        {"id": r["id"], "box": r["native_box"], "path": str(Path(r["images"][0]).relative_to(Path.cwd()))}
        for r in manifest["regions"]
    ]}


def validate_fixed_edges(item):
    count = len(item["polygon"])
    edges = item.get("fixed_edges", [])
    if not isinstance(edges, list) or any(type(i) is not int for i in edges):
        raise ValueError("fixed_edges must be a list of integer starting vertex indices")
    invalid = {i for i in edges if not 0 <= i < count}
    if not invalid:
        return
    # At the closed-ring seam, vertex N repeats vertex 0. Accept this alias
    # only when both incident segments are already explicitly fixed.
    if invalid == {count} and 0 in edges and count - 1 in edges:
        item["fixed_edges"] = [i for i in edges if i != count]
        return
    raise ValueError("fixed_edges indexes start at zero and must refer to polygon vertices")


def submit(args, batches):
    cfg = read("config.json")
    width, height = cfg["size_wh"]
    notes = {}
    seen = viewed_tiles()
    for group in batches:
        tiles = group["tiles"]
        path = draft(group["batch"])
        if not path.exists() or not {tile_path(t) for t in tiles}.issubset(seen):
            raise ValueError(f"batch {group['batch']} lacks observations or viewed pixels")
        values = read(path)
        if set(values) != {t["id"] for t in tiles}:
            raise ValueError(f"incomplete draft for batch {group['batch']}")
        notes.update(values)
    roads = args["roads"]
    ids = {r["id"] for r in roads}
    if len(ids) != len(roads):
        raise ValueError("road IDs must be unique")
    aliases = args.get("aliases", {})
    for value in notes.values():
        value["roads"] = list(dict.fromkeys(aliases.get(r, r) for r in value["roads"]))
    missing = {r for value in notes.values() for r in value["roads"]} - ids
    if missing:
        raise ValueError(f"missing road IDs {sorted(missing)}; supply roads or aliases")
    for road in roads:
        if not all(road.get(k) for k in ("id", "evidence", "continuation")):
            raise ValueError("each road needs id, evidence and continuation")
        if not isinstance(road.get("obstacles"), list) or not isinstance(road.get("exits"), list):
            raise ValueError("each road needs obstacles and exits lists")
        if any(not (e.get("id") and e.get("evidence")) for e in road["exits"]):
            raise ValueError("record an id and evidence for each exit")
    for item in [*roads, *args["exclusions"]]:
        points = item["polygon"]
        if len(points) < 3 or any(
            len(p) != 2 or not all(type(v) is int for v in p)
            or not (0 <= p[0] <= width and 0 <= p[1] <= height) for p in points
        ):
            raise ValueError("polygon needs at least three native integer [x,y] vertices within the map")
        validate_fixed_edges(item)
        if type(item.get("radius", 12)) is not int or item.get("radius", 12) < 0:
            raise ValueError("radius must be a nonnegative integer")
    inventory = {**args, "tiles": notes}
    inventory.pop("aliases", None)
    import core
    target = Path("run/maps") / cfg["map"] / "tools/inventory_r1"
    completed = Path("initial_build_receipt.json")
    if completed.exists():
        if inventory == read("inventory_r1.json"):
            return read(completed)
        raise ValueError("initial build already completed")
    if target.exists():
        # Preserve partial output for diagnosis and let the renderer retry a fresh revision.
        target.rename(target.with_name(f"partial_r1_{time.time_ns()}"))
    write("inventory_r1.json", inventory)
    core.mark("initial_build")
    core.build_all(["inventory_r1.json"])
    receipt = {"revision": "inventory_r1", "mask_sha256": core.wf.sha(target / "mask.png")}
    write(completed, receipt)
    return receipt


if __name__ == "__main__":
    action = {"record_source_batch": batch_notes, "view_source_details": source_details, "submit_roads": submit}[sys.argv[1]]
    try:
        result = action(json.load(sys.stdin), read("source_batches.json"))
        print(json.dumps(result))
    except (KeyError, TypeError, ValueError) as error:
        sys.exit(str(error))
