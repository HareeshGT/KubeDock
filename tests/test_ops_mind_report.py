import unittest

from ops_mind_report import render_investigation_report


class OpsMindReportRenderingTests(unittest.TestCase):
    def test_report_renders_headings_tables_lists_and_inline_formatting(self):
        tick = chr(96)
        markdown = f"""## Investigation summary
The **worker** is using {tick}4.6 GiB{tick} against a {tick}5 GiB{tick} limit.

## Current resource utilization
| Metric | Current | Limit |
|---|---:|---:|
| **Memory** | 4.6 GiB | 5 GiB |
| CPU | 96m | 1 core |

## Confirmed findings
- Container usage is currently high.
- No OOM event was observed.

## Recommended actions
1. Review the scanner workload.
2. Compare daily memory averages.
"""
        html = render_investigation_report(
            markdown,
            target="pod/cowrustfs-123",
            context="dev-cc · cn-dev-aks",
        )
        self.assertIn("Investigation report", html)
        self.assertIn("pod/cowrustfs-123", html)
        self.assertIn("<strong>worker</strong>", html)
        self.assertIn("<code", html)
        self.assertIn("<table", html)
        self.assertIn("<th", html)
        self.assertIn("<ul", html)
        self.assertIn("<ol", html)
        self.assertNotIn("| Current |", html)
        self.assertNotIn("**worker**", html)

    def test_placeholder_one_column_tables_are_removed(self):
        malformed = """## Risk assessment
| &#x20; |
| ------ |
**Risk**
| &#x20; |
| ------ |
**Severity**
| &#x20; |
| ------ |
**Confidence**
"""
        html = render_investigation_report(
            malformed,
            target="pod/api-1",
            context="test",
        )
        self.assertNotIn("&#x20;", html)
        self.assertNotIn("| ------ |", html)
        self.assertNotIn("<table", html)
        self.assertIn("<strong>Risk</strong>", html)

    def test_utilization_table_is_built_from_collected_evidence(self):
        evidence = {
            "utilization": {
                "containers": [{
                    "container": "rustfs",
                    "memory": "4719Mi",
                    "request_memory": "3Gi",
                    "limit_memory": "5Gi",
                    "memory_request_utilization_percent": 153.6,
                    "memory_limit_utilization_percent": 92.2,
                    "cpu": "151m",
                    "request_cpu": "1000m",
                    "limit_cpu": "3000m",
                    "cpu_request_utilization_percent": 15.1,
                    "cpu_limit_utilization_percent": 5.0,
                }]
            }
        }
        html = render_investigation_report(
            "## Investigation summary\\nThe memory use is high.",
            target="pod/cowrustfs",
            context="dev-cc",
            evidence=evidence,
        )
        for value in (
            "4719Mi", "3Gi", "5Gi", "153.6%", "92.2%",
            "151m", "1000m", "3000m", "15.1%", "5.0%",
        ):
            self.assertIn(value, html)
        self.assertIn("A request is not a limit", html)

    def test_ai_timeout_keeps_the_evidence_card_visible(self):
        evidence = {
            "utilization": {
                "containers": [{
                    "container": "rustfs",
                    "memory": "4719Mi",
                    "request_memory": "3Gi",
                    "limit_memory": "5Gi",
                    "memory_request_utilization_percent": 153.6,
                    "memory_limit_utilization_percent": 92.2,
                    "cpu": "151m",
                    "request_cpu": "1000m",
                    "limit_cpu": "3000m",
                    "cpu_request_utilization_percent": 15.1,
                    "cpu_limit_utilization_percent": 5.0,
                }]
            }
        }
        html = render_investigation_report(
            "",
            target="pod/cowrustfs",
            context="dev-cc",
            evidence=evidence,
            analysis_error="AI provider request timed out.",
        )
        self.assertIn("AI analysis did not complete", html)
        self.assertIn("AI provider request timed out", html)
        self.assertIn("4719Mi", html)
        self.assertIn("92.2%", html)

    def test_model_authored_html_and_metadata_are_escaped(self):
        html = render_investigation_report(
            "## Findings\n<script>alert(1)</script> and **safe text**",
            target='pod/x"><img src=x onerror=alert(1)>',
            context="<script>ctx</script>",
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertIn("<strong>safe text</strong>", html)


if __name__ == "__main__":
    unittest.main()
