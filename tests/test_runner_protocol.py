"""Check the agent/tool boundary without calling the model API."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("protocol_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerProtocolTest(unittest.TestCase):
    def test_transient_empty_instruction_read_is_retried_before_submission(self):
        path = Mock()
        policy = "街道".encode("utf-8")
        path.stat.return_value.st_size = len(policy)
        path.read_bytes.side_effect = [b"", policy]
        with patch.object(runner.time, "sleep"):
            self.assertEqual(runner.read_instructions(path), "街道")
        self.assertEqual(path.read_bytes.call_count, 2)
        path.read_bytes.side_effect = [b"", b"", b""]
        with patch.object(runner.time, "sleep"), self.assertRaises(OSError):
            runner.read_instructions(path)
        path.read_bytes.side_effect = [policy]
        with self.assertRaisesRegex(ValueError, "Prepared instructions changed"):
            runner.read_instructions(path, "different")

    def test_request_keeps_agent_configuration_and_continuation(self):
        inputs = [{"type": "function_call_output", "call_id": "call_1", "output": "ok"}]
        first = runner.build_response_request(inputs, None)
        resumed = runner.build_response_request(inputs, "response_1")
        self.assertEqual(first["input"], inputs)
        self.assertNotIn("previous_response_id", first)
        self.assertEqual(resumed["previous_response_id"], "response_1")
        self.assertEqual(resumed["model"], "gpt-6.2-sol")
        self.assertEqual(resumed["reasoning"], {"effort": "medium"})
        self.assertFalse(resumed["parallel_tool_calls"])
        self.assertEqual(resumed["tools"], runner.TOOLS)

    def test_initial_and_max_expose_their_native_tools(self):
        for mode in ("initial", "max"):
            body = runner.build_response_request(
                [],
                None,
                case_tools=json.loads((ROOT / "workflow/api_tools.json").read_text()),
                mode=mode,
            )
            names = {tool["name"] for tool in body["tools"]}
            self.assertTrue(
                {"view_images", "record_source_batch", "view_source_details", "submit_roads"}
                <= names
            )
            self.assertEqual("run_command" in names, mode == "max")

    def test_command_output_and_artifact_snapshot_are_preserved(self):
        call = {
            "name": "run_command",
            "call_id": "call_1",
            "arguments": json.dumps({"command": "example"}),
        }
        result = {"exit_code": 0, "stdout": "result", "stderr": ""}
        with patch.object(runner, "artifact_state", return_value={"before": (1, 2)}), patch.object(
            runner, "run_command", return_value=result
        ) as command, patch.object(runner, "snapshot_artifacts") as snapshot:
            output = runner.execute_tool_call(call, 7)
        command.assert_called_once_with("example")
        snapshot.assert_called_once_with({"before": (1, 2)}, 7, "call_1")
        self.assertEqual(
            output,
            {"type": "function_call_output", "call_id": "call_1", "output": json.dumps(result)},
        )

    def test_image_output_and_error_are_returned_to_agent(self):
        call = {
            "name": "view_images",
            "call_id": "call_2",
            "arguments": json.dumps({"paths": ["a.png"]}),
        }
        image_output = [
            {"type": "input_text", "text": "metadata"},
            {"type": "input_image", "image_url": "test"},
        ]
        with patch.object(runner, "image_output", return_value=image_output):
            self.assertEqual(runner.execute_tool_call(call, 8)["output"], image_output)
        with patch.object(runner, "image_output", side_effect=ValueError("bad image")):
            output = runner.execute_tool_call(call, 8)
        self.assertEqual(json.loads(output["output"]), {"error": "ValueError: bad image"})
        self.assertEqual(output["call_id"], "call_2")


if __name__ == "__main__":
    unittest.main()
