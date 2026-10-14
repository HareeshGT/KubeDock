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



# Resource sorting is local: these helpers consume existing Docker rows.
SORT_OPTIONS = {
    "containers": [
        ("Name", "name"), ("State", "state"), ("Image", "image"), ("Status", "status")
    ],
    "images": [
        ("Repository", "repository"), ("Tag", "tag"), ("Size", "size"),
        ("Created", "created")
    ],
    "volumes": [("Name", "name"), ("Driver", "driver"), ("Scope", "scope")],
    "networks": [("Name", "name"), ("Driver", "driver"), ("Scope", "scope")],
}


def natural_sort_key(value):
    """Case-insensitive natural key (so app2 sorts before app10)."""
    parts = re.split(r"(\d+)", str(value or "").casefold())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def size_to_bytes(value):
    """Parse common Docker size strings; return None when the size is unknown."""
    if value is None:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)?",
                      str(value).strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    multipliers = {
        "B": 1, "KB": 1000, "KIB": 1024, "MB": 1000 ** 2,
        "MIB": 1024 ** 2, "GB": 1000 ** 3, "GIB": 1024 ** 3,
        "TB": 1000 ** 4, "TIB": 1024 ** 4,
    }
    return int(number * multipliers[unit])


def _created_sort_value(row):
    """Return a comparable timestamp when exact or relative creation metadata exists."""
    import datetime

    for key in ("CreatedAt", "Created", "CreatedTime", "CreatedTimestamp"):
        value = row.get(key)
        if value is None or str(value).strip() == "":
            continue
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            return float(value)
        text = str(value).strip().replace(" UTC", "").replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(text).timestamp()
        except (ValueError, OverflowError, OSError):
            for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.datetime.strptime(text, fmt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    pass

    # CreatedSince is approximate but conventionally ordered by relative age.
    relative = re.search(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        cell(row, "CreatedSince").strip().lower()
    )
    if relative:
        units = {
            "second": 1, "minute": 60, "hour": 3600, "day": 86400,
            "week": 604800, "month": 2629800, "year": 31557600,
        }
        return -int(relative.group(1)) * units[relative.group(2)]
    return None


def _sort_value(kind, row, sort_key):
    fields = {
        "containers": {
            "name": ("Names", "ID"), "state": ("State",),
            "image": ("Image",), "status": ("Status",),
        },
        "images": {
            "repository": ("Repository",), "tag": ("Tag",),
            "size": ("Size",), "created": (),
        },
        "volumes": {"name": ("Name",), "driver": ("Driver",), "scope": ("Scope",)},
        "networks": {"name": ("Name",), "driver": ("Driver",), "scope": ("Scope",)},
    }
    if kind not in fields or sort_key not in fields[kind]:
        raise ValueError("Unsupported Docker sort: {} / {}".format(kind, sort_key))
    if kind == "images" and sort_key == "size":
        return size_to_bytes(row.get("Size"))
    if kind == "images" and sort_key == "created":
        return _created_sort_value(row)
    for field in fields[kind][sort_key]:
        value = cell(row, field).strip()
        if value:
            return value
    return None


def sort_resources(rows, kind, sort_key="", descending=False):
    """Sort by source metadata, keeping unknown values last in both directions."""
    rows = list(rows or [])
    if not sort_key:
        return rows
    keyed, missing = [], []
    for index, row in enumerate(rows):
        value = _sort_value(kind, row, sort_key)
        if value is None or value == "":
            missing.append((index, row))
        else:
            key = natural_sort_key(value) if isinstance(value, str) else value
            keyed.append((index, row, key))
    keyed.sort(key=lambda entry: entry[2], reverse=bool(descending))
    return [entry[1] for entry in keyed] + [entry[1] for entry in missing]



def docker_label_map(row):
    """Parse Docker's comma-delimited Labels field without guessing from names."""
    labels = row.get("Labels", row.get("Label", ""))
    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}
    result = {}
    for entry in str(labels or "").split(","):
        key, separator, value = entry.strip().partition("=")
        if separator and key:
            result[key] = value
    return result


