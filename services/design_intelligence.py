"""Optional HTTP client for the external Design Intelligence System (DIS).

This adapter only reports results returned by a reachable DIS API. It never
fabricates agent output or silently falls back to simulated reasoning.
Configure KUBEDOCK_DIS_URL to enable it; leave unset to keep DIS disabled.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DesignIntelligenceError(RuntimeError):
    """Raised when DIS is disabled, unreachable, or returns an invalid response."""


class DesignIntelligenceService:
    """Small stdlib-only adapter to the DIS FastAPI service."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 8.0):
        configured = base_url if base_url is not None else os.getenv("KUBEDOCK_DIS_URL", "")
        self.base_url = configured.strip().rstrip("/")
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("DIS URL must be an HTTP(S) base URL without credentials, query, or fragment.")
            local_hosts = {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme != "https" and parsed.hostname not in local_hosts:
                raise ValueError("Remote DIS endpoints must use HTTPS; HTTP is allowed only for localhost.")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def status(self) -> dict[str, Any]:
        """Return safe configuration status without making a network request."""
        return {
            "enabled": self.enabled,
            "configured": self.enabled,
            "service": "design-intelligence-system",
            "base_url": self.base_url or None,
        }

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.enabled:
            raise DesignIntelligenceError(
                "DIS is not configured. Set KUBEDOCK_DIS_URL to the deployed DIS API URL."
            )
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Accept": "application/json", **(
                {"Content-Type": "application/json"} if body is not None else {}
            )},
            method="GET" if body is None else "POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise DesignIntelligenceError("DIS response exceeded the 2 MB safety limit.")
            return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            raise DesignIntelligenceError(f"DIS API returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DesignIntelligenceError(
                "DIS API is unreachable. Verify KUBEDOCK_DIS_URL and the service health."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesignIntelligenceError("DIS API returned invalid JSON.") from exc

    def health(self) -> dict[str, Any]:
        result = self._request("/health")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise DesignIntelligenceError("DIS health response was not healthy.")
        return result

    def list_agents(self) -> list[dict[str, Any]]:
        result = self._request("/api/v1/agents")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise DesignIntelligenceError("DIS agent registry response has an invalid shape.")
        return result

    def run_design_review(
        self,
        brief: str,
        *,
        product_context: str = "KubeDock desktop application for Kubernetes, Docker, SSH and DevOps workflows.",
        constraints: list[str] | None = None,
        target_platforms: list[str] | None = None,
        project_id: str = "kubedock",
        autonomy: str = "high",
    ) -> dict[str, Any]:
        """Call the real DIS API and return its validated top-level run payload."""
        if not isinstance(brief, str) or not 5 <= len(brief.strip()) <= 10_000:
            raise ValueError("brief must contain between 5 and 10000 characters.")
        if autonomy not in {"low", "medium", "high"}:
            raise ValueError("autonomy must be low, medium, or high.")
        payload = {
            "brief": brief.strip(),
            "product_context": product_context[:10_000],
            "constraints": list(constraints or []),
            "target_platforms": list(target_platforms or ["desktop"]),
            "project_id": project_id,
            "autonomy": autonomy,
        }
        result = self._request("/api/v1/design/runs", payload)
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise DesignIntelligenceError("DIS design-run response has an invalid shape.")
        if not isinstance(result.get("quality"), dict):
            raise DesignIntelligenceError("DIS response omitted its quality evaluation.")
        return result
