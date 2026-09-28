"""The agent loop.

Read top to bottom -- this is the whole agent:

    while steps remain:
        turn = model(context, tool schemas)   # decide
        if turn asks for no tools: stop        # done
        for each tool call:                   # act
            result = dispatch(call)
            feed result back verbatim          # observe
        compact context if needed

Everything the loop guarantees, and nothing it does not:

  * a model exception ends the run with that error, never silently.
  * a tool exception is turned into a tool *result* and fed back, so the
    model gets one chance to correct itself. That one retry is the entire
    error-recovery story; anything beyond it is the model's problem to
    report, not ours to paper over.
  * step and tool-call counts are hard-capped, so a confused model loops
    forever instead of burning your money.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Context
from .provider import AssistantTurn, Provider, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools import Tool, ToolDenied, ToolError, Workspace, tool_schemas

MAX_STEPS = 40
MAX_TOOL_CALLS_PER_TURN = 1  # serialized turns: one action, then look again


@dataclass
class Step:
    index: int
    text: str
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    # (name, args-preview, result-headline)


@dataclass
class Run:
    task: str
    steps: list[Step] = field(default_factory=list)
    final_text: str = ""
    stop_reason: str = ""
    tool_calls_made: int = 0
    tool_errors: int = 0
    compactions: int = 0
    turns_used: int = 0
    elapsed: float = 0.0
    trace: str = ""

    @property
    def ok(self) -> bool:
        return self.stop_reason == "done"


def _to_openai_tool_calls(calls: list[ToolCall]) -> list[dict[str, Any]]:
    """Serialize so the model sees its own calls in the transcript.

    Dropping these is a classic bug: the model then has tool results with
    no matching calls and starts hallucinating new ones.
    """
    return [
        {
            "id": c.id,
            "type": "function",
            "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
        }
        for c in calls
    ]


def _preview(args: dict[str, Any]) -> str:
    if not args:
        return ""
    for key in ("path", "command", "pattern", "query"):
        if key in args:
            return f"{key}={str(args[key])[:60]!r}"
    return ", ".join(f"{k}={str(v)[:24]!r}" for k, v in list(args.items())[:2])


def _headline(result: str) -> str:
    for line in result.splitlines():
        s = line.strip()
        if s and not s.startswith("---") and not s.startswith("$ "):
            return s[:90]
    return result.strip().splitlines()[0][:90] if result.strip() else "(empty)"


class Agent:
    def __init__(
        self,
        provider: Provider,
        workspace: str | Workspace,
        system_prompt: str = SYSTEM_PROMPT,
        max_steps: int = MAX_STEPS,
        budget_tokens: int = 60_000,
        keep_recent: int = 12,
    ) -> None:
        self.provider = provider
        self.ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.tools: dict[str, Tool] = self._build_tools()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.budget_tokens = budget_tokens
        self.keep_recent = keep_recent

    def _build_tools(self) -> dict[str, Tool]:
        from .tools import build_tools

        return build_tools(self.ws)

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, call: ToolCall) -> tuple[str, bool]:
        """Run one tool. Returns (result_text, is_error)."""
        tool = self.tools.get(call.name)
        if tool is None:
            available = ", ".join(sorted(self.tools))
            return (
                f"no such tool: {call.name}. Available tools: {available}. "
                "Do not invent tool names.",
                True,
            )
        try:
            return tool.fn(**call.arguments), False
        except (ToolError, ToolDenied) as exc:
            return str(exc), True
        except TypeError as exc:
            return (
                f"bad arguments for {call.name}: {exc}. Check the tool's parameter list; "
                "every required parameter must be present and no extras allowed.",
                True,
            )
        except Exception as exc:  # noqa: BLE001 - a crashing tool must not kill the run
            return (
                f"{call.name} crashed: {type(exc).__name__}: {exc}\n"
                f"{_indent_tail(traceback.format_exc(), 900)}",
                True,
            )

    # -- the loop ---------------------------------------------------------

    def run(self, task: str, on_event: Callable[[str], None] | None = None) -> Run:
        emit = on_event or (lambda _msg: None)
        ctx = Context(self.system_prompt, task, self.budget_tokens, self.keep_recent)
        schemas = tool_schemas(self.tools)
        result = Run(task=task)
        started = time.monotonic()

        for i in range(1, self.max_steps + 1):
            emit(f"\n--- step {i} ---")
            try:
                turn: AssistantTurn = self.provider.complete(ctx.messages, schemas)
            except Exception as exc:  # noqa: BLE001 - surface, never hide
                result.stop_reason = f"provider error: {type(exc).__name__}: {exc}"
                result.trace = traceback.format_exc()
                result.elapsed = time.monotonic() - started
                emit("  ! " + result.stop_reason)
                return result

            result.turns_used = i
            if turn.text.strip():
                emit("  " + first(turn.text.strip(), 300))
            ctx.add_assistant(turn.text, _to_openai_tool_calls(turn.tool_calls))

            if not turn.tool_calls:
                result.final_text = turn.text
                result.stop_reason = "done"
                break

            step = Step(index=i, text=turn.text)
            for call in turn.tool_calls[:MAX_TOOL_CALLS_PER_TURN]:
                output, is_error = self.dispatch(call)
                result.tool_calls_made += 1
                if is_error:
                    result.tool_errors += 1
                step.calls.append((call.name, _preview(call.arguments), _headline(output)))
                # Verbatim. The model must see the real error, not a summary
                # of it, or it will retry the same broken call.
                ctx.add_tool_result(call.id, call.name, output)
                flag = "x" if is_error else "."
                emit(f"  [{flag}] {call.name}({_preview(call.arguments)}) -> {_headline(output)}")
            result.steps.append(step)

            if ctx.compact():
                result.compactions = ctx.compactions
                emit(f"  ~ context compacted ({ctx.tokens} tokens, step {i})")
        else:
            result.final_text = ctx.messages[-1].get("content", "")
            result.stop_reason = f"step limit reached ({self.max_steps} steps)"

        result.compactions = ctx.compactions
        result.elapsed = time.monotonic() - started
        return result


def first(text: str, n: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[:n] + " ..."


def _indent_tail(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text
