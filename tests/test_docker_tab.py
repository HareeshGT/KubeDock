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


def _resource_item(view, name):
    for index in range(view.count()):
        item = view.item(index)
        row = item.data(Qt.UserRole) or {}
        if item.data(Qt.UserRole + 3) == "resource" and row.get("Names") == name:
            return item
    raise AssertionError("resource card not found: " + name)


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


def test_card_grids_render_each_resource_type(tab):
    assert {kind: sum(
        1 for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource"
    ) for kind, view in tab._views.items()} == {
        "containers": 3, "images": 1, "volumes": 1, "networks": 1
    }
    container = tab._views["containers"].itemWidget(_resource_item(tab._views["containers"], "web"))
    image = tab._views["images"].itemWidget(tab._views["images"].item(0))
    volume = tab._views["volumes"].itemWidget(tab._views["volumes"].item(0))
    network = tab._views["networks"].itemWidget(tab._views["networks"].item(0))
    assert container.title_lbl.toolTip() == "web"
    assert "nginx:1.27" in " ".join(label.text() for label in container.detail_labels)
    assert image.title_lbl.toolTip() == "nginx:1.27"
    assert volume.title_lbl.toolTip() == "pgdata"
    assert network.title_lbl.toolTip() == "backend"


def test_refresh_reuses_cards_when_resources_are_unchanged(tab):
    view = tab._views["containers"]
    original = view.itemWidget(_resource_item(view, "web"))
    tab.refresh("containers")
    assert view.itemWidget(_resource_item(view, "web")) is original


def test_card_click_selects_existing_resource_model(tab):
    view = tab._views["containers"]
    tab._select_card_item("containers", _resource_item(view, "db"))
    assert tab._selected_ident("containers") == "db"
    assert tab._buttons[("containers", "start")].isEnabled()
    assert not tab._buttons[("containers", "stop")].isEnabled()


def test_filter_hides_non_matching_cards(tab):
    tab._filters["containers"].setText("postgres")
    view = tab._views["containers"]
    hidden = {
        (view.item(i).data(Qt.UserRole) or {})["Names"]: view.item(i).isHidden()
        for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource"
    }
    assert hidden == {"web": True, "db": False, "cache": True}


def test_empty_resource_results_clear_cards(tab):
    FakeWorker.responses["docker images"] = ("", "", 0)
    tab.refresh("images")
    assert tab._tables["images"].rowCount() == 0
    assert tab._views["images"].count() == 0


def test_card_handles_missing_metadata_and_long_names(tab):
    long_name = "service-" + ("very-long-name-" * 8)
    tab._populate("volumes", [{"Name": long_name}])
    card = tab._views["volumes"].itemWidget(tab._views["volumes"].item(0))
    assert card.title_lbl.toolTip() == long_name
    assert "Unavailable" in " ".join(label.text() for label in card.detail_labels)
    assert card.action_buttons["inspect"].isEnabled()


def test_container_stats_are_merged_in_one_batch(tab):
    stats = "\n".join([
        _row(ID="aaa111", Container="aaa111", Name="web",
             CPUPerc="1.20%", MemUsage="20MiB / 1GiB", MemPerc="2.00%"),
        _row(ID="ccc333", Container="ccc333", Name="cache",
             CPUPerc="0.30%", MemUsage="5MiB / 1GiB", MemPerc="0.50%"),
    ])
    tab._on_container_stats(stats, "", 0)
    cards = tab._views["containers"]
    web = cards.itemWidget(_resource_item(cards, "web"))
    cache = cards.itemWidget(_resource_item(cards, "cache"))
    assert "CPU 1.20%" in " ".join(label.text() for label in web.detail_labels)
    assert "Memory 20MiB / 1GiB" in " ".join(label.text() for label in web.detail_labels)
    assert "CPU 0.30%" in " ".join(label.text() for label in cache.detail_labels)


def test_batched_network_inspect_shows_connected_container_count(tab):
    detail = _row(Name="backend", Driver="bridge", Scope="local",
                  Containers={"id1": {}, "id2": {}})
    tab._on_resource_details("networks", detail, "", 0)
    card = tab._views["networks"].itemWidget(tab._views["networks"].item(0))
    assert "Connected containers  ·  2" in " ".join(label.text() for label in card.detail_labels)