def compose_metadata(row):
    """Return explicit Compose project/service labels, if present."""
    labels = docker_label_map(row or {})
    project = labels.get("com.docker.compose.project", "").strip()
    service = labels.get("com.docker.compose.service", "").strip()
    # A service label without a project is insufficient evidence of membership.
    return (project, service) if project else ("", "")


def compose_groups(rows):
    """Group containers by Compose project labels and keep standalone rows separate."""
    projects = {}
    standalone = []
    for row in rows or []:
        project, _service = compose_metadata(row)
        if project:
            projects.setdefault(project, []).append(row)
        else:
            standalone.append(row)
    groups = []
    for project, members in sorted(projects.items(), key=lambda item: natural_sort_key(item[0])):
        groups.append({
            "key": "compose:" + project,
            "name": project,
            "kind": "compose",
            "containers": list(members),
        })
    if standalone:
        groups.append({
            "key": "standalone",
            "name": "Standalone containers",
            "kind": "standalone",
            "containers": list(standalone),
        })
    return groups



CLEANUP_CATEGORIES = {
    "stopped_containers": "Stopped containers",
    "dangling_images": "Dangling images",
    "unused_images": "Unused images (not used by any container)",
    "build_cache": "Reclaimable build cache",
}


def cleanup_preview_command():
    """One SSH command composed only of read-only Docker inspection commands."""
    fmt = shlex.quote("{{json .}}")
    return "; ".join([
        "printf '%s\\n' '__KD_STOPPED_CONTAINERS__'",
        "docker ps -a -s --filter status=exited --format {} 2>&1".format(fmt),
        "printf '%s\\n' '__KD_DANGLING_IMAGES__'",
        "docker images --no-trunc --filter dangling=true --format {} 2>&1".format(fmt),
        "printf '%s\\n' '__KD_ALL_IMAGES__'",
        "docker images --no-trunc --format {} 2>&1".format(fmt),
        "printf '%s\\n' '__KD_USED_IMAGE_IDS__'",
        """ids=$(docker ps -aq 2>&1); case "$ids" in *error*|*Error*|*denied*|*Cannot*) printf '%s\\n' "$ids" ;; *) for id in $ids; do docker inspect --format '{{.Image}}' "$id" 2>&1; done ;; esac""",
        "printf '%s\\n' '__KD_BUILDER_CACHE__'",
        "docker buildx du --verbose 2>&1",
    ])


def _cleanup_sections(text):
    matches = list(re.finditer(r"(?m)^__KD_([A-Z_]+)__\s*$", text or ""))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text or "")
        sections[match.group(1)] = (text or "")[match.end():end].strip()
    return sections


def _section_error(text):
    lowered = (text or "").lower()
    markers = (
        "permission denied", "error response", "error: ", "no such container",
        "cannot connect to the docker daemon",
        "is the docker daemon running", "unknown flag", "unknown command",
        "is not a docker command", "is not a docker subcommand",
        "command not found", "not supported", "docker: not found",
    )
    return next((line.strip() for line in (text or "").splitlines()
                 if any(marker in line.lower() for marker in markers)), "")


def _builder_cache_candidates(text):
    """Read the verbose builder-usage report; only include records marked reclaimable."""
    candidates = []
    blocks = re.split(r"(?m)(?=^ID:\s*)", text or "")
    for block in blocks:
        ident = re.search(r"(?m)^ID:\s*(\S+)", block)
        reclaimable = re.search(r"(?im)^Reclaimable:\s*true\b", block)
        if not ident or not reclaimable:
            continue
        size_match = re.search(r"(?im)^(?:Size|Private):\s*(.+?)\s*$", block)
        candidates.append({
            "id": ident.group(1),
            "size_bytes": size_to_bytes(size_match.group(1)) if size_match else None,
        })
    return candidates


