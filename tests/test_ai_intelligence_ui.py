import unittest
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication

import ai_intelligence_ui


class AIIntelligenceWorkspaceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def test_live_rightsizing_parser_uses_peak_sample_and_reads_requests(self):
        with patch("ai_intelligence_ui.load_settings", return_value={"ai": {"usage_telemetry_enabled": False}}):
            dialog = ai_intelligence_ui.AIIntelligenceDialog()
        try:
            lines = []
            for number, cpu, memory in (
                (1, "100m", "100Mi"),
                (2, "200m", "200Mi"),
                (3, "300m", "300Mi"),
                (4, "400m", "400Mi"),
                (5, "500m", "500Mi"),
            ):
                lines.extend([f"__SAMPLE_{number}__", f"api-pod api-container {cpu} {memory}"])
            lines.extend(["__REQUESTS__", "CPU=250m;MEM=256Mi"])
            dialog._on_rightsizing_metrics("\n".join(lines), "", 0)
            self.assertEqual(dialog.sample_count.value(), 5, dialog.rightsizing_output.toPlainText())
            self.assertEqual(dialog.cpu_usage.value(), 500)
            self.assertEqual(dialog.memory_usage.value(), 500 * 1024 * 1024)
            self.assertEqual(dialog.cpu_request.value(), 250)
            self.assertEqual(dialog.memory_request.value(), 256 * 1024 * 1024)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_workspace_builds_all_feature_tabs_offscreen(self):
        with patch("ai_intelligence_ui.load_settings", return_value={"ai": {"usage_telemetry_enabled": False}}):
            dialog = ai_intelligence_ui.AIIntelligenceDialog()
        try:
            labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
            self.assertIn("Overview", labels)
            self.assertIn("Incident investigator", labels)
            self.assertIn("Runbooks", labels)
            self.assertIn("Cluster comparison", labels)
            self.assertIn("Watch mode", labels)
            self.assertIn("Right-sizing", labels)
            self.assertIn("Remediation planner", labels)
            self.assertIn("Service index", labels)
            self.assertIn("Cross-system diagnosis", labels)
            self.assertIn("Usage / latency", labels)
            self.assertEqual(dialog.incident_profile_combo.itemData(0), "quick")
            self.assertFalse(dialog.incident_cloud_consent_chk.isChecked())
            self.assertFalse(dialog.incident_include_runbooks_chk.isChecked())
            self.assertTrue(dialog.prometheus_history_btn.isEnabled())
            self.assertFalse(dialog.send_remediation_btn.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
