"""Conversation context with deterministic compaction.

A coding agent dies of context exhaustion, not of bad code. So this class
owns two things:

  * an honest token estimate (we can't count real tokens without a
    tokenizer, and 1 token ~= 4 chars is close enough to budget on);
  * compaction that keeps the parts a later step cannot reconstruct.

What compaction preserves, in order:
  1. the system prompt and the original task -- the goal never goes away;
  2. the most recent messages -- the immediate state is what the model
     is acting on;
  3. a structured digest of the middle: which tools ran, on which files,
     and the first line of each result.

It drops raw middle content, which is the expensive part. Note this is
extractive, not a summarization call: it needs no extra LLM round trip,
is deterministic, and cannot hallucinate. The tradeoff is fidelity -- if
you need real comprehension here, swap in a model-summarized variant and
keep the same interface.
"""

from __future__ import annotations

import json
from typing import Any

Message = dict[str, Any]

CHARS_PER_TOKEN = 4
DEFAULT_BUDGET_TOKENS = 60_000
DEFAULT_KEEP_RECENT = 12


def estimate_tokens(messages: list[Message]) -> int:
    total = 0
    for m in messages:
        # +8 for per-message role/format overhead, which is roughly right.
        total += (len(json.dumps(m, ensure_ascii=False)) // CHARS_PER_TOKEN) + 8
    return total


def _first_line(text: str, limit: int = 100) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("---"):
            return line[:limit]
    return ""


def digest(messages: list[Message]) -> str:
    """One compact line per step describing what happened."""
    lines = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                for c in calls:
                    fn = c.get("function") or {}
                    args = fn.get("arguments") or "{}"
                    try:
                        parsed = json.loads(args)
                        target = parsed.get("path") or parsed.get("command") or parsed.get("pattern") or ""
                        target = str(target)[:80]
                    except json.JSONDecodeError:
                        target = "<unparseable args>"
                    lines.append(f"- called {fn.get('name')}({target})")
            elif m.get("content"):
                lines.append(f"- said: {_first_line(m['content'], 120)}")
        elif role == "tool":
            head = _first_line(str(m.get("content", "")))
            if head:
                lines.append(f"  -> {head}")
    return "\n".join(lines)


class Context:
    def __init__(
        self,
        system_prompt: str,
        task: str,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ) -> None:
        self.budget_tokens = budget_tokens
        self.keep_recent = keep_recent
        self.messages: list[Message] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        self.compactions = 0

    # -- reads ------------------------------------------------------------

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.messages)

    def over_budget(self) -> bool:
        return self.tokens > self.budget_tokens

    # -- writes -----------------------------------------------------------

    def add_assistant(self, text: str, tool_calls: list[dict] | None = None) -> None:
        msg: Message = {"role": "assistant", "content": text or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content}
        )

    # -- compaction -------------------------------------------------------

    def compact(self) -> bool:
        """Drop the middle if over budget. Returns True if anything changed."""
        if not self.over_budget():
            return False

        # Everything after the task message is fair game to compress.
        head, tail_mid = self.messages[:2], self.messages[2:]
        if len(tail_mid) <= self.keep_recent:
            return False

        middle, recent = tail_mid[: -self.keep_recent], tail_mid[-self.keep_recent :]

        # Never cut inside an assistant/tool exchange. `middle` is a
        # contiguous prefix, so it is internally sound; the only seam is at
        # the head of `recent`. If it starts with a tool result, that result
        # lost the assistant message which called it -- an orphan, and most
        # providers reject the entire request. Trim from the front.
        while recent and recent[0].get("role") == "tool":
            recent.pop(0)
        if not recent or not middle:
            return False

        summary = [
            {
                "role": "system",
                "content": (
                    f"[context compacted] {len(middle)} earlier messages were dropped to stay "
                    f"within the budget. This is their digest -- it is a record of what happened, "
                    f"not a substitute for re-reading a file if you need its exact contents.\n"
                    f"{digest(middle)}"
                ),
            }
        ]
        self.messages = head + summary + recent
        self.compactions += 1
        return True
