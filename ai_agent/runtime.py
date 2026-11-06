"""Bounded multi-step agent loop with explicit tool authorization."""
from __future__ import annotations
import json
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional
from .models import AgentEvent, AgentPolicy, AgentResult, AgentStatus, AssistantMessage, ToolCall, ToolResult
from .registry import ToolRegistry


_DEPLOYMENT_DIAGNOSIS_RE = re.compile(
    r"\b(?:why\s+is|why\s+does|diagnos(?:e|is)|troubleshoot|investigate|stuck|containercreating|container\s+creating|failing|not\s+ready)\b"
    r".{0,160}?\bdeployment\s+([a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)\b",
    re.IGNORECASE | re.DOTALL,
)


_POD_DIAGNOSIS_RE = re.compile(
    r"\bpod\s+([a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)\b",
    re.IGNORECASE,
)
_CONTAINER_CREATING_RE = re.compile(r"container\s*creating|containercreating", re.IGNORECASE)


_CONFIGMAP_KEY_LOOKUP_RE = re.compile(
    r"\b(?:get|show|read|fetch|retrieve)\s+(?:the\s+)?"
    r"(?:value\s+of\s+)?(?:key\s+)?"
    r"([A-Za-z0-9_.-]+)\s+from\s+([A-Za-z0-9_.-]+)"
    r"(?:\s+in\s+(?:namespace\s+)?([a-z0-9-]+))?\b",
    re.IGNORECASE,
)


