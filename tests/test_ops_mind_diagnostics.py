"""Tests for Ops Mind's deterministic Kubernetes diagnostic helpers."""

import unittest

from k8s_ai_ops_parts.operations import (
    _kubectl_failure_hint,
    _kubectl_workload_hint,
)
from k8s_ai_ops_parts.core import _build_k8s_ops_prompt


class KubectlFailureHintTests(unittest.TestCase):
    def test_permission_denied_suggests_rbac_check(self):
        hint = _kubectl_failure_hint("Error from server (Forbidden): cannot list pods")
        self.assertIn("RBAC", hint)

    def test_missing_resource_suggests_name_and_namespace_check(self):
        hint = _kubectl_failure_hint("Error from server (NotFound): pods \"web\" not found")
        self.assertIn("namespace", hint.lower())

    def test_connectivity_error_suggests_network_check(self):
        hint = _kubectl_failure_hint("Unable to connect to the server: connection refused")
        self.assertIn("connectivity", hint.lower())

    def test_unknown_error_has_no_speculative_hint(self):
        self.assertEqual("", _kubectl_failure_hint("unexpected internal failure"))

    def test_crashloop_hint_is_read_only(self):
        hint = _kubectl_failure_hint("Pod status: CrashLoopBackOff")
        self.assertIn("logs", hint.lower())
        self.assertIn("events", hint.lower())

    def test_workload_hint_for_image_pull_failure(self):
        hint = _kubectl_workload_hint("web-123 ImagePullBackOff")
        self.assertIn("image", hint.lower())
        self.assertIn("imagePullSecrets", hint)

    def test_workload_hint_for_oom_kill(self):
        hint = _kubectl_workload_hint("Last State: Terminated, Reason: OOMKilled")
        self.assertIn("memory", hint.lower())

    def test_healthy_output_has_no_hint(self):
        self.assertEqual("", _kubectl_workload_hint("NAME READY STATUS\nweb 1/1 Running"))


class OpsMindPromptSafetyTests(unittest.TestCase):
    def test_live_container_inspection_is_not_misrepresented_as_get(self):
        prompt = _build_k8s_ops_prompt("show actual env variables inside pod web", "", "dev", [])
        self.assertIn("live environment", prompt)
        self.assertIn("Do not convert requests to inspect logs", prompt)

    def test_prompt_keeps_single_allowlisted_operation_contract(self):
        prompt = _build_k8s_ops_prompt("get pod web", "default", "dev", [])
        self.assertIn("EXACTLY ONE allow-listed operation", prompt)
        self.assertIn("Never return more than one operation", prompt)


if __name__ == "__main__":
    unittest.main()
