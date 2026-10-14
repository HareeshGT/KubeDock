"""Headless tests for DockerTab (offscreen Qt, SSH worker faked)."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

import docker_tab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _row(**kw):
    return json.dumps(kw)


PS_OUT = "\n".join([
    _row(ID="aaa111", Names="web", Image="nginx:1.27", State="running",
         Status="Up 2 hours", Ports="0.0.0.0:80->80/tcp"),
    _row(ID="bbb222", Names="db", Image="postgres:16", State="exited",
         Status="Exited (0) 3 days ago", Ports=""),
    _row(ID="ccc333", Names="cache", Image="redis:7", State="paused",
         Status="Up 1 hour (Paused)", Ports=""),
])
IMAGES_OUT = _row(ID="sha256:111", Repository="nginx", Tag="1.27", CreatedSince="2 weeks ago", Size="188MB")
VOLUMES_OUT = _row(Name="pgdata", Driver="local", Scope="local")
NETWORKS_OUT = _row(ID="net1", Name="backend", Driver="bridge", Scope="local")


class FakeWorker(QObject):
    """Stands in for CommandWorker: answers synchronously from `responses`."""

    result = pyqtSignal(str, str, int)
    error = pyqtSignal(str)
    commands = []
    responses = {}

    def __init__(self, ssh, cmd, cwd=None, sudo_user=None, timeout=None, raw=False):
        super().__init__()
        self.cmd = cmd
        self.sudo_user = sudo_user
        FakeWorker.commands.append(cmd)

    def start(self):
        for prefix, (out, err, code) in FakeWorker.responses.items():
            if self.cmd.startswith(prefix):
                self.result.emit(out, err, code)
                return
        self.result.emit("", "", 0)


@pytest.fixture()
def tab(qapp, monkeypatch):
    FakeWorker.commands = []
    FakeWorker.responses = {
        "docker ps": (PS_OUT, "", 0),
        "docker images": (IMAGES_OUT, "", 0),
        "docker volume ls": (VOLUMES_OUT, "", 0),
        "docker network ls": (NETWORKS_OUT, "", 0),
    }
    monkeypatch.setattr(docker_tab, "CommandWorker", FakeWorker)
    monkeypatch.setattr(docker_tab, "track_worker", lambda pool, w: w)
    t = docker_tab.DockerTab()
    t.set_ssh(object())
    t.refresh_all()
    return t


def _select(tab, kind, name):
    table = tab._tables[kind]
    for r in range(table.rowCount()):
        if table.item(r, 0).text() == name:
            table.selectRow(r)
            return
    raise AssertionError("row not found: " + name)


def test_refresh_populates_all_tables(tab):
    assert tab._tables["containers"].rowCount() == 3
    assert tab._tables["images"].rowCount() == 1
    assert tab._tables["volumes"].rowCount() == 1
    assert tab._tables["networks"].rowCount() == 1
    assert tab._tables["containers"].item(0, 0).text() == "web"
    assert not tab.banner.isVisible() or tab.banner.text() == ""


def test_buttons_follow_container_state(tab):
    b = lambda key: tab._buttons[("containers", key)]  # noqa: E731
    assert not b("start").isEnabled()  # nothing selected yet

    _select(tab, "containers", "web")  # running
    assert b("stop").isEnabled() and b("restart").isEnabled()
    assert b("pause").isEnabled() and b("exec").isEnabled()
    assert not b("start").isEnabled() and not b("unpause").isEnabled()

    _select(tab, "containers", "db")  # exited
    assert b("start").isEnabled()
    assert not b("stop").isEnabled() and not b("exec").isEnabled()

    _select(tab, "containers", "cache")  # paused
    assert b("unpause").isEnabled() and not b("pause").isEnabled()


def test_filter_hides_non_matching_rows(tab):
    tab._filters["containers"].setText("postgres")
    table = tab._tables["containers"]
    hidden = {table.item(r, 0).text(): table.isRowHidden(r) for r in range(table.rowCount())}
    assert hidden == {"web": True, "db": False, "cache": True}


def test_selection_survives_refresh(tab):
    _select(tab, "containers", "db")
    tab.refresh("containers")
    assert tab._selected_ident("containers") == "db"


def test_container_action_runs_command_and_refreshes(tab):
    _select(tab, "containers", "db")
    FakeWorker.commands.clear()
    tab._container_action("start")
    assert FakeWorker.commands[0] == "docker start db"
    assert any(c.startswith("docker ps") for c in FakeWorker.commands[1:])


def test_remove_running_container_forces_after_confirm(tab, monkeypatch):
    _select(tab, "containers", "web")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._remove("containers")
    assert FakeWorker.commands[0] == "docker rm -f web"


def test_remove_stopped_container_not_forced(tab, monkeypatch):
    _select(tab, "containers", "db")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._remove("containers")
    assert FakeWorker.commands[0] == "docker rm db"


def test_remove_declined_runs_nothing(tab, monkeypatch):
    _select(tab, "containers", "db")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    FakeWorker.commands.clear()
    tab._remove("containers")
    assert FakeWorker.commands == []


def test_remove_image_uses_id(tab, monkeypatch):
    _select(tab, "images", "nginx")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._remove("images")
    assert FakeWorker.commands[0] == "docker rmi sha256:111"


def test_docker_missing_shows_friendly_banner(qapp, monkeypatch):
    FakeWorker.commands = []
    FakeWorker.responses = {"docker ": ("", "bash: docker: command not found", 127)}
    monkeypatch.setattr(docker_tab, "CommandWorker", FakeWorker)
    monkeypatch.setattr(docker_tab, "track_worker", lambda pool, w: w)
    t = docker_tab.DockerTab()
    t.set_ssh(object())
    t.refresh_all()
    assert "not installed" in t.banner.text()
    assert t._tables["containers"].rowCount() == 0


def test_disconnect_clears_tables(tab):
    tab.set_ssh(None)
    assert all(t.rowCount() == 0 for t in tab._tables.values())
    assert "Connect to a VM" in tab.banner.text()
    assert not tab._buttons[("containers", "stop")].isEnabled()


def test_sudo_user_is_passed_to_worker(tab):
    seen = []
    orig = docker_tab.CommandWorker

    class Spy(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.sudo_user)

    docker_tab.CommandWorker = Spy
    try:
        tab._sudo_user = "ubuntu"
        tab.refresh("volumes")
    finally:
        docker_tab.CommandWorker = orig
    assert seen == ["ubuntu"]
