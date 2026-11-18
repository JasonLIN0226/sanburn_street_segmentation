"""Map discovery and run-backed job state for the local workflow UI."""

import fcntl
import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

# Re-export shared helpers for existing UI modules and integrations.
from storage import atomic_json, load_rows, read_json
from usage import MODEL, PRICE, cost_estimate

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "maps"
RUNS = ROOT / "runs"
STATE = ROOT / ".ui_state"
REGIONS = {
    "new_york": "New York",
    "san_francisco": "San Francisco",
    "atlanta": "Atlanta",
    "austin": "Austin",
    ".": "Other maps",
}
EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".jp2"}


@contextmanager
def job_guard(state, job_id):
    lock = Path(state) / "locks" / (job_id + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cancelled(state, job_id):
    return (Path(state) / "cancelled" / (job_id + ".json")).exists()


def map_catalog():
    items = []
    if not MAPS.exists():
        return items
    for p in MAPS.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTENSIONS:
            continue
        try:
            if not p.resolve().is_relative_to(MAPS.resolve()):
                continue
        except OSError:
            continue
        rel = p.relative_to(MAPS)
        region = rel.parts[0] if len(rel.parts) > 1 else "."
        items.append(
            {
                "id": rel.as_posix(),
                "region": region,
                "region_label": REGIONS.get(region, region),
                "label": p.stem,
                "file": p.name,
                "format": p.suffix.lower()[1:],
            }
        )

    def key(v):
        parts = re.split(r"(\d+)", v["label"].lower())
        return (
            list(REGIONS).index(v["region"]) if v["region"] in REGIONS else 99,
            [(0, int(p)) if p.isdigit() else (1, p) for p in parts],
            v["file"],
        )

    return sorted(items, key=key)


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, TypeError, ProcessLookupError, PermissionError):
        return False


def job_snapshot(job):
    job = dict(job)
    parts = Path(job.get("map_path", "")).parts
    region = parts[0] if len(parts) > 1 else "."
    if region in REGIONS:
        job["region_label"] = REGIONS[region]
    legacy_errors = {
        "\u539f\u56fe\u4e0d\u5b58\u5728\u6216\u4e0d\u5728 maps \u6587\u4ef6\u5939": "Source map is missing or outside the maps directory.",
        "\u51c6\u5907\u9636\u6bb5\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u65e5\u5fd7": "Preparation failed; check the log.",
        "\u5de5\u4f5c\u6d41\u672a\u5b8c\u6210\uff0c\u8bf7\u67e5\u770b\u65e5\u5fd7": "Workflow incomplete; check the log.",
        "\u65e0\u6cd5\u542f\u52a8\u672c\u5730\u8c03\u5ea6\u5668": "Could not start the local scheduler.",
    }
    if job.get("error") in legacy_errors:
        job["error"] = legacy_errors[job["error"]]
    case = RUNS / job["case_name"]
    job.setdefault("mode", "max")
    reqs = load_rows(case / "requests.jsonl")
    job["usage"] = cost_estimate(reqs, case)
    execution = read_json(case / "execution.json") or {}
    if execution.get("status") == "completed" and (case / "mask.png").is_file():
        job["status"] = "completed"
    elif case.is_dir() and job.get("status") == "completed":
        job["status"] = "interrupted"
    stages = load_rows(case / "stages.jsonl")
    job["stage"] = stages[-1].get("stage") if stages else None
    if (
        job.get("status") in ("queued", "preparing", "running")
        and job.get("worker_pid")
        and not process_alive(job["worker_pid"])
    ):
        job["status"] = "interrupted"
    if (
        job.get("status") == "queued"
        and not job.get("worker_pid")
        and time.time() - job.get("created_at", time.time()) > 30
    ):
        job["status"] = "interrupted"
    start = job.get("started_at")
    end = job.get("finished_at") or time.time()
    job["elapsed_seconds"] = (
        max(0, end - start) if start and job.get("status") != "interrupted" else None
    )
    job["files"] = {
        name: (case / name).is_file()
        for name in ("source.png", "overlay.jpg", "mask.png", "summary.json", "execution.json")
    }
    versions = {}
    for kind, name in [("source", "source.png"), ("overlay", "overlay.jpg"), ("mask", "mask.png")]:
        try:
            st = (case / name).stat()
            versions[kind] = f"{st.st_mtime_ns}-{st.st_size}"
        except OSError:
            pass
    job["file_versions"] = versions
    job["case_path"] = str(case)
    job["has_failure"] = (case / "failure.json").is_file()
    return job


def recovered_job(case):
    """Describe a run without creating an independent history record."""
    cfg = read_json(case / "config.json")
    if not isinstance(cfg, dict):
        return None
    m = re.search(r"_api_ui_([0-9a-f]{16})$", case.name)
    jid = m.group(1) if m else hashlib.sha256(case.name.encode()).hexdigest()[:16]
    map_id = str(cfg.get("map") or case.name)
    source = Path(str(cfg.get("source_original") or ""))
    try:
        rel = source.relative_to(MAPS).as_posix()
    except ValueError:
        rel = source.name or map_id
    parts = Path(rel).parts
    region = parts[0] if len(parts) > 1 else "."
    execution = read_json(case / "execution.json") or {}
    failure = read_json(case / "failure.json") or {}
    status = (
        "completed"
        if execution.get("status") == "completed" and (case / "mask.png").is_file()
        else "failed" if failure else "interrupted"
    )
    job = {
        "id": jid,
        "case_name": case.name,
        "map_id": map_id,
        "map_path": rel,
        "map_label": source.stem or map_id,
        "region_label": REGIONS.get(region, region),
        "mode": cfg.get("mode", "max"),
        "status": status,
        "created_at": case.stat().st_mtime,
        "error": failure.get("error"),
    }
    seconds = execution.get("seconds")
    setup = read_json(case / "setup.json") or {}
    if (
        status == "completed"
        and isinstance(seconds, (int, float))
        and execution.get("seconds_scope") != "resume segment"
    ):
        job["elapsed_seconds"] = seconds + (setup.get("seconds") or 0)
    return job


def jobs_list():
    records = {}
    for p in (STATE / "jobs").glob("*.json") if (STATE / "jobs").exists() else ():
        job = read_json(p)
        if (
            isinstance(job, dict)
            and re.fullmatch(r"[0-9a-f]{16}", str(job.get("id", "")))
            and job.get("case_name")
        ):
            records[job["case_name"]] = job
    jobs = []
    seen = set()
    if RUNS.exists():
        for case in RUNS.iterdir():
            if not case.is_dir() or case.is_symlink():
                continue
            job = records.get(case.name) or recovered_job(case)
            if not job or cancelled(STATE, job["id"]):
                continue
            seen.add(case.name)
            snapshot = job_snapshot(job)
            if snapshot["elapsed_seconds"] is None and job.get("elapsed_seconds") is not None:
                snapshot["elapsed_seconds"] = job["elapsed_seconds"]
            jobs.append(snapshot)
    # Queued or preparing tasks may not have a run directory yet.
    for name, job in records.items():
        if (
            name in seen
            or cancelled(STATE, job["id"])
            or job.get("status") not in ("queued", "preparing", "running")
        ):
            continue
        if process_alive(job.get("worker_pid")) or (
            job.get("status") == "queued" and time.time() - job.get("created_at", 0) < 30
        ):
            jobs.append(job_snapshot(job))
    return sorted(jobs, key=lambda x: x.get("created_at", 0), reverse=True)
