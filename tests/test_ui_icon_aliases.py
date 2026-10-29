from pathlib import Path
import unittest

from ui_icons import ICON_ALIASES, LABEL_TO_ICON, icon_path


class IconAliasTests(unittest.TestCase):
    def test_aliases_resolve_to_existing_canonical_svg(self):
        for alias, canonical in ICON_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(icon_path(alias), icon_path(canonical))
                self.assertTrue(Path(icon_path(alias)).is_file())

    def test_all_semantic_labels_resolve_to_existing_svg(self):
        for label, icon_name in LABEL_TO_ICON.items():
            with self.subTest(label=label, icon_name=icon_name):
                self.assertTrue(
                    Path(icon_path(icon_name)).is_file(),
                    f"Missing icon for semantic label {label!r}: {icon_name!r}",
                )


if __name__ == "__main__":
    unittest.main()
