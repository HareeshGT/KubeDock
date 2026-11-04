import json
import unittest

from k8s_node_metrics import (
    build_node_inventory_command, parse_node_inventory,
    split_node_inventory_output,
)


def node(name, ready="True", memory_pressure="False", unschedulable=False):
    return {
        "metadata": {
            "name": name,
            "labels": {"node-role.kubernetes.io/control-plane": ""},
        },
        "spec": {"unschedulable": unschedulable},
        "status": {
            "conditions": [
                {"type": "Ready", "status": ready},
                {"type": "MemoryPressure", "status": memory_pressure},
                {"type": "DiskPressure", "status": "False"},
                {"type": "PIDPressure", "status": "False"},
                {"type": "NetworkUnavailable", "status": "False"},
            ],
            "capacity": {"cpu": "4", "memory": "8Gi"},
            "allocatable": {"cpu": "3800m", "memory": "7Gi"},
            "nodeInfo": {"kubeletVersion": "v1.30.1"},
        },
    }


class KubernetesNodeMetricsTests(unittest.TestCase):
    def test_joins_node_usage_and_allocatable_by_name(self):
        payload = {"items": [node("worker-a")]}
        top = "worker-a 1900m 50% 3584Mi 50%"
        parsed = parse_node_inventory(json.dumps(payload), top)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "worker-a")
        self.assertEqual(parsed[0].tone, "healthy")
        self.assertEqual(parsed[0].cpu_percent, 50)
        self.assertEqual(parsed[0].memory_percent, 50)
        self.assertEqual(parsed[0].cpu_capacity, "3800m")

    def test_not_ready_is_critical(self):
        parsed = parse_node_inventory(json.dumps({"items": [node("broken", ready="False")]}))
        self.assertEqual(parsed[0].tone, "critical")
        self.assertEqual(parsed[0].status, "NotReady")

    def test_pressure_and_unschedulable_are_warnings(self):
        pressured = parse_node_inventory(json.dumps({"items": [node("pressured", memory_pressure="True")]}))
        cordoned = parse_node_inventory(json.dumps({"items": [node("cordoned", unschedulable=True)]}))
        self.assertEqual(pressured[0].tone, "warning")
        self.assertEqual(cordoned[0].tone, "warning")

    def test_missing_readiness_is_unknown_not_healthy(self):
        item = node("unknown")
        item["status"]["conditions"] = []
        parsed = parse_node_inventory(json.dumps({"items": [item]}))
        self.assertEqual(parsed[0].tone, "unknown")
        self.assertIsNone(parsed[0].cpu_percent)

    def test_missing_metrics_are_not_reported_as_zero(self):
        parsed = parse_node_inventory(json.dumps({"items": [node("worker-a")]}))
        self.assertEqual(parsed[0].cpu_usage, "N/A")
        self.assertIsNone(parsed[0].cpu_percent)
        self.assertEqual(parsed[0].memory_usage, "N/A")

    def test_invalid_payload_returns_empty_inventory(self):
        self.assertEqual(parse_node_inventory("not-json"), [])
        self.assertEqual(parse_node_inventory(json.dumps({"items": "invalid"})), [])

    def test_inventory_command_uses_selected_context_and_bounded_calls(self):
        command = build_node_inventory_command("--context=staging")
        self.assertIn("kubectl --context=staging get nodes --request-timeout=15s -o json", command)
        self.assertIn("kubectl --context=staging top nodes --no-headers --request-timeout=15s", command)

    def test_output_sections_split_without_mixing_json_and_metrics(self):
        output = (
            "__KD_NODE_JSON__\n{\"items\": []}\n"
            "__KD_NODE_TOP__\nworker-a 10m 1% 20Mi 1%"
        )
        nodes_json, top = split_node_inventory_output(output)
        self.assertEqual(nodes_json, '{"items": []}')
        self.assertEqual(top, "worker-a 10m 1% 20Mi 1%")


if __name__ == "__main__":
    unittest.main()
