"""Read-only Kubernetes tools registered through KubeDock's shared command service."""
from __future__ import annotations
import re
import shlex
import json
import unicodedata

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


def _run(kubernetes_service, command, timeout=20, max_chars=12000, reject_truncation=False):
    """Execute a bounded read command and redact its returned output."""
    code, output, error = kubernetes_service.commands.execute(command, timeout=timeout)
    if code != 0:
        detail = error or output or "kubectl exited with status " + str(code)
        raise RuntimeError(_redact_sensitive_text(str(detail))[:2000])
    elif str(output or "").strip():
        text = output
    elif str(error or "").strip() and not str(output or "").strip():
        # A zero exit code with stderr-only output is ambiguous (for example,
        # an SSH/RBAC warning with no actual resource data). Treat it as
        # unverified instead of passing diagnostics off as a resource value.
        raise RuntimeError(_redact_sensitive_text(str(error))[:2000])
    else:
        text = output
    safe_text = _redact_sensitive_text(str(text))
    if len(safe_text) > max_chars:
        if reject_truncation:
            raise RuntimeError("Kubernetes returned more data than the safe exact-value limit; the value is unverified.")
        return safe_text[:max_chars] + "\n[TRUNCATED: additional output was omitted; do not infer missing data]"
    return safe_text


