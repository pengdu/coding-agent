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

MAX_LIST_ENTRIES = 300  # entries shown by list_dir before it says "... more"
MAX_GREP_FILE_BYTES = 2_000_000  # grep skips anything bigger, and says so
MAX_MATCH_CHARS = 300  # a single grep hit is truncated to this
MAX_SHOWN_MATCHES = 10  # ambiguous-edit error shows at most this many lines
MAX_NEAR_MATCHES = 20  # large-file edit hint shows at most this many lines
SMALL_FILE_LINES = 60  # at or below this, a failed edit dumps the whole file
NEAR_MATCH_PREFIX = 60  # chars of old_string matched against to find near-hits
LINE_NUMBER_WIDTH = 6  # read_file right-aligns line numbers in this many columns


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


def _numbered(lineno: int, text: str) -> str:
    """One source line, prefixed with its aligned line number and a tab.

    Every tool that shows file content uses this, so the model learns one
    format instead of re-parsing a different one per tool.
    """
    return f"{lineno:{LINE_NUMBER_WIDTH}d}\t{text}\n"


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

    def display(self, absolute: str) -> str:
        """`rel`, but '.' for the root itself rather than an empty string.

        One rule for "how do I name this path to the model", so no tool has to
        remember the `or '.'` half of it.
        """
        return self.rel(absolute) or "."


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

        first, last = offset, offset + len(chunk) - 1
        body = "".join(_numbered(first + i, line.rstrip(chr(10))) for i, line in enumerate(chunk))
        header = f"{path} (lines {first}-{last} of {total})\n"
        footer = "" if last >= total else f"\n... {total - last} more lines; read with offset={last + 1}\n"
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


def _miss_hint(text: str, old_string: str) -> str:
    """Why the match failed, plus enough of the file to fix it.

    Telling the model *how close it got* is the difference between one retry
    and five, so this returns copy-pasteable text rather than a diagnosis.
    """
    lines = text.splitlines()
    if len(lines) <= SMALL_FILE_LINES:
        # Small file: showing all of it is the most useful recovery we can
        # offer, because the model can then copy an exact line instead of
        # guessing at whitespace again.
        body = "".join(_numbered(i + 1, ln) for i, ln in enumerate(lines))
        return (
            f"\n\nHere is the whole file ({len(lines)} lines). Copy from it exactly, "
            f"including indentation:\n{body}"
        )

    first = old_string.strip().splitlines()[0] if old_string.strip() else ""
    near = [
        _numbered(i + 1, ln).rstrip("\n")
        for i, ln in enumerate(lines)
        if first and first[:NEAR_MATCH_PREFIX] in ln
    ][:MAX_NEAR_MATCHES]
    if not near:
        return ""
    return (
        "\n\nA line starting like your old_string was found here -- your match "
        "probably has wrong indentation or trailing whitespace:\n"
        + "\n".join(near)
        + "\nCopy the exact text from the file, including indentation."
    )


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
            raise ToolError(
                f"old_string not found in {path} (searched {len(text)} chars)."
                + _miss_hint(text, old_string)
            )

        source_lines = text.splitlines()
        if count > 1 and not replace_all:
            positions = [text[:i].count("\n") + 1 for i in _find_all(text, old_string)]
            first_few = positions[:MAX_SHOWN_MATCHES]
            shown = "\n".join(
                _numbered(ln, source_lines[ln - 1]).rstrip("\n")
                for ln in first_few
                if 0 < ln <= len(source_lines)
            )
            raise ToolError(
                f"old_string appears {count} times in {path} (lines {first_few}). "
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
            return f"{ws.display(target)} is empty."
        rows = []
        for name in entries[:MAX_LIST_ENTRIES]:
            full = os.path.join(target, name)
            if os.path.isdir(full):
                rows.append(f"  {name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = -1
                rows.append(f"  {name}  ({size} bytes)")
        more = "" if len(entries) <= MAX_LIST_ENTRIES else f"\n... {len(entries) - MAX_LIST_ENTRIES} more entries"
        return f"{ws.display(target)}/ contains {len(entries)} entries:\n" + "\n".join(rows) + more

    return list_dir


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


def _grep_file(
    rx: "re.Pattern[str]", path: str, show: Callable[[str], str], limit: int
) -> list[str]:
    """Up to `limit` matches in one file, as `path:line: text`.

    Stops reading once it has enough. Without the limit, grepping a dense
    2MB file would scan all of it to return the first line the caller wanted.
    """
    out: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if rx.search(line):
                    out.append(f"{show(path)}:{n}: {line.rstrip()[:MAX_MATCH_CHARS]}")
                    if len(out) >= limit:
                        break
    except OSError:
        return out
    return out


def make_grep(ws: Workspace) -> Callable[..., str]:
    def grep(pattern: str, path: str = ".", glob: str = "*", is_regex: bool = False) -> str:
        if not pattern:
            raise ToolError("pattern must not be empty.")
        try:
            rx = re.compile(pattern if is_regex else re.escape(pattern))
        except re.error as exc:
            # A malformed pattern is the model's mistake, not a tool crash. It
            # must come back as advice, never as a stdlib traceback.
            raise ToolError(
                f"pattern is not a valid regex: {exc}. Fix the pattern, or set "
                "is_regex=false to search for this text literally."
            ) from exc
        root = ws.resolve(path)
        if not os.path.exists(root):
            raise ToolError(f"no such path: {path}")

        hits: list[str] = []
        skipped = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                room = MAX_GREP_LINES - len(hits)
                if room <= 0:
                    break
                if not fnmatch.fnmatch(fname, glob):
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    oversized = os.path.getsize(full) > MAX_GREP_FILE_BYTES
                except OSError:
                    oversized = False  # unstattable (broken link, no permission)
                if oversized:
                    skipped += 1
                    continue
                hits.extend(_grep_file(rx, full, ws.rel, room))
            if len(hits) >= MAX_GREP_LINES:
                break

        if not hits:
            where = "" if root == ws.root else f" in {ws.display(root)}"
            scope = f" (files matching {glob})" if glob != "*" else ""
            return f"no matches for {pattern!r}{where}{scope}. Nothing to do here."
        note = ""
        if len(hits) >= MAX_GREP_LINES:
            note = f"\n... stopped at {MAX_GREP_LINES} matches; narrow the pattern or set glob to see more."
        if skipped:
            note += f"\n... skipped {skipped} file(s) larger than {MAX_GREP_FILE_BYTES // 1_000_000}MB."
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
