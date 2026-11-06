"""Tests for exact Pod inspection and Pod-filtered Kubernetes events."""
import json
import unittest

from ai_agent.integrations.kubernetes import register_kubernetes_read_tools
from ai_agent.registry import ToolRegistry


POD_NAME = "cowgraphloader-b7ff85c76-br92r"


class FakeCommands:
    def __init__(self):
        self.commands = []

    def execute(self, command, timeout=20):
        self.commands.append(command)
        if "get pod/" in command:
            payload = {
                "metadata": {
                    "name": POD_NAME, "namespace": "dev-cc",
                    "uid": "pod-uid", "labels": {"app": "cowgraphloader"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "cowgraphloader-b7ff85c76", "uid": "rs-uid"}],
                },
                "spec": {
                    "nodeName": "node-1",
                    "containers": [{"name": "cowgraphloader", "image": "registry/app:test"}],
                    "initContainers": [{"name": "init-config", "image": "registry/init:test"}],
                    "volumes": [{"name": "config", "configMap": {"name": "app-config"}}],
                },
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{
                        "name": "cowgraphloader", "ready": False, "restartCount": 0,
                        "state": {"waiting": {"reason": "ContainerCreating", "message": ""}},
                    }],
                    "initContainerStatuses": [{
                        "name": "init-config",
                        "state": {"waiting": {"reason": "CreateContainerConfigError", "message": "config map not found"}},
                    }],
                    "conditions": [{"type": "PodScheduled", "status": "True"}],
                },
            }
            return 0, json.dumps(payload), ""
        if "get events" in command:
            return 0, json.dumps({"items": [{
                "involvedObject": {"kind": "Pod", "name": POD_NAME},
                "reason": "FailedMount", "message": "MountVolume.SetUp failed",
                "count": 2, "type": "Warning", "lastTimestamp": "2026-11-06T08:00:00Z",
            }]}), ""
        if "get pods" in command:
            return 0, json.dumps({"items": [{
                "metadata": {"name": POD_NAME, "namespace": "dev-cc",
                             "ownerReferences": [{"kind": "ReplicaSet", "name": "cowgraphloader-b7ff85c76"}]},
                "spec": {"nodeName": "node-1", "containers": [{"name": "cowgraphloader"}]},
                "status": {"phase": "Pending", "containerStatuses": [{
                    "name": "cowgraphloader", "state": {"waiting": {"reason": "ContainerCreating"}}
                }]},
            }]}), ""
        return 1, "", "unexpected command"


class KubernetesPodToolTests(unittest.TestCase):
    def setUp(self):
        self.commands = FakeCommands()
        service = type("Service", (), {"commands": self.commands})()
        self.registry = ToolRegistry()
        register_kubernetes_read_tools(self.registry, service, context="test-context")

    def test_exact_pod_inspection_returns_container_and_owner_evidence(self):
        result = self.registry.get("k8s_get_pod").handler({
            "namespace": "dev-cc", "pod": POD_NAME,
        })
        self.assertEqual(result["name"], POD_NAME)
        self.assertEqual(result["phase"], "Pending")
        self.assertEqual(result["owners"][0]["name"], "cowgraphloader-b7ff85c76")
        self.assertEqual(result["containers"][0]["reason"], "ContainerCreating")
        self.assertEqual(result["initContainers"][0]["reason"], "CreateContainerConfigError")
        self.assertEqual(result["initContainers"][0]["message"], "config map not found")

    def test_events_are_filtered_to_the_requested_pod(self):
        result = self.registry.get("k8s_get_events").handler({
            "namespace": "dev-cc", "pod": POD_NAME,
        })
        self.assertEqual(result[0]["object"], POD_NAME)
        self.assertEqual(result[0]["reason"], "FailedMount")
        self.assertIn("--field-selector=", self.commands.commands[-1])
        self.assertIn("involvedObject.name=" + POD_NAME, self.commands.commands[-1])

    def test_pod_inventory_returns_structured_owner_and_state_data(self):
        result = self.registry.get("k8s_list_pods").handler({"namespace": "dev-cc"})
        self.assertEqual(result[0]["name"], POD_NAME)
        self.assertEqual(result[0]["owners"][0]["name"], "cowgraphloader-b7ff85c76")
        self.assertEqual(result[0]["containers"][0]["reason"], "ContainerCreating")


if __name__ == "__main__":
    unittest.main()
