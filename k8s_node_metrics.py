"""Pure node inventory and utilization parsing for the Kubernetes UI."""

import json
from typing import NamedTuple, Optional

from k8s_metrics import (
    format_cpu, format_memory, parse_nodes_json, parse_top_nodes_output,
)


NODE_JSON_MARKER = "__KD_NODE_JSON__"
NODE_TOP_MARKER = "__KD_NODE_TOP__"


def build_node_inventory_command(context_flag=""):
    """Fetch node readiness/capacity and live usage in one bounded request."""
    base = " ".join(part for part in ("kubectl", context_flag) if part)
    timeout = "--request-timeout=15s"
    return "\n".join((
        f"echo {NODE_JSON_MARKER}",
        f"{base} get nodes {timeout} -o json 2>&1",
        f"echo {NODE_TOP_MARKER}",
        f"{base} top nodes --no-headers {timeout} 2>&1",
    ))


def split_node_inventory_output(output):
    sections = {"json": [], "top": []}
    current = "json"
    markers = {NODE_JSON_MARKER: "json", NODE_TOP_MARKER: "top"}
    for line in (output or "").splitlines():
        marker = line.strip()
        if marker in markers:
            current = markers[marker]
        else:
            sections[current].append(line)
    return "\n".join(sections["json"]), "\n".join(sections["top"])

class NodeSnapshot(NamedTuple):
    name: str
    status: str
    tone: str
    roles: str
    version: str
    cpu_usage: str
    cpu_percent: Optional[int]
    memory_usage: str
    memory_percent: Optional[int]
    cpu_capacity: str
    memory_capacity: str
    tooltip: str


def _percent(usage, denominator):
    if usage is None or denominator is None or denominator <= 0:
        return None
    return max(0, int((usage * 100.0 / denominator) + 0.5))


def parse_node_inventory(nodes_json, top_nodes_text=""):
    """Join node readiness/capacity with kubectl top data by node name.

    Missing metrics/capacity stay unavailable; they are never represented as
    zero. Node pressure and schedulability affect health independently of
    resource utilization.
    """
    try:
        payload = json.loads(nodes_json or "")
    except (TypeError, ValueError):
        return []

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []

    usage, _problem = parse_top_nodes_output(top_nodes_text)
    resources = parse_nodes_json(nodes_json)
    result = []

    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "").strip()
        if not name:
            continue

        status_data = item.get("status") or {}
        conditions = {
            str(condition.get("type") or ""): str(condition.get("status") or "")
            for condition in (status_data.get("conditions") or [])
            if isinstance(condition, dict)
        }
        ready = conditions.get("Ready")
        pressure = [
            key for key in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
            if conditions.get(key) == "True"
        ]
        spec = item.get("spec") or {}
        unschedulable = bool(spec.get("unschedulable", False))

        if ready == "False":
            tone, label = "critical", "NotReady"
        elif ready != "True":
            tone, label = "unknown", "Unknown"
        elif pressure:
            tone, label = "warning", "Pressure"
        elif unschedulable:
            tone, label = "warning", "Unschedulable"
        else:
            tone, label = "healthy", "Ready"

        labels = metadata.get("labels") or {}
        roles = sorted(
            key.split("/", 1)[1] for key, value in labels.items()
            if key.startswith("node-role.kubernetes.io/") and value is not None
        )
        role_text = ", ".join(roles) if roles else "worker"
        version = str((status_data.get("nodeInfo") or {}).get("kubeletVersion") or "N/A")

        node_usage = usage.get(name)
        node_resource = resources.get(name)
        cpu_denominator = node_resource.cpu_millicores if node_resource else None
        memory_denominator = node_resource.memory_bytes if node_resource else None
        cpu_percent = _percent(node_usage.cpu_millicores, cpu_denominator) if node_usage else None
        memory_percent = _percent(node_usage.memory_bytes, memory_denominator) if node_usage else None

        cpu_text = format_cpu(node_usage.cpu_millicores) if node_usage else "N/A"
        memory_text = format_memory(node_usage.memory_bytes) if node_usage else "N/A"
        cpu_capacity = format_cpu(cpu_denominator) if cpu_denominator is not None else "N/A"
        memory_capacity = format_memory(memory_denominator) if memory_denominator is not None else "N/A"

        details = [f"Node: {name}", f"Readiness: {label}", f"Roles: {role_text}"]
        if pressure:
            details.append("Pressure conditions: " + ", ".join(pressure))
        if unschedulable:
            details.append("Scheduling: disabled")
        details.extend([
            f"CPU: {cpu_text} / {cpu_capacity} ({cpu_percent if cpu_percent is not None else 'N/A'}%)",
            f"Memory: {memory_text} / {memory_capacity} ({memory_percent if memory_percent is not None else 'N/A'}%)",
        ])
        result.append(NodeSnapshot(
            name, label, tone, role_text, version,
            cpu_text, cpu_percent, memory_text, memory_percent,
            cpu_capacity, memory_capacity, "\n".join(details),
        ))

    return sorted(result, key=lambda node: node.name.casefold())
