"""Provider-adapter tests.

The network call is not tested -- that is the provider's job, not ours. What
is tested is the part we own and can silently get wrong: turning a provider
response into a ToolCall, and turning a Tool into a wire schema. Both have
bitten real implementations.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.provider import (  # noqa: E402
    AssistantTurn,
    MockProvider,
    OpenAIProvider,
    ProviderError,
    ToolCall,
    openai_to_assistant_turn,
    tool_to_openai,
)
from agent.tools import build_tools, Workspace  # noqa: E402
from agent.loop import _to_openai_tool_calls  # noqa: E402


def _reply(payload: dict):
    """A stand-in for urlopen that returns `payload` as the API response."""
    def urlopen(req, timeout=None):
        return mock.MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: False,
            read=lambda: json.dumps(payload).encode(),
        )
    return urlopen


class TestResponseParsing(unittest.TestCase):
    def test_tool_call_arguments_are_parsed_from_the_json_string(self):
        turn = openai_to_assistant_turn(
            {
                "content": "let me look",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py", "limit": 10}'},
                    }
                ],
            },
            "m",
        )
        self.assertEqual(turn.text, "let me look")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "read_file")
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "a.py", "limit": 10})

    def test_unparseable_arguments_raise_instead_of_silently_emptying(self):
        # Emptying them would make the tool fire with no arguments and fail
        # somewhere much less obvious.
        with self.assertRaises(ProviderError) as ctx:
            openai_to_assistant_turn(
                {"tool_calls": [{"id": "c", "function": {"name": "read_file", "arguments": "{oops"}}]},
                "m",
            )
        self.assertIn("unparseable arguments", str(ctx.exception))

    def test_a_non_object_arguments_payload_is_coerced_not_passed_through(self):
        turn = openai_to_assistant_turn(
            {"tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "[1,2]"}}]}, "m"
        )
        self.assertEqual(turn.tool_calls[0].arguments, {})

    def test_empty_arguments_mean_no_arguments(self):
        turn = openai_to_assistant_turn(
            {"tool_calls": [{"id": "c", "function": {"name": "list_dir", "arguments": ""}}]}, "m"
        )
        self.assertEqual(turn.tool_calls[0].arguments, {})

    def test_a_plain_text_turn_has_no_tool_calls(self):
        turn = openai_to_assistant_turn({"content": "I am done"}, "m")
        self.assertEqual(turn.text, "I am done")
        self.assertEqual(turn.tool_calls, [])

    def test_missing_content_is_an_empty_string_not_none(self):
        # None would serialize to `null` and some providers reject that.
        turn = openai_to_assistant_turn({"tool_calls": []}, "m")
        self.assertEqual(turn.text, "")


class TestRequestBuilding(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tools = build_tools(Workspace(self._tmp.name))

    def test_tool_schema_matches_the_wire_format(self):
        for tool in self.tools.values():
            wire = tool_to_openai(tool.to_schema())
            with self.subTest(tool=wire["function"]["name"]):
                self.assertEqual(wire["type"], "function")
                self.assertEqual(wire["function"]["parameters"]["type"], "object")

    def test_a_malformed_tool_names_the_missing_field(self):
        with self.assertRaises(ProviderError) as ctx:
            tool_to_openai({"name": "read_file"})
        self.assertIn("description", str(ctx.exception))
        self.assertIn("parameters", str(ctx.exception))

    def test_tool_calls_serialize_back_for_the_transcript(self):
        calls = [ToolCall(id="c1", name="read_file", arguments={"path": "a.py", "n": 3})]
        wire = _to_openai_tool_calls(calls)
        self.assertEqual(wire[0]["id"], "c1")
        self.assertEqual(wire[0]["type"], "function")
        self.assertEqual(json.loads(wire[0]["function"]["arguments"]), {"path": "a.py", "n": 3})

    def test_unicode_survives_serialization(self):
        wire = _to_openai_tool_calls([ToolCall("c", "write_file", {"path": "a.py", "content": "中文\n"})])
        self.assertIn("中文", wire[0]["function"]["arguments"])


class TestOpenAIProviderConfig(unittest.TestCase):
    def test_no_key_is_a_clear_error_not_a_401_later(self):
        # `None` means "go and find a key"; not finding one has to fail here,
        # naming the variables, rather than at the first request as a bare 401.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderError) as ctx:
                OpenAIProvider()
        msg = str(ctx.exception)
        self.assertIn("OPENAI_API_KEY", msg)
        self.assertIn("DEEPSEEK_API_KEY", msg)
        self.assertIn("--demo", msg)

    def test_an_empty_key_means_this_endpoint_needs_no_auth(self):
        # A local vLLM is the case. Forcing a dummy key in to satisfy a check
        # that does not apply to it would be worse than sending no header.
        p = OpenAIProvider(api_key="", base_url="http://localhost:8000/v1", model="m")
        self.assertEqual(p.api_key, "")
        self.assertNotIn("Authorization", p._headers())

    def test_a_key_means_the_authorization_header_is_present(self):
        p = OpenAIProvider(api_key="sk-1", base_url="http://x/v1", model="m")
        self.assertEqual(p._headers()["Authorization"], "Bearer sk-1")

    def test_env_vars_are_the_default_source(self):
        env = {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://example.test/v1", "OPENAI_MODEL": "m1"}
        with mock.patch.dict(os.environ, env, clear=False):
            p = OpenAIProvider()
        self.assertEqual(p.api_key, "sk-test")
        self.assertEqual(p.base_url, "https://example.test/v1")
        self.assertEqual(p.model, "m1")

    def test_trailing_slash_in_base_url_is_normalized(self):
        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1/")
        self.assertEqual(p.base_url, "https://example.test/v1")

    def test_http_error_surfaces_the_provider_message(self):
        import urllib.error

        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1")
        boom = urllib.error.HTTPError(
            "https://example.test/v1/chat/completions", 401, "Unauthorized", {}, None
        )
        boom.read = lambda: b'{"error":"bad key"}'
        self.addCleanup(boom.close)
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(ProviderError) as ctx:
                p.complete([{"role": "user", "content": "hi"}], [])
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("bad key", str(ctx.exception))

    def test_a_truncated_reply_says_so_instead_of_looking_terse(self):
        # finish_reason=length means the output ran out mid-sentence. The
        # model does not know that, so it would report success and stop --
        # which reads as the agent deciding to be brief.
        payload = {
            "choices": [
                {"finish_reason": "length", "message": {"content": "I looked at the", "tool_calls": []}}
            ]
        }
        with mock.patch("urllib.request.urlopen", _reply(payload)):
            turn = OpenAIProvider(api_key="k", base_url="https://x.test/v1", model="m").complete(
                [{"role": "user", "content": "hi"}], []
            )
        self.assertIn("I looked at the", turn.text)
        self.assertIn("output token limit", turn.text)

    def test_a_complete_reply_is_not_annotated(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "all done", "tool_calls": []}}]}
        with mock.patch("urllib.request.urlopen", _reply(payload)):
            turn = OpenAIProvider(api_key="k", base_url="https://x.test/v1", model="m").complete(
                [{"role": "user", "content": "hi"}], []
            )
        self.assertEqual(turn.text, "all done")

    def test_a_truncated_reply_that_made_tool_calls_is_left_alone(self):
        # The tool results are the useful part; a truncation note on top would
        # be noise, and the calls are what the loop acts on.
        payload = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function",
                             "function": {"name": "list_dir", "arguments": "{}"}}
                        ],
                    },
                }
            ]
        }
        with mock.patch("urllib.request.urlopen", _reply(payload)):
            turn = OpenAIProvider(api_key="k", base_url="https://x.test/v1", model="m").complete(
                [{"role": "user", "content": "hi"}], []
            )
        self.assertEqual([c.name for c in turn.tool_calls], ["list_dir"])
        self.assertNotIn("output token limit", turn.text)

    def test_serialized_turns_are_requested_explicitly(self):
        # We want one action per turn so the model re-reads state after every
        # change. Asking costs one line in the body; not asking means the
        # model batches by default. Note the loop still *answers* a batched
        # turn if one arrives -- this only asserts that we ask nicely.
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return _reply({"choices": [{"message": {"content": "ok", "tool_calls": []}}]})(req)

        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1", model="m")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            p.complete([{"role": "user", "content": "hi"}], [{"name": "n", "description": "d", "parameters": {}}])
        self.assertIs(sent["body"]["parallel_tool_calls"], False)
        self.assertEqual(sent["body"]["tool_choice"], "auto")

    def test_unreachable_endpoint_surfaces_the_reason(self):
        # The other transport failure mode. Distinct from HTTPError because
        # the fix is different: a key problem vs. a network/DNS problem.
        import urllib.error

        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1")
        boom = urllib.error.URLError("Name or service not known")
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(ProviderError) as ctx:
                p.complete([{"role": "user", "content": "hi"}], [])
        msg = str(ctx.exception)
        self.assertIn("cannot reach", msg)
        self.assertIn("Name or service not known", msg)

    def test_empty_choices_is_an_error(self):
        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1")

        class Resp:
            def read(self):
                return json.dumps({"choices": []}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            with self.assertRaises(ProviderError) as ctx:
                p.complete([{"role": "user", "content": "hi"}], [])
        self.assertIn("no choices", str(ctx.exception))

    def test_a_successful_call_returns_a_parsed_turn(self):
        p = OpenAIProvider(api_key="k", base_url="https://example.test/v1")
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "reading",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
                        ],
                    }
                }
            ]
        }

        class Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            turn = p.complete(
                [{"role": "user", "content": "hi"}],
                [{"name": "read_file", "description": "d", "parameters": {"type": "object"}}],
            )
        self.assertEqual(turn.text, "reading")
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "a.py"})


class TestMockProvider(unittest.TestCase):
    def test_turns_are_returned_in_order(self):
        p = MockProvider(["a", "b"])
        self.assertEqual(p.complete([], []).text, "a")
        self.assertEqual(p.complete([], []).text, "b")

    def test_a_bare_string_becomes_a_text_only_turn(self):
        p = MockProvider(["just talking"])
        turn = p.complete([], [])
        self.assertEqual(turn.text, "just talking")
        self.assertEqual(turn.tool_calls, [])

    def test_running_out_raises(self):
        p = MockProvider(["a"])
        p.complete([], [])
        with self.assertRaises(AssertionError):
            p.complete([], [])

    def test_callables_receive_the_conversation(self):
        p = MockProvider([lambda msgs: AssistantTurn(text=str(len(msgs)))])
        self.assertEqual(p.complete([{"role": "user", "content": "x"}], []).text, "1")

    def test_every_call_is_recorded_for_inspection(self):
        p = MockProvider(["a", "b"])
        p.complete([{"role": "user", "content": "one"}], [])
        p.complete([{"role": "user", "content": "two"}], [])
        self.assertEqual(len(p.calls), 2)
        self.assertEqual(p.calls[1]["messages"][0]["content"], "two")


if __name__ == "__main__":
    unittest.main()
