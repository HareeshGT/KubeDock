"""Kubernetes management tab.

The public KubernetesTab class remains the stable entry point while feature
implementations live in focused mixin modules under kubernetes_tab_parts/.
"""

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import pyqtSignal, QTimer
from themes import load_settings
from utils import REMOTE_TUNNEL_CSV_PATH
from kubernetes_tab_parts.core import KubernetesCoreMixin
from kubernetes_tab_parts.ui import KubernetesUiMixin
from kubernetes_tab_parts.workloads import KubernetesWorkloadsMixin
from kubernetes_tab_parts.storage_config_events import KubernetesStorageConfigEventsMixin
from kubernetes_tab_parts.resources import KubernetesResourcesMixin
from kubernetes_tab_parts.tunnels import KubernetesTunnelsMixin

class KubernetesTab(KubernetesCoreMixin, KubernetesUiMixin, KubernetesWorkloadsMixin, KubernetesStorageConfigEventsMixin, KubernetesResourcesMixin, KubernetesTunnelsMixin, QWidget):
  status_msg = pyqtSignal(str)
  # Emitted whenever the selected kubeconfig context changes. Other tabs
  # (Dashboard, future observability views, etc.) can follow the same
  # cluster without touching the jump host's global current-context.
  context_changed = pyqtSignal(str)

  # Cap on simultaneous tunnel-restart CommandWorkers. Each worker's
  # run() opens TWO channels on the shared SSH transport (one exec_command
  # to probe $HOME, one for the actual restart command) — see workers.py.
  # sshd's default MaxSessions caps concurrent channels per connection at
  # 10, so firing off every selected service's worker at once (previously
  # all of them, unbounded) blew past that ceiling once more than ~5
  # services were selected, and every worker past the limit failed with
  # ChannelException(2, 'Connect failed') / "Unable to open channel."
  # Staying at 4 concurrent workers (≤8 channels) keeps headroom under
  # the default limit even on servers with other channels already open.
  MAX_CONCURRENT_TUNNEL_RESTARTS = 4

  def __init__(self, parent=None):
    super().__init__(parent)
    self.ssh     = None
    self._sudo_user = None
    self._current_ns = "default"
    self._current_context = ""
    self._cluster_available = False
    self._cluster_probe_in_progress = False
    self._contexts = []
    self._contexts_pending_select = None
    self._namespaces = []
    self._namespaces_pending_select = None
    self._workers   = []
    # Inline " AI" button on pod cards (see _on_pod_card_ai_requested):
    # single-flight state so a second click (on any card) while a
    # diagnosis is already running for one pod just no-ops rather than
    # overlapping requests / dialogs.
    self._pod_ai_pod    = None  # name of the pod currently being diagnosed, if any
    self._pod_ai_card    = None  # the PodCardWidget that started it, so its button can be re-enabled
    self._pod_ai_log_worker = None
    self._pod_ai_worker   = None
    self._pod_ai_dialog   = None
    self._events_raw = ""
    self._events_warnings_only = False
    self._auto_refresh_timer = QTimer(self)
    self._auto_refresh_timer.timeout.connect(self._auto_refresh)
    # Local (client-side) connection details, used to run the SSH
    # tunnel on the machine running this app rather than over the
    # existing remote `self.ssh` session.
    self._conn_host  = None
    self._conn_port  = 22
    self._conn_user  = None
    self._conn_pem  = None
    self._tunnel_services = []
    self._tunnel_col_widths = (20, 20)
    self._tunnel_process = None
    # "all" | "active" | "inactive" — set by the status-filter toggle
    # in the Tunnels tab; combined with the text search in
    # _filter_tunnel_services().
    self._tunnel_status_filter = "all"
    # Remote CSV path tunnel services are read from/written to — lets
    # each person point this at their own file (e.g. a per-project or
    # per-team convention) instead of being locked to the hardcoded
    # default. Persisted across restarts via themes.save_settings.
    self._tunnel_csv_path = load_settings().get("tunnel_csv_path") or REMOTE_TUNNEL_CSV_PATH
    self._build_ui()

  # ── Kubernetes context helpers ───────────────────────────
