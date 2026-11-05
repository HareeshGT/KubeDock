import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from services.embedded_llm import (EmbeddedModelError, MAX_MODEL_BYTES, ModelSpec, verify_model, bundled_model_directory, selected_model_filename, MODEL_SHA256_BY_FILENAME)


class EmbeddedModelSafetyTests(unittest.TestCase):
    def spec(self, data=b"model"):
        return ModelSpec("test", "model.gguf", "https://example.test/model.gguf",
                         len(data), hashlib.sha256(data).hexdigest())

    def test_bundled_model_directory_is_inside_app_for_frozen_windows_linux(self):
        with patch("services.embedded_llm.sys.frozen", True, create=True), \
             patch("services.embedded_llm.sys.platform", "linux"), \
             patch("services.embedded_llm.sys.executable", "/opt/KubeDock/KubeDock"):
            self.assertEqual(bundled_model_directory(), Path("/opt/KubeDock/models"))

    def test_bundled_model_directory_uses_macos_resources(self):
        with patch("services.embedded_llm.sys.frozen", True, create=True), \
             patch("services.embedded_llm.sys.platform", "darwin"), \
             patch("services.embedded_llm.sys.executable", "/Applications/KubeDock.app/Contents/MacOS/KubeDock"):
            self.assertEqual(bundled_model_directory(), Path("/Applications/KubeDock.app/Contents/Resources/models"))


    def test_selected_model_manifest_accepts_only_catalogued_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "selected_model.json").write_text(
                '{"id":"qwen3-06b-q4km","filename":"Qwen_Qwen3-0.6B-Q4_K_M.gguf"}',
                encoding="utf-8",
            )
            with patch("services.embedded_llm.bundled_model_directory", return_value=root):
                self.assertEqual(selected_model_filename(), "Qwen_Qwen3-0.6B-Q4_K_M.gguf")
            (root / "selected_model.json").write_text(
                '{"filename":"../../unknown.gguf"}', encoding="utf-8"
            )
            with patch("services.embedded_llm.bundled_model_directory", return_value=root):
                self.assertEqual(selected_model_filename(), "SmolLM2-360M-Instruct-Q4_K_M.gguf")

    def test_qwen_model_is_pinned_in_runtime_catalog(self):
        self.assertEqual(
            MODEL_SHA256_BY_FILENAME["Qwen_Qwen3-0.6B-Q4_K_M.gguf"],
            "9acfc1e001311f34b4252001b626f2e466d592a42065f66571bff3790d4e1b14",
        )


    def test_requires_https(self):
        with self.assertRaises(EmbeddedModelError):
            ModelSpec("x", "x.gguf", "http://example.test/x", 1, "0" * 64).validate()

    def test_enforces_500mb_limit(self):
        with self.assertRaises(EmbeddedModelError):
            ModelSpec("x", "x.gguf", "https://example.test/x", MAX_MODEL_BYTES + 1, "0" * 64).validate()

    def test_rejects_path_traversal(self):
        with self.assertRaises(EmbeddedModelError):
            ModelSpec("x", "../x.gguf", "https://example.test/x", 1, "0" * 64).validate()

    def test_exact_size_and_digest(self):
        data = b"model"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "model.gguf"
            path.write_bytes(data)
            self.assertTrue(verify_model(path, self.spec(data)))
            path.write_bytes(b"tampered")
            self.assertFalse(verify_model(path, self.spec(data)))

    def test_rejects_unpinned_digest(self):
        with self.assertRaises(EmbeddedModelError):
            ModelSpec("x", "x.gguf", "https://example.test/x", 1, "bad").validate()


if __name__ == "__main__":
    unittest.main()
