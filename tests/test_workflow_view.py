"""Offline tests of history reconstruction and historical image integrity."""

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import common
import server
import workflow_view


class WorkflowViewTest(unittest.TestCase):
    def test_agent_output_versions_are_preserved_in_response_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp).resolve()
            (case / "responses").mkdir()
            spec = importlib.util.spec_from_file_location("artifact_runner", ROOT / "runner.py")
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            requests = []
            with patch.object(runner, "CASE", case):
                for seq, value in [(1, "first observation"), (2, "revised observation")]:
                    before = runner.artifact_state()
                    (case / "draft_batch_1.json").write_text(json.dumps({"observation": value}))
                    if seq == 2:
                        (case / "review_notes.txt").write_text("agent text output")
                    runner.snapshot_artifacts(before, seq, "call_" + str(seq))
                    (case / "responses" / f"{seq:04d}.json").write_text(
                        json.dumps(
                            {
                                "output": [
                                    {
                                        "type": "function_call",
                                        "name": "run_command",
                                        "call_id": "call_" + str(seq),
                                        "arguments": '{"command":"write"}',
                                    }
                                ]
                            }
                        )
                    )
                    requests.append({"seq": seq, "stage": "source_review"})
            (case / "requests.jsonl").write_text("".join(json.dumps(x) + "\n" for x in requests))
            result = workflow_view.workflow(case)
            saved = [
                [
                    o
                    for o in e["outputs"]
                    if o["type"] == "saved_artifact" and o["name"] == "draft_batch_1.json"
                ][0]["data"]["observation"]
                for e in result["events"]
            ]
            self.assertEqual(saved, ["first observation", "revised observation"])
            self.assertEqual(result["artifacts"][0]["data"]["observation"], "revised observation")
            self.assertEqual(
                [
                    o["data"]["observation"]
                    for o in result["events"][1]["outputs"]
                    if o["type"] == "saved_artifact" and o["name"] == "review_notes.txt"
                ],
                ["agent text output"],
            )

    def test_saved_label_versions_are_discovered_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp).resolve()
            (case / "config.json").write_text(json.dumps({"map": "MAP"}))
            rev = case / "run/maps/MAP/tools/inventory_r1"
            rev.mkdir(parents=True)
            Image.new("RGB", (10, 10), "white").save(rev / "overview.jpg")
            self.assertEqual(workflow_view.revisions(case)[0]["revision"], "inventory_r1")
            (case / "config.json").write_text(json.dumps({"map": "../../outside"}))
            self.assertEqual(workflow_view.revisions(case), [])

    def test_new_view_records_exact_sent_pixels_even_after_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp).resolve()
            img = case / "crop.png"
            Image.new("RGB", (10, 10), "white").save(img)
            spec = importlib.util.spec_from_file_location("snapshot_runner", ROOT / "runner.py")
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            with patch.object(runner, "CASE", case):
                output = runner.image_output(["crop.png"])
            sent = base64.b64decode(output[1]["image_url"].split(",", 1)[1])
            img.write_bytes(b"changed")
            restored = workflow_view.receipt_image(case, 0, 0)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.read_bytes(), sent)
            restored.write_bytes(b"changed")
            self.assertIsNone(workflow_view.receipt_image(case, 0, 0))

    def test_receipts_and_tools_linked_without_inventing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            (case / "responses").mkdir()
            img = case / "crop.png"
            Image.new("RGB", (10, 10), "white").save(img)
            sha = hashlib.sha256(img.read_bytes()).hexdigest()
            (case / "requests.jsonl").write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "stage": "source_review",
                        "seconds": 2,
                        "usage": {"input_tokens": 7, "output_tokens": 2},
                    }
                )
                + "\n"
            )
            (case / "responses/0001.json").write_text(
                json.dumps(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "view_images",
                                "arguments": json.dumps({"paths": ["crop.png"]}),
                            }
                        ]
                    }
                )
            )
            (case / "image_receipts.jsonl").write_text(
                json.dumps(
                    {
                        "stage": "source_review",
                        "images": [
                            {
                                "path": "crop.png",
                                "file_sha256": sha,
                                "native_size": [10, 10],
                                "display_size": [10, 10],
                            }
                        ],
                    }
                )
                + "\n"
            )
            (case / "qa_r1.json").write_text(json.dumps({"tiles": {"x0": {"issues": ["gap"]}}}))
            result = workflow_view.workflow(case)
            self.assertEqual(result["events"][0]["outputs"][0]["images"][0]["path"], "crop.png")
            self.assertEqual(result["artifacts"][0]["data"]["tiles"]["x0"]["issues"], ["gap"])
            self.assertEqual(workflow_view.receipt_image(case, 0, 0), img.resolve())
            Image.new("RGB", (10, 10), "black").save(img)
            self.assertIsNone(workflow_view.receipt_image(case, 0, 0))
            (case / "responses/0001.json").write_text(
                json.dumps(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "view_images",
                                "arguments": json.dumps({"paths": ["different.png"]}),
                            }
                        ]
                    }
                )
            )
            self.assertEqual(
                workflow_view.workflow(case)["events"][0]["outputs"][0]["type"], "missing_images"
            )

    def test_running_job_history_is_read_only_and_preview_rejects_changed_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            runs = root / "runs"
            case = runs / "example"
            case.mkdir(parents=True)
            jid = "a" * 16
            common.atomic_json(
                state / "jobs" / (jid + ".json"),
                {"id": jid, "case_name": "example", "status": "running"},
            )
            img = case / "im.png"
            Image.new("RGB", (10, 10), "white").save(img)
            sha = hashlib.sha256(img.read_bytes()).hexdigest()
            (case / "image_receipts.jsonl").write_text(
                json.dumps({"images": [{"path": "im.png", "file_sha256": sha}]}) + "\n"
            )
            srv = server.Server(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(srv.server_port)
            try:
                with patch.multiple(server, STATE=state, RUNS=runs):
                    with urllib.request.urlopen(base + "/api/workflow-version/" + jid) as r:
                        first = json.load(r)["version"]
                    (case / "requests.jsonl").write_text(
                        json.dumps({"seq": 1, "stage": "source_review"}) + "\n"
                    )
                    with urllib.request.urlopen(base + "/api/workflow-version/" + jid) as r:
                        second = json.load(r)["version"]
                    self.assertNotEqual(first, second)
                    with urllib.request.urlopen(base + "/api/workflow/" + jid) as r:
                        data = json.load(r)
                        self.assertEqual(data["version"], second)
                        self.assertEqual(len(data["events"]), 1)
                    with urllib.request.urlopen(base + "/api/workflow-image/" + jid + "/0/0") as r:
                        self.assertEqual(r.headers["Content-Type"], "image/png")
                    Image.new("RGB", (10, 10), "black").save(img)
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        urllib.request.urlopen(base + "/api/workflow-image/" + jid + "/0/0")
                    self.assertEqual(e.exception.code, 404)
                    self.assertEqual(
                        common.read_json(state / "jobs" / (jid + ".json"))["status"], "running"
                    )
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
