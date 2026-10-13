"""k8s_metrics.py — CPU/memory usage helpers for the Kubernetes → Pods cards.

Pure Python (no Qt, no SSH) so it can be unit-tested on its own. The Pods
view loads the primary list with build_pod_list_command(), then loads its
supporting metrics/capacity separately with build_pod_metrics_command(). The
older build_pods_command() helper remains available for compatibility.

split_pod_sections() handles the metrics output markers, while
parse_top_output() / parse_limits_output() turn responses into dicts keyed by
(namespace, pod name) — never by name alone, since identical pod names are
legal in different namespaces — and build_pod_metrics() combines them into
the PodMetrics a PodCardWidget renders.

Rules enforced here (see the feature spec):
  * Usage comes only from `kubectl top`; limits only from the container
    specs. Requests are never read, so they can't be mistaken for usage.
  * Missing metrics are "N/A", never 0. A real 0m / 0Mi reading stays 0.
  * Utilization % = pod usage / sum of container limits. If ANY container
    has no limit the % is unavailable (we never invent one) while the
    usage itself is still shown.
  * Thresholds are applied to the whole-number percent that is displayed
    (rounded half up), so the number and its colour can't disagree.
"""

import json
import math
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

TOP_MARKER = "__KD_TOP__"
LIMITS_MARKER = "__KD_LIMITS__"
TOP_NODES_MARKER = "__KD_TOP_NODES__"
NODES_JSON_MARKER = "__KD_NODES_JSON__"

# Utilization colour bands (whole percent): 0–59 low, 60–79 moderate, 80+ high.
MODERATE_FROM = 60
HIGH_FROM = 80

NA = "N/A"

# Per-request upper bound for supporting metrics calls. These run separately
# from the primary pod list, so a hung metrics-server cannot hold up the list.
_REQUEST_TIMEOUT = "15s"

# One container per ";"-terminated segment: "<cpu limit>,<memory limit>".
# Init containers are listed too, as "<restartPolicy>,<cpu>,<memory>" —
# only the restartPolicy=Always ones (native sidecars) run alongside the app
# and therefore show up in `kubectl top`, so only those count toward limits.
_LIMITS_JSONPATH = (
  '{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}'
  '{range .spec.containers[*]}{.resources.limits.cpu}{","}'
  '{.resources.limits.memory}{";"}{end}{"|"}'
  '{range .spec.initContainers[*]}{.restartPolicy}{","}'
  '{.resources.limits.cpu}{","}{.resources.limits.memory}{";"}{end}{"\\n"}{end}'
)


# ── command ──────────────────────────────────────────────────────────

def build_pod_list_command(ns_flag: str, context_flag: str = "") -> str:
  """Build the primary pod-list request independently of optional metrics.

  The caller can render the pod cards as soon as this command completes,
  instead of waiting for metrics-server and node inventory requests.
  """
  base = " ".join(p for p in ("kubectl", context_flag) if p)
  return f"{base} get pods {ns_flag} -o wide 2>&1"


def build_pod_metrics_command(ns_flag: str, context_flag: str = "") -> str:
  """Build the supporting CPU/memory collection as a separate request.

  This may take longer than the primary list (for example when
  metrics-server is slow), so the UI runs it independently and updates card
  indicators after the pod list has already been displayed.
  """
  base = " ".join(p for p in ("kubectl", context_flag) if p)
  timeout = f"--request-timeout={_REQUEST_TIMEOUT}"
  return "\n".join((
    f"echo {TOP_MARKER}",
    f"{base} top pods -A --no-headers {timeout} 2>&1",
    f"echo {LIMITS_MARKER}",
    f"{base} get pods {ns_flag} {timeout} -o jsonpath='{_LIMITS_JSONPATH}' 2>&1",
    # Node usage was fetched by the previous implementation but never used
    # in the pod percentage calculation. Keep an empty section marker for the
    # shared parser, but avoid this redundant API request.
    f"echo {TOP_NODES_MARKER}",
    f"echo {NODES_JSON_MARKER}",
    f"{base} get nodes {timeout} -o json 2>&1",
  ))


