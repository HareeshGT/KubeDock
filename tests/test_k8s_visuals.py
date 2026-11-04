import unittest

from k8s_visuals import resource_status_tone, summarize_pod_statuses


class KubernetesVisualStateTests(unittest.TestCase):
    def test_running_pod_is_healthy(self):
        self.assertEqual(resource_status_tone("Running"), "healthy")

    def test_completed_job_is_healthy(self):
        self.assertEqual(resource_status_tone("Completed"), "healthy")

    def test_restart_count_flags_attention_without_marking_failure(self):
        self.assertEqual(resource_status_tone("Running", "2"), "warning")

    def test_pending_and_terminating_are_warnings(self):
        self.assertEqual(resource_status_tone("Pending"), "warning")
        self.assertEqual(resource_status_tone("Terminating"), "warning")

    def test_crash_loop_and_failed_states_are_critical(self):
        self.assertEqual(resource_status_tone("CrashLoopBackOff"), "critical")
        self.assertEqual(resource_status_tone("ImagePullBackOff"), "critical")
        self.assertEqual(resource_status_tone("Failed"), "critical")

    def test_unknown_state_is_not_assumed_healthy(self):
        self.assertEqual(resource_status_tone(""), "unknown")
        self.assertEqual(resource_status_tone("SomeFuturePhase"), "unknown")

    def test_summary_counts_every_pod_exactly_once(self):
        pods = [
            {"status": "Running", "restarts": "0"},
            {"status": "Running", "restarts": "1"},
            {"status": "Pending", "restarts": "0"},
            {"status": "CrashLoopBackOff", "restarts": "4"},
            {"status": "", "restarts": "0"},
        ]
        summary = summarize_pod_statuses(pods)
        self.assertEqual(summary, {
            "healthy": 1, "warning": 2, "critical": 1, "unknown": 1,
        })
        self.assertEqual(sum(summary.values()), len(pods))


if __name__ == "__main__":
    unittest.main()
