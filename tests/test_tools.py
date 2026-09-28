"""Tool-level tests.

These are the tests worth writing first in any agent: a tool whose error
message is unclear costs the model a retry, and a tool whose error message
is wrong costs it a wrong fix.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import ToolDenied, ToolError, Workspace, build_tools  # noqa: E402


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace(self._tmp.name)
        self.tools = build_tools(self.ws)

    def call(self, name, **kwargs):
        return self.tools[name].fn(**kwargs)

    def write(self, name, text):
        path = os.path.join(self.ws.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path


class TestReadFile(ToolTestCase):
    def test_line_numbers_are_one_based_and_aligned(self):
        self.write("a.txt", "one\ntwo\nthree\n")
        out = self.call("read_file", path="a.txt")
        self.assertIn("     1\tone", out)
        self.assertIn("     3\tthree", out)
        self.assertIn("(lines 1-3 of 3)", out)

    def test_offset_and_limit_report_the_remaining_lines(self):
        self.write("a.txt", "".join(f"line{i}\n" for i in range(1, 11)))
        out = self.call("read_file", path="a.txt", offset=4, limit=2)
        self.assertIn("(lines 4-5 of 10)", out)
        self.assertIn("line4", out)
        self.assertIn("line5", out)
        self.assertNotIn("line6", out)
        self.assertIn("read with offset=6", out)

    def test_missing_file_error_names_the_fix(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("read_file", path="nope.py")
        self.assertIn("no such file: nope.py", str(ctx.exception))
        self.assertIn("list_dir", str(ctx.exception))

    def test_directory_guides_toward_list_dir(self):
        os.makedirs(os.path.join(self.ws.root, "pkg"))
        with self.assertRaises(ToolError) as ctx:
            self.call("read_file", path="pkg")
        self.assertIn("use list_dir", str(ctx.exception).lower())


class TestEditFile(ToolTestCase):
    def test_unique_match_replaces_once(self):
        self.write("a.py", "x = 1\ny = 2\n")
        out = self.call("edit_file", path="a.py", old_string="y = 2", new_string="y = 3")
        self.assertIn("edited a.py", out)
        with open(os.path.join(self.ws.root, "a.py")) as fh:
            self.assertEqual(fh.read(), "x = 1\ny = 3\n")

    def test_ambiguous_match_reports_the_line_numbers(self):
        self.write("a.py", "v = 1\nv = 2\nv = 3\n")
        with self.assertRaises(ToolError) as ctx:
            self.call("edit_file", path="a.py", old_string="v = ", new_string="w = ")
        msg = str(ctx.exception)
        self.assertIn("appears 3 times", msg)
        self.assertIn("lines [1, 2, 3]", msg)
        self.assertIn("replace_all=true", msg)

    def test_replace_all_is_opt_in(self):
        self.write("a.py", "v = 1\nv = 1\n")
        self.call("edit_file", path="a.py", old_string="v = 1", new_string="v = 9", replace_all=True)
        with open(os.path.join(self.ws.root, "a.py")) as fh:
            self.assertEqual(fh.read(), "v = 9\nv = 9\n")

    def test_miss_shows_the_nearby_line_so_the_model_can_self_correct(self):
        # The realistic failure: right text, wrong indentation.
        self.write("a.py", "def add(a, b):\n    return a + b\n")
        with self.assertRaises(ToolError) as ctx:
            self.call("edit_file", path="a.py", old_string="def add(a, b):\n  return a + b\n", new_string="x")
        msg = str(ctx.exception)
        self.assertIn("old_string not found", msg)
        # For a small file we hand back the whole thing, so the model can copy
        # an exact line rather than guess at whitespace a second time.
        self.assertIn("return a + b", msg)
        self.assertIn("including indentation", msg)

    def test_miss_on_a_large_file_still_points_at_the_first_line(self):
        body = "".join(f"line{i}\n" for i in range(200))
        self.write("big.py", "def target():\n    pass\n" + body)
        with self.assertRaises(ToolError) as ctx:
            self.call("edit_file", path="big.py", old_string="def target():\n    pas\n", new_string="x")
        msg = str(ctx.exception)
        self.assertIn("old_string not found", msg)
        self.assertIn("def target():", msg)
        self.assertIn("wrong indentation", msg)

    def test_no_op_edit_is_rejected(self):
        self.write("a.py", "x = 1\n")
        with self.assertRaises(ToolError):
            self.call("edit_file", path="a.py", old_string="x = 1", new_string="x = 1")

    def test_editing_a_missing_file_points_at_write_file(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("edit_file", path="nope.py", old_string="a", new_string="b")
        self.assertIn("write_file", str(ctx.exception))


class TestWriteAndList(ToolTestCase):
    def test_write_creates_parents_and_reports_create_vs_overwrite(self):
        out = self.call("write_file", path="pkg/deep/new.py", content="a\nb\n")
        self.assertIn("created pkg/deep/new.py", out)
        out = self.call("write_file", path="pkg/deep/new.py", content="c\n")
        self.assertIn("overwrote", out)

    def test_list_dir_distinguishes_dirs_from_files(self):
        self.write("z.txt", "x")
        self.write("sub/y.txt", "x")
        out = self.call("list_dir", path=".")
        self.assertIn("sub/", out)
        self.assertIn("z.txt", out)
        self.assertIn("bytes", out)


class TestGrep(ToolTestCase):
    def test_reports_path_line_and_matching_text(self):
        self.write("a.py", "alpha\nbeta\n")
        self.write("b.py", "gamma\n")
        out = self.call("grep", pattern="beta")
        self.assertIn("a.py:2: beta", out)

    def test_glob_narrows_the_search(self):
        self.write("a.py", "needle\n")
        self.write("b.md", "needle\n")
        self.call("grep", pattern="needle", glob="*.md")
        self.assertNotIn("a.py", self.call("grep", pattern="needle", glob="*.md"))

    def test_no_match_is_reported_as_such(self):
        self.write("a.py", "alpha\n")
        out = self.call("grep", pattern="zzzz")
        self.assertIn("no matches", out)

    def test_regex_opt_in(self):
        self.write("a.py", "value = 42\n")
        self.assertIn("a.py:1", self.call("grep", pattern=r"value = \d+", is_regex=True))

    def test_skips_vcs_and_build_noise(self):
        self.write(".git/config", "needle\n")
        self.write("a.py", "alpha\n")
        self.assertIn("no matches", self.call("grep", pattern="needle"))


class TestRunCommand(ToolTestCase):
    def test_reports_exit_code_stdout_and_stderr(self):
        out = self.call("run_command", command="echo out; echo err 1>&2; exit 3")
        self.assertIn("exit code: 3", out)
        self.assertIn("--- stdout ---", out)
        self.assertIn("out", out)
        self.assertIn("--- stderr ---", out)
        self.assertIn("err", out)
        self.assertIn("failed", out)

    def test_success_does_not_say_it_failed(self):
        out = self.call("run_command", command="echo hi")
        self.assertIn("exit code: 0", out)
        self.assertNotIn("This command failed", out)

    def test_silent_command_is_reported_as_such(self):
        self.assertIn("(no output)", self.call("run_command", command="true"))

    def test_runs_in_the_workspace(self):
        self.write("marker.txt", "x")
        out = self.call("run_command", command="ls")
        self.assertIn("marker.txt", out)

    def test_destructive_patterns_are_blocked(self):
        for bad in ("rm -rf /", "rm -rf ~", "dd if=x of=/dev/sda", "shutdown now", "git push --force origin main"):
            with self.subTest(cmd=bad), self.assertRaises(ToolDenied):
                self.call("run_command", command=bad)

    def test_ordinary_rm_of_a_named_path_is_allowed(self):
        self.write("scratch.txt", "x")
        out = self.call("run_command", command="rm scratch.txt")
        self.assertIn("exit code: 0", out)


class TestWorkspaceContainment(ToolTestCase):
    def test_relative_escape_is_blocked(self):
        with self.assertRaises(ToolError) as ctx:
            self.call("read_file", path="../../etc/passwd")
        self.assertIn("outside the workspace", str(ctx.exception))

    def test_absolute_escape_is_blocked(self):
        with self.assertRaises(ToolError):
            self.call("read_file", path="/etc/passwd")

    def test_interior_paths_still_work(self):
        self.write("a/b/c.txt", "ok")
        self.assertIn("ok", self.call("read_file", path="a/b/c.txt"))
        self.assertIn("ok", self.call("read_file", path="./a/b/../b/c.txt"))

    def test_symlink_escape_is_blocked(self):
        # The link must point somewhere genuinely outside the workspace, so
        # this is a second temp dir rather than a sibling of the root.
        with tempfile.TemporaryDirectory() as elsewhere:
            with open(os.path.join(elsewhere, "secret.txt"), "w") as fh:
                fh.write("secret")
            os.symlink(elsewhere, os.path.join(self.ws.root, "link"))
            with self.assertRaises(ToolError) as ctx:
                self.call("read_file", path="link/secret.txt")
            self.assertIn("outside the workspace", str(ctx.exception))

    def test_a_link_staying_inside_the_workspace_is_allowed(self):
        os.makedirs(os.path.join(self.ws.root, "real"))
        with open(os.path.join(self.ws.root, "real", "ok.txt"), "w") as fh:
            fh.write("fine\n")
        os.symlink(os.path.join(self.ws.root, "real"), os.path.join(self.ws.root, "alias"))
        self.assertIn("fine", self.call("read_file", path="alias/ok.txt"))


class TestSchemaShape(ToolTestCase):
    def test_schemas_are_valid_and_closed(self):
        for name, tool in self.tools.items():
            schema = tool.to_schema()
            with self.subTest(tool=name):
                self.assertEqual(schema["name"], name)
                self.assertTrue(schema["description"].strip(), "description must not be empty")
                params = schema["parameters"]
                self.assertEqual(params["type"], "object")
                self.assertFalse(params["additionalProperties"], "closed schemas catch bad args early")
                for req in params["required"]:
                    self.assertIn(req, params["properties"])

    def test_every_tool_documents_itself(self):
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertTrue(callable(tool.fn))


if __name__ == "__main__":
    unittest.main()
