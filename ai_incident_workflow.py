"""Safe building blocks for KubeDock's end-to-end AI incident workflow.

All collectors are read-only, accept a strict resource allowlist, and avoid
retrieving Secret/ConfigMap values. Runbook documents are local-only until the
user explicitly starts an AI investigation.
"""
from __future__ import annotations

import ipaddress
import math
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit


ALLOWED_INCIDENT_RESOURCES = frozenset({
    "pod", "deployment", "statefulset", "daemonset", "replicaset",
    "service", "ingress", "job", "cronjob",
})
MAX_RUNBOOK_FILES = 100
MAX_RUNBOOK_FILE_BYTES = 256 * 1024
MAX_RUNBOOK_TOTAL_BYTES = 5 * 1024 * 1024
MAX_RUNBOOK_CONTEXT_CHARS = 8_000
_ALLOWED_RUNBOOK_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})


def validate_incident_target(context: str, namespace: str, resource_kind: str, resource_name: str) -> dict:
    """Validate a user-selected target before constructing a remote read-only command."""
    context = str(context or "").strip()
    namespace = str(namespace or "").strip()
    kind = str(resource_kind or "").strip().lower()
    name = str(resource_name or "").strip()
    if kind not in ALLOWED_INCIDENT_RESOURCES:
        raise ValueError("Choose a supported non-secret Kubernetes resource type.")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", namespace) or len(namespace) > 253:
        raise ValueError("Enter a valid Kubernetes namespace.")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", name) or len(name) > 253:
        raise ValueError("Enter a valid Kubernetes resource name.")
    if context and (
        len(context) > 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}", context)
    ):
        raise ValueError("Kubernetes context contains unsupported characters.")
    return {"context": context, "namespace": namespace, "resource_kind": kind, "resource_name": name}


def build_live_incident_command(context: str, namespace: str, resource_kind: str, resource_name: str) -> str:
    """Build a bounded, read-only command. Never fetch Secret values or full object JSON."""
    import shlex

    target = validate_incident_target(context, namespace, resource_kind, resource_name)
    context_flag = ("--context " + shlex.quote(target["context"]) + " ") if target["context"] else ""
    ns = shlex.quote(target["namespace"])
    kind = shlex.quote(target["resource_kind"])
    name = shlex.quote(target["resource_name"])
    base = "kubectl " + context_flag + "-n " + ns
    if target["resource_kind"] == "pod":
        summary = (
            "Name={.metadata.name}{\"\\n\"}Phase={.status.phase}{\"\\n\"}"
            "Node={.spec.nodeName}{\"\\n\"}StartTime={.status.startTime}{\"\\n\"}"
            "ContainerNames={.spec.containers[*].name}{\"\\n\"}"
            "Images={.spec.containers[*].image}{\"\\n\"}"
            "ContainerStates={.status.containerStatuses[*].state}{\"\\n\"}"
            "RestartCounts={.status.containerStatuses[*].restartCount}{\"\\n\"}"
            "Ready={.status.containerStatuses[*].ready}{\"\\n\"}"
            "Conditions={.status.conditions[*].type}:{.status.conditions[*].status}:{.status.conditions[*].reason}"
        )
    elif target["resource_kind"] in {"deployment", "statefulset", "daemonset", "replicaset"}:
        summary = (
            "Name={.metadata.name}{\"\\n\"}Desired={.spec.replicas}{\"\\n\"}"
            "Ready={.status.readyReplicas}{\"\\n\"}Available={.status.availableReplicas}{\"\\n\"}"
            "Updated={.status.updatedReplicas}{\"\\n\"}Images={.spec.template.spec.containers[*].image}{\"\\n\"}"
            "Conditions={.status.conditions[*].type}:{.status.conditions[*].status}:{.status.conditions[*].reason}"
        )
    elif target["resource_kind"] == "service":
        summary = (
            "Name={.metadata.name}{\"\\n\"}Type={.spec.type}{\"\\n\"}"
            "ClusterIP={.spec.clusterIP}{\"\\n\"}Ports={.spec.ports[*].port}:{.spec.ports[*].targetPort}"
            "{\"\\n\"}Selector={.spec.selector}"
        )
    else:
        summary = (
            "Name={.metadata.name}{\"\\n\"}Created={.metadata.creationTimestamp}{\"\\n\"}"
            "Conditions={.status.conditions[*].type}:{.status.conditions[*].status}:{.status.conditions[*].reason}"
        )
    commands = [
        "printf '%s\\n' '__KUBEDOCK_RESOURCE__'",
        base + " get " + kind + " " + name + " -o jsonpath=" + shlex.quote(summary) + " 2>&1",
        "printf '\\n%s\\n' '__KUBEDOCK_TARGET_EVENTS__'",
        base + " get events --field-selector " + shlex.quote("involvedObject.name=" + target["resource_name"])
        + " --sort-by=.lastTimestamp -o custom-columns=TIME:.lastTimestamp,TYPE:.type,REASON:.reason,MESSAGE:.message --no-headers 2>&1 | tail -n 30",
        "printf '\\n%s\\n' '__KUBEDOCK_NAMESPACE_WARNINGS__'",
        base + " get events --field-selector type=Warning --sort-by=.lastTimestamp "
        "-o custom-columns=TIME:.lastTimestamp,OBJECT:.involvedObject.name,REASON:.reason,MESSAGE:.message "
        "--no-headers 2>&1 | tail -n 30",
    ]
    if target["resource_kind"] == "pod":
        commands.extend([
            "printf '\\n%s\\n' '__KUBEDOCK_LOGS__'",
            base + " logs " + name + " --all-containers=true --prefix=true --tail=100 2>&1",
            "printf '\\n%s\\n' '__KUBEDOCK_METRICS__'",
            base + " top pod " + name + " --containers 2>&1",
        ])
    elif target["resource_kind"] == "service":
        commands.extend([
            "printf '\\n%s\\n' '__KUBEDOCK_ENDPOINTS__'",
            base + " get endpoints " + name + " -o wide 2>&1",
        ])
    # No mutating verbs, secret reads, exec, or shell text from user fields.
    return "bash -lc " + shlex.quote("set +e; " + "; ".join(commands))


