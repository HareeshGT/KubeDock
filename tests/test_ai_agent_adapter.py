"""Tests for the provider-neutral prompt tool-call adapter."""
import unittest
import json
from ai_agent.adapters import PromptToolAdapter

class PromptToolAdapterTests(unittest.TestCase):
    def _adapter(self, response, error=None):
        calls = []
        def provider(*args, **kwargs):
            calls.append((args, kwargs))
            return response, error
        return PromptToolAdapter("ollama", "", "qwen2.5:7b", provider), calls

    def test_parses_json_tool_calls(self):
        adapter, calls = self._adapter(
            '{"tool_calls":[{"id":"t1","name":"inspect_pod","arguments":{"name":"api"}}],"content":"Checking pod."}'
        )
        result = adapter.complete(messages=[{"role": "user", "content": "Check api"}],
                                  tools=[{"name": "inspect_pod"}])
        self.assertEqual(result.tool_calls[0].name, "inspect_pod")
        self.assertEqual(result.tool_calls[0].arguments, {"name": "api"})
        self.assertEqual(len(calls), 1)

    def test_parses_fenced_json_and_string_arguments(self):
        adapter, _ = self._adapter(' ```json\n{"tool_calls":[{"name":"inspect_pod","arguments":"{\\"name\\":\\"api\\"}"}]}\n``` ')
        result = adapter.complete(messages=[], tools=[])
        self.assertEqual(result.tool_calls[0].id, "call_1")
        self.assertEqual(result.tool_calls[0].arguments, {"name": "api"})


    def test_missing_or_invalid_arguments_remain_invalid(self):
        for response in (
            '{"tool_calls":[{"name":"inspect_pod"}]}',
            '{"tool_calls":[{"name":"inspect_pod","arguments":""}]}',
            '{"tool_calls":[{"name":"inspect_pod","arguments":"not-json"}]}',
            '{"tool_calls":[{"name":"inspect_pod","arguments":"[]"}]}',
            '{"tool_calls":[{"name":"inspect_pod","arguments":"null"}]}',
        ):
            with self.subTest(response=response):
                adapter, _ = self._adapter(response)
                result = adapter.complete(messages=[], tools=[])
                self.assertIsNone(result.tool_calls[0].arguments)

    def test_explicit_empty_object_is_valid_zero_argument_call(self):
        for arguments in ('{}', {}):
            response = json.dumps({
                "tool_calls": [{"name": "list_nodes", "arguments": arguments}]
            })
            adapter, _ = self._adapter(response)
            result = adapter.complete(messages=[], tools=[])
            self.assertEqual(result.tool_calls[0].arguments, {})

    def test_plain_text_is_final_answer(self):
        adapter, _ = self._adapter("The cluster looks healthy.")
        result = adapter.complete(messages=[], tools=[])
        self.assertEqual(result.content, "The cluster looks healthy.")
        self.assertEqual(result.tool_calls, [])

    def test_provider_errors_are_raised(self):
        adapter, _ = self._adapter("", "Provider unavailable")
        with self.assertRaisesRegex(RuntimeError, "Provider unavailable"):
            adapter.complete(messages=[], tools=[])

if __name__ == "__main__":
    unittest.main()
