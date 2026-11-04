import unittest

from k8s_navigation import build_navigation_path


class KubernetesNavigationTests(unittest.TestCase):
  def test_formats_context_namespace_and_tab(self):
    self.assertEqual(
      build_navigation_path("prod-eu", "payments", "Pods"),
      "Context: prod-eu  ›  Namespace: payments  ›  Pods",
    )

  def test_adds_selected_resource_when_present(self):
    self.assertEqual(
      build_navigation_path("dev", "default", "Pods", "Pod: api-7d9f"),
      "Context: dev  ›  Namespace: default  ›  Pods  ›  Pod: api-7d9f",
    )

  def test_missing_values_are_explicit(self):
    self.assertEqual(
      build_navigation_path("", "", ""),
      "Context: Not selected  ›  Namespace: Not selected  ›  Resources",
    )

  def test_omits_blank_resource(self):
    self.assertEqual(
      build_navigation_path("dev", "default", "Pods", "  "),
      "Context: dev  ›  Namespace: default  ›  Pods",
    )


if __name__ == "__main__":
  unittest.main()
