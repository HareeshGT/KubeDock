"""dashboard_tab.py — Live VM + Kubernetes node dashboard tab.

Shows the connected instance's specs (hostname/OS/kernel, CPU, RAM, disk)
and — when a cluster is reachable — a cluster overview plus one-card-per-node
summary with live CPU/memory usage, capacity/allocatable resources,
Kubernetes/runtime metadata, and MemoryPressure/DiskPressure/PIDPressure
conditions. Double-clicking a node opens a separate window with the
pods actually running on it, including each pod's own CPU/memory usage,
restart count, and ready state (see NodeDetailWindow below).

Polling behaviour: while the tab is the visible tab in main_tabs it ticks
its normal refresh timer (REFRESH_MS). The owner (main_window.py) calls
set_active(True/False) from its main_tabs.currentChanged handler;
navigating away stops that timer and starts a separate background timer
that runs the SAME collection cycle at most once per BACKGROUND_REFRESH_MS.
Background results are cached (not rendered) and shown when the tab is
re-opened.

Node/pod layout: the nodes table itself only ever shows one row per node
(no inline pod children — that's what made the panel feel cramped).
Double-clicking a node opens a separate, independent top-level window
(NodeDetailWindow) listing the pods scheduled on that node along with
their CPU/memory usage, restart count, and ready state. Each open node
window is kept in self._node_windows and is refreshed in place on every
dashboard tick (piggy-backing on the same SSH round-trip the main tab
already makes — no extra polling), rather than issuing its own SSH calls.
"""
import json
import os
import re
import time
import shlex
import stat as _stat
import posixpath
from datetime import datetime
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame, QScrollArea, QTreeWidget, QTreeWidgetItem, QGraphicsDropShadowEffect, QSizePolicy, QComboBox
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QVariantAnimation, QEasingCurve, QEvent
from PyQt5.QtGui import QColor, QPainter, QPen, QPainterPath
from ui_icons import set_icon, icon_pixmap, apply_text_icon, add_icon_tab, split_icon_text, icon_button
from themes import T
from workers import CommandWorker, track_worker
from utils import monospace_font, size_fmt
from progress_ring import CircularProgress
REFRESH_MS = 3000
BACKGROUND_REFRESH_MS = 60000  # unfocused cadence: at most one collection per minute
_PODMAP_CMD = '\npodmap_status=0\npodmap_output=$(kubectl get pods --all-namespaces -o custom-columns=\'NAMESPACE:.metadata.namespace,NAME:.metadata.name,NODE:.spec.nodeName\' --no-headers 2>/dev/null) || podmap_status=$?\necho __PODNODEMAP__\nprintf \'%s\\n\' "$podmap_output"\necho __PODNODEMAP_STATUS__\nprintf \'%s\\n\' "$podmap_status"\n'

