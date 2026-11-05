import importlib.util
import unittest
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "installer" / "local_model_catalog.py"
SPEC = importlib.util.spec_from_file_location("local_model_catalog_test", MODULE_PATH)
assert SPEC and SPEC.loader
catalog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


class HardwareAwareModelCatalogTests(unittest.TestCase):
    def hardware(self, ram=16, available=12, cores=8, disk=20, gpu="Not detected", vram=0):
        return catalog.Hardware("Linux", "x86_64", "test CPU", cores, ram, available, gpu, vram, disk)

    def test_catalog_models_have_pinned_https_downloads_and_requirements(self):
        self.assertGreaterEqual(len(catalog.MODELS), 2)
        for model in catalog.MODELS:
            self.assertTrue(model.url.startswith("https://"))
            self.assertEqual(len(model.sha256), 64)
            self.assertTrue(all(ch in "0123456789abcdef" for ch in model.sha256))
            self.assertGreater(model.min_ram_gb, 0)
            self.assertGreater(model.min_cpu_cores, 0)
            self.assertGreater(model.min_disk_gb, 0)

    def test_low_memory_hardware_recommends_lightweight_model(self):
        model = catalog.recommended_model(self.hardware(ram=4, available=2, cores=2, disk=3))
        self.assertEqual(model.id, "smollm2-360m-q4")

    def test_higher_memory_hardware_recommends_qwen(self):
        model = catalog.recommended_model(self.hardware(ram=16, available=8, cores=8, disk=10))
        self.assertEqual(model.id, "qwen3-06b-q4km")

    def test_insufficient_cpu_or_disk_excludes_qwen(self):
        models = catalog.compatible_models(self.hardware(ram=16, available=8, cores=2, disk=1))
        self.assertNotIn("qwen3-06b-q4km", {model.id for model in models})

    def test_unavailable_ram_detection_does_not_recommend_larger_model(self):
        model = catalog.recommended_model(self.hardware(ram=16, available=3, cores=8, disk=10))
        self.assertEqual(model.id, "smollm2-360m-q4")


if __name__ == "__main__":
    unittest.main()
