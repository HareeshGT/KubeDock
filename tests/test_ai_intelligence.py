import tempfile
import unittest
from pathlib import Path

from ai_intelligence import (
    UsageLedger, bounded_watch_config, build_incident_bundle, build_service_index,
    compare_cluster_snapshots, correlate_incident_signals, recommend_rightsizing,
    redact_evidence, validate_remediation_plan,
)


class AIIntelligenceTests(unittest.TestCase):
    def test_incident_bundle_redacts_secret_and_has_hash(self):
        bundle = build_incident_bundle(
            title="Pod crash", cluster="dev", namespace="app", resource="pod/api",
            evidence={"logs": "DB_PASSWORD=secret-value", "status": "CrashLoopBackOff"},
            collected_at="2026-01-01T00:00:00Z",
        )
        self.assertNotIn("secret-value", str(bundle))
        self.assertEqual(len(bundle["evidence_sha256"]), 64)

    def test_redaction_handles_private_key_and_bearer(self):
        value = redact_evidence("Authorization: Bearer abcdefghijklmnop\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        self.assertNotIn("abcdefghijklmnop", value)
        self.assertNotIn("\nabc\n", value)

    def test_signal_correlation_is_not_claimed_as_proof(self):
        result = correlate_incident_signals(logs=["OOMKilled after memory cgroup out of memory"])
        self.assertEqual(result["observations"][0]["signal"], "oom")
        self.assertIn("not proof", result["note"])

    def test_remediation_is_proposal_only(self):
        result = validate_remediation_plan({"action": "scale", "namespace": "apps", "resource": "deployment", "name": "api", "replicas": 3})
        self.assertEqual(result["execution"], "not_executed")
        self.assertTrue(result["requires_user_approval"])

    def test_delete_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            validate_remediation_plan({"action": "delete", "namespace": "apps", "resource": "pod", "name": "api"})
    
    def test_protected_namespace_rejected(self):
        with self.assertRaisesRegex(ValueError, "protected"):
            validate_remediation_plan({"action": "restart", "namespace": "kube-system", "resource": "deployment", "name": "dns"})

    def test_watch_config_is_bounded_and_non_mutating(self):
        config = bounded_watch_config(duration_seconds=120, interval_seconds=30)
        self.assertEqual(config["max_observations"], 4)
        self.assertFalse(config["automatic_mutations"])
        with self.assertRaises(ValueError):
            bounded_watch_config(duration_seconds=7200, interval_seconds=1)

    def test_cluster_compare_reports_changes_and_missing_resources(self):
        left = {"cluster": "dev", "resources": [{"kind": "Pod", "namespace": "a", "name": "x", "phase": "Running"}]}
        right = {"cluster": "test", "resources": [{"kind": "Pod", "namespace": "a", "name": "x", "phase": "Pending"}, {"kind": "Service", "namespace": "a", "name": "api"}]}
        result = compare_cluster_snapshots(left, right)
        self.assertEqual(result["changed"][0]["left"]["phase"], "Running")
        self.assertEqual(result["only_right"], [["service", "a", "api"]])

    def test_rightsizing_requires_samples_and_review(self):
        sparse = recommend_rightsizing(cpu_usage_millicores=100, memory_usage_bytes=1000, sample_count=2)
        self.assertEqual(sparse["status"], "insufficient_data")
        result = recommend_rightsizing(cpu_usage_millicores=100, memory_usage_bytes=1000, sample_count=10)
        self.assertEqual(result["recommendations"][0]["suggested_request"], 125)
        self.assertTrue(result["requires_human_review"])

    def test_service_index_deduplicates_and_sorts(self):
        result = build_service_index([{"name": "api", "namespace": "app"}, {"name": "api", "namespace": "app"}])
        self.assertEqual(len(result), 1)

    def test_usage_ledger_stores_only_aggregates_and_honors_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(str(Path(directory) / "usage.db"))
            ledger.record("groq", "quick", success=True, latency_ms=100)
            ledger.record("groq", "quick", success=False, latency_ms=300)
            ledger.record("openai", "voice", success=True, enabled=False)
            rows = ledger.summary()
            self.assertEqual(rows, [{"provider": "groq", "profile": "quick", "calls": 2, "errors": 1, "average_latency_ms": 200}])