def build_pods_command(ns_flag: str, context_flag: str = "") -> str:
  """Build all pod-card data collection into one worker invocation per refresh.

  Pod listing and container limits follow the selected namespace. Pod usage,
  node usage, and node capacity are cluster-wide and each queried once.
  Every kubectl command repeats the selected context because context
  injection patches only the first command in a shell script.
  """
  base = " ".join(p for p in ("kubectl", context_flag) if p)
  timeout = f"--request-timeout={_REQUEST_TIMEOUT}"
  return "\n".join((
    f"{base} get pods {ns_flag} -o wide 2>&1",
    f"echo {TOP_MARKER}",
    f"{base} top pods -A --no-headers {timeout} 2>&1",
    f"echo {LIMITS_MARKER}",
    f"{base} get pods {ns_flag} {timeout} -o jsonpath='{_LIMITS_JSONPATH}' 2>&1",
    f"echo {TOP_NODES_MARKER}",
    f"{base} top nodes --no-headers {timeout} 2>&1",
    f"echo {NODES_JSON_MARKER}",
    f"{base} get nodes {timeout} -o json 2>&1",
  ))

def split_pod_sections(out: str) -> Tuple[str, str, str, str, str]:
  """Return pod list, pod usage, pod limits, node usage, and node JSON text."""
  sections = {"pods": [], "top": [], "limits": [], "top_nodes": [], "nodes": []}
  current = "pods"
  marker_sections = {
    TOP_MARKER: "top",
    LIMITS_MARKER: "limits",
    TOP_NODES_MARKER: "top_nodes",
    NODES_JSON_MARKER: "nodes",
  }
  for line in (out or "").splitlines():
    stripped = line.strip()
    if stripped in marker_sections:
      current = marker_sections[stripped]
    else:
      sections[current].append(line)
  return tuple("\n".join(sections[name]) for name in
               ("pods", "top", "limits", "top_nodes", "nodes"))


# ── Kubernetes quantities ────────────────────────────────────────────

_QTY_RE = re.compile(
  r"^([0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
  r"(?:([eE][+-]?[0-9]+)|(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E))?$"
)
_SUFFIX = {
  "": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9,
  "T": 1e12, "P": 1e15, "E": 1e18,
  "Ki": 2.0 ** 10, "Mi": 2.0 ** 20, "Gi": 2.0 ** 30, "Ti": 2.0 ** 40,
  "Pi": 2.0 ** 50, "Ei": 2.0 ** 60,
}
_KI, _MI, _GI = 2.0 ** 10, 2.0 ** 20, 2.0 ** 30


def parse_quantity(text) -> Optional[float]:
  """A Kubernetes quantity ("500m", "1.5", "256Mi", "129e6") in base units
  (cores / bytes), or None if it is empty or not a valid quantity."""
  m = _QTY_RE.match((text or "").strip())
  if not m:
    return None
  number, exponent, suffix = m.groups()
  try:
    value = float(number)
    if exponent:
      value *= 10.0 ** int(exponent[1:])
    else:
      value *= _SUFFIX[suffix or ""]
  except (ValueError, OverflowError):
    return None
  return value if math.isfinite(value) else None


def parse_cpu_millicores(text) -> Optional[float]:
  cores = parse_quantity(text)
  return None if cores is None else cores * 1000.0


def parse_memory_bytes(text) -> Optional[float]:
  return parse_quantity(text)


def _round_half_up(x: float) -> int:
  # round() is banker's rounding (0.5 -> 0, 2.5 -> 2); thresholds want 0.5 -> 1.
  return int(x + 0.5)


def format_cpu(millicores: float) -> str:
  return f"{_round_half_up(millicores)}m"


