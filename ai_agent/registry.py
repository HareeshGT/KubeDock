"""Validated registry for application-owned AI tools."""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Mapping
from .models import ToolSpec

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TYPE_MAP = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}

class ToolRegistry:
    """Stores explicit tool capabilities; the model never supplies executable code."""
    def __init__(self):
        """Create an empty registry of explicitly granted tool capabilities."""
        self._tools = {}
    def register(self, spec: ToolSpec) -> None:
        """Register a unique tool after validating its name and object schema."""
        if not isinstance(spec, ToolSpec):
            raise TypeError("Register a ToolSpec instance.")
        if not _TOOL_NAME_RE.fullmatch(spec.name):
            raise ValueError("Tool names must use lowercase snake_case (2-64 characters).")
        if spec.name in self._tools:
            raise ValueError("Tool already registered: " + spec.name)
        if not isinstance(spec.parameters, Mapping) or spec.parameters.get("type") != "object":
            raise ValueError("Tool parameters must be an object schema.")
        self._tools[spec.name] = spec
    def get(self, name: str):
        """Return a registered tool by name, or None when it is unavailable."""
        return self._tools.get(str(name or ""))
    def names(self):
        """Return registered tool names in stable sorted order."""
        return tuple(sorted(self._tools))
    def describe(self, allowed_tools=None):
        """Return model-facing schemas, optionally filtered by an allowlist."""
        allowed = set(allowed_tools) if allowed_tools is not None else None
        return [{"name": spec.name, "description": spec.description, "parameters": dict(spec.parameters)}
                for name in self.names() if allowed is None or name in allowed
                for spec in (self._tools[name],)]
    @staticmethod
    def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate JSON arguments and return a clean mapping or raise ValueError."""
        if not isinstance(arguments, Mapping):
            raise ValueError("Tool arguments must be a JSON object.")
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise ValueError("Tool schema is invalid.")
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ValueError("Missing required argument(s): " + ", ".join(map(str, missing)))
        if schema.get("additionalProperties", False) is False:
            unexpected = [key for key in arguments if key not in properties]
            if unexpected:
                raise ValueError("Unexpected argument(s): " + ", ".join(map(str, unexpected)))
        cleaned = {}
        for key, value in arguments.items():
            rule = properties.get(key)
            if not isinstance(rule, Mapping):
                raise ValueError("No schema is defined for argument: " + str(key))
            expected = rule.get("type")
            expected_type = _TYPE_MAP.get(expected)
            if expected_type is None:
                raise ValueError("Unsupported schema type for argument: " + str(key))
            if expected in {"integer", "number"} and isinstance(value, bool):
                raise ValueError("Argument has the wrong type: " + str(key))
            if not isinstance(value, expected_type):
                raise ValueError("Argument has the wrong type: " + str(key))
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError("Argument has an unsupported value: " + str(key))
            if expected == "string":
                if len(value) > int(rule.get("maxLength", 4096)) or len(value) < int(rule.get("minLength", 0)):
                    raise ValueError("Argument length is invalid: " + str(key))
            if expected in {"integer", "number"}:
                if "minimum" in rule and value < rule["minimum"]:
                    raise ValueError("Argument is below the minimum: " + str(key))
                if "maximum" in rule and value > rule["maximum"]:
                    raise ValueError("Argument exceeds the maximum: " + str(key))
            cleaned[key] = value
        return cleaned
    @staticmethod
    def serialize_result(value: Any) -> str:
        """Convert a tool result to JSON text suitable for model context."""
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
