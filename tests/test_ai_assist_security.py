import unittest
from unittest.mock import patch

import ai_assist


class AIContextSecurityTests(unittest.TestCase):
    def test_redacts_assignments_bearer_tokens_and_private_keys(self):
        raw = (
            'DB_PASSWORD="db-password-value"\\n'
            'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\\n'
            '-----BEGIN PRIVATE KEY-----\\nprivate-key-material\\n-----END PRIVATE KEY-----'
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
            resource_context="Conditions: Ready=False\\nEvents: BackOff; API_TOKEN=super-secret-token",
        )
        self.assertIn("resource_evidence", prompt)
        self.assertIn("Conditions: Ready=False", prompt)
        self.assertIn("### Finding", prompt)
        self.assertIn("### Verification", prompt)
        self.assertNotIn("super-secret-password", prompt)
        self.assertNotIn("super-secret-token", prompt)

    def test_followup_context_is_redacted_and_bounded(self):
        prompt = ai_assist._build_followup_prompt(
            "Pod: api\\nDB_PASSWORD=super-secret-password",
            [{"role": "assistant", "content": "Token: abcdefghijklmnopqrstuvwxyz123456"}],
            "Why did API_TOKEN=super-secret-token fail?",
        )
        self.assertNotIn("super-secret-password", prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", prompt)
        self.assertNotIn("super-secret-token", prompt)

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