def format_memory(num_bytes: float) -> str:
  """Mi for most pods (what `kubectl top` prints), Gi from 1024Mi up,
  Ki below 1Mi."""
  if num_bytes <= 0:
    return "0Mi"
  ki = _round_half_up(num_bytes / _KI)
  if ki < 1024:  # genuinely below 1Mi: say Ki rather than rounding up to 1Mi
    return f"{max(1, ki)}Ki"
  mi = _round_half_up(num_bytes / _MI)
  if mi >= 1024:
    gi = round(num_bytes / _GI, 2)
    return f"{gi:.2f}".rstrip("0").rstrip(".") + "Gi"
  return f"{mi}Mi"


# ── kubectl output parsing ───────────────────────────────────────────

class Usage(NamedTuple):
  cpu_millicores: float
  memory_bytes: float


class PodLimits(NamedTuple):
  """Summed container limits. None = at least one container has no (valid)
  limit for that resource, so a utilization % can't be computed."""
  cpu_millicores: Optional[float]
  memory_bytes: Optional[float]


PodKey = Tuple[str, str]  # (namespace, pod name)


def parse_top_output(text: str, default_namespace: Optional[str] = None
           ) -> Tuple[Dict[PodKey, Usage], str]:
  """Parse `kubectl top pods --no-headers` output.

  Rows are "NAMESPACE NAME CPU MEM" with --all-namespaces and "NAME CPU MEM"
  with -n <ns> (then default_namespace supplies the namespace). Each row is
  validated by shape, so error text such as "error: Metrics API not
  available" or "Error from server (Forbidden) …" can never be mistaken for
  data. Returns (usage by (ns, name), first_problem_line).
  """
  usage: Dict[PodKey, Usage] = {}
  problem = ""
  for line in (text or "").splitlines():
    parts = line.split()
    if not parts:
      continue
    ns = name = ""
    cpu = mem = None
    if len(parts) == 4:
      ns, name = parts[0], parts[1]
      cpu, mem = parse_cpu_millicores(parts[2]), parse_memory_bytes(parts[3])
    elif len(parts) == 3 and default_namespace:
      ns, name = default_namespace, parts[0]
      cpu, mem = parse_cpu_millicores(parts[1]), parse_memory_bytes(parts[2])
    if cpu is not None and mem is not None:
      usage[(ns, name)] = Usage(cpu, mem)
    elif not problem and parts[0].upper() not in ("NAME", "NAMESPACE"):
      problem = line.strip()
  return usage, problem


def parse_limits_output(text: str) -> Dict[PodKey, PodLimits]:
  """Parse the limits section: "ns|name|<cpu>,<mem>;…|<policy>,<cpu>,<mem>;…"
  per pod. Lines that don't have that shape (e.g. an error message) are
  skipped, leaving those pods without an entry = "limits unknown"."""
  limits: Dict[PodKey, PodLimits] = {}
  for line in (text or "").splitlines():
    bits = line.split("|")
    if len(bits) < 3 or not bits[0].strip() or not bits[1].strip():
      continue
    ns, name = bits[0].strip(), bits[1].strip()
    cpus: List[Optional[float]] = []
    mems: List[Optional[float]] = []
    for seg in (s for s in bits[2].split(";") if s != ""):
      fields = seg.split(",")
      cpus.append(parse_cpu_millicores(fields[0]) if fields else None)
      mems.append(parse_memory_bytes(fields[1]) if len(fields) > 1 else None)
    if len(bits) > 3:  # native sidecars: their usage is part of the pod's
      for seg in (s for s in bits[3].split(";") if s != ""):
        fields = seg.split(",")
        if len(fields) >= 3 and fields[0] == "Always":
          cpus.append(parse_cpu_millicores(fields[1]))
          mems.append(parse_memory_bytes(fields[2]))
    limits[(ns, name)] = PodLimits(_sum_limits(cpus), _sum_limits(mems))
  return limits


class NodeResources(NamedTuple):
  """Node denominator values and source labels, cached once per refresh."""
  cpu_millicores: Optional[float]
  memory_bytes: Optional[float]
  cpu_source: str
  memory_source: str


