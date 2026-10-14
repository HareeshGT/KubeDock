"""Pure helpers for the Docker tab.

Everything here is free of Qt so command construction and output parsing can
be unit-tested without a display. Every identifier that ends up in a remote
shell command is validated against a strict pattern *and* shell-quoted; the
only free-form text allowed through is the command typed into the "Run
command" dialog, which is always passed as a single quoted argument.
"""

import json
import re
import shlex
from typing import Dict, List, Optional, Tuple

# Container / volume / network names and short or long hex IDs.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Image references: repo[:tag], registry:port/repo@sha256:digest, or an image ID.
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")

_MAX_REF_LEN = 512

# `--format '{{json .}}'` prints one JSON object per line for all of these.
LIST_COMMANDS: Dict[str, str] = {
    "containers": "docker ps -a --format {fmt}",
    "images": "docker images --format {fmt}",
    "volumes": "docker volume ls --format {fmt}",
    "networks": "docker network ls --format {fmt}",
}

# kind -> [(column header, JSON key, fallback keys)]
COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    "containers": [
        ("Name", "Names"), ("Image", "Image"), ("State", "State"),
        ("Status", "Status"), ("Ports", "Ports"), ("ID", "ID"),
    ],
    "images": [
        ("Repository", "Repository"), ("Tag", "Tag"), ("ID", "ID"),
        ("Created", "CreatedSince"), ("Size", "Size"),
    ],
    "volumes": [("Name", "Name"), ("Driver", "Driver"), ("Scope", "Scope")],
    "networks": [("Name", "Name"), ("Driver", "Driver"), ("Scope", "Scope"), ("ID", "ID")],
}

CONTAINER_ACTIONS = ("start", "stop", "restart", "pause", "unpause", "kill")


def validate_name(value: str, what: str = "name") -> str:
    """Return ``value`` if it is a safe container/volume/network name or ID."""
    value = (value or "").strip()
    if not value or len(value) > _MAX_REF_LEN or not _NAME_RE.fullmatch(value):
        raise ValueError("Invalid Docker {}: {!r}".format(what, value))
    return value


def validate_image_ref(value: str) -> str:
    """Return ``value`` if it is a safe image reference or image ID."""
    value = (value or "").strip()
    if not value or len(value) > _MAX_REF_LEN or not _IMAGE_RE.fullmatch(value):
        raise ValueError("Invalid Docker image reference: {!r}".format(value))
    return value


def list_command(kind: str) -> str:
    if kind not in LIST_COMMANDS:
        raise ValueError("Unknown Docker resource kind: {!r}".format(kind))
    return LIST_COMMANDS[kind].format(fmt=shlex.quote("{{json .}}"))


def info_command() -> str:
    """Cheap probe: succeeds only if the CLI exists *and* the daemon answers."""
    return "docker version --format {}".format(shlex.quote("{{.Server.Version}}"))


def container_action_command(action: str, ident: str) -> str:
    if action not in CONTAINER_ACTIONS:
        raise ValueError("Unsupported container action: {!r}".format(action))
    return "docker {} {}".format(action, shlex.quote(validate_name(ident, "container")))


def remove_container_command(ident: str, force: bool = False) -> str:
    return "docker rm{} {}".format(
        " -f" if force else "", shlex.quote(validate_name(ident, "container"))
    )


def remove_image_command(ident: str, force: bool = False) -> str:
    return "docker rmi{} {}".format(
        " -f" if force else "", shlex.quote(validate_image_ref(ident))
    )


def remove_volume_command(name: str) -> str:
    return "docker volume rm {}".format(shlex.quote(validate_name(name, "volume")))


def remove_network_command(name: str) -> str:
    return "docker network rm {}".format(shlex.quote(validate_name(name, "network")))


def pull_command(ref: str) -> str:
    return "docker pull {}".format(shlex.quote(validate_image_ref(ref)))


