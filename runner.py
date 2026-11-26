#!/usr/bin/env python3
"""Independent street labeling: conditional renderer + Responses agent loop."""
import argparse
import base64
import email.utils
import fcntl
import hashlib
import io
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DEFAULT_WORKFLOW = "scoped_paths_20261122"
WORKFLOW = ROOT / "workflow"
PYTHON = PROJECT / ".venv/bin/python"
CASE = ROOT / "runs/NY_8_api"
KEY = ROOT / "secrets/openai_api_key.txt"
MODEL = "gpt-6.2-sol"
REASONING_EFFORT = "medium"
MODES = ("initial", "max")
MAP_NAME = "NY_8"
MAP_SOURCE = ROOT / "maps/new_york/8.tif"
MAPS = {
    "NY_8": ROOT / "maps/new_york/8.tif",
    "SF_8": ROOT / "maps/san_francisco/8.tif",
    "ATLANTA_303": ROOT / "maps/atlanta/303.jpg",
}
Image.MAX_IMAGE_PIXELS = None


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append(path, obj):
    with Path(path).open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for part in iter(lambda: f.read(1048576), b""):
            h.update(part)
    return h.hexdigest()


def read_instructions(path, expected_sha256=None):
    # A cloud-backed file can briefly read as zero bytes while its directory
    # entry already advertises its full size. Retry before making an API call.
    for attempt in range(3):
        size = path.stat().st_size
        data = path.read_bytes()
        if len(data) == size and data.strip():
            if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
                raise ValueError(f"Prepared instructions changed: {path.name}")
            return data.decode("utf-8")
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    raise OSError(f"Could not read complete instructions from {path}")


def stage():
    p = CASE / "stages.jsonl"
    return (
        json.loads(p.read_text().splitlines()[-1])["stage"]
        if p.exists() and p.stat().st_size
        else "agent_initialization"
    )


