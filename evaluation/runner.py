from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import Agent, Workspace
from agent.loop import Run
from agent.tools import truncate_middle

from .manifest import TaskManifest, load_manifest
from .task_provider import load_scripted_provider

SECURITY_SCOPE = "tool-policy-and-workspace-paths"


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    category: str
    expected_outcome: str
    outcome: str
    failure_stage: str | None
    run: dict[str, object]
    verification: dict[str, object]
    workspace: str
    initial_commit: str | None
    diff_path: str | None
    changed_paths: list[str]
    security_scope: str
    started_at: str
    elapsed_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def create_run_directory(output_root: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(output_root, timestamp)
    candidate = base
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    os.makedirs(candidate)
    return candidate


def run_suite(tasks_root: str, output_root: str) -> list[EvaluationResult]:
    manifests = sorted(Path(tasks_root).glob("*/task.json"))
    if not manifests:
        raise ValueError(f"no task manifests found in {tasks_root}")
    return [_run_task_catching_failures(str(path), output_root) for path in manifests]


def write_reports(results: list[EvaluationResult], output_root: str) -> tuple[str, str]:
    os.makedirs(output_root, exist_ok=True)
    jsonl_path = os.path.join(output_root, "results.jsonl")
    summary_path = os.path.join(output_root, "summary.json")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    outcomes: dict[str, int] = {}
    categories: dict[str, int] = {}
    for result in results:
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
        categories[result.category] = categories.get(result.category, 0) + 1
    averages = {
        "turns_used": sum(int(result.run.get("turns_used", 0)) for result in results) / len(results) if results else 0,
        "estimated_tokens": sum(int(result.run.get("estimated_tokens", 0)) for result in results) / len(results) if results else 0,
    }
    totals = {
        "tool_errors": sum(int(result.run.get("tool_errors", 0)) for result in results),
        "tool_denials": sum(int(result.run.get("tool_denials", 0)) for result in results),
        "compactions": sum(int(result.run.get("compactions", 0)) for result in results),
    }
    summary = {
        "total": len(results),
        "task_passed": sum(result.outcome == result.expected_outcome for result in results),
        "outcomes": dict(sorted(outcomes.items())),
        "categories": dict(sorted(categories.items())),
        "averages": averages,
        "totals": totals,
        "security_scope": SECURITY_SCOPE,
    }
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, sort_keys=True)
    return jsonl_path, summary_path


