"""Typed contracts shared by the AI agent runtime and provider adapters."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

@dataclass(frozen=True)
class ToolSpec:
    """Describe one application-owned tool and its permission requirements."""
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], Any] = field(repr=False, compare=False)
    permission: str = "read"
    requires_approval: bool = False
    def __post_init__(self):
        if self.permission not in {"read", "write"}:
            raise ValueError("Tool permission must be 'read' or 'write'.")
        if not callable(self.handler):
            raise ValueError("Tool handler must be callable.")

@dataclass(frozen=True)
class ToolCall:
    """Represent one normalized model request to invoke a registered tool."""
    id: str
    name: str
    arguments: Mapping[str, Any]

@dataclass(frozen=True)
class AssistantMessage:
    """Represent one assistant response containing text and optional tool calls."""
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""

@dataclass(frozen=True)
class ToolResult:
    """Record the status and bounded textual result of a tool invocation."""
    tool_call_id: str
    name: str
    status: str
    content: str

@dataclass(frozen=True)
class AgentEvent:
    """Describe a user-visible event in the agent execution timeline."""
    kind: str
    message: str
    tool_name: str = ""
    tool_call_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class AgentPolicy:
    """Set resource, authorization, and tool-allowlist limits for one run."""
    max_iterations: int = 6
    max_tool_calls: int = 12
    max_result_chars: int = 12000
    max_total_result_chars: int = 48000
    max_run_seconds: float = 90.0
    auto_execute_read_only: bool = True
    require_approval_for_mutations: bool = True
    allowed_tools: Optional[frozenset] = None
    def __post_init__(self):
        for name in ("max_iterations", "max_tool_calls", "max_result_chars", "max_total_result_chars"):
            if getattr(self, name) < 1:
                raise ValueError(name + " must be at least 1.")
        if self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive.")

class AgentStatus:
    """Stable status values returned by the agent runtime."""
    COMPLETED = "completed"
    NEEDS_APPROVAL = "needs_approval"
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"

@dataclass
class AgentResult:
    """Collect the final status, response, tool results, and conversation state."""
    status: str
    answer: str = ""
    events: List[AgentEvent] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    pending_approvals: List[ToolCall] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
