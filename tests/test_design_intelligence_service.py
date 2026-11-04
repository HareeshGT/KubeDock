"""Tests for the optional real DIS HTTP integration."""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from services.design_intelligence import (
    DesignIntelligenceError,
    DesignIntelligenceService,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return json.dumps(self.payload).encode("utf-8")


class DesignIntelligenceServiceTests(unittest.TestCase):
    def test_unconfigured_service_is_disabled_and_never_fakes_results(self):
        service = DesignIntelligenceService(base_url="")
        self.assertFalse(service.enabled)
        self.assertIsNone(service.status()["base_url"])
        with self.assertRaisesRegex(DesignIntelligenceError, "not configured"):
            service.run_design_review("Review the Kubernetes dashboard")

    @patch("services.design_intelligence.urlopen")
    def test_design_review_calls_real_endpoint_with_product_context(self, urlopen):
        expected = {
            "project_id": "kubedock",
            "status": "completed",
            "results": [{"role": "visual_qa_engineer", "summary": "API result"}],
            "quality": {"score": 0.8},
        }
        urlopen.return_value = FakeResponse(expected)
        service = DesignIntelligenceService("https://dis.example.test/", timeout_seconds=3)
        result = service.run_design_review(
            "Review the Kubernetes dashboard",
            constraints=["Keep current theme support", "Accessibility"],
            target_platforms=["macOS", "Windows"],
        )
        self.assertEqual(result, expected)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://dis.example.test/api/v1/design/runs")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["project_id"], "kubedock")
        self.assertEqual(payload["target_platforms"], ["macOS", "Windows"])
        self.assertIn("KubeDock", payload["product_context"])

    @patch("services.design_intelligence.urlopen")
    def test_network_failure_is_reported_not_simulated(self, urlopen):
        urlopen.side_effect = URLError("offline")
        service = DesignIntelligenceService("http://127.0.0.1:8000")
        with self.assertRaisesRegex(DesignIntelligenceError, "unreachable"):
            service.run_design_review("Review the pod details panel")

    @patch("services.design_intelligence.urlopen")
    def test_invalid_response_is_rejected(self, urlopen):
        urlopen.return_value = FakeResponse({"results": [], "quality": []})
        service = DesignIntelligenceService("http://127.0.0.1:8000")
        with self.assertRaisesRegex(DesignIntelligenceError, "quality evaluation"):
            service.run_design_review("Review the pod details panel")

    def test_brief_and_autonomy_are_validated_before_network_call(self):
        service = DesignIntelligenceService("http://127.0.0.1:8000")
        with self.assertRaises(ValueError):
            service.run_design_review("tiny")
        with self.assertRaises(ValueError):
            service.run_design_review("Review the dashboard", autonomy="unbounded")


if __name__ == "__main__":
    unittest.main()
