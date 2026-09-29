#!/usr/bin/env python3
"""CLI entry point.

    python3 main.py "add a retry helper"       # needs an API key
    python3 main.py --demo                     # scripted model, no key
    python3 main.py --profile local "..."      # a profile from .agent.json
    python3 main.py --workspace ~/some/repo "..." --max-steps 60

Which model to talk to comes from a JSON config file (.agent.json), which
lists named profiles -- an endpoint, a model, and the *name* of the
environment variable holding the key. Keys are never stored in that file,
because it is meant to be committed. `python3 main.py --list-profiles`
shows what is available.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from agent import Agent, OpenAIProvider, Workspace
from agent.config import CONFIG_FILENAMES, Config, ConfigError, load_config_or_builtin
from demo import TASK, build_script


def resolve_provider(args: argparse.Namespace, config: Config | None = None) -> OpenAIProvider:
    """Build the provider from a profile, letting explicit flags win.

    Two layers, each overriding the one below: command-line flags, then the
    chosen profile. The profile comes from --profile, or is inferred from
    whichever key is actually exported, so the common case needs no flag.
    """
    config = config if config is not None else load_config_or_builtin(args.config)
    profile = config.get(args.profile) if args.profile else config.infer()

    if not profile.api_key() and profile.api_key_env:
        raise SystemExit(
            f"profile '{profile.name}' needs {profile.api_key_env}, which is not set.\n"
            f"  export {profile.api_key_env}=...      or      --profile {_other(config, profile)}"
        )

    return OpenAIProvider(
        # Not `or None`: None means "fall back to the environment", and that
        # would turn a deliberately keyless local endpoint back into a 401.
        api_key=profile.api_key(),
        base_url=args.base_url or profile.base_url,
        model=args.model or profile.model,
        timeout=profile.timeout or 180,
    )


def _other(config: Config, current: "object") -> str:
    """A profile name worth suggesting instead of the one that has no key."""
    for name in config.names():
        if name != getattr(current, "name", None):
            return name
    return config.default


def _list_profiles(config: Config) -> int:
    print(f"config: {config.source}")
    for name in config.names():
        profile = config.profiles[name]
        marker = " (default)" if name == config.default else ""
        print(f"  {name}{marker}")
        print(f"      {profile.describe()}")
    if not config.names():
        print("  (none)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI's flags, in one place so tests can parse without running."""
    p = argparse.ArgumentParser(prog="coding-agent", description="A small coding agent.")
    p.add_argument("task", nargs="?", help="what you want done")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="run the scripted demo, no API key needed")
    group.add_argument("--eval-dir", metavar="DIR", help="run deterministic evaluation tasks from DIR")
    p.add_argument("--config", metavar="FILE",
                   help="JSON file of model profiles (default: look for "
                        f"{' or '.join(CONFIG_FILENAMES)} in this directory and upwards)")
    p.add_argument("--profile", metavar="NAME",
                   help="which profile to use (default: the one whose key is exported)")
    p.add_argument("--list-profiles", action="store_true",
                   help="print the profiles this config defines, then exit")
    p.add_argument("--workspace", "-w", default=None,
                   help="directory the agent may touch (default: cwd, or demo_workspace for --demo)")
    p.add_argument("--max-steps", type=int, default=40, help="hard cap on model turns")
    p.add_argument("--budget-tokens", type=int, default=60_000, help="context budget before compaction")
    p.add_argument("--model", help="model id, overriding the profile's")
    p.add_argument("--base-url", help="base url, overriding the profile's")
    p.add_argument("--eval-output", metavar="DIR", default=".eval_runs", help="write evaluation reports and workspaces here")
    p.add_argument("--quiet", "-q", action="store_true", help="only print the final answer")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.list_profiles and not args.demo and not args.eval_dir and not args.task:
        # Checked before touching the config: a user who forgot the task should
        # be told that, not told their API key is missing.
        build_parser().error("give a task, pass --demo, or pass --eval-dir")

    if args.eval_dir:
        from evaluation.runner import create_run_directory, run_suite, write_reports

        try:
            run_dir = create_run_directory(args.eval_output)
            results = run_suite(args.eval_dir, run_dir)
        except ValueError as exc:
            print(f"evaluation problem: {exc}", file=sys.stderr)
            return 2
        jsonl_path, summary_path = write_reports(results, run_dir)
        succeeded = sum(result.outcome == result.expected_outcome for result in results)
        print(f"evaluations: {len(results)} | success: {succeeded}")
        print(f"results: {jsonl_path}")
        print(f"summary: {summary_path}")
        return 0 if succeeded == len(results) else 1

    try:
        if args.list_profiles:
            return _list_profiles(load_config_or_builtin(args.config))

        provider = build_script() if args.demo else resolve_provider(args)
    except ConfigError as exc:
        # The config is hand-written, so its errors are the user's to fix, not
        # a crash. A traceback here would bury the one line that says what to
        # change, behind six frames of library internals.
        print(f"config problem: {exc}", file=sys.stderr)
        return 2

    if args.workspace:
        workspace = args.workspace
    elif args.demo:
        # The demo writes to the workspace, so run it on a fresh copy of the
        # fixture every time. Otherwise the second run has nothing left to fix
        # and the interesting turns go quiet.
        fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_workspace")
        workspace = os.path.join(os.path.dirname(fixture), ".demo_run")
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(fixture, workspace)
    else:
        workspace = "."

    task = TASK if args.demo else args.task

    ws = Workspace(workspace)
    agent = Agent(
        provider=provider,
        workspace=ws,
        max_steps=args.max_steps,
        budget_tokens=args.budget_tokens,
    )

    print(f"workspace: {ws.root}")
    if not args.demo:
        print(f"model: {provider.model} @ {provider.base_url}")
    print(f"task: {task}\n")

    result = agent.run(task, on_event=(lambda m: None) if args.quiet else print)

    print("\n" + "=" * 60)
    if result.final_text:
        print(result.final_text)
    print("=" * 60)
    print(
        f"stop: {result.stop_reason} | steps: {result.turns_used} | "
        f"tool calls: {result.tool_calls_made} | errors: {result.tool_errors} | "
        f"compactions: {result.compactions} | {result.elapsed:.1f}s"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