def logs_command(ident: str, tail: int = 200, follow: bool = False,
                 timestamps: bool = False) -> str:
    tail = max(1, min(int(tail), 100000))
    parts = ["docker logs", "--tail {}".format(tail)]
    if timestamps:
        parts.append("-t")
    if follow:
        parts.append("-f")
    parts.append(shlex.quote(validate_name(ident, "container")))
    # Container stderr is part of the log; merge it so the viewer shows both.
    return " ".join(parts) + " 2>&1"


def exec_command(ident: str, command: str) -> str:
    """Run one shell command inside a running container (no TTY)."""
    command = (command or "").strip()
    if not command:
        raise ValueError("Command is empty")
    return "docker exec {} sh -c {} 2>&1".format(
        shlex.quote(validate_name(ident, "container")), shlex.quote(command)
    )


def inspect_command(kind: str, ident: str) -> str:
    if kind in ("container", "containers"):
        target = shlex.quote(validate_name(ident, "container"))
    elif kind in ("image", "images"):
        target = shlex.quote(validate_image_ref(ident))
    elif kind in ("volume", "volumes", "network", "networks"):
        target = shlex.quote(validate_name(ident, kind.rstrip("s")))
        return "docker {} inspect {}".format(kind.rstrip("s"), target)
    else:
        raise ValueError("Unknown Docker resource kind: {!r}".format(kind))
    return "docker inspect {}".format(target)


def stats_command() -> str:
    """Return one batched, non-streaming snapshot of running container usage."""
    return "docker stats --no-stream --format {}".format(shlex.quote("{{json .}}"))


def details_command(kind: str) -> str:
    """Inspect all volumes or networks in one Docker CLI invocation."""
    fmt = shlex.quote("{{json .}}")
    if kind == "volumes":
        return 'ids=$(docker volume ls -q); if [ -n "$ids" ]; then docker volume inspect --format {} $ids; fi'.format(fmt)
    if kind == "networks":
        return 'ids=$(docker network ls -q); if [ -n "$ids" ]; then docker network inspect --format {} $ids; fi'.format(fmt)
    raise ValueError("No batched detail command for Docker resource kind: {!r}".format(kind))


def parse_json_lines(text: str) -> List[dict]:
    """Parse ``--format '{{json .}}'`` output.

    Lines that are not JSON objects (sudo notices, shell warnings, a
    "WARNING: ..." banner from the CLI) are skipped rather than failing the
    whole refresh.
    """
    rows: List[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def cell(row: dict, key: str) -> str:
    value = row.get(key, "")
    return "" if value is None else str(value)


def image_display_ref(row: dict) -> str:
    """Human label for an image row; dangling images fall back to their ID."""
    repo, tag = cell(row, "Repository"), cell(row, "Tag")
    if repo and repo != "<none>":
        return "{}:{}".format(repo, tag) if tag and tag != "<none>" else repo
    return cell(row, "ID")


def classify_error(stderr: str, exit_code: int) -> Optional[str]:
    """Turn common docker failures into a short, actionable message.

    Returns None when the output does not look like a known failure.
    """
    text = (stderr or "").lower()
    if "command not found" in text or "docker: not found" in text:
        return "Docker is not installed on this host (or not on the SSH user's PATH)."
    if "permission denied" in text and "docker.sock" in text:
        return ("Permission denied talking to the Docker daemon. Add this user to the "
                "'docker' group, or switch to a user that can run docker.")
    if "cannot connect to the docker daemon" in text or "is the docker daemon running" in text:
        return "The Docker daemon is not running (or not reachable) on this host."
    if exit_code not in (0, None) and not text.strip():
        return "Docker exited with status {}.".format(exit_code)
    return None


def state_kind(state: str) -> str:
    """Map a container State to one of: running, paused, stopped, other."""
    state = (state or "").strip().lower()
    if state == "running":
        return "running"
    if state == "paused":
        return "paused"
    if state in ("exited", "dead", "created"):
        return "stopped"
    return "other"
