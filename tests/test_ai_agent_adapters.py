"""Regression tests for model tool-call response parsing."""
import unittest

from ai_agent.adapters import PromptToolAdapter
from ai_agent.models import AssistantMessage, AgentStatus, ToolSpec
from ai_agent.registry import ToolRegistry
from ai_agent.runtime import AgentRuntime


class PromptToolAdapterTests(unittest.TestCase):
    def _adapter(self, response):
        def provider(*args, **kwargs):
            return response, None
        return PromptToolAdapter("embedded", "", "test-model", provider)

    def test_parses_plain_json_tool_calls(self):
        result = self._adapter(
            '{"tool_calls":[{"id":"call_1","name":"k8s_list_pods","arguments":{"namespace":"test-cc"}}],"content":"Checking pods."}'
        ).complete(messages=[], tools=[])
        self.assertIsInstance(result, AssistantMessage)
        self.assertEqual(result.content, "Checking pods.")
        self.assertEqual(result.tool_calls[0].name, "k8s_list_pods")
        self.assertEqual(result.tool_calls[0].arguments, {"namespace": "test-cc"})

    def test_parses_fenced_json(self):
        result = self._adapter(
            'Here is the plan:\n```json\n{"tool_calls":[{"name":"k8s_list_deployments","arguments":{"namespace":"test-cc"}}]}\n```'
        ).complete(messages=[], tools=[])
        self.assertEqual(result.tool_calls[0].name, "k8s_list_deployments")

    def test_parses_json_after_short_prose_prefix(self):
        result = self._adapter(
            'I will inspect the cluster first. {"tool_calls":[{"name":"k8s_get_events","arguments":{"namespace":"test-cc","limit":20}}],"content":"Checking events."}'
        ).complete(messages=[], tools=[])
        self.assertEqual(result.tool_calls[0].name, "k8s_get_events")
        self.assertEqual(result.tool_calls[0].arguments["limit"], 20)

    def test_parses_openai_style_function_call(self):
        result = self._adapter(
            '{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"k8s_list_pods","arguments":"{\\\"namespace\\\":\\\"test-cc\\\"}"}}]}'
        ).complete(messages=[], tools=[])
        self.assertEqual(result.tool_calls[0].arguments, {"namespace": "test-cc"})

    def test_braces_inside_json_strings_do_not_end_object_early(self):
        result = self._adapter(
            '{"tool_calls":[{"name":"k8s_get_pod_logs","arguments":{"namespace":"test-cc","pod":"api","tail":20}}],"content":"Inspect {pod} logs."}'
        ).complete(messages=[], tools=[])
        self.assertEqual(result.tool_calls[0].name, "k8s_get_pod_logs")
        self.assertEqual(result.content, "Inspect {pod} logs.")

    def test_recovers_once_from_malformed_tool_json(self):
        responses = iter([
            '{"tool_calls":[{"name":"k8s_list_pods","arguments":{}}',
            '{"tool_calls":[{"name":"k8s_list_pods","arguments":{"namespace":"dev-cc"}}]}',
        ])
        calls = []
        def provider(*args, **kwargs):
            calls.append(args[3])
            return next(responses), None
        result = PromptToolAdapter("embedded", "", "test-model", provider).complete(
            messages=[], tools=[]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.tool_calls[0].name, "k8s_list_pods")
        self.assertEqual(result.tool_calls[0].arguments, {"namespace": "dev-cc"})
        self.assertIn("FORMAT RECOVERY", calls[1])


    def test_incomplete_tool_call_json_is_an_error_not_raw_text(self):
        with self.assertRaisesRegex(ValueError, "incomplete tool-call JSON"):
            self._adapter('{"tool_calls":[{"name":"k8s_list_pods","arguments":{}}').complete(
                messages=[], tools=[]
            )

    def test_tool_calls_are_dispatched_and_results_return_to_the_model(self):
        responses = iter([
            '{"tool_calls":[{"id":"call_1","name":"read_cluster","arguments":{}}],"content":"Checking cluster state."}',
            "The cluster read completed successfully.",
        ])
        def provider(*args, **kwargs):
            return next(responses), None
        adapter = PromptToolAdapter("embedded", "", "test-model", provider)
        registry = ToolRegistry()
        executions = []
        registry.register(ToolSpec(
            "read_cluster", "Read cluster state.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: executions.append("called") or {"status": "Ready"},
        ))
        result = AgentRuntime(registry).run("Check cluster", adapter)
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(result.answer, "The cluster read completed successfully.")
        self.assertEqual(executions, ["called"])
        self.assertTrue(any(message.get("role") == "tool" for message in result.messages))

    def test_invalid_tool_calls_shape_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "expected a JSON array"):
            self._adapter('{"tool_calls":"not-an-array"}').complete(messages=[], tools=[])

    def test_plain_text_answer_is_preserved(self):
        result = self._adapter("The deployment is healthy based on the supplied evidence.").complete(
            messages=[], tools=[]
        )
        self.assertEqual(result.content, "The deployment is healthy based on the supplied evidence.")
        self.assertEqual(result.tool_calls, [])

    def test_empty_tool_calls_with_content_is_a_normal_answer(self):
        result = self._adapter('{"tool_calls":[],"content":"No additional checks are needed."}').complete(
            messages=[], tools=[]
        )
        self.assertEqual(result.content, "No additional checks are needed.")
        self.assertEqual(result.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
