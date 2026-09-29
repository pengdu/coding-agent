from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from agent import Agent, Workspace
from agent.loop import Run
from agent.tools import truncate_middle

from .manifest import TaskManifest, load_manifest
from .task_provider import load_scripted_provider


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    outcome: str
    run: dict[str, object]
    verification: dict[str, object]
    workspace: str
    started_at: str
    elapsed_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_task(manifest_path: str, output_root: str) -> EvaluationResult:
    manifest = load_manifest(manifest_path)
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    workspace = _copy_fixture(manifest, output_root)
    before = _snapshot(workspace)
    agent = Agent(
        provider=load_scripted_provider(_script_path(manifest_path)),
        workspace=Workspace(workspace),
        max_steps=manifest.max_steps,
        budget_tokens=manifest.budget_tokens,
    )
    run = agent.run(manifest.task)
    verification = _verify(manifest.verify_command, workspace, manifest.timeout_seconds)
    path_error = _check_allowed_paths(before, workspace, manifest.allowed_paths)
    if path_error:
        verification["returncode"] = 1
        verification["path_error"] = path_error
    return EvaluationResult(
        task_id=manifest.id,
        outcome=classify_run(run, verification["returncode"], bool(verification["timed_out"])),
        run=_run_data(run),
        verification=verification,
        workspace=workspace,
        started_at=started_at,
        elapsed_seconds=round(time.monotonic() - started, 6),
    )


def classify_run(run: Run, verify_returncode: int | None, verification_timed_out: bool) -> str:
    reason = run.stop_reason
    if reason.startswith("provider error:"):
        return "provider_error"
    if reason.startswith("step limit reached"):
        return "step_limit"
    if "budget" in reason.lower():
        return "budget_exhausted"
    if run.tool_errors and "blocked as unrecoverable" in run.final_text:
        return "safety_denied"
    if verification_timed_out or verify_returncode != 0:
        return "verification_failure"
    if run.tool_errors:
        return "tool_error"
    return "success" if run.ok else "verification_failure"


def _script_path(manifest_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)), "script.json")


def _copy_fixture(manifest: TaskManifest, output_root: str) -> str:
    workspace = os.path.realpath(os.path.join(output_root, "workspaces", manifest.id))
    root = os.path.realpath(os.path.join(output_root, "workspaces"))
    if workspace != root and not workspace.startswith(root + os.sep):
        raise ValueError(f"task id resolves outside evaluation output: {manifest.id!r}")
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(os.path.dirname(workspace), exist_ok=True)
    shutil.copytree(manifest.fixture_dir, workspace)
    return workspace


def _verify(command: str, workspace: str, timeout_seconds: int) -> dict[str, object]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "timed_out": True,
            "stdout": truncate_middle((exc.stdout or "").decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""),
            "stderr": truncate_middle((exc.stderr or "").decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""),
        }
    return {
        "command": command,
        "returncode": proc.returncode,
        "timed_out": False,
        "stdout": truncate_middle(proc.stdout),
        "stderr": truncate_middle(proc.stderr),
    }


def _snapshot(root: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for directory, _, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


def _check_allowed_paths(before: dict[str, bytes], workspace: str, allowed_paths: tuple[str, ...]) -> str:
    after = _snapshot(workspace)
    changed = sorted({path for path in set(before) | set(after) if before.get(path) != after.get(path)})
    disallowed = [path for path in changed if not any(_allowed(path, allowed) for allowed in allowed_paths)]
    if disallowed:
        return f"changes outside allowed_paths: {disallowed}"
    return ""


def _allowed(path: str, allowed: str) -> bool:
    return allowed == "." or path == allowed or path.startswith(allowed.rstrip("/") + "/")


def _run_data(run: Run) -> dict[str, object]:
    return {
        "stop_reason": run.stop_reason,
        "turns_used": run.turns_used,
        "tool_calls_made": run.tool_calls_made,
        "tool_errors": run.tool_errors,
        "compactions": run.compactions,
        "elapsed": run.elapsed,
    }
