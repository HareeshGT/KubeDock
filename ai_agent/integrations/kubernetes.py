"""Read-only Kubernetes tools registered through KubeDock's shared command service."""
from __future__ import annotations
import re
import shlex
import unicodedata
import json

from ai_assist import _redact_sensitive_text
from ..models import ToolSpec

_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _validate_namespace(value):
    """Validate and return a Kubernetes DNS-label namespace."""
    value = str(value or "").strip()
    if not _NAMESPACE_RE.fullmatch(value) or len(value) > 63:
        raise ValueError("A valid Kubernetes namespace is required.")
    return value


def _validate_name(value, label="resource"):
    """Validate and return a Kubernetes resource name."""
    value = str(value or "").strip()
    if not _NAME_RE.fullmatch(value) or len(value) > 253:
        raise ValueError("A valid Kubernetes " + label + " name is required.")
    return value


def _base(context, namespace=None):
    """Build a safely quoted kubectl prefix for a validated context/namespace."""
    context = str(context or "")
    if any(unicodedata.category(char) == "Cc" for char in context):
        raise ValueError("The selected Kubernetes context contains control characters.")
    command = "kubectl"
    if context:
        command += " --context=" + shlex.quote(context)
    if namespace:
        command += " -n " + shlex.quote(_validate_namespace(namespace))
    return command


def _run(kubernetes_service, command, timeout=20):
    """Execute a bounded read command and redact its returned output."""
    code, output, error = kubernetes_service.commands.execute(command, timeout=timeout)
    if code != 0:
        detail = error or output or "kubectl exited with status " + str(code)
        raise RuntimeError(_redact_sensitive_text(str(detail))[:2000])
    elif str(output or "").strip():
        text = output
    elif str(error or "").strip():
        # Some SSH/command wrappers return a zero status with diagnostics on
        # stderr and no stdout. Do not turn an RBAC/connection warning into
        # an apparently empty, successful result.
        text = error
    else:
        text = output
    return _redact_sensitive_text(str(text))[:12000]


def register_kubernetes_read_tools(registry, kubernetes_service, context="", include_logs=False):
    """Register bounded read-only tools; no tool can submit a Kubernetes mutation."""
    if kubernetes_service is None or getattr(kubernetes_service, "commands", None) is None:
        raise ValueError("A shared Kubernetes command service is required.")
    selected_context = str(context or "")
    _base(selected_context)  # validate once before closing over this context

    def list_pods(args):
        """List a bounded set of pods in the requested namespace."""
        namespace = _validate_namespace(args["namespace"])
        return _run(kubernetes_service, _base(selected_context, namespace) + " get pods -o wide | head -n 100", 20)

    def list_deployments(args):
        """List a bounded set of deployments in the requested namespace."""
        namespace = _validate_namespace(args["namespace"])
        return _run(kubernetes_service, _base(selected_context, namespace) + " get deployments -o wide | head -n 100", 20)


    def get_configmap_key(args):
        """Read exactly one key from one named ConfigMap; return no unrelated data."""
        namespace = _validate_namespace(args["namespace"])
        name = _validate_name(args["configmap"], "ConfigMap")
        key = str(args["key"] or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", key):
            raise ValueError("A valid ConfigMap data key is required.")
        raw = _run(kubernetes_service,
                   _base(selected_context, namespace) + " get configmap/" + shlex.quote(name) + " -o json", 20)
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Kubernetes returned an invalid ConfigMap JSON response.") from exc
        data = obj.get("data") or {}
        if key not in data:
            return {"found": False, "resource": "ConfigMap", "name": name,
                    "namespace": namespace, "key": key,
                    "reason": "The ConfigMap exists, but the requested key is not present."}
        value = data[key]
        if not isinstance(value, str):
            raise RuntimeError("The requested ConfigMap key did not contain a string value.")
        return {"found": True, "resource": "ConfigMap", "name": name,
                "namespace": namespace, "key": key,
                "value": _redact_sensitive_text(value)[:4000]}

    def list_nodes(_args):
        """List a bounded set of cluster nodes."""
        return _run(kubernetes_service, _base(selected_context) + " get nodes -o wide | head -n 100", 20)

    def get_events(args):
        """Return a bounded, recent event sample for one namespace."""
        namespace = _validate_namespace(args["namespace"])
        limit = int(args.get("limit", 30))
        if not 1 <= limit <= 50:
            raise ValueError("Event limit must be between 1 and 50.")
        command = (
            _base(selected_context, namespace)
            + " get events --sort-by=.lastTimestamp "
            + "-o custom-columns=TIME:.lastTimestamp,TYPE:.type,OBJECT:.involvedObject.name,REASON:.reason,MESSAGE:.message "
            + "--no-headers | tail -n " + str(limit)
        )
        return _run(kubernetes_service, command, 20)

    def get_pod_logs(args):
        """Return a byte- and line-bounded pod log tail after redaction."""
        namespace = _validate_namespace(args["namespace"])
        pod = _validate_name(args["pod"], "pod")
        tail = int(args.get("tail", 50))
        if not 1 <= tail <= 100:
            raise ValueError("Log tail must be between 1 and 100 lines.")
        command = _base(selected_context, namespace) + " logs " + shlex.quote(pod) + " --all-containers=true --prefix=true --tail=" + str(tail) + " --limit-bytes=65536"
        return _run(kubernetes_service, command, 25)

    registry.register(ToolSpec(
        "k8s_list_pods", "List pods in one namespace. Read-only.",
        {"type": "object", "properties": {"namespace": {"type": "string", "minLength": 1, "maxLength": 63}},
         "required": ["namespace"], "additionalProperties": False},
        list_pods,
    ))
    registry.register(ToolSpec(
        "k8s_list_deployments", "List deployments in one namespace. Read-only.",
        {"type": "object", "properties": {"namespace": {"type": "string", "minLength": 1, "maxLength": 63}},
         "required": ["namespace"], "additionalProperties": False},
        list_deployments,
    ))
    registry.register(ToolSpec(
        "k8s_get_configmap_key",
        "Read exactly one key from the exact named ConfigMap in one namespace. Returns only the requested key and value. Never substitute another resource. Read-only.",
        {"type": "object", "properties": {
            "namespace": {"type": "string", "minLength": 1, "maxLength": 63},
            "configmap": {"type": "string", "minLength": 1, "maxLength": 253},
            "key": {"type": "string", "minLength": 1, "maxLength": 253}},
         "required": ["namespace", "configmap", "key"], "additionalProperties": False},
        get_configmap_key,
    ))
    registry.register(ToolSpec(
        "k8s_list_nodes", "List cluster nodes. Read-only.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        list_nodes,
    ))
    registry.register(ToolSpec(
        "k8s_get_events", "Read the latest events from one namespace. Read-only.",
        {"type": "object", "properties": {
            "namespace": {"type": "string", "minLength": 1, "maxLength": 63},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         "required": ["namespace"], "additionalProperties": False},
        get_events,
    ))
    if include_logs:
        registry.register(ToolSpec(
            "k8s_get_pod_logs", "Read a bounded tail of pod logs. Output is redacted before AI use. Read-only.",
            {"type": "object", "properties": {
                "namespace": {"type": "string", "minLength": 1, "maxLength": 63},
                "pod": {"type": "string", "minLength": 1, "maxLength": 253},
                "tail": {"type": "integer", "minimum": 1, "maximum": 100}},
             "required": ["namespace", "pod"], "additionalProperties": False},
            get_pod_logs,
        ))
    return registry
