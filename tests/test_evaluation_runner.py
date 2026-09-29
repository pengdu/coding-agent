import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.manifest import ManifestError  # noqa: E402
from evaluation.task_provider import load_scripted_provider  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