def setup(mode="initial"):
    street_policy = read_instructions(ROOT / "workflow/street_policy.txt").strip()
    template = read_instructions(ROOT / "workflow/prompt.txt")
    if "{street_policy}" not in template:
        raise ValueError("Workflow prompt is missing its street policy field")
    CASE.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    shutil.copytree(WORKFLOW / "src", CASE / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("core.py", "conditional.py", "initial_tools.py", "api_tools.json"):
        shutil.copy2(WORKFLOW / name, CASE / name)
    source = MAP_SOURCE
    with Image.open(source) as im:
        rgb = im.convert("RGB")
        rgb.save(CASE / "source.png")
        size = rgb.size
        rgb.thumbnail((1600, 1800))
        rgb.save(CASE / "overview.jpg", quality=95)
    save(
        CASE / "config.json",
        {
            "map": MAP_NAME,
            "method": "new",
            "variant": "conditional_source_batch_api",
            "size_wh": size,
            "model": MODEL,
            "effort": REASONING_EFFORT,
            "mode": mode,
            "source_batch_tools": True,
            "native_detail_tool": True,
            "workflow_version": DEFAULT_WORKFLOW,
            "street_policy": street_policy,
            "source_original": str(source),
        },
    )
    sys.path.insert(0, str(CASE / "src"))
    import workflow as wf

    work = CASE / "run/maps" / MAP_NAME
    state = wf.init(work, CASE / "source.png", tile=2304, overlap=192)
    (CASE / "run/reviews").mkdir()
    for t in state["tiles"]:
        wf.make_crop(work, state, t)
    batches = [
        {
            "batch": i // 4 + 1,
            "tiles": [
                {
                    "id": t["id"],
                    "native_box": t["box"],
                    "edge_sides": t["edge_sides"],
                    "image": str(work / "source_tiles" / (t["id"] + ".png")),
                }
                for t in state["tiles"][i : i + 4]
            ],
        }
        for i in range(0, len(state["tiles"]), 4)
    ]
    save(CASE / "source_batches.json", batches)
    original = template.format(
        py=str(PYTHON),
        model=MODEL,
        effort=REASONING_EFFORT,
        width=size[0],
        height=size[1],
        street_policy=street_policy,
    )
    if mode == "initial":
        initial, marker, _ = original.partition("\n3 FIRST_REVIEW:")
        if not marker:
            raise ValueError("Missing first-review stage in workflow prompt")
        original = initial.rstrip() + (
            "\n\nInitial Mask mode ends after build inventory_r1 succeeds. "
            "The runner exports the initial mask and overlay after checking source coverage. "
            "Do not start visual review or build another revision.\n"
        )
    (CASE / "prompt.txt").write_text(original)
    save(
        CASE / "setup.json",
        {
            "seconds": time.monotonic() - start,
            "tiles": len(state["tiles"]),
            "source_sha256": digest(CASE / "source.png"),
            "original_sha256": digest(source),
            "prompt_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "agent": {"model": MODEL, "effort": REASONING_EFFORT},
            "source_tools_sha256": digest(CASE / "initial_tools.py"),
            "api_tools_sha256": digest(CASE / "api_tools.json"),
            "engine_files": {p.name: digest(p) for p in sorted((CASE / "src").glob("*.py"))},
        },
    )
    print("prepared independent case; source tiles:", len(state["tiles"]), flush=True)


# The command sandbox restricts reads/writes inside the user's home to the
# independent case and project Python environment, and forbids network access.
def sandbox_profile():
    def quote(p):
        return json.dumps(str(Path(p).resolve()))

    home = Path.home().resolve()
    ancestors = set()
    for target in (CASE.resolve(), (PROJECT / ".venv").resolve()):
        for parent in target.parents:
            if parent == home or home in parent.parents:
                ancestors.add(parent)
    metadata = "".join(
        "(allow file-read-metadata (literal " + quote(parent) + "))\n"
        for parent in sorted(ancestors)
    )
    return (
        "(version 1)\n(allow default)\n(deny network*)\n(deny file-read* file-write* (subpath "
        + quote(home)
        + "))\n"
        + metadata
        + "(allow file-read* file-write* (subpath "
        + quote(CASE)
        + "))\n(allow file-read* (subpath "
        + quote(PROJECT / ".venv")
        + "))\n"
    )


def run_command(command, *, stdin=None, call_id=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
    }
    env.update(HOME=str(CASE), TMPDIR=str(CASE), PYTHONDONTWRITEBYTECODE="1")
    start = time.monotonic()
    try:
        p = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", sandbox_profile(), "/bin/zsh", "-c", command],
            cwd=CASE,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=600,
        )
        result = {
            "exit_code": p.returncode,
            "stdout": p.stdout[-45000:],
            "stderr": p.stderr[-12000:],
        }
    except subprocess.TimeoutExpired:
        result = {"error": "command timed out after 600 seconds"}
    append(
        CASE / "tool_calls.jsonl",
        {
            "stage": stage(),
            "tool": "run_command",
            "call_id": call_id,
            "command": command,
            "seconds": time.monotonic() - start,
            "exit_code": result.get("exit_code"),
            "result": result,
        },
    )
    return result


