"""Embedded KubeDock Web server.

The desktop application supplies its live Paramiko SSHClient through
``configure_runtime``. This module never creates an SSH connection of its
own; commands use KubeDock's managed SSH session helper from workers.py.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import hashlib
import ipaddress
import json
import logging
import os
import posixpath
import platform
import re
import secrets
import shlex
import socket
import subprocess
import stat
import threading
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from typing import Callable, Optional

import paramiko
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from pydantic import BaseModel

from themes import THEMES, apply_qss_to, apply_theme_vars, load_settings, save_settings
from utils import load_recent_instances
from webapp.otp_auth import OTPConfigurationError, check_totp, get_or_create_secret
from webapp.operation_audit import list_operations, record_operation
try:
    from workers import managed_exec_command
except ImportError:  # pragma: no cover
    managed_exec_command = None

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.path.expanduser("~")) / ".kubedock"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "logs/webapp.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("kubedeck.webapp")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.propagate = False

_SSH_PROVIDER: Optional[Callable[[], Optional[paramiko.SSHClient]]] = None
_RUNTIME_LOCK = threading.RLock()
_REQUEST_ID: ContextVar[str] = ContextVar("kubedeck_web_request_id", default="-")
_AUTH_USER: ContextVar[str] = ContextVar("kubedeck_web_auth_user", default="-")
_KUBE_CONTEXT: ContextVar[str] = ContextVar("kubedock_web_kube_context", default="")
_SESSION_CONTEXTS: dict[str, str] = {}
_APP_PROVIDER: Optional[Callable[[], object]] = None
_DESKTOP_BRIDGE = None
_MAC_CACHE: dict[str, tuple[float, str]] = {}
_MAC_CACHE_LOCK = threading.RLock()
_MAC_CACHE_TTL = 300.0
_MAC_RE = re.compile(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b|\b[0-9a-f]{2}(?:-[0-9a-f]{2}){5}\b")
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

def _validate_k8s_name(value: str, field: str) -> str:
    value = str(value or "").strip().lower()
    if not value or len(value) > 253 or not _K8S_NAME_RE.fullmatch(value):
        raise HTTPException(400, f"Invalid Kubernetes {field}")
    return value



class _DesktopBridge(QObject):
    requested = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requested.connect(self._dispatch, Qt.QueuedConnection)

    @pyqtSlot(object, object)
    def _dispatch(self, callback, future):
        if future.cancelled():
            return
        try:
            future.set_result(callback())
        except BaseException as exc:
            future.set_exception(exc)

    def invoke(self, callback, timeout: float = 5.0):
        if QThread.currentThread() == self.thread():
            return callback()
        future = Future()
        self.requested.emit(callback, future)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise HTTPException(504, "Desktop operation timed out") from exc


def configure_runtime(
    ssh_provider: Callable[[], Optional[paramiko.SSHClient]],
    app_provider: Optional[Callable[[], object]] = None,
) -> None:
    global _SSH_PROVIDER, _APP_PROVIDER, _DESKTOP_BRIDGE
    with _RUNTIME_LOCK:
        _SSH_PROVIDER = ssh_provider
        _APP_PROVIDER = app_provider
        if app_provider is not None:
            try:
                window = app_provider()
                if window is not None and (
                    _DESKTOP_BRIDGE is None or _DESKTOP_BRIDGE.parent() is not window
                ):
                    _DESKTOP_BRIDGE = _DesktopBridge(parent=window)
            except Exception:
                logger.exception("Unable to attach desktop control bridge")
                _DESKTOP_BRIDGE = None
        else:
            _DESKTOP_BRIDGE = None
    logger.info("Web runtime attached to KubeDock SSH provider")


def _ssh() -> paramiko.SSHClient:
    with _RUNTIME_LOCK:
        provider = _SSH_PROVIDER
    if provider is None:
        raise HTTPException(503, "KubeDock SSH runtime is not attached")
    try:
        client = provider()
    except Exception:
        logger.exception("SSH provider failed")
        raise HTTPException(503, "KubeDock SSH runtime is unavailable")
    if client is None:
        raise HTTPException(503, "Connect to a VM in KubeDock first")
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise HTTPException(503, "KubeDock SSH connection is not active")
    return client


_AUTH_FAIL_LOCK = threading.RLock()
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_MAX_FAILURES = 5
_AUTH_WINDOW_SECONDS = 300.0
_AUTH_LOCKOUT_SECONDS = 60.0
_SESSION_LOCK = threading.RLock()
_WEB_SESSIONS: dict[str, tuple[str, float]] = {}
_SESSION_TTL_SECONDS = 8 * 60 * 60
_SESSION_COOKIE = "kubedock_web_session"
_OTP_VERIFY_LOCK = threading.RLock()


class LoginRequest(BaseModel):
    code: str


def _ensure_secure_transport(request: Request) -> None:
    try:
        client = ipaddress.ip_address(_client_ip(request))
    except ValueError:
        client = None
    if request.url.scheme != "https" and not (client and client.is_loopback):
        raise HTTPException(
            403,
            "HTTPS is required for remote WebApp login. Configure "
            "WEBAPP_TLS_CERTFILE and WEBAPP_TLS_KEYFILE, or use a secure tunnel.",
        )


def _new_session(username: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires = perf_counter() + _SESSION_TTL_SECONDS
    with _SESSION_LOCK:
        now = perf_counter()
        for old_token, (_user, expiry) in list(_WEB_SESSIONS.items()):
            if expiry <= now:
                _WEB_SESSIONS.pop(old_token, None)
                _SESSION_CONTEXTS.pop(old_token, None)
        _WEB_SESSIONS[token] = (username, expires)
    return token, _SESSION_TTL_SECONDS


def require_login(request: Request) -> None:
    _ensure_secure_transport(request)
    token = request.cookies.get(_SESSION_COOKIE, "")
    username = None
    now = perf_counter()
    if token:
        with _SESSION_LOCK:
            session = _WEB_SESSIONS.get(token)
            if session and session[1] > now:
                username = session[0]
            elif session:
                _WEB_SESSIONS.pop(token, None)
                _SESSION_CONTEXTS.pop(token, None)
    if not username:
        raise HTTPException(401, "Authentication required")
    request.state.auth_user = username
    _AUTH_USER.set(username)
    _KUBE_CONTEXT.set(_SESSION_CONTEXTS.get(token, ""))
    _log_client_identity(request, username)


def _auth_block_seconds(client_ip: str) -> int:
    now = perf_counter()
    with _AUTH_FAIL_LOCK:
        failures = [t for t in _AUTH_FAILURES.get(client_ip, []) if now - t < _AUTH_WINDOW_SECONDS]
        _AUTH_FAILURES[client_ip] = failures
        if len(failures) < _AUTH_MAX_FAILURES:
            return 0
        remaining = _AUTH_LOCKOUT_SECONDS - (now - failures[-_AUTH_MAX_FAILURES])
        return max(1, int(remaining)) if remaining > 0 else 0


def _record_auth_failure(client_ip: str) -> None:
    now = perf_counter()
    with _AUTH_FAIL_LOCK:
        failures = [t for t in _AUTH_FAILURES.get(client_ip, []) if now - t < _AUTH_WINDOW_SECONDS]
        failures.append(now)
        _AUTH_FAILURES[client_ip] = failures[-_AUTH_MAX_FAILURES:]


def _clear_auth_failures(client_ip: str) -> None:
    with _AUTH_FAIL_LOCK:
        _AUTH_FAILURES.pop(client_ip, None)


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client and client.host else "-"


def _safe_user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "-").replace("\r", "").replace("\n", "")[:1000]


def _lookup_client_mac(client_ip: str) -> str:
    """Best-effort MAC lookup from the KubeDock host's local neighbor/ARP table.

    MAC addresses are normally available only when the requesting device is on
    the same Layer-2 network as the KubeDock host. Forwarded MAC/IP headers are
    intentionally ignored.
    """
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return "N/A"

    if not isinstance(ip, ipaddress.IPv4Address):
        return "N/A"
    if ip.is_loopback or not ip.is_private:
        return "N/A"

    now = perf_counter()
    with _MAC_CACHE_LOCK:
        cached = _MAC_CACHE.get(client_ip)
        if cached and now - cached[0] < _MAC_CACHE_TTL:
            return cached[1]

    system = platform.system()
    commands: list[list[str]] = []
    if system == "Linux":
        commands.append(["ip", "neigh", "show", client_ip])
    elif system == "Darwin":
        commands.append(["arp", "-n", client_ip])
    elif system == "Windows":
        commands.append(["arp", "-a", client_ip])
    else:
        commands.extend(
            [
                ["ip", "neigh", "show", client_ip],
                ["arp", "-n", client_ip],
                ["arp", "-a", client_ip],
            ]
        )

    mac = "N/A"
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue

        match = _MAC_RE.search(result.stdout or "")
        if match:
            mac = match.group(0).replace("-", ":").lower()
            break

    with _MAC_CACHE_LOCK:
        _MAC_CACHE[client_ip] = (now, mac)
    return mac


def _log_client_identity(request: Request, username: str) -> None:
    client_ip = _client_ip(request)
    mac = _lookup_client_mac(client_ip)
    user_agent = _safe_user_agent(request)
    logger.info(
        "[req=%s] AUTH user=%r client_ip=%s client_mac=%s user_agent=%r",
        _REQUEST_ID.get(),
        username,
        client_ip,
        mac,
        user_agent,
    )



def _exec(
    ssh: paramiko.SSHClient,
    command: str,
    timeout: int = 30,
    input_data: Optional[bytes] = None,
    sensitive_output: bool = False,
) -> tuple[int, str, str]:
    request_id = _REQUEST_ID.get()
    started = perf_counter()

    def run_channel(stdout, stderr):
        channel = stdout.channel
        timed_out = threading.Event()

        def abort_channel():
            timed_out.set()
            try:
                channel.close()
            except Exception:
                pass

        timer = threading.Timer(max(0.1, float(timeout)), abort_channel)
        timer.daemon = True
        timer.start()
        try:
            try:
                channel.settimeout(max(0.1, float(timeout)))
            except Exception:
                pass
            code = channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(
                    "kubectl command timed out after {} seconds".format(timeout)
                ) from exc
            raise
        finally:
            timer.cancel()

        if timed_out.is_set():
            raise TimeoutError(
                "kubectl command timed out after {} seconds".format(timeout)
            )
        return code, out, err

    logged_command = "<redacted sensitive command>" if sensitive_output else command
    logger.info(
        "[req=%s] KUBECTL START user=%r: %s",
        request_id, _AUTH_USER.get(), logged_command
    )
    try:
        if managed_exec_command is None:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            try:
                if input_data is not None:
                    stdin.write(input_data)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                code, out, err = run_channel(stdout, stderr)
            finally:
                for stream in (stdin, stdout, stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
        else:
            with managed_exec_command(
                ssh, command, channel_timeout=timeout
            ) as (_stdin, stdout, stderr):
                if input_data is not None:
                    _stdin.write(input_data)
                    _stdin.flush()
                    _stdin.channel.shutdown_write()
                code, out, err = run_channel(stdout, stderr)
    except Exception:
        logger.exception(
            "[req=%s] KUBECTL EXCEPTION user=%r: %s (%.1f ms)",
            request_id, _AUTH_USER.get(), logged_command,
            (perf_counter() - started) * 1000
        )
        raise

    duration_ms = (perf_counter() - started) * 1000
    logger.info(
        "[req=%s] KUBECTL END user=%r: exit=%s duration=%.1f ms stdout=%d bytes stderr=%d bytes",
        request_id, _AUTH_USER.get(), code, duration_ms, len(out), len(err)
    )
    if err.strip() and not sensitive_output:
        logger.info(
            "[req=%s] KUBECTL STDERR user=%r: %s",
            request_id, _AUTH_USER.get(), err.strip()[:4000]
        )
    return code, out, err


app = FastAPI(title="KubeDock Web")


def _audited_kubectl(action: str, target: str, command: str, timeout: int = 30) -> str:
    """Run a mutation and persist metadata only (never command output or secrets)."""
    started = perf_counter()
    status = "failed"
    error_code = None
    try:
        output = _kubectl(command, timeout=timeout)
        status = "succeeded"
        return output
    except HTTPException as exc:
        error_code = int(exc.status_code)
        raise
    except Exception:
        error_code = 500
        raise
    finally:
        try:
            record_operation(
                username=_AUTH_USER.get(),
                category="kubernetes",
                action=action,
                target=target,
                status=status,
                duration_ms=round((perf_counter() - started) * 1000),
                error_code=error_code,
            )
        except Exception:
            # Do not mask the Kubernetes result if the audit store is unavailable.
            logger.exception("Unable to persist operation audit record")


@app.get("/api/operations")
def operations(
    limit: int = 50,
    _: None = Depends(require_login),
) -> list:
    """Return recent action metadata without command output or secret values."""
    return list_operations(limit=max(1, min(int(limit), 200)))


def _parse_json_output(output: str):
    """Parse kubectl JSON even when the remote shell prepends/appends text.

    Some SSH environments emit shell banners, profile messages, or other
    stdout text around a command's actual output. That is harmless for a
    human but breaks json.loads(). Prefer the complete payload first, then
    recover a JSON object/array embedded in the output.
    """
    text = output.lstrip("\ufeff \r\n\t")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        trailing = text[index + end:].strip()
        if not trailing:
            return value
        # Allow harmless text after an otherwise valid JSON payload.
        return value

    raise json.JSONDecodeError("No JSON object or array found", text, 0)


def _kubectl(args: str, timeout: int = 30, json_output: bool = False, input_data: Optional[bytes] = None):
    try:
        context = _KUBE_CONTEXT.get()
        # Context selection is per authenticated web session; it never mutates
        # the remote user's shared kubeconfig current-context.
        prefix = f"--context {shlex.quote(context)} " if context and not args.lstrip().startswith("config ") else ""
        code, out, err = _exec(_ssh(), f"kubectl {prefix}{args}", timeout, input_data=input_data, sensitive_output=input_data is not None)
    except TimeoutError as exc:
        raise HTTPException(504, str(exc))
    if code != 0:
        logger.warning(
            "[req=%s] kubectl failed user=%r (%s): %s",
            _REQUEST_ID.get(),
            _AUTH_USER.get(),
            code,
            "input-based operation failed; details suppressed from logs" if input_data is not None else err.strip(),
        )
        # Several mutating/log commands intentionally use 2>&1, so kubectl
        # diagnostics may be in stdout rather than stderr.
        detail = err.strip() or out.strip()
        raise HTTPException(502, detail or "kubectl command failed")
    if not json_output:
        return out
    try:
        return _parse_json_output(out)
    except json.JSONDecodeError:
        logger.error(
            "[req=%s] kubectl returned invalid JSON user=%r stdout_prefix=%r",
            _REQUEST_ID.get(),
            _AUTH_USER.get(),
            out[:1000],
        )
        raise HTTPException(502, "kubectl returned non-JSON output")


@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    _ensure_secure_transport(request)
    client_ip = _client_ip(request)
    retry_after = _auth_block_seconds(client_ip)
    if retry_after:
        raise HTTPException(429, "Too many login attempts; try again later.",
                            headers={"Retry-After": str(retry_after)})
    try:
        secret = get_or_create_secret()
    except OTPConfigurationError:
        raise HTTPException(503, "OTP authentication is not configured. Open KubeDock Settings and ensure the OS credential vault is available.")

    with _OTP_VERIFY_LOCK:
        settings = load_settings() or {}
        try:
            previous = settings.get("webapp_totp_last_counter")
            last_counter = int(previous) if previous is not None else None
        except (TypeError, ValueError):
            last_counter = None
        valid, reason, accepted_counter = check_totp(
            secret, body.code, last_accepted_counter=last_counter
        )
        if not valid:
            _record_auth_failure(client_ip)
            retry_after = _auth_block_seconds(client_ip)
            if retry_after:
                raise HTTPException(429, "Too many login attempts; try again later.",
                                    headers={"Retry-After": str(retry_after)})
            messages = {
                "invalid": "Invalid OTP. Enter the current six-digit code.",
                "expired": "OTP expired. Use the newly displayed code.",
                "replayed": "This OTP has already been used. Wait for the next code.",
            }
            raise HTTPException(401, messages.get(reason, "OTP authentication failed."))
        try:
            save_settings(webapp_totp_last_counter=accepted_counter)
        except Exception:
            logger.exception("Unable to persist WebApp OTP replay state")
            raise HTTPException(503, "Unable to securely record OTP use; try again.")

    _clear_auth_failures(client_ip)
    username = str(settings.get("webapp_username", "")).strip() or "webapp"
    token, ttl = _new_session(username)
    response.set_cookie(
        key=_SESSION_COOKIE, value=token, max_age=ttl, httponly=True,
        secure=request.url.scheme == "https", samesite="strict", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "expires_in": ttl}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    with _SESSION_LOCK:
        token = request.cookies.get(_SESSION_COOKIE, "")
        _WEB_SESSIONS.pop(token, None)
        _SESSION_CONTEXTS.pop(token, None)
    response.delete_cookie(key=_SESSION_COOKIE, path="/", httponly=True,
                           secure=request.url.scheme == "https", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@app.get("/api/session")
def session_info(request: Request, _: None = Depends(require_login)) -> dict:
    return {"authenticated": True, "user": getattr(request.state, "auth_user", "webapp")}


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    request_id = secrets.token_hex(4)
    request_token = _REQUEST_ID.set(request_id)
    cookie_token = request.cookies.get(_SESSION_COOKIE, "")
    with _SESSION_LOCK:
        session = _WEB_SESSIONS.get(cookie_token)
        authenticated_user = session[0] if session and session[1] > perf_counter() else "-"
        session_context = _SESSION_CONTEXTS.get(cookie_token, "")
    auth_token = _AUTH_USER.set(authenticated_user)
    context_token = _KUBE_CONTEXT.set(session_context)
    started = perf_counter()
    query = request.url.query
    target = request.url.path + (f"?{query}" if query else "")
    client_ip = _client_ip(request)

    logger.info(
        "[req=%s] HTTP START: %s %s client_ip=%s user_agent=%r",
        request_id,
        request.method,
        target,
        client_ip,
        _safe_user_agent(request),
    )

    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            if origin:
                from urllib.parse import urlsplit
                parsed_origin = urlsplit(origin)
                host = request.headers.get("host", "")
                if parsed_origin.netloc.lower() != host.lower() or parsed_origin.scheme != request.url.scheme:
                    return JSONResponse(status_code=403, content={"detail": "Cross-origin state-changing requests are not allowed"})
        response = await call_next(request)
        duration_ms = (perf_counter() - started) * 1000
        username = getattr(request.state, "auth_user", "-")
        logger.info(
            "[req=%s] HTTP END user=%r: %s %s -> %s duration=%.1f ms client_ip=%s",
            request_id,
            username,
            request.method,
            target,
            response.status_code,
            duration_ms,
            client_ip,
        )
        return response
    except Exception:
        username = getattr(request.state, "auth_user", "-")
        logger.exception(
            "[req=%s] HTTP EXCEPTION user=%r: %s %s client_ip=%s (%.1f ms)",
            request_id,
            username,
            request.method,
            target,
            client_ip,
            (perf_counter() - started) * 1000,
        )
        raise
    finally:
        _KUBE_CONTEXT.reset(context_token)
        _AUTH_USER.reset(auth_token)
        _REQUEST_ID.reset(request_token)


def _ready(status_obj: dict) -> str:
    cs = status_obj.get("containerStatuses") or []
    return f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)}"


def _restarts(status_obj: dict) -> int:
    return sum(
        c.get("restartCount", 0)
        for c in (status_obj.get("containerStatuses") or [])
    )


def _ns(namespace: Optional[str]) -> str:
    return (
        f"-n {shlex.quote(namespace)}"
        if namespace and namespace != "all"
        else "-A"
    )


@app.get("/api/health")
def health(_: None = Depends(require_login)) -> dict:
    code, *_ = _exec(
        _ssh(),
        "kubectl version --client=true -o json 2>&1",
        10,
    )
    return {"ssh_ok": code == 0}


@app.get("/api/namespaces")
def namespaces(_: None = Depends(require_login)) -> list:
    data = _kubectl("get namespaces -o json", json_output=True)
    return [x["metadata"]["name"] for x in data.get("items", [])]


@app.get("/api/pods")
def pods(
    namespace: Optional[str] = None,
    _: None = Depends(require_login),
) -> list:
    data = _kubectl(
        f"get pods {_ns(namespace)} -o json",
        json_output=True,
    )
    result = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        stat = item.get("status", {})
        spec = item.get("spec", {})
        result.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "phase": stat.get("phase"),
                "ready": _ready(stat),
                "restarts": _restarts(stat),
                "node": spec.get("nodeName"),
                "containers": [
                    c.get("name") for c in spec.get("containers", [])
                ],
                "startTime": stat.get("startTime"),
            }
        )
    return result


@app.get("/api/deployments")
def deployments(
    namespace: Optional[str] = None,
    _: None = Depends(require_login),
) -> list:
    data = _kubectl(
        f"get deployments {_ns(namespace)} -o json",
        json_output=True,
    )
    result = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        stat = item.get("status", {})
        spec = item.get("spec", {})
        result.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "replicas": spec.get("replicas", 0),
                "ready": stat.get("readyReplicas", 0),
                "available": stat.get("availableReplicas", 0),
                "updated": stat.get("updatedReplicas", 0),
            }
        )
    return result


@app.get("/api/services")
def services(
    namespace: Optional[str] = None,
    _: None = Depends(require_login),
) -> list:
    data = _kubectl(
        f"get services {_ns(namespace)} -o json",
        json_output=True,
    )
    result = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        ports = [
            f"{p.get('port')}:{p.get('targetPort')}/{p.get('protocol', 'TCP')}"
            for p in spec.get("ports", [])
        ]
        result.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "type": spec.get("type"),
                "clusterIP": spec.get("clusterIP"),
                "ports": ports,
            }
        )
    return result


@app.get("/api/nodes")
def nodes(_: None = Depends(require_login)) -> list:
    data = _kubectl("get nodes -o json", json_output=True)
    result = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        stat = item.get("status", {})
        conditions = {
            c.get("type"): c.get("status")
            for c in stat.get("conditions", [])
        }
        result.append(
            {
                "name": meta.get("name"),
                "ready": conditions.get("Ready") == "True",
                "cpu": stat.get("capacity", {}).get("cpu"),
                "memory": stat.get("capacity", {}).get("memory"),
                "version": stat.get("nodeInfo", {}).get("kubeletVersion"),
            }
        )
    return result


@app.get("/api/events")
def events(
    namespace: Optional[str] = None,
    _: None = Depends(require_login),
) -> list:
    data = _kubectl(
        f"get events {_ns(namespace)} --sort-by=.lastTimestamp -o json",
        json_output=True,
    )
    result = []
    for item in data.get("items", []):
        obj = item.get("involvedObject", {})
        result.append(
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "message": item.get("message"),
                "object": f"{obj.get('kind', '')}/{obj.get('name', '')}",
                "namespace": item.get("metadata", {}).get("namespace"),
                "lastTimestamp": item.get("lastTimestamp")
                or item.get("eventTime"),
                "count": item.get("count", 1),
            }
        )
    return list(reversed(result))[:100]


@app.get("/api/logs/{namespace}/{pod}")
def logs(
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    tail: int = 200,
    _: None = Depends(require_login),
) -> dict:
    namespace = _validate_k8s_name(namespace, "namespace")
    pod = _validate_k8s_name(pod, "pod")
    if container:
        container = _validate_k8s_name(container, "container")
    tail = max(1, min(tail, 2000))
    container_flag = f"-c {shlex.quote(container)} " if container else ""
    out = _kubectl(
        f"logs {shlex.quote(pod)} -n {shlex.quote(namespace)} "
        f"{container_flag}--tail={tail} 2>&1",
        timeout=20,
    )
    return {"logs": out}


class ScaleRequest(BaseModel):
    replicas: int


@app.post("/api/deployments/{namespace}/{name}/restart")
def restart(
    namespace: str,
    name: str,
    _: None = Depends(require_login),
) -> dict:
    namespace = _validate_k8s_name(namespace, "namespace")
    name = _validate_k8s_name(name, "deployment")
    out = _audited_kubectl(
        "restart", f"deployment/{namespace}/{name}",
        f"rollout restart deployment/{shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} 2>&1",
    )
    return {"ok": True, "output": out.strip()}


@app.post("/api/deployments/{namespace}/{name}/scale")
def scale(
    namespace: str,
    name: str,
    body: ScaleRequest,
    _: None = Depends(require_login),
) -> dict:
    namespace = _validate_k8s_name(namespace, "namespace")
    name = _validate_k8s_name(name, "deployment")
    if body.replicas < 0 or body.replicas > 100:
        raise HTTPException(400, "replicas must be between 0 and 100")
    out = _audited_kubectl(
        "scale", f"deployment/{namespace}/{name}",
        f"scale deployment/{shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} --replicas={body.replicas} 2>&1",
    )
    return {"ok": True, "output": out.strip()}


@app.post("/api/pods/{namespace}/{name}/delete")
def delete(
    namespace: str,
    name: str,
    _: None = Depends(require_login),
) -> dict:
    namespace = _validate_k8s_name(namespace, "namespace")
    name = _validate_k8s_name(name, "pod")
    out = _audited_kubectl(
        "delete", f"pod/{namespace}/{name}",
        f"delete pod {shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} 2>&1",
    )
    return {"ok": True, "output": out.strip()}



# ---------- Remote control APIs ----------
# These APIs operate only on the SSH connection already owned by KubeDock.
# They never create a second SSH connection or accept browser-supplied credentials.

def _audit_remote(action: str, target: str, status: str, started: float, error_code: Optional[int] = None) -> None:
    try:
        record_operation(
            username=_AUTH_USER.get(),
            category="remote",
            action=action,
            target=target,
            status=status,
            duration_ms=round((perf_counter() - started) * 1000),
            error_code=error_code,
        )
    except Exception:
        logger.exception("Unable to persist remote operation audit record")


def _run_remote(command: str, timeout: int = 20, sensitive: bool = False) -> tuple[int, str, str]:
    try:
        return _exec(_ssh(), command, timeout=timeout, sensitive_output=sensitive)
    except TimeoutError as exc:
        raise HTTPException(504, "Remote command timed out") from exc


def _safe_remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\x00" in value:
        raise HTTPException(400, "Invalid remote path")
    value = value.replace("\\", "/")
    windows_drive = re.match(r"^[A-Za-z]:/", value)
    if not value.startswith("/") and not windows_drive:
        raise HTTPException(400, "Remote paths must be absolute")
    if any(part == ".." for part in value.split("/")):
        raise HTTPException(400, "Path traversal is not allowed")
    if windows_drive and re.fullmatch(r"[A-Za-z]:/", value):
        return value[:2].upper() + "/"
    return posixpath.normpath(value)


def _sftp():
    try:
        return _ssh().open_sftp()
    except Exception:
        logger.exception("Unable to open SFTP channel")
        raise HTTPException(502, "SFTP is unavailable for the connected machine")


@app.get("/api/remote/target")
def remote_target(_: None = Depends(require_login)) -> dict:
    """Return facts about the currently connected KubeDock SSH target."""
    ssh = _ssh()
    transport = ssh.get_transport()
    peer = None
    try:
        peer = transport.getpeername() if transport else None
    except Exception:
        peer = None
    code, output, error = _run_remote(
        "printf 'HOSTNAME='; hostname 2>/dev/null; "
        "printf '\\nOS='; uname -srm 2>/dev/null || ver 2>&1; "
        "printf '\\nUPTIME='; uptime 2>/dev/null; "
        "printf '\\nCPU_COUNT='; getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null; "
        "printf '\\nMEMORY='; free -b 2>/dev/null | head -2; "
        "printf '\\nROOT_DISK='; df -Pk / 2>/dev/null | tail -1; "
        "printf '\\nDOCKER='; command -v docker 2>/dev/null || where docker 2>NUL",
        timeout=10,
    )
    if code != 0 and not output.strip():
        raise HTTPException(502, "Unable to inspect the connected machine")
    return {
        "connected": bool(transport and transport.is_active()),
        "target": str(peer[0]) if isinstance(peer, tuple) and peer else "KubeDock active SSH connection",
        "port": peer[1] if isinstance(peer, tuple) and len(peer) > 1 else None,
        "output": output.strip(),
        "warning": error.strip()[:500] if error.strip() else None,
    }


@app.get("/api/kube/contexts")
def kube_contexts(request: Request, _: None = Depends(require_login)) -> dict:
    code, output, error = _run_remote("kubectl config get-contexts -o name 2>&1", timeout=10)
    if code != 0:
        raise HTTPException(502, error.strip() or output.strip() or "Unable to list Kubernetes contexts")
    contexts = [line.strip() for line in output.splitlines() if line.strip()]
    token = request.cookies.get(_SESSION_COOKIE, "")
    with _SESSION_LOCK:
        session = _WEB_SESSIONS.get(token)
    if not session:
        raise HTTPException(401, "Authentication required")
    selected = _SESSION_CONTEXTS.get(token, "")
    current_code, current_output, _ = _run_remote("kubectl config current-context 2>/dev/null", timeout=10)
    actual = current_output.strip() if current_code == 0 else None
    return {
        "contexts": contexts,
        "session_context": selected or actual,
        "shared_kubeconfig_context": actual,
        "scope": "web-session",
        "note": "Selecting a context affects this web session only; it does not change the shared kubeconfig.",
    }


class ContextSelection(BaseModel):
    context: str
    scope: str = "session"
    confirm_shared_change: bool = False


@app.post("/api/kube/contexts/select")
def select_kube_context(body: ContextSelection, request: Request, _: None = Depends(require_login)) -> dict:
    context = str(body.context or "").strip()
    scope = str(body.scope or "session").strip().lower()
    if scope not in {"session", "shared"}:
        raise HTTPException(400, "Context scope must be session or shared")
    if len(context) > 253 or any(ord(ch) < 32 for ch in context):
        raise HTTPException(400, "Invalid Kubernetes context")
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not context:
        if scope != "session":
            raise HTTPException(400, "The shared context cannot be reset to an unnamed value")
        with _SESSION_LOCK:
            if token not in _WEB_SESSIONS:
                raise HTTPException(401, "Authentication required")
            _SESSION_CONTEXTS.pop(token, None)
        _KUBE_CONTEXT.set("")
        _audit_remote("reset-kubernetes-context", "shared kubeconfig default", "succeeded", perf_counter())
        return {"ok": True, "context": None, "scope": "web-session", "note": "Using the shared kubeconfig default without changing it."}
    code, output, error = _run_remote("kubectl config get-contexts -o name 2>&1", timeout=10)
    if code != 0:
        raise HTTPException(502, error.strip() or output.strip() or "Unable to validate Kubernetes context")
    if context not in {line.strip() for line in output.splitlines()}:
        raise HTTPException(404, "Kubernetes context was not found")
    if scope == "shared":
        if body.confirm_shared_change is not True:
            raise HTTPException(409, "Explicit confirmation is required to change the shared kubeconfig context")
        code, output, error = _run_remote("kubectl config use-context " + shlex.quote(context) + " 2>&1", timeout=15)
        if code != 0:
            raise HTTPException(502, (error or output).strip()[:1000] or "Unable to change the shared kubeconfig context")
    with _SESSION_LOCK:
        if token not in _WEB_SESSIONS:
            raise HTTPException(401, "Authentication required")
        _SESSION_CONTEXTS[token] = context
    _KUBE_CONTEXT.set(context)
    _audit_remote("select-kubernetes-context-" + scope, context, "succeeded", perf_counter())
    return {"ok": True, "context": context, "scope": "web-session" if scope == "session" else "shared-kubeconfig"}


def _validate_config_entry_name(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,253}", value):
        raise HTTPException(400, f"Invalid {field}")
    return value


@app.get("/api/kube/config/entries")
def kube_config_entries(_: None = Depends(require_login)) -> dict:
    results = {}
    for key, command in (
        ("clusters", "kubectl config get-clusters 2>&1"),
        ("users", "kubectl config get-users 2>&1"),
    ):
        code, output, error = _run_remote(command, timeout=10)
        if code != 0:
            raise HTTPException(502, (error or output).strip()[:1000] or f"Unable to list configured {key}")
        results[key] = [line.strip() for line in output.splitlines() if line.strip() and line.strip().upper() != "NAME"]
    return results


class ContextRegisterRequest(BaseModel):
    context: str
    cluster: str
    user: str
    namespace: str = "default"
    confirm_shared_change: bool = False


@app.get("/api/kube/contexts/inspect")
def inspect_kube_context(context: str, _: None = Depends(require_login)) -> dict:
    context = _validate_config_entry_name(context, "context name")
    command = "kubectl config view --minify --context " + shlex.quote(context) + " -o json 2>&1"
    code, output, error = _run_remote(command, timeout=12)
    if code != 0:
        raise HTTPException(404, (error or output).strip()[:1000] or "Unable to inspect Kubernetes context")
    try:
        config = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Kubernetes returned an invalid context configuration") from exc
    clusters = config.get("clusters") or []
    users = config.get("users") or []
    contexts = config.get("contexts") or []
    cluster = (clusters[0].get("cluster") or {}) if clusters else {}
    user = (users[0].get("user") or {}) if users else {}
    context_info = (contexts[0].get("context") or {}) if contexts else {}
    auth_methods = [key for key in ("token", "tokenFile", "client-certificate", "client-key", "client-certificate-data", "exec", "auth-provider", "username", "password") if key in user]
    status_code, status_output, status_error = _run_remote("kubectl --context " + shlex.quote(context) + " cluster-info 2>&1", timeout=20)
    return {
        "context": context,
        "cluster": clusters[0].get("name") if clusters else None,
        "endpoint": cluster.get("server"),
        "user": users[0].get("name") if users else None,
        "namespace": context_info.get("namespace", "default"),
        "auth_methods": auth_methods,
        "connection_valid": status_code == 0,
        "connection_status": (status_output or status_error).strip()[:2000],
        "secrets_exposed": False,
    }


@app.post("/api/kube/contexts/register")
def register_kube_context(body: ContextRegisterRequest, _: None = Depends(require_login)) -> dict:
    context = _validate_config_entry_name(body.context, "context name")
    cluster = _validate_config_entry_name(body.cluster, "cluster name")
    user = _validate_config_entry_name(body.user, "user name")
    namespace = _validate_k8s_name(body.namespace, "namespace")
    if body.confirm_shared_change is not True:
        raise HTTPException(409, "Explicit confirmation is required to write the shared kubeconfig")
    clusters_code, clusters_output, clusters_error = _run_remote("kubectl config get-clusters 2>&1", timeout=10)
    users_code, users_output, users_error = _run_remote("kubectl config get-users 2>&1", timeout=10)
    if clusters_code != 0 or users_code != 0:
        raise HTTPException(502, (clusters_error or users_error or "Unable to validate cluster/user entries").strip()[:1000])
    configured_clusters = {line.strip() for line in clusters_output.splitlines() if line.strip() and line.strip().upper() != "NAME"}
    configured_users = {line.strip() for line in users_output.splitlines() if line.strip() and line.strip().upper() != "NAME"}
    if cluster not in configured_clusters:
        raise HTTPException(404, "Cluster entry does not exist in the remote kubeconfig")
    if user not in configured_users:
        raise HTTPException(404, "User entry does not exist in the remote kubeconfig")
    started = perf_counter()
    command = "kubectl config set-context " + shlex.quote(context) + " --cluster=" + shlex.quote(cluster) + " --user=" + shlex.quote(user) + " --namespace=" + shlex.quote(namespace) + " 2>&1"
    code, output, error = _run_remote(command, timeout=15)
    if code != 0:
        _audit_remote("register-kubernetes-context", context, "failed", started, 502)
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to register Kubernetes context")
    _audit_remote("register-kubernetes-context", context, "succeeded", started)
    return {"ok": True, "context": context, "scope": "shared-kubeconfig", "output": output.strip()}


class ContextDeleteRequest(BaseModel):
    context: str
    confirm_context: str
    confirm_shared_change: bool = False


@app.post("/api/kube/contexts/delete")
def delete_kube_context(body: ContextDeleteRequest, request: Request, _: None = Depends(require_login)) -> dict:
    context = _validate_config_entry_name(body.context, "context name")
    if body.confirm_context != context or body.confirm_shared_change is not True:
        raise HTTPException(409, "Confirm the exact context name and shared kubeconfig change")
    code, current, error = _run_remote("kubectl config current-context 2>&1", timeout=10)
    if code != 0:
        raise HTTPException(502, (error or current).strip()[:1000] or "Unable to read current shared context")
    if current.strip() == context:
        raise HTTPException(409, "Switch the shared current context before removing it")
    started = perf_counter()
    code, output, error = _run_remote("kubectl config delete-context " + shlex.quote(context) + " 2>&1", timeout=15)
    if code != 0:
        _audit_remote("delete-kubernetes-context", context, "failed", started, 502)
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to delete Kubernetes context")
    with _SESSION_LOCK:
        for session_token, selected in list(_SESSION_CONTEXTS.items()):
            if selected == context:
                _SESSION_CONTEXTS.pop(session_token, None)
    _audit_remote("delete-kubernetes-context", context, "succeeded", started)
    return {"ok": True, "context": context, "scope": "shared-kubeconfig", "output": output.strip()}


@app.get("/api/remote/files")
def remote_files(path: str = "/", _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(path)
    sftp = _sftp()
    try:
        attrs = sftp.lstat(path)
        if stat.S_ISLNK(attrs.st_mode):
            raise HTTPException(400, "Refusing to follow a symbolic link")
        if not stat.S_ISDIR(attrs.st_mode):
            raise HTTPException(400, "Path is not a directory")
        entries = []
        for item in sftp.listdir_attr(path):
            is_link = stat.S_ISLNK(item.st_mode)
            entries.append({
                "name": item.filename,
                "path": posixpath.join(path, item.filename),
                "kind": "symlink" if is_link else ("directory" if stat.S_ISDIR(item.st_mode) else "file"),
                "size": int(item.st_size or 0),
                "modified": int(item.st_mtime or 0),
                "permissions": oct(stat.S_IMODE(item.st_mode)),
            })
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].lower()))
        return {"path": path, "parent": posixpath.dirname(path) if path != "/" else None, "entries": entries}
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(403 if getattr(exc, "errno", None) == 13 else 404, "Unable to browse this remote path") from exc
    finally:
        sftp.close()


@app.get("/api/remote/files/read")
def remote_file_read(path: str, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(path)
    sftp = _sftp()
    try:
        attrs = sftp.lstat(path)
        if stat.S_ISLNK(attrs.st_mode) or not stat.S_ISREG(attrs.st_mode):
            raise HTTPException(400, "Only regular, non-symlink files can be read")
        if attrs.st_size > 1_000_000:
            raise HTTPException(413, "File is larger than the 1 MB text editor limit")
        with sftp.open(path, "rb") as handle:
            data = handle.read(1_000_001)
        if len(data) > 1_000_000:
            raise HTTPException(413, "File is larger than the 1 MB text editor limit")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(415, "File is not UTF-8 text") from exc
        return {"path": path, "content": content, "size": len(data)}
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(403 if getattr(exc, "errno", None) == 13 else 404, "Unable to read this remote file") from exc
    finally:
        sftp.close()


@app.put("/api/remote/files/write")
def remote_file_write(body: dict, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(body.get("path", ""))
    content = body.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
        raise HTTPException(413, "Text content must be at most 1 MB")
    started = perf_counter()
    sftp = _sftp()
    try:
        try:
            attrs = sftp.lstat(path)
            if stat.S_ISLNK(attrs.st_mode) or not stat.S_ISREG(attrs.st_mode):
                raise HTTPException(400, "Only regular, non-symlink files can be edited")
            if body.get("confirm_overwrite") != path:
                raise HTTPException(409, "Explicit overwrite confirmation is required")
        except FileNotFoundError:
            if body.get("create") is not True:
                raise HTTPException(404, "File does not exist; set create=true to create it")
        with sftp.open(path, "wb") as handle:
            handle.write(content.encode("utf-8"))
        _audit_remote("write-file", path, "succeeded", started)
        return {"ok": True, "path": path, "bytes_written": len(content.encode("utf-8"))}
    except HTTPException as exc:
        _audit_remote("write-file", path, "failed", started, exc.status_code)
        raise
    except OSError as exc:
        _audit_remote("write-file", path, "failed", started, 500)
        raise HTTPException(403, "Unable to write this remote file") from exc
    finally:
        sftp.close()


@app.post("/api/remote/files/mkdir")
def remote_file_mkdir(body: dict, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(body.get("path", ""))
    started = perf_counter()
    sftp = _sftp()
    try:
        sftp.mkdir(path)
        _audit_remote("create-directory", path, "succeeded", started)
        return {"ok": True, "path": path}
    except Exception as exc:
        _audit_remote("create-directory", path, "failed", started, 500)
        raise HTTPException(400, "Unable to create directory") from exc
    finally:
        sftp.close()


@app.post("/api/remote/files/create")
def remote_file_create(body: dict, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(body.get("path", ""))
    content = body.get("content", "")
    if path == "/" or not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
        raise HTTPException(400, "Provide a non-root path and UTF-8 content up to 1 MB")
    started = perf_counter()
    sftp = _sftp()
    try:
        try:
            sftp.lstat(path)
            raise HTTPException(409, "Destination already exists; use the editor to overwrite it explicitly")
        except FileNotFoundError:
            pass
        with sftp.open(path, "x") as handle:
            handle.write(content.encode("utf-8"))
        _audit_remote("create-file", path, "succeeded", started)
        return {"ok": True, "path": path, "bytes_written": len(content.encode("utf-8"))}
    except HTTPException as exc:
        _audit_remote("create-file", path, "failed", started, exc.status_code)
        raise
    except OSError as exc:
        _audit_remote("create-file", path, "failed", started, 500)
        raise HTTPException(403, "Unable to create file at this path") from exc
    finally:
        sftp.close()


@app.post("/api/remote/files/move")
def remote_file_move(body: dict, _: None = Depends(require_login)) -> dict:
    source = _safe_remote_path(body.get("source", ""))
    destination = _safe_remote_path(body.get("destination", ""))
    if source == "/" or destination == "/" or source == destination:
        raise HTTPException(400, "Source and destination must be different non-root paths")
    if body.get("confirm_source") != source or body.get("confirm_destination") != destination:
        raise HTTPException(409, "Confirm the exact source and destination paths")
    started = perf_counter()
    sftp = _sftp()
    try:
        sftp.lstat(source)
        parent = posixpath.dirname(destination)
        parent_attrs = sftp.lstat(parent)
        if stat.S_ISLNK(parent_attrs.st_mode) or not stat.S_ISDIR(parent_attrs.st_mode):
            raise HTTPException(400, "Destination parent must be a real directory")
        try:
            sftp.lstat(destination)
            raise HTTPException(409, "Destination already exists; refusing to overwrite it")
        except FileNotFoundError:
            pass
        sftp.rename(source, destination)
        _audit_remote("move-file", source + " -> " + destination, "succeeded", started)
        return {"ok": True, "source": source, "destination": destination}
    except HTTPException as exc:
        _audit_remote("move-file", source + " -> " + destination, "failed", started, exc.status_code)
        raise
    except OSError as exc:
        _audit_remote("move-file", source + " -> " + destination, "failed", started, 500)
        raise HTTPException(400, "Unable to move path; check permissions and destination") from exc
    finally:
        sftp.close()


@app.post("/api/remote/files/delete")
def remote_file_delete(body: dict, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(body.get("path", ""))
    if path == "/" or body.get("confirm_path") != path:
        raise HTTPException(400, "Confirm the exact path before deleting")
    started = perf_counter()
    sftp = _sftp()
    try:
        attrs = sftp.lstat(path)
        if stat.S_ISLNK(attrs.st_mode):
            sftp.remove(path)
        elif stat.S_ISDIR(attrs.st_mode):
            # Only empty directories can be removed; recursive deletion is deliberately excluded.
            sftp.rmdir(path)
        else:
            sftp.remove(path)
        _audit_remote("delete-file", path, "succeeded", started)
        return {"ok": True, "path": path}
    except HTTPException as exc:
        _audit_remote("delete-file", path, "failed", started, exc.status_code)
        raise
    except OSError as exc:
        _audit_remote("delete-file", path, "failed", started, 500)
        raise HTTPException(400, "Unable to delete path; directories must be empty") from exc
    finally:
        sftp.close()


@app.post("/api/remote/docker/resources/remove")
def remote_docker_resource_remove(body: dict, _: None = Depends(require_login)) -> dict:
    kind = str(body.get("kind", "")).strip().lower()
    identifier = str(body.get("identifier", "")).strip()
    if kind not in {"images", "volumes", "networks"}:
        raise HTTPException(400, "Only image, volume, and network removal is supported here")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}", identifier):
        raise HTTPException(400, "Invalid Docker resource identifier")
    target = f"{kind}/{identifier}"
    if body.get("confirm_target") != target:
        raise HTTPException(409, "Confirm the exact Docker resource identifier before removal")
    command_prefix = {"images": "image rm", "volumes": "volume rm", "networks": "network rm"}[kind]
    started = perf_counter()
    code, output, error = _run_remote("docker " + command_prefix + " " + shlex.quote(identifier), timeout=45)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("docker-remove-" + kind, target, status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Docker resource removal failed")
    return {"ok": True, "target": target, "output": output.strip()}


class DockerCreateRequest(BaseModel):
    image: str
    name: Optional[str] = None
    ports: Optional[list[str]] = None
    environment: Optional[dict[str, str]] = None


@app.post("/api/remote/docker/containers/create")
def remote_docker_create(body: DockerCreateRequest, _: None = Depends(require_login)) -> dict:
    image = str(body.image or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", image):
        raise HTTPException(400, "Invalid Docker image reference")
    name = str(body.name or "").strip()
    if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise HTTPException(400, "Invalid Docker container name")
    ports = list(body.ports or [])
    if len(ports) > 50:
        raise HTTPException(400, "At most 50 port mappings are supported")
    for mapping in ports:
        match = re.fullmatch(r"([0-9]{1,5}):([0-9]{1,5})", str(mapping))
        if not match or not all(1 <= int(part) <= 65535 for part in match.groups()):
            raise HTTPException(400, "Port mappings must use local:container numeric ports")
    environment = dict(body.environment or {})
    if len(environment) > 100:
        raise HTTPException(400, "At most 100 environment variables are supported")
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", str(key)):
            raise HTTPException(400, "Invalid environment variable name")
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise HTTPException(400, "Environment variable values must be strings up to 4096 characters")
    args = ["docker", "create"]
    if name:
        args += ["--name", name]
    for mapping in ports:
        args += ["-p", mapping]
    for key, value in environment.items():
        args += ["-e", key + "=" + value]
    args.append(image)
    command = " ".join(shlex.quote(arg) for arg in args)
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=45, sensitive=bool(environment))
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("docker-create-container", name or image, status, started, None if code == 0 else 502)
    if code != 0:
        detail = "Docker container creation failed; environment values were suppressed." if environment else (error or output).strip()[:1000]
        raise HTTPException(502, detail or "Docker container creation failed")
    return {"ok": True, "name": name or None, "image": image, "container_id": output.strip().splitlines()[0] if output.strip() else None}


@app.get("/api/remote/docker/stats")
def remote_docker_stats(_: None = Depends(require_login)) -> dict:
    code, output, error = _run_remote("docker stats --no-stream --format '{{json .}}' 2>&1", timeout=25)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to read Docker stats")
    items = []
    for line in output.splitlines():
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"target": "active KubeDock SSH machine", "items": items}


@app.get("/api/remote/docker/containers/{identifier}/inspect")
def remote_docker_inspect(identifier: str, _: None = Depends(require_login)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier):
        raise HTTPException(400, "Invalid container identifier")
    code, output, error = _run_remote("docker inspect " + shlex.quote(identifier) + " 2>&1", timeout=20)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to inspect container")
    try:
        result = json.loads(output)
        container = result[0] if isinstance(result, list) and result else result
    except (json.JSONDecodeError, IndexError) as exc:
        raise HTTPException(502, "Docker returned invalid inspect JSON") from exc
    config = container.get("Config") or {}
    env = config.get("Env") or []
    safe_env = []
    for entry in env:
        key, sep, value = str(entry).partition("=")
        if re.search(r"(?i)(pass(word)?|token|secret|credential|auth|private|api[_-]?key|access[_-]?key)", key):
            value = "<redacted>"
        safe_env.append(key + (sep + value if sep else ""))
    if "Env" in config:
        config["Env"] = safe_env
    container["Config"] = config
    return {"container": container, "secrets_redacted": True}


@app.post("/api/remote/docker/compose/action")
def remote_docker_compose_action(body: dict, _: None = Depends(require_login)) -> dict:
    project = str(body.get("project", "")).strip()
    action = str(body.get("action", "")).strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project):
        raise HTTPException(400, "Invalid Compose project name")
    if action not in {"start", "stop", "restart", "up", "down"}:
        raise HTTPException(400, "Unsupported Compose action")
    if action == "down" and body.get("confirm_target") != project:
        raise HTTPException(409, "Confirm the exact Compose project before bringing it down")
    code, output, error = _run_remote("docker compose ls --format json 2>&1", timeout=20)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to list Compose projects")
    try:
        projects = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Docker Compose returned invalid JSON") from exc
    match = next((item for item in projects if str(item.get("Name") or item.get("Project") or "") == project), None)
    if not match:
        raise HTTPException(404, "Compose project was not found")
    config_files = match.get("ConfigFiles") or match.get("ConfigFile")
    if isinstance(config_files, str):
        config_files = [part.strip() for part in config_files.split(",") if part.strip()]
    if not isinstance(config_files, list) or not config_files:
        raise HTTPException(409, "Compose project has no discoverable configuration file; use the desktop app")
    args = ["docker", "compose", "-p", project]
    for path in config_files:
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise HTTPException(400, "Compose configuration path is invalid")
        args += ["-f", path]
    args += ["up", "-d"] if action == "up" else ["down"] if action == "down" else [action]
    command = " ".join(shlex.quote(arg) for arg in args)
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=120)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("docker-compose-" + action, project, status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Compose operation failed")
    return {"ok": True, "project": project, "action": action, "output": output.strip()}


@app.get("/api/remote/docker/{kind}")
def remote_docker_list(kind: str, _: None = Depends(require_login)) -> dict:
    commands = {
        "containers": ["ps", "-a", "--format", "{{json .}}"],
        "images": ["images", "--format", "{{json .}}"],
        "volumes": ["volume", "ls", "--format", "{{json .}}"],
        "networks": ["network", "ls", "--format", "{{json .}}"],
        "compose": ["compose", "ls", "--format", "json"],
    }
    if kind not in commands:
        raise HTTPException(404, "Unsupported Docker resource type")
    command = "docker " + " ".join(shlex.quote(arg) for arg in commands[kind])
    code, output, error = _run_remote(command, timeout=25)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Docker command failed")
    items = []
    if kind == "compose":
        try:
            parsed = json.loads(output or "[]")
            items = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            items = [{"raw": line} for line in output.splitlines() if line.strip()]
    else:
        for line in output.splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"target": "active KubeDock SSH machine", "kind": kind, "items": items}


@app.get("/api/remote/docker/containers/{identifier}/logs")
def remote_docker_logs(identifier: str, tail: int = 200, _: None = Depends(require_login)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier):
        raise HTTPException(400, "Invalid container identifier")
    tail = max(1, min(int(tail), 2000))
    code, output, error = _run_remote(
        "docker logs --tail " + str(tail) + " " + shlex.quote(identifier) + " 2>&1",
        timeout=20,
    )
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Unable to read container logs")
    return {"container": identifier, "logs": output}


@app.post("/api/remote/docker/containers/{identifier}/{action}")
def remote_docker_action(identifier: str, action: str, body: dict, _: None = Depends(require_login)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier):
        raise HTTPException(400, "Invalid container identifier")
    if action not in {"start", "stop", "restart", "remove"}:
        raise HTTPException(400, "Unsupported container action")
    if action == "remove" and body.get("confirm_target") != identifier:
        raise HTTPException(409, "Confirm the exact container ID before removal")
    verb = "rm -f" if action == "remove" else action
    command = "docker " + verb + " " + shlex.quote(identifier)
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=45)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("docker-" + action, identifier, status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Docker operation failed")
    return {"ok": True, "action": action, "container": identifier, "output": output.strip()}




@app.get("/api/remote/files/download")
def remote_file_download(path: str, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(path)
    sftp = _sftp()
    try:
        attrs = sftp.lstat(path)
        if stat.S_ISLNK(attrs.st_mode) or not stat.S_ISREG(attrs.st_mode):
            raise HTTPException(400, "Only regular, non-symlink files can be downloaded")
        if attrs.st_size > 5_000_000:
            raise HTTPException(413, "File is larger than the 5 MB download limit")
        with sftp.open(path, "rb") as handle:
            data = handle.read(5_000_001)
        if len(data) > 5_000_000:
            raise HTTPException(413, "File is larger than the 5 MB download limit")
        return {"path": path, "content_base64": base64.b64encode(data).decode("ascii"), "size": len(data)}
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(403 if getattr(exc, "errno", None) == 13 else 404, "Unable to download this remote file") from exc
    finally:
        sftp.close()


@app.put("/api/remote/files/upload")
def remote_file_upload(body: dict, _: None = Depends(require_login)) -> dict:
    path = _safe_remote_path(body.get("path", ""))
    encoded = body.get("content_base64")
    if not isinstance(encoded, str) or len(encoded) > 7_000_000:
        raise HTTPException(413, "Upload must be at most 5 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise HTTPException(400, "Invalid base64 file content") from exc
    if len(data) > 5_000_000:
        raise HTTPException(413, "Upload must be at most 5 MB")
    started = perf_counter()
    sftp = _sftp()
    try:
        try:
            attrs = sftp.lstat(path)
            if stat.S_ISLNK(attrs.st_mode) or not stat.S_ISREG(attrs.st_mode):
                raise HTTPException(400, "Uploads may only replace regular, non-symlink files")
            if body.get("confirm_overwrite") != path:
                raise HTTPException(409, "Confirm the exact path to overwrite")
        except FileNotFoundError:
            if body.get("create") is not True:
                raise HTTPException(404, "File does not exist; set create=true to create it")
        with sftp.open(path, "wb") as handle:
            handle.write(data)
        _audit_remote("upload-file", path, "succeeded", started)
        return {"ok": True, "path": path, "bytes_written": len(data)}
    except HTTPException as exc:
        _audit_remote("upload-file", path, "failed", started, exc.status_code)
        raise
    except OSError as exc:
        _audit_remote("upload-file", path, "failed", started, 500)
        raise HTTPException(403, "Unable to upload file to this path") from exc
    finally:
        sftp.close()


def _remote_os_family() -> str:
    code, output, _ = _run_remote("uname -s", timeout=5)
    if code == 0:
        value = output.strip().lower()
        if "linux" in value:
            return "linux"
        if "darwin" in value:
            return "macos"
    code, output, _ = _run_remote('powershell -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"', timeout=5)
    if code == 0 and output.strip():
        return "windows"
    return "unknown"


@app.get("/api/remote/system/identity")
def remote_system_identity(_: None = Depends(require_login)) -> dict:
    family = _remote_os_family()
    command = 'powershell -NoProfile -Command "hostname"' if family == "windows" else "hostname"
    code, output, error = _run_remote(command, timeout=8)
    hostname = output.strip().splitlines()[0] if code == 0 and output.strip() else ""
    if not hostname:
        raise HTTPException(502, (error or "Unable to identify the remote machine").strip()[:500])
    return {"hostname": hostname, "os": family, "target": "active KubeDock SSH machine"}


@app.get("/api/remote/system/{kind}")
def remote_system_inspection(kind: str, _: None = Depends(require_login)) -> dict:
    family = _remote_os_family()
    commands_by_os = {
        "linux": {
            "processes": "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu 2>&1 | head -51",
            "disk": "df -h 2>&1 | head -51",
            "ports": "ss -lntup 2>&1 | head -101",
            "services": "systemctl --no-pager --type=service --state=running 2>&1 | head -101",
            "metrics": "printf 'HOST='; hostname; printf '\\nUPTIME='; uptime; printf '\\nMEMORY='; free -h 2>&1; printf '\\nDISK='; df -h / 2>&1",
            "network": "ip -brief address 2>&1; ip route 2>&1; ss -s 2>&1",
            "logs": "journalctl -n 200 --no-pager 2>&1 | head -201",
            "packages": "if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W 2>&1 | head -101; elif command -v rpm >/dev/null 2>&1; then rpm -qa 2>&1 | head -101; fi",
        },
        "macos": {
            "processes": "ps -Ao pid,ppid,user,%cpu,%mem,state,comm -r | head -51",
            "disk": "df -h",
            "ports": "lsof -nP -iTCP -sTCP:LISTEN 2>&1 | head -101",
            "services": "launchctl list 2>&1 | head -101",
            "metrics": "printf 'HOST='; hostname; printf '\\nUPTIME='; uptime; printf '\\nMEMORY='; vm_stat; printf '\\nDISK='; df -h /",
            "network": "ifconfig 2>&1; netstat -rn 2>&1",
            "logs": "log show --last 1h --style compact --info 2>&1 | head -201",
            "packages": "brew list --versions 2>&1 | head -101",
        },
        "windows": {
            "processes": 'powershell -NoProfile -Command "Get-Process | Sort-Object CPU -Descending | Select-Object -First 50 Id,ProcessName,CPU,WorkingSet64 | Format-Table -AutoSize"',
            "disk": 'powershell -NoProfile -Command "Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,SizeRemaining | Format-Table -AutoSize"',
            "ports": 'powershell -NoProfile -Command "Get-NetTCPConnection -State Listen | Select-Object -First 100 LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize"',
            "services": 'powershell -NoProfile -Command "Get-Service | Where-Object Status -eq Running | Select-Object -First 100 Name,DisplayName,Status | Format-Table -AutoSize"',
            "metrics": 'powershell -NoProfile -Command "hostname; Get-CimInstance Win32_OperatingSystem | Select-Object Caption,LastBootUpTime,TotalVisibleMemorySize,FreePhysicalMemory; Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free"',
            "network": 'powershell -NoProfile -Command "Get-NetIPConfiguration; Get-NetRoute | Select-Object -First 50"',
            "logs": 'powershell -NoProfile -Command "Get-WinEvent -LogName System -MaxEvents 100 | Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,Message | Format-List"',
            "packages": 'powershell -NoProfile -Command "Get-Package | Select-Object -First 100 Name,Version,ProviderName | Format-Table -AutoSize"',
        },
    }
    commands = commands_by_os.get(family)
    if commands is None:
        raise HTTPException(501, "System inspection is unsupported for this remote shell/OS")
    if kind not in commands:
        raise HTTPException(404, "Unsupported system inspection")
    started = perf_counter()
    code, output, error = _run_remote(commands[kind], timeout=15)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("system-" + kind, "active SSH target", status, started, None if code == 0 else 502)
    if code != 0 and not output.strip():
        raise HTTPException(502, (error or "System inspection failed").strip()[:1000])
    return {"target": "active KubeDock SSH machine", "os": family, "kind": kind, "output": output[:50000], "warning": error.strip()[:1000] if error.strip() else None}

@app.websocket("/api/remote/terminal")
async def remote_terminal(websocket: WebSocket) -> None:
    token = websocket.cookies.get(_SESSION_COOKIE, "")
    now = perf_counter()
    with _SESSION_LOCK:
        session = _WEB_SESSIONS.get(token)
        if session and session[1] <= now:
            _WEB_SESSIONS.pop(token, None)
            _SESSION_CONTEXTS.pop(token, None)
            session = None
    if not session:
        await websocket.close(code=4401, reason="Authentication required")
        return
    host = websocket.headers.get("host", "")
    origin = websocket.headers.get("origin", "")
    if origin:
        from urllib.parse import urlsplit
        parsed_origin = urlsplit(origin)
        if parsed_origin.netloc.lower() != host.lower() or parsed_origin.scheme not in {"http", "https"}:
            await websocket.close(code=4403, reason="Origin not allowed")
            return
    try:
        client_ip = ipaddress.ip_address(websocket.client.host if websocket.client else "")
    except ValueError:
        client_ip = None
    if websocket.url.scheme != "wss" and not (client_ip and client_ip.is_loopback):
        await websocket.close(code=4403, reason="Secure WebSocket required")
        return
    try:
        ssh = _ssh()
        channel = ssh.invoke_shell(term="xterm-256color", width=120, height=32)
    except Exception:
        logger.exception("Unable to open remote PTY")
        await websocket.close(code=1011, reason="Unable to open remote terminal")
        return
    await websocket.accept()
    _AUTH_USER.set(session[0])
    started = perf_counter()
    _audit_remote("terminal-connect", "active SSH target", "succeeded", started)
    async def pump_output():
        try:
            while not channel.closed:
                data = await asyncio.to_thread(channel.recv, 4096)
                if not data:
                    break
                await websocket.send_text(data.decode("utf-8", errors="replace"))
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
    pump = asyncio.create_task(pump_output())
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "input":
                value = message.get("data", "")
                if isinstance(value, str) and len(value) <= 8192:
                    channel.send(value)
            elif kind == "resize":
                cols = max(40, min(int(message.get("cols", 120)), 300))
                rows = max(10, min(int(message.get("rows", 32)), 120))
                channel.resize_pty(width=cols, height=rows)
            elif kind == "close":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Remote terminal session failed")
    finally:
        pump.cancel()
        try:
            channel.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        _audit_remote("terminal-disconnect", "active SSH target", "succeeded", started)



def _desktop_call(callback, timeout: float = 5.0):
    with _RUNTIME_LOCK:
        provider = _APP_PROVIDER
        bridge = _DESKTOP_BRIDGE
    if provider is None or bridge is None:
        raise HTTPException(503, "Desktop control bridge is not attached")
    try:
        window = provider()
    except Exception as exc:
        logger.exception("Desktop provider failed")
        raise HTTPException(503, "KubeDock desktop process is unavailable") from exc
    if window is None:
        raise HTTPException(503, "KubeDock desktop process is unavailable")
    return bridge.invoke(lambda: callback(window), timeout=timeout)


class AppSettingsUpdate(BaseModel):
    theme: Optional[str] = None
    tunnel_csv_path: Optional[str] = None


@app.get("/api/app/settings")
def desktop_settings(_: None = Depends(require_login)) -> dict:
    def inspect(_window):
        settings = load_settings() or {}
        return {
            "theme": settings.get("theme", "App Default"),
            "available_themes": sorted(THEMES.keys()),
            "tunnel_csv_path": settings.get("tunnel_csv_path", ""),
        }
    return _desktop_call(inspect)


@app.put("/api/app/settings")
def update_desktop_settings(body: AppSettingsUpdate, _: None = Depends(require_login)) -> dict:
    if body.theme is None and body.tunnel_csv_path is None:
        raise HTTPException(400, "Provide at least one supported setting")
    updates = {}
    if body.theme is not None:
        if body.theme not in THEMES:
            raise HTTPException(400, "Unknown theme")
        def apply_theme(window):
            apply_theme_vars(body.theme)
            apply_qss_to(window)
            save_settings(theme=body.theme)
            return body.theme
        updates["theme"] = _desktop_call(apply_theme)
    if body.tunnel_csv_path is not None:
        path = _safe_remote_path(body.tunnel_csv_path)
        def apply_tunnel_path(window):
            save_settings(tunnel_csv_path=path)
            tab = getattr(window, "k8s_tab", None)
            if tab is not None:
                tab._tunnel_csv_path = path
                reload_services = getattr(tab, "_load_tunnel_csv", None)
                if callable(reload_services) and getattr(window, "ssh", None):
                    reload_services()
            return path
        updates["tunnel_csv_path"] = _desktop_call(apply_tunnel_path)
    _audit_remote("update-desktop-settings", ",".join(sorted(updates)), "succeeded", perf_counter())
    return {"ok": True, "updated": updates}


class TunnelStartRequest(BaseModel):
    service_names: list[str]


class TunnelStopRequest(BaseModel):
    confirm_stop: bool = False


@app.get("/api/app/tunnels")
def desktop_tunnels(_: None = Depends(require_login)) -> dict:
    def inspect(window):
        tab = getattr(window, "k8s_tab", None)
        if tab is None:
            raise HTTPException(503, "Kubernetes tab is unavailable")
        services = getattr(tab, "_tunnel_services", []) or []
        process = getattr(tab, "_tunnel_process", None)
        running = False
        if process is not None:
            try:
                running = int(process.state()) != 0
            except Exception:
                running = False
        forwarded = set(getattr(tab, "_tunnel_forwarded_ports", set()) or set())
        safe_services = []
        for service in services:
            safe_services.append({
                "name": str(service.get("name", "")),
                "namespace": str(service.get("namespace", "")),
                "port": service.get("port"),
                "forwarded": service.get("port") in forwarded,
            })
        return {"services": safe_services, "running": running, "target": str(getattr(window, "host_label", "") or "")}
    return _desktop_call(inspect)


@app.post("/api/app/tunnels/start")
def desktop_tunnel_start(body: TunnelStartRequest, _: None = Depends(require_login)) -> dict:
    names = [str(name).strip() for name in body.service_names]
    if not names or len(names) > 20 or any(not name or len(name) > 253 for name in names):
        raise HTTPException(400, "Select between 1 and 20 configured tunnel services")
    if len(set(names)) != len(names):
        raise HTTPException(400, "Duplicate tunnel service names are not allowed")
    started = perf_counter()
    def start(window):
        tab = getattr(window, "k8s_tab", None)
        if tab is None or getattr(window, "ssh", None) is None:
            raise HTTPException(503, "Connect to an SSH machine in KubeDock first")
        services = getattr(tab, "_tunnel_services", []) or []
        by_name = {str(service.get("name", "")): service for service in services}
        missing = [name for name in names if name not in by_name]
        if missing:
            raise HTTPException(404, "Unknown tunnel service(s): " + ", ".join(missing))
        method = getattr(tab, "_start_tunnel_services", None)
        if not callable(method):
            raise HTTPException(501, "This KubeDock version does not expose reusable tunnel controls")
        result = method([by_name[name] for name in names])
        if not result.get("ok"):
            raise HTTPException(409 if result.get("already_running") else 400, result.get("error", "Tunnel start failed"))
        return result
    result = _desktop_call(start, timeout=8.0)
    _audit_remote("start-local-ssh-tunnel", ",".join(names), "succeeded", started)
    return result


@app.post("/api/app/tunnels/stop")
def desktop_tunnel_stop(body: TunnelStopRequest, _: None = Depends(require_login)) -> dict:
    if body.confirm_stop is not True:
        raise HTTPException(409, "Explicit confirmation is required to stop the active tunnel")
    started = perf_counter()
    def stop(window):
        tab = getattr(window, "k8s_tab", None)
        if tab is None:
            raise HTTPException(503, "Kubernetes tab is unavailable")
        method = getattr(tab, "_stop_tunnel", None)
        if not callable(method):
            raise HTTPException(501, "This KubeDock version does not expose tunnel controls")
        method()
        return {"ok": True, "stopped": True}
    result = _desktop_call(stop, timeout=8.0)
    _audit_remote("stop-local-ssh-tunnel", "active tunnel", "succeeded", started)
    return result


class MachineConnectRequest(BaseModel):
    profile_index: int
    confirm_target: str


@app.get("/api/app/machines")
def desktop_machines(_: None = Depends(require_login)) -> dict:
    def inspect(_window):
        machines = []
        for index, profile in enumerate(load_recent_instances()):
            host = str(profile.get("host", "")).strip()
            user = str(profile.get("user", "")).strip()
            alias = str(profile.get("alias", "")).strip()
            protocol = str(profile.get("protocol", "ssh")).lower()
            target = alias or (f"{user}@{host}" if user and host else host)
            pem = str(profile.get("pem", "")).strip()
            key_available = bool(pem and os.path.isabs(pem) and os.path.isfile(pem) and os.access(pem, os.R_OK))
            machines.append({
                "profile_index": index,
                "target": target,
                "host": host,
                "port": int(profile.get("port", 22)) if str(profile.get("port", "22")).isdigit() else 22,
                "user": user,
                "protocol": protocol,
                "has_private_key": key_available,
                "can_connect": protocol == "ssh" and key_available,
            })
        return {"machines": machines, "scope": "saved-local-profiles", "note": "Only saved key-based SSH profiles can be switched to remotely; passwords and key paths are never returned."}
    return _desktop_call(inspect)


@app.post("/api/app/machines/connect")
def desktop_connect_machine(body: MachineConnectRequest, _: None = Depends(require_login)) -> dict:
    if body.profile_index < 0 or body.profile_index > 1000:
        raise HTTPException(400, "Invalid saved machine profile index")
    started = perf_counter()
    def connect(window):
        method = getattr(window, "_connect_profile_from_web", None)
        if not callable(method):
            raise HTTPException(501, "This KubeDock version does not expose saved-profile switching")
        try:
            return method(body.profile_index, body.confirm_target)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    result = _desktop_call(connect, timeout=8.0)
    _audit_remote("connect-saved-machine", body.confirm_target, "succeeded", started)
    return result


@app.get("/api/app/status")
def desktop_status(_: None = Depends(require_login)) -> dict:
    def inspect(window):
        ssh = getattr(window, "ssh", None)
        connected = False
        if ssh is not None:
            try:
                transport = ssh.get_transport()
                connected = bool(transport and transport.is_active())
            except Exception:
                connected = False
        tunnel_process = getattr(getattr(window, "k8s_tab", None), "_tunnel_process", None)
        tunnel_running = False
        if tunnel_process is not None:
            try:
                tunnel_running = int(tunnel_process.state()) != 0
            except Exception:
                tunnel_running = False
        return {
            "application": "KubeDock",
            "desktop_process": "running",
            "connected": connected,
            "machine": str(getattr(window, "host_label", "") or "") or None,
            "host": getattr(window, "_conn_host", None),
            "port": getattr(window, "_conn_port", None),
            "protocol": getattr(window, "_conn_protocol", None),
            "tunnel_running": tunnel_running,
            "last_connection_error": getattr(window, "_last_web_connect_error", None),
            "scope": "desktop-process",
        }
    return _desktop_call(inspect)


@app.post("/api/app/refresh")
def desktop_refresh(_: None = Depends(require_login)) -> dict:
    started = perf_counter()
    def refresh(window):
        dashboard = getattr(window, "dashboard_tab", None)
        refresh_dashboard = getattr(dashboard, "_refresh", None)
        if callable(refresh_dashboard):
            refresh_dashboard(background=True)
        k8s_tab = getattr(window, "k8s_tab", None)
        reload_tunnels = getattr(k8s_tab, "_load_tunnel_csv", None)
        if callable(reload_tunnels) and getattr(window, "ssh", None):
            reload_tunnels()
        refresh_tunnels = getattr(k8s_tab, "_refresh_tunnel_status", None)
        if callable(refresh_tunnels) and getattr(window, "ssh", None):
            refresh_tunnels()
        return {"ok": True, "refreshed": ["dashboard", "tunnel-status"]}
    result = _desktop_call(refresh)
    _audit_remote("desktop-refresh", "KubeDock desktop process", "succeeded", started)
    return result


@app.post("/api/app/disconnect")
def desktop_disconnect(body: dict, _: None = Depends(require_login)) -> dict:
    current_target = _desktop_call(lambda window: str(getattr(window, "host_label", "") or ""))
    if not current_target or body.get("confirm_target") != current_target:
        raise HTTPException(409, "Confirm the exact active machine label before disconnecting")
    started = perf_counter()
    def disconnect(window):
        method = getattr(window, "_disconnect", None)
        if not callable(method):
            raise HTTPException(501, "This desktop version does not expose a disconnect operation")
        method("Disconnected remotely through KubeDock Web")
        return {"ok": True, "disconnected": current_target}
    result = _desktop_call(disconnect)
    _audit_remote("desktop-disconnect", current_target, "succeeded", started)
    return result



_ALLOWED_RESOURCE_KINDS = {
    "pods", "deployments", "statefulsets", "daemonsets", "replicasets",
    "jobs", "cronjobs", "services", "configmaps", "secrets", "serviceaccounts",
    "persistentvolumeclaims", "ingresses", "networkpolicies", "namespaces",
    "nodes", "persistentvolumes", "storageclasses", "events",
}
_NAMESPACED_RESOURCE_KINDS = {
    "pods", "deployments", "statefulsets", "daemonsets", "replicasets",
    "jobs", "cronjobs", "services", "configmaps", "secrets", "serviceaccounts",
    "persistentvolumeclaims", "ingresses", "networkpolicies", "events",
}


def _resource_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in _ALLOWED_RESOURCE_KINDS:
        raise HTTPException(400, "Unsupported Kubernetes resource kind")
    return kind


@app.get("/api/kube/resources/{kind}")
def list_kube_resources(
    kind: str,
    namespace: Optional[str] = None,
    _: None = Depends(require_login),
) -> dict:
    kind = _resource_kind(kind)
    if kind in _NAMESPACED_RESOURCE_KINDS:
        namespace_args = _ns(namespace)
        data = _kubectl(f"get {kind} {namespace_args} -o json", json_output=True)
    else:
        data = _kubectl(f"get {kind} -o json", json_output=True)
    items = data.get("items", [])
    if kind == "secrets":
        for item in items:
            item.pop("data", None)
            item.pop("stringData", None)
    return {"kind": kind, "namespace": namespace or "all", "items": items}


@app.get("/api/kube/resources/{kind}/{namespace}/{name}/yaml")
def get_kube_resource_yaml(
    kind: str,
    namespace: str,
    name: str,
    _: None = Depends(require_login),
) -> dict:
    kind = _resource_kind(kind)
    if kind == "secrets":
        raise HTTPException(403, "Secret YAML is not exposed by the remote editor")
    name = _validate_k8s_name(name, "resource name")
    if kind in _NAMESPACED_RESOURCE_KINDS:
        namespace = _validate_k8s_name(namespace, "namespace")
        args = f"get {kind} {shlex.quote(name)} -n {shlex.quote(namespace)} -o yaml"
    else:
        if namespace != "cluster":
            raise HTTPException(400, "Cluster-scoped resources use namespace=cluster")
        args = f"get {kind} {shlex.quote(name)} -o yaml"
    return {"kind": kind, "namespace": namespace, "name": name, "yaml": _kubectl(args, timeout=20)}


class ResourceYamlRequest(BaseModel):
    yaml: str


class ResourceApplyRequest(BaseModel):
    yaml: str
    confirm_apply: bool = False


@app.post("/api/kube/resources/validate")
def validate_kube_resource(body: ResourceYamlRequest, _: None = Depends(require_login)) -> dict:
    content = body.yaml.encode("utf-8") if isinstance(body.yaml, str) else b""
    if not content or len(content) > 1_000_000:
        raise HTTPException(400, "YAML must be non-empty and at most 1 MB")
    output = _kubectl("apply --dry-run=server -f - 2>&1", timeout=45, input_data=content)
    return {"ok": True, "output": output.strip(), "validation": "server-side dry run"}


@app.post("/api/kube/resources/apply")
def apply_kube_resource(body: ResourceApplyRequest, _: None = Depends(require_login)) -> dict:
    if body.confirm_apply is not True:
        raise HTTPException(409, "Confirm the target cluster before applying YAML")
    content = body.yaml.encode("utf-8") if isinstance(body.yaml, str) else b""
    if not content or len(content) > 1_000_000:
        raise HTTPException(400, "YAML must be non-empty and at most 1 MB")
    started = perf_counter()
    target_context = _KUBE_CONTEXT.get() or "shared-current-context"
    try:
        _kubectl("apply --dry-run=server -f - 2>&1", timeout=45, input_data=content)
        output = _kubectl("apply -f - 2>&1", timeout=60, input_data=content)
        _audit_remote("kubernetes-apply-yaml", target_context, "succeeded", started)
        return {"ok": True, "context": target_context, "output": output.strip()}
    except HTTPException as exc:
        _audit_remote("kubernetes-apply-yaml", target_context, "failed", started, exc.status_code)
        raise


@app.post("/api/kube/resources/{kind}/{namespace}/{name}/delete")
def delete_kube_resource(
    kind: str,
    namespace: str,
    name: str,
    body: dict,
    _: None = Depends(require_login),
) -> dict:
    kind = _resource_kind(kind)
    name = _validate_k8s_name(name, "resource name")
    target = f"{kind}/{namespace}/{name}"
    if body.get("confirm_target") != target:
        raise HTTPException(409, "Confirm the exact Kubernetes resource target before deletion")
    if kind in _NAMESPACED_RESOURCE_KINDS:
        namespace = _validate_k8s_name(namespace, "namespace")
        args = f"delete {kind} {shlex.quote(name)} -n {shlex.quote(namespace)} 2>&1"
    else:
        if namespace != "cluster":
            raise HTTPException(400, "Cluster-scoped resources use namespace=cluster")
        args = f"delete {kind} {shlex.quote(name)} 2>&1"
    output = _audited_kubectl("delete-resource", target, args, timeout=45)
    return {"ok": True, "target": target, "output": output.strip()}




class ProcessTerminateRequest(BaseModel):
    pid: int
    confirm_pid: int


class ServiceActionRequest(BaseModel):
    name: str
    action: str
    confirm_target: str


class PowerActionRequest(BaseModel):
    action: str
    confirm_target: str
    confirm_phrase: str


@app.post("/api/remote/system/process/terminate")
def remote_terminate_process(body: ProcessTerminateRequest, _: None = Depends(require_login)) -> dict:
    if body.pid <= 1 or body.pid != body.confirm_pid:
        raise HTTPException(409, "Confirm the exact process ID; PID 1 and system root processes are not allowed")
    family = _remote_os_family()
    if family in {"linux", "macos"}:
        command = f"kill -TERM {body.pid}"
    elif family == "windows":
        command = f"powershell -NoProfile -Command \"{verb} -Name '{name}' -ErrorAction Stop\""
    else:
        raise HTTPException(501, "Process termination is unsupported for this remote OS")
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=15)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("terminate-process", f"pid/{body.pid}", status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Process termination failed")
    return {"ok": True, "pid": body.pid, "output": output.strip()}


@app.post("/api/remote/system/service")
def remote_service_action(body: ServiceActionRequest, _: None = Depends(require_login)) -> dict:
    name = str(body.name or "").strip()
    action = str(body.action or "").strip().lower()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, "Unsupported service action")
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", name):
        raise HTTPException(400, "Invalid service name")
    target = f"{action} service/{name}"
    if body.confirm_target != target:
        raise HTTPException(409, "Confirm the exact service action and name")
    family = _remote_os_family()
    if family == "linux":
        command = f"systemctl {action} {shlex.quote(name)}"
    elif family == "windows":
        verb = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[action]
        command = f"powershell -NoProfile -Command \"{verb} -Name '{name}' -ErrorAction Stop\""
    else:
        raise HTTPException(501, "Service start/stop/restart is supported only on Linux and Windows targets")
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=30)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("service-" + action, name, status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Service action failed")
    return {"ok": True, "action": action, "service": name, "output": output.strip()}


@app.post("/api/remote/system/power")
def remote_power_action(body: PowerActionRequest, _: None = Depends(require_login)) -> dict:
    action = str(body.action or "").strip().lower()
    if action not in {"reboot", "shutdown"}:
        raise HTTPException(400, "Action must be reboot or shutdown")
    family = _remote_os_family()
    hostname_command = 'powershell -NoProfile -Command "hostname"' if family == "windows" else "hostname"
    host_code, hostname_out, host_error = _run_remote(hostname_command, timeout=8)
    hostname = hostname_out.strip().splitlines()[0] if host_code == 0 and hostname_out.strip() else ""
    if not hostname:
        raise HTTPException(502, (host_error or "Unable to identify the remote hostname").strip()[:500])
    expected_phrase = f"{action.upper()} {hostname}"
    if body.confirm_target != hostname or body.confirm_phrase != expected_phrase:
        raise HTTPException(409, f"Confirm target hostname and type exactly: {expected_phrase}")
    if family == "windows":
        command = "shutdown /r /t 60 /c \"Requested through KubeDock Web\"" if action == "reboot" else "shutdown /s /t 60 /c \"Requested through KubeDock Web\""
    elif family in {"linux", "macos"}:
        command = "shutdown -r +1" if action == "reboot" else "shutdown -h +1"
    else:
        raise HTTPException(501, "Power management is unsupported for this remote OS")
    started = perf_counter()
    code, output, error = _run_remote(command, timeout=15)
    status = "succeeded" if code == 0 else "failed"
    _audit_remote("system-" + action, hostname, status, started, None if code == 0 else 502)
    if code != 0:
        raise HTTPException(502, (error or output).strip()[:1000] or "Power action failed; the SSH account may lack required privileges")
    return {"ok": True, "action": action, "hostname": hostname, "output": output.strip(), "scheduled_delay_seconds": 60}



def _static_response():
    static_file = APP_DIR / "static" / "index.html"
    if static_file.exists():
        return FileResponse(str(static_file))
    try:
        from webapp.static_content import INDEX_HTML
    except ImportError:
        logger.exception("Missing packaged Web UI")
        raise HTTPException(500, "Web UI assets are missing")
    return HTMLResponse(INDEX_HTML)


@app.get("/")
def index(request: Request):
    _ensure_secure_transport(request)
    return _static_response()


_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None
_server_port: Optional[int] = None
_server_host: Optional[str] = None
_SERVER_LOCK = threading.RLock()


def _reserve_port(
    host: str,
    preferred_port: int,
    attempts: int = 100,
) -> tuple[int, socket.socket]:
    last_error = None
    for candidate in range(preferred_port, preferred_port + attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            if candidate != preferred_port:
                logger.warning(
                    "Web port %s is occupied; using port %s instead",
                    preferred_port,
                    candidate,
                )
            return candidate, sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError(
        f"Unable to bind Web App to ports {preferred_port}-"
        f"{preferred_port + attempts - 1}: {last_error}"
    )


def _tls_configured() -> bool:
    """True when both TLS certificate and key paths are set in the environment."""
    return bool(
        os.environ.get("WEBAPP_TLS_CERTFILE", "").strip()
        and os.environ.get("WEBAPP_TLS_KEYFILE", "").strip()
    )


def _default_bind_host() -> str:
    """Safe default bind address.

    The dashboard can delete pods and scale deployments, so it only listens on
    all interfaces when TLS is configured. Without TLS it stays on loopback
    (use an SSH tunnel for remote access). An explicit WEBAPP_HOST always wins.
    """
    return "0.0.0.0" if _tls_configured() else "127.0.0.1"


def _is_loopback_host(host: Optional[str]) -> bool:
    host = (host or "").strip().lower()
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _get_lan_host() -> str:
    """Return the local LAN address used to reach the embedded web server."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packet is sent; this asks the OS which local interface would
            # be used to reach an external IPv4 address.
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
        finally:
            sock.close()
        if host and host != "127.0.0.1":
            return host
    except OSError:
        pass
    return "127.0.0.1"


