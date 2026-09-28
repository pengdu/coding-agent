"""Config-file tests.

A config file is the one input a user hand-writes, so the bar is that a
mistake produces a message naming the profile and the field, not a stack
trace or a silently wrong endpoint. Most of what is tested here is error
messaging.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import (  # noqa: E402
    Config,
    ConfigError,
    builtin_config,
    find_config,
    load_config,
    load_config_or_builtin,
)

GOOD = {
    "default": "fast",
    "profiles": {
        "fast": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                 "api_key_env": "DS_KEY"},
        "local": {"base_url": "http://localhost:8000/v1/", "model": "qwen-max",
                  "api_key_env": None, "timeout": 60},
    },
}


def _write(tmp: str, data, name=".agent.json") -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


class ConfigFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def write(self, data, name=".agent.json") -> str:
        return _write(self.tmp, data, name)

    def load(self, data, name=".agent.json"):
        return load_config(self.write(data, name))


class TestLoading(ConfigFileTestCase):
    def test_profiles_and_default_come_through(self):
        c = self.load(GOOD)
        self.assertEqual(c.default, "fast")
        self.assertEqual(sorted(c.names()), ["fast", "local"])
        self.assertEqual(c.get("local").model, "qwen-max")
        self.assertEqual(c.get(None).name, "fast", "no name means the default")

    def test_trailing_slash_on_base_url_is_normalized(self):
        # Otherwise the request goes to "https://host/v1//chat/completions",
        # which some gateways answer and others 404.
        self.assertEqual(self.load(GOOD).get("local").base_url, "http://localhost:8000/v1")

    def test_a_null_api_key_env_means_no_auth(self):
        # Distinct from "" so a local vLLM can be addressed without inventing
        # a dummy key to satisfy a check that does not apply to it.
        self.assertIsNone(self.load(GOOD).get("local").api_key_env)
        self.assertEqual(self.load(GOOD).get("local").api_key(), "")

    def test_timeout_survives_and_defaults_to_none(self):
        self.assertEqual(self.load(GOOD).get("local").timeout, 60)
        self.assertIsNone(self.load(GOOD).get("fast").timeout)

    def test_no_default_means_the_first_profile_in_the_file(self):
        data = {"profiles": {"a": {"base_url": "http://a", "model": "m"},
                             "b": {"base_url": "http://b", "model": "m"}}}
        self.assertEqual(self.load(data).default, "a")

    def test_the_source_path_is_reported_in_errors(self):
        # An error that does not say which file is useless once there are
        # several: repo config, home config, per-checkout config.
        c = self.load(GOOD)
        self.assertEqual(c.source, self.write(GOOD))
        with self.assertRaises(ConfigError) as ctx:
            c.get("nope")
        self.assertIn(os.path.basename(c.source), str(ctx.exception))

    def test_load_with_no_path_gives_the_built_ins(self):
        # No file is the normal case, not an error.
        c = load_config(None)
        self.assertEqual(sorted(c.names()), ["deepseek", "openai"])
        self.assertEqual(c.get("deepseek").base_url, "https://api.deepseek.com")

    def test_builtin_config_also_loads_through_the_public_entry_point(self):
        # With no file to find, the public entry point and the constructor
        # have to agree -- otherwise the difference would only show up on a
        # machine that happens to have no config.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(load_config_or_builtin().names(), builtin_config().names())

    def test_an_explicit_missing_path_does_not_fall_back_to_builtins(self):
        # Silently using the built-ins would send a request to a host the user
        # did not name, and they would not find out until the bill arrived.
        with self.assertRaises(ConfigError):
            load_config_or_builtin(os.path.join(self.tmp, "absent.json"))


class TestKeysAreReadNotStored(ConfigFileTestCase):
    def test_an_inline_key_is_refused_with_a_reason(self):
        # The single most important rule here: this file gets committed, and a
        # key in it would only surface as a leak after somebody pushed.
        data = {"profiles": {"p": {"base_url": "http://x", "model": "m",
                                  "api_key": "sk-oops"}}}
        with self.assertRaises(ConfigError) as ctx:
            self.load(data)
        msg = str(ctx.exception)
        self.assertIn("api_key", msg)
        self.assertIn("api_key_env", msg)
        # Must read as a refusal to store a secret, not as a typo: the two
        # look alike to the user, and only one of them explains the risk.
        self.assertIn("committed", msg)
        self.assertNotIn("unknown field", msg)

    def test_the_key_is_read_when_asked_not_when_loaded(self):
        # Loading a config and exporting the key afterwards has to work. A
        # config that captured the key at load time would keep reporting
        # itself unusable for the whole process.
        with mock.patch.dict(os.environ, {"LATE_KEY": "late"}, clear=False):
            c = self.load({"profiles": {"p": {"base_url": "http://x", "model": "m",
                                              "api_key_env": "LATE_KEY"}}})
            self.assertEqual(c.get("p").api_key(), "late")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(c.get("p").api_key(), "")


class TestProfileSelection(ConfigFileTestCase):
    def setUp(self):
        super().setUp()
        # GOOD points at DS_KEY; a real key in the developer's environment
        # would otherwise decide the outcome of every test here.
        self._saved = os.environ.pop("DS_KEY", None)
        if self._saved is not None:
            self.addCleanup(os.environ.__setitem__, "DS_KEY", self._saved)

    def _infer(self):
        return load_config(self.write(GOOD)).infer()

    def test_the_profile_whose_key_is_present_wins(self):
        os.environ["DS_KEY"] = "x"
        self.assertEqual(self._infer().name, "fast")

    def test_no_keys_means_the_default(self):
        # And the provider will then complain about the missing key, which is
        # a better error than silently picking a random endpoint.
        self.assertEqual(self._infer().name, "fast")

    def test_infer_prefers_a_usable_profile_over_the_default_order(self):
        # `local` needs no key, so it is always usable. It should not win
        # simply by being in the file -- only an *exported* key implies intent.
        data = {"default": "fast",
                "profiles": {"fast": {"base_url": "http://a", "model": "m",
                                      "api_key_env": "NOPE_UNSET"},
                             "local": {"base_url": "http://b", "model": "m",
                                       "api_key_env": None}}}
        self.assertEqual(load_config(self.write(data)).infer().name, "fast")


class TestErrors(ConfigFileTestCase):
    def test_malformed_json_says_where(self):
        path = os.path.join(self.tmp, ".agent.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"profiles": {"a": {"base_url": "http://x",}}\n')  # trailing comma
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        msg = str(ctx.exception)
        self.assertIn("line 1", msg)
        self.assertIn(".agent.json", msg)

    def test_a_missing_explicit_path_is_an_error_not_a_fallback(self):
        # Silently using the built-ins would send a request somewhere the user
        # did not ask for, and they would not know until the bill arrived.
        with self.assertRaises(ConfigError) as ctx:
            load_config(os.path.join(self.tmp, "nope.json"))
        self.assertIn("nope.json", str(ctx.exception))

    def test_an_empty_file_explains_the_shape_it_wanted(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({})
        self.assertIn("profiles", str(ctx.exception))

    def test_profiles_must_be_an_object(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": ["a"]})
        self.assertIn("array", str(ctx.exception))

    def test_a_profile_missing_its_endpoint_names_the_profile(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"broken": {"model": "m"}}})
        msg = str(ctx.exception)
        self.assertIn("broken", msg)
        self.assertIn("base_url", msg)

    def test_a_missing_model_is_named(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"p": {"base_url": "http://x"}}})
        self.assertIn("model", str(ctx.exception))

    def test_an_unknown_field_is_rejected_rather_than_ignored(self):
        # A typo like "base_uri" that falls back to a default endpoint is how
        # you end up debugging the wrong host.
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"p": {"base_url": "http://x", "model": "m",
                                         "base_uri": "typo"}}})
        msg = str(ctx.exception)
        self.assertIn("base_uri", msg)
        self.assertIn("base_url", msg)

    def test_a_default_naming_no_profile_lists_the_real_ones(self):
        data = dict(GOOD, default="ghost")
        with self.assertRaises(ConfigError) as ctx:
            self.load(data)
        msg = str(ctx.exception)
        self.assertIn("ghost", msg)
        self.assertIn("fast", msg)
        self.assertIn("local", msg)

    def test_a_non_positive_timeout_is_refused(self):
        for bad in (0, -1):
            with self.subTest(timeout=bad), self.assertRaises(ConfigError) as ctx:
                self.load({"profiles": {"p": {"base_url": "http://x", "model": "m",
                                              "timeout": bad}}})
            self.assertIn("positive", str(ctx.exception))

    def test_a_non_numeric_timeout_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"p": {"base_url": "http://x", "model": "m",
                                          "timeout": "60s"}}})
        self.assertIn("timeout", str(ctx.exception))

    def test_a_non_string_api_key_env_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"p": {"base_url": "http://x", "model": "m",
                                          "api_key_env": 7}}})
        self.assertIn("api_key_env", str(ctx.exception))

    def test_a_profile_that_is_not_an_object_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load({"profiles": {"p": "https://api.deepseek.com"}})
        self.assertIn("object", str(ctx.exception))


class TestSearchUpwards(ConfigFileTestCase):
    def test_finds_the_file_in_the_starting_directory(self):
        path = self.write(GOOD)
        self.assertEqual(find_config(self.tmp), path)

    def test_finds_the_file_in_a_parent(self):
        # Running from a subdirectory should still see the project config.
        self.write(GOOD)
        deep = os.path.join(self.tmp, "a", "b", "c")
        os.makedirs(deep)
        self.assertEqual(find_config(deep), self.write(GOOD))

    def test_the_dotfile_wins_over_the_bare_name(self):
        # A committed .agent.json and a local agent.json can coexist; the
        # dot-prefixed one is the deliberate choice.
        dotted = self.write(GOOD, name=".agent.json")
        self.write({"profiles": {"z": {"base_url": "http://z", "model": "m"}}},
                   name="agent.json")
        self.assertEqual(find_config(self.tmp), dotted)

    def test_no_file_anywhere_up_the_tree_returns_none(self):
        # Assert about this temp dir only: the real filesystem root may have a
        # config from another user's run, which would make this flaky.
        self.assertIsNone(find_config(self.tmp))

    def test_an_explicit_path_beats_the_search(self):
        self.write(GOOD, name="agent.json")
        explicit = _write(self.tmp, GOOD, "other.json")
        self.assertEqual(load_config_or_builtin(explicit).source, explicit)


if __name__ == "__main__":
    unittest.main()