def _deployment_namespace_requested(request: str) -> str:
    """Extract the user's explicit namespace before falling back to UI context."""
    text = str(request or "")
    user_request = re.search(r"\\buser request\\s*:\\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if user_request:
        explicit = _deployment_namespace_from_text(user_request.group(1))
        if explicit:
            return explicit
    return _deployment_namespace_from_text(text)


def _deployment_namespace_from_text(text: str) -> str:
    """Parse namespace wording without mistaking status text for a namespace."""
    text = str(text or "")
    namespace = r"([a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)"
    patterns = (
        # Prefer an explicit UI/context namespace label when present.
        rf"\bnamespace\s*[:=]\s*{namespace}\b",
        # Common natural-language form: "in dev-cc ns" / "in dev-cc namespace".
        rf"\b(?:in|within)\s+{namespace}\s+(?:ns|namespace)\b",
        # Explicit form: "in namespace dev-cc".
        rf"\b(?:in|within)\s+namespace\s+{namespace}\b",
        # Explicit trailing form: "namespace dev-cc".
        rf"\bnamespace\s+{namespace}\s*$",
        # Short form at the end: "in dev-cc".
        rf"\b(?:in|within)\s+{namespace}\s*$",
    )
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if matches:
            return matches[-1].group(1).lower()
    return ""


def _configmap_lookup_requested(request: str):
    """Identify a direct key lookup and its explicitly named ConfigMap/namespace."""
    match = _CONFIGMAP_KEY_LOOKUP_RE.search(str(request or ""))
    return (match.group(1), match.group(2), match.group(3)) if match else None


class AgentRuntime:
    """Run normalized provider responses against application-owned tool capabilities."""
    SYSTEM_PROMPT = (
        "You are KubeDock's infrastructure assistant. Use only supplied tool results as evidence for live cluster facts. "
        "Never infer resource names, namespaces, object types, configuration keys, environment-variable values, replica counts, "
        "or ownership from naming patterns, pod names, workload roles, conventions, or likely architecture. "
        "A plausible guess is not evidence. If a tool was not run, its result is unavailable. "
        "If a tool reports error, not found, forbidden, timeout, empty, truncated, or unavailable, treat that fact as unknown; "
        "never convert failure or missing output into a successful finding. "
        "For exact values, quote only the exact value returned by a successful tool that inspected the exact requested object and key. "
        "Do not substitute a similarly named object or a different resource type. If the available tools cannot inspect the requested source, "
        "say so and ask for the missing detail or explain the limitation. "
        "For a question about the live state or failure of a specifically named Deployment, inspect that exact Deployment with k8s_get_deployment before stating whether it exists or describing its rollout. "
        "A successful list of other Deployments does not prove the requested Deployment is absent. If exact inspection fails, report the failure as unknown, not as absence. "
        "For a named Pod in ContainerCreating, first call k8s_get_pod for that exact Pod and namespace, then k8s_get_events with the same namespace and pod filter, and k8s_list_pods to compare the other Pods. "
        "Use Pod owner references to identify sibling Pods from the same ReplicaSet/workload; distinguish those from unrelated Pods in the namespace. "
        "A ContainerCreating/waiting state alone is not a root cause. Report a cause only when an exact Pod waiting reason/message or a matching Pod event explicitly identifies it. "
        "If no matching events are returned, say that no matching events were returned and the cause remains unconfirmed; do not guess image pull, volume mount, runtime, network, or node problems. "
        "Do not request logs for a container that has not started; logs are not expected to be available in ContainerCreating. "
        "Never infer Pod health from Deployment ready/available counts alone. Do not add a duplicate User-Facing Answer section or repeat the same conclusion. "
        "Distinguish observed facts from inference, and label any inference explicitly. "
        "Do not claim an operation succeeded unless a tool result confirms it. Prefer read-only checks. Explain uncertainty and impact."
    )
    def __init__(self, registry: ToolRegistry, policy: Optional[AgentPolicy] = None):
        """Bind an explicit tool registry to a bounded execution policy."""
        self.registry = registry
        self.policy = policy or AgentPolicy()

    @staticmethod
    def _normalize_response(response):
        """Normalize provider output without silently repairing invalid arguments."""
        if isinstance(response, AssistantMessage):
            return response
        if not isinstance(response, Mapping):
            raise TypeError("Provider adapter must return AssistantMessage or a mapping.")
        calls = []
        for item in response.get("tool_calls") or []:
            if isinstance(item, ToolCall):
                calls.append(item)
                continue
            if not isinstance(item, Mapping):
                raise ValueError("Provider returned a malformed tool call.")
            args = item.get("arguments", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = None
            calls.append(ToolCall(str(item.get("id") or ""), str(item.get("name") or ""), args))
        return AssistantMessage(str(response.get("content") or ""), calls, str(response.get("finish_reason") or ""))

    def run(self, user_message: str, adapter, *, conversation=None, approval_handler=None, event_handler=None) -> AgentResult:
        """Execute a bounded multi-turn plan and return its answer and audit events."""
        request = str(user_message or "").strip()
        if not request:
            return AgentResult(status=AgentStatus.ERROR, error="Enter a request.")
        if len(request) > 8000:
            return AgentResult(status=AgentStatus.ERROR, error="Request exceeds the 8,000 character limit.")
        # The policy prompt is mandatory on every run. Conversation history is
        # context only; never let a carried system message replace the safety rules.
        history = [
            item for item in (conversation or [])
            if isinstance(item, Mapping) and str(item.get("role", "")).lower() != "system"
        ]
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": request})
        events, results, pending = [], [], []
        total_chars = 0
        call_count = 0
        started = time.monotonic()

        def emit(kind, message, tool_name="", tool_call_id="", details=None):
            event = AgentEvent(kind, message, tool_name, tool_call_id, details or {})
            events.append(event)
            if event_handler:
                event_handler(event)

        for _iteration in range(self.policy.max_iterations):
            if time.monotonic() - started > self.policy.max_run_seconds:
                emit("limit", "Agent run time limit reached.")
                return AgentResult(AgentStatus.LIMIT_REACHED, events=events, tool_results=results, messages=messages)
            try:
                response = self._normalize_response(adapter.complete(
                    messages=messages, tools=self.registry.describe(self.policy.allowed_tools)))
            except Exception as exc:
                emit("error", "Provider request failed.")
                return AgentResult(AgentStatus.ERROR, events=events, tool_results=results, messages=messages, error=str(exc)[:1000])
            if not response.tool_calls:
                answer = response.content.strip()
                lookup = _configmap_lookup_requested(request)
                if lookup:
                    requested_key, requested_configmap, requested_namespace = lookup
                    evidence = next((
                        item for item in reversed(results)
                        if item.name == "k8s_get_configmap_key" and item.status == "success"
                    ), None)
                    if evidence is None:
                        answer = (
                            "I couldn't verify the requested value from the exact ConfigMap. "
                            "No value has been inferred from pod names, deployment names, or conventions. "
                            "Please confirm the ConfigMap name, namespace, and key, or clarify if the source is a Secret."
                        )
                    else:
                        try:
                            payload = json.loads(evidence.content)
                            if (payload.get("found") is True
                                    and payload.get("resource") == "ConfigMap"
                                    and payload.get("key") == requested_key
                                    and payload.get("name") == requested_configmap
                                    and (not requested_namespace or payload.get("namespace") == requested_namespace)):
                                answer = (
                                    "Verified from ConfigMap " + str(payload["name"])
                                    + " in namespace " + str(payload["namespace"])
                                    + ": " + str(payload["key"]) + "=" + str(payload["value"])
                                )
                            else:
                                answer = (
                                    "The exact ConfigMap lookup did not return the requested key and object. "
                                    "The value is unverified; I will not guess or substitute another resource."
                                )
                        except (TypeError, ValueError, KeyError):
                            answer = (
                                "The ConfigMap tool returned an invalid evidence payload. "
                                "The value is unverified; I will not guess."
                            )
                elif not lookup and _DEPLOYMENT_DIAGNOSIS_RE.search(request):
                    deployment_match = _DEPLOYMENT_DIAGNOSIS_RE.search(request)
                    if deployment_match:
                        requested_name = deployment_match.group(1)
                        requested_namespace = _deployment_namespace_requested(request)
                        exact_evidence = None
                        for item in reversed(results):
                            if item.name != "k8s_get_deployment" or item.status != "success":
                                continue
                            try:
                                payload = json.loads(item.content)
                            except (TypeError, ValueError):
                                continue
                            if (payload.get("found") is True
                                    and payload.get("resource") == "Deployment"
                                    and payload.get("name") == requested_name
                                    and (not requested_namespace or payload.get("namespace") == requested_namespace)):
                                exact_evidence = payload
                                break
                        if exact_evidence is None:
                            answer = (
                                "I couldn't verify the exact Deployment "
                                + requested_name
                                + ((" in namespace " + requested_namespace) if requested_namespace else "")
                                + ". The list/diagnostic results are not sufficient to conclude that it does not exist, "
                                "and failed event or log reads do not establish a root cause. "
                                "Please retry the exact Deployment inspection and Pod-specific event checks."
                            )
                elif _CONTAINER_CREATING_RE.search(request) and _POD_DIAGNOSIS_RE.search(request):
                    pod_match = _POD_DIAGNOSIS_RE.search(request)
                    requested_name = pod_match.group(1)
                    requested_namespace = _deployment_namespace_requested(request)
                    pod_evidence = None
                    for item in reversed(results):
                        if item.name != "k8s_get_pod" or item.status != "success":
                            continue
                        try:
                            payload = json.loads(item.content)
                        except (TypeError, ValueError):
                            continue
                        if (payload.get("found") is True
                                and payload.get("resource") == "Pod"
                                and payload.get("name") == requested_name
                                and (not requested_namespace or payload.get("namespace") == requested_namespace)):
                            pod_evidence = payload
                            break
                    if pod_evidence is None:
                        answer = (
                            "I couldn't verify the exact Pod " + requested_name
                            + ((" in namespace " + requested_namespace) if requested_namespace else "")
                            + ". A namespace-wide Pod list or failed log/event request is not enough to establish its state or cause. "
                            "Please retry the exact Pod inspection and Pod-filtered event check."
                        )
                    else:
                        namespace = str(pod_evidence.get("namespace") or requested_namespace or "")
                        phase = str(pod_evidence.get("phase") or "Unknown")
                        node = str(pod_evidence.get("node") or "")
                        container_rows = []
                        specific_waiting_evidence = []
                        for container in (pod_evidence.get("initContainers") or []) + (pod_evidence.get("containers") or []):
                            name = str(container.get("name") or "container")
                            state = str(container.get("state") or "unknown")
                            reason = str(container.get("reason") or "")
                            message = str(container.get("message") or "")
                            detail = name + ": " + state
                            if reason:
                                detail += " (" + reason + ")"
                            if message:
                                detail += " — " + message
                            container_rows.append(detail)
                            if message or (reason and reason.lower() not in {"containercreating", "podinitializing"}):
                                specific_waiting_evidence.append(detail)
                        event_rows = []
                        events_checked = False
                        for item in reversed(results):
                            if item.name != "k8s_get_events" or item.status != "success":
                                continue
                            try:
                                event_payload = json.loads(item.content)
                            except (TypeError, ValueError):
                                continue
                            if (isinstance(event_payload, dict)
                                    and event_payload.get("filteredPod") == requested_name
                                    and (not requested_namespace or event_payload.get("namespace") == requested_namespace)):
                                events_checked = True
                                event_rows = [
                                    event for event in (event_payload.get("events") or [])
                                    if str(event.get("object") or "") == requested_name
                                    and (not event.get("objectKind") or str(event.get("objectKind")).lower() == "pod")
                                ]
                                break
                        pod_list = []
                        for item in reversed(results):
                            if item.name != "k8s_list_pods" or item.status != "success":
                                continue
                            try:
                                candidate = json.loads(item.content)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(candidate, list):
                                pod_list = candidate
                                break
                        owner_refs = pod_evidence.get("owners") or []
                        owner_names = {
                            str(owner.get("name") or "")
                            for owner in owner_refs
                            if owner.get("name")
                        }
                        other_pods = [
                            row for row in pod_list
                            if str(row.get("name") or "") != requested_name
                        ]
                        siblings = [
                            row for row in other_pods
                            if owner_names and any(
                                str(owner.get("name") or "") in owner_names
                                for owner in (row.get("owners") or [])
                            )
                        ]
                        lines = [
                            "**Verified Pod:** " + requested_name + " in namespace " + namespace,
                            "- Phase: **" + phase + "**",
                            "- Node: " + (node or "not assigned"),
                        ]
                        if container_rows:
                            lines.append("- Containers: " + "; ".join(container_rows))
                        for condition in pod_evidence.get("conditions") or []:
                            if str(condition.get("status") or "").lower() == "false":
                                detail = str(condition.get("type") or "Condition") + " is False"
                                if condition.get("reason"):
                                    detail += " (" + str(condition["reason"]) + ")"
                                if condition.get("message"):
                                    detail += " — " + str(condition["message"])
                                lines.append("- " + detail)
                        if event_rows:
                            lines.append("")
                            lines.append("**Events for this Pod:**")
                            for event in event_rows[-8:]:
                                detail = str(event.get("reason") or "Event")
                                if event.get("message"):
                                    detail += ": " + str(event["message"])
                                if event.get("count"):
                                    detail += " (count " + str(event["count"]) + ")"
                                lines.append("- " + detail)
                        elif specific_waiting_evidence:
                            lines.extend(["", "**Container status evidence:** " + "; ".join(specific_waiting_evidence)])
                            lines.append("This status provides a more specific startup clue, but check the corresponding event/message before treating it as the complete root cause.")
                        elif events_checked:
                            lines.extend(["", "**Root cause:** No events matching this Pod were returned. The cause is not confirmed by the available evidence."])
                        else:
                            lines.extend(["", "**Root cause:** Pod-specific events were not successfully retrieved, so the cause is unverified."])
                        lines.extend(["", "**Other Pods in " + namespace + ":**"])
                        if siblings:
                            lines.append("Same owner/workload:")
                            for row in siblings[:8]:
                                states = ", ".join(
                                    str(container.get("name") or "container") + "="
                                    + str(container.get("state") or "unknown")
                                    + ((" (" + str(container.get("reason")) + ")") if container.get("reason") else "")
                                    for container in (row.get("containers") or [])
                                ) or "no container status"
                                lines.append("- " + str(row.get("name") or "") + ": "
                                             + str(row.get("phase") or "Unknown") + "; " + states)
                        unrelated = [row for row in other_pods if row not in siblings]
                        if unrelated:
                            lines.append("Other workloads (not siblings):")
                            for row in unrelated[:8]:
                                lines.append("- " + str(row.get("name") or "") + ": "
                                             + str(row.get("phase") or "Unknown"))
                        if not pod_list:
                            lines.append("The Pod inventory was not successfully retrieved, so other running Pods cannot be confirmed.")
                        answer = "\n".join(lines)
                elif results and not any(item.status == "success" for item in results):
                    answer = (
                        "I couldn't verify the requested cluster information because none of the "
                        "Kubernetes tool checks completed successfully. I won't infer or guess the result. "
                        "Please check the selected context, namespace, permissions, and resource details."
                    )
                if not answer:
                    emit("error", "Provider returned an empty response.")
                    return AgentResult(
                        AgentStatus.ERROR, events=events, tool_results=results,
                        messages=messages, error="Provider returned an empty response without text or tool calls.",
                    )
                messages.append({"role": "assistant", "content": answer})
                emit("completed", "Agent response completed.")
                return AgentResult(AgentStatus.COMPLETED, answer, events, results, pending, messages)
            assistant_calls = []
            for call in response.tool_calls:
                if call.id and call.name:
                    assistant_calls.append({"id": call.id, "name": call.name, "arguments": call.arguments})
                else:
                    emit("rejected", "A tool call was missing its identifier or name.", call.name, call.id)
            messages.append({"role": "assistant", "content": response.content, "tool_calls": assistant_calls})
            for call in response.tool_calls:
                call_count += 1
                if call_count > self.policy.max_tool_calls:
                    emit("limit", "Maximum tool-call limit reached.", call.name, call.id)
                    return AgentResult(AgentStatus.LIMIT_REACHED, events=events, tool_results=results, messages=messages)
                if time.monotonic() - started > self.policy.max_run_seconds:
                    emit("limit", "Agent run time limit reached.", call.name, call.id)
                    return AgentResult(AgentStatus.LIMIT_REACHED, events=events, tool_results=results, messages=messages)
                if not call.id or not call.name:
                    continue
                spec = self.registry.get(call.name)
                if spec is None:
                    result = ToolResult(call.id, call.name, "rejected", "Unknown tool.")
                    results.append(result)
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": "rejected", "content": result.content}, ensure_ascii=False)})
                    emit("rejected", "Unknown tool was rejected.", call.name, call.id)
                    continue
                if self.policy.allowed_tools is not None and call.name not in self.policy.allowed_tools:
                    result = ToolResult(call.id, call.name, "rejected", "This tool is not allowed by the active policy.")
                    results.append(result)
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": "rejected", "content": result.content}, ensure_ascii=False)})
                    emit("rejected", "Tool is outside the active allowlist.", call.name, call.id)
                    continue
                try:
                    arguments = self.registry.validate_arguments(spec.parameters, call.arguments)
                except (TypeError, ValueError) as exc:
                    result = ToolResult(call.id, call.name, "invalid_arguments", str(exc))
                    results.append(result)
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": "invalid_arguments", "content": result.content}, ensure_ascii=False)})
                    emit("rejected", "Tool arguments failed validation.", call.name, call.id)
                    continue
                needs_approval = ((spec.permission == "write" and self.policy.require_approval_for_mutations) or spec.requires_approval)
                if spec.permission == "read" and not self.policy.auto_execute_read_only:
                    needs_approval = True
                if needs_approval:
                    approved = False
                    if approval_handler is not None:
                        try:
                            approved = bool(approval_handler(spec, arguments))
                        except Exception:
                            approved = False
                    if not approved:
                        pending.append(call)
                        result = ToolResult(call.id, call.name, "approval_required", "Not executed. Explicit approval is required.")
                        results.append(result)
                        messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": "approval_required", "content": result.content}, ensure_ascii=False)})
                        emit("approval_required", "Waiting for approval; tool was not executed.", call.name, call.id)
                        continue
                try:
                    content = self.registry.serialize_result(spec.handler(arguments))
                    status = "success"
                except Exception as exc:
                    content = "Tool execution failed: " + str(exc)[:500]
                    status = "error"
                remaining = self.policy.max_total_result_chars - total_chars
                content = content[:min(self.policy.max_result_chars, max(0, remaining))]
                total_chars += len(content)
                result = ToolResult(call.id, call.name, status, content)
                results.append(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps({"status": status, "content": content}, ensure_ascii=False)})
                emit("tool_result", "Tool execution " + status + ".", call.name, call.id,
                     {"result_chars": len(content), "permission": spec.permission})
            if pending:
                return AgentResult(AgentStatus.NEEDS_APPROVAL, events=events, tool_results=results,
                                   pending_approvals=pending, messages=messages)
        emit("limit", "Maximum reasoning iterations reached.")
        return AgentResult(AgentStatus.LIMIT_REACHED, events=events, tool_results=results, messages=messages)
