"""CLI tests.

main.py is the only file with no coverage, and it is the one a user types
first. Two things it decides are easy to break silently: where the agent is
allowed to write, and what exit code the shell sees.

Note: the --demo tests write to .demo_run/ beside the package, which is the
documented behavior of the demo (it re-copies the fixture every run). That
directory is gitignored.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as cli  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_workspace")


def _stats(out: str) -> str:
    """The `stop: ... | steps: ...` summary line, without the timing."""
    line = next((ln for ln in out.splitlines() if ln.startswith("stop:")), "")
    return line.split("|")[-2].strip() if line else ""


class TestArgumentValidation(unittest.TestCase):
    def test_a_bare_invocation_explains_itself(self):
        # The first command a new user types. argparse exits 2 on usage error,
        # and the message must reach stderr where a shell will show it.
        for argv in ([], ["--quiet"]):
            err = io.StringIO()
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as ctx, redirect_stderr(err):
                cli.main(argv)
            self.assertEqual(ctx.exception.code, 2, "usage error must be exit 2, not a traceback")
            self.assertIn("give a task, pass --demo, or pass --eval-dir", err.getvalue())

    def test_help_documents_the_flags_a_user_has_to_remember(self):
        # --help is the only interface documentation the CLI has, so a flag
        # that loses its help= text becomes undiscoverable.
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0, "--help must exit 0, not look like an error")
        text = out.getvalue()
        for flag in ("task", "--demo", "--workspace", "--max-steps", "--quiet"):
            self.assertIn(flag, text, f"{flag} is missing from --help")
        # "what does this flag do" beats "what is this flag called".
        self.assertIn("directory the agent may touch", text)


def _run_demo(*extra: str) -> tuple[int, str]:
    """Run the CLI's demo path, returning (exit code, everything it printed)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["--demo", *extra])
    return code, buf.getvalue()


class TestProviderResolution(unittest.TestCase):
    """Which endpoint/model/key a run actually uses.

    A config profile, two flags and the exported key all decide this, and
    every mistake is silent: the agent either talks to the wrong host or
    dies on a 401. These go through the real argparse, because the flags are
    where the wiring actually goes wrong.
    """

    CONFIG = {
        "default": "fast",
        "profiles": {
            "fast": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                     "api_key_env": "DS_KEY"},
            "local": {"base_url": "http://localhost:8000/v1", "model": "qwen-max",
                      "api_key_env": None, "timeout": 45},
        },
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = os.path.join(self._tmp.name, "agent.json")
        with open(self.cfg, "w") as fh:
            json.dump(self.CONFIG, fh)

    def resolve(self, *argv, env=None):
        args = cli.build_parser().parse_args(["--config", self.cfg, *argv, "some task"])
        with mock.patch.dict(os.environ, env or {}, clear=True):
            return cli.resolve_provider(args)

    def test_the_default_profile_is_used_when_nothing_else_is_said(self):
        # The whole point of a config file: no flags at all.
        with self.assertRaises(SystemExit) as ctx:
            self.resolve()
        self.assertIn("DS_KEY", str(ctx.exception))

    def test_a_profile_supplies_its_own_endpoint_and_key(self):
        p = self.resolve("--profile", "local")
        self.assertEqual(p.base_url, "http://localhost:8000/v1")
        self.assertEqual(p.model, "qwen-max")
        self.assertEqual(p.timeout, 45, "the profile's timeout must reach the provider")

    def test_a_profile_needs_no_key_to_be_selected(self):
        # `api_key_env: null` is a local vLLM: nothing to export, and the
        # provider must not invent a key for it.
        self.assertEqual(self.resolve("--profile", "local").api_key, "")

    def test_the_exported_key_picks_the_profile_when_none_is_named(self):
        p = self.resolve(env={"DS_KEY": "sk-1"})
        self.assertEqual((p.base_url, p.model, p.api_key),
                         ("https://api.deepseek.com", "deepseek-chat", "sk-1"))

    def test_flags_override_the_profile(self):
        # --model without --base-url is the case that used to need both set by
        # hand, which is what made switching models a chore.
        p = self.resolve("--profile", "local", "--model", "qwen-plus", env={})
        self.assertEqual(p.model, "qwen-plus")
        self.assertEqual(p.base_url, "http://localhost:8000/v1", "the profile's url stays")

    def test_a_named_profile_wins_over_the_exported_key(self):
        p = self.resolve("--profile", "local", env={"DS_KEY": "sk-1"})
        self.assertEqual(p.api_key, "", "explicit choice beats inference")

    def test_an_unknown_profile_lists_the_ones_that_exist(self):
        with self.assertRaises(Exception) as ctx:
            self.resolve("--profile", "ghost", env={})
        msg = str(ctx.exception)
        self.assertIn("ghost", msg)
        self.assertIn("fast", msg)
        self.assertIn("local", msg)

    def test_a_missing_key_names_the_variable_and_an_alternative(self):
        # Otherwise the user has to read the config file to find out what to
        # export, and may well pick the wrong variable.
        with self.assertRaises(SystemExit) as ctx:
            self.resolve("--profile", "fast", env={})
        msg = str(ctx.exception)
        self.assertIn("DS_KEY", msg)
        self.assertIn("--profile local", msg)

    def test_a_broken_config_stops_the_run_before_any_request(self):
        with open(self.cfg, "w") as fh:
            json.dump({"profiles": {"p": {"base_url": "http://x"}}}, fh)
        with self.assertRaises(Exception) as ctx:
            self.resolve("--profile", "p", env={})
        self.assertIn("model", str(ctx.exception))


