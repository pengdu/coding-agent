import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.manifest import ManifestError, load_manifest  # noqa: E402


class TestLoadManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = os.path.join(self._tmp.name, "task")
        os.makedirs(os.path.join(self.task_dir, "fixture"))
        self.path = os.path.join(self.task_dir, "task.json")

    def write_manifest(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_missing_task_is_actionable(self):
        data = self.valid_manifest()
        del data["task"]
        self.write_manifest(data)

        with self.assertRaisesRegex(ManifestError, r"missing required field\(s\): task"):
            load_manifest(self.path)

    def test_rejects_fixture_paths_outside_the_task_directory(self):
        for fixture, message in (("../outside", "must stay under"), ("/tmp/x", "must be relative")):
            with self.subTest(fixture=fixture):
                self.write_manifest(self.valid_manifest(fixture=fixture))
                with self.assertRaisesRegex(ManifestError, message):
                    load_manifest(self.path)

    def test_rejects_unknown_fields_and_non_positive_limits(self):
        self.write_manifest({**self.valid_manifest(), "extra": True})
        with self.assertRaisesRegex(ManifestError, "unknown field"):
            load_manifest(self.path)

        self.write_manifest({**self.valid_manifest(), "max_steps": 0})
        with self.assertRaisesRegex(ManifestError, "max_steps must be a positive integer"):
            load_manifest(self.path)

    def test_loads_resolved_fixture_and_defaults(self):
        self.write_manifest({
            **self.valid_manifest(),
            "allowed_paths": ["src", "tests"],
        })

        manifest = load_manifest(self.path)

        self.assertEqual(manifest.fixture_dir, os.path.realpath(os.path.join(self.task_dir, "fixture")))
        self.assertEqual(manifest.allowed_paths, ("src", "tests"))
        self.assertEqual(manifest.timeout_seconds, 60)

    @staticmethod
    def valid_manifest(**overrides):
        return {
            "id": "x",
            "fixture": "fixture",
            "task": "Change the code.",
            "verify_command": "python3 -m unittest -q",
            "max_steps": 8,
            "budget_tokens": 8000,
            **overrides,
        }


if __name__ == "__main__":
    unittest.main()