def get_server_url() -> str:
    with _SERVER_LOCK:
        port = _server_port
        bound_host = _server_host
    if port is None:
        try:
            port = int(os.environ.get("WEBAPP_PORT", "8000"))
        except ValueError:
            port = 8000
    if bound_host is None:
        bound_host = os.environ.get("WEBAPP_HOST", "").strip() or _default_bind_host()
    scheme = "https" if _tls_configured() else "http"
    # A loopback-only server is not reachable via the LAN address.
    host = "127.0.0.1" if _is_loopback_host(bound_host) else _get_lan_host()
    return f"{scheme}://{host}:{port}"


def start_server(
    ssh_provider: Callable[[], Optional[paramiko.SSHClient]],
    host: Optional[str] = None,
    port: Optional[int] = None,
    app_provider: Optional[Callable[[], object]] = None,
) -> bool:
    global _server, _server_thread, _server_port, _server_host
    # Bind to loopback unless TLS is configured (then all interfaces, so a
    # phone on the LAN can reach it over HTTPS). Set WEBAPP_HOST to override.
    host = (host or os.environ.get("WEBAPP_HOST", "")).strip() or _default_bind_host()
    if not _tls_configured() and not _is_loopback_host(host):
        logger.warning(
            "Web app is listening on %s without TLS; remote requests will be "
            "rejected. Configure WEBAPP_TLS_CERTFILE and WEBAPP_TLS_KEYFILE.",
            host,
        )
    try:
        preferred_port = int(port or os.environ.get("WEBAPP_PORT", "8000"))
    except ValueError:
        preferred_port = 8000

    configure_runtime(ssh_provider, app_provider=app_provider)

    with _SERVER_LOCK:
        if _server_thread is not None and _server_thread.is_alive():
            return False

        try:
            selected_port, reserved_socket = _reserve_port(host, preferred_port)
        except OSError:
            logger.exception("Unable to start KubeDock Web server")
            return False

        tls_certfile = os.environ.get("WEBAPP_TLS_CERTFILE", "").strip()
        tls_keyfile = os.environ.get("WEBAPP_TLS_KEYFILE", "").strip()
        if bool(tls_certfile) != bool(tls_keyfile):
            logger.error("WebApp TLS requires both WEBAPP_TLS_CERTFILE and WEBAPP_TLS_KEYFILE")
            reserved_socket.close()
            return False
        config = uvicorn.Config(
            app, host=host, port=selected_port, log_level="info",
            access_log=True, log_config=None,
            ssl_certfile=tls_certfile or None, ssl_keyfile=tls_keyfile or None,
        )
        server = uvicorn.Server(config)
        _server = server
        _server_port = selected_port
        _server_host = host

        def run() -> None:
            logger.info(
                "Starting KubeDock Web on http://%s:%s",
                host,
                selected_port,
            )
            try:
                server.run(sockets=[reserved_socket])
            except Exception:
                logger.exception("Embedded web server stopped with an error")
            finally:
                try:
                    reserved_socket.close()
                except OSError:
                    pass
                logger.info("KubeDock Web server stopped")

        _server_thread = threading.Thread(
            target=run,
            name="KubeDock-Web",
            daemon=True,
        )
        _server_thread.start()
        return True


def stop_server(timeout: float = 3.0) -> None:
    global _server, _server_thread, _server_port, _server_host
    with _SERVER_LOCK:
        server, thread = _server, _server_thread
        _server = None
        _server_thread = None
        _server_port = None
        _server_host = None

    # A stop/restart must invalidate browser sessions, even when it occurs
    # inside the same desktop process rather than via a full app shutdown.
    with _SESSION_LOCK:
        _WEB_SESSIONS.clear()
        _SESSION_CONTEXTS.clear()

    if server is not None:
        logger.info("Stopping KubeDock Web server")
        server.should_exit = True

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