def run_task(manifest_path: str, output_root: str) -> EvaluationResult:
    manifest = load_manifest(manifest_path)
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    workspace = _copy_fixture(manifest, output_root)
    try:
        initial_commit = _validate_fixture_git(workspace)
    except RuntimeError as exc:
        return _failure_result(manifest, workspace, started_at, started, "fixture", str(exc))

    agent = Agent(
        provider=load_scripted_provider(_script_path(manifest_path)),
        workspace=Workspace(workspace),
        max_steps=manifest.max_steps,
        budget_tokens=manifest.budget_tokens,
    )
    run = agent.run(manifest.task)
    verification = _verify(manifest.verify_command, workspace, manifest.timeout_seconds)
    try:
        diff_path, changed_paths = _collect_git_evidence(workspace, output_root, manifest.id)
    except RuntimeError as exc:
        return _failure_result(manifest, workspace, started_at, started, "runner", str(exc), initial_commit, run)
    path_error = _check_allowed_paths(changed_paths, manifest.allowed_paths)
    if path_error:
        verification["returncode"] = 1
        verification["path_error"] = path_error
    failure_stage = _failure_stage(run, verification)
    return EvaluationResult(
        task_id=manifest.id,
        category=manifest.category,
        expected_outcome=manifest.expected_outcome,
        outcome=classify_run(run, verification["returncode"], bool(verification["timed_out"])),
        failure_stage=failure_stage,
        run=_run_data(run),
        verification=verification,
        workspace=workspace,
        initial_commit=initial_commit,
        diff_path=diff_path,
        changed_paths=changed_paths,
        security_scope=SECURITY_SCOPE,
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
    if run.tool_denials:
        return "safety_denied"
    if verification_timed_out or verify_returncode != 0:
        return "verification_failure"
    if run.tool_errors:
        return "tool_error"
    return "success" if run.ok else "verification_failure"


def _run_task_catching_failures(manifest_path: str, output_root: str) -> EvaluationResult:
    try:
        return run_task(manifest_path, output_root)
    except Exception as exc:
        task_id = Path(manifest_path).parent.name
        return EvaluationResult(
            task_id=task_id,
            category="unknown",
            expected_outcome="success",
            outcome="verification_failure",
            failure_stage="manifest",
            run={},
            verification={"error": f"{type(exc).__name__}: {exc}"},
            workspace="",
            initial_commit=None,
            diff_path=None,
            changed_paths=[],
            security_scope=SECURITY_SCOPE,
            started_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=0.0,
        )


def _failure_result(manifest: TaskManifest, workspace: str, started_at: str, started: float, stage: str, error: str, initial_commit: str | None = None, run: Run | None = None) -> EvaluationResult:
    return EvaluationResult(
        task_id=manifest.id,
        category=manifest.category,
        expected_outcome=manifest.expected_outcome,
        outcome="verification_failure",
        failure_stage=stage,
        run=_run_data(run) if run else {},
        verification={"error": error},
        workspace=workspace,
        initial_commit=initial_commit,
        diff_path=None,
        changed_paths=[],
        security_scope=SECURITY_SCOPE,
        started_at=started_at,
        elapsed_seconds=round(time.monotonic() - started, 6),
    )


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


def _validate_fixture_git(workspace: str) -> str:
    if _git(["status", "--porcelain"], workspace):
        raise RuntimeError("fixture Git status is not clean")
    return _git(["rev-parse", "HEAD"], workspace).strip()


def _collect_git_evidence(workspace: str, output_root: str, task_id: str) -> tuple[str, list[str]]:
    diff = _git(["diff", "--binary", "HEAD"], workspace)
    names = _git(["diff", "--name-status", "HEAD"], workspace)
    diff_dir = os.path.join(output_root, "diffs")
    os.makedirs(diff_dir, exist_ok=True)
    diff_path = os.path.join(diff_dir, f"{task_id}.patch")
    with open(diff_path, "w", encoding="utf-8") as fh:
        fh.write(diff)
    changed_paths = []
    for line in names.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            changed_paths.extend(fields[1:])
    return diff_path, sorted(set(changed_paths))


def _git(args: list[str], cwd: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"git could not run: {exc}") from exc
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _verify(command: str, workspace: str, timeout_seconds: int) -> dict[str, object]:
    try:
        proc = subprocess.run(command, shell=True, cwd=workspace, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return {"command": command, "returncode": None, "timed_out": True, "stdout": truncate_middle(_text(exc.stdout)), "stderr": truncate_middle(_text(exc.stderr))}
    return {"command": command, "returncode": proc.returncode, "timed_out": False, "stdout": truncate_middle(proc.stdout), "stderr": truncate_middle(proc.stderr)}


def _text(value: str | bytes | None) -> str:
    return value.decode() if isinstance(value, bytes) else value or ""


def _check_allowed_paths(changed_paths: list[str], allowed_paths: tuple[str, ...]) -> str:
    disallowed = [path for path in changed_paths if not any(_allowed(path, allowed) for allowed in allowed_paths)]
    return f"changes outside allowed_paths: {disallowed}" if disallowed else ""


def _allowed(path: str, allowed: str) -> bool:
    return allowed == "." or path == allowed or path.startswith(allowed.rstrip("/") + "/")


def _failure_stage(run: Run, verification: dict[str, object]) -> str | None:
    if run.stop_reason.startswith("provider error:") or not run.ok:
        return "agent"
    if verification.get("returncode") != 0 or verification.get("timed_out"):
        return "verification"
    return None


def _run_data(run: Run) -> dict[str, object]:
    return {
        "stop_reason": run.stop_reason,
        "turns_used": run.turns_used,
        "tool_calls_made": run.tool_calls_made,
        "tool_errors": run.tool_errors,
        "tool_denials": run.tool_denials,
        "compactions": run.compactions,
        "estimated_tokens": run.estimated_tokens,
        "trace_events": run.trace_events,
        "elapsed": run.elapsed,
    }
