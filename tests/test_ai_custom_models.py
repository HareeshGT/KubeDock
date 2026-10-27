import json
import unittest
from unittest.mock import patch

import ai_assist


class CustomAIModelTests(unittest.TestCase):
    def setUp(self):
        self.settings_store = {
            "ai": {
                "provider": "mistral",
                "api_keys": {"mistral": "mistral-key", "openai": "openai-key"},
                "models": {"mistral": "mistral-small-latest"},
            },
            "theme": "App Default",
        }
        self.load_patch = patch("ai_assist.load_settings", side_effect=self._load)
        self.save_patch = patch("ai_assist.save_settings", side_effect=self._save)
        self.load_patch.start()
        self.save_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.save_patch.stop)

    def _load(self):
        return json.loads(json.dumps(self.settings_store))

    def _save(self, **values):
        self.settings_store.update(values)

    def test_add_edit_delete_custom_model_persists(self):
        added = ai_assist.add_custom_model("openai", "OpenAI 120B", "provider_model_120b")
        self.assertEqual(added, {
            "display_name": "OpenAI 120B", "model_id": "provider_model_120b"
        })
        self.assertEqual(ai_assist.get_ai_settings()["custom_models"]["openai"], [added])
        updated = ai_assist.update_custom_model(
            "openai", "provider_model_120b", "OpenAI Large", "provider_model_large"
        )
        self.assertEqual(updated["display_name"], "OpenAI Large")
        self.assertEqual(updated["model_id"], "provider_model_large")
        ai_assist.delete_custom_model("openai", "provider_model_large")
        self.assertEqual(ai_assist.get_ai_settings()["custom_models"]["openai"], [])

    def test_duplicate_id_and_builtin_collision_are_rejected(self):
        ai_assist.add_custom_model("groq", "Custom OSS", "custom_oss_120b")
        with self.assertRaisesRegex(ValueError, "already exists"):
            ai_assist.add_custom_model("groq", "Duplicate label", "custom_oss_120b")
        with self.assertRaisesRegex(ValueError, "already exists"):
            ai_assist.add_custom_model("groq", "Built-in collision", "openai/gpt-oss-120b")

    def test_invalid_custom_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Display name"):
            ai_assist.add_custom_model("mistral", " ", "mistral-small")
        with self.assertRaisesRegex(ValueError, "exact API model ID"):
            ai_assist.add_custom_model("mistral", "Bad ID", "bad model id")

    def test_selected_custom_model_and_keys_survive_settings_reload(self):
        records = {
            "gemini": [{"display_name": "Gemini Custom", "model_id": "gemini_custom_v2"}]
        }
        ai_assist.save_ai_settings(
            "gemini",
            {"gemini": "gem-key", "mistral": "mistral-key"},
            {"gemini": "gemini_custom_v2", "mistral": "mistral-small-latest"},
            custom_models=records,
        )
        reloaded = ai_assist.get_ai_settings()
        self.assertEqual(reloaded["provider"], "gemini")
        self.assertEqual(reloaded["models"]["gemini"], "gemini_custom_v2")
        self.assertEqual(ai_assist.get_model("gemini"), "gemini_custom_v2")
        self.assertEqual(ai_assist.get_api_key("gemini"), "gem-key")
        self.assertEqual(
            reloaded["custom_models"]["gemini"][0]["model_id"], "gemini_custom_v2"
        )

    def test_each_provider_builder_passes_exact_model_id(self):
        model_id = "vendor/model_v2-preview:2026"
        for provider_id, provider in ai_assist.PROVIDERS.items():
            with self.subTest(provider=provider_id):
                url, headers, body = provider["build_request"](
                    "hello", "test-key", model_id
                )
                if provider_id == "gemini":
                    self.assertIn("/models/" + model_id + ":generateContent", url)
                else:
                    self.assertEqual(json.loads(body)["model"], model_id)
                expected_header = (
                    "x-api-key" if provider_id == "anthropic" else
                    "x-goog-api-key" if provider_id == "gemini" else
                    "Authorization"
                )
                self.assertIn(expected_header, headers)

    def test_backward_compatibility_for_existing_settings(self):
        self.settings_store["ai"] = {
            "provider": "mistral",
            "api_key": "legacy-anthropic-key",
            "api_keys": {"mistral": "existing-mistral-key"},
            "models": {"mistral": "old-custom-model-id"},
        }
        settings = ai_assist.get_ai_settings()
        self.assertEqual(settings["provider"], "mistral")
        self.assertEqual(settings["api_keys"]["mistral"], "existing-mistral-key")
        self.assertEqual(settings["api_keys"]["anthropic"], "legacy-anthropic-key")
        self.assertEqual(settings["models"]["mistral"], "old-custom-model-id")
        self.assertEqual(settings["custom_models"]["mistral"], [])

    def test_blank_or_invalid_selected_model_does_not_fall_back(self):
        result, error = ai_assist._call_provider(
            "openai", "test-api-key", "", "hello"
        )
        self.assertIsNone(result)
        self.assertIn("No model selected", error)
        result, error = ai_assist._call_provider(
            "openai", "test-api-key", "bad model id", "hello"
        )
        self.assertIsNone(result)
        self.assertIn("Invalid model ID syntax", error)


if __name__ == "__main__":
    unittest.main()