def build_cross_system_collection_command(context: str, namespace: str, resource_kind: str, resource_name: str) -> str:
    """Collect low-risk host/Docker inventory plus targeted Kubernetes evidence over SSH."""
    import shlex
    target = validate_incident_target(context, namespace, resource_kind, resource_name)
    incident_command = build_live_incident_command(
        target["context"], target["namespace"], target["resource_kind"], target["resource_name"]
    )
    host_commands = [
        "printf '%s\\n' '__KUBEDOCK_HOST__'",
        "(date -u; uptime; free -m 2>&1; df -h 2>&1 | tail -n 15) 2>&1",
        "printf '\\n%s\\n' '__KUBEDOCK_DOCKER__'",
        "docker ps --no-trunc --format " + shlex.quote("{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}") + " 2>&1 | head -n 40",
        incident_command,
    ]
    return "bash -lc " + shlex.quote("set +e; " + "; ".join(host_commands))


def parse_live_incident_output(output: str, *, max_chars_per_section: int = 12_000) -> dict:
    """Split collector output into named sections and redact every section."""
    try:
        from ai_intelligence import redact_evidence
    except Exception:
        redact_evidence = lambda value: str(value or "")[:max_chars_per_section]
    markers = {
        "__KUBEDOCK_HOST__": "host_summary",
        "__KUBEDOCK_DOCKER__": "docker_inventory",
        "__KUBEDOCK_RESOURCE__": "resource_summary",
        "__KUBEDOCK_TARGET_EVENTS__": "target_events",
        "__KUBEDOCK_NAMESPACE_WARNINGS__": "namespace_warnings",
        "__KUBEDOCK_LOGS__": "logs",
        "__KUBEDOCK_METRICS__": "metrics",
        "__KUBEDOCK_ENDPOINTS__": "endpoints",
    }
    result = {}
    current = None
    chunks = {}
    for line in str(output or "").splitlines():
        key = markers.get(line.strip())
        if key is not None:
            current = key
            chunks.setdefault(current, [])
            continue
        if current is not None:
            chunks[current].append(line)
    for key, lines in chunks.items():
        result[key] = redact_evidence("\n".join(lines))[:max_chars_per_section]
    if "logs" not in result:
        result["logs"] = ""
    result["collection_note"] = (
        "Collected read-only resource summary, bounded event excerpts, and pod logs/metrics where applicable. "
        "Secret and ConfigMap values were not queried; unavailable evidence remains unknown."
    )
    return result


def load_runbook_documents(directory: str | Path) -> list[dict]:
    """Load a bounded set of local Markdown/text runbooks without following symlinks."""
    root = Path(directory).expanduser()
    if not root.exists() or not root.is_dir():
        raise ValueError("Choose an existing local runbook directory.")
    documents = []
    total_bytes = 0
    try:
        candidates = sorted(
            path for path in root.rglob("*")
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in _ALLOWED_RUNBOOK_SUFFIXES
        )
        for path in candidates:
            if len(documents) >= MAX_RUNBOOK_FILES or total_bytes >= MAX_RUNBOOK_TOTAL_BYTES:
                break
            size = path.stat().st_size
            if size <= 0 or size > MAX_RUNBOOK_FILE_BYTES or total_bytes + size > MAX_RUNBOOK_TOTAL_BYTES:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                from ai_intelligence import redact_evidence
                content = redact_evidence(raw)[:MAX_RUNBOOK_FILE_BYTES]
            except Exception:
                content = raw[:MAX_RUNBOOK_FILE_BYTES]
            if not content.strip():
                continue
            documents.append({
                "title": path.stem[:160],
                "filename": path.name[:200],
                "content": content,
                "bytes": size,
            })
            total_bytes += size
    except OSError as exc:
        raise ValueError("Unable to read the selected runbook directory.") from exc
    return documents