def test_batched_volume_inspect_shows_mountpoint(tab):
    detail = _row(Name="pgdata", Driver="local", Scope="local",
                  Mountpoint="/var/lib/docker/volumes/pgdata/_data")
    tab._on_resource_details("volumes", detail, "", 0)
    card = tab._views["volumes"].itemWidget(tab._views["volumes"].item(0))
    assert "/var/lib/docker/volumes/pgdata/_data" in " ".join(
        label.text() for label in card.detail_labels)


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
    assert FakeWorker.commands[0] == "docker start bbb222"
    assert any(c.startswith("docker ps") for c in FakeWorker.commands[1:])


def test_remove_running_container_forces_after_confirm(tab, monkeypatch):
    _select(tab, "containers", "web")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._remove("containers")
    assert FakeWorker.commands[0] == "docker rm -f aaa111"


def test_remove_stopped_container_not_forced(tab, monkeypatch):
    _select(tab, "containers", "db")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._remove("containers")
    assert FakeWorker.commands[0] == "docker rm bbb222"


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
    assert seen and all(username == "ubuntu" for username in seen)


def test_sort_controls_reorder_locally_and_keep_selection(tab):
    _select(tab, "containers", "db")
    combo, order = tab._sort_controls["containers"]
    before = list(FakeWorker.commands)
    combo.setCurrentIndex(combo.findData("name"))
    names = [
        tab._tables["containers"].item(i, 0).text()
        for i in range(tab._tables["containers"].rowCount())
    ]
    assert names == ["cache", "db", "web"]
    assert tab._selected_ident("containers") == "db"
    order.setCurrentIndex(order.findData(True))
    names_desc = [
        tab._tables["containers"].item(i, 0).text()
        for i in range(tab._tables["containers"].rowCount())
    ]
    assert names_desc == ["web", "db", "cache"]
    assert tab._selected_ident("containers") == "db"
    assert FakeWorker.commands == before


def test_actions_still_target_selected_resource_after_sort(tab):
    _select(tab, "containers", "db")
    combo, _order = tab._sort_controls["containers"]
    combo.setCurrentIndex(combo.findData("image"))
    FakeWorker.commands.clear()
    tab._container_action("start")
    assert FakeWorker.commands[0] == "docker start bbb222"


def test_compose_groups_render_expand_and_collapse_without_extra_ssh(tab):
    rows = [
        {"ID": "a1", "Names": "web", "Image": "nginx", "State": "running",
         "Status": "Up", "Labels": "com.docker.compose.project=shop,com.docker.compose.service=web"},
        {"ID": "a2", "Names": "db", "Image": "postgres", "State": "exited",
         "Status": "Exited", "Labels": "com.docker.compose.project=shop,com.docker.compose.service=db"},
        {"ID": "b1", "Names": "manual", "Image": "busybox", "State": "paused",
         "Status": "Paused", "Labels": ""},
    ]
    before = list(FakeWorker.commands)
    tab._populate("containers", rows)
    view = tab._views["containers"]
    group_items = [
        view.item(i) for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "group"
    ]
    assert len(group_items) == 2
    shop = next(item for item in group_items if item.data(Qt.UserRole)["name"] == "shop")
    header = view.itemWidget(shop)
    assert "2 containers" in header.text()
    tab._toggle_compose_group("compose:shop")
    assert all(
        view.item(i).isHidden()
        for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource"
        and docker_tab.du.compose_metadata(view.item(i).data(Qt.UserRole))[0] == "shop"
    )
    tab._toggle_compose_group("compose:shop")
    assert FakeWorker.commands == before


def test_compose_filter_matches_project_and_service_labels(tab):
    tab._populate("containers", [
        {"ID": "x1", "Names": "api", "Image": "api:v1", "State": "running",
         "Status": "Up", "Labels": "com.docker.compose.project=backend,com.docker.compose.service=rest"},
        {"ID": "x2", "Names": "database", "Image": "postgres", "State": "running",
         "Status": "Up", "Labels": "com.docker.compose.project=store,com.docker.compose.service=db"},
    ])
    tab._filters["containers"].setText("backend")
    view = tab._views["containers"]
    visible_names = [
        (view.item(i).data(Qt.UserRole) or {}).get("Names")
        for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource" and not view.item(i).isHidden()
    ]
    assert visible_names == ["api"]
    tab._filters["containers"].setText("rest")
    visible_names = [
        (view.item(i).data(Qt.UserRole) or {}).get("Names")
        for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource" and not view.item(i).isHidden()
    ]
    assert visible_names == ["api"]


