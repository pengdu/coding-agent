from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.provider import AssistantTurn, MockProvider, ToolCall

from .manifest import ManifestError


def load_scripted_provider(path: str) -> MockProvider:
    script_path = Path(path)
    try:
        raw = json.loads(script_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read script {script_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"script {script_path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ManifestError(f"script {script_path} must be a JSON array")

    turns: list[AssistantTurn] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        turns.append(_turn(item, script_path, index, seen_ids))
    return MockProvider(turns, name=f"eval:{script_path.parent.name}")


def _turn(item: Any, path: Path, index: int, seen_ids: set[str]) -> AssistantTurn:
    if not isinstance(item, dict):
        raise ManifestError(f"script {path} turn {index} must be an object")
    unknown = sorted(set(item) - {"text", "tool_calls"})
    if unknown:
        raise ManifestError(f"script {path} turn {index} has unknown field(s): {unknown}")
    text = item.get("text", "")
    if not isinstance(text, str):
        raise ManifestError(f"script {path} turn {index} field text must be a string")
    calls = item.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ManifestError(f"script {path} turn {index} field tool_calls must be an array")
    return AssistantTurn(text=text, tool_calls=[_call(raw, path, index, seen_ids) for raw in calls])


def _call(raw: Any, path: Path, turn_index: int, seen_ids: set[str]) -> ToolCall:
    if not isinstance(raw, dict):
        raise ManifestError(f"script {path} turn {turn_index} tool call must be an object")
    required = {"id", "name", "arguments"}
    unknown = sorted(set(raw) - required)
    missing = sorted(required - set(raw))
    if unknown:
        raise ManifestError(f"script {path} turn {turn_index} tool call has unknown field(s): {unknown}")
    if missing:
        raise ManifestError(f"script {path} turn {turn_index} tool call missing field(s): {', '.join(missing)}")
    call_id = raw["id"]
    name = raw["name"]
    arguments = raw["arguments"]
    if not isinstance(call_id, str) or not call_id:
        raise ManifestError(f"script {path} turn {turn_index} tool call id must be a non-empty string")
    if call_id in seen_ids:
        raise ManifestError(f"script {path} turn {turn_index} has duplicate tool call id: {call_id}")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"script {path} turn {turn_index} tool call name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise ManifestError(f"script {path} turn {turn_index} tool call arguments must be an object")
    seen_ids.add(call_id)
    return ToolCall(id=call_id, name=name, arguments=arguments)
