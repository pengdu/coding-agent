#!/usr/bin/env python3
"""CLI entry point.

    python3 main.py "add a retry helper"       # needs OPENAI_API_KEY
    python3 main.py --demo                     # scripted model, no key
    python3 main.py --workspace ~/some/repo "..." --max-steps 60
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from agent import Agent, MockProvider, OpenAIProvider, Workspace
from demo import TASK, build_script


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coding-agent", description="A small coding agent.")
    p.add_argument("task", nargs="?", help="what you want done")
    p.add_argument("--demo", action="store_true", help="run the scripted demo, no API key needed")
    p.add_argument("--workspace", "-w", default=None,
                   help="directory the agent may touch (default: cwd, or demo_workspace for --demo)")
    p.add_argument("--max-steps", type=int, default=40, help="hard cap on model turns")
    p.add_argument("--budget-tokens", type=int, default=60_000, help="context budget before compaction")
    p.add_argument("--model", help="model id, e.g. gpt-4o, deepseek-chat, qwen-max")
    p.add_argument("--base-url", help="OpenAI-compatible base url, e.g. https://api.deepseek.com/v1")
    p.add_argument("--quiet", "-q", action="store_true", help="only print the final answer")
    args = p.parse_args(argv)

    if not args.demo and not args.task:
        p.error("give a task, or pass --demo")

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
    provider = build_script() if args.demo else OpenAIProvider(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=args.base_url,
        model=args.model,
    )

    ws = Workspace(workspace)
    agent = Agent(
        provider=provider,
        workspace=ws,
        max_steps=args.max_steps,
        budget_tokens=args.budget_tokens,
    )

    print(f"workspace: {ws.root}")
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