def test_cleanup_preview_does_not_prune_until_user_selects_and_confirms(tab, monkeypatch):
    # A preview itself must only issue the read-only inspection command.
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    FakeWorker.responses["printf"] = (
        "__KD_STOPPED_CONTAINERS__\n\n"
        "__KD_DANGLING_IMAGES__\n\n"
        "__KD_ALL_IMAGES__\n\n"
        "__KD_USED_IMAGE_IDS__\n\n"
        "__KD_BUILDER_CACHE__\n", "", 0
    )
    before = len(FakeWorker.commands)
    tab._cleanup_preview()
    preview_commands = FakeWorker.commands[before:]
    assert preview_commands
    assert "docker buildx du" in preview_commands[0]
    assert "docker rm " not in preview_commands[0]
    assert "docker image rm " not in preview_commands[0]
    assert "docker buildx prune" not in preview_commands[0]


def test_cleanup_cancel_keeps_resources_untouched(tab, monkeypatch):
    report = docker_tab.du.parse_cleanup_preview(
        "__KD_STOPPED_CONTAINERS__\n" + '{"ID":"abc123","Names":"old"}' +
        "\n__KD_DANGLING_IMAGES__\n__KD_ALL_IMAGES__\n__KD_USED_IMAGE_IDS__\n__KD_BUILDER_CACHE__"
    )
    tab._cleanup_report = report
    monkeypatch.setattr(docker_tab.QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(docker_tab.QInputDialog, "getItem", lambda *a, **k: ("Stopped containers (1)", True))
    monkeypatch.setattr(docker_tab.QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    FakeWorker.commands.clear()
    tab._on_cleanup_preview(
        "__KD_STOPPED_CONTAINERS__\n" + '{"ID":"abc123","Names":"old"}' +
        "\n__KD_DANGLING_IMAGES__\n__KD_ALL_IMAGES__\n__KD_USED_IMAGE_IDS__\n__KD_BUILDER_CACHE__",
        "", 0, tab._connection_generation)
    assert not any(command.startswith(("docker rm ", "docker image rm ", "docker buildx prune"))
                   for command in FakeWorker.commands)


def test_selection_survives_sort_with_duplicate_container_display_names(tab):
    tab._populate("containers", [
        {"ID": "aaa111", "Names": "same-name", "Image": "nginx",
         "State": "running", "Status": "Up"},
        {"ID": "bbb222", "Names": "same-name", "Image": "redis",
         "State": "exited", "Status": "Exited"},
    ])
    table = tab._tables["containers"]
    table.selectRow(1)
    assert tab._selected_identity("containers") == "bbb222"
    combo, _order = tab._sort_controls["containers"]
    combo.setCurrentIndex(combo.findData("state"))
    assert tab._selected_identity("containers") == "bbb222"
    FakeWorker.commands.clear()
    tab._container_action("start")
    assert FakeWorker.commands[0] == "docker start bbb222"


def test_confirmed_cleanup_runs_only_selected_operation(tab, monkeypatch):
    preview = (
        '__KD_STOPPED_CONTAINERS__\n{"ID":"abc123","Names":"old-worker","Size":"4MB"}\n'
        '__KD_DANGLING_IMAGES__\n__KD_ALL_IMAGES__\n'
        '__KD_USED_IMAGE_IDS__\n__KD_BUILDER_CACHE__'
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(docker_tab.QInputDialog, "getItem",
                        lambda *a, **k: ("Stopped containers (1)", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    FakeWorker.commands.clear()
    tab._on_cleanup_preview(preview, "", 0, tab._connection_generation)
    destructive = [command for command in FakeWorker.commands
                   if command.startswith(("docker rm ", "docker image rm ", "docker buildx prune"))]
    assert destructive == ["docker rm abc123"]


def test_filter_still_applies_after_sorting(tab):
    combo, _order = tab._sort_controls["containers"]
    combo.setCurrentIndex(combo.findData("image"))
    tab._filters["containers"].setText("postgres")
    view = tab._views["containers"]
    visible_names = [
        (view.item(i).data(Qt.UserRole) or {}).get("Names")
        for i in range(view.count())
        if view.item(i).data(Qt.UserRole + 3) == "resource" and not view.item(i).isHidden()
    ]
    assert visible_names == ["db"]