class _FTPDashboardWorker(QThread):
    """Collect richer FTP/FTPS server and current-directory information.

  A short-lived second FTP control connection is used for dashboard reads so
  the live file-transfer connection is never shared across threads. That
  avoids interleaving PWD/FEAT/MLSD with an upload/download operation.
  """
    done = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, fs, path):
        super().__init__()
        self.fs = fs
        self.path = path
        self.finished.connect(self.deleteLater)

    @staticmethod
    def _clean_features(raw):
        out = []
        for line in str(raw or '').splitlines():
            line = line.strip().lstrip('-').strip()
            if not line or line.upper() in {'211 END', '211 END OF FEAT'} or line.startswith('211') or (line.upper() == 'FEAT'):
                continue
            token = line.split()[0] if line else ''
            if token and token.upper() not in {'211', 'FEAT'} and (token not in out):
                out.append(token)
        return out

    @staticmethod
    def _format_age(seconds):
        if seconds is None:
            return '—'
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f'{seconds}s'
        minutes = seconds // 60
        if minutes < 60:
            return f'{minutes}m'
        hours = minutes // 60
        if hours < 24:
            return f'{hours}h {minutes % 60}m'
        days = hours // 24
        return f'{days}d {hours % 24}h'

    def run(self):
        probe = None
        try:
            from ftp_fs import FTPFS
            if not self.fs or not getattr(self.fs, '_ftp', None):
                raise RuntimeError('FTP connection is not active')
            probe = FTPFS(self.fs.host, self.fs.port, self.fs.user, self.fs.password, tls=getattr(self.fs, 'tls', False), passive=getattr(self.fs, 'passive', True), timeout=min(int(getattr(self.fs, 'timeout', 15) or 15), 10), initial_path='/')
            ftp = probe._ftp
            current = probe.normalize(self.path or '/')
            started = time.monotonic()
            try:
                ftp.voidcmd('NOOP')
                latency_ms = round((time.monotonic() - started) * 1000.0, 1)
            except Exception:
                latency_ms = None
            try:
                pwd = ftp.pwd() or current
            except Exception:
                pwd = current
            welcome = str(getattr(ftp, 'welcome', '') or '').strip()
            try:
                syst = str(ftp.sendcmd('SYST') or '').strip()
            except Exception:
                syst = 'Unavailable'
            features = []
            try:
                features = self._clean_features(ftp.sendcmd('FEAT'))
            except Exception:
                pass
            entries = probe.listdir_attr(current)
            files = folders = 0
            total_bytes = 0
            largest_name = '—'
            largest_size = 0
            newest_name = '—'
            newest_ts = None
            extensions = {}
            for e in entries:
                mode = int(getattr(e, 'st_mode', 0) or 0)
                is_dir = _stat.S_ISDIR(mode)
                name = str(getattr(e, 'filename', '') or '')
                if is_dir:
                    folders += 1
                else:
                    files += 1
                    size = int(getattr(e, 'st_size', 0) or 0)
                    total_bytes += size
                    if size > largest_size:
                        largest_size = size
                        largest_name = name or '—'
                    suffix = os.path.splitext(name)[1].lower().lstrip('.')
                    if suffix:
                        extensions[suffix] = extensions.get(suffix, 0) + 1
            sample_files = [e.filename for e in entries if not _stat.S_ISDIR(int(getattr(e, 'st_mode', 0) or 0))][:20]
            for name in sample_files:
                try:
                    reply = ftp.sendcmd('MDTM ' + probe.normalize(posixpath.join(current, name)))
                    digits = reply.split()[-1]
                    if digits.isdigit() and len(digits) >= 14:
                        dt = datetime.strptime(digits[:14], '%Y%m%d%H%M%S')
                        ts = dt.timestamp()
                        if newest_ts is None or ts > newest_ts:
                            newest_ts, newest_name = (ts, name)
                except Exception:
                    continue
            sock = getattr(ftp, 'sock', None)
            tls_version = tls_cipher = None
            try:
                if sock is not None and hasattr(sock, 'version'):
                    tls_version = sock.version()
                if sock is not None and hasattr(sock, 'cipher'):
                    c = sock.cipher()
                    tls_cipher = c[0] if c else None
            except Exception:
                pass
            stats = self.fs
            uploaded = int(getattr(stats, 'uploaded_bytes', 0) or 0)
            downloaded = int(getattr(stats, 'downloaded_bytes', 0) or 0)
            download_count = int(getattr(stats, 'download_count', 0) or 0)
            upload_count = int(getattr(stats, 'upload_count', 0) or 0)
            connected_at = getattr(stats, 'connected_at', None)
            uptime = time.time() - connected_at if connected_at else None
            ext_top = sorted(extensions.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            feature_text = ', '.join(features[:20]) if features else 'Not reported'
            self.done.emit({'protocol': 'FTPS' if getattr(self.fs, 'tls', False) else 'FTP', 'host': str(getattr(self.fs, 'host', '') or ''), 'port': int(getattr(self.fs, 'port', 21) or 21), 'user': str(getattr(self.fs, 'user', '') or ''), 'tls': bool(getattr(self.fs, 'tls', False)), 'passive': bool(getattr(self.fs, 'passive', True)), 'cwd': str(pwd), 'system': syst, 'welcome': welcome, 'files': files, 'folders': folders, 'total_bytes': total_bytes, 'items': len(entries), 'largest_name': largest_name, 'largest_size': largest_size, 'newest_name': newest_name, 'newest_age': self._format_age(time.time() - newest_ts) if newest_ts else 'Unavailable', 'extensions': ext_top, 'features': features[:20], 'feature_text': feature_text, 'latency_ms': latency_ms, 'tls_version': tls_version or '—', 'tls_cipher': tls_cipher or '—', 'encoding': getattr(ftp, 'encoding', None) or '—', 'timeout': getattr(self.fs, 'timeout', None), 'uptime': self._format_age(uptime), 'downloaded_bytes': downloaded, 'uploaded_bytes': uploaded, 'download_count': download_count, 'upload_count': upload_count, 'last_operation': getattr(stats, 'last_operation', None) or 'None', 'last_operation_age': self._format_age(time.time() - stats.last_operation_at) if getattr(stats, 'last_operation_at', None) else '—'})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass

def _is_light_background():
    """Return True when the active theme background is light.

  This deliberately derives readability from the current background rather
  than trusting a stale per-widget text colour. Theme switching changes the
  global palette/QSS, and this keeps Dashboard text black on light themes and
  white on dark themes even if a widget was created under the previous theme.
  """
    c = QColor(T.get('BG_DARK', '#000000'))
    return c.lightness() >= 160

def _dashboard_text(kind='primary'):
    """Return high-contrast Dashboard text for the active light/dark theme."""
    return {'primary': T['TEXT_PRIMARY'], 'dim': T['TEXT_DIM'],
            'muted': T['TEXT_MUTED']}.get(kind, T['TEXT_PRIMARY'])
_HOST_CMD = '\nUNAME_S=$(uname -s 2>/dev/null)\necho __HOST__\nhostname 2>&1\necho __OS__\nif [ "$UNAME_S" = "Darwin" ]; then\n echo "$(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"\nelse\n (grep -m1 PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d \'"\') || uname -s\nfi\necho __KERNEL__\nuname -r 2>&1\necho __UPTIME__\nif [ "$UNAME_S" = "Darwin" ]; then\n uptime 2>&1 | sed -E \'s/,[[:space:]]+[0-9]+ users?,.*$//\'\nelse\n (uptime -p 2>/dev/null || uptime) 2>&1\nfi\necho __LOAD__\nif [ "$UNAME_S" = "Darwin" ]; then\n sysctl -n vm.loadavg 2>/dev/null | tr -d \'{}\'\nelse\n cat /proc/loadavg 2>/dev/null\nfi\necho __CPU__\nif [ "$UNAME_S" = "Darwin" ]; then\n sysctl -n hw.ncpu 2>/dev/null\n CHIP=$(sysctl -n machdep.cpu.brand_string 2>/dev/null)\n if [ -z "$CHIP" ]; then\n  CHIP=$(system_profiler SPHardwareDataType 2>/dev/null | awk -F\': \' \'/Chip:/{print $2; exit} /Processor Name:/{print $2; exit}\')\n fi\n echo "$CHIP"\nelse\n nproc 2>/dev/null\n grep -m1 \'model name\' /proc/cpuinfo 2>/dev/null | cut -d: -f2\nfi\necho __MEM__\nif [ "$UNAME_S" = "Darwin" ]; then\n PGSZ=$(sysctl -n hw.pagesize 2>/dev/null)\n TOTB=$(sysctl -n hw.memsize 2>/dev/null)\n VMS=$(vm_stat 2>/dev/null)\n # Activity Monitor\'s "Memory Used" = App Memory + Wired + Compressed,\n # where App Memory = anonymous (internal) pages minus purgeable ones.\n # "Pages active" is NOT that: it includes file-backed cache, so using it\n # over-reports (a mostly-idle 8 GB Mac read ~77% here).\n ANON=$(echo "$VMS" | awk \'/^Anonymous pages/{gsub(/\\./,"",$3); print $3}\')\n PURG=$(echo "$VMS" | awk \'/^Pages purgeable/{gsub(/\\./,"",$3); print $3}\')\n ACT=$(echo "$VMS" | awk \'/^Pages active/{gsub(/\\./,"",$3); print $3}\')\n WIR=$(echo "$VMS" | awk \'/^Pages wired down/{gsub(/\\./,"",$4); print $4}\')\n CMP=$(echo "$VMS" | awk \'/^Pages occupied by compressor/{gsub(/\\./,"",$5); print $5}\')\n awk -v pg="$PGSZ" -v tot="$TOTB" -v anon="${ANON:-}" -v purg="${PURG:-0}" -v act="${ACT:-0}" -v wir="${WIR:-0}" -v cmp="${CMP:-0}" \\\n  \'BEGIN { totmb = tot/1024/1024; app = (anon != "") ? anon - purg : act; if (app < 0) app = 0; usedmb = (app+wir+cmp)*pg/1024/1024; if (usedmb>totmb) usedmb=totmb; printf "Mem: %d %d %d 0 0 %d\\n", totmb, usedmb, totmb-usedmb, totmb-usedmb }\'\nelse\n free -m 2>/dev/null | grep -i \'^mem\'\nfi\necho __DISK__\nif [ "$UNAME_S" = "Darwin" ]; then\n # On macOS `df -hP` silently drops -h (POSIX mode prints raw 512-byte\n # blocks), so ask for 1K blocks and humanize them here. APFS helper\n # volumes (VM, Preboot, Update, xarts, ...) live in the same container and\n # only add noise, so just the system volume, Data and real mounts show.\n df -kP 2>/dev/null | awk \'\n  function hum(kb,  v, i, U) { split("K M G T P", U, " "); v = kb + 0; i = 1\n   while (v >= 1024 && i < 5) { v /= 1024; i++ }\n   return (v >= 10 || i == 1) ? sprintf("%d%s", v + 0.5, U[i]) : sprintf("%.1f%s", v, U[i]) }\n  NR > 1 && $1 ~ /^\\/dev\\// {\n   m = $6; for (i = 7; i <= NF; i++) m = m " " $i\n   if (m ~ /^\\/System\\/Volumes\\// && m != "/System/Volumes/Data") next\n   printf "%s %s %s %s %s %s\\n", $1, hum($2), hum($3), hum($4), $5, m }\'\nelse\n df -hP -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null | tail -n +2\n echo __STORAGE__\n df -P -k -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null | awk \'\n  NR > 1 && $1 !~ /^$/ {\n    fs = $1\n    if (!(fs in seen)) {\n      seen[fs] = 1\n      total += $2\n      used += $3\n    }\n  }\n  END { if (total > 0) printf "%.0f %.0f\\n", total * 1024, used * 1024 }\'\nfi\nif [ "$UNAME_S" = "Darwin" ]; then\n echo __STORAGE__\n # Every APFS volume in a container reports the container\'s full size and\n # free space, so summing rows counts one disk many times over. Count each\n # distinct (size, avail) pair once; used = size - avail (what Finder shows).\n df -kP 2>/dev/null | awk \'\n  NR > 1 && $1 ~ /^\\/dev\\// {\n   m = $6; for (i = 7; i <= NF; i++) m = m " " $i\n   if (m ~ /^\\/System\\/Volumes\\// && m != "/System/Volumes/Data") next\n   k = $2 ":" $4\n   if (!(k in s)) { s[k] = 1; t += $2; u += ($2 - $4) } }\n  END { if (t > 0) printf "%.0f %.0f\\n", t * 1024, u * 1024 }\'\nfi\necho __CPUPCT__\nif [ "$UNAME_S" = "Darwin" ]; then\n # First `top` sample is not measured over an interval, so it is skewed;\n # take two samples one second apart and use the last "CPU usage" line.\n top -l 2 -n 0 -s 1 2>/dev/null | grep "CPU usage" | tail -1\nelse\n (vmstat 1 2 2>/dev/null | tail -1) || true\nfi\n'
_K8S_CMD = '\nif ! command -v kubectl >/dev/null 2>&1; then\n echo __NODES__\n echo "kubectl: not found"\n echo __NODEDETAILS__\n echo __TOP__\n echo __PODS__\n echo __PODTOP__\n echo __WORKLOADS__\n echo __SERVICES_ENDPOINTS__\n echo __EVENTS__\nelse\n echo __NODES__\n kubectl get nodes -o wide --no-headers 2>/dev/null\n\n echo __NODEDETAILS__\n # Conditions and node metadata/capacity in one API request.\n kubectl get nodes -o jsonpath=\'{range .items[*]}{.metadata.name}|{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="MemoryPressure")].status}|{.status.conditions[?(@.type=="DiskPressure")].status}|{.status.conditions[?(@.type=="PIDPressure")].status}|{.status.nodeInfo.kubeletVersion}|{.status.nodeInfo.osImage}|{.status.nodeInfo.kernelVersion}|{.status.nodeInfo.containerRuntimeVersion}|{.status.addresses[?(@.type=="InternalIP")].address}|{.status.capacity.cpu}|{.status.capacity.memory}|{.status.capacity.pods}|{.status.allocatable.cpu}|{.status.allocatable.memory}|{.status.allocatable.pods}{"\\n"}{end}\' 2>/dev/null\n\n echo __TOP__\n kubectl top nodes --no-headers 2>/dev/null\n\n echo __NAMESPACES__\n kubectl get namespaces -o jsonpath=\'{range .items[*]}{.metadata.name}{"\\\\n"}{end}\' 2>/dev/null\n\n echo __PODS__\n # jsonpath, pipe-delimited — see the note below this script for why this\n # replaced the old `-o wide` text table.\n kubectl get pods --all-namespaces -o jsonpath=\'{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.status.phase}|{.spec.nodeName}|{.status.podIP}|{.status.hostIP}|{.status.qosClass}|{.metadata.creationTimestamp}|{.status.reason}|{range .status.containerStatuses[*]}{.ready}{","}{end}|{range .status.containerStatuses[*]}{.restartCount}{","}{end}|{.status.message}{"\\n"}{end}\' 2>/dev/null\n echo __PODOWNERS__\n kubectl get pods --all-namespaces --no-headers -o custom-columns=\'NAMESPACE:.metadata.namespace,NAME:.metadata.name,OWNERKIND:.metadata.ownerReferences[0].kind,OWNERNAME:.metadata.ownerReferences[0].name\' 2>/dev/null\n\n echo __PODTOP__\n kubectl top pods --all-namespaces --no-headers 2>/dev/null\n\n echo __WORKLOADS__\n # Deployment/StatefulSet/DaemonSet data in one API request.\n kubectl get deployments,statefulsets,daemonsets --all-namespaces -o jsonpath=\'{range .items[*]}{.kind}|{.metadata.namespace}|{.metadata.name}|{.metadata.creationTimestamp}|{.status.replicas}|{.status.readyReplicas}|{.status.availableReplicas}|{.status.currentReplicas}|{.status.updatedReplicas}|{.status.unavailableReplicas}|{.spec.replicas}|{.status.desiredNumberScheduled}|{.status.numberReady}|{.status.numberAvailable}|{.status.updatedNumberScheduled}|{.status.numberUnavailable}{"\\n"}{end}\' 2>/dev/null\n\n echo __SERVICES_ENDPOINTS__\n # Service and Endpoints data in one API request.\n kubectl get services,endpoints --all-namespaces -o jsonpath=\'{range .items[*]}{.kind}|{.metadata.namespace}|{.metadata.name}|{.spec.type}|{.spec.clusterIP}|{.status.loadBalancer.ingress[0].ip}|{.status.loadBalancer.ingress[0].hostname}|{range .spec.ports[*]}{.name}:{.port}/{.protocol}:{.nodePort}{","}{end}|{.metadata.creationTimestamp}|{range .subsets[*].addresses[*]}1{";"}{end}|{range .subsets[*].notReadyAddresses[*]}1{";"}{end}{"\\n"}{end}\' 2>/dev/null\n\n echo __EVENTS__\n kubectl get events --all-namespaces --sort-by=.lastTimestamp -o jsonpath=\'{range .items[*]}{.lastTimestamp}|{.type}|{.reason}|{.involvedObject.kind}|{.involvedObject.namespace}|{.involvedObject.name}|{.message}{"\\n"}{end}\' 2>/dev/null\nfi\n'

class HistoryChart(QWidget):
    """Small, dependency-free line chart for dashboard history."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.points = []
        self.setMinimumHeight(150)
        self._hover_i = None
        self._plot_geom = None
        self.setMouseTracking(True)

    def set_points(self, points):
        self.points = [(str(t), float(v)) for t, v in points if v is not None]
        if self._hover_i is not None and self._hover_i >= len(self.points):
            self._hover_i = None
        self.update()

    def leaveEvent(self, event):
        self._hover_i = None
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        if self._plot_geom and len(self.points) > 1:
            left, top, w, h, lo, hi = self._plot_geom
            n = max(1, len(self.points) - 1)
            rel = (event.x() - left) / w
            i = round(rel * n)
            i = max(0, min(n, i))
            if i != self._hover_i:
                self._hover_i = i
                self.update()
        elif self._hover_i is not None:
            self._hover_i = None
            self.update()
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T['BG_ITEM']))
        p.setPen(QColor(_dashboard_text('muted')))
        p.drawText(10, 20, self.title)
        if not self.points:
            self._plot_geom = None
            p.drawText(10, 45, 'Collecting data…')
            return
        left, top, right, bottom = (38, 30, 10, 24)
        w = max(1, self.width() - left - right)
        h = max(1, self.height() - top - bottom)
        vals = [v for _, v in self.points]
        lo, hi = (min(vals), max(vals))
        if hi == lo:
            pad = max(1, abs(hi) * 0.05)
            lo, hi = (lo - pad, hi + pad)
        self._plot_geom = (left, top, w, h, lo, hi)
        p.setPen(QPen(QColor(T['BORDER']), 1))
        for i in range(5):
            y = top + int(h * i / 4)
            p.drawLine(left, y, left + w, y)
        p.setPen(QColor(_dashboard_text('muted')))
        p.drawText(3, top + 4, f'{hi:.0f}')
        p.drawText(3, top + h, f'{lo:.0f}')
        pen = QPen(QColor(T['ACCENT']), 2)
        p.setPen(pen)
        n = max(1, len(self.points) - 1)
        prev = None
        hover_xy = None
        for i, (_, value) in enumerate(self.points):
            x = left + int(w * i / n)
            y = top + int((hi - value) / (hi - lo) * h)
            if prev:
                p.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)
            if i == self._hover_i:
                hover_xy = (x, y)
        p.setPen(QColor(_dashboard_text('muted')))
        first, last = self.points[0][0], self.points[-1][0]
        fm = p.fontMetrics()
        if fm.horizontalAdvance(first) + fm.horizontalAdvance(last) + 16 > w:
            # Too narrow for two full "YYYY-MM-DD HH:MM" labels: drop the year.
            first, last = (t[5:] if len(t) > 10 else t for t in (first, last))
        p.drawText(left, self.height() - 6, first)
        p.drawText(max(left, left + w - fm.horizontalAdvance(last)), self.height() - 6, last)
        if hover_xy is not None:
            hx, hy = hover_xy
            h_time, h_val = self.points[self._hover_i]
            p.setPen(QPen(QColor(_dashboard_text('muted')), 1, Qt.DashLine))
            p.drawLine(hx, top, hx, top + h)
            p.setPen(QPen(QColor(T['ACCENT']), 2))
            p.setBrush(QColor(T['BG_ITEM']))
            p.drawEllipse(hx - 4, hy - 4, 8, 8)
            label = f'{h_time}  {h_val:.1f}'
            fm = p.fontMetrics()
            box_w = fm.horizontalAdvance(label) + 12
            box_h = fm.height() + 8
            bx = hx + 8
            if bx + box_w > self.width():
                bx = hx - 8 - box_w
            bx = max(2, bx)
            by = max(2, hy - box_h - 8)
            path = QPainterPath()
            path.addRoundedRect(bx, by, box_w, box_h, 4, 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T['BG_HOVER']))
            p.drawPath(path)
            p.setPen(QColor(_dashboard_text('primary')))
            p.drawText(bx + 6, by + box_h - 6, label)

class Sparkline(QWidget):
    """Tiny live-updating line chart for a ring's last ~60 seconds of
  samples, at the dashboard's own poll cadence (see REFRESH_MS) —
  distinct from HistoryChart above, which plots the separate 1-sample-
  per-minute, 500-sample-deep history log.

  Each push() eases the whole line from its previous shape into the new
  one over a short animation rather than hard-jumping, so a fresh
  sample reads as a live update instead of a redraw. Points age one
  slot to the left every push (a scrolling window), not by fixed pixel
  position, so the animation always represents "the line just shifted
  and gained a new point on the right."
  """
    MAX_POINTS = 20

    def __init__(self, color: str=None, parent=None):
        super().__init__(parent)
        self._color = color or T['ACCENT2']
        self._values = []
        self._prev_shape = []
        self._next_shape = []
        self._t = 1.0
        self.setFixedHeight(30)
        self.setMinimumWidth(60)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_step)

    def set_color(self, color: str):
        if color and color != self._color:
            self._color = color
            self.update()

    def clear(self):
        self._anim.stop()
        self._values = []
        self._prev_shape = []
        self._next_shape = []
        self._t = 1.0
        self.update()

    def push(self, value):
        """Add one sample (e.g. this tick's cpu_pct/mem_pct) and animate
    into the new shape. Skip calling this on a tick where the value is
    unavailable — that freezes the sparkline on its last known shape
    rather than plotting a misleading 0."""
        if value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        prev_norm = self._next_shape or self._normalized(self._values)
        self._values.append(value)
        self._values = self._values[-self.MAX_POINTS:]
        next_norm = self._normalized(self._values)
        n = len(next_norm)
        if len(prev_norm) < n:
            prev_norm = [None] * (n - len(prev_norm)) + list(prev_norm)
        elif len(prev_norm) > n:
            prev_norm = prev_norm[-n:]
        self._prev_shape = prev_norm
        self._next_shape = next_norm
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    @staticmethod
    def _normalized(values):
        if not values:
            return []
        lo, hi = (min(values), max(values))
        if hi == lo:
            lo, hi = (lo - 1.0, hi + 1.0)
        return [(v - lo) / (hi - lo) for v in values]

    def _on_step(self, t):
        self._t = t
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if len(self._next_shape) < 2:
            return
        w, h = (self.width(), self.height())
        pad_x, pad_y = (3, 4)
        n = len(self._next_shape)
        color = QColor(self._color)
        pts = []
        for i in range(n):
            nxt = self._next_shape[i]
            prv = self._prev_shape[i] if i < len(self._prev_shape) else None
            frac = nxt if prv is None else prv + (nxt - prv) * self._t
            x = pad_x + (w - 2 * pad_x) * (i / (n - 1))
            y = pad_y + (h - 2 * pad_y) * (1 - frac)
            pts.append((x, y))
        fill = QColor(color)
        fill.setAlpha(40)
        path = QPainterPath()
        path.moveTo(pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        path.lineTo(pts[-1][0], h - pad_y)
        path.lineTo(pts[0][0], h - pad_y)
        path.closeSubpath()
        p.fillPath(path, fill)
        p.setPen(QPen(color, 1.6))
        for i in range(len(pts) - 1):
            p.drawLine(int(pts[i][0]), int(pts[i][1]), int(pts[i + 1][0]), int(pts[i + 1][1]))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        lx, ly = pts[-1]
        p.drawEllipse(int(lx) - 2, int(ly) - 2, 4, 4)

def _split_sections(out: str) -> dict:
    """Split output that was built from `echo __MARKER__; ...` into a dict
  of marker -> list-of-lines."""
    sections, current = ({}, None)
    for line in (out or '').splitlines():
        s = line.strip()
        if s.startswith('__') and s.endswith('__') and (len(s) > 4) and (' ' not in s):
            current = s.strip('_')
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return sections
_DF_SIZE_RE = re.compile('^([\\d.]+)([KMGTP]?)i?$')
_DF_SIZE_MULT = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4, 'P': 1024 ** 5}

def _short_age(timestamp: str) -> str:
    """Convert a Kubernetes ISO-8601 timestamp to a compact age."""
    if not timestamp:
        return '—'
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(timestamp.strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if seconds < 60:
            return f'{seconds}s'
        minutes = seconds // 60
        if minutes < 60:
            return f'{minutes}m'
        hours = minutes // 60
        if hours < 24:
            return f'{hours}h'
        days = hours // 24
        if days < 30:
            return f'{days}d'
        months = days // 30
        if months < 12:
            return f'{months}mo'
        return f'{days // 365}y'
    except (ValueError, TypeError, OverflowError):
        return timestamp[:19].replace('T', ' ')

def _parse_df_size(s: str):
    m = _DF_SIZE_RE.match((s or '').strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * _DF_SIZE_MULT[m.group(2)]
    except (ValueError, KeyError):
        return None

def _pct_color(pct: float) -> str:
    if pct >= 85:
        return T['DANGER']
    if pct >= 65:
        return T['WARNING']
    return T['SUCCESS']

def _lerp_color(c1: str, c2: str, t: float) -> str:
    """Blend two hex colours at fraction t (0 = c1, 1 = c2) — drives the
  hover border-color animation frame by frame since QSS itself can't
  transition a colour."""
    a, b = (QColor(c1), QColor(c2))
    r = round(a.red() + (b.red() - a.red()) * t)
    g = round(a.green() + (b.green() - a.green()) * t)
    bl = round(a.blue() + (b.blue() - a.blue()) * t)
    return f'#{r:02x}{g:02x}{bl:02x}'

def _status_color(status: str) -> str:
    s = (status or '').lower()
    if 'running' in s or s == 'true':
        return T['SUCCESS']
    if 'pending' in s or 'init' in s:
        return T['WARNING']
    if any((x in s for x in ('error', 'crash', 'fail', 'evict', 'unknown'))):
        return T['DANGER']
    return _dashboard_text('dim')
_NODE_STATUS_WORDS = ('ready', 'notready', 'unknown', 'schedulingdisabled')

def _looks_like_node_row(line: str) -> bool:
    """True only for lines structurally matching a `kubectl get nodes -o
  wide --no-headers` row (NAME STATUS ROLES AGE VERSION ...)."""
    parts = line.split()
    if len(parts) < 5:
        return False
    return any((w in parts[1].lower() for w in _NODE_STATUS_WORDS))
_CPU_VAL_RE = re.compile('^\\d+m?$')
_MEM_VAL_RE = re.compile('^\\d+(Ki|Mi|Gi)?$')

def _k8s_mem_to_bytes(value: str):
    """Convert a Kubernetes memory quantity to bytes for compact display."""
    m = re.match('^(\\d+(?:\\.\\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$', (value or '').strip())
    if not m:
        return None
    multipliers = {None: 1, 'K': 1000, 'M': 1000 ** 2, 'G': 1000 ** 3, 'T': 1000 ** 4, 'Ki': 1024, 'Mi': 1024 ** 2, 'Gi': 1024 ** 3, 'Ti': 1024 ** 4}
    try:
        return float(m.group(1)) * multipliers[m.group(2)]
    except (ValueError, KeyError):
        return None

def _k8s_mem_fmt(value: str) -> str:
    """Format a Kubernetes memory quantity as a compact binary unit."""
    raw = _k8s_mem_to_bytes(value)
    if raw is None:
        return value or 'n/a'
    for unit, factor in (('Ti', 1024 ** 4), ('Gi', 1024 ** 3), ('Mi', 1024 ** 2), ('Ki', 1024)):
        if raw >= factor:
            amount = raw / factor
            return f'{amount:.1f} {unit}' if amount < 100 else f'{amount:.0f} {unit}'
    return f'{raw:.0f} B'

def _k8s_cpu_fmt(value: str) -> str:
    """Format a Kubernetes CPU quantity in cores."""
    value = (value or '').strip()
    if not value:
        return 'n/a'
    try:
        if value.endswith('m'):
            cores = float(value[:-1]) / 1000.0
        else:
            cores = float(value)
    except ValueError:
        return value
    return f'{cores:.2f} cores' if cores < 10 else f'{cores:.0f} cores'

class NodeDetailWindow(QWidget):
    """Standalone operational view of the pods scheduled on one node."""
    closed = pyqtSignal(str)

    def __init__(self, node_name: str, parent=None):
        super().__init__(parent, Qt.Window)
        self.node_name = node_name
        self.setWindowTitle(f'Node — {node_name}')
        self.resize(1120, 620)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        self.title_lbl = QLabel(f'⎈ {node_name}')
        root.addWidget(self.title_lbl)
        self.summary_lbl = QLabel('Waiting for data…')
        root.addWidget(self.summary_lbl)
        sort_row = QHBoxLayout()
        sort_row.setSpacing(8)
        sort_label = QLabel('Sort by')
        sort_label.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        sort_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(['Pod name', 'Namespace', 'Status', 'CPU', 'Memory', 'Restarts', 'Ready', 'Age', 'Owner'])
        self.sort_combo.setMinimumWidth(145)
        self.sort_combo.setToolTip('Choose the pod column to sort by')
        sort_row.addWidget(self.sort_combo)
        self.sort_order_btn = QPushButton('↑ Ascending')
        self.sort_order_btn.setToolTip('Switch between ascending and descending order')
        sort_row.addWidget(self.sort_order_btn)
        sort_row.addStretch(1)
        root.addLayout(sort_row)
        self.pod_tree = QTreeWidget()
        self.pod_tree.setHeaderLabels(['Pod', 'Namespace', 'Status', 'CPU', 'Memory', 'Restarts', 'Ready', 'IP', 'Age', 'Owner'])
        self.pod_tree.setRootIsDecorated(False)
        self.pod_tree.setUniformRowHeights(True)
        self.pod_tree.setFont(monospace_font(12))
        self.pod_tree.itemDoubleClicked.connect(self._show_pod_details)
        root.addWidget(self.pod_tree)
        self._pod_snapshot = {}
        self._pod_usage = {}
        self._sort_key = 'Pod name'
        self._sort_ascending = True
        self.sort_combo.currentTextChanged.connect(self._on_sort_changed)
        self.sort_order_btn.clicked.connect(self._toggle_sort_order)
        self._apply_styles()

    def _apply_styles(self):
        self.setStyleSheet(f"background: {T['BG_DARK']};")
        self.title_lbl.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 15px; font-weight: 700;")
        self.summary_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        self.pod_tree.setStyleSheet(f'QTreeWidget {{ font-size: 12px; }} QTreeWidget::item {{ height: 30px; }}')

    def refresh_theme(self):
        self._apply_styles()

    def update_pods(self, pods: list, pod_usage: dict, parse_error: str=None):
        self._pod_snapshot = {(p['namespace'], p['name']): p for p in pods}
        self._pod_usage = pod_usage or {}
        self._rebuild_pod_rows()
        if parse_error:
            self.summary_lbl.setText(f"⚠ {parse_error} · updated {time.strftime('%H:%M:%S')}")
            self.summary_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")
        else:
            self.summary_lbl.setText(f"{len(pods)} pod(s) scheduled here · double-click a pod for details · updated {time.strftime('%H:%M:%S')}")
            self.summary_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")

    def _on_sort_changed(self, value):
        self._sort_key = value
        self._rebuild_pod_rows()

    def _toggle_sort_order(self):
        self._sort_ascending = not self._sort_ascending
        self.sort_order_btn.setText('↑ Ascending' if self._sort_ascending else '↓ Descending')
        self._rebuild_pod_rows()

    def _sorted_pods(self, pods):

        def key_value(p):
            if self._sort_key == 'Pod name':
                return (str(p.get('name') or '').lower(), False)
            if self._sort_key == 'Namespace':
                return (str(p.get('namespace') or '').lower(), False)
            if self._sort_key == 'Status':
                status_order = {'running': 0, 'pending': 1, 'succeeded': 2, 'failed': 3, 'unknown': 4}
                status = str(p.get('phase') or 'Unknown').lower()
                return (status_order.get(status, 99), False)
            if self._sort_key == 'Owner':
                owner = str(p.get('owner') or '').lower()
                return (owner, not bool(owner))
            if self._sort_key == 'Restarts':
                return (int(p.get('restarts') or 0), False)
            if self._sort_key == 'Ready':
                raw = str(p.get('ready') or '')
                try:
                    got, want = raw.split('/', 1)
                    return ((int(got), int(want)), False)
                except (ValueError, TypeError):
                    return (None, True)
            if self._sort_key == 'Age':
                value = self._age_seconds(p)
                return (value, value is None)
            if self._sort_key in ('CPU', 'Memory'):
                usage = self._pod_usage.get((p.get('namespace'), p.get('name'))) or {}
                value = self._metric_value(usage.get('cpu' if self._sort_key == 'CPU' else 'mem'))
                return (value, value is None)
            return (str(p.get('name') or '').lower(), False)
        valid = []
        missing = []
        for pod in pods:
            value, is_missing = key_value(pod)
            (missing if is_missing else valid).append((value, pod))
        valid.sort(key=lambda item: item[0], reverse=not self._sort_ascending)
        return [pod for _, pod in valid] + [pod for _, pod in missing]

    @staticmethod
    def _metric_value(value):
        if not value or str(value).strip().lower() in ('n/a', '—', '-'):
            return None
        m = re.match('^([0-9]+(?:\\.[0-9]+)?)\\s*([a-zA-Z]+)?$', str(value).strip())
        if not m:
            return None
        number = float(m.group(1))
        unit = (m.group(2) or '').lower()
        multipliers = {'': 1.0, 'm': 0.001, 'u': 1e-06, 'n': 1e-09, 'ki': 1024.0, 'mi': 1024.0 ** 2, 'gi': 1024.0 ** 3, 'ti': 1024.0 ** 4}
        return number * multipliers.get(unit, 1.0)

    @staticmethod
    def _age_seconds(p):
        created = str(p.get('created') or '')
        if created:
            try:
                return max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(created.replace('Z', '+00:00'))).total_seconds())
            except (ValueError, TypeError):
                pass
        age = str(p.get('age') or '')
        m = re.match('^(\\d+)([smhdw])$', age.lower())
        if m:
            return int(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[m.group(2)]
        return None

    def _rebuild_pod_rows(self):
        pods = list(self._pod_snapshot.values())
        self.pod_tree.clear()
        for pod in self._sorted_pods(pods):
            usage = self._pod_usage.get((pod['namespace'], pod['name']))
            item = QTreeWidgetItem([pod['name'], pod['namespace'], pod['phase'], usage['cpu'] if usage else 'n/a', usage['mem'] if usage else 'n/a', str(pod['restarts']), pod['ready'], pod.get('pod_ip') or '—', pod.get('age') or _short_age(pod.get('created', '')), pod.get('owner') or '—'])
            item.setData(0, Qt.UserRole, (pod['namespace'], pod['name']))
            item.setForeground(2, QColor(_status_color(pod['phase'])))
            if pod['restarts'] > 0:
                item.setForeground(5, QColor(T['WARNING'] if pod['restarts'] < 5 else T['DANGER']))
            for col in (3, 4):
                item.setForeground(col, QColor(_dashboard_text('primary') if usage else _dashboard_text('muted')))
            tooltip = self._pod_tooltip(pod)
            for col in range(10):
                item.setToolTip(col, tooltip)
            self.pod_tree.addTopLevelItem(item)
        for col in range(10):
            self.pod_tree.resizeColumnToContents(col)

    def _pod_tooltip(self, pod):
        lines = [f"Pod: {pod['name']}", f"Namespace: {pod['namespace']}", f"Phase: {pod['phase']}", f"Ready: {pod['ready']}", f"Restarts: {pod['restarts']}"]
        for key, label in (('pod_ip', 'Pod IP'), ('host_ip', 'Host IP'), ('qos', 'QoS'), ('owner', 'Owner'), ('reason', 'Reason'), ('message', 'Message'), ('waiting', 'Container state')):
            if pod.get(key):
                lines.append(f'{label}: {pod[key]}')
        return '\n'.join(lines)

    def _show_pod_details(self, item, _column):
        key = item.data(0, Qt.UserRole)
        if not key:
            return
        pod = self._pod_snapshot.get(tuple(key))
        if not pod:
            return
        from PyQt5.QtWidgets import QMessageBox
        lines = [f"Pod: {pod['name']}", f"Namespace: {pod['namespace']}", f"Phase: {pod['phase']}", f"Ready: {pod['ready']}", f"Restarts: {pod['restarts']}", f'Node: {self.node_name}', f"Pod IP: {pod.get('pod_ip') or '—'}", f"Host IP: {pod.get('host_ip') or '—'}", f"QoS: {pod.get('qos') or '—'}", f"Age: {_short_age(pod.get('created', ''))}", f"Owner: {pod.get('owner') or '—'}"]
        if pod.get('reason'):
            lines.append(f"Reason: {pod['reason']}")
        if pod.get('message'):
            lines.append(f"Message: {pod['message']}")
        if pod.get('waiting'):
            lines.append(f"Container state: {pod['waiting']}")
        QMessageBox.information(self, f"Pod — {pod['name']}", '\n'.join(lines))

    def closeEvent(self, event):
        self.closed.emit(self.node_name)
        super().closeEvent(event)

class NodeCard(QFrame):
    """A single node's summary shown as a square card tile in the
  Kubernetes nodes grid (3 cards per row — see DashboardTab._add_node_
  card). Replaces the old one-row-per-node table; double-clicking a card
  opens the same NodeDetailWindow with the pods scheduled on it.

  The card's normal size is driven by set_side(), which the owning
  DashboardTab calls with a side length computed from the available grid
  width so the row of cards always fills the section edge-to-edge.
  Hovering just highlights the card's border (see the QFrame#node_card:hover
  rule in _apply_styles) — the card doesn't move or resize.

  Pass is_bucket=True for the synthetic "Unscheduled / other" tile that
  groups pods not attributable to any real node — it renders with a
  dashed muted border and an explanatory blurb instead of CPU/Memory
  rings and a Ready/pressure badge, so it can't be mistaken for an
  actual node reporting 0% usage or sitting in an unknown state.
  """
    doubleClicked = pyqtSignal(str)

    def __init__(self, node_name: str, parent=None, is_bucket: bool=False):
        super().__init__(parent)
        self.node_name = node_name
        self._base_side = 300
        self._is_bucket = is_bucket
        self.setObjectName('node_card')
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._base_side, self._base_side)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(14)
        icon = '' if is_bucket else '⎈'
        head = QHBoxLayout()
        head.setSpacing(6)
        self.name_lbl = QLabel(f'{icon} {node_name}')
        self.name_lbl.setWordWrap(True)
        head.addWidget(self.name_lbl, 1)
        self.status_lbl = QLabel('')
        head.addWidget(self.status_lbl, 0, Qt.AlignTop)
        outer.addLayout(head)
        self.roles_lbl = QLabel('')
        outer.addWidget(self.roles_lbl)
        self.meta_lbl = QLabel('')
        self.meta_lbl.setWordWrap(True)
        self.meta_lbl.setMinimumHeight(28)
        outer.addWidget(self.meta_lbl)
        outer.addStretch(1)
        self.cpu_ring = self.mem_ring = None
        self.cpu_spark = self.mem_spark = None
        self._ring_caps = []
        if is_bucket:
            self.roles_lbl.setText('Pods not tied to a specific node')
            self.meta_lbl.hide()
            self.status_lbl.hide()
            blurb = QLabel("Includes pending pods with no node\nassignment yet, and pods reported\nagainst a node outside the cluster's\ncurrent node list.")
            blurb.setAlignment(Qt.AlignCenter)
            blurb.setWordWrap(True)
            self._blurb_lbl = blurb
            blurb_row = QHBoxLayout()
            blurb_row.addStretch(1)
            blurb_row.addWidget(blurb)
            blurb_row.addStretch(1)
            outer.addLayout(blurb_row)
        else:
            rings_row = QHBoxLayout()
            rings_row.setSpacing(32)
            self.cpu_ring = CircularProgress(size=112, thickness=11, show_text=True, font_size=18)
            self.mem_ring = CircularProgress(size=112, thickness=11, show_text=True, font_size=18)
            self.cpu_spark = Sparkline()
            self.mem_spark = Sparkline()
            for cap_text, ring, spark in (('CPU', self.cpu_ring, self.cpu_spark), ('Memory', self.mem_ring, self.mem_spark)):
                col = QVBoxLayout()
                col.setSpacing(6)
                r_row = QHBoxLayout()
                r_row.addStretch(1)
                r_row.addWidget(ring)
                r_row.addStretch(1)
                col.addLayout(r_row)
                spark.setFixedWidth(96)
                spark_row = QHBoxLayout()
                spark_row.addStretch(1)
                spark_row.addWidget(spark)
                spark_row.addStretch(1)
                col.addLayout(spark_row)
                cap = QLabel(cap_text)
                cap.setAlignment(Qt.AlignHCenter)
                col.addWidget(cap)
                self._ring_caps.append(cap)
                rings_row.addLayout(col)
            outer.addLayout(rings_row)
        outer.addStretch(1)
        self._detail_strip = None
        if not is_bucket:
            self._detail_strip = QLabel('')
            self._detail_strip.setWordWrap(True)
            self._detail_strip.hide()
            outer.addWidget(self._detail_strip)
        footer = QHBoxLayout()
        self.pods_lbl = QLabel('')
        footer.addWidget(self.pods_lbl)
        footer.addStretch(1)
        self.pressure_lbl = QLabel('')
        self.pressure_lbl.setAlignment(Qt.AlignRight)
        if is_bucket:
            self.pressure_lbl.hide()
        footer.addWidget(self.pressure_lbl)
        outer.addLayout(footer)
        self.setAttribute(Qt.WA_Hover, True)
        self._cpu_detail = self._mem_detail = ''
        self._hover_t = 0.0
        self._rest_y = None
        self._animating_lift = False
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(10)
        self._shadow.setOffset(0, 2)
        self._shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(self._shadow)
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(170)
        self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_step)
        self._apply_styles()

    def set_side(self, side: int):
        """Set the card's square side length. Called by DashboardTab
    whenever the grid's available width changes, so the row of cards
    keeps filling the section."""
        self._base_side = side
        self.setFixedSize(side, side)

    def _apply_styles(self):
        if self._is_bucket:
            self._border_rest = _dashboard_text('muted')
            self._border_hover = _dashboard_text('dim')
            self._border_style = 'dashed'
            self.setStyleSheet(f"QFrame#node_card {{ background: {T['BG_PANEL']}; border: 1px dashed {self._border_rest}; border-radius: 14px; }}")
        else:
            self._border_rest = T['BORDER']
            self._border_hover = T['ACCENT']
            self._border_style = 'solid'
            self.setStyleSheet(f"QFrame#node_card {{ background: {T['BG_ITEM']}; border: 1px solid {self._border_rest}; border-radius: 14px; }}")
        self.name_lbl.setStyleSheet(f"color: {(_dashboard_text('dim') if self._is_bucket else _dashboard_text('primary'))}; font-size: 15px; font-weight: 700;")
        self.roles_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        self.meta_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 10px;")
        self.pods_lbl.setStyleSheet(f"color: {_dashboard_text('dim')}; font-size: 12px;")
        for cap in self._ring_caps:
            cap.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 11px; font-weight: 600;")
        if self._is_bucket:
            self._blurb_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        if self._detail_strip is not None:
            self._detail_strip.setStyleSheet(f"background: {T['BG_HOVER']}; color: {_dashboard_text('dim')}; font-size: 11px; border-radius: 8px; padding: 6px 8px;")

    def refresh_theme(self):
        self._apply_styles()
        self._set_border_color(_lerp_color(self._border_rest, self._border_hover, self._hover_t))
        if self.cpu_ring is not None:
            self.cpu_ring.refresh_theme()
        if self.mem_ring is not None:
            self.mem_ring.refresh_theme()
        if self.cpu_spark is not None:
            self.cpu_spark.update()
        if self.mem_spark is not None:
            self.mem_spark.update()

    def update_data(self, status: str, roles: str, cpu_pct, mem_pct, pod_count: int, pressure_text: str, pressure_ok: bool, cpu_tip: str=None, mem_tip: str=None, node_info: dict=None):
        """Update a real-node card. Not used for bucket cards — those only
    ever show a pod count, set directly via update_pod_count()."""
        self.status_lbl.setText(status or '—')
        status_color = T['SUCCESS'] if (status or '').lower() == 'ready' else T['DANGER']
        self.status_lbl.setStyleSheet(f'color: {status_color}; font-size: 12px; font-weight: 700;')
        self.roles_lbl.setText(roles or '—')
        info = node_info or {}
        version = info.get('kubelet') or 'Kubernetes version unavailable'
        internal_ip = info.get('ip') or 'IP unavailable'
        cpu_cap = _k8s_cpu_fmt(info.get('cpu_capacity', ''))
        cpu_alloc = _k8s_cpu_fmt(info.get('cpu_allocatable', ''))
        mem_cap = _k8s_mem_fmt(info.get('mem_capacity', ''))
        mem_alloc = _k8s_mem_fmt(info.get('mem_allocatable', ''))
        self.meta_lbl.setText(f'{version} · {internal_ip}\nCPU {cpu_cap} cap / {cpu_alloc} alloc · RAM {mem_cap} cap / {mem_alloc} alloc')
        self.meta_lbl.show()
        self.setToolTip(f"{self.node_name}\nKubernetes: {version}\nOS: {info.get('os', 'n/a')}\nKernel: {info.get('kernel', 'n/a')}\nRuntime: {info.get('runtime', 'n/a')}\nInternal IP: {internal_ip}\nCPU capacity: {cpu_cap}\nCPU allocatable: {cpu_alloc}\nMemory capacity: {mem_cap}\nMemory allocatable: {mem_alloc}")
        if cpu_pct is not None:
            self.cpu_ring.setValue(cpu_pct, _pct_color(cpu_pct))
            self.cpu_spark.set_color(_pct_color(cpu_pct))
            self.cpu_spark.push(cpu_pct)
            self._cpu_detail = (cpu_tip or f'CPU usage: {cpu_pct:.1f}%').replace('\n', ' ')
        else:
            self.cpu_ring.setValue(0)
            self._cpu_detail = 'CPU usage unavailable'
        if mem_pct is not None:
            self.mem_ring.setValue(mem_pct, _pct_color(mem_pct))
            self.mem_spark.set_color(_pct_color(mem_pct))
            self.mem_spark.push(mem_pct)
            self._mem_detail = (mem_tip or f'Memory usage: {mem_pct:.1f}%').replace('\n', ' ')
        else:
            self.mem_ring.setValue(0)
            self._mem_detail = 'Memory usage unavailable'
        self.pods_lbl.setText(f' {pod_count} pod(s)')
        self.pressure_lbl.setText(pressure_text or '')
        if (pressure_text or '').strip().lower() == 'unknown':
            pressure_color = T['WARNING']
        else:
            pressure_color = T['SUCCESS'] if pressure_ok else T['DANGER']
        self.pressure_lbl.setStyleSheet(f'color: {pressure_color}; font-size: 12px; font-weight: 600;')

    def update_pod_count(self, pod_count: int):
        """Update a bucket card — the only thing it ever shows is how many
    pods currently fall in it."""
        self.pods_lbl.setText(f' {pod_count} pod(s)')

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit(self.node_name)
        super().mouseDoubleClickEvent(event)
    LIFT_PX = 7

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self._animating_lift:
            self._rest_y = self.y()

    def enterEvent(self, event):
        if self._detail_strip is not None:
            text = '  ·  '.join((t for t in (self._cpu_detail, self._mem_detail) if t))
            if text:
                self._detail_strip.setText(text)
                self._detail_strip.show()
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._detail_strip is not None:
            self._detail_strip.hide()
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target: float):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def _set_border_color(self, color: str):
        self.setStyleSheet(f"QFrame#node_card {{ background: {(T['BG_PANEL'] if self._is_bucket else T['BG_ITEM'])}; border: 1px {self._border_style} {color}; border-radius: 14px; }}")

    def _on_hover_step(self, value):
        self._hover_t = value
        if self._rest_y is None:
            self._rest_y = self.y()
        self._animating_lift = True
        self.move(self.x(), self._rest_y - round(self.LIFT_PX * value))
        self._animating_lift = False
        self._shadow.setBlurRadius(10 + 26 * value)
        self._shadow.setOffset(0, 2 + 10 * value)
        self._shadow.setColor(QColor(0, 0, 0, round(110 + 90 * value)))
        self._set_border_color(_lerp_color(self._border_rest, self._border_hover, value))

class _NodeGridContainer(QWidget):
    """Plain container for the node-card QGridLayout that emits resized()
  on every size change, so DashboardTab can rescale cards to keep
  filling the available width (see DashboardTab._rescale_node_cards)."""
    resized = pyqtSignal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

# DashboardTab implementation is kept as the public coordinator/facade.
# Methods are grouped into mixins to keep this file focused on lifecycle and
# public object identity while preserving the existing DashboardTab API.
from dashboard_tab_parts import (
  DashboardConnectionMixin, DashboardUIMixin, DashboardRefreshMixin,
  DashboardNodesHistoryMixin,
)

class DashboardTab(DashboardConnectionMixin, DashboardUIMixin, DashboardRefreshMixin, DashboardNodesHistoryMixin, QWidget):
    status_msg = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ssh = None
        self.ftp = None
        self._connection_protocol = None
        self._connection_host = None
        self._connection_port = None
        self._connection_user = None
        self._ftp_busy = False
        self._process_busy = False
        self._process_generation = 0
        self._ftp_worker = None
        self._ftp_snapshot = None
        self._ftp_current_path = '/'
        self._kube_context = ''
        self._active = False
        self._busy = False
        self._host_done = False
        self._k8s_done = False
        self._podmap_done = False
        self._refresh_started_at = 0.0
        self._host_snapshot = None
        self._k8s_snapshot = None
        self._podmap_snapshot = None
        self._snapshot_generation = 0
        self._workers = []
        self._pods_by_node_cache = {}
        self._last_good_pods_by_node = {}
        self._pod_usage_cache = {}
        self._pods_parse_error = None
        self._node_windows = {}
        self._node_cards = {}
        self._card_grid_pos = {}
        self._node_order = []
        self._pressure_names = {"mem": "MemoryPressure", "disk": "DiskPressure", "pid": "PIDPressure"}
        self._history_file = os.path.join(os.path.expanduser('~'), '.ec2_manager_dashboard_history.json')
        self._history_key = None
        self._history = []
        self._last_history_time = 0
        self._history_cpu_limit = 85
        self._history_mem_limit = 85
        self._history_restart_limit = 5
        self._history_pending_limit = 1
        self._ftp_fields = {}
        self._ftp_snapshot = {}
        self._ftp_current_path = '/'
        self.ftp = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        # Single unfocused-cadence timer. Mutually exclusive with self._timer
        # (see set_active) and restarted, never duplicated, on tab switches.
        self._bg_timer = QTimer(self)
        self._bg_timer.setInterval(BACKGROUND_REFRESH_MS)
        self._bg_timer.timeout.connect(self._background_refresh)
        self._bg_cache = None          # latest unfocused collection, not yet rendered
        self._cycle_background = False  # True while the in-flight cycle is a background one
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(1000)
        self._process_timer.timeout.connect(self._refresh_processes)
        self._build_ui()
        self._show_disconnected()
    NODE_GRID_COLS = 3
    NODE_CARD_MIN_SIDE = 320
    NODE_CARD_MAX_SIDE = 420