import unittest
from k8s_metrics import (
    PodLimits, Usage, build_pod_metrics, build_pods_command,
    parse_cpu_millicores, parse_limits_output, parse_memory_bytes,
    parse_nodes_json, parse_top_nodes_output, parse_top_output,
    refresh_result_is_current, usage_level,
)

class PodMetricFallbackTests(unittest.TestCase):
    def test_missing_limits_use_node_allocatable(self):
        nodes = parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{"cpu":"4","memory":"8Gi"},"capacity":{"cpu":"8","memory":"16Gi"}}}]}')
        result = build_pod_metrics(("ns","p"), {("ns","p"):Usage(100,1024**3)}, {("ns","p"):PodLimits(None,None)}, node_name="n", node_resources=nodes)
        self.assertEqual(result.cpu.percent, 3)
        self.assertEqual(result.memory.percent, 13)
        self.assertIn("Node allocatable", result.cpu.tooltip)

    def test_duplicate_pod_names_use_namespace_key(self):
        usage, _ = parse_top_output("a same 100m 10Mi\nb same 200m 20Mi")
        self.assertEqual(usage[("a","same")].cpu_millicores, 100)
        self.assertEqual(usage[("b","same")].cpu_millicores, 200)

    def test_shared_queries_appear_once(self):
        cmd = build_pods_command("--all-namespaces", "--context=test")
        self.assertEqual(cmd.count("top pods -A"), 1)
        self.assertEqual(cmd.count("top nodes --no-headers"), 1)
        self.assertEqual(cmd.count("get nodes "), 1)

    def test_color_thresholds(self):
        self.assertEqual(usage_level(59), "low")
        self.assertEqual(usage_level(60), "moderate")
        self.assertEqual(usage_level(79), "moderate")
        self.assertEqual(usage_level(80), "high")
        self.assertEqual(usage_level(140), "high")


    def test_limit_and_node_fallback_are_independent(self):
        nodes = parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{"cpu":"4","memory":"8Gi"},"capacity":{"cpu":"8","memory":"16Gi"}}}]}')
        result = build_pod_metrics(("ns","p"), {("ns","p"):Usage(200,1024**3)}, {("ns","p"):PodLimits(400,None)}, node_name="n", node_resources=nodes)
        self.assertEqual(result.cpu.percent, 50)
        self.assertEqual(result.memory.percent, 13)
        self.assertIn("Pod limit", result.cpu.tooltip)
        self.assertIn("Node allocatable", result.memory.tooltip)

    def test_allocatable_missing_uses_capacity(self):
        nodes = parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{},"capacity":{"cpu":"2","memory":"4Gi"}}}]}')
        result = build_pod_metrics(("ns","p"), {("ns","p"):Usage(100,1024**3)}, {("ns","p"):PodLimits(None,None)}, node_name="n", node_resources=nodes)
        self.assertEqual(result.cpu.percent, 5)
        self.assertIn("Node capacity", result.cpu.tooltip)

    def test_units_are_normalized(self):
        self.assertEqual(parse_cpu_millicores("1"), 1000)
        self.assertEqual(parse_cpu_millicores("250m"), 250)
        self.assertEqual(parse_memory_bytes("1Gi"), 1024**3)
        self.assertEqual(parse_memory_bytes("512Mi"), 512*1024**2)

    def test_partial_container_limits_only_fall_back_for_incomplete_resource(self):
        limits = parse_limits_output("ns|p|500m,128Mi;,256Mi;|")
        result = build_pod_metrics(("ns","p"), {("ns","p"):Usage(200,128*1024**2)}, limits, node_name="n", node_resources=parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{"cpu":"4","memory":"8Gi"},"capacity":{}}}]}'))
        self.assertEqual(result.cpu.percent, 5)
        self.assertEqual(result.memory.percent, 33)

    def test_missing_usage_and_node_are_not_fabricated_as_zero(self):
        missing = build_pod_metrics(("ns","p"), {}, {})
        self.assertFalse(missing.cpu.available)
        result = build_pod_metrics(("ns","p"), {("ns","p"):Usage(100,1024**3)}, {("ns","p"):PodLimits(None,None)}, node_name="-", node_resources={})
        self.assertEqual(result.cpu.value, "100m")
        self.assertIsNone(result.cpu.percent)

    def test_node_usage_is_not_used_as_capacity(self):
        usage, err = parse_top_nodes_output("n 250m 5% 1Gi 12%")
        self.assertFalse(err)
        self.assertEqual(usage["n"].cpu_millicores, 250)
        capacity = parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{"cpu":"4","memory":"8Gi"},"capacity":{}}}]}')
        self.assertEqual(capacity["n"].cpu_millicores, 4000)

    def test_stale_refresh_generation_is_rejected(self):
        key = ("context","default")
        self.assertTrue(refresh_result_is_current(3,3,key,key))
        self.assertFalse(refresh_result_is_current(2,3,key,key))


    def test_explicit_zero_limit_is_not_treated_as_missing(self):
        result = build_pod_metrics(
            ("ns","p"), {("ns","p"):Usage(100,1024**3)},
            {("ns","p"):PodLimits(0,None)}, node_name="n",
            node_resources=parse_nodes_json('{"items":[{"metadata":{"name":"n"},"status":{"allocatable":{"cpu":"4","memory":"8Gi"},"capacity":{}}}]}'))
        self.assertIsNone(result.cpu.percent)
        self.assertIn("configured pod limit is zero", result.cpu.tooltip)

if __name__ == "__main__":
    unittest.main()