class TestListProfiles(unittest.TestCase):
    """--list-profiles is how a user finds out what to pass to --profile."""

    def test_it_names_the_default_and_says_which_keys_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "agent.json")
            with open(cfg, "w") as fh:
                json.dump(
                    {"default": "a", "profiles": {
                        "a": {"base_url": "http://a", "model": "m", "api_key_env": "SET_ONE"},
                        "b": {"base_url": "http://b", "model": "m", "api_key_env": "UNSET_TWO"},
                        "c": {"base_url": "http://c", "model": "m", "api_key_env": None},
                    }},
                    fh,
                )
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"SET_ONE": "x"}, clear=True), redirect_stdout(out):
                code = cli.main(["--config", cfg, "--list-profiles"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("a (default)", text)
        self.assertIn("SET_ONE is set", text)
        self.assertIn("UNSET_TWO is NOT set", text)
        self.assertIn("no auth needed", text)

    def test_it_works_without_a_task_or_a_key(self):
        # Listing what exists must not require having any of it.
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
            code = cli.main(["--list-profiles"])
        self.assertEqual(code, 0)
        self.assertIn("openai", out.getvalue())

    def test_the_config_path_is_reported(self):
        # With several possible config files, "which one am I looking at" is
        # the first question a user has.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "agent.json")
            with open(cfg, "w") as fh:
                json.dump({"profiles": {"p": {"base_url": "http://p", "model": "m"}}}, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["--config", cfg, "--list-profiles"])
        self.assertIn(cfg, out.getvalue())


class TestConfigErrorsReachTheUser(unittest.TestCase):
    """A bad config is the user's typo, not a crash.

    The config is hand-written, so its failure has to arrive as one actionable
    line. A traceback would bury that line under library frames, which is the
    difference between a two-second fix and a debugging session.
    """

    def run_cli(self, data, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "agent.json")
            with open(cfg, "w") as fh:
                json.dump(data, fh)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--config", cfg, *argv, "a task"])
            return code, out.getvalue() + err.getvalue()

    def test_a_typo_in_the_config_prints_the_field_and_exits_2(self):
        code, text = self.run_cli(
            {"profiles": {"p": {"base_url": "http://x", "model": "m", "base_uri": "typo"}}},
            "--profile", "p",
        )
        self.assertEqual(code, 2)
        self.assertIn("base_uri", text)
        self.assertNotIn("Traceback", text)

    def test_an_unknown_profile_name_prints_not_a_traceback(self):
        code, text = self.run_cli(
            {"profiles": {"p": {"base_url": "http://x", "model": "m"}}}, "--profile", "ghost"
        )
        self.assertEqual(code, 2)
        self.assertIn("ghost", text)
        self.assertIn("Known: p", text, "it should list what is available")
        self.assertNotIn("Traceback", text)

    def test_a_malformed_config_file_prints_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "agent.json")
            with open(cfg, "w") as fh:
                fh.write('{"profiles": {"a": {"base_url": "http://x",}}')
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["--config", cfg, "--list-profiles"])
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_an_inline_key_is_refused_before_anything_is_sent(self):
        code, text = self.run_cli(
            {"profiles": {"p": {"base_url": "http://x", "model": "m", "api_key": "sk-oops"}}}
        )
        self.assertEqual(code, 2)
        self.assertIn("committed", text)


