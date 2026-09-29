import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.manifest import load_manifest  # noqa: E402
from evaluation.runner import run_suite  # noqa: E402
from evaluation.task_provider import load_scripted_provider  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = ROOT / "evaluation" / "tasks"
KINDS = ("bugfix", "cross-file", "test-change", "refactor", "cli", "recovery", "safety", "compaction")


class TestTaskCatalog(unittest.TestCase):
    def test_exactly_twenty_four_task_manifests_exist(self):
        manifests = sorted(TASKS_ROOT.glob("*/task.json"))
        self.assertEqual(len(manifests), 24)
        self.assertEqual(
            {load_manifest(str(path)).id for path in manifests},
            {f"{kind}-{number:02d}" for kind in KINDS for number in range(1, 4)},
        )
    def test_categories_have_semantic_task_shapes(self):
        manifests = {load_manifest(str(path)).id: (path, load_manifest(str(path))) for path in TASKS_ROOT.glob("*/task.json")}
        for category in KINDS:
            category_items = [(path, manifest) for path, manifest in manifests.values() if manifest.category == category]
            self.assertEqual(len(category_items), 3, category)
            for path, manifest in category_items:
                script = (path.parent / "script.json").read_text(encoding="utf-8")
                sources = list((path.parent / "fixture").glob("*.py"))
                with self.subTest(task=manifest.id):
                    self.assertIn("category", (path.parent / "task.json").read_text(encoding="utf-8"))
                    if category in {"cross-file", "refactor"}:
                        self.assertGreaterEqual(len(sources), 2)
                    if category == "recovery":
                        self.assertIn('"old_string": "missing"', script)
                    if category == "safety":
                        self.assertEqual(manifest.expected_outcome, "safety_denied")
                        self.assertTrue((path.parent / "fixture" / "sentinel.txt").exists())
                    if category == "compaction":
                        self.assertGreaterEqual(script.count('"name": "read_file"'), 8)
                    if category == "test-change":
                        self.assertTrue(any(item.name.startswith("test_") for item in sources))
                    if category == "cli":
                        self.assertIn("app.py", manifest.verify_command)

        manifests = sorted(TASKS_ROOT.glob("*/task.json"))
        for path in manifests:
            with self.subTest(manifest=path.parent.name):
                manifest = load_manifest(str(path))
                provider = load_scripted_provider(str(path.parent / "script.json"))
                self.assertGreater(provider.turn_count, 0)
                self.assertTrue((path.parent / "fixture").is_dir())
                self.assertTrue(manifest.verify_command)

    def test_full_suite_completes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as output:
            results = run_suite(str(TASKS_ROOT), output)
        failures = [result for result in results if result.outcome != result.expected_outcome]
        self.assertEqual([], failures, [(result.task_id, result.outcome, result.workspace) for result in failures])


if __name__ == "__main__":
    unittest.main()
