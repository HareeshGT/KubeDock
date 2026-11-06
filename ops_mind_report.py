"""Theme-aware safe HTML rendering for Ops Mind investigation reports."""
from __future__ import annotations

import re
from html import escape, unescape

from themes import T

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_UNORDERED = re.compile(r"^\s*[-*+]\s+(.+)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_SINGLE_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*\|?\s*$")
_CODE = re.compile(r"\x60([^\x60\n]+)\x60")


def _inline(value: str) -> str:
    text = escape(str(value or ""), quote=True)
    codes = []

    def keep_code(match):
        codes.append(match.group(1))
        return f"@@CODE{len(codes) - 1}@@"

    text = _CODE.sub(keep_code, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    for i, raw in enumerate(codes):
        text = text.replace(
            f"@@CODE{i}@@",
            f"<code style='background:{T['BG_ITEM']};color:{T['ACCENT2']};"
            "padding:2px 5px;border-radius:4px;font-family:Menlo,Consolas,monospace'>"
            + escape(raw, quote=True) + "</code>",
        )
    return text


def _cells(line: str) -> list[str]:
    value = line.strip().strip("|")
    return [cell.strip() for cell in value.split("|")]


def _is_empty_placeholder(value: str) -> bool:
    """Recognize blank cells emitted by broken model-generated tables."""
    normalized = unescape(str(value or "")).replace("\\u00a0", " ").strip().lower()
    return normalized in {"", "&#x20;", "&nbsp;", "&#160;", "—", "-"}


def _table(rows: list[str]) -> str:
    parsed = [_cells(row) for row in rows]
    if not parsed:
        return ""
    width = max(len(row) for row in parsed)
    head = parsed[0] + [""] * (width - len(parsed[0]))
    html = [
        f"<table style='width:100%;table-layout:fixed;border-collapse:collapse;"
        f"margin:8px 0 13px;border:1px solid {T['BORDER']};font-size:12px'>",
        "<thead><tr>",
    ]
    for cell in head:
        html.append(
            f"<th style='text-align:left;padding:9px 10px;background:{T['BG_ITEM']};"
            f"color:{T['TEXT_PRIMARY']};border-bottom:1px solid {T['BORDER']}'>"
            f"{_inline(cell)}</th>"
        )
    html.append("</tr></thead><tbody>")
    for i, row in enumerate(parsed[1:]):
        row += [""] * (width - len(row))
        bg = T["BG_PANEL"] if i % 2 == 0 else T["BG_DARK"]
        html.append("<tr>")
        for cell in row:
            html.append(
                f"<td style='vertical-align:top;padding:8px 10px;background:{bg};"
                f"color:{T['TEXT_DIM']};border-bottom:1px solid {T['BORDER']};"
                "white-space:normal;word-wrap:break-word'>"
                f"{_inline(cell)}</td>"
            )
        html.append("</tr>")
    html.append("</tbody></table>")
    return "".join(html)


def _render_blocks(markdown: str) -> str:
    lines = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output = []
    paragraph = []
    items = []
    list_tag = ""

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            text = " ".join(part.strip() for part in paragraph).strip()
            if text:
                output.append(
                    f"<p style='margin:5px 0 9px;line-height:1.58;color:{T['TEXT_DIM']};"
                    f"font-size:13px'>{_inline(text)}</p>"
                )
            paragraph = []

    def flush_list():
        nonlocal items, list_tag
        if items:
            output.append(
                f"<{list_tag} style='margin:4px 0 12px;padding-left:23px;"
                f"color:{T['TEXT_DIM']};font-size:13px;line-height:1.55'>"
            )
            for item in items:
                output.append(f"<li style='padding:2px 0 4px 2px'>{_inline(item)}</li>")
            output.append(f"</{list_tag}>")
            items = []
            list_tag = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            flush_paragraph()
            flush_list()
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            title = _inline(heading.group(2))
            if level <= 2:
                output.append(
                    f"<div style='margin:17px 0 8px;padding-bottom:7px;"
                    f"border-bottom:1px solid {T['BORDER']};color:{T['ACCENT2']};"
                    "font-size:14px;font-weight:750'>"
                    f"{title}</div>"
                )
            else:
                output.append(
                    f"<div style='margin:12px 0 5px;color:{T['TEXT_PRIMARY']};"
                    f"font-size:13px;font-weight:700'>{title}</div>"
                )
            i += 1
            continue

        if line in {"---", "***", "___"}:
            flush_paragraph()
            flush_list()
            output.append(f"<div style='height:1px;background:{T['BORDER']};margin:9px 0'></div>")
            i += 1
            continue

        # Some model responses contain empty one-column "tables" such as
        # | &#x20; | / | ------ | between every label and value. Drop these
        # presentation artifacts instead of rendering empty table fragments.
        if (
            i + 1 < len(lines)
            and "|" in line
            and _SINGLE_TABLE_SEPARATOR.match(lines[i + 1].strip())
            and len(_cells(line)) == 1
            and _is_empty_placeholder(_cells(line)[0])
        ):
            flush_paragraph()
            flush_list()
            i += 2
            continue

        if _SINGLE_TABLE_SEPARATOR.match(line):
            i += 1
            continue

        if i + 1 < len(lines) and "|" in line and _TABLE_SEPARATOR.match(lines[i + 1].strip()):
            flush_paragraph()
            flush_list()
            rows = [line]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                if _SINGLE_TABLE_SEPARATOR.match(lines[i].strip()):
                    break
                rows.append(lines[i])
                i += 1
            # Ignore a malformed single-column table. Real report tables must
            # have at least two meaningful columns.
            if len(_cells(rows[0])) >= 2:
                output.append(_table(rows))
            continue

        ordered = _ORDERED.match(line)
        unordered = _UNORDERED.match(line)
        if ordered or unordered:
            flush_paragraph()
            tag = "ol" if ordered else "ul"
            if items and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append((ordered or unordered).group(1))
            i += 1
            continue

        flush_list()
        paragraph.append(line)
        i += 1

    flush_paragraph()
    flush_list()
    return "".join(output)


def _render_live_utilization(evidence: dict | None) -> str:
    """Render measured usage and configured requests/limits from local evidence."""
    evidence = evidence if isinstance(evidence, dict) else {}
    utilization = evidence.get("utilization") or {}
    containers = utilization.get("containers") or []
    if not containers:
        return (
            f"<div style='margin:16px 0 8px;padding:12px 14px;background:{T['BG_DARK']};"
            f"border:1px solid {T['BORDER']};border-radius:8px'>"
            f"<div style='font-size:13px;font-weight:700;color:{T['TEXT_PRIMARY']}'>"
            "Live resource utilization</div>"
            f"<p style='margin:7px 0 0;color:{T['TEXT_MUTED']};font-size:12px'>"
            "Container metrics were not available. This is unknown, not zero usage.</p></div>"
        )

    parts = [
        f"<div style='margin:16px 0 9px;padding:13px 14px;background:{T['BG_DARK']};"
        f"border:1px solid {T['BORDER']};border-radius:9px'>",
        f"<div style='font-size:13px;font-weight:750;color:{T['TEXT_PRIMARY']}'>"
        "Live resource utilization</div>",
        f"<div style='font-size:11px;color:{T['TEXT_MUTED']};margin:4px 0 9px'>"
        "Measured container usage compared with its configured Kubernetes requests and limits.</div>",
    ]
    for container in containers[:20]:
        name = escape(str(container.get("container") or "container"), quote=True)
        parts.append(
            f"<div style='margin:9px 0 5px;font-size:12px;font-weight:700;"
            f"color:{T['ACCENT2']}'>{name}</div>"
        )
        parts.append(
            f"<table style='width:100%;border-collapse:collapse;table-layout:fixed;"
            f"font-size:11px;border:1px solid {T['BORDER']}'>"
            "<thead><tr>"
        )
        headers = ("Metric", "Measured", "Request", "Limit", "% of request", "% of limit")
        for header in headers:
            parts.append(
                f"<th style='text-align:left;padding:7px 6px;background:{T['BG_ITEM']};"
                f"color:{T['TEXT_PRIMARY']};border-bottom:1px solid {T['BORDER']}'>"
                f"{header}</th>"
            )
        parts.append("</tr></thead><tbody>")
        for metric, usage_key in (("Memory", "memory"), ("CPU", "cpu")):
            measured = escape(str(container.get(usage_key) or "Not collected"), quote=True)
            request = escape(str(container.get(f"request_{usage_key}") or "Not configured"), quote=True)
            limit = escape(str(container.get(f"limit_{usage_key}") or "Not configured"), quote=True)
            request_pct = container.get(f"{usage_key}_request_utilization_percent")
            limit_pct = container.get(f"{usage_key}_limit_utilization_percent")
            request_text = f"{request_pct:.1f}%" if isinstance(request_pct, (int, float)) else "—"
            limit_text = f"{limit_pct:.1f}%" if isinstance(limit_pct, (int, float)) else "—"
            parts.append("<tr>")
            for cell in (metric, measured, request, limit, request_text, limit_text):
                color = T["TEXT_PRIMARY"] if cell in {measured, metric} else T["TEXT_DIM"]
                parts.append(
                    f"<td style='padding:7px 6px;vertical-align:top;background:{T['BG_PANEL']};"
                    f"color:{color};border-bottom:1px solid {T['BORDER']};"
                    "white-space:normal;word-wrap:break-word'>"
                    f"{cell}</td>"
                )
            parts.append("</tr>")
        parts.append("</tbody></table>")
    parts.append(
        f"<div style='font-size:10px;line-height:1.45;color:{T['TEXT_MUTED']};margin-top:8px'>"
        "Utilization is measured usage ÷ request or limit. A request is not a limit; "
        "— means no valid comparison could be calculated.</div></div>"
    )
    return "".join(parts)


def _without_model_resource_section(markdown_text: str) -> str:
    """Avoid duplicate AI-authored metric tables; the app renders these from evidence."""
    lines = str(markdown_text or "").splitlines()
    kept = []
    skipping = False
    for line in lines:
        heading = _HEADING.match(line.strip())
        if heading:
            title = heading.group(2).strip().rstrip("#").strip().lower()
            if title == "current resource utilization":
                skipping = True
                continue
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def render_investigation_report(
    markdown_text: str,
    *,
    target: str,
    context: str,
    evidence: dict | None = None,
    analysis_error: str | None = None,
) -> str:
    """Build a theme-aware report card with measured resource data rendered locally."""
    target_html = escape(str(target or "Kubernetes resource"), quote=True)
    context_html = escape(str(context or "Current context"), quote=True)
    body = _render_blocks(_without_model_resource_section(markdown_text))
    live_utilization = _render_live_utilization(evidence)
    error_html = ""
    if analysis_error:
        safe_error = escape(str(analysis_error), quote=True)
        error_html = (
            f"<div style='margin:12px 0 6px;padding:11px 13px;border-radius:8px;"
            f"border:1px solid {T['WARNING']};background:{T['BG_DARK']}'>"
            f"<div style='font-size:12px;font-weight:750;color:{T['WARNING']}'>"
            "AI analysis did not complete</div>"
            f"<div style='font-size:12px;line-height:1.5;color:{T['TEXT_DIM']};margin-top:4px'>"
            f"{safe_error}</div>"
            f"<div style='font-size:11px;color:{T['TEXT_MUTED']};margin-top:5px'>"
            "The Kubernetes evidence above is preserved. You can run the investigation again; "
            "no cluster changes were made.</div></div>"
        )
    return (
        f"<div style='margin:7px 0 15px;font-family:Inter,-apple-system,BlinkMacSystemFont,"
        f"Segoe UI,Arial,sans-serif;color:{T['TEXT_PRIMARY']}'>"
        f"<div style='padding:15px 17px 13px;background:{T['BG_PANEL']};"
        f"border:1px solid {T['BORDER']};border-left:3px solid {T['ACCENT']};"
        "border-radius:10px 10px 0 0'>"
        f"<div style='font-size:10px;font-weight:800;letter-spacing:1.1px;color:{T['ACCENT2']}'>"
        "KUBEDOCK&nbsp;&nbsp;/&nbsp;&nbsp;OPS MIND</div>"
        f"<div style='font-size:19px;font-weight:750;margin:5px 0 9px;color:{T['TEXT_PRIMARY']}'>"
        "Investigation report</div>"
        f"<div style='font-size:12px;line-height:1.5;color:{T['TEXT_DIM']}'>"
        f"<b style='color:{T['TEXT_PRIMARY']}'>{target_html}</b>"
        f"<span style='color:{T['TEXT_MUTED']}'> &nbsp;·&nbsp; {context_html}</span></div>"
        f"<div style='margin-top:11px'>"
        f"<span style='padding:4px 8px;border-radius:20px;background:{T['BG_ITEM']};"
        f"color:{T['SUCCESS']};font-size:10px;font-weight:700'>● READ-ONLY ANALYSIS</span>"
        f"<span style='margin-left:6px;padding:4px 8px;border-radius:20px;"
        f"background:{T['BG_ITEM']};color:{T['TEXT_MUTED']};font-size:10px;font-weight:700'>"
        "EVIDENCE-BASED</span></div></div>"
        f"<div style='padding:3px 16px 14px;background:{T['BG_PANEL']};"
        f"border:1px solid {T['BORDER']};border-top:0;border-radius:0 0 10px 10px'>"
        f"{live_utilization}{error_html}{body}</div></div>"
    )


def render_ops_mind_markdown(markdown: str) -> str:
    """Render a model-authored Ops Mind reply as safe, readable theme-aware HTML."""
    return _render_blocks(markdown)


__all__ = ["render_investigation_report", "render_ops_mind_markdown"]
