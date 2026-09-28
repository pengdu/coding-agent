"""Model providers.

The rest of the agent only ever needs one operation:

    complete(messages, tools) -> AssistantTurn

so a provider is anything that turns a conversation into
(text, [ToolCall]). Two implementations ship here:

  MockProvider   - scripted turns, no network, no key. Proves the loop.
  OpenAIProvider - any OpenAI-compatible endpoint (OpenAI, DeepSeek,
                   Moonshot, OpenRouter, SiliconFlow, local vLLM, ...).

Message format is OpenAI chat format. It is the lingua franca here, so
adding a second native protocol (Anthropic tool use, say) means one
adapter that translates both directions -- see translate_notes below.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Message = dict[str, Any]


# --------------------------------------------------------------------------
# wire types
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One model-emitted intent to act. `arguments` is already parsed."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class Provider(Protocol):
    def complete(self, messages: list[Message], tools: list[dict]) -> AssistantTurn:
        ...


# --------------------------------------------------------------------------
# mock
# --------------------------------------------------------------------------

ScriptedTurn = AssistantTurn | str | Callable[[list[Message]], AssistantTurn]


class MockProvider:
    """Replays a fixed list of turns, one per loop step.

    A turn may be a plain AssistantTurn, a bare string (coerced to a
    text-only turn, because writing AssistantTurn(text=...) for a throwaway
    assertion is noise), or a callable that inspects the conversation --
    which is how tests assert that tool results made it back into context.

    Running out of script is an error on purpose: a silent zero-step
    "success" is the worst possible test failure mode.
    """

    def __init__(self, script: list[ScriptedTurn], name: str = "mock") -> None:
        self._script = list(script)
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def turn_count(self) -> int:
        return len(self._script)

    def complete(self, messages: list[Message], tools: list[dict]) -> AssistantTurn:
        idx = len(self.calls)
        if idx >= len(self._script):
            raise AssertionError(
                f"mock provider exhausted: loop asked for turn {idx + 1}, "
                f"script has {len(self._script)}"
            )
        self.calls.append({"messages": list(messages), "tools": tools})

        turn = self._script[idx]
        if callable(turn):
            return turn(messages)
        if isinstance(turn, str):
            return AssistantTurn(text=turn)
        return turn


# --------------------------------------------------------------------------
# openai-compatible
# --------------------------------------------------------------------------


class ProviderError(RuntimeError):
    pass


def tool_to_openai(tool: dict) -> dict:
    """{name, description, parameters} -> chat/completions tools[] entry.

    Validated rather than indexed, so a malformed tool surfaces as a message
    that says which field is missing instead of a bare KeyError.
    """
    missing = [k for k in ("name", "description", "parameters") if k not in tool]
    if missing:
        raise ProviderError(
            f"tool schema is missing {missing}; got keys {sorted(tool)}. "
            "Every tool needs a name, a description, and a parameters schema."
        )
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }


def openai_to_assistant_turn(message: dict, model: str) -> AssistantTurn:
    calls: list[ToolCall] = []
    for i, raw in enumerate(message.get("tool_calls") or []):
        fn = raw.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"model returned unparseable arguments for {fn.get('name')!r}: {exc}"
            ) from exc
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{i}",
                name=fn.get("name") or "",
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return AssistantTurn(text=message.get("content") or "", tool_calls=calls)


class OpenAIProvider:
    """POSTs to {base_url}/chat/completions with urllib. No dependencies."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 180,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        if not self.api_key:
            raise ProviderError(
                "no API key: set OPENAI_API_KEY (and OPENAI_BASE_URL / "
                "OPENAI_MODEL if you are not talking to api.openai.com). "
                "Use MockProvider to run without one."
            )

    def complete(self, messages: list[Message], tools: list[dict]) -> AssistantTurn:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tool_choice": "auto",
        }
        if tools:
            body["tools"] = [tool_to_openai(t) for t in tools]
            body["parallel_tool_calls"] = False

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            raise ProviderError(f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"cannot reach {self.base_url}: {exc.reason}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(f"no choices in response: {payload}")
        return openai_to_assistant_turn(choices[0]["message"], self.model)


# --------------------------------------------------------------------------
# translate_notes
# --------------------------------------------------------------------------
#
# Adding Anthropic (or any other native protocol) means one adapter with
# two pure functions, because the agent's internals speak OpenAI format:
#
#   to_native(messages, tools)     -> request body
#   from_native(response)          -> AssistantTurn
#
# Two mappings are lossy and need care:
#   * Anthropic puts `system` outside `messages`. Our Context may emit
#     several system messages (compaction does), so the adapter has to
#     merge them into one leading system block.
#   * Our compaction summary is injected as a system message; a native
#     port must keep it in the system block, not drop it.
