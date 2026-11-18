"""Offline tests of rate-limit retries and evidence-preserving continuation."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rate_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def http_error(code, err, headers=None):
    return urllib.error.HTTPError(
        "https://example.invalid",
        code,
        "error",
        headers or {},
        io.BytesIO(json.dumps({"error": err}).encode()),
    )


class RateTest(unittest.TestCase):
    def test_retry_after_without_changing_request(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            runner, "CASE", Path(d)
        ), patch.object(runner.time, "sleep") as sleep, patch.object(
            runner.random, "uniform", return_value=0.2
        ):
            failure = http_error(
                429,
                {
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                    "message": "Rate limit reached",
                },
                {"Retry-After": "3"},
            )
            result = io.BytesIO(b'{"id":"ok"}')
            with patch.object(
                runner.urllib.request, "urlopen", side_effect=[failure, result]
            ) as post:
                self.assertEqual(runner.post("secret", {"model": "test"}), {"id": "ok"})
                self.assertEqual(post.call_count, 2)
                self.assertEqual(
                    post.call_args_list[0].args[0].data, post.call_args_list[1].args[0].data
                )
            self.assertGreaterEqual(sleep.call_args.args[0], 3)
            self.assertNotIn("secret", (Path(d) / "rate_limit_events.jsonl").read_text())

    def test_quota_or_ambiguous_transport_not_retried(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            runner, "CASE", Path(d)
        ), patch.object(runner.time, "sleep") as sleep:
            failure = http_error(
                429,
                {
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                    "message": "Rate limit reached; quota exceeded",
                },
            )
            with patch.object(runner.urllib.request, "urlopen", side_effect=failure) as post:
                with self.assertRaises(RuntimeError):
                    runner.post("secret", {})
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()
            with patch.object(runner.urllib.request, "urlopen", side_effect=TimeoutError) as post:
                with self.assertRaises(RuntimeError):
                    runner.post("secret", {})
                self.assertEqual(post.call_count, 1)

    def test_exhausted_and_excessive_retry_after_defer(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            runner, "CASE", Path(d)
        ), patch.object(runner.time, "sleep") as sleep:
            failure = http_error(
                429,
                {"type": "rate_limit_error", "message": "Rate limit reached"},
                {"Retry-After": "999"},
            )
            with patch.object(runner.urllib.request, "urlopen", side_effect=failure) as post:
                with self.assertRaises(runner.ConfirmedRateLimit):
                    runner.post("secret", {}, max_wait=10)
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()
        self.assertAlmostEqual(runner.retry_hint({}, "Please try again in 478ms."), 0.478)
        self.assertAlmostEqual(
            runner.retry_hint({"Retry-After": "nan"}, "Please try again in 478ms."), 0.478
        )
        self.assertAlmostEqual(
            runner.retry_hint({"retry-after-ms": "inf"}, "Please try again in 478ms."), 0.478
        )
        self.assertIsNone(runner.retry_hint({"Retry-After": "nonsense"}, ""))


class RecoveryTest(unittest.TestCase):
    def make_case(self, d, kind="run_command"):
        root = Path(d).resolve()
        source = root / "source_input.png"
        Image.new("RGB", (100, 100), "white").save(source)
        runner.CASE = root / "case"
        runner.MAP_NAME = "TEST"
        runner.MAP_SOURCE = source
        runner.setup("max")
        case = runner.CASE
        (case / "stages.jsonl").write_text('{"stage":"first_review"}\n')
        (case / "failure.json").write_text(
            json.dumps(
                {
                    "stage": "first_review",
                    "error": "Responses API HTTP 429: Rate limit reached on tokens per min. Please try again in 478ms.",
                }
            )
        )
        if kind == "run_command":
            args = {"command": "echo one-time"}
            output = {"exit_code": 0, "stdout": "one-time\n", "stderr": ""}
            (case / "tool_calls.jsonl").write_text(
                json.dumps({"command": args["command"], "result": output}) + "\n"
            )
        else:
            image = case / "source.png"
            args = {"paths": [str(image)]}
            runner.image_output(args["paths"])
            output = None
        resp = {
            "id": "resp_last",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": kind,
                    "call_id": "call_1",
                    "arguments": json.dumps(args),
                }
            ],
        }
        (case / "responses").mkdir()
        (case / "responses/0001.json").write_text(json.dumps(resp))
        (case / "requests.jsonl").write_text(
            json.dumps({"seq": 1, "response_id": "resp_last"}) + "\n"
        )
        return case, output

    def test_saved_command_continues_without_reexecution(self):
        with tempfile.TemporaryDirectory() as d:
            case, output = self.make_case(d)
            config = json.loads((case / "config.json").read_text())
            config.pop("mode")
            runner.save(case / "config.json", config)
            cont = runner.recovery_continuation()
            self.assertEqual(json.loads(cont["inputs"][0]["output"]), output)
            response = {
                "id": "resp_next",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch.object(
                runner, "post", return_value=response
            ) as post, patch.object(runner, "run_command") as command, patch.object(
                runner, "sandbox_test"
            ) as sandbox, patch.object(
                runner,
                "audit",
                return_value={
                    "summary_exists": True,
                    "source_missing": [],
                    "first_review_missing": [],
                },
            ):
                runner.run(resume=True)
                command.assert_not_called()
                sandbox.assert_not_called()
                body = post.call_args.args[1]
                self.assertEqual(body["previous_response_id"], "resp_last")
                self.assertEqual(json.loads(body["input"][0]["output"]), output)
            self.assertTrue((case / "execution.json").exists())
            self.assertEqual(len((case / "requests.jsonl").read_text().splitlines()), 2)
            self.assertFalse((case / "failure.json").exists())

    def test_image_evidence_must_match_and_receipt_not_duplicated(self):
        with tempfile.TemporaryDirectory() as d:
            case, _ = self.make_case(d, "view_images")
            before = (case / "image_receipts.jsonl").read_bytes()
            cont = runner.recovery_continuation()
            self.assertEqual(cont["inputs"][0]["output"][1]["type"], "input_image")
            self.assertEqual(before, (case / "image_receipts.jsonl").read_bytes())
            Image.new("RGB", (100, 100), "black").save(case / "source.png")
            with self.assertRaises(ValueError):
                runner.recovery_continuation()

    def test_checkpoint_survives_confirmed_rate_limit_only(self):
        with tempfile.TemporaryDirectory() as d:
            case, _ = self.make_case(d)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch.object(
                runner,
                "post",
                side_effect=runner.ConfirmedRateLimit("Responses API HTTP 429: Rate limit reached"),
            ):
                with self.assertRaises(runner.ConfirmedRateLimit):
                    runner.run(resume=True)
            self.assertTrue((case / "rate_limit_checkpoint.json").exists())
            self.assertFalse((case / "recovery_inflight.json").exists())
            self.assertEqual((case / "rate_limit_checkpoint.json").stat().st_mode & 0o777, 0o600)
            (case / "failure.json").write_text(
                json.dumps(
                    {
                        "stage": "first_review",
                        "error_type": "ConfirmedRateLimit",
                        "error": "Responses API HTTP 429: Rate limit reached",
                    }
                )
            )
            self.assertEqual(runner.recovery_continuation()["completed_requests"], 1)

    def test_ambiguous_recovery_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            case, _ = self.make_case(d)
            (case / "recovery_inflight.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                runner.recovery_continuation()


if __name__ == "__main__":
    unittest.main()
