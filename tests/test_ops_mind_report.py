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
