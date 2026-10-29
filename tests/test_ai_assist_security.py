import io
import unittest
import urllib.error
from unittest.mock import patch

import ai_assist


class AIContextSecurityTests(unittest.TestCase):
    def test_redacts_assignments_bearer_tokens_and_private_keys(self):
        raw = (
            'DB_PASSWORD="db-password-value"\n'
            'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n'
            '-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----'
        )
        cleaned = ai_assist._redact_sensitive_text(raw)
        self.assertNotIn("db-password-value", cleaned)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", cleaned)
        self.assertNotIn("private-key-material", cleaned)
        self.assertIn("[REDACTED", cleaned)

    def test_pod_prompt_keeps_live_evidence_and_redacts_secrets(self):
        prompt = ai_assist._build_prompt(
            "orders-7f7c", "production", "api",
            "ERROR DB_PASSWORD=super-secret-password",
            resource_context="Conditions: Ready=False\nEvents: BackOff; API_TOKEN=super-secret-token",
        )
        self.assertIn("resource_evidence", prompt)
        self.assertIn("Conditions: Ready=False", prompt)
        self.assertIn("### Finding", prompt)
        self.assertIn("### Verification", prompt)
        self.assertNotIn("super-secret-password", prompt)
        self.assertNotIn("super-secret-token", prompt)

    def test_response_detail_preference_applies_to_diagnosis_and_followups(self):
        with patch("ai_assist.load_settings", return_value={"ai_response_detail": "concise"}):
            concise = ai_assist._build_prompt("pod", "ns", "", "log")
            followup = ai_assist._build_followup_prompt("evidence", [], "why?")
        self.assertIn("Be concise", concise)
        self.assertIn("Be concise", followup)

        with patch("ai_assist.load_settings", return_value={"ai_response_detail": "detailed"}):
            detailed = ai_assist._build_command_prompt("kubectl get pods", 1, "denied")
        self.assertIn("in more depth", detailed)

    def test_resource_evidence_clip_preserves_head_and_tail(self):
        resource = (
            "Name: api-123\nNamespace: prod\nStatus: Running\n"
            "Containers:\n  State: Running\n  Ready: False\n"
            + "".join(f"annotation-{i}: value-{i}\n" for i in range(500))
            + "Events:\n  Warning BackOff: restarting container\n"
            "Metrics:\n  api 120m 256Mi"
        )
        clipped = ai_assist._clip_resource_evidence(resource, limit=4_000)
        self.assertLessEqual(len(clipped), 4_000)
        self.assertIn("Status: Running", clipped)
        self.assertIn("Ready: False", clipped)
        self.assertIn("Warning BackOff", clipped)
        self.assertIn("api 120m 256Mi", clipped)
        self.assertIn("middle of resource evidence omitted", clipped)

    def test_followup_context_is_redacted_and_bounded(self):
        prompt = ai_assist._build_followup_prompt(
            "Pod: api\nDB_PASSWORD=super-secret-password",
            [{"role": "assistant", "content": "Token: abcdefghijklmnopqrstuvwxyz123456"}],
            "Why did API_TOKEN=super-secret-token fail?",
        )
        self.assertNotIn("super-secret-password", prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", prompt)
        self.assertNotIn("super-secret-token", prompt)

    def test_provider_retries_transient_server_error_once(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return b'{"choices":[{"message":{"content":"Recovered"},"finish_reason":"stop"}]}'

        transient = urllib.error.HTTPError(
            "https://api.mistral.ai/v1/chat/completions",
            503, "Service Unavailable", {}, io.BytesIO(b'{"error":{"message":"temporary"}}'),
        )
        with patch("ai_assist.time.sleep") as sleep, patch(
            "ai_assist.urllib.request.urlopen",
            side_effect=[transient, Response()],
        ) as opened:
            result, error = ai_assist._call_provider(
                "mistral", "test-api-key-123456789", "mistral-small-latest",
                "simple test prompt",
            )
        self.assertEqual(result, "Recovered")
        self.assertIsNone(error)
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_provider_does_not_retry_rate_limit(self):
        rate_limited = urllib.error.HTTPError(
            "https://api.mistral.ai/v1/chat/completions",
            429, "Too Many Requests", {}, io.BytesIO(b'{"error":{"message":"slow down"}}'),
        )
        with patch(
            "ai_assist.urllib.request.urlopen", side_effect=rate_limited
        ) as opened:
            result, error = ai_assist._call_provider(
                "mistral", "test-api-key-123456789", "mistral-small-latest",
                "simple test prompt",
            )
        self.assertIsNone(result)
        self.assertIn("Rate limited", error)
        opened.assert_called_once()

    def test_provider_response_body_is_bounded(self):
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                self.requested_size = size
                return b"x" * size

        response = OversizedResponse()
        with patch("ai_assist.urllib.request.urlopen", return_value=response) as opened:
            result, error = ai_assist._call_provider(
                "mistral", "test-api-key-123456789", "mistral-small-latest",
                "simple test prompt",
            )
        self.assertIsNone(result)
        self.assertIn("size limit", error)
        self.assertEqual(response.requested_size, ai_assist.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(opened.call_args.kwargs["timeout"], ai_assist.PROVIDER_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
