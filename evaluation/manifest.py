from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class TaskManifest:
    id: str
    category: str
    expected_outcome: str
    fixture_dir: str
    task: str
    verify_command: str
    max_steps: int
    budget_tokens: int
    allowed_paths: tuple[str, ...]
    timeout_seconds: int


_REQUIRED = {"id", "fixture", "task", "verify_command", "max_steps", "budget_tokens"}
_OPTIONAL = {"allowed_paths", "timeout_seconds", "category", "expected_outcome"}
_CATEGORIES = {"bugfix", "cross-file", "test-change", "refactor", "cli", "recovery", "safety", "compaction"}
_OUTCOMES = {"success", "safety_denied"}


def load_manifest(path: str) -> TaskManifest:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} must be a JSON object")

    unknown = sorted(set(raw) - _REQUIRED - _OPTIONAL)
    if unknown:
        raise ManifestError(f"manifest {path} has unknown field(s): {unknown}")
    missing = sorted(_REQUIRED - set(raw))
    if missing:
        raise ManifestError(f"manifest {path} missing required field(s): {', '.join(missing)}")

    id_ = _string(raw, "id", path)
    fixture = _string(raw, "fixture", path)
    task = _string(raw, "task", path)
    verify_command = _string(raw, "verify_command", path)
    max_steps = _positive_integer(raw, "max_steps", path)
    budget_tokens = _positive_integer(raw, "budget_tokens", path)
    timeout_seconds = _positive_integer(raw, "timeout_seconds", path, default=60)
    allowed_paths = _string_list(raw, "allowed_paths", path, default=["."])
    category = raw.get("category", "bugfix")
    expected_outcome = raw.get("expected_outcome", "success")
    if category not in _CATEGORIES:
        raise ManifestError(f"category must be one of {sorted(_CATEGORIES)} in {path}")
    if expected_outcome not in _OUTCOMES:
        raise ManifestError(f"expected_outcome must be one of {sorted(_OUTCOMES)} in {path}")

    if os.path.isabs(fixture):
        raise ManifestError(f"fixture must be relative: {fixture!r}")
    pieces = fixture.replace("\\", "/").split("/")
    if ".." in pieces:
        raise ManifestError(f"fixture must stay under the task directory: {fixture!r}")

    fixture_dir = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(path)), fixture))
    task_dir = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    if fixture_dir != task_dir and not fixture_dir.startswith(task_dir + os.sep):
        raise ManifestError(f"fixture must stay under the task directory: {fixture!r}")
    if not os.path.isdir(fixture_dir):
        raise ManifestError(f"fixture directory does not exist: {fixture!r}")

    return TaskManifest(
        id=id_,
        category=category,
        expected_outcome=expected_outcome,
        fixture_dir=fixture_dir,
        task=task,
        verify_command=verify_command,
        max_steps=max_steps,
        budget_tokens=budget_tokens,
        allowed_paths=tuple(allowed_paths),
        timeout_seconds=timeout_seconds,
    )


def _string(raw: dict[str, Any], field: str, path: str) -> str:
    value = raw[field]
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string in {path}")
    return value


def _positive_integer(raw: dict[str, Any], field: str, path: str, default: int | None = None) -> int:
    value = raw.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{field} must be a positive integer in {path}")
    return value


def _string_list(raw: dict[str, Any], field: str, path: str, default: list[str]) -> list[str]:
    value = raw.get(field, default)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise ManifestError(f"{field} must be a non-empty list of strings in {path}")
    return value
