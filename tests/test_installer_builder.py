import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "installer" / "build_prebuilt.py"
SPEC = importlib.util.spec_from_file_location("kubedock_build_prebuilt", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

WIN_INSTALLER_PATH = ROOT / "installer" / "installer-win.py"
WIN_SPEC = importlib.util.spec_from_file_location("kubedock_windows_installer", WIN_INSTALLER_PATH)
assert WIN_SPEC is not None and WIN_SPEC.loader is not None
win_installer = importlib.util.module_from_spec(WIN_SPEC)
WIN_SPEC.loader.exec_module(win_installer)


class WindowsReleaseAssetURLTests(unittest.TestCase):
    def test_windows_installer_pins_bundle_and_checksum_to_latest_tag(self):
        tag = "fast-installer-f5b9fa8-40904841970"
        bundle_url = win_installer.release_asset_url(tag, "KubeDock-windows-x86_64.zip")
        checksum_url = win_installer.release_asset_url(tag, "SHA256SUMS")

        self.assertEqual(
            bundle_url,
            "https://github.com/HareeshGT/KubeDock/releases/download/"
            "fast-installer-f5b9fa8-40904841970/KubeDock-windows-x86_64.zip",
        )
        self.assertEqual(
            checksum_url,
            "https://github.com/HareeshGT/KubeDock/releases/download/"
            "fast-installer-f5b9fa8-40904841970/SHA256SUMS",
        )
        self.assertNotIn("/download/fast-installer/", bundle_url)

    def test_windows_installer_resolves_latest_published_release(self):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "tag_name": "fast-installer-current-123",
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(win_installer.urllib.request, "urlopen", return_value=response) as urlopen:
            tag = win_installer.get_latest_release_tag()
        self.assertEqual(tag, "fast-installer-current-123")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, win_installer.LATEST_RELEASE_API)

    def test_windows_installer_rejects_release_asset_path_traversal(self):
        with self.assertRaises(ValueError):
            win_installer.release_asset_url("latest", "../KubeDock.zip")
        with self.assertRaises(ValueError):
            win_installer.release_asset_url("latest", "folder\\KubeDock.zip")

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

    def test_build_metadata_records_source_and_optional_features(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"GITHUB_SHA": "test-source-sha", "GITHUB_REF_NAME": "test-branch"},
        ):
            bundle = Path(directory) / "KubeDock"
            bundle.mkdir()
            builder.write_build_metadata(bundle, include_qwen=False)
            metadata_path = bundle / "KubeDock-BUILD-METADATA.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(metadata["application"], "KubeDock")
        self.assertEqual(metadata["source_sha"], "test-source-sha")
        self.assertEqual(metadata["source_branch"], "test-branch")
        self.assertFalse(metadata["qwen_tts"])
        self.assertEqual(metadata["bundle_format"], "onedir")
        self.assertTrue(metadata["platform"])
        self.assertTrue(metadata["architecture"])
        self.assertTrue(metadata["python"])

    def test_macos_openssl_repair_copies_matching_library_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "openssl"
            bundle = root / "KubeDock.app"
            source.mkdir()
            (source / "libssl.3.dylib").write_bytes(b"matching-ssl")
            (source / "libcrypto.3.dylib").write_bytes(b"matching-crypto")
            framework_dir = bundle / "Contents" / "Frameworks"
            framework_dir.mkdir(parents=True)
            (framework_dir / "libssl.3.dylib").write_bytes(b"stale-ssl")
            (framework_dir / "libcrypto.3.dylib").write_bytes(b"stale-crypto")

            builder.repair_macos_openssl(bundle, source_dir=source)

            self.assertEqual((framework_dir / "libssl.3.dylib").read_bytes(), b"matching-ssl")
            self.assertEqual((framework_dir / "libcrypto.3.dylib").read_bytes(), b"matching-crypto")

    def test_macos_openssl_repair_is_noop_for_static_linkage(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(builder.importlib.util, "find_spec", return_value=SimpleNamespace(origin="rust-extension.so")), \
             patch.object(builder.subprocess, "check_output", return_value="rust-extension.so:\n  /usr/lib/libcrypto.3.dylib (compatibility version 3.0.0)") as inspect:
            bundle = Path(directory) / "KubeDock.app"
            builder.repair_macos_openssl(bundle)
            inspect.assert_called_once()
            self.assertFalse((bundle / "Contents" / "Frameworks").exists())

    def test_runtime_requirements_keep_build_and_webengine_optional(self):
        core = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        build = (ROOT / "requirements-build.txt").read_text(encoding="utf-8").lower()
        editor = (ROOT / "requirements-editor.txt").read_text(encoding="utf-8").lower()
        voice = (ROOT / "requirements-voice.txt").read_text(encoding="utf-8").lower()

        core_requirements = [
            line.strip() for line in core.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any(line.startswith("pyinstaller") for line in core_requirements))
        self.assertFalse(any(line.startswith("pyqtwebengine") for line in core_requirements))
        self.assertFalse(any(line.startswith("python-dotenv") for line in core_requirements))
        self.assertFalse(any(line.startswith("uvicorn[standard]") for line in core_requirements))
        self.assertTrue(any(line.startswith("websockets") for line in core_requirements))
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
