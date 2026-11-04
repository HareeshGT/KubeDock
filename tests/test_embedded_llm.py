import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from services.embedded_llm import EmbeddedModelError, MAX_MODEL_BYTES, ModelSpec, verify_model, bundled_model_directory


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
