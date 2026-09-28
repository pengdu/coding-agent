"""Tools: JSON-Schema declarations + executors, confined to a workspace.

The lesson this file exists to encode: for a coding agent, the quality of
the tool surface matters more than the model. Three things decide whether
a tool works:

  1. The description states *when* to use it, not just what it is.
  2. Errors are actionable -- they name the fix, not the failure.
  3. Truncation keeps the informative part. Command output ends with the
     error, so we keep the tail. File reads begin with the top, so we keep
     the head. Getting this backwards is a real and common bug.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

# Output caps. Generous enough for real work, small enough to protect context.
MAX_READ_LINES = 2000
MAX_OUTPUT_CHARS = 8000
MAX_GREP_LINES = 200
COMMAND_TIMEOUT = 180


class ToolError(Exception):
    """A tool failed in a way the model can act on. Message goes back as-is."""


class ToolDenied(Exception):
    """A dangerous operation was blocked. Not recoverable by retrying."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def truncate_middle(text: str, limit: int = MAX_OUTPUT_CHARS, marker: str = "\n...[truncated]...\n") -> str:
    """Keep the head and the tail. The tail is where errors live."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + marker + text[-tail:]


# --------------------------------------------------------------------------
# tool definition
# --------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class Workspace:
    """Resolves paths and refuses to leave the root.

    Containment is checked on the *resolved* path, so `../../etc/passwd`
    and symlinks are both caught.
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(os.path.expanduser(root))
        os.makedirs(self.root, exist_ok=True)

    def resolve(self, path: str) -> str:
        if os.path.isabs(path):
            candidate = os.path.realpath(path)
        else:
            candidate = os.path.realpath(os.path.join(self.root, path))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise ToolError(
                f"path {path!r} resolves outside the workspace root {self.root}. "
                "Stay inside it; there is no --allow-outside flag on purpose."
            )
        return candidate

    def rel(self, absolute: str) -> str:
        try:
            return os.path.relpath(absolute, self.root)
        except ValueError:  # different drive on some platforms
            return absolute


# --------------------------------------------------------------------------
# implementations
# --------------------------------------------------------------------------


