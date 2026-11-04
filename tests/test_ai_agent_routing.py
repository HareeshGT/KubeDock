"""Tests for deterministic routing between the agent and legacy Ops Mind."""
import unittest

from ai_agent.routing import should_use_legacy_interpreter


class AgentRoutingTests(unittest.TestCase):
    def test_mutations_stay_on_legacy_interpreter(self):
        for request in ("delete pod api", "restart deployment web", "scale api to 3"):
            with self.subTest(request=request):
                self.assertTrue(should_use_legacy_interpreter(request))

    def test_investigation_requests_stay_on_existing_investigation_workflow(self):
        for request in ("investigate pod api", "root cause investigation for pod api"):
            with self.subTest(request=request):
                self.assertTrue(should_use_legacy_interpreter(request))

    def test_describe_requests_stay_on_legacy_interpreter(self):
        for request in ("describe pod api", "describe deployment web"):
            with self.subTest(request=request):
                self.assertTrue(should_use_legacy_interpreter(request))

    def test_get_key_requests_stay_on_legacy_interpreter(self):
        for request in (
            "get key DB_USER from configmap app-config",
            "get keys DB_USER and DB_PASS from secret app-secret",
        ):
            with self.subTest(request=request):
                self.assertTrue(should_use_legacy_interpreter(request))

    def test_supported_read_only_requests_use_agent(self):
        for request in ("list pods", "show deployments", "why is pod api pending"):
            with self.subTest(request=request):
                self.assertFalse(should_use_legacy_interpreter(request))


if __name__ == "__main__":
    unittest.main()
