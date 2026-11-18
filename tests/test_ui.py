"""Offline UI tests. Fake runner performs no API calls."""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import common
import server
import worker

FAKE = """import argparse,json,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--map',required=True);p.add_argument('--run-id',required=True);p.add_argument('--source');p.add_argument('--mode');a=p.parse_args()
r=Path(__file__).parent/'runs'/(a.map+'_api_'+a.run_id)
if a.action=='setup':
 r.mkdir(parents=True);(r/'config.json').write_text(json.dumps({'map':a.map,'mode':a.mode}));time.sleep(.2)
else:
 start=time.time();time.sleep(.35);end=time.time();(r/'mask.png').write_bytes(b'fake')
 (r/'execution.json').write_text(json.dumps({'status':'completed','fake_start':start,'fake_end':end}));(r/'summary.json').write_text('{}')
"""


class CostTest(unittest.TestCase):
    def test_existing_task_region_is_displayed_in_english(self):
        with tempfile.TemporaryDirectory() as t, patch.object(common, "RUNS", Path(t)):
            job = {
                "id": "a" * 16,
                "case_name": "case",
                "map_path": "new_york/9.tif",
                "region_label": "\u7ebd\u7ea6",
                "status": "completed",
            }
            self.assertEqual(common.job_snapshot(job)["region_label"], "New York")
            self.assertEqual(job["region_label"], "\u7ebd\u7ea6")

    def test_short_and_long_context_cache_rates(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t)
            (r / "responses").mkdir()
            (r / "responses/0001.json").write_text('{"service_tier":"default"}')
            rows = [
                {
                    "seq": 1,
                    "model": "gpt-6.1-sol",
                    "usage": {
                        "input_tokens": 1000000,
                        "output_tokens": 20000,
                        "input_tokens_details": {
                            "cached_tokens": 200000,
                            "cache_write_tokens": 300000,
                            "cache_write_tokens_details": {
                                "ttl_30m_tokens": 200000,
                                "ttl_12h_tokens": 100000,
                            },
                        },
                    },
                }
            ]
            x = common.cost_estimate(rows, r)
            self.assertAlmostEqual(x["cost_usd"], 4.34)
            self.assertEqual(x["tokens"], 1020000)

    def test_unknown_model_or_missing_usage_is_not_a_fake_cost(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(
                common.cost_estimate(
                    [
                        {
                            "seq": 1,
                            "model": "unknown",
                            "usage": {"input_tokens": 20, "output_tokens": 10},
                        }
                    ],
                    Path(t),
                )["cost_usd"]
            )
            self.assertIsNone(common.cost_estimate([], Path(t))["tokens"])


class WorkerTest(unittest.TestCase):
    def test_fake_runs_parallel_and_serial(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / ".ui_state"
            runs = root / "runs"
            maps = root / "maps"
            maps.mkdir()
            (maps / "a.jpg").write_bytes(b"a")
            (maps / "b.jpg").write_bytes(b"b")
            fake = root / "fake_runner.py"
            fake.write_text(FAKE)
            with patch.multiple(
                worker, ROOT=root, RUNS=runs, STATE=state, RUNNER=fake, PYTHON=Path(sys.executable)
            ):
                for mode, parallel in [("serial", 1), ("parallel", 2)]:
                    ids = []
                    for k in range(2):
                        jid = f"{parallel}{k}" + ("a" * 14)
                        ids.append(jid)
                        name = "T" + str(k)
                        rid = "r" + mode
                        job = {
                            "id": jid,
                            "map_path": ["a.jpg", "b.jpg"][k],
                            "map_id": name,
                            "run_id": rid,
                            "case_name": name + "_api_" + rid,
                            "status": "queued",
                            "mode": "initial" if k == 0 else "max",
                        }
                        common.atomic_json(state / "jobs" / (jid + ".json"), job)
                    bid = f"{parallel}" + "b" * 15
                    common.atomic_json(
                        state / "batches" / (bid + ".json"), {"job_ids": ids, "workers": parallel}
                    )
                    worker.run_batch(bid)
                    jobs = [common.read_json(state / "jobs" / (x + ".json")) for x in ids]
                    self.assertTrue(all(j["status"] == "completed" for j in jobs))
                    configs = [common.read_json(runs / j["case_name"] / "config.json") for j in jobs]
                    self.assertEqual([c["mode"] for c in configs], ["initial", "max"])
                    e = [common.read_json(runs / j["case_name"] / "execution.json") for j in jobs]
                    overlap = max(x["fake_start"] for x in e) < min(x["fake_end"] for x in e)
                    self.assertEqual(overlap, parallel == 2)


class ServerTest(unittest.TestCase):
    def test_local_endpoints_csrf_and_mock_start(self):
        srv = server.Server(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(srv.server_port)
        try:
            with urllib.request.urlopen(base + "/api/maps") as r:
                self.assertGreater(len(json.load(r)["maps"]), 0)
            body = json.dumps({"maps": ["new_york/8.tif"], "workers": 2}).encode()
            req = urllib.request.Request(
                base + "/api/start", data=body, headers={"Content-Type": "application/json"}
            )
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            with patch.object(
                server, "launch_batch", return_value={"batch_id": "test", "job_ids": ["abc"]}
            ) as mock:
                req = urllib.request.Request(
                    base + "/api/start",
                    data=body,
                    headers={"Content-Type": "application/json", "X-UI-Token": server.TOKEN},
                )
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 202)
                mock.assert_called_once_with(["new_york/8.tif"], 2, "initial")
                req.data = json.dumps({"maps": ["new_york/8.tif"], "workers": 2, "mode": "max"}).encode()
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 202)
                mock.assert_called_with(["new_york/8.tif"], 2, "max")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/secrets/openai_api_key.txt")
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
