"""Dashboard domain-service tests."""

from services.dashboard import DashboardService


class FakeConnection:
    pass


class FakeCommands:
    pass


def test_contextual_command_pins_all_kubectl_lines_once():
    service = DashboardService(FakeConnection(), FakeCommands())
    command = "kubectl get pods\necho done; kubectl get nodes"
    result = service.contextual_command(command, "dev cluster")

    assert result.count("--context='dev cluster'") == 2
    assert "kubectl --context='dev cluster' get pods" in result
    assert "kubectl --context='dev cluster' get nodes" in result


def test_process_snapshot_parser_extracts_linux_metrics_and_processes():
    output = """__TOP_PROCESS__
top - 10:00:00 up 3 days, 2 users, load average: 0.10, 0.20, 0.30
Tasks: 120 total, 1 running, 119 sleeping, 0 stopped, 0 zombie
%Cpu(s): 2.0 us, 1.0 sy, 0.0 ni, 96.0 id, 1.0 wa, 0.0 hi, 0.0 si, 0.0 st
KiB Mem : 1000000 total, 200000 free, 500000 used, 300000 buff/cache
PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
123 root 20 0 1000 200 100 S 35.0 5.0 0:01.00 python worker
"""
    result = DashboardService.parse_process_snapshot(output)

    assert result["uptime"] == "3 days"
    assert result["load"] == "0.10, 0.20, 0.30"
    assert result["tasks"] == "120 total · 1 running"
    assert result["cpu"] == "4.0% used"
    assert result["processes"][0]["pid"] == "123"
    assert result["processes"][0]["command"] == "python worker"


def test_process_snapshot_parser_returns_none_for_missing_marker():
    assert DashboardService.parse_process_snapshot("no process data") is None