_RUNBOOK_STOP_WORDS = frozenset({
    "this", "that", "with", "from", "have", "were", "when", "then", "into", "your",
    "what", "where", "which", "while", "there", "their", "would", "could", "should",
    "please", "kubedock", "kubernetes", "incident", "failure", "error", "logs",
})


def match_runbook_snippets(query: str, documents, *, max_results: int = 4, max_chars: int = 1800) -> list[dict]:
    """Rank local runbook snippets by deterministic term overlap; never invent citations."""
    query_terms = {
        term for term in re.findall(r"[a-zA-Z0-9_.-]{3,}", str(query or "").lower())
        if term not in _RUNBOOK_STOP_WORDS
    }
    if not query_terms:
        return []
    ranked = []
    for doc in list(documents or [])[:MAX_RUNBOOK_FILES]:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or doc.get("filename") or "Runbook")[:160]
        content = str(doc.get("content") or "")
        if not content:
            continue
        title_terms = set(re.findall(r"[a-zA-Z0-9_.-]{3,}", title.lower()))
        content_terms = set(re.findall(r"[a-zA-Z0-9_.-]{3,}", content.lower()))
        score = len(query_terms & content_terms) + 2 * len(query_terms & title_terms)
        if score <= 0:
            continue
        lines = content.splitlines()
        line_scores = []
        for idx, line in enumerate(lines):
            tokens = set(re.findall(r"[a-zA-Z0-9_.-]{3,}", line.lower()))
            line_scores.append((len(tokens & query_terms), idx))
        best_lines = [idx for count, idx in sorted(line_scores, reverse=True)[:5] if count > 0]
        selected = sorted(set(best_lines))
        snippet = "\n".join(lines[idx] for idx in selected) if selected else content[:max_chars]
        try:
            from ai_intelligence import redact_evidence
            snippet = redact_evidence(snippet)
        except Exception:
            pass
        ranked.append({
            "title": title,
            "filename": str(doc.get("filename") or title)[:200],
            "score": score,
            "snippet": snippet[:max_chars],
            "source": "local runbook",
        })
    ranked.sort(key=lambda item: (-item["score"], item["title"].lower()))
    return ranked[:max(1, min(int(max_results), 10))]


def compact_evidence_text(value: str, *, max_chars: int = 12_000, max_lines: int = 240) -> str:
    """Deterministically deduplicate repeated log lines and bound AI context."""
    text = str(value or "")
    lines = []
    seen = set()
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        key = normalized[:1000]
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= max_lines:
            lines.append("[additional evidence lines omitted]")
            break
    result = "\n".join(lines)
    if len(result) > max_chars:
        half = max_chars // 2
        result = result[:half] + "\n[ middle evidence omitted to respect the context budget ]\n" + result[-(max_chars - half - 55):]
    try:
        from ai_intelligence import redact_evidence
        result = redact_evidence(result)
    except Exception:
        result = result[:max_chars]
    return result[:max_chars]


def estimate_prompt_tokens(text: str) -> int:
    """A conservative UI estimate only; this is not a model tokenizer."""
    return max(1, math.ceil(len(str(text or "")) / 4))


def compare_watch_states(previous: str, current: str) -> dict:
    """Return a bounded diff while ignoring volatile AGE values that change every poll."""
    prev_lines = [line.strip() for line in str(previous or "").splitlines() if line.strip()]
    curr_lines = [line.strip() for line in str(current or "").splitlines() if line.strip()]
    age_pattern = re.compile(r"(?<=\\s)(?:\\d+[smhdwy])+(?=\\s|$)", re.I)
    def normalize(line):
        return age_pattern.sub("<AGE>", line)
    normalized_previous = {normalize(line): line for line in prev_lines}
    normalized_current = {normalize(line): line for line in curr_lines}
    added = [line for key, line in normalized_current.items() if key not in normalized_previous]
    removed = [line for key, line in normalized_previous.items() if key not in normalized_current]
    critical_terms = ("crashloopbackoff", "oomkilled", "imagepullbackoff", "errimagepull", "failedscheduling", "ready=false", "unhealthy")
    previous_lower = "\n".join(prev_lines).lower()
    current_lower = "\n".join(curr_lines).lower()
    critical = [term for term in critical_terms if term in current_lower]
    previous_critical = [term for term in critical_terms if term in previous_lower]
    new_critical = [term for term in critical if term not in previous_critical]
    changed = bool(added or removed)
    return {
        "changed": changed,
        "added": added[:30],
        "removed": removed[:30],
        "severity": "high" if new_critical else ("info" if changed else "none"),
        "critical_signals": critical,
        "new_critical_signals": new_critical,
        "message": "Meaningful state changes detected." if changed else "No state change after normalizing volatile AGE values.",
    }