def artifact_state():
    """List small agent output files without reading full contents."""
    excluded = {
        "config.json",
        "setup.json",
        "source_batches.json",
        "execution.json",
        "failure.json",
        "rate_limit_checkpoint.json",
        "recovery_inflight.json",
        "prompt.txt",
    }
    result = {}
    for p in CASE.iterdir():
        try:
            if (
                p.suffix.lower() not in (".json", ".txt", ".md")
                or p.name in excluded
                or not p.resolve().is_relative_to(CASE.resolve())
                or not p.is_file()
            ):
                continue
            st = p.stat()
            result[p.name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return result


def snapshot_artifacts(before, seq, call_id):
    """Preserve changed outputs after a tool call; failures never change the tool result."""
    try:
        after = artifact_state()
        saved = []
        for name, signature in after.items():
            if before.get(name) == signature or signature[1] > 5_000_000:
                continue
            try:
                data = (CASE / name).read_bytes()
                kind = "json" if Path(name).suffix.lower() == ".json" else "text"
                if kind == "json":
                    json.loads(data)
                else:
                    data.decode("utf-8")
                sha = hashlib.sha256(data).hexdigest()
                directory = CASE / "agent_artifacts"
                directory.mkdir(exist_ok=True)
                target = directory / (sha + (".json" if kind == "json" else ".txt"))
                if not target.exists():
                    temp = directory / (sha + "." + str(time.time_ns()) + ".tmp")
                    try:
                        with temp.open("xb") as f:
                            f.write(data)
                        os.replace(temp, target)
                    finally:
                        temp.unlink(missing_ok=True)
                saved.append(
                    {
                        "name": name,
                        "sha256": sha,
                        "path": str(target.relative_to(CASE)),
                        "format": kind,
                    }
                )
            except (OSError, ValueError, UnicodeDecodeError):
                continue
        if saved:
            append(
                CASE / "artifact_events.jsonl",
                {"seq": seq, "call_id": call_id, "stage_after": stage(), "artifacts": saved},
            )
    except Exception as e:
        print("Could not preserve an agent output snapshot: " + type(e).__name__, flush=True)


def image_output(paths, record=True, call_id=None):
    if not 1 <= len(paths) <= 4:
        raise ValueError("one to four images per call")
    result = []
    records = []
    snapshots = []
    for raw in paths:
        p = Path(raw)
        p = (CASE / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_relative_to(CASE) or not p.is_file():
            raise ValueError("image must be inside independent case")
        with Image.open(p) as im:
            im = im.convert("RGB")
            native = im.size
            im.thumbnail((1600, 1600))
            b = io.BytesIO()
            im.save(b, format="PNG")
            meta = {
                "path": str(p.relative_to(CASE)),
                "file_sha256": digest(p),
                "native_size": native,
                "display_size": im.size,
            }
        data = b.getvalue()
        records.append(meta)
        result.append({"type": "input_text", "text": json.dumps(meta)})
        result.append(
            {
                "type": "input_image",
                "detail": "high",
                "image_url": "data:image/png;base64," + base64.b64encode(data).decode(),
            }
        )
        if record:
            snapshots.append((meta, data))
    if record:
        append(
            CASE / "image_receipts.jsonl", {"stage": stage(), "images": records, "call_id": call_id}
        )
        try:
            directory = CASE / "image_snapshots"
            directory.mkdir(exist_ok=True)
            saved = []
            for meta, data in snapshots:
                sha = hashlib.sha256(data).hexdigest()
                p = directory / (sha + ".png")
                try:
                    with p.open("xb") as f:
                        f.write(data)
                except FileExistsError:
                    pass
                saved.append(
                    {
                        "source_sha256": meta["file_sha256"],
                        "display_size": meta["display_size"],
                        "snapshot_sha256": sha,
                        "path": str(p.relative_to(CASE)),
                    }
                )
            append(CASE / "image_snapshots.jsonl", {"images": saved})
        except OSError as e:
            print(
                "Could not save image snapshot; model call continues: " + type(e).__name__,
                flush=True,
            )
    return result


TOOLS = [
    {
        "type": "function",
        "name": "run_command",
        "description": "Run local shell command in independent case; return stdout, stderr and exit code. Use this to read and write files and invoke original renderer.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "view_images",
        "description": "Actually view one to four local images from the case, including native source, overlay and detail crops. Returns image pixels and metadata.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
]


def sandbox_test():
    a = run_command(
        "test -r source.png && test -r conditional.py && test ! -r "
        + str(KEY)
        + " && test ! -r "
        + str(ROOT / "workflow/conditional.py")
        + " && "
        + str(PYTHON)
        + " -c \"import PIL,numpy,cv2; print('sandbox_ok')\""
    )
    if a.get("exit_code") != 0 or "sandbox_ok" not in a.get("stdout", ""):
        raise RuntimeError("isolated sandbox check failed: " + json.dumps(a))
    print("independent read isolation and local interpreter verified", flush=True)


class ConfirmedRateLimit(RuntimeError):
    """A rejected temporary 429; safe to retain the unsent continuation."""


def transient_rate_limit(error):
    code = str(error.get("code") or "").lower()
    kind = str(error.get("type") or "").lower()
    msg = str(error.get("message") or "").lower()
    if any(x in code + " " + kind + " " + msg for x in ("quota", "credit", "spend", "billing")):
        return False
    return (
        code in ("rate_limit_exceeded", "rate_limit_reached", "slow_down", "tokens", "requests")
        or kind == "rate_limit_error"
        or "rate limit reached" in msg
    )


def retry_hint(headers, message):
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (TypeError, ValueError):
            try:
                return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError, AttributeError):
                pass
    value = headers.get("retry-after-ms") if headers else None
    if value:
        try:
            seconds = float(value) / 1000
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (TypeError, ValueError):
            pass
    match = re.search(
        r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s|seconds?)\b", str(message), re.I
    )
    if match:
        return float(match.group(1)) / (1000 if match.group(2).lower() == "ms" else 1)
    return None


def post(key, payload, max_attempts=12, max_wait=240):
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    wait_total = 0.0
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=900) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            try:
                error = json.loads(e.read()).get("error", {})
                error = error if isinstance(error, dict) else {}
            except Exception:
                error = {}
            msg = str(error.get("message", "")).replace(key, "[REDACTED]")[:1000]
            detail = "Responses API HTTP " + str(e.code) + ": " + msg
            if e.code != 429 or not transient_rate_limit(error):
                raise RuntimeError(detail) from None
            hint = retry_hint(e.headers, msg)
            delay = max(hint or 0, min(30.0, 2.0 ** (attempt - 1))) + random.uniform(0.1, 0.8)
            if attempt >= max_attempts or wait_total + delay > max_wait:
                append(
                    CASE / "rate_limit_events.jsonl",
                    {
                        "stage": stage(),
                        "attempt": attempt,
                        "action": "exhausted",
                        "wait_seconds": wait_total,
                        "server_hint_seconds": hint,
                    },
                )
                raise ConfirmedRateLimit(detail) from None
            append(
                CASE / "rate_limit_events.jsonl",
                {
                    "stage": stage(),
                    "attempt": attempt,
                    "action": "wait_and_retry",
                    "delay_seconds": round(delay, 3),
                    "server_hint_seconds": hint,
                },
            )
            print(
                f"API rate limit; retrying in {delay:.1f}s ({attempt}/{max_attempts-1})", flush=True
            )
            time.sleep(delay)
            wait_total += delay
        except Exception as e:
            raise RuntimeError(
                "API transport failed ("
                + type(e).__name__
                + "); do not automatically retry an ambiguously billed request"
            ) from None


