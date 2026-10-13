"""Regression tests for low-overhead Kubernetes resource loading."""

from k8s_metrics import (
    LIMITS_MARKER,
    NODES_JSON_MARKER,
    TOP_MARKER,
    TOP_NODES_MARKER,
    build_pod_list_command,
    build_pod_metrics_command,
    build_pods_command,
    refresh_result_is_current,
    split_pod_sections,
)
from command_utils import (
    build_prepared_command,
    command_operation_label,
)


def test_prepared_command_uses_shell_home_instead_of_probe_result():
    command = build_prepared_command("kubectl get pods")

    assert command.startswith(
        'export PATH="$HOME:$HOME/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH";'
    )
    assert "echo $HOME" not in command
    assert command.endswith("kubectl get pods")


def test_sudo_path_setup_runs_inside_target_users_shell():
    command = build_prepared_command("kubectl get pods", sudo_user="build user")

    assert command.startswith("sudo -u 'build user' sh -c '")
    assert 'export PATH="$HOME:$HOME/bin:' in command
    assert command.endswith("kubectl get pods'")


def test_working_directory_with_spaces_is_quoted():
    command = build_prepared_command("ls", cwd="/tmp/my workspace")
    assert "cd '/tmp/my workspace' 2>/dev/null; ls" in command


def test_operation_labels_do_not_include_resource_names_or_arguments():
    label = command_operation_label(
        "kubectl --context=prod get pods -n private-namespace secret-pod-123"
    )
    assert label == "kubectl get pods"
    assert "prod" not in label
    assert "private-namespace" not in label
    assert "secret-pod-123" not in label


def test_primary_pod_list_command_is_independent_of_metrics():
    command = build_pod_list_command("-n staging", "--context=prod")

    assert command == "kubectl --context=prod get pods -n staging -o wide 2>&1"
    assert command.count("kubectl ") == 1
    assert "top pods" not in command
    assert "jsonpath" not in command


def test_metrics_command_omits_redundant_node_usage_request():
    command = build_pod_metrics_command("-n staging", "--context=prod")

    assert f"echo {TOP_MARKER}" in command
    assert f"echo {LIMITS_MARKER}" in command
    assert f"echo {TOP_NODES_MARKER}" in command
    assert f"echo {NODES_JSON_MARKER}" in command
    assert "kubectl --context=prod top pods -A" in command
    assert "kubectl --context=prod get pods -n staging" in command
    assert "kubectl --context=prod get nodes" in command
    assert "kubectl --context=prod top nodes" not in command
    assert command.count("kubectl ") == 3
    legacy = build_pods_command("-n staging", "--context=prod")
    assert legacy.count("kubectl ") == 5
    # The active UI path combines this three-command support query with one
    # primary list request, reducing refresh calls from five to four.
    assert build_pod_list_command("-n staging", "--context=prod").count("kubectl ") + command.count("kubectl ") == 4


def test_legacy_combined_builder_stays_compatible():
    """Keep the existing helper usable by callers outside the main UI path."""
    command = build_pods_command("--all-namespaces", "--context=prod")
    assert "kubectl --context=prod get pods --all-namespaces" in command
    assert "kubectl --context=prod top pods -A" in command
    assert f"echo {NODES_JSON_MARKER}" in command


def test_old_or_wrong_context_results_are_rejected():
    assert refresh_result_is_current(4, 4, ("prod", "staging"), ("prod", "staging"))
    assert not refresh_result_is_current(3, 4, ("prod", "staging"), ("prod", "staging"))
    assert not refresh_result_is_current(4, 4, ("prod", "default"), ("prod", "staging"))


def test_metrics_sections_are_split_after_primary_rows():
    output = """NAME READY STATUS RESTARTS AGE IP NODE
pod-a 1/1 Running 0 1m 10.0.0.1 node-a
__KD_TOP__
NAMESPACE NAME CPU MEMORY
staging pod-a 25m 32Mi
__KD_LIMITS__
staging|pod-a|100m,128Mi;
__KD_TOP_NODES__
__KD_NODES_JSON__
{"items": []}
"""
    pods, top, limits, top_nodes, nodes = split_pod_sections(output)
    assert "pod-a 1/1 Running" in pods
    assert "staging pod-a 25m 32Mi" in top
    assert "staging|pod-a|100m,128Mi;" in limits
    assert top_nodes == ""
    assert '{"items": []}' in nodes