def validate_prometheus_base_url(base_url: str) -> str:
    """Validate a Prometheus endpoint without credentials or unsafe plain-HTTP remote hosts."""
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Prometheus URL must be an http(s) base URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Prometheus URL must not contain credentials, query parameters, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Enter the Prometheus origin only, for example https://metrics.example.com.")
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise ValueError("Plain HTTP is allowed only for localhost. Use HTTPS for a remote Prometheus endpoint.")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def prometheus_query_url(base_url: str, query: str) -> str:
    """Build a Prometheus instant-query URL after validating the origin."""
    from urllib.parse import quote
    base = validate_prometheus_base_url(base_url)
    if not str(query or "").strip() or len(query) > 4000:
        raise ValueError("Prometheus query is empty or too long.")
    return base + "/api/v1/query?" + urlencode({"query": str(query)})


def build_prometheus_history_queries(namespace: str, pod: str, days: int = 7) -> dict:
    """Build fixed PromQL expressions for P95 CPU/memory and data-point counts."""
    namespace = str(namespace or "").strip()
    pod = str(pod or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", namespace) or len(namespace) > 253:
        raise ValueError("Enter a valid Kubernetes namespace.")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", pod) or len(pod) > 253:
        raise ValueError("Enter a valid Kubernetes pod name.")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("Historical window must be between 1 and 30 days.")
    labels = f'namespace="{namespace}",pod="{pod}",container!="",image!=""'
    cpu_rate = f'rate(container_cpu_usage_seconds_total{{{labels}}}[5m])'
    memory = f'container_memory_working_set_bytes{{{labels}}}'
    return {
        "cpu_p95_cores": f"sum(quantile_over_time(0.95, ({cpu_rate})[{days}d:5m]))",
        "memory_p95_bytes": f"sum(quantile_over_time(0.95, {memory}[{days}d]))",
        "cpu_sample_count": f"sum(count_over_time(({cpu_rate})[{days}d:5m]))",
        "memory_sample_count": f"sum(count_over_time({memory}[{days}d]))",
    }


def parse_prometheus_instant_response(payload) -> float:
    """Extract a finite numeric value from a Prometheus instant-query JSON response."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        message = payload.get("error") if isinstance(payload, dict) else None
        raise ValueError("Prometheus query failed" + (": " + str(message)[:300] if message else "."))
    result = (payload.get("data") or {}).get("result")
    if not isinstance(result, list) or not result:
        raise ValueError("Prometheus returned no matching time series for this target/window.")
    values = []
    for series in result[:100]:
        value = series.get("value") if isinstance(series, dict) else None
        if not isinstance(value, list) or len(value) < 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            values.append(number)
    if not values:
        raise ValueError("Prometheus returned no valid numeric samples.")
    return sum(values)


def parse_prometheus_history_results(results: dict) -> dict:
    """Validate all four P95/count responses and normalize them for the right-sizer."""
    required = {"cpu_p95_cores", "memory_p95_bytes", "cpu_sample_count", "memory_sample_count"}
    if not isinstance(results, dict) or not required <= results.keys():
        raise ValueError("Prometheus history response is incomplete.")
    cpu_cores = parse_prometheus_instant_response(results["cpu_p95_cores"])
    memory_bytes = parse_prometheus_instant_response(results["memory_p95_bytes"])
    cpu_count = parse_prometheus_instant_response(results["cpu_sample_count"])
    memory_count = parse_prometheus_instant_response(results["memory_sample_count"])
    samples = int(min(cpu_count, memory_count))
    if samples < 5:
        raise ValueError("Fewer than five historical samples are available. No recommendation was generated.")
    return {
        "cpu_usage_millicores": cpu_cores * 1000.0,
        "memory_usage_bytes": int(memory_bytes),
        "sample_count": samples,
        "source": "Prometheus historical P95",
    }


__all__ = [
    "ALLOWED_INCIDENT_RESOURCES", "validate_incident_target", "build_live_incident_command",
    "build_cross_system_collection_command", "parse_live_incident_output", "load_runbook_documents", "match_runbook_snippets",
    "compact_evidence_text", "estimate_prompt_tokens", "compare_watch_states",
    "validate_prometheus_base_url", "prometheus_query_url", "build_prometheus_history_queries",
    "parse_prometheus_instant_response", "parse_prometheus_history_results",
]
