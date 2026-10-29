import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "installer" / "release_version.py"
SPEC = importlib.util.spec_from_file_location("kubedock_release_version", VERSION_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_version)


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release_starts_at_v1_0_0(self):
        self.assertEqual(release_version.next_version(["fast-installer", "fast-installer-ab123-987"]), "v1.0.0")

    def test_patch_versions_increment_sequentially(self):
        tags = ["v1.0.0", "v1.0.1", "fast-installer-random-123"]
        self.assertEqual(release_version.next_version(tags), "v1.0.2")

    def test_minor_and_major_bumps_reset_lower_components(self):
        self.assertEqual(release_version.next_version(["v1.4.7"], "minor"), "v1.5.0")
        self.assertEqual(release_version.next_version(["v1.4.7"], "major"), "v2.0.0")

    def test_tags_are_parsed_strictly(self):
        self.assertEqual(release_version.parse_version_tag("v2.10.3"), (2, 10, 3))
        for tag in ("fast-installer-v2.10.3-123", "v1.0", "v1.0.0-rc.1", "vv1.0.0", "v01.2.3"):
            with self.subTest(tag=tag):
                self.assertIsNone(release_version.parse_version_tag(tag))

    def test_invalid_bump_is_rejected(self):
        with self.assertRaises(ValueError):
            release_version.next_version(["v1.0.0"], "random")


if __name__ == "__main__":
    unittest.main()
