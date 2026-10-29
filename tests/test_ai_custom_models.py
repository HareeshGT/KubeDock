import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_assist
import db


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
                if provider_id == "ollama":
                    self.assertEqual(url, "http://127.0.0.1:11434/v1/chat/completions")
                    self.assertNotIn("Authorization", headers)
                else:
                    expected_header = (
                        "x-api-key" if provider_id == "anthropic" else
                        "x-goog-api-key" if provider_id == "gemini" else
                        "Authorization"
                    )
                    self.assertIn(expected_header, headers)


    def test_task_profiles_and_local_provider_are_persisted(self):
        profiles = {
            "quick": {"provider": "groq"},
            "deep_diagnosis": {"provider": "anthropic"},
            "voice": {"provider": "mistral"},
            "local_private": {"provider": "ollama"},
        }
        ai_assist.save_ai_settings(
            "mistral", {"mistral": "mistral-key"},
            {"mistral": "mistral-small-latest", "ollama": "qwen2.5:7b"},
            task_profiles=profiles, auto_escalation=True,
        )
        settings = ai_assist.get_ai_settings()
        self.assertEqual(settings["task_profiles"]["local_private"]["provider"], "ollama")
        self.assertTrue(settings["auto_escalation"])
        self.assertEqual(settings["models"]["ollama"], "qwen2.5:7b")

    def test_ollama_does_not_require_an_api_key(self):
        result, error = ai_assist._call_provider("ollama", "", "", "hello")
        self.assertIsNone(result)
        self.assertIn("No model selected", error)


    def test_provider_wrapper_records_only_opt_in_aggregates(self):
        self.settings_store["ai"]["usage_telemetry_enabled"] = True
        self.settings_store["ai"]["task_profiles"] = {
            "quick": {"provider": "groq"}
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("db.DB_PATH", Path(temp_dir) / "usage.db"):
                with patch("ai_assist._call_provider_impl", return_value=("answer", None)):
                    with patch("ai_assist.time.monotonic", side_effect=[1.0, 1.25]):
                        answer, error = ai_assist._call_provider("groq", "secret-key", "model", "private prompt")
                self.assertEqual((answer, error), ("answer", None))
                rows = ai_assist.ai_intelligence.UsageLedger(str(Path(temp_dir) / "usage.db")).summary()
                self.assertEqual(rows[0]["provider"], "groq")
                self.assertEqual(rows[0]["profile"], "quick")
                self.assertEqual(rows[0]["calls"], 1)
                self.assertEqual(rows[0]["average_latency_ms"], 250)
                self.assertNotIn("private prompt", str(rows))
                self.assertNotIn("secret-key", str(rows))

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





class SQLiteAICustomModelPersistenceTests(unittest.TestCase):
    def test_selected_custom_model_is_persisted_in_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / ".kubedock"
            with (
                patch("db.APP_DIR", app_dir),
                patch("db.DB_PATH", app_dir / "kubedock.db"),
                patch("db.LEGACY_SETTINGS_PATH", app_dir / "settings.json"),
            ):
                db.initialize()
                custom = {
                    "openai": [{
                        "display_name": "OpenAI 120B",
                        "model_id": "provider_model_120b",
                    }]
                }
                ai_assist.save_ai_settings(
                    "openai",
                    {"openai": "openai-test-key"},
                    {"openai": "provider_model_120b"},
                    custom_models=custom,
                )

                # Reload from the SQLite settings table, as a fresh app startup does.
                persisted = db.get_setting("ai")
                self.assertEqual(
                    persisted["custom_models"]["openai"][0]["model_id"],
                    "provider_model_120b",
                )
                self.assertEqual(persisted["models"]["openai"], "provider_model_120b")
                self.assertEqual(ai_assist.get_model("openai"), "provider_model_120b")
                self.assertEqual(ai_assist.get_api_key("openai"), "openai-test-key")


if __name__ == "__main__":
    unittest.main()