def audit():
    state = json.loads((CASE / "run/maps" / MAP_NAME / "workflow.json").read_text())
    ids = {t["id"] for t in state["tiles"]}
    receipts = CASE / "image_receipts.jsonl"
    rows = [json.loads(x) for x in receipts.read_text().splitlines()] if receipts.exists() else []
    src = set()
    first = set()
    for row in rows:
        for x in row["images"]:
            p = Path(x["path"])
            if row["stage"] == "source_review" and p.parent.name == "source_tiles":
                src.add(p.stem)
            if row["stage"] == "first_review" and p.parent.name == "inventory_r1":
                first.add(p.stem)
    summary = {
        "all_source_tiles": len(src),
        "all_first_review_tiles": len(first),
        "expected_tiles": len(ids),
        "source_missing": sorted(ids - src),
        "first_review_missing": sorted(ids - first),
        "summary_exists": (CASE / "summary.json").exists(),
        "note": "Receipts establish presentation of image pixels, not semantic comprehension or ground-truth accuracy.",
    }
    save(CASE / "coverage_audit.json", summary)
    return summary


def save_junction_previews(locations):
    if not locations:
        return
    with Image.open(CASE / "source.png") as source, Image.open(CASE / "overlay.jpg") as overlay:
        for i, finding in enumerate(locations, 1):
            xs, ys = zip(*finding["native_triangle"])
            pad = max(1, max(max(xs) - min(xs), max(ys) - min(ys)) // 4)
            box = (
                max(0, min(xs) - pad),
                max(0, min(ys) - pad),
                min(source.width, max(xs) + pad + 1),
                min(source.height, max(ys) + pad + 1),
            )
            left = source.crop(box)
            right = overlay.crop(box)
            left.thumbnail((900, 900))
            right.thumbnail((900, 900))
            pair = Image.new("RGB", (2 * left.width + 2, left.height), "white")
            pair.paste(left, (0, 0))
            pair.paste(right, (left.width + 2, 0))
            finding["preview"] = f"junction_check_{i}.jpg"
            finding["native_box"] = list(box)
            pair.save(CASE / finding["preview"], quality=93)


def publish_initial(revision, coverage, config):
    mask = revision / "mask.png"
    with Image.open(mask) as image:
        if image.format != "PNG" or image.mode != "L" or list(image.size) != config["size_wh"]:
            raise ValueError("Initial mask does not match the native source dimensions or format")
        image.verify()
    for name in ("mask.png", "overlay.jpg", "overview.jpg"):
        shutil.copyfile(revision / name, CASE / name)
    proposal = json.loads((revision / "proposal.json").read_text())
    save_junction_previews(proposal.get("review_locations", []))
    save(
        CASE / "summary.json",
        {
            "map": config["map"],
            "mode": "initial",
            "selected_revision": revision.name,
            "review_mode": "initial_only",
            **(
                {"review_locations": proposal["review_locations"]}
                if "review_locations" in proposal
                else {}
            ),
            "surface_check": None,
            "source_tiles": coverage["expected_tiles"],
            "mask_sha256": digest(mask),
            "notice": "Initial street mask rendered after source review. Full-map MAX review was skipped.",
            "geometry_review": (
                json.loads((CASE / "initial_build_receipt.json").read_text())
                if (CASE / "initial_build_receipt.json").is_file()
                else None
            ),
        },
    )
    coverage["summary_exists"] = True
    coverage["first_review_required"] = False
    save(CASE / "coverage_audit.json", coverage)
    append(
        CASE / "stages.jsonl",
        {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": "complete"},
    )


def recovery_continuation():
    """Reconstruct only a definitively rejected 429, never execute old tools again."""
    failure_path = CASE / "failure.json"
    if (CASE / "execution.json").exists():
        raise ValueError("Case already completed")
    if (CASE / "recovery_inflight.json").exists():
        raise ValueError("Previous recovery outcome is ambiguous; manual reconciliation required")
    if not failure_path.is_file():
        raise ValueError("No saved failure to recover")
    failure = json.loads(failure_path.read_text())
    message = str(failure.get("error", ""))
    legacy_tpm = (
        failure.get("error_type") is None
        and "tokens per min" in message.lower()
        and "try again" in message.lower()
    )
    if (
        "HTTP 429" not in message
        or not transient_rate_limit({"message": message})
        or not (failure.get("error_type") == "ConfirmedRateLimit" or legacy_tpm)
    ):
        raise ValueError("Only a confirmed temporary 429 can be resumed automatically")
    if failure.get("stage") != stage():
        raise ValueError("Failure stage no longer matches the case")
    cfg = json.loads((CASE / "config.json").read_text())
    setup = json.loads((CASE / "setup.json").read_text())
    saved_agent = setup.get("agent", {"model": "gpt-6.1-sol", "effort": "high"})
    if cfg.get("map") != MAP_NAME or saved_agent != {
        "model": cfg.get("model"),
        "effort": cfg.get("effort"),
    }:
        raise ValueError("Map or model configuration changed")
    if digest(CASE / "source.png") != setup.get("source_sha256"):
        raise ValueError("Source image changed")
    engines = setup.get("engine_files")
    if (
        not isinstance(engines, dict)
        or not engines
        or any(Path(name).name != name for name in engines)
    ):
        raise ValueError("Missing or invalid renderer source checksums")
    for name, expected in engines.items():
        if digest(CASE / "src" / name) != expected:
            raise ValueError("Renderer source changed: " + name)
    helpers = ["core.py", "conditional.py"]
    if setup.get("source_tools_sha256"):
        if digest(CASE / "initial_tools.py") != setup["source_tools_sha256"]:
            raise ValueError("Saved source tools changed")
    elif cfg.get("source_batch_tools"):
        helpers.append("initial_tools.py")
    if (
        setup.get("api_tools_sha256")
        and digest(CASE / "api_tools.json") != setup["api_tools_sha256"]
    ):
        raise ValueError("Saved API tool definitions changed")
    for name in helpers:
        if digest(CASE / name) != digest(WORKFLOW / name):
            raise ValueError("Workflow tool changed: " + name)
    request_path = CASE / "requests.jsonl"
    rows = (
        [json.loads(x) for x in request_path.read_text().splitlines()]
        if request_path.exists()
        else []
    )
    if [x.get("seq") for x in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Request log is incomplete")
    saved = sorted((CASE / "responses").glob("*.json")) if (CASE / "responses").exists() else []
    if [p.name for p in saved] != [f"{i:04d}.json" for i in range(1, len(rows) + 1)]:
        raise ValueError("Unmatched response files; outcome may be ambiguous")
    prev = rows[-1].get("response_id") if rows else None
    if rows:
        response = json.loads(saved[-1].read_text())
        if not prev or response.get("id") != prev or response.get("status") != "completed":
            raise ValueError("Last response does not match the request log")
    checkpoint = CASE / "rate_limit_checkpoint.json"
    if checkpoint.exists():
        pending = json.loads(checkpoint.read_text())
        if (
            pending.get("seq") != len(rows) + 1
            or pending.get("previous_response_id") != prev
            or pending.get("stage") != failure.get("stage")
        ):
            raise ValueError("Saved continuation does not match recorded history")
        inputs = pending["inputs"]
    else:
        if not rows:
            raise ValueError("No saved continuation or previous model response")
        calls = [x for x in response.get("output", []) if x.get("type") == "function_call"]
        if len(calls) != 1 or not calls[0].get("call_id"):
            raise ValueError("Cannot establish one exact pending tool result")
        call = calls[0]
        args = json.loads(call["arguments"])
        if call["name"] == "run_command":
            records = [json.loads(x) for x in (CASE / "tool_calls.jsonl").read_text().splitlines()]
            last = records[-1]
            if last.get("command") != args.get("command") or not isinstance(
                last.get("result"), dict
            ):
                raise ValueError("Saved command result does not match last tool call")
            output = json.dumps(last["result"])
        elif call["name"] == "view_images":
            receipts = [
                json.loads(x) for x in (CASE / "image_receipts.jsonl").read_text().splitlines()
            ]
            last = receipts[-1]["images"]
            requested = [
                (CASE / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
                for p in args["paths"]
            ]
            if requested != [(CASE / x["path"]).resolve() for x in last]:
                raise ValueError("Image receipt does not match last tool call")
            output = image_output(args["paths"], record=False)
            if [json.loads(x["text"]) for x in output if x["type"] == "input_text"] != last:
                raise ValueError("Viewed image or presentation changed")
        else:
            raise ValueError("Unsupported pending tool call")
        inputs = [{"type": "function_call_output", "call_id": call["call_id"], "output": output}]
    return {
        "previous_response_id": prev,
        "inputs": inputs,
        "completed_requests": len(rows),
        "failure_stage": failure.get("stage"),
    }


def save_rate_checkpoint(seq, prev, inputs, before):
    p = CASE / "rate_limit_checkpoint.json"
    tmp = p.with_suffix(".tmp")
    data = {"seq": seq, "previous_response_id": prev, "inputs": inputs, "stage": before}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def build_response_request(
    inputs,
    previous_response_id,
    *,
    model=MODEL,
    effort=REASONING_EFFORT,
    mode="max",
    case_tools=None,
):
    """Build the next Responses request without changing the agent contract."""
    tools = (
        TOOLS
        if case_tools is None
        else [tool for tool in case_tools if mode == "max" or tool["name"] != "run_command"]
    )
    body = {
        "model": model,
        "reasoning": {"effort": effort},
        "input": inputs,
        "tools": tools,
        "parallel_tool_calls": False,
        "store": True,
        "max_output_tokens": 32768,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def execute_tool_call(call, sequence):
    """Execute one agent tool call and return its Responses-compatible output."""
    try:
        args = json.loads(call["arguments"])
        name = call["name"]
        if name in ("run_command", "record_source_batch", "view_source_details", "submit_roads"):
            try:
                before_artifacts = artifact_state()
            except Exception:
                before_artifacts = {}
            if name == "run_command":
                output = json.dumps(run_command(args["command"]))
            else:
                result = run_command(
                    f"{shlex.quote(str(PYTHON))} initial_tools.py {name}",
                    stdin=json.dumps(args),
                    call_id=call["call_id"],
                )
                if result.get("exit_code") != 0:
                    output = json.dumps(
                        {"error": result.get("stderr") or result.get("error") or result}
                    )
                else:
                    reply = json.loads(result["stdout"].splitlines()[-1])
                    if "tiles" in reply:
                        paths = [tile["path"] for tile in reply["tiles"]]
                        output = [{"type": "input_text", "text": json.dumps(reply)}]
                        output.extend(image_output(paths, call_id=call["call_id"]))
                    else:
                        output = json.dumps(reply)
            snapshot_artifacts(before_artifacts, sequence, call["call_id"])
        elif name == "view_images":
            output = image_output(args["paths"])
        else:
            output = json.dumps({"error": "unknown tool"})
    except Exception as error:
        output = json.dumps({"error": type(error).__name__ + ": " + str(error)})
    return {"type": "function_call_output", "call_id": call["call_id"], "output": output}


def run(resume=False):
    if not resume and (CASE / "requests.jsonl").exists():
        raise RuntimeError("Case already has API requests; choose a new --run-id.")
    config = json.loads((CASE / "config.json").read_text())
    mode = config.get("mode", "max")
    scoped_paths = config.get("workflow_version") == DEFAULT_WORKFLOW
    case_tools = json.loads((CASE / "api_tools.json").read_text()) if scoped_paths else None
    initial_revision = CASE / "run/maps" / MAP_NAME / "tools/inventory_r1"
    if resume:
        recovered = recovery_continuation()
        prev = recovered["previous_response_id"]
        inputs = recovered["inputs"]
        i = recovered["completed_requests"]
    else:
        prev = None
        setup_path = CASE / "setup.json"
        setup_record = json.loads(setup_path.read_text()) if setup_path.is_file() else {}
        inputs = [
            {
                "role": "user",
                "content": read_instructions(
                    CASE / "prompt.txt", setup_record.get("prompt_sha256")
                ),
            }
        ]
        i = 0
    key = os.environ.get("OPENAI_API_KEY") or KEY.read_text().strip()
    if not key:
        raise RuntimeError("OpenAI API key missing")
    if resume:
        history = CASE / "failure_history"
        history.mkdir(exist_ok=True)
        os.replace(CASE / "failure.json", history / (str(time.time_ns()) + ".json"))
        append(
            CASE / "recovery_events.jsonl",
            {
                "action": "resume_confirmed_429",
                "after_request": i,
                "stage": recovered["failure_stage"],
                "utc_unix": time.time(),
            },
        )
        print(
            f"Resuming after response {i}; completed tool calls will not be repeated.", flush=True
        )
    else:
        sandbox_test()
    started = time.monotonic()
    first_recovery_request = resume
    while True:
        i += 1
        before = stage()
        t = time.monotonic()
        body = build_response_request(
            inputs,
            prev,
            model=config["model"],
            effort=config["effort"],
            mode=mode,
            case_tools=case_tools,
        )
        if first_recovery_request:
            save(
                CASE / "recovery_inflight.json",
                {"seq": i, "previous_response_id": prev, "started_unix": time.time()},
            )
        try:
            r = post(key, body)
        except ConfirmedRateLimit:
            save_rate_checkpoint(i, prev, inputs, before)
            if first_recovery_request:
                (CASE / "recovery_inflight.json").unlink(missing_ok=True)
            raise
        save(CASE / "responses" / f"{i:04d}.json", r)
        append(
            CASE / "requests.jsonl",
            {
                "seq": i,
                "stage": before,
                "scope": before,
                "seconds": time.monotonic() - t,
                "response_id": r.get("id"),
                "model": r.get("model"),
                "effort": config["effort"],
                "status": r.get("status"),
                "usage": r.get("usage", {}),
            },
        )
        if first_recovery_request:
            (CASE / "recovery_inflight.json").unlink(missing_ok=True)
            (CASE / "rate_limit_checkpoint.json").unlink(missing_ok=True)
            first_recovery_request = False
        if r.get("status") != "completed":
            raise RuntimeError("API response incomplete; inspect response " + str(i))
        prev = r["id"]
        calls = [x for x in r.get("output", []) if x.get("type") == "function_call"]
        inputs = [execute_tool_call(call, i) for call in calls]
        initial_built = mode == "initial" and all(
            (initial_revision / name).is_file()
            for name in ("mask.png", "overlay.jpg", "overview.jpg", "proposal.json")
        )
        if initial_built and config.get("source_batch_tools"):
            initial_built = (CASE / "initial_build_receipt.json").is_file()
        if initial_built or not calls:
            coverage = audit()
            if mode == "initial":
                if initial_built and not coverage["source_missing"]:
                    publish_initial(initial_revision, coverage, config)
                    break
                instruction = (
                    "View missing original tiles during source_review and complete the "
                    "inventory_r1 build. "
                    + (
                        "Initial Mask mode ends there."
                        if scoped_paths
                        else "Follow the next decision requested by the runner."
                    )
                )
            elif (
                coverage["summary_exists"]
                and not coverage["source_missing"]
                and not coverage["first_review_missing"]
            ):
                break
            else:
                instruction = (
                    "Inspect missing tiles or complete finish, without inventing observations."
                )
            inputs.append(
                {
                    "role": "user",
                    "content": f"Workflow is incomplete. Coverage audit: {json.dumps(coverage)}. "
                    + instruction,
                }
            )
        if i % 5 == 0:
            print(
                "responses:",
                i,
                "stage:",
                stage(),
                "elapsed_s:",
                round(time.monotonic() - started),
                flush=True,
            )
        if i >= 180:
            raise RuntimeError("180-request limit reached; preserve partial artifacts")
    save(
        CASE / "execution.json",
        {
            "status": "completed",
            "mode": mode,
            "seconds": time.monotonic() - started,
            "seconds_scope": "resume segment" if resume else "full agent run",
            "resumed": resume,
            "requests": i,
            "coverage": coverage,
        },
    )
    print(
        (
            "initial mask complete"
            if mode == "initial"
            else "workflow complete; visual independent review still required"
        ),
        flush=True,
    )


def main():
    global CASE, MAP_NAME, MAP_SOURCE
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["setup", "sandbox-test", "run", "resume", "audit"])
    p.add_argument(
        "--map", default="NY_8", help="Case/map ID; a built-in map or a custom ID with --source"
    )
    p.add_argument(
        "--source", type=Path, help="Original map for setup; required for a custom map ID"
    )
    p.add_argument(
        "--run-id", default="", help="Optional unique suffix to avoid overwriting another run"
    )
    p.add_argument("--mode", choices=MODES, help="Setup mode. Default: initial.")
    a = p.parse_args()
    if a.mode and a.action != "setup":
        p.error("--mode is set at setup")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.map) or (
        a.run_id and not re.fullmatch(r"[A-Za-z0-9_-]+", a.run_id)
    ):
        p.error("map and run-id must contain only letters, numbers, underscore or hyphen")
    MAP_NAME = a.map
    CASE = ROOT / "runs" / (MAP_NAME + "_api" + ("_" + a.run_id if a.run_id else ""))
    if a.source:
        MAP_SOURCE = a.source.resolve()
    elif MAP_NAME in MAPS:
        MAP_SOURCE = MAPS[MAP_NAME]
    elif a.action == "setup":
        p.error("--source is required for a custom map")
    if a.action == "setup" and not MAP_SOURCE.is_file():
        p.error("source image does not exist")
    if a.action == "setup":
        setup(a.mode or "initial")
    elif a.action == "sandbox-test":
        sandbox_test()
    elif a.action == "audit":
        print(json.dumps(audit(), indent=2))
    else:
        if not (CASE / "config.json").is_file():
            print(
                "ERROR: case not prepared; run setup with the same --map and --run-id first.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if a.action == "run" and (CASE / "requests.jsonl").exists():
            print(
                "ERROR: case already has requests; choose a new --run-id or use validated resume for a confirmed 429.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        with (CASE / ".runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("ERROR: another runner is active for this case.", file=sys.stderr)
                raise SystemExit(1)
            try:
                run(resume=a.action == "resume")
            except Exception as e:
                if not (CASE / "failure.json").exists():
                    save(
                        CASE / "failure.json",
                        {
                            "status": "failed",
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "stage": stage(),
                        },
                    )
                print("ERROR:", str(e), file=sys.stderr)
                raise SystemExit(1)


if __name__ == "__main__":
    main()
