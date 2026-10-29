import unittest

from app_preferences import (
    PERFORMANCE_PROFILES, k8s_connection_profile_key,
    normalize_connection_preferences, normalize_performance_settings,
)


class PerformancePreferencesTests(unittest.TestCase):
    def test_defaults_preserve_existing_refresh_cadence(self):
        actual = normalize_performance_settings({})
        self.assertEqual(actual["dashboard_refresh_seconds"], 3)
        self.assertEqual(actual["background_refresh_seconds"], 60)
        self.assertEqual(actual["kubernetes_refresh_seconds"], 30)
        self.assertEqual(actual["docker_refresh_seconds"], 8)
        self.assertEqual(actual["tunnel_refresh_seconds"], 15)
        self.assertFalse(actual["pause_dashboard_when_inactive"])

    def test_profiles_have_expected_intervals(self):
        for name, expected in PERFORMANCE_PROFILES.items():
            actual = normalize_performance_settings({"performance_settings": {"profile": name}})
            for key, value in expected.items():
                self.assertEqual(actual[key], value)

    def test_custom_intervals_are_bounded(self):
        actual = normalize_performance_settings({"performance_settings": {
            "profile": "custom", "dashboard_refresh_seconds": -999,
            "background_refresh_seconds": 9000, "kubernetes_refresh_seconds": "bad",
            "docker_refresh_seconds": 2, "tunnel_refresh_seconds": 9999,
            "pause_dashboard_when_inactive": True,
        }})
        self.assertEqual(actual["dashboard_refresh_seconds"], 1)
        self.assertEqual(actual["background_refresh_seconds"], 600)
        self.assertEqual(actual["kubernetes_refresh_seconds"], 30)
        self.assertEqual(actual["docker_refresh_seconds"], 3)
        self.assertEqual(actual["tunnel_refresh_seconds"], 300)
        self.assertTrue(actual["pause_dashboard_when_inactive"])

    def test_connection_preferences_are_bounded(self):
        actual = normalize_connection_preferences({"connection_preferences": {
            "ssh_connect_timeout_seconds": 500, "ssh_keepalive_seconds": -1,
        }, "remember_k8s_context": False})
        self.assertEqual(actual["ssh_connect_timeout_seconds"], 60)
        self.assertEqual(actual["ssh_keepalive_seconds"], 0)
        self.assertFalse(actual["remember_k8s_context"])

    def test_connection_keys_are_stable_and_opaque(self):
        a = k8s_connection_profile_key("example.internal", 22, "admin")
        b = k8s_connection_profile_key("EXAMPLE.INTERNAL", "22", "admin")
        c = k8s_connection_profile_key("example.internal", 2222, "admin")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn("example.internal", a)
        self.assertNotIn("admin", a)


if __name__ == "__main__":
    unittest.main()
