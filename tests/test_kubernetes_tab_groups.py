import unittest

from kubernetes_tab_parts.core import K8S_TAB_GROUPS


class KubernetesTabGroupTests(unittest.TestCase):
    def test_groups_cover_each_resource_tab_once(self):
        groups = {name: titles for name, titles in K8S_TAB_GROUPS.items() if name != "all"}
        expected = {
            "pods", "deployments", "statefulsets", "daemonsets", "hpa",
            "jobs & cronjobs", "services", "ingress", "storage",
            "config & secrets", "events", "terminal", "tunnels", "ops mind",
        }
        memberships = [title for titles in groups.values() for title in titles]
        self.assertEqual(set(memberships), expected)
        self.assertEqual(len(memberships), len(set(memberships)))

    def test_all_sections_does_not_filter_tabs(self):
        self.assertIsNone(K8S_TAB_GROUPS["all"])


if __name__ == "__main__":
    unittest.main()