def parse_top_nodes_output(text: str) -> Tuple[Dict[str, Usage], str]:
  """Parse node usage output, keyed by node name.

  Node usage stays separate from capacity and is never used as a denominator
  for pod percentages.
  """
  usage: Dict[str, Usage] = {}
  problem = ""
  for line in (text or "").splitlines():
    parts = line.split()
    if not parts:
      continue
    if len(parts) >= 5:
      cpu = parse_cpu_millicores(parts[1])
      memory = parse_memory_bytes(parts[3])
      if cpu is not None and memory is not None:
        usage[parts[0]] = Usage(cpu, memory)
        continue
    if not problem and parts[0].upper() != "NAME":
      problem = line.strip()
  return usage, problem


def _node_denominator(allocatable: dict, capacity: dict, resource: str,
                      parser) -> Tuple[Optional[float], str]:
  value = parser(allocatable.get(resource, ""))
  if value is not None and value > 0:
    return value, "Node allocatable"
  value = parser(capacity.get(resource, ""))
  if value is not None and value > 0:
    return value, "Node capacity"
  return None, "Node capacity/allocatable unavailable"


def parse_nodes_json(text: str) -> Dict[str, NodeResources]:
  """Parse one nodes JSON response into per-node CPU/memory denominators."""
  try:
    payload = json.loads(text or "")
  except (TypeError, ValueError):
    return {}
  if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
    return {}
  result: Dict[str, NodeResources] = {}
  for item in payload["items"]:
    if not isinstance(item, dict):
      continue
    metadata = item.get("metadata") or {}
    name = metadata.get("name")
    status = item.get("status") or {}
    allocatable = status.get("allocatable") or {}
    capacity = status.get("capacity") or {}
    if not name:
      continue
    cpu, cpu_source = _node_denominator(allocatable, capacity, "cpu", parse_cpu_millicores)
    memory, memory_source = _node_denominator(allocatable, capacity, "memory", parse_memory_bytes)
    result[name] = NodeResources(cpu, memory, cpu_source, memory_source)
  return result


def _sum_limits(values: List[Optional[float]]) -> Optional[float]:
  # No containers, or any container without a usable (positive) limit,
  # means there is no honest total to divide by.
  if not values or any(v is None or v <= 0 for v in values):
    return None
  return sum(values)


# ── utilization ──────────────────────────────────────────────────────

def utilization_percent(usage: Optional[float], limit: Optional[float]) -> Optional[int]:
  """Whole-number percent of the limit in use, or None if not computable.
  May exceed 100 (CPU/memory can briefly overshoot a limit)."""
  if usage is None or limit is None or limit <= 0:
    return None
  return _round_half_up(usage / limit * 100.0)


def usage_level(percent: Optional[int]) -> Optional[str]:
  """'low' (0–59), 'moderate' (60–79), 'high' (80+), or None."""
  if percent is None:
    return None
  if percent >= HIGH_FROM:
    return "high"
  if percent >= MODERATE_FROM:
    return "moderate"
  return "low"


class Reading(NamedTuple):
  """One indicator (CPU or memory) on a pod card."""
  available: bool          # False -> no usage figure at all ("N/A")
  value: str               # "125m" / "256Mi", or "N/A"
  percent: Optional[int]   # whole percent of limit, None if unavailable
  level: Optional[str]     # "low" | "moderate" | "high" | None
  tooltip: str


class PodMetrics(NamedTuple):
  cpu: Reading
  memory: Reading


def unavailable_reading(reason: str) -> Reading:
  return Reading(False, NA, None, None, reason)


