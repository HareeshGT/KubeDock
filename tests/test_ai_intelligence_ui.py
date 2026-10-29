import unittest
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication

import ai_intelligence_ui


class AIIntelligenceWorkspaceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_workspace_builds_all_feature_tabs_offscreen(self):
        with patch("ai_intelligence_ui.load_settings", return_value={"ai": {"usage_telemetry_enabled": False}}):
            dialog = ai_intelligence_ui.AIIntelligenceDialog()
        try:
            labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
            self.assertIn("Incident investigator", labels)
            self.assertIn("Cluster comparison", labels)
            self.assertIn("Watch mode", labels)
            self.assertIn("Right-sizing", labels)
            self.assertIn("Remediation planner", labels)
            self.assertIn("Service index", labels)
            self.assertIn("Cross-system diagnosis", labels)
            self.assertIn("Usage / latency", labels)
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
