"""Unit tests for the provider-neutral AI agent runtime."""
import unittest
from ai_agent import AgentPolicy, AgentRuntime, AgentStatus, AssistantMessage, ToolCall, ToolRegistry, ToolSpec

SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 40},
    "count": {"type": "integer", "minimum": 0, "maximum": 10}},
    "required": ["name"], "additionalProperties": False}

class FakeAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def complete(self, *, messages, tools):
        self.calls.append((messages, tools))
        return self.responses.pop(0)

class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.executed = []
        self.registry.register(ToolSpec("inspect_pod", "Read pod state.", SCHEMA,
            lambda args: self.executed.append(dict(args)) or {"phase": "Running"}))
        self.registry.register(ToolSpec("restart_workload", "Restart a workload.", SCHEMA,
            lambda args: self.executed.append(dict(args)) or "restarted", permission="write"))

    def test_read_tool_runs_and_model_receives_result(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "inspect_pod", {"name": "api"})]),
                               AssistantMessage(content="The pod is Running.")])
        result = AgentRuntime(self.registry).run("Check api", adapter)
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertIn("Running", result.answer)
        self.assertEqual(len(self.executed), 1)
        self.assertTrue(any(m.get("role") == "tool" for m in result.messages))

    def test_mutation_is_not_run_without_approval(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "restart_workload", {"name": "api"})])])
        result = AgentRuntime(self.registry).run("Restart api", adapter)
        self.assertEqual(result.status, AgentStatus.NEEDS_APPROVAL)
        self.assertEqual(self.executed, [])
        self.assertEqual(result.pending_approvals[0].name, "restart_workload")

    def test_mutation_runs_only_after_explicit_approval(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "restart_workload", {"name": "api"})]),
                               AssistantMessage(content="Restart completed.")])
        result = AgentRuntime(self.registry).run("Restart api", adapter, approval_handler=lambda spec, args: True)
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(len(self.executed), 1)

    def test_invalid_arguments_do_not_execute(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "inspect_pod", {"name": "api", "extra": "x"})]),
                               AssistantMessage(content="I could not inspect the pod.")])
        result = AgentRuntime(self.registry).run("Check api", adapter)
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(self.executed, [])
        self.assertEqual(result.tool_results[0].status, "invalid_arguments")

    def test_unknown_tool_is_rejected(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "run_shell", {"command": "whoami"})]),
                               AssistantMessage(content="I cannot run that tool.")])
        result = AgentRuntime(self.registry).run("Run a command", adapter)
        self.assertEqual(result.tool_results[0].status, "rejected")
        self.assertEqual(self.executed, [])

    def test_tool_allowlist_is_enforced(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "inspect_pod", {"name": "api"})]),
                               AssistantMessage(content="Not allowed.")])
        policy = AgentPolicy(allowed_tools=frozenset({"restart_workload"}))
        result = AgentRuntime(self.registry, policy).run("Check api", adapter)
        self.assertEqual(result.tool_results[0].status, "rejected")
        self.assertEqual(self.executed, [])

    def test_call_limit_stops_execution(self):
        adapter = FakeAdapter([AssistantMessage(tool_calls=[
            ToolCall("c1", "inspect_pod", {"name": "one"}), ToolCall("c2", "inspect_pod", {"name": "two"})])])
        result = AgentRuntime(self.registry, AgentPolicy(max_tool_calls=1)).run("Check pods", adapter)
        self.assertEqual(result.status, AgentStatus.LIMIT_REACHED)
        self.assertEqual(len(self.executed), 1)

    def test_result_size_is_bounded(self):
        self.registry.register(ToolSpec("read_large", "Read a large result.",
            {"type": "object", "properties": {}, "additionalProperties": False}, lambda args: "x" * 500))
        adapter = FakeAdapter([AssistantMessage(tool_calls=[ToolCall("c1", "read_large", {})]),
                               AssistantMessage(content="Done.")])
        result = AgentRuntime(self.registry, AgentPolicy(max_result_chars=20)).run("Read", adapter)
        tool_message = next(m for m in result.messages if m.get("role") == "tool")
        self.assertLessEqual(len(tool_message["content"]), 20)

    def test_empty_request_is_rejected(self):
        self.assertEqual(AgentRuntime(self.registry).run(" ", FakeAdapter([])).status, AgentStatus.ERROR)

    def test_duplicate_tool_registration_is_rejected(self):
        spec = ToolSpec("another_tool", "A tool.", {"type": "object"}, lambda args: None)
        self.registry.register(spec)
        with self.assertRaises(ValueError):
            self.registry.register(spec)

if __name__ == "__main__":
    unittest.main()