def register_kubernetes_read_tools(registry, kubernetes_service, context="", include_logs=False):
    """Register bounded read-only tools; no tool can submit a Kubernetes mutation."""
    if kubernetes_service is None or getattr(kubernetes_service, "commands", None) is None:
        raise ValueError("A shared Kubernetes command service is required.")
    selected_context = str(context or "")
    _base(selected_context)  # validate once before closing over this context

    def list_pods(args):
        """List a bounded set of pods in the requested namespace."""
        namespace = _validate_namespace(args["namespace"])
        return _run(kubernetes_service, _base(selected_context, namespace) + " get pods -o wide", 20)

    def list_deployments(args):
        """List a bounded set of deployments in the requested namespace."""
        namespace = _validate_namespace(args["namespace"])
        return _run(kubernetes_service, _base(selected_context, namespace) + " get deployments -o wide", 20)


    def get_deployment(args):
        """Inspect one exact Deployment; never infer its existence from a list."""
        namespace = _validate_namespace(args["namespace"])
        name = _validate_name(args["name"], "Deployment")
        raw = _run(
            kubernetes_service,
            _base(selected_context, namespace) + " get deployment/" + shlex.quote(name) + " -o json",
            20, max_chars=100000, reject_truncation=True,
        )
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Kubernetes returned invalid Deployment JSON; its state is unverified.") from exc
        metadata = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        pod_template = spec.get("template") or {}
        pod_spec = pod_template.get("spec") or {}
        return {
            "found": True,
            "resource": "Deployment",
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "uid": metadata.get("uid", ""),
            "generation": metadata.get("generation"),
            "observedGeneration": status.get("observedGeneration"),
            "replicas": {
                "desired": spec.get("replicas", 1),
                "updated": status.get("updatedReplicas", 0),
                "ready": status.get("readyReplicas", 0),
                "available": status.get("availableReplicas", 0),
                "unavailable": status.get("unavailableReplicas", 0),
            },
            "selector": spec.get("selector") or {},
            "conditions": status.get("conditions") or [],
            "containers": [
                {"name": c.get("name", ""), "image": c.get("image", "")}
                for c in pod_spec.get("containers") or []
            ],
        }

    def get_pod_status(args):
        """Inspect one exact Pod, including waiting reasons, node assignment and owners."""
        namespace = _validate_namespace(args["namespace"])
        pod = _validate_name(args["pod"], "pod")
        raw = _run(
            kubernetes_service,
            _base(selected_context, namespace) + " get pod/" + shlex.quote(pod) + " -o json",
            20, max_chars=100000, reject_truncation=True,
        )
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Kubernetes returned invalid Pod JSON; its state is unverified.") from exc
        metadata = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        containers = []
        for container in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
            state = container.get("state") or {}
            waiting = state.get("waiting") or {}
            terminated = state.get("terminated") or {}
            running = state.get("running") or {}
            containers.append({
                "name": container.get("name", ""),
                "ready": container.get("ready"),
                "restartCount": container.get("restartCount", 0),
                "image": container.get("image", ""),
                "state": "waiting" if waiting else "terminated" if terminated else "running" if running else "unknown",
                "reason": waiting.get("reason") or terminated.get("reason") or "",
                "message": waiting.get("message") or terminated.get("message") or "",
                "startedAt": running.get("startedAt") or terminated.get("startedAt") or "",
                "finishedAt": terminated.get("finishedAt") or "",
                "exitCode": terminated.get("exitCode"),
            })
        return {
            "found": True,
            "resource": "Pod",
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "uid": metadata.get("uid", ""),
            "phase": status.get("phase", "Unknown"),
            "reason": status.get("reason", ""),
            "message": status.get("message", ""),
            "nodeName": spec.get("nodeName", ""),
            "startTime": status.get("startTime", ""),
            "ownerReferences": [
                {"kind": owner.get("kind", ""), "name": owner.get("name", ""), "uid": owner.get("uid", "")}
                for owner in metadata.get("ownerReferences") or []
            ],
            "conditions": [
                {"type": condition.get("type", ""), "status": condition.get("status", ""),
                 "reason": condition.get("reason", ""), "message": condition.get("message", "")}
                for condition in status.get("conditions") or []
            ],
            "containers": containers,
            "initContainers": [
                {"name": container.get("name", ""), "image": container.get("image", "")}
                for container in spec.get("initContainers") or []
            ],
            "volumes": [
                {"name": volume.get("name", ""),
                 "sources": [key for key in ("configMap", "secret", "persistentVolumeClaim", "projected", "emptyDir", "hostPath") if key in volume]}
                for volume in spec.get("volumes") or []
            ],
        }

    def get_configmap_key(args):
        """Read exactly one key from one named ConfigMap; return no unrelated data."""
        namespace = _validate_namespace(args["namespace"])
        name = _validate_name(args["configmap"], "ConfigMap")
        key = str(args["key"] or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", key):
            raise ValueError("A valid ConfigMap data key is required.")
        jsonpath = shlex.quote("{.data['" + key + "']}")
        value = _run(
            kubernetes_service,
            _base(selected_context, namespace) + " get configmap/" + shlex.quote(name)
            + " -o jsonpath=" + jsonpath + " --allow-missing-template-keys=false",
            20, max_chars=4000, reject_truncation=True,
        )
        return {"found": True, "resource": "ConfigMap", "name": name,
                "namespace": namespace, "key": key,
                "value": _redact_sensitive_text(value)[:4000]}

    def list_nodes(_args):
        """List a bounded set of cluster nodes."""
        return _run(kubernetes_service, _base(selected_context) + " get nodes -o wide", 20)

    def get_events(args):
        """Return a bounded, recent event sample for one namespace."""
        namespace = _validate_namespace(args["namespace"])
        limit = int(args.get("limit", 30))
        if not 1 <= limit <= 50:
            raise ValueError("Event limit must be between 1 and 50.")
        pod = _validate_name(args["pod"], "pod") if args.get("pod") else ""
        command = _base(selected_context, namespace) + " get events"
        if pod:
            command += " --field-selector=" + shlex.quote("involvedObject.name=" + pod)
        command += " --sort-by=.lastTimestamp -o json"
        raw = _run(kubernetes_service, command, 20, max_chars=100000, reject_truncation=True)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Kubernetes returned an invalid events JSON response.") from exc
        items = payload.get("items") or []
        if pod:
            # Keep the result exact even if a proxy/API implementation ignores
            # the field selector; unrelated namespace events are never evidence.
            items = [
                item for item in items
                if (item.get("involvedObject") or {}).get("name") == pod
            ]
        return [{
            "time": item.get("lastTimestamp") or item.get("eventTime") or item.get("metadata", {}).get("creationTimestamp", ""),
            "type": item.get("type", ""),
            "object": (item.get("involvedObject") or {}).get("name", ""),
            "kind": (item.get("involvedObject") or {}).get("kind", ""),
            "uid": (item.get("involvedObject") or {}).get("uid", ""),
            "reason": item.get("reason", ""),
            "message": item.get("message", ""),
            "count": item.get("count", 1),
        } for item in items[-limit:]]

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
        "k8s_get_deployment",
        "Inspect exactly one named Deployment in one namespace. Returns its actual namespace, rollout counts, selector, conditions and container images. Use this before claiming whether a named Deployment exists. Read-only.",
        {"type": "object", "properties": {
            "namespace": {"type": "string", "minLength": 1, "maxLength": 63},
            "name": {"type": "string", "minLength": 1, "maxLength": 253}},
         "required": ["namespace", "name"], "additionalProperties": False},
        get_deployment,
    ))
    registry.register(ToolSpec(
        "k8s_get_pod_status",
        "Inspect exactly one named Pod in one namespace. Returns its phase, node, owner, conditions, container waiting/terminated reasons and messages, restart counts, images and volume source types. Use this for ContainerCreating and Pod diagnosis. Read-only.",
        {"type": "object", "properties": {
            "namespace": {"type": "string", "minLength": 1, "maxLength": 63},
            "pod": {"type": "string", "minLength": 1, "maxLength": 253}},
         "required": ["namespace", "pod"], "additionalProperties": False},
        get_pod_status,
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
            "pod": {"type": "string", "minLength": 1, "maxLength": 253},
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
