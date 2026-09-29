import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.manifest import ManifestError  # noqa: E402
from evaluation.task_provider import load_scripted_provider  # noqa: E402
from evaluation.runner import classify_run, run_suite, run_task, write_reports  # noqa: E402
from agent.loop import Run  # noqa: E402


class TestTaskProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "script.json")

    def write_script(self, value):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(value, fh)

    def test_loads_scripted_tool_call(self):
        self.write_script([{
            "text": "inspect file",
            "tool_calls": [{"id": "read-1", "name": "read_file", "arguments": {"path": "src/math.py"}}],
        }])

        turn = load_scripted_provider(self.path).complete([], [])

        self.assertEqual(turn.text, "inspect file")
        self.assertEqual(turn.tool_calls[0].name, "read_file")
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "src/math.py"})

    def test_rejects_invalid_script_shapes(self):
        cases = [
            ({"text": "not a list"}, "must be a JSON array"),
            ([{"text": "x", "tool_calls": [{"id": "a", "arguments": {}}]}], "name"),
            ([{"text": "x", "tool_calls": [{"id": "a", "name": "read_file", "arguments": []}]}], "arguments"),
            ([
                {"text": "x", "tool_calls": [{"id": "a", "name": "read_file", "arguments": {}}]},
                {"text": "y", "tool_calls": [{"id": "a", "name": "list_dir", "arguments": {}}]},
            ], "duplicate tool call id"),
        ]
        for value, message in cases:
            with self.subTest(value=value):
                self.write_script(value)
                with self.assertRaisesRegex(ManifestError, message):
                    load_scripted_provider(self.path)


class TestRunTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = os.path.join(self._tmp.name, "task")
        self.fixture = os.path.join(self.task_dir, "fixture")
        os.makedirs(self.fixture)
        with open(os.path.join(self.fixture, "answer.txt"), "w", encoding="utf-8") as fh:
            fh.write("old")
        self.manifest_path = os.path.join(self.task_dir, "task.json")
        self.script_path = os.path.join(self.task_dir, "script.json")
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump({
                "id": "answer",
                "fixture": "fixture",
                "task": "Set the answer.",
                "verify_command": "python3 -c \"assert open('answer.txt').read() == 'new'\"",
                "max_steps": 4,
                "budget_tokens": 8000,
                "allowed_paths": ["answer.txt"],
            }, fh)
        with open(self.script_path, "w", encoding="utf-8") as fh:
            json.dump([
                {"text": "edit", "tool_calls": [{
                    "id": "edit-1", "name": "edit_file",
                    "arguments": {"path": "answer.txt", "old_string": "old", "new_string": "new"},
                }]},
                {"text": "done"},
            ], fh)

    def test_successful_scripted_task(self):
        result = run_task(self.manifest_path, os.path.join(self._tmp.name, "out"))

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.run["stop_reason"], "done")
        self.assertEqual(result.verification["returncode"], 0)
        self.assertTrue(os.path.exists(os.path.join(result.workspace, "answer.txt")))
        with open(os.path.join(self.fixture, "answer.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "old")

    def test_classifies_failure_modes_in_priority_order(self):
        self.assertEqual(classify_run(Run(task="t", stop_reason="provider error: offline"), 0, False), "provider_error")
        self.assertEqual(classify_run(Run(task="t", stop_reason="step limit reached (2 steps)"), 0, False), "step_limit")
        self.assertEqual(classify_run(Run(task="t", stop_reason="done"), 1, False), "verification_failure")
        self.assertEqual(classify_run(Run(task="t", stop_reason="done"), None, True), "verification_failure")
        self.assertEqual(classify_run(Run(task="t", stop_reason="done", tool_errors=1), 0, False), "tool_error")


class TestSuiteReporting(unittest.TestCase):
    def test_suite_continues_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "tasks")
            self.make_task(tasks, "bad", "old", "wrong")
            self.make_task(tasks, "good", "old", "new")

            results = run_suite(tasks, os.path.join(tmp, "out"))
            jsonl_path, summary_path = write_reports(results, os.path.join(tmp, "reports"))

            self.assertEqual([result.task_id for result in results], ["bad", "good"])
            self.assertEqual(results[0].outcome, "verification_failure")
            self.assertEqual(results[1].outcome, "success")
            with open(jsonl_path, encoding="utf-8") as fh:
                self.assertEqual(len(fh.read().splitlines()), 2)
            with open(summary_path, encoding="utf-8") as fh:
                summary = json.load(fh)
            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["outcomes"], {"success": 1, "verification_failure": 1})

    @staticmethod
    def make_task(root, task_id, initial, expected):
        task_dir = os.path.join(root, task_id)
        fixture = os.path.join(task_dir, "fixture")
        os.makedirs(fixture)
        with open(os.path.join(fixture, "answer.txt"), "w", encoding="utf-8") as fh:
            fh.write(initial)
        with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "id": task_id, "fixture": "fixture", "task": "Set answer.",
                "verify_command": f"python3 -c \"assert open('answer.txt').read() == '{expected}'\"",
                "max_steps": 3, "budget_tokens": 8000,
            }, fh)
        with open(os.path.join(task_dir, "script.json"), "w", encoding="utf-8") as fh:
            json.dump([
                {"text": "edit", "tool_calls": [{"id": "edit", "name": "edit_file", "arguments": {
                    "path": "answer.txt", "old_string": initial, "new_string": "new",
                }}]},
                {"text": "done"},
            ], fh)


if __name__ == "__main__":
    unittest.main()
