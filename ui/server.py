#!/usr/bin/env python3
"""Local-only control surface. Agent execution is delegated to runner.py."""
import argparse
import errno
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from cleanup import cleanup_stale
from common import (
    REGIONS,
    ROOT,
    RUNS,
    STATE,
    atomic_json,
    cancelled,
    job_guard,
    jobs_list,
    map_catalog,
    process_alive,
    read_json,
    recovered_job,
)
from PIL import Image
from workflow_view import receipt_image, revisions, workflow, workflow_version

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
PREVIEW_LOCKS = [threading.Lock() for _ in range(32)]


def preview_lock(path):
    return PREVIEW_LOCKS[hash(str(path)) % len(PREVIEW_LOCKS)]


Image.MAX_IMAGE_PIXELS = None
STATICS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def get_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        return None
    job = read_json(STATE / "jobs" / (job_id + ".json"))
    if not isinstance(job, dict) or job.get("id") != job_id:
        job = None
        if RUNS.is_dir():
            for candidate in RUNS.iterdir():
                if candidate.is_dir() and not candidate.is_symlink():
                    item = recovered_job(candidate)
                    if item and item["id"] == job_id:
                        job = item
                        break
        if not job:
            return None
    case = (RUNS / job.get("case_name", "")).resolve()
    if not case.is_relative_to(RUNS.resolve()) or case == RUNS.resolve():
        return None
    return job, case


def launch_batch(selected, workers):
    catalog = {x["id"]: x for x in map_catalog()}
    if (
        not isinstance(selected, list)
        or not selected
        or len(selected) > 24
        or len(set(selected)) != len(selected)
        or any(not isinstance(x, str) or x not in catalog for x in selected)
    ):
        raise ValueError("Select up to 24 distinct, available maps.")
    if type(workers) != int or workers not in (1, 2, 4):
        raise ValueError("Concurrency must be 1, 2, or 4.")
    for d in ("jobs", "batches", "logs", "previews"):
        (STATE / d).mkdir(parents=True, exist_ok=True)
    batch_id = uuid.uuid4().hex[:16]
    created = time.time()
    job_ids = []
    for item in selected:
        meta = catalog[item]
        job_id = uuid.uuid4().hex[:16]
        slug = (
            re.sub(r"[^A-Za-z0-9_-]+", "_", item.rsplit(".", 1)[0]).strip("_").upper()[:70] or "MAP"
        )
        run_id = "ui_" + job_id
        case_name = slug + "_api_" + run_id
        job = {
            "id": job_id,
            "batch_id": batch_id,
            "map_id": slug,
            "map_path": item,
            "region_label": meta["region_label"],
            "map_label": meta["label"],
            "case_name": case_name,
            "run_id": run_id,
            "status": "queued",
            "created_at": created,
            "cancel_safe": True,
        }
        atomic_json(STATE / "jobs" / (job_id + ".json"), job)
        job_ids.append(job_id)
    batch = {
        "id": batch_id,
        "job_ids": job_ids,
        "workers": min(workers, len(job_ids)),
        "created_at": created,
        "status": "queued",
    }
    batch_path = STATE / "batches" / (batch_id + ".json")
    atomic_json(batch_path, batch)
    try:
        with (STATE / "logs" / ("batch_" + batch_id + ".log")).open("a") as log:
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "worker.py"), batch_id],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        threading.Thread(target=proc.wait, daemon=True).start()
    except Exception:
        for job_id in job_ids:
            p = STATE / "jobs" / (job_id + ".json")
            j = read_json(p)
            j.update(
                status="failed",
                error="Could not start the local scheduler.",
                finished_at=time.time(),
            )
            atomic_json(p, j)
        raise
    return {"batch_id": batch_id, "job_ids": job_ids}


