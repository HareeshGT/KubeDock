import importlib.util
import unittest
from pathlib import Path


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

    def test_qwen_runtime_is_opt_in(self):
        args = builder.build_args(include_qwen=True)
        self.assertIn("--collect-all=torch", args)
        self.assertIn("--collect-all=qwen_tts", args)
        self.assertIn("--collect-all=transformers", args)

    def test_metadata_source_can_be_supplied_by_ci(self):
        metadata = {
            "source_sha": "abc123",
            "source_branch": "main",
            "qwen_tts": False,
        }
        self.assertEqual(metadata["source_branch"], "main")
        self.assertFalse(metadata["qwen_tts"])


if __name__ == "__main__":
    unittest.main()
