"""Bounded multi-step agent loop with explicit tool authorization."""
from __future__ import annotations
import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional
from .models import AgentEvent, AgentPolicy, AgentResult, AgentStatus, AssistantMessage, ToolCall, ToolResult
from .registry import ToolRegistry

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
