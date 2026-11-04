"""Semantic Kubernetes resource states used by cards and summary UI.

This module deliberately contains no Qt imports so status classification can
be tested independently of the desktop runtime.
"""

from collections import Counter


def resource_status_tone(status, restarts=0):
    """Return a semantic tone for a Kubernetes resource status.

    A non-zero restart count on an otherwise Running pod is attention-worthy,
    but is not treated as a failed pod.
    """
    value = str(status or "").strip().casefold().replace("_", "")
    if any(token in value for token in (
        "crash", "backoff", "error", "fail", "evict", "unknown", "oomkilled",
    )):
        return "critical"
    if any(token in value for token in (
        "pending", "init", "creating", "terminating", "notready",
        "containercreating", "imagepull",
    )):
        return "warning"
    if any(token in value for token in ("running", "completed", "succeeded", "ready")):
        try:
            if int(str(restarts or "0").strip() or "0") > 0:
                return "warning"
        except (TypeError, ValueError):
            pass
        return "healthy"
    return "unknown"


def summarize_pod_statuses(pods):
    """Count pods by semantic state without inferring missing information."""
    counts = Counter({"healthy": 0, "warning": 0, "critical": 0, "unknown": 0})
    for pod in pods:
        tone = resource_status_tone(pod.get("status"), pod.get("restarts"))
        counts[tone] += 1
    return dict(counts)