def delete_job(job_id):
    """Stop one isolated job and hide its record; keep its case files intact."""
    with job_guard(STATE, job_id):
        found = get_job(job_id)
        if not found:
            raise ValueError("Task not found.")
        job, case = found
        if cancelled(STATE, job_id):
            return {"status": "deleted"}
        registry = read_json(STATE / "processes" / (job_id + ".json"))
        pid = None
        legacy_pids = []
        if registry:
            pid = registry.get("pid")
            command = registry.get("command") or []
            if (
                type(pid) != int
                or pid <= 1
                or not isinstance(command, list)
                or len(command) < 2
                or job.get("run_id") not in command
                or job.get("map_id") not in command
            ):
                raise ValueError("Cannot verify the task process; task was not deleted.")
            if process_alive(pid):
                try:
                    actual = subprocess.check_output(
                        ["/bin/ps", "-p", str(pid), "-o", "command="], text=True, timeout=3
                    )
                    if (
                        os.getpgid(pid) != pid
                        or str(command[1]) not in actual
                        or "--run-id " + job["run_id"] not in actual
                    ):
                        raise ValueError("Cannot verify the task process; task was not deleted.")
                except ProcessLookupError:
                    pid = None
            else:
                pid = None
        elif (
            job.get("status") in ("queued", "preparing", "running")
            and process_alive(job.get("worker_pid"))
            and not job.get("cancel_safe")
        ):
            # Older workers share a process group across tasks; target only the exact runner and descendants.
            if job.get("status") == "queued":
                raise ValueError(
                    "This task was queued by an older scheduler and cannot be safely stopped without affecting other tasks."
                )
            try:
                lines = subprocess.check_output(
                    ["/bin/ps", "-axo", "pid=,ppid=,command="], text=True, timeout=3
                ).splitlines()
                processes = []
                for line in lines:
                    fields = line.strip().split(None, 2)
                    if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                        processes.append((int(fields[0]), int(fields[1]), fields[2]))
                matches = [
                    p
                    for p, parent, cmd in processes
                    if parent == int(job["worker_pid"])
                    and str(ROOT / "runner.py") in cmd
                    and "--run-id " + job["run_id"] in cmd
                    and "--map " + job["map_id"] in cmd
                ]
                if len(matches) != 1:
                    raise ValueError("Cannot verify this older task process; task was not deleted.")
                legacy_pids = matches[:]
                frontier = matches[:]
                while frontier:
                    frontier = [
                        p
                        for p, parent, cmd in processes
                        if parent in frontier and p not in legacy_pids
                    ]
                    legacy_pids.extend(frontier)
            except (OSError, subprocess.SubprocessError, KeyError, TypeError):
                raise ValueError(
                    "Cannot verify this older task process; task was not deleted."
                ) from None
        atomic_json(
            STATE / "cancelled" / (job_id + ".json"),
            {"id": job_id, "requested_at": time.time(), "preserved_case": str(case)},
        )
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                (STATE / "cancelled" / (job_id + ".json")).unlink(missing_ok=True)
                raise ValueError("Could not stop the task; it has not been deleted.") from None

            def finish_stop():
                time.sleep(2)
                try:
                    actual = subprocess.check_output(
                        ["/bin/ps", "-p", str(pid), "-o", "command="], text=True, timeout=3
                    )
                    if (
                        os.getpgid(pid) == pid
                        and str(command[1]) in actual
                        and "--run-id " + job["run_id"] in actual
                    ):
                        os.killpg(pid, signal.SIGKILL)
                except (OSError, subprocess.SubprocessError):
                    pass

            threading.Thread(target=finish_stop, daemon=True).start()
        for legacy_pid in reversed(legacy_pids):
            try:
                os.kill(legacy_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                (STATE / "cancelled" / (job_id + ".json")).unlink(missing_ok=True)
                raise ValueError("Could not stop the task; it has not been deleted.") from None
    return {"status": "deleted", "files_preserved": True}


def remove_run(job_id):
    """Permanently delete only an inactive, explicitly selected run."""
    with job_guard(STATE, job_id):
        found = get_job(job_id)
        if not found:
            raise ValueError("Task not found.")
        job, case = found
        registry = read_json(STATE / "processes" / (job_id + ".json")) or {}
        if job.get("status") in ("queued", "preparing", "running") or process_alive(
            registry.get("pid")
        ):
            raise ValueError("Stop the task before deleting its files.")
        map_id = job.get("map_id")
        run_id = job.get("run_id")
        if not run_id and "_api_" in job.get("case_name", ""):
            map_id, run_id = job["case_name"].rsplit("_api_", 1)
        if map_id and run_id:
            try:
                lines = subprocess.check_output(
                    ["/bin/ps", "-axo", "command="], text=True, timeout=3
                ).splitlines()
            except (OSError, subprocess.SubprocessError):
                raise ValueError("Cannot verify whether this run is active.") from None
            for line in lines:
                try:
                    args = shlex.split(line)
                except ValueError:
                    continue
                if not any(Path(arg).name == "runner.py" for arg in args):
                    continue
                if "--map" in args and "--run-id" in args:
                    mi = args.index("--map")
                    ri = args.index("--run-id")
                    if (
                        mi + 1 < len(args)
                        and ri + 1 < len(args)
                        and args[mi + 1] == map_id
                        and args[ri + 1] == run_id
                    ):
                        raise ValueError("This run is active; stop it before deleting its files.")
        raw = RUNS / job.get("case_name", "")
        if (
            raw.is_symlink()
            or raw.parent.resolve() != RUNS.resolve()
            or not raw.is_dir()
            or raw.resolve() != case
        ):
            raise ValueError("Run directory is unavailable or unsafe.")
        shutil.rmtree(raw)
        for path in (STATE / "previews").glob(job_id + "_*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        for folder in ("jobs", "cancelled", "processes", "logs"):
            (STATE / folder / (job_id + (".log" if folder == "logs" else ".json"))).unlink(
                missing_ok=True
            )
    return {"status": "deleted", "files_preserved": False}


def preview(case, job_id, kind):
    name = {"source": "source.png", "overlay": "overlay.jpg", "mask": "mask.png"}[kind]
    src = case / name
    if not src.is_file():
        return None
    st = src.stat()
    target = (
        STATE / "previews" / f"{job_id}_{kind}_{st.st_mtime_ns}_{st.st_size}.png"
        if kind == "mask"
        else STATE / "previews" / f"{job_id}_{kind}_{st.st_mtime_ns}_{st.st_size}.jpg"
    )
    if target.exists():
        return target
    with preview_lock(target):
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.stem + ".tmp" + target.suffix)
        with Image.open(src) as im:
            im = im.convert("L" if kind == "mask" else "RGB")
            im.thumbnail(
                (1800, 1800),
                Image.Resampling.NEAREST if kind == "mask" else Image.Resampling.LANCZOS,
            )
            if kind == "mask":
                im.save(temp, format="PNG")
            else:
                im.save(temp, format="JPEG", quality=88)
        os.replace(temp, target)
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "StreetUI/1.0"

    def log_message(self, fmt, *args):
        pass

    def headers_base(self, status=200, ctype="application/json; charset=utf-8", length=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def json(self, data, status=200):
        b = json.dumps(data, ensure_ascii=False).encode()
        self.headers_base(status, length=len(b))
        self.wfile.write(b)

    def valid_host(self):
        return self.headers.get("Host", "").lower() in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def sendfile(self, path, ctype, download=False, cacheable=False):
        try:
            with Path(path).open("rb") as f:
                size = os.fstat(f.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Cache-Control",
                    (
                        "private, max-age=3600, immutable"
                        if cacheable and not download
                        else "no-store"
                    ),
                )
                if download:
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{Path(path).name}"'
                    )
                self.end_headers()
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.json({"error": "File unavailable."}, 404)

    def do_GET(self):
        if not self.valid_host():
            return self.json({"error": "invalid host"}, 403)
        path = urlparse(self.path).path
        if path in STATICS:
            name, typ = STATICS[path]
            data = (HERE / name).read_bytes()
            self.headers_base(200, typ, len(data))
            self.wfile.write(data)
            return
        if path == "/api/session":
            return self.json({"token": TOKEN})
        if path == "/api/maps":
            return self.json({"maps": map_catalog()})
        if path == "/api/jobs":
            return self.json({"jobs": jobs_list()})
        m = re.fullmatch(r"/api/workflow-version/([0-9a-f]{16})", path)
        if m:
            found = get_job(m.group(1))
            if not found:
                return self.json({"error": "Task not found."}, 404)
            return self.json({"version": workflow_version(found[1])})
        m = re.fullmatch(r"/api/workflow/([0-9a-f]{16})", path)
        if m:
            found = get_job(m.group(1))
            if not found:
                return self.json({"error": "Task not found."}, 404)
            version = workflow_version(found[1])
            data = workflow(found[1])
            data["version"] = version
            return self.json(data)
        m = re.fullmatch(r"/api/stage-preview/([0-9a-f]{16})/(inventory_r[0-9]+)", path)
        if m:
            job_id, rev = m.groups()
            found = get_job(job_id)
            if not found:
                return self.json({"error": "Task not found."}, 404)
            candidate = next((x for x in revisions(found[1]) if x["revision"] == rev), None)
            if not candidate:
                return self.json({"error": "Label version not available."}, 404)
            return self.sendfile(
                found[1] / candidate["overview"],
                "image/jpeg",
                cacheable=bool(urlparse(self.path).query),
            )
        m = re.fullmatch(r"/api/workflow-image/([0-9a-f]{16})/(\d+)/(\d+)", path)
        if m:
            job_id, ri, ii = m.groups()
            found = get_job(job_id)
            if not found:
                return self.json({"error": "Task not found."}, 404)
            try:
                src = receipt_image(found[1], int(ri), int(ii))
            except (OSError, ValueError, TypeError):
                src = None
            if not src:
                return self.json({"error": "Historical image is missing or has changed."}, 404)
            # New snapshots already are the exact PNG sent to the agent.
            if src.is_relative_to((found[1] / "image_snapshots").resolve()):
                return self.sendfile(src, "image/png", cacheable=bool(urlparse(self.path).query))
            # Recreate the same RGB/1600px representation sent to the model.
            target = (
                STATE
                / "previews"
                / f"{job_id}_receipt_{ri}_{ii}_{src.stat().st_mtime_ns}_{src.stat().st_size}.png"
            )
            try:
                with preview_lock(target):
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temp = target.with_name(target.stem + ".tmp.png")
                        with Image.open(src) as im:
                            im = im.convert("RGB")
                            im.thumbnail((1600, 1600))
                            im.save(temp, format="PNG")
                        os.replace(temp, target)
                return self.sendfile(target, "image/png", cacheable=bool(urlparse(self.path).query))
            except (OSError, ValueError):
                return self.json({"error": "Could not generate image preview."}, 500)
        m = re.fullmatch(r"/api/log/([0-9a-f]{16})", path)
        if m:
            if not get_job(m.group(1)):
                return self.json({"error": "Task not found."}, 404)
            f = STATE / "logs" / (m.group(1) + ".log")
            try:
                with f.open("rb") as fp:
                    fp.seek(max(0, f.stat().st_size - 12000))
                    s = fp.read().decode("utf-8", "replace")
            except OSError:
                s = "No log available."
            return self.json({"log": s})
        m = re.fullmatch(r"/api/(preview|download)/([0-9a-f]{16})/(source|overlay|mask)", path)
        if m:
            mode, job_id, kind = m.groups()
            found = get_job(job_id)
            if not found:
                return self.json({"error": "Task not found."}, 404)
            job, case = found
            if job.get("status") != "completed":
                return self.json({"error": "Result is not complete."}, 409)
            src = case / (
                {"source": "source.png", "overlay": "overlay.jpg", "mask": "mask.png"}[kind]
            )
            if mode == "preview":
                try:
                    src = preview(case, job_id, kind)
                except Exception:
                    return self.json({"error": "Could not generate preview."}, 500)
            if src is None or not src.is_file():
                return self.json({"error": "File not found."}, 404)
            return self.sendfile(
                src,
                mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                mode == "download",
                cacheable=mode == "preview" and bool(urlparse(self.path).query),
            )
        return self.json({"error": "Not found."}, 404)

    def do_POST(self):
        if not self.valid_host() or self.headers.get("X-UI-Token") != TOKEN:
            return self.json({"error": "Request rejected."}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }:
            return self.json({"error": "Origin mismatch."}, 403)
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/delete/([0-9a-f]{16})", path)
        if m:
            try:
                return self.json(delete_job(m.group(1)))
            except ValueError as e:
                return self.json({"error": str(e)}, 409)
            except Exception:
                return self.json({"error": "Could not stop and delete task."}, 500)
        m = re.fullmatch(r"/api/remove-run/([0-9a-f]{16})", path)
        if m:
            try:
                return self.json(remove_run(m.group(1)))
            except ValueError as e:
                return self.json({"error": str(e)}, 409)
            except Exception:
                return self.json({"error": "Could not delete run files."}, 500)
        if path != "/api/start":
            return self.json({"error": "Not found."}, 404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 20000:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(n))
            r = launch_batch(data.get("maps"), data.get("workers"))
            return self.json(r, 202)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return self.json({"error": str(e)}, 400)
        except Exception:
            return self.json({"error": "Could not start tasks; check the local log."}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    a = p.parse_args()
    for d in ("jobs", "batches", "logs", "previews"):
        (STATE / d).mkdir(parents=True, exist_ok=True)
    try:
        server = Server(("127.0.0.1", a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        url = f"http://127.0.0.1:{a.port}/"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                existing = r.read(20000)
            if b"street-segmentation-ui" not in existing:
                raise ValueError("port occupied by another service")
        except Exception:
            raise SystemExit(
                "The interface port is in use by another program; choose another port with --port."
            ) from None
        print("Street Segmentation UI already running: " + url, flush=True)
        if not a.no_open:
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=cleanup_stale, args=(STATE, RUNS), daemon=True).start()
    print("Street Segmentation UI: " + url, flush=True)
    if not a.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
