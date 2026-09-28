"""Context and compaction tests.

Compaction is the part of an agent that only fails in production: it
works fine on turn 3 and corrupts the transcript on turn 40. So the
invariants get tested directly rather than waited for.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.context import Context, estimate_tokens  # noqa: E402


def fill(ctx: Context, n: int, size: int = 400) -> None:
    """Append n realistic assistant/tool pairs, with real tool_call records."""
    for i in range(n):
        ctx.add_assistant(
            f"thinking {i} " + "x" * size,
            tool_calls=[
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'},
                }
            ],
        )
        ctx.add_tool_result(f"c{i}", "read_file", f"result {i} " + "y" * size)


class TestEstimate(unittest.TestCase):
    def test_scales_with_content(self):
        small = [{"role": "user", "content": "hi"}]
        big = [{"role": "user", "content": "hi" * 4000}]
        self.assertGreater(estimate_tokens(big), estimate_tokens(small) * 100)

    def test_empty_input_costs_nothing(self):
        self.assertEqual(estimate_tokens([]), 0)

    def test_counts_every_message(self):
        two = [{"role": "user", "content": "x" * 400} for _ in range(2)]
        self.assertEqual(estimate_tokens(two), 2 * estimate_tokens(two[:1]))


class TestNoCompactionNeeded(unittest.TestCase):
    def test_small_context_is_untouched(self):
        ctx = Context("sys", "task", budget_tokens=100_000)
        before = list(ctx.messages)
        self.assertFalse(ctx.over_budget())
        self.assertFalse(ctx.compact())
        self.assertEqual(ctx.messages, before)
        self.assertEqual(ctx.compactions, 0)


class TestCompaction(unittest.TestCase):
    def make(self, budget=2000, keep=6):
        ctx = Context("SYSTEM", "THE TASK", budget_tokens=budget, keep_recent=keep)
        fill(ctx, 20)
        return ctx

    def test_triggers_when_over_budget(self):
        ctx = self.make()
        self.assertTrue(ctx.over_budget())
        self.assertTrue(ctx.compact())
        self.assertEqual(ctx.compactions, 1)

    def test_ends_up_under_budget(self):
        ctx = self.make()
        ctx.compact()
        self.assertLessEqual(ctx.tokens, ctx.budget_tokens)

    def test_preserves_system_and_task(self):
        ctx = self.make()
        ctx.compact()
        self.assertEqual(ctx.messages[0], {"role": "system", "content": "SYSTEM"})
        self.assertEqual(ctx.messages[1], {"role": "user", "content": "THE TASK"})

    def test_preserves_the_most_recent_turns(self):
        ctx = self.make(keep=6)
        before = list(ctx.messages)
        ctx.compact()
        # The seam may trim a leading orphan tool result, so the guarantee is
        # about the tail: the last few messages must survive verbatim.
        self.assertEqual(ctx.messages[-4:], before[-4:])

    def test_the_summary_sits_between_the_task_and_the_recent_window(self):
        ctx = self.make(keep=6)
        ctx.compact()
        self.assertEqual(ctx.messages[0]["role"], "system")
        self.assertEqual(ctx.messages[1]["content"], "THE TASK")
        self.assertIn("context compacted", ctx.messages[2]["content"])

    def test_digest_records_what_happened(self):
        ctx = self.make()
        ctx.compact()
        summary = "\n".join(str(m["content"]) for m in ctx.messages if m.get("role") == "system")
        self.assertIn("context compacted", summary)
        self.assertIn("called read_file", summary)
        self.assertIn("dropped", summary)

    def test_never_leaves_an_orphaned_tool_result(self):
        # A tool result whose assistant message was compacted away breaks the
        # request shape; most providers reject the whole call.
        for keep in range(2, 14):
            with self.subTest(keep=keep):
                ctx = Context("s", "t", budget_tokens=2000, keep_recent=keep)
                fill(ctx, 20)
                if ctx.compact():
                    roles = [m["role"] for m in ctx.messages]
                    for i, role in enumerate(roles):
                        if role == "tool":
                            self.assertEqual(
                                roles[i - 1], "assistant", f"orphan tool result with keep={keep}"
                            )

    def test_is_idempotent_enough_not_to_loop(self):
        ctx = self.make()
        ctx.compact()
        tokens_after_first = ctx.tokens
        ctx.compact()
        self.assertLessEqual(ctx.tokens, tokens_after_first + 1)

    def test_compaction_count_accumulates_across_runs(self):
        ctx = self.make()
        ctx.compact()
        fill(ctx, 20, size=600)
        ctx.compact()
        self.assertEqual(ctx.compactions, 2)


class TestShortContexts(unittest.TestCase):
    def test_too_few_messages_to_compact_is_a_no_op(self):
        ctx = Context("s", "t", budget_tokens=1, keep_recent=100)
        fill(ctx, 3)
        before = list(ctx.messages)
        self.assertFalse(ctx.compact())
        self.assertEqual(ctx.messages, before)

    def test_summarizes_even_with_no_tool_output(self):
        ctx = Context("s", "t", budget_tokens=50, keep_recent=2)
        for i in range(20):
            ctx.add_assistant(f"message {i} " + "z" * 200)
        self.assertTrue(ctx.compact())
        self.assertLessEqual(len(ctx.messages), 2 + 1 + 2)


if __name__ == "__main__":
    unittest.main()
