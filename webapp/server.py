"""Embedded KubeDock Web server.

The desktop application supplies its live Paramiko SSHClient through
``configure_runtime``. This module never creates an SSH connection of its
own; commands use KubeDock's managed SSH session helper from workers.py.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shlex
import socket
import subprocess
import threading
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from typing import Callable, Optional

import paramiko
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from themes import load_settings, save_settings
try:
    from workers import managed_exec_command
except ImportError:  # pragma: no cover
    managed_exec_command = None

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.path.expanduser("~")) / ".vm_visualizer"
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



def configure_runtime(ssh_provider: Callable[[], Optional[paramiko.SSHClient]]) -> None:
    global _SSH_PROVIDER
    with _RUNTIME_LOCK:
        _SSH_PROVIDER = ssh_provider
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


_WEB_PASSWORD_ITERATIONS = 200_000
_AUTH_FAIL_LOCK = threading.RLock()
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_MAX_FAILURES = 5
_AUTH_WINDOW_SECONDS = 300.0
_AUTH_LOCKOUT_SECONDS = 60.0


def hash_web_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        _WEB_PASSWORD_ITERATIONS,
    ).hex()
    return salt, digest


def verify_web_password(password: str, salt: str, expected: str) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt),
            _WEB_PASSWORD_ITERATIONS,
        ).hex()
        return secrets.compare_digest(digest, expected)
    except (TypeError, ValueError):
        return False


def _credentials() -> tuple[str, str, str, str]:
    settings = load_settings() or {}
    username = str(settings.get("webapp_username", "")).strip()
    password_hash = str(settings.get("webapp_password_hash", ""))
    password_salt = str(settings.get("webapp_password_salt", ""))
    legacy_password = str(settings.get("webapp_password", ""))
    if legacy_password and not (password_hash and password_salt):
        try:
            password_salt, password_hash = hash_web_password(legacy_password)
            save_settings(
                webapp_password_salt=password_salt,
                webapp_password_hash=password_hash,
                webapp_password="",
            )
            legacy_password = ""
        except Exception:
            logger.exception("Failed to migrate legacy web password")
    return username, password_hash, password_salt, legacy_password

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

    logger.info(
        "[req=%s] KUBECTL START user=%r: %s",
        request_id, _AUTH_USER.get(), command
    )
    try:
        if managed_exec_command is None:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            try:
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
                code, out, err = run_channel(stdout, stderr)
    except Exception:
        logger.exception(
            "[req=%s] KUBECTL EXCEPTION user=%r: %s (%.1f ms)",
            request_id, _AUTH_USER.get(), command,
            (perf_counter() - started) * 1000
        )
        raise

    duration_ms = (perf_counter() - started) * 1000
    logger.info(
        "[req=%s] KUBECTL END user=%r: exit=%s duration=%.1f ms stdout=%d bytes stderr=%d bytes",
        request_id, _AUTH_USER.get(), code, duration_ms, len(out), len(err)
    )
    if err.strip():
        logger.info(
            "[req=%s] KUBECTL STDERR user=%r: %s",
            request_id, _AUTH_USER.get(), err.strip()[:4000]
        )
    return code, out, err


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


def _kubectl(args: str, timeout: int = 30, json_output: bool = False):
    try:
        code, out, err = _exec(_ssh(), f"kubectl {args}", timeout)
    except TimeoutError as exc:
        raise HTTPException(504, str(exc))
    if code != 0:
        logger.warning(
            "[req=%s] kubectl failed user=%r (%s): %s",
            _REQUEST_ID.get(),
            _AUTH_USER.get(),
            code,
            err.strip(),
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


security = HTTPBasic()


def require_login(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> None:
    username, password_hash, password_salt, legacy_password = _credentials()
    if not username or not (password_hash and password_salt or legacy_password):
        raise HTTPException(503, "Web App login is not configured in KubeDock Settings")

    client_ip = _client_ip(request)
    retry_after = _auth_block_seconds(client_ip)
    if retry_after:
        raise HTTPException(
            429, "Too many login attempts; try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    valid = (
        secrets.compare_digest(credentials.username, username)
        and (
            verify_web_password(credentials.password, password_salt, password_hash)
            if password_hash and password_salt
            else secrets.compare_digest(credentials.password, legacy_password)
        )
    )

    if not valid:
        _record_auth_failure(client_ip)
        logger.warning(
            "[req=%s] Rejected web login username=%r client_ip=%s user_agent=%r",
            _REQUEST_ID.get(), credentials.username, client_ip,
            _safe_user_agent(request),
        )
        retry_after = _auth_block_seconds(client_ip)
        headers = {"WWW-Authenticate": "Basic"}
        if retry_after:
            headers["Retry-After"] = str(retry_after)
            raise HTTPException(429, "Too many login attempts; try again later.", headers=headers)
        raise HTTPException(401, "Invalid credentials", headers=headers)

    _clear_auth_failures(client_ip)

    # Migrate legacy plaintext passwords after a successful login.
    if legacy_password and not (password_hash and password_salt):
        try:
            salt, digest = hash_web_password(legacy_password)
            save_settings(
                webapp_password_salt=salt,
                webapp_password_hash=digest,
                webapp_password="",
            )
        except Exception:
            logger.exception("Failed to migrate legacy web password to a hash")

    _AUTH_USER.set(username)
    request.state.auth_user = username
    _log_client_identity(request, username)


app = FastAPI(title="KubeDock Web")


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    request_id = secrets.token_hex(4)
    request_token = _REQUEST_ID.set(request_id)
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
    out = _kubectl(
        f"rollout restart deployment/{shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} 2>&1"
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
    out = _kubectl(
        f"scale deployment/{shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} --replicas={body.replicas} 2>&1"
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
    out = _kubectl(
        f"delete pod {shlex.quote(name)} "
        f"-n {shlex.quote(namespace)} 2>&1"
    )
    return {"ok": True, "output": out.strip()}


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
def index(_: None = Depends(require_login)):
    return _static_response()


_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None
_server_port: Optional[int] = None
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


def get_server_url() -> str:
    with _SERVER_LOCK:
        port = _server_port
    if port is None:
        try:
            port = int(os.environ.get("WEBAPP_PORT", "8000"))
        except ValueError:
            port = 8000
    return f"http://127.0.0.1:{port}"


def start_server(
    ssh_provider: Callable[[], Optional[paramiko.SSHClient]],
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> bool:
    global _server, _server_thread, _server_port
    host = (host or "127.0.0.1").strip() or "127.0.0.1"
    try:
        preferred_port = int(port or os.environ.get("WEBAPP_PORT", "8000"))
    except ValueError:
        preferred_port = 8000

    configure_runtime(ssh_provider)

    with _SERVER_LOCK:
        if _server_thread is not None and _server_thread.is_alive():
            return False

        try:
            selected_port, reserved_socket = _reserve_port(host, preferred_port)
        except OSError:
            logger.exception("Unable to start KubeDock Web server")
            return False

        config = uvicorn.Config(
            app,
            host=host,
            port=selected_port,
            log_level="info",
            access_log=True,
            log_config=None,
        )
        server = uvicorn.Server(config)
        _server = server
        _server_port = selected_port

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
    global _server, _server_thread, _server_port
    with _SERVER_LOCK:
        server, thread = _server, _server_thread
        _server = None
        _server_thread = None
        _server_port = None

    if server is not None:
        logger.info("Stopping KubeDock Web server")
        server.should_exit = True

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
