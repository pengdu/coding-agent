"""Loop tests.

The loop is the thing that must not have bugs, because every other layer
is downstream of it. The properties tested here are the ones that, when
broken, produce an agent that looks like it is working:

  * failures are surfaced, never swallowed;
  * tool errors reach the model verbatim, so it can correct itself;
  * a confused model is capped, not billed forever;
  * the transcript stays in a shape the provider will accept.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent, MockProvider, Workspace  # noqa: E402
from agent.provider import AssistantTurn, ProviderError, ToolCall  # noqa: E402
from demo import TASK, build_script  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_workspace")


def call(name, args, i=1):
    return ToolCall(id=f"c{i}", name=name, arguments=args)


def turn(text="", calls=None):
    return AssistantTurn(text=text, tool_calls=calls or [])


class LoopTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace(self._tmp.name)

    def fixture_workspace(self) -> str:
        """A copy of demo_workspace, so tests never mutate the real one."""
        dest = os.path.join(self._tmp.name, "proj")
        shutil.copytree(FIXTURE, dest)
        return dest

    def agent(self, provider, **kw):
        return Agent(provider=provider, workspace=self.ws, **kw)


class TestScriptedDemo(unittest.TestCase):
    """End to end. Fails if any guarantee in the loop regresses.

    All nine tests below assert on properties of *one* run, so the run happens
    once per class. Per-test setup meant paying the whole 9-turn agent for
    every assertion, with no extra coverage -- a General Fixture whose only
    effect was to make the suite slow.
    """

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        # A copy of the fixture, so the demo never mutates the shared one.
        cls.proj = os.path.join(tmp.name, "proj")
        shutil.copytree(FIXTURE, cls.proj)
        cls.provider = build_script()
        cls.events = []
        cls.demo_run = Agent(provider=cls.provider, workspace=cls.proj).run(
            TASK, on_event=cls.events.append
        )

    def test_completes_normally(self):
        self.assertEqual(self.demo_run.stop_reason, "done")
        self.assertTrue(self.demo_run.ok)
        self.assertEqual(self.demo_run.turns_used, 9)

    def test_the_deliberate_bad_edit_produced_exactly_one_error(self):
        self.assertEqual(self.demo_run.tool_errors, 1)
        self.assertEqual(self.demo_run.tool_calls_made, 8)

    def test_the_file_was_actually_changed(self):
        with open(os.path.join(self.proj, "utils.py")) as fh:
            self.assertIn("def retry_call", fh.read())

    def test_and_the_projects_tests_now_pass(self):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-q"],
            cwd=self.proj,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    def test_final_message_reports_verification(self):
        lowered = self.demo_run.final_text.lower()
        self.assertIn("retry_call", lowered)
        self.assertIn("verified", lowered)

    def test_events_were_emitted_for_each_step(self):
        self.assertTrue(any("step 1" in e for e in self.events))
        self.assertTrue(any("edit_file" in e for e in self.events))
        # the failing edit is flagged, not shown as success
        self.assertTrue(any(e.strip().startswith("[x]") for e in self.events))

    def test_transcript_never_orphans_a_tool_result(self):
        for step in self.provider.calls:
            roles = [m.get("role") for m in step["messages"]]
            for i, role in enumerate(roles):
                if role == "tool":
                    self.assertEqual(roles[i - 1], "assistant", "orphan tool result")

    def test_every_tool_result_matches_a_recorded_call_id(self):
        msgs = self.provider.calls[-1]["messages"]
        for i, m in enumerate(msgs):
            if m.get("role") != "tool":
                continue
            prior = msgs[i - 1]
            ids = [c["id"] for c in prior.get("tool_calls", [])]
            self.assertIn(m["tool_call_id"], ids)

    def test_the_fixture_itself_was_untouched(self):
        with open(os.path.join(FIXTURE, "utils.py")) as fh:
            self.assertNotIn("def retry_call", fh.read(), "test mutated the shared fixture")


class TestFeedback(LoopTestCase):
    def test_tool_errors_reach_the_model_verbatim(self):
        # A summarized error is worse than no error: the model cannot correct
        # a cause it was never shown.
        with open(os.path.join(self.ws.root, "boom.sh"), "w") as fh:
            fh.write("echo 'THE EXACT ERROR TEXT' 1>&2\nexit 7\n")
        provider = MockProvider(
            [
                turn("run it", [call("run_command", {"command": "sh boom.sh"})]),
                turn("done"),
            ]
        )
        self.agent(provider).run("t")
        seen = "\n".join(str(m.get("content", "")) for m in provider.calls[1]["messages"])
        self.assertIn("THE EXACT ERROR TEXT", seen)
        self.assertIn("exit code: 7", seen)

    def test_oversized_output_keeps_the_tail_where_errors_are(self):
        provider = MockProvider(
            [
                turn("go", [call("run_command", {"command": "python3 -c \"print('HEAD' + 'h'*40000); "
                                                          "import sys; print('TAIL_ERRLINE', file=sys.stderr)\""})]),
                turn("done"),
            ]
        )
        self.agent(provider).run("t")
        last = [m for m in provider.calls[1]["messages"] if m.get("role") == "tool"][-1]["content"]
        self.assertIn("TAIL_ERRLINE", last)
        self.assertIn("truncated", last)
        self.assertLess(len(last), 20_000)

    def test_actionable_edit_error_travels_back_and_the_model_can_fix_it(self):
        with open(os.path.join(self.ws.root, "a.py"), "w") as fh:
            fh.write("def add(a, b):\n    return a + b\n")
        provider = MockProvider(
            [
                turn("bad indent", [call("edit_file", {
                    "path": "a.py",
                    "old_string": "def add(a, b):\n  return a + b\n",
                    "new_string": "nope",
                })]),
                turn("fixed", [call("edit_file", {
                    "path": "a.py",
                    "old_string": "def add(a, b):\n    return a + b\n",
                    "new_string": "def add(a, b):\n    return a * b\n",
                })]),
                turn("done"),
            ]
        )
        result = self.agent(provider).run("t")
        self.assertEqual(result.tool_errors, 1)
        with open(os.path.join(self.ws.root, "a.py")) as fh:
            self.assertIn("return a * b", fh.read())


class TestFailureModes(LoopTestCase):
    def test_provider_errors_are_surfaced_not_swallowed(self):
        class Boom:
            def complete(self, messages, tools):
                raise ProviderError("connection reset by peer")

        result = self.agent(Boom()).run("t")
        self.assertFalse(result.ok)
        self.assertIn("provider error", result.stop_reason)
        self.assertIn("connection reset by peer", result.stop_reason)
        self.assertIn("Traceback", result.trace)

    def test_batched_tool_calls_are_all_answered(self):
        # Real models batch calls even when asked not to. Answering only the
        # first leaves the assistant message carrying two tool_calls with one
        # tool result, and the provider rejects the next request -- which is
        # how this was found, against a live API. Every id must be answered.
        provider = MockProvider(
            [
                turn(
                    "two at once",
                    [call("list_dir", {"path": "."}, 1), call("list_dir", {"path": "."}, 2)],
                ),
                turn("done"),
            ]
        )
        r = self.agent(provider).run("t")
        self.assertTrue(r.ok, r.stop_reason)
        self.assertEqual(r.tool_calls_made, 2)
        asked = [c["id"] for m in provider.calls[1]["messages"] if m["role"] == "assistant"
                 for c in m["tool_calls"]]
        answered = [m["tool_call_id"] for m in provider.calls[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(sorted(asked), sorted(answered), "every tool_call needs a tool result")

    def test_step_limit_stops_a_looping_model(self):
        provider = MockProvider(
            [turn("again", [call("list_dir", {"path": "."}, i)]) for i in range(1, 30)]
        )
        result = self.agent(provider, max_steps=3).run("t")
        self.assertFalse(result.ok)
        self.assertIn("step limit reached (3 steps)", result.stop_reason)
        self.assertEqual(result.turns_used, 3)
        self.assertEqual(result.tool_calls_made, 3)

    def test_step_limit_reports_the_models_words_not_a_tool_result(self):
        # On this exit path the last message in the context is a tool result,
        # so reporting `messages[-1]` would hand the user a directory listing
        # and call it the agent's answer.
        with open(os.path.join(self.ws.root, "m.txt"), "w") as fh:
            fh.write("contents\n")
        provider = MockProvider(
            [turn("I am still thinking", [call("read_file", {"path": "m.txt"}, i)]) for i in range(1, 6)]
        )
        result = self.agent(provider, max_steps=3).run("t")
        self.assertEqual(result.final_text, "I am still thinking")
        self.assertNotIn("contents", result.final_text)

    def test_exhausted_mock_is_an_error_not_a_silent_success(self):
        # Two scripted turns, both asking for another tool. The loop asks for
        # a third and the mock has none. That must surface as a failure --
        # the loop turns it into a provider error rather than raising, which
        # is what stops a real crash from killing the process.
        provider = MockProvider(
            [turn("more", [call("list_dir", {"path": "."}, i)]) for i in (1, 2)]
        )
        result = self.agent(provider, max_steps=5).run("t")
        self.assertFalse(result.ok)
        self.assertIn("exhausted", result.stop_reason)
        self.assertEqual(result.turns_used, 2, "the failing turn must not be counted")

    def test_unknown_tool_is_reported_to_the_model(self):
        result, is_error = self.agent(MockProvider([])).dispatch(call("frobnicate", {}))
        self.assertTrue(is_error)
        self.assertIn("no such tool", result)
        self.assertIn("read_file", result)

    def test_bad_arguments_are_reported_not_raised(self):
        result, is_error = self.agent(MockProvider([])).dispatch(call("read_file", {"nope": 1}))
        self.assertTrue(is_error)
        self.assertIn("bad arguments", result)

    def test_a_crashing_tool_does_not_kill_the_run(self):
        agent = self.agent(MockProvider([]))

        def explode(**_kwargs):
            raise RuntimeError("segfault-ish")

        agent.tools["list_dir"].fn = explode
        result, is_error = agent.dispatch(call("list_dir", {"path": "."}))
        self.assertTrue(is_error)
        self.assertIn("list_dir crashed", result)
        self.assertIn("segfault-ish", result)

    def test_a_denial_is_reported_as_an_error_result(self):
        result, is_error = self.agent(MockProvider([])).dispatch(
            call("run_command", {"command": "rm -rf /"})
        )
        self.assertTrue(is_error)
        self.assertIn("blocked", result)

    def test_workspace_escape_surfaces_as_a_tool_error(self):
        result, is_error = self.agent(MockProvider([])).dispatch(
            call("read_file", {"path": "/etc/passwd"})
        )
        self.assertTrue(is_error)
        self.assertIn("outside the workspace", result)


    def test_denials_are_structured_even_when_the_model_does_not_repeat_them(self):
        provider = MockProvider([
            turn("try", [call("run_command", {"command": "rm -rf /"})]),
            turn("I cannot do that"),
        ])

        result = self.agent(provider).run("t")

        self.assertEqual(result.tool_denials, 1)
        self.assertTrue(result.trace_events[0]["is_denied"])
        self.assertEqual(result.trace_events[0]["tool"], "run_command")
        self.assertGreater(result.estimated_tokens, 0)


class TestTranscript(LoopTestCase):
    def test_assistant_tool_calls_are_recorded_so_the_model_sees_its_own_actions(self):
        provider = MockProvider(
            [turn("looking", [call("list_dir", {"path": "."})]), turn("done")]
        )
        self.agent(provider).run("t")
        assistant = [m for m in provider.calls[-1]["messages"] if m.get("role") == "assistant"][0]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "list_dir")
        self.assertIn("arguments", assistant["tool_calls"][0]["function"])

    def test_final_answer_with_no_tool_calls_ends_the_run(self):
        provider = MockProvider([turn("all done, nothing more to do")])
        result = self.agent(provider).run("t")
        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.final_text, "all done, nothing more to do")
        self.assertEqual(result.tool_calls_made, 0)

    def test_every_tool_is_advertised_to_the_model(self):
        result = self.agent(MockProvider([turn("done")])).run("t")
        self.assertTrue(result.ok)
        self.assertEqual(len(self.agent(MockProvider([])).tools), 6)

    def test_compaction_runs_during_a_real_session(self):
        provider = MockProvider(
            [turn("read", [call("read_file", {"path": "big.txt"})]) for _ in range(1, 9)]
            + [turn("done")]
        )
        with open(os.path.join(self.ws.root, "big.txt"), "w") as fh:
            fh.write("line of text\n" * 4000)
        result = self.agent(provider, budget_tokens=1500, keep_recent=6).run("t")
        self.assertGreater(result.compactions, 0)
        self.assertEqual(result.stop_reason, "done")

    def test_task_stays_in_context_after_compaction(self):
        provider = MockProvider(
            [turn("read", [call("read_file", {"path": "big.txt"})]) for _ in range(1, 9)]
            + [turn("done")]
        )
        with open(os.path.join(self.ws.root, "big.txt"), "w") as fh:
            fh.write("line of text\n" * 4000)
        agent = self.agent(provider, budget_tokens=1500, keep_recent=6)
        agent.run("THE ACTUAL TASK")
        final_ctx = provider.calls[-1]["messages"]
        self.assertEqual(final_ctx[0]["role"], "system")
        self.assertEqual(final_ctx[1]["content"], "THE ACTUAL TASK")


class TestWorkspaceCreation(LoopTestCase):
    def test_workspace_is_created_if_missing(self):
        nested = os.path.join(self._tmp.name, "does", "not", "exist")
        ws = Workspace(nested)
        self.assertTrue(os.path.isdir(ws.root))

    def test_symlinked_tmpdir_is_resolved_so_containment_holds(self):
        # macOS puts temp dirs behind a symlink; containment must compare
        # resolved paths or every path looks like an escape.
        real = os.path.join(self._tmp.name, "real")
        os.makedirs(real)
        link = os.path.join(self._tmp.name, "link")
        os.symlink(real, link)
        ws = Workspace(link)
        self.assertEqual(ws.root, os.path.realpath(real))

        with open(os.path.join(ws.root, "f.txt"), "w") as fh:
            fh.write("inside\n")
        resolved = ws.resolve("f.txt")
        self.assertTrue(resolved.startswith(ws.root))
        with open(resolved) as fh:
            self.assertEqual(fh.read(), "inside\n")


if __name__ == "__main__":
    unittest.main()