def make_read_file(ws: Workspace) -> Callable[..., str]:
    def read_file(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
        target = ws.resolve(path)
        if os.path.isdir(target):
            raise ToolError(
                f"{path} is a directory, not a file. Use list_dir on it."
            )
        if not os.path.exists(target):
            raise ToolError(
                f"no such file: {path}. Use list_dir on the parent to see what exists."
            )
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

        total = len(lines)
        if offset < 1:
            raise ToolError(f"offset is 1-based; got {offset}.")
        if offset > total and total:
            raise ToolError(
                f"{path} has {total} lines, offset {offset} is past the end."
            )
        if limit < 1:
            raise ToolError(f"limit must be >= 1; got {limit}.")

        chunk = lines[offset - 1 : offset - 1 + limit]
        if not chunk:
            return f"{path} is empty (0 lines)."

        body = "".join(
            f"{offset + i:6d}\t{line.rstrip(chr(10))}\n" for i, line in enumerate(chunk)
        )
        header = f"{path} (lines {offset}-{offset + len(chunk) - 1} of {total})\n"
        footer = "" if offset + len(chunk) - 1 >= total else f"\n... {total - (offset + len(chunk) - 1)} more lines; read with offset={offset + len(chunk)}\n"
        return header + body + footer

    return read_file


def make_write_file(ws: Workspace) -> Callable[..., str]:
    def write_file(path: str, content: str) -> str:
        target = ws.resolve(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        existed = os.path.exists(target)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        verb = "overwrote" if existed else "created"
        return f"{verb} {path} ({n_lines} lines, {len(content)} bytes)"

    return write_file


def make_edit_file(ws: Workspace) -> Callable[..., str]:
    """Exact-string replacement, and the only safe way to change a file.

    The two error messages here are the whole product. When a model can't
    find the string, telling it *how close it got* is the difference
    between one retry and five.
    """

    def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        target = ws.resolve(path)
        if not os.path.exists(target):
            raise ToolError(f"no such file: {path}. Create it with write_file first.")
        with open(target, "r", encoding="utf-8") as fh:
            text = fh.read()

        if old_string == new_string:
            raise ToolError("old_string and new_string are identical; nothing to do.")

        count = text.count(old_string)
        if count == 0:
            lines = text.splitlines()
            if len(lines) <= 60:
                # Small file: showing all of it is the most useful recovery
                # we can offer, because the model can then copy an exact line
                # instead of guessing at whitespace again.
                body = "".join(f"{i + 1:6d}\t{ln}\n" for i, ln in enumerate(lines))
                hint = (
                    f"\n\nHere is the whole file ({len(lines)} lines). Copy from it exactly, "
                    f"including indentation:\n{body}"
                )
            else:
                first = old_string.strip().splitlines()[0] if old_string.strip() else ""
                near = [
                    f"{i + 1:6d}\t{ln}"
                    for i, ln in enumerate(lines)
                    if first and first[:60] in ln
                ][:20]
                hint = ""
                if near:
                    hint = (
                        "\n\nA line starting like your old_string was found here -- your match "
                        "probably has wrong indentation or trailing whitespace:\n"
                        + "\n".join(near)
                        + "\nCopy the exact text from the file, including indentation."
                    )
            raise ToolError(
                f"old_string not found in {path} (searched {len(text)} chars).{hint}"
            )

        if count > 1 and not replace_all:
            positions = [text[:i].count("\n") + 1 for i in _find_all(text, old_string)]
            shown = "\n".join(
                f"{ln:6d}\t{text.splitlines()[ln - 1]}"
                for ln in positions[:10]
                if 0 < ln <= len(text.splitlines())
            )
            raise ToolError(
                f"old_string appears {count} times in {path} (lines {positions[:10]}). "
                "Include more surrounding context so the match is unique, or set "
                f"replace_all=true.\n\n{shown}"
            )

        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(new_text)

        delta = new_text.count("\n") - text.count("\n")
        return (
            f"edited {path} ({count if replace_all else 1} replacement"
            f"{'s' if count > 1 and replace_all else ''}, {delta:+d} lines). "
            "Read it back to confirm the result is what you intended."
        )

    return edit_file


def _find_all(haystack: str, needle: str) -> list[int]:
    out, start = [], 0
    while (i := haystack.find(needle, start)) != -1:
        out.append(i)
        start = i + 1
    return out


def make_list_dir(ws: Workspace) -> Callable[..., str]:
    def list_dir(path: str = ".") -> str:
        target = ws.resolve(path)
        if not os.path.exists(target):
            raise ToolError(f"no such directory: {path}")
        if not os.path.isdir(target):
            raise ToolError(f"{path} is a file, not a directory. Use read_file on it.")

        entries = sorted(os.listdir(target), key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))
        if not entries:
            return f"{ws.rel(target) or '.'} is empty."
        rows = []
        for name in entries[:300]:
            full = os.path.join(target, name)
            if os.path.isdir(full):
                rows.append(f"  {name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = -1
                rows.append(f"  {name}  ({size} bytes)")
        more = "" if len(entries) <= 300 else f"\n... {len(entries) - 300} more entries"
        prefix = ws.rel(target) or "."
        return f"{prefix}/ contains {len(entries)} entries:\n" + "\n".join(rows) + more

    return list_dir


def make_grep(ws: Workspace) -> Callable[..., str]:
    def grep(pattern: str, path: str = ".", glob: str = "*", is_regex: bool = False) -> str:
        if not pattern:
            raise ToolError("pattern must not be empty.")
        rx = re.compile(pattern if is_regex else re.escape(pattern))
        root = ws.resolve(path)
        if not os.path.exists(root):
            raise ToolError(f"no such path: {path}")

        hits: list[str] = []
        skipped = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
            ]
            for fname in filenames:
                if not fnmatch.fnmatch(fname, glob):
                    continue
                full = os.path.join(dirpath, fname)
                if os.path.getsize(full) > 2_000_000:
                    skipped += 1
                    continue
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        for n, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append(f"{ws.rel(full)}:{n}: {line.rstrip()[:300]}")
                                if len(hits) >= MAX_GREP_LINES:
                                    break
                except OSError:
                    continue
                if len(hits) >= MAX_GREP_LINES:
                    break
            if len(hits) >= MAX_GREP_LINES:
                break

        if not hits:
            where = f" in {ws.rel(root) or '.'}" if root != ws.root else ""
            scope = f" (files matching {glob})" if glob != "*" else ""
            return f"no matches for {pattern!r}{where}{scope}. Nothing to do here."
        note = ""
        if len(hits) >= MAX_GREP_LINES:
            note = f"\n... stopped at {MAX_GREP_LINES} matches; narrow the pattern or set glob to see more."
        if skipped:
            note += f"\n... skipped {skipped} file(s) larger than 2MB."
        return "\n".join(hits) + note

    return grep


# Patterns that are unrecoverable. Deliberately short: this is a speed bump
# against the obvious footguns, not a sandbox. Real isolation is the OS.
_BLOCKED = re.compile(
    r"(rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|\*)\s*$)"
    r"|(:\(\)\s*\{.*\}\s*;?\s*:)"
    r"|(dd\s+[^|]*of=/dev/)"
    r"|(mkfs|>\s*/dev/sd)"
    r"|(shutdown|reboot|halt|poweroff)\b"
    r"|(git\s+push\s+[^\s]*\s*(-f|--force))"
    r"|(chmod\s+-R\s+777\s+/)",
    re.IGNORECASE,
)


def make_run_command(ws: Workspace) -> Callable[..., str]:
    def run_command(command: str, timeout: int = COMMAND_TIMEOUT) -> str:
        if not command.strip():
            raise ToolError("command must not be empty.")
        if _BLOCKED.search(command.strip()):
            raise ToolDenied(
                f"blocked as unrecoverable: {command!r}\n"
                "Rewrite it to be narrower (name the specific path), or run it "
                "yourself outside the agent."
            )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=ws.root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return (
                f"command timed out after {timeout}s: {command}\n"
                "It may be waiting for input. Add a timeout flag, or avoid "
                "interactive commands."
            )

        parts = [f"$ {command}", f"exit code: {proc.returncode}"]
        if proc.stdout:
            parts.append("--- stdout ---\n" + proc.stdout.rstrip())
        if proc.stderr:
            parts.append("--- stderr ---\n" + proc.stderr.rstrip())
        if not proc.stdout and not proc.stderr:
            parts.append("(no output)")
        out = "\n".join(parts)
        if proc.returncode != 0:
            out += "\n\nThis command failed. Read the stderr above and fix the cause before retrying."
        return truncate_middle(out)

    return run_command


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def build_tools(ws: Workspace) -> dict[str, Tool]:
    def p(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    return {
        "read_file": Tool(
            name="read_file",
            description=(
                "Read a text file with 1-based line numbers. Use this BEFORE editing any file "
                "and instead of guessing its contents -- line numbers here are what you pass to "
                "read_file offset later. For a big file, read a slice with offset/limit."
            ),
            parameters=p(
                {
                    "path": {"type": "string", "description": "Path relative to the workspace root."},
                    "offset": {"type": "integer", "description": "First line to read. Default 1.", "minimum": 1},
                    "limit": {"type": "integer", "description": "Max lines to read.", "minimum": 1},
                },
                ["path"],
            ),
            fn=make_read_file(ws),
        ),
        "write_file": Tool(
            name="write_file",
            description=(
                "Create a file, or replace its entire contents. Prefer edit_file for changing an "
                "existing file -- a full overwrite loses anything you did not carry over."
            ),
            parameters=p(
                {"path": {"type": "string"}, "content": {"type": "string", "description": "Full new contents."}},
                ["path", "content"],
            ),
            fn=make_write_file(ws),
        ),
        "edit_file": Tool(
            name="edit_file",
            description=(
                "Replace an exact string in an existing file. old_string must match the file "
                "byte-for-byte, including indentation, and must appear exactly once unless "
                "replace_all is true. Always read_file first and copy the text from there. "
                "This is the only way to modify a file you have read."
            ),
            parameters=p(
                {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to find."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
                },
                ["path", "old_string", "new_string"],
            ),
            fn=make_edit_file(ws),
        ),
        "list_dir": Tool(
            name="list_dir",
            description="List a directory. Start here when you do not know what exists yet.",
            parameters=p({"path": {"type": "string", "description": "Directory. Default: the workspace root."}}, []),
            fn=make_list_dir(ws),
        ),
        "grep": Tool(
            name="grep",
            description=(
                "Search file contents by literal text (or regex with is_regex). Use this to find "
                "where something is defined or used before reading whole files. Returns "
                "path:line: text."
            ),
            parameters=p(
                {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Subtree to search. Default: workspace root."},
                    "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'. Default '*'."},
                    "is_regex": {"type": "boolean", "description": "Treat pattern as a regex. Default false."},
                },
                ["pattern"],
            ),
            fn=make_grep(ws),
        ),
        "run_command": Tool(
            name="run_command",
            description=(
                "Run a shell command in the workspace root. Use it to run tests, linters, git, "
                "and build steps -- and to verify your own changes. A failing command returns its "
                "exit code and stderr so you can diagnose it. Never use it to read files; use "
                "read_file so line numbers are reliable."
            ),
            parameters=p(
                {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Seconds. Default 180."},
                },
                ["command"],
            ),
            fn=make_run_command(ws),
        ),
    }


def tool_schemas(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    return [t.to_schema() for t in tools.values()]
