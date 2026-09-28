"""A scripted model, so the loop can be exercised with no API key.

The script walks the loop the way a real model does -- look, read,
baseline, misfire, recover, verify -- so running the demo tests the
machinery instead of a happy path.

Several turns are check functions that assert against the conversation
before returning their scripted reply. That is the load-bearing trick: it
verifies tool results really made it back into the context, which is
exactly the property that silently breaks in real agent implementations.
"""

from __future__ import annotations

from agent.provider import AssistantTurn, MockProvider, ToolCall

TASK = (
    "utils.py has a `fetch` that fails intermittently and callers give up on "
    "the first error. add a `retry_call` helper and make the tests pass"
)

RUN_TESTS = "python3 -m unittest discover -q"

# The change, correct. `time` is already imported in the fixture.
GOOD_OLD = "def add(a, b):\n    return a + b\n"
GOOD_NEW = (
    "def add(a, b):\n    return a + b\n"
    "\n"
    "\n"
    "def retry_call(fn, times=3, delay=0.0):\n"
    '    """Call fn, retrying on exception, up to `times` attempts."""\n'
    "    last = None\n"
    "    for _ in range(times):\n"
    "        try:\n"
    "            return fn()\n"
    "        except Exception as exc:  # noqa: BLE001 - the caller decides what is fatal\n"
    "            last = exc\n"
    "            if delay:\n"
    "                time.sleep(delay)\n"
    "    raise last\n"
)

# The same edit, indented with two spaces instead of four. The realistic
# mistake -- and the one edit_file's error message is built to fix.
BAD_OLD = "def add(a, b):\n  return a + b\n"


def _call(name: str, args: dict, i: int) -> ToolCall:
    return ToolCall(id=f"call_{i}", name=name, arguments=args)


def build_script() -> MockProvider:
    """The script, with the loop's own indexing spelled out.

    A tool result is only visible to the *next* turn, so each check hangs on
    the turn after the call that produced it. Getting this off by one is the
    easiest mistake to make when writing a script, and it fails as a baffling
    'expected exit code: 0' against a tool result about something else.
    """
    return MockProvider(
        [
            # --- turn 0: look before touching
            AssistantTurn("Let me see what is in the workspace first.",
                          [_call("list_dir", {"path": "."}, 0)]),

            # --- turn 1: read the target. Sees turn 0's listing.
            _all_tool_output_contains("utils.py", AssistantTurn(
                "Found it. Reading it before I edit.",
                [_call("read_file", {"path": "utils.py"}, 1)])),

            # --- turn 2: baseline. Sees turn 1's file contents.
            _all_tool_output_contains("def add", AssistantTurn(
                "Baseline first -- what do the tests say right now?",
                [_call("run_command", {"command": RUN_TESTS}, 2)])),

            # --- turn 3: reads turn 2's failure, then acts on it.
            _last_tool_result_contains("exit code: 1", AssistantTurn(
                "Failing because retry_call does not exist. Adding it.",
                [_call("edit_file", {
                    "path": "utils.py",
                    "old_string": BAD_OLD,
                    "new_string": GOOD_NEW.replace("    ", "  "),
                }, 3)])),

            # --- turn 4: the edit 404'd. Reacts to the real error instead of
            #     resending the same call -- the guarantee the loop provides.
            _last_tool_result_contains("old_string not found", AssistantTurn(
                "My match had the wrong indentation. The error showed me the real "
                "line, so I can copy it exactly this time.",
                [_call("edit_file", {
                    "path": "utils.py",
                    "old_string": GOOD_OLD,
                    "new_string": GOOD_NEW,
                }, 4)])),

            # --- turn 5: verify by running the tests, not by assuming.
            _last_tool_result_contains("edited utils.py", AssistantTurn(
                "Edit applied. Re-running the tests to verify.",
                [_call("run_command", {"command": RUN_TESTS}, 5)])),

            # --- turn 6: only now is turn 5's result visible. Green.
            _last_tool_result_contains("exit code: 0", AssistantTurn(
                "Green. Reading the file back to confirm the final contents.",
                [_call("read_file", {"path": "utils.py", "limit": 40}, 6)])),

            # --- turn 7: the read-back shows the change is really in the file.
            _all_tool_output_contains("def retry_call", AssistantTurn(
                "Confirmed -- retry_call is in the file. One more test run to be sure.",
                [_call("run_command", {"command": RUN_TESTS}, 7)])),

            # --- turn 8: no tool calls -> the loop stops here.
            AssistantTurn(
                "Added `retry_call(fn, times=3, delay=0.0)` to utils.py. It retries on "
                "exception and re-raises the last one after `times` attempts, so callers "
                "still see the real error.\n\n"
                "Verified: `python3 -m unittest discover -q` went from 1 failure to 2 "
                "passing, and I re-read the file to confirm the final contents."
            ),
        ],
        name="scripted-demo",
    )


# -- assertions on the context -------------------------------------------
# Each wraps a scripted turn and checks it against the live conversation
# before the loop gets to use it. A broken guarantee fails the demo loudly
# instead of passing quietly.


def _tool_outputs(msgs: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "tool")


def _last_tool_result(msgs: list[dict]) -> str:
    for m in reversed(msgs):
        if m.get("role") == "tool":
            return str(m.get("content", ""))
    return ""


def _all_tool_output_contains(needle: str, turn: AssistantTurn):
    def check(msgs: list[dict]) -> AssistantTurn:
        assert needle in _tool_outputs(msgs), (
            f"expected {needle!r} in the tool results; the loop did not feed them back"
        )
        return turn

    return check


def _last_tool_result_contains(needle: str, turn: AssistantTurn):
    def check(msgs: list[dict]) -> AssistantTurn:
        last = _last_tool_result(msgs)
        assert needle in last, (
            f"expected {needle!r} in the most recent tool result, got:\n{last[-500:]}"
        )
        return turn

    return check