class TestDemoRun(unittest.TestCase):
    """End to end through the CLI.

    A default-flag run happens once per class and is shared by the read-only
    assertions below. The three tests that vary a flag each need their own
    run, but no more: re-running the demo per assertion buys nothing and
    turns a 1s check into a 6s one.
    """

    @classmethod
    def setUpClass(cls):
        cls.code, cls.out = _run_demo()

    def test_demo_completes_and_exits_zero(self):
        self.assertEqual(self.code, 0, self.out)
        self.assertIn("stop: done", self.out)
        self.assertIn("steps: 9", self.out)

    def test_demo_announces_the_workspace_it_used(self):
        # The agent can write anywhere under this path, so the user has to be
        # told which directory that is before the run, not after.
        self.assertIn("workspace:", self.out)
        self.assertIn(".demo_run", self.out)

    def test_quiet_keeps_the_answer_but_drops_the_step_events(self):
        code, out = _run_demo("--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("--- step 1 ---", self.out, "default output should be step-by-step")
        self.assertNotIn("--- step 1 ---", out, "--quiet must suppress step events")
        self.assertIn("retry_call", out, "the final answer is not a step event")

    def test_a_second_run_gives_the_same_result(self):
        # The demo edits files, so a second run has to start from a clean copy
        # again. Same stats twice is the observable form of "it re-copies".
        code, out = _run_demo()
        self.assertEqual(code, 0, out)
        self.assertEqual(_stats(out), _stats(self.out))
        with open(os.path.join(FIXTURE, "utils.py")) as fh:
            self.assertNotIn("def retry_call", fh.read(), "the demo mutated the shared fixture")

    def test_an_explicit_workspace_is_honoured_over_the_demo_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, out = _run_demo("--workspace", tmp)
            # The scripted demo edits utils.py, so an empty dir makes it fail.
            # That is fine: the assertion is about *where* it was pointed.
            self.assertIn(os.path.realpath(tmp), out)
            self.assertNotIn(".demo_run", out)


class TestEvaluationCli(unittest.TestCase):
    def test_eval_dir_runs_without_resolving_a_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = os.path.join(tmp, "tasks", "answer")
            fixture = os.path.join(task_dir, "fixture")
            os.makedirs(fixture)
            with open(os.path.join(fixture, "answer.txt"), "w") as fh:
                fh.write("old")
            subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], cwd=fixture, check=True)
            with open(os.path.join(task_dir, "task.json"), "w") as fh:
                json.dump({"id": "answer", "fixture": "fixture", "task": "Set answer.", "verify_command": "python3 -c \"assert open('answer.txt').read() == 'new'\"", "max_steps": 3, "budget_tokens": 8000}, fh)
            with open(os.path.join(task_dir, "script.json"), "w") as fh:
                json.dump([{"text": "edit", "tool_calls": [{"id": "edit", "name": "edit_file", "arguments": {"path": "answer.txt", "old_string": "old", "new_string": "new"}}]}, {"text": "done"}], fh)
            output = os.path.join(tmp, "out")
            with mock.patch.dict(os.environ, {}, clear=True):
                code = cli.main(["--eval-dir", os.path.join(tmp, "tasks"), "--eval-output", output])
            self.assertEqual(code, 0)
            self.assertEqual(len(list(Path(output).glob("*/summary.json"))), 1)


if __name__ == "__main__":
    unittest.main()