def parse_cleanup_preview(text):
    """Parse read-only Docker output into candidate categories; never deletes anything."""
    sections = _cleanup_sections(text)
    stopped_text = sections.get("STOPPED_CONTAINERS", "")
    dangling_text = sections.get("DANGLING_IMAGES", "")
    images_text = sections.get("ALL_IMAGES", "")
    used_text = sections.get("USED_IMAGE_IDS", "")
    builder_text = sections.get("BUILDER_CACHE", "")

    stopped_error = _section_error(stopped_text)
    dangling_error = _section_error(dangling_text)
    images_error = _section_error(images_text)
    used_error = _section_error(used_text)
    builder_error = _section_error(builder_text)

    stopped = []
    for row in parse_json_lines(stopped_text):
        ident = cell(row, "ID").strip()
        if ident:
            stopped.append({
                "id": ident, "name": cell(row, "Names") or ident,
                "size_bytes": size_to_bytes(row.get("Size")),
            })

    dangling_rows = parse_json_lines(dangling_text)
    dangling = []
    dangling_ids = set()
    for row in dangling_rows:
        ident = cell(row, "ID").strip()
        if not ident:
            continue
        dangling_ids.add(ident)
        dangling.append({
            "id": ident, "ref": ident,
            "name": image_display_ref(row) or ident,
            "size_bytes": size_to_bytes(row.get("Size")),
        })

    used_ids = {
        line.strip() for line in used_text.splitlines()
        if re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{12,}", line.strip())
    }
    unused = []
    if not images_error and not used_error:
        unused_by_id = {}
        for row in parse_json_lines(images_text):
            ident = cell(row, "ID").strip()
            repo, tag = cell(row, "Repository"), cell(row, "Tag")
            if not ident or ident in used_ids or ident in dangling_ids:
                continue
            if repo == "<none>" or tag == "<none>":
                continue
            ref = image_display_ref(row)
            if not ref:
                continue
            # Image IDs are stable targets. Show every associated tag because
            # removing by ID can untag all aliases of the same image.
            existing = unused_by_id.get(ident)
            if existing is not None:
                if ref not in existing["refs"]:
                    existing["refs"].append(ref)
                    existing["name"] = ", ".join(existing["refs"])
                continue
            unused_by_id[ident] = {
                "id": ident, "ref": ref, "refs": [ref], "name": ref,
                "size_bytes": size_to_bytes(row.get("Size")),
            }
        unused = list(unused_by_id.values())

    builder = _builder_cache_candidates(builder_text)
    # The Buildx summary reports aggregate reclaimable size; this avoids
    # over-counting shared cache layers by summing individual record sizes.
    total_match = re.search(
        r"(?im)^Reclaimable:\s*(?!(?:true|false)\b)(.+?)\s*$", builder_text
    )
    builder_total_bytes = size_to_bytes(total_match.group(1)) if total_match else None
    categories = {
        "stopped_containers": {
            "label": CLEANUP_CATEGORIES["stopped_containers"],
            "items": stopped, "available": "STOPPED_CONTAINERS" in sections and not stopped_error,
            "error": stopped_error,
        },
        "dangling_images": {
            "label": CLEANUP_CATEGORIES["dangling_images"],
            "items": dangling, "available": "DANGLING_IMAGES" in sections and not dangling_error,
            "error": dangling_error,
        },
        "unused_images": {
            "label": CLEANUP_CATEGORIES["unused_images"],
            "items": unused,
            "available": ("ALL_IMAGES" in sections and "USED_IMAGE_IDS" in sections
                          and not images_error and not used_error),
            "error": images_error or used_error,
        },
        "build_cache": {
            "label": CLEANUP_CATEGORIES["build_cache"],
            "items": builder, "available": "BUILDER_CACHE" in sections and not builder_error,
            "reported_size_bytes": builder_total_bytes, "error": builder_error,
        },
    }
    return categories


def _format_bytes(value):
    if value is None:
        return "size unavailable"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return "{} {}".format("{:.1f}".format(size).rstrip("0").rstrip("."), unit)
        size /= 1000
    return "{} TB".format(size)


def _reported_size(items):
    known = [item["size_bytes"] for item in items if item.get("size_bytes") is not None]
    if not items:
        return _format_bytes(0)
    if not known:
        return "size unavailable"
    total = sum(known)
    if len(known) != len(items):
        return "{} reported (some sizes unavailable)".format(_format_bytes(total))
    return _format_bytes(total)


