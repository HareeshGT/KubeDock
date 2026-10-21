import unittest

from k8s_ai_ops_parts.operations import (
    _build_mutation_verification_command,
    _mutation_verification_outcome,
)


class MutationVerificationTests(unittest.TestCase):
    def test_scale_requires_actual_replica_count_to_match(self):
        action = {"action": "scale", "resource": "deployment", "name": "api", "namespace": "prod", "replicas": 3}
        self.assertTrue(_mutation_verification_outcome(action, "3", "", 0)[0])
        self.assertFalse(_mutation_verification_outcome(action, "2", "", 0)[0])
        self.assertFalse(_mutation_verification_outcome(action, "3", "RBAC error", 0)[0])

    def test_delete_only_passes_when_not_found_is_confirmed(self):
        action = {"action": "delete", "resource": "pod", "name": "api-123", "namespace": "prod"}
        self.assertTrue(_mutation_verification_outcome(action, "Error from server (NotFound)", "", 1)[0])
        self.assertFalse(_mutation_verification_outcome(action, "pod/api-123", "", 0)[0])
        self.assertFalse(_mutation_verification_outcome(action, "Forbidden", "", 1)[0])

    def test_restart_requires_successful_rollout_status(self):
        action = {"action": "restart", "resource": "deployment", "name": "api", "namespace": "prod"}
        self.assertTrue(_mutation_verification_outcome(action, "deployment \"api\" successfully rolled out", "", 0)[0])
        self.assertFalse(_mutation_verification_outcome(action, "deadline exceeded", "", 1)[0])

    def test_verification_command_is_built_from_validated_target(self):
        action = {
            "action": "scale", "resource": "deployment", "name": "api",
            "namespace": "prod", "context": "prod-cluster", "replicas": 4,
        }
        command = _build_mutation_verification_command(action)
        self.assertIn("--context prod-cluster", command)
        self.assertIn("-n prod", command)
        self.assertIn("deployment/api", command)
        self.assertIn(".spec.replicas", command)


if __name__ == "__main__":
    unittest.main()
