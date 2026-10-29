import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "installer" / "build_prebuilt.py"
SPEC = importlib.util.spec_from_file_location("kubedock_build_prebuilt", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class PrebuiltBundleBuilderTests(unittest.TestCase):
    def test_build_arguments_include_core_runtime_and_static_ui(self):
        args = builder.build_args(include_qwen=False)
        joined = " ".join(args)
        self.assertIn("--collect-all=paramiko", joined)
        self.assertIn("--hidden-import=webapp.server", joined)
        self.assertIn("--collect-submodules=k8s_ai_ops_parts", joined)
        self.assertIn("assets", joined)
        self.assertIn("webapp/static", joined)
        self.assertNotIn("--collect-all=torch", joined)
        self.assertNotIn("--collect-all=qwen_tts", joined)

    def test_runtime_requirements_keep_build_and_webengine_optional(self):
        core = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        build = (ROOT / "requirements-build.txt").read_text(encoding="utf-8").lower()
        editor = (ROOT / "requirements-editor.txt").read_text(encoding="utf-8").lower()
        voice = (ROOT / "requirements-voice.txt").read_text(encoding="utf-8").lower()

        self.assertNotIn("pyinstaller", core)
        self.assertNotIn("pyqtwebengine", core)
        self.assertNotIn("python-dotenv", core)
        self.assertNotIn("uvicorn[standard]", core)
        self.assertIn("websockets", core)
        self.assertIn("pyinstaller", build)
        self.assertIn("pyqtwebengine", editor)
        self.assertIn("pyobjc-framework-cocoa", voice)
        self.assertNotIn("\npyobjc;", voice)

    def test_qwen_runtime_is_opt_in(self):
        args = builder.build_args(include_qwen=True)
        self.assertIn("--collect-all=torch", args)
        self.assertIn("--collect-all=qwen_tts", args)
        self.assertIn("--collect-all=transformers", args)

    def test_metadata_records_source_revision_and_optional_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "KubeDock"
            bundle.mkdir()
            with patch.dict(os.environ, {"GITHUB_SHA": "abc123", "GITHUB_REF_NAME": "main"}):
                builder.write_build_metadata(bundle, include_qwen=False)
            metadata = json.loads((bundle / "KubeDock-BUILD-METADATA.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["source_sha"], "abc123")
        self.assertEqual(metadata["source_branch"], "main")
        self.assertFalse(metadata["qwen_tts"])
        self.assertIn("monaco_web_editor", metadata)
        self.assertIsInstance(metadata["monaco_web_editor"], bool)
        self.assertEqual(metadata["bundle_format"], "onedir")


if __name__ == "__main__":
    unittest.main()
