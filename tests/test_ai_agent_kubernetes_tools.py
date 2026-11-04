"""Tests for the explicitly registered, read-only Kubernetes agent tools."""
import unittest
from types import SimpleNamespace
from ai_agent import ToolRegistry
from ai_agent.integrations.kubernetes import register_kubernetes_read_tools

class FakeCommands:
    def __init__(self, output="ok", error="", code=0):
        self.output = output
        self.error = error
        self.code = code
        self.commands = []
    def execute(self, command, timeout=30):
        self.commands.append((command, timeout))
        return self.code, self.output, self.error

class KubernetesToolTests(unittest.TestCase):
    def setUp(self):
        self.commands = FakeCommands()
        self.service = SimpleNamespace(commands=self.commands)
        self.registry = register_kubernetes_read_tools(ToolRegistry(), self.service, context="dev-cluster", include_logs=True)

    def test_registers_only_read_tools(self):
        self.assertEqual(set(self.registry.names()), {
            "k8s_list_pods", "k8s_list_deployments", "k8s_list_nodes",
            "k8s_get_events", "k8s_get_pod_logs", "k8s_get_configmap_key",
        })
        self.assertTrue(all(self.registry.get(name).permission == "read" for name in self.registry.names()))

    def test_pod_listing_uses_fixed_read_only_command(self):
        result = self.registry.get("k8s_list_pods").handler({"namespace": "dev"})
        command, timeout = self.commands.commands[0]
        self.assertIn("kubectl --context=dev-cluster -n dev get pods -o wide", command)
        self.assertNotIn("delete", command)
        self.assertEqual(result, "ok")
        self.assertLessEqual(timeout, 25)

    def test_invalid_namespace_is_rejected_before_remote_execution(self):
        with self.assertRaises(ValueError):
            self.registry.get("k8s_list_pods").handler({"namespace": "dev; rm -rf /"})
        self.assertEqual(self.commands.commands, [])

    def test_pod_name_is_quoted_and_tail_is_bounded(self):
        with self.assertRaises(ValueError):
            self.registry.get("k8s_get_pod_logs").handler({
                "namespace": "dev", "pod": "api; touch /tmp/pwned", "tail": 50,
            })
        self.assertEqual(self.commands.commands, [])
        self.registry.get("k8s_get_pod_logs").handler({
            "namespace": "dev", "pod": "api-123", "tail": 100,
        })
        command, _ = self.commands.commands[0]
        self.assertIn("--tail=100", command)

    def test_event_limit_is_bounded(self):
        with self.assertRaises(ValueError):
            self.registry.get("k8s_get_events").handler({"namespace": "dev", "limit": 500})
        self.assertEqual(self.commands.commands, [])

    def test_success_with_empty_stdout_preserves_stderr(self):
        self.commands.output = "  "
        self.commands.error = "Warning: unable to retrieve optional metadata"
        result = self.registry.get("k8s_list_nodes").handler({})
        self.assertIn("unable to retrieve optional metadata", result)

    def test_failed_command_prefers_stderr(self):
        self.commands.code = 1
        self.commands.output = "partial output"
        self.commands.error = "Forbidden"
        with self.assertRaisesRegex(RuntimeError, "Forbidden"):
            self.registry.get("k8s_list_nodes").handler({})

    def test_log_output_is_redacted(self):
        self.commands.output = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        result = self.registry.get("k8s_get_pod_logs").handler({"namespace": "dev", "pod": "api"})
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result)
        self.assertIn("[REDACTED]", result)

    def test_pod_logs_are_not_registered_without_opt_in(self):
        registry = register_kubernetes_read_tools(ToolRegistry(), self.service, context="dev-cluster")
        self.assertNotIn("k8s_get_pod_logs", registry.names())

    def test_namespace_must_be_a_dns_label(self):
        with self.assertRaises(ValueError):
            self.registry.get("k8s_list_pods").handler({"namespace": "dev.team"})
        self.assertEqual(self.commands.commands, [])

    def test_context_is_validated(self):
        with self.assertRaises(ValueError):
            register_kubernetes_read_tools(ToolRegistry(), self.service, context="dev; rm -rf /")

    def test_context_names_with_spaces_are_safely_quoted(self):
        registry = register_kubernetes_read_tools(ToolRegistry(), self.service, context="team staging")
        registry.get("k8s_list_nodes").handler({})
        command, _ = self.commands.commands[-1]
        self.assertIn("--context='team staging'", command)

    def test_context_control_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            register_kubernetes_read_tools(ToolRegistry(), self.service, context="team\nstaging")
        self.assertEqual(self.commands.commands, [])


    def test_configmap_key_tool_reads_only_the_requested_key(self):
        self.commands.output = "queue-ready"
        result = self.registry.get("k8s_get_configmap_key").handler({
            "namespace": "dev-cc", "configmap": "cow-env",
            "key": "RECORDS_READY_FOR_PROCESSING_QUEUE_NAME",
        })
        self.assertEqual(result, {
            "found": True, "resource": "ConfigMap", "name": "cow-env",
            "namespace": "dev-cc", "key": "RECORDS_READY_FOR_PROCESSING_QUEUE_NAME",
            "value": "queue-ready",
        })
        command, _ = self.commands.commands[-1]
        self.assertIn("get configmap/cow-env", command)
        self.assertIn("RECORDS_READY_FOR_PROCESSING_QUEUE_NAME", command)
        self.assertNotIn("OTHER", command)
        self.assertNotIn("get pods", command)

    def test_configmap_key_tool_treats_missing_key_as_unverified(self):
        self.commands.code = 1
        self.commands.error = 'error: error executing jsonpath: no entry for key "MISSING_KEY"'
        with self.assertRaisesRegex(RuntimeError, "no entry for key"):
            self.registry.get("k8s_get_configmap_key").handler({
                "namespace": "dev-cc", "configmap": "cow-env", "key": "MISSING_KEY",
            })

    def test_configmap_key_tool_rejects_invalid_key_before_remote_execution(self):
        with self.assertRaises(ValueError):
            self.registry.get("k8s_get_configmap_key").handler({
                "namespace": "dev-cc", "configmap": "cow-env", "key": "X; cat /etc/passwd",
            })
        self.assertEqual(self.commands.commands, [])

    def test_kubectl_not_found_is_a_tool_error_not_successful_empty_result(self):
        self.commands.code = 1
        self.commands.error = 'Error from server (NotFound): configmaps "cow-env" not found'
        with self.assertRaisesRegex(RuntimeError, "NotFound"):
            self.registry.get("k8s_get_configmap_key").handler({
                "namespace": "dev-cc", "configmap": "cow-env", "key": "RECORDS_READY_FOR_PROCESSING_QUEUE_NAME",
            })


if __name__ == "__main__":
    unittest.main()
