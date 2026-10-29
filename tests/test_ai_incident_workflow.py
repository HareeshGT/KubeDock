import tempfile
import unittest
from pathlib import Path

from ai_incident_workflow import (
    build_live_incident_command,
    build_prometheus_history_queries,
    compact_evidence_text,
    compare_watch_states,
    estimate_prompt_tokens,
    load_runbook_documents,
    match_runbook_snippets,
    parse_live_incident_output,
    parse_prometheus_history_results,
    validate_incident_target,
    validate_prometheus_base_url,
)


class IncidentWorkflowTests(unittest.TestCase):
    def test_live_target_rejects_secret_and_shell_injection(self):
        with self.assertRaisesRegex(ValueError, "non-secret"):
            validate_incident_target("dev", "apps", "secret", "api")
        with self.assertRaises(ValueError):
            validate_incident_target("dev; touch /tmp/pwned", "apps", "pod", "api")
        with self.assertRaises(ValueError):
            validate_incident_target("dev", "apps", "pod", "api;cat /etc/passwd")

    def test_live_command_is_read_only_and_does_not_fetch_secret_values(self):
        command = build_live_incident_command("dev", "apps", "pod", "api-123")
        for forbidden in (" delete ", " apply ", " patch ", " get secret", "get configmap"):
            self.assertNotIn(forbidden, command)
        self.assertIn("__KUBEDOCK_LOGS__", command)
        self.assertIn("--tail=100", command)
        self.assertIn("top pod", command)

    def test_cross_system_collection_includes_host_docker_and_read_only_kubernetes(self):
        from ai_incident_workflow import build_cross_system_collection_command
        command = build_cross_system_collection_command("dev", "apps", "pod", "api-123")
        self.assertIn("__KUBEDOCK_HOST__", command)
        self.assertIn("__KUBEDOCK_DOCKER__", command)
        self.assertIn("docker ps", command)
        self.assertIn("kubectl", command)
        self.assertNotIn("docker logs", command)
        self.assertNotIn("delete", command)

    def test_live_output_splits_sections_and_redacts(self):
        sections = parse_live_incident_output(
            "__KUBEDOCK_RESOURCE__\nPhase=Pending\n"
            "__KUBEDOCK_LOGS__\nERROR API_TOKEN=super-secret-value\n"
            "__KUBEDOCK_METRICS__\napi 100m 128Mi\n"
        )
        self.assertIn("Pending", sections["resource_summary"])
        self.assertNotIn("super-secret-value", sections["logs"])
        self.assertIn("128Mi", sections["metrics"])

    def test_runbooks_are_bounded_and_ranked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "oom.md").write_text(
                "# OOMKilled\nIf a pod is OOMKilled, inspect memory limits and recent working-set metrics.",
                encoding="utf-8",
            )
            (root / "dns.txt").write_text(
                "DNS troubleshooting: inspect CoreDNS and verify the service hostname.",
                encoding="utf-8",
            )
            (root / "ignore.yaml").write_text("password: should not load", encoding="utf-8")
            docs = load_runbook_documents(root)
            self.assertEqual(len(docs), 2)
            ranked = match_runbook_snippets("pod OOMKilled memory limits", docs)
            self.assertEqual(ranked[0]["title"], "oom")
            self.assertIn("memory limits", ranked[0]["snippet"])

    def test_compaction_deduplicates_and_respects_limits(self):
        text = compact_evidence_text("same line\nsame line\n" + "\n".join(f"line {i}" for i in range(100)), max_chars=300, max_lines=40)
        self.assertLessEqual(len(text), 300)
        self.assertEqual(text.count("same line"), 1)
        self.assertGreater(estimate_prompt_tokens(text), 0)

    def test_watch_state_comparison_identifies_critical_signal(self):
        result = compare_watch_states("NAME READY STATUS\napi 1/1 Running", "NAME READY STATUS\napi 0/1 CrashLoopBackOff")
        self.assertTrue(result["changed"])
        self.assertEqual(result["severity"], "high")
        self.assertIn("crashloopbackoff", result["critical_signals"])

    def test_prometheus_url_rejects_cleartext_remote_and_credentials(self):
        self.assertEqual(validate_prometheus_base_url("http://localhost:9090/"), "http://localhost:9090")
        self.assertEqual(validate_prometheus_base_url("https://metrics.example.com/"), "https://metrics.example.com")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_prometheus_base_url("http://metrics.example.com:9090")
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_prometheus_base_url("https://user:pass@metrics.example.com")

    def test_prometheus_queries_validate_target_and_window(self):
        query = build_prometheus_history_queries("apps", "api-123", 7)
        self.assertIn("quantile_over_time(0.95", query["cpu_p95_cores"])
        self.assertIn('[7d:5m]', query["cpu_sample_count"])
        with self.assertRaises(ValueError):
            build_prometheus_history_queries("apps;evil", "api", 7)
        with self.assertRaises(ValueError):
            build_prometheus_history_queries("apps", "api", 31)

    def test_prometheus_results_require_five_samples(self):
        def response(value):
            return {"status": "success", "data": {"result": [{"value": [0, str(value)]}]}}
        payload = {
            "cpu_p95_cores": response(0.2),
            "memory_p95_bytes": response(100000000),
            "cpu_sample_count": response(10),
            "memory_sample_count": response(12),
        }
        result = parse_prometheus_history_results(payload)
        self.assertEqual(result["cpu_usage_millicores"], 200.0)
        self.assertEqual(result["memory_usage_bytes"], 100000000)
        self.assertEqual(result["sample_count"], 10)
        payload["cpu_sample_count"] = response(2)
        with self.assertRaisesRegex(ValueError, "Fewer than five"):
            parse_prometheus_history_results(payload)


if __name__ == "__main__":
    unittest.main()
