"""Regression tests for hybrid SSH connection autoscaling."""

import pytest

from workers_parts import ssh as ssh_parts


class FakeTransport:
    def __init__(self, active=True):
        self.active = active

    def is_active(self):
        return self.active


class FakeSSH:
    def __init__(self):
        self.transport = FakeTransport()
        self.close_calls = 0

    def get_transport(self):
        return self.transport

    def close(self):
        self.close_calls += 1


def make_pool(monkeypatch, **kwargs):
    pool = ssh_parts.SSHConnectionPool(
        "host", 22, "operator", maintenance_interval=60, **kwargs
    )
    created = []

    def connect(**_kwargs):
        ssh = FakeSSH()
        created.append(ssh)
        return ssh

    monkeypatch.setattr(pool, "_connect_secondary", connect)
    return pool, created


def reserve(pool, count):
    slots = []
    for _ in range(count):
        slots.append(pool._acquire_slot(None))
    return slots


def release_all(pool, slots):
    for slot in slots:
        pool._release_slot(slot)


def test_default_pool_limit_counts_primary_and_allows_four_secondaries(monkeypatch):
    pool, created = make_pool(monkeypatch)
    try:
        # Five slots at the 70% scale threshold, followed by a fifth total
        # connection (one primary + four secondary transports).
        slots = reserve(pool, 20)
        status = pool.snapshot()
        assert status["max_connections"] == 5
        assert status["total_connections"] == 5
        assert status["secondary_connections"] == 4
        assert len(created) == 4
        assert status["active_channels"] == 20
        release_all(pool, slots)
    finally:
        pool.close()


def test_pool_scales_out_before_saturating_existing_transport(monkeypatch):
    pool, created = make_pool(monkeypatch, max_total_connections=3)
    try:
        first = reserve(pool, 5)
        assert pool.snapshot()["secondary_connections"] == 1

        sixth = pool._acquire_slot(None)
        assert pool.snapshot()["secondary_connections"] == 2
        assert sixth is not first[0]
        assert len(created) == 2

        # New work is assigned to the least-loaded connection.
        seventh = pool._acquire_slot(None)
        assert seventh is sixth
        assert first[0].active_channels == 5
        assert sixth.active_channels == 2
        release_all(pool, first + [sixth, seventh])
    finally:
        pool.close()


def test_pool_respects_five_total_connection_cap_under_pressure(monkeypatch):
    pool, created = make_pool(monkeypatch)
    try:
        slots = reserve(pool, 24)
        status = pool.snapshot()
        assert status["total_connections"] == 5
        assert status["secondary_connections"] == 4
        assert status["active_channels"] == 24
        assert all(slot.active_channels == 6 for slot in pool._slots)
        assert len(created) == 4
        release_all(pool, slots)
    finally:
        pool.close()


def test_idle_secondary_connections_are_reaped_but_primary_is_retained(monkeypatch):
    pool, created = make_pool(monkeypatch, idle_timeout_seconds=0)
    try:
        slots = reserve(pool, 7)
        release_all(pool, slots)
        assert pool.snapshot()["secondary_connections"] == 2

        removed = pool._reap_idle_connections()
        assert removed == 2
        status = pool.snapshot()
        assert status["total_connections"] == 1
        assert status["secondary_connections"] == 0
        assert all(ssh.close_calls == 1 for ssh in created)
    finally:
        pool.close()


def test_active_secondary_connection_is_not_reaped(monkeypatch):
    pool, _created = make_pool(monkeypatch, idle_timeout_seconds=0)
    try:
        slot = pool._acquire_slot(None)
        assert pool._reap_idle_connections() == 0
        assert pool.snapshot()["secondary_connections"] == 1
        pool._release_slot(slot)
        assert pool._reap_idle_connections() == 1
    finally:
        pool.close()


def test_pool_configuration_can_lower_runtime_limit(monkeypatch):
    pool, _created = make_pool(monkeypatch)
    try:
        pool.configure(max_total_connections=2)
        assert pool.snapshot()["max_connections"] == 2
        slots = reserve(pool, 12)
        assert pool.snapshot()["total_connections"] == 2
        assert pool.snapshot()["secondary_connections"] == 1
        release_all(pool, slots)
    finally:
        pool.close()


def test_inactive_transports_are_evicted_before_reuse(monkeypatch):
    pool, created = make_pool(monkeypatch, max_total_connections=2)
    try:
        first = pool._acquire_slot(None)
        first.ssh.transport.active = False
        second = pool._acquire_slot(None)
        assert second is not first
        assert first.ssh.close_calls == 1
        assert len(created) == 2
        pool._release_slot(second)
    finally:
        pool.close()