def format_cleanup_preview(report):
    """Human-readable summary; all sizes are estimates, not reclaim guarantees."""
    lines = [
        "Docker cleanup preview (read-only)",
        "Nothing has been removed.",
        "",
    ]
    for key, category in report.items():
        if not category["available"]:
            detail = ": {}".format(category["error"]) if category.get("error") else ""
            lines.append("{}: unavailable{}".format(category["label"], detail))
            continue
        items = category["items"]
        size_summary = (
            _format_bytes(category["reported_size_bytes"])
            if key == "build_cache" and category.get("reported_size_bytes") is not None
            else _reported_size(items)
        )
        lines.append("{}: {} candidate(s); {}".format(
            category["label"], len(items), size_summary))
        for item in items:
            name = item.get("name", item.get("ref", item.get("id", "Unknown resource")))
            size = _format_bytes(item.get("size_bytes"))
            lines.append("  - {} [{}] — {}".format(name, item.get("id", item.get("ref", "")), size))
        if key == "stopped_containers":
            lines.append("  Why: containers in the exited state are stopped and no longer running.")
            lines.append("  Estimates reflect reported container writable-layer sizes.")
        elif key == "dangling_images":
            lines.append("  Why: dangling images have no repository tag.")
            lines.append("  Summed image sizes are an upper-bound; image layers may be shared.")
        elif key == "unused_images":
            lines.append("  Why: no container (including stopped containers) references these image IDs.")
            lines.append("  Summed image sizes are an upper-bound; image layers may be shared.")
        elif key == "build_cache":
            lines.append("  Why: Docker marks these builder-cache records reclaimable.")
            lines.append("  Includes only builder records Docker marks reclaimable.")
    lines.extend([
        "",
        "Sizes are estimates based on Docker's metadata and may differ from actual disk space reclaimed.",
        "Volumes are excluded from every default cleanup category.",
    ])
    return "\n".join(lines)


def cleanup_scope_text(category, report):
    """Describe exactly which named candidates a selected cleanup would target."""
    if category not in CLEANUP_CATEGORIES or category not in report:
        raise ValueError("Unknown Docker cleanup category: {!r}".format(category))
    data = report[category]
    if not data["available"]:
        raise ValueError("Docker did not provide a safe preview for this category.")
    items = data["items"]
    lines = ["Selected cleanup: {}".format(data["label"]),
             "Candidate count: {}".format(len(items))]
    if category == "build_cache":
        lines.append("Scope: all cache entries Docker marks reclaimable when the command runs.")
        lines.append("Docker does not support deleting these builder records individually by ID.")
        lines.append("The candidate list can change between preview and execution.")
        lines.extend("  {} ({})".format(item["id"], _format_bytes(item.get("size_bytes")))
                     for item in items)
    else:
        lines.append("Targets:")
        lines.extend("  {}  [{}]".format(item.get("name", item.get("ref", item["id"])),
                                          item.get("id", item.get("ref", "")))
                     for item in items)
        if category == "unused_images":
            lines.append("Each image ID is targeted once; all tags listed for an ID may be removed.")
    lines.append("No volumes are included.")
    return "\n".join(lines)

def cleanup_command(category, report):
    """Build a targeted command for one explicitly confirmed cleanup category."""
    if category not in CLEANUP_CATEGORIES or category not in report:
        raise ValueError("Unknown Docker cleanup category: {!r}".format(category))
    data = report[category]
    if not data.get("available"):
        raise ValueError("Cannot clean up a category without a successful preview.")
    items = data.get("items") or []
    if not items:
        raise ValueError("There are no previewed candidates in this category.")
    if category == "stopped_containers":
        refs = [validate_name(item["id"], "container") for item in items]
        return "docker rm " + " ".join(shlex.quote(ref) for ref in refs)
    if category in ("dangling_images", "unused_images"):
        # Image IDs are immutable; a tag could be retargeted after the preview.
        refs = [validate_image_ref(item.get("id") or item["ref"]) for item in items]
        return "docker image rm " + " ".join(shlex.quote(ref) for ref in refs)
    if category == "build_cache":
        # --force only suppresses the interactive prompt; candidate scope is
        # the optional category explicitly selected and confirmed by the user.
        return "docker buildx prune --force"
    raise ValueError("Unsupported Docker cleanup category: {!r}".format(category))


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