def _reading(kind: str, usage: float, denominator: Optional[float],
             limits_known: bool, fmt, source: str = "Pod limit",
             fallback_reason: str = "") -> Reading:
  value = fmt(usage)
  pct = utilization_percent(usage, denominator)
  if pct is not None:
    tip = f"{kind} usage {value} of {fmt(denominator)} {source} ({pct}%)"
  elif not limits_known:
    tip = (f"{kind} usage {value}. Container limits could not be read, "
           "so the percentage is unavailable.")
  elif fallback_reason:
    tip = f"{kind} usage {value}. {fallback_reason}"
  else:
    tip = (f"{kind} usage {value}. A valid {kind.lower()} denominator "
           "is unavailable, so the percentage cannot be calculated.")
  return Reading(True, value, pct, usage_level(pct), tip)


def build_pod_metrics(key: PodKey, usage: Dict[PodKey, Usage],
                      limits: Dict[PodKey, PodLimits], problem: str = "",
                      node_name: str = "",
                      node_resources: Optional[Dict[str, NodeResources]] = None
                      ) -> PodMetrics:
  """Use pod limits when known and present; otherwise node allocatable/capacity.

  CPU and memory fall back independently. If pod limits could not be read at
  all, do not assume limits are absent. Node usage is never used as a proxy
  for pod usage or as a denominator.
  """
  u = usage.get(key)
  if u is None:
    reason = (f"Metrics unavailable: {problem[:160]}" if problem else
              "No metrics reported for this pod yet (it may be new, not "
              "running, or metrics-server may be unavailable).")
    r = unavailable_reading(reason)
    return PodMetrics(r, r)

  pod_limits = limits.get(key)
  limits_known = pod_limits is not None
  node_name = (node_name or "").strip()
  assigned_node = None
  if node_name and node_name not in ("-", "<none>", "<unknown>"):
    assigned_node = (node_resources or {}).get(node_name)

  def resource_reading(kind: str, actual: float, pod_limit: Optional[float],
                       node_value: str, source_value: str, fmt) -> Reading:
    if not limits_known:
      return _reading(kind, actual, None, False, fmt)
    if pod_limit is not None and pod_limit > 0:
      return _reading(kind, actual, pod_limit, True, fmt, "Pod limit")
    if assigned_node is not None:
      denominator = getattr(assigned_node, node_value)
      source = getattr(assigned_node, source_value)
      if denominator is not None and denominator > 0:
        return _reading(kind, actual, denominator, True, fmt, source)
    if not node_name or node_name in ("-", "<none>", "<unknown>"):
      reason = (f"No pod {kind.lower()} limit and this pod has no assigned "
                "node, so the percentage is unavailable.")
    else:
      reason = (f"No pod {kind.lower()} limit and node '{node_name}' has no "
                f"valid {kind.lower()} allocatable/capacity value.")
    return _reading(kind, actual, None, True, fmt, fallback_reason=reason)

  return PodMetrics(
    resource_reading("CPU", u.cpu_millicores,
      pod_limits.cpu_millicores if limits_known else None,
      "cpu_millicores", "cpu_source", format_cpu),
    resource_reading("Memory", u.memory_bytes,
      pod_limits.memory_bytes if limits_known else None,
      "memory_bytes", "memory_source", format_memory),
  )


def refresh_result_is_current(request_generation: int, current_generation: int,
                              request_key: tuple, current_key: tuple) -> bool:
  """Reject a response invalidated by a newer refresh or changed view."""
  return request_generation == current_generation and request_key == current_key


# ── presentation (string-level; the card maps colour keys onto the theme) ─

def indicator_text(label: str, reading: Reading) -> str:
  """'CPU: 125m (42%)', 'CPU: 125m (N/A)' (usage but no limit) or 'CPU: N/A'."""
  if not reading.available:
    return f"{label}: {NA}"
  pct = f"{reading.percent}%" if reading.percent is not None else NA
  return f"{label}: {reading.value} ({pct})"


def color_key(reading: Reading) -> str:
  """Theme colour key for an indicator. Green/orange/red only when a real
  percentage backs them; otherwise a neutral key so nothing misleads."""
  if not reading.available:
    return "TEXT_MUTED"
  return {"low": "SUCCESS", "moderate": "WARNING", "high": "DANGER"}.get(
    reading.level or "", "TEXT_DIM")