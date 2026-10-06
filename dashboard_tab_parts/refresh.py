import logging
from dashboard_tab import *
from dashboard_tab import (
  _CPU_VAL_RE, _HOST_CMD, _K8S_CMD, _MEM_VAL_RE, _PODMAP_CMD,
  _looks_like_node_row, _parse_df_size, _pct_color,
  _short_age, _split_sections,
)

from dashboard_tab_parts import windows_metrics as _winm

_log = logging.getLogger("kubedock.dashboard")

"""DashboardTab implementation mixin.

The public DashboardTab class remains in dashboard_tab.py; this module only
contains logically grouped DashboardTab methods.
"""

_PROCESS_CMD = r"""
UNAME_S=$(uname -s 2>/dev/null)
echo __TOP_PROCESS__
if [ "$UNAME_S" = "Darwin" ]; then
  top -l 1 -n 12 -o cpu 2>/dev/null
else
  COLUMNS=180 top -b -n 1 -o %CPU 2>/dev/null | head -n 22
fi
"""

class DashboardRefreshMixin:
  def _contextual_command(self, command: str) -> str:
      ctx = (self._kube_context or "").strip()
      if not ctx:
        return command
      flag = f"--context={shlex.quote(ctx)}"
      return re.sub(
        r"(^\s*)kubectl(?=\s)",
        lambda m: m.group(1) + "kubectl " + flag,
        command,
        flags=re.MULTILINE,
      )
  
  
  def _contextual_k8s_command(self) -> str:
      """Bind every kubectl invocation in the dashboard snapshot to the
      selected context. The `=value` form is deliberate: context names are
      allowed to begin with '-' and must never be parsed as a new flag."""
      ctx = (self._kube_context or "").strip()
      if not ctx:
        return _K8S_CMD
      flag = f"--context={shlex.quote(ctx)}"
      return re.sub(r"(^\s*)kubectl(?=\s)", lambda m: m.group(1) + "kubectl " + flag, _K8S_CMD, flags=re.MULTILINE)
  
  
  def _refresh_processes(self):
      """Collect one finite top snapshot without blocking the Qt thread."""
      if not self.ssh or not self._active or self._process_busy:
        return
      if self._remote_os is None:
        return  # OS not detected yet; `top` must not run on a Windows host
      if self._remote_os == "windows":
        # `top` is POSIX-only; no Windows equivalent is collected here.
        self.process_status.setText("Live process monitor is not available for Windows hosts")
        self.process_status.setStyleSheet(f"color: {T['WARNING']}; font-size: 11px;")
        return
      self._process_busy = True
      generation = self._process_generation
      worker = CommandWorker(self.ssh, _PROCESS_CMD, timeout=8)
      worker.done.connect(
        lambda out, g=generation: self._on_process_snapshot(out)
        if g == self._process_generation else None
      )
      worker.error.connect(
        lambda err, g=generation: self._on_process_error(err)
        if g == self._process_generation else None
      )
      worker.finished.connect(self._process_worker_done)
      track_worker(self._workers, worker)
      worker.start()

  @staticmethod
  def _parse_process_snapshot(out: str):
      """Parse Linux/macOS `top` output into stable dashboard fields."""
      lines = _split_sections(out).get("TOP_PROCESS", [])
      text = "\n".join(lines).strip()
      if not text:
        return None

      result = {
        "uptime": "—", "load": "—", "tasks": "—",
        "cpu": "—", "cpu_detail": "—", "memory": "—",
        "memory_detail": "—", "processes": [],
      }

      first = lines[0] if lines else ""
      m = re.search(r"up\s+(.+?)(?:,\s*\d+\s+users?|,\s*load average:|$)", first, re.I)
      if m:
        result["uptime"] = m.group(1).strip()
      m = re.search(r"load average[s]?:\s*([0-9., ]+)", first, re.I)
      if m:
        result["load"] = " ".join(m.group(1).split())

      # Linux: Tasks: 193 total, 1 running, 137 sleeping...
      m = re.search(r"Tasks?:\s*(\d+)\s+total,\s*(\d+)\s+running,\s*(\d+)\s+sleeping", text, re.I)
      if m:
        result["tasks"] = f"{m.group(1)} total · {m.group(2)} running"
      else:
        # macOS: Processes: 193 total, 2 running, 191 sleeping, ...
        m = re.search(r"Processes?:\s*(\d+)\s+total,\s*(\d+)\s+running,\s*(\d+)\s+sleeping", text, re.I)
        if m:
          result["tasks"] = f"{m.group(1)} total · {m.group(2)} running"

      m = re.search(
        r"%?Cpu\(s\):\s*([0-9.]+)\s*us,\s*([0-9.]+)\s*sy,.*?([0-9.]+)\s*id,\s*([0-9.]+)\s*wa",
        text, re.I
      )
      if m:
        user, system, idle, wait = m.groups()
        result["cpu"] = f"{100 - float(idle):.1f}% used"
        result["cpu_detail"] = f"User {user}% · System {system}% · Idle {idle}% · I/O {wait}%"
      else:
        m = re.search(r"CPU usage:\s*([0-9.]+)% user,\s*([0-9.]+)% sys,\s*([0-9.]+)% idle", text, re.I)
        if m:
          user, system, idle = m.groups()
          result["cpu"] = f"{100 - float(idle):.1f}% used"
          result["cpu_detail"] = f"User {user}% · System {system}% · Idle {idle}%"

      # Linux free output. Values are normally KiB; format them consistently.
      m = re.search(r"KiB Mem\s*:\s*([\d]+)\s+([\d]+)\s+([\d]+)\s+([\d]+)", text, re.I)
      if m:
        total, free, used, cache = map(int, m.groups())
        fmt = lambda k: size_fmt(k * 1024)
        result["memory"] = f"{fmt(used)} / {fmt(total)}"
        result["memory_detail"] = f"Used {fmt(used)} · Free {fmt(free)} · Cache {fmt(cache)}"
      else:
        m = re.search(r"PhysMem:\s*([0-9.]+[KMG]?) used,\s*([0-9.]+[KMG]?) unused", text, re.I)
        if m:
          result["memory"] = f"{m.group(1)} used"
          result["memory_detail"] = f"Used {m.group(1)} · Free {m.group(2)}"

      # Linux process rows. Keep the command as the final field so spaces in
      # command lines do not break the table.
      for line in lines:
        line = line.strip()
        if not re.match(r"^\d+\s+", line):
          continue
        parts = line.split(None, 11)
        if len(parts) < 12:
          continue
        try:
          pid = int(parts[0])
          cpu = float(parts[8].replace(',', '.'))
          mem = float(parts[9].replace(',', '.'))
        except (ValueError, IndexError):
          continue
        result["processes"].append({
          "pid": str(pid), "user": parts[1], "cpu": cpu, "mem": mem,
          "state": parts[7], "time": parts[10], "command": parts[11],
        })

      # macOS process rows: PID COMMAND %CPU TIME ... MEM ... STATE ...
      if not result["processes"]:
        for line in lines:
          m = re.match(r"^\s*(\d+)\s+(.+?)\s+([0-9.]+)\s+([^\s]+)\s+.*?\s+([0-9.]+[KMG]?)\s+.*?\s+([A-Z<]+)\s*$", line)
          if not m:
            continue
          pid, command, cpu, proc_time, mem, state = m.groups()
          result["processes"].append({
            "pid": pid, "user": "—", "cpu": float(cpu), "mem": 0.0,
            "state": state, "time": proc_time, "command": command,
          })

      result["processes"] = sorted(result["processes"], key=lambda p: p["cpu"], reverse=True)[:12]
      return result

  def _on_process_snapshot(self, out: str):
      snapshot = self._parse_process_snapshot(out)
      if not snapshot:
        self.process_status.setText("Live process monitor returned no data")
        self.process_status.setStyleSheet(f"color: {T['WARNING']}; font-size: 11px;")
        return

      self.process_metrics["uptime"].setText(snapshot["uptime"])
      self.process_metrics["load"].setText(snapshot["load"])
      self.process_metrics["tasks"].setText(snapshot["tasks"])
      self.process_metrics["cpu"].setText(snapshot["cpu"])
      self.process_metrics["memory"].setText(snapshot["memory"])
      self.process_detail_lbl.setText(
        f"{snapshot['cpu_detail']}   ·   {snapshot['memory_detail']}   ·   Load {snapshot['load']}"
      )

      tree = self.process_tree
      tree.setSortingEnabled(False)
      tree.clear()
      for proc in snapshot["processes"]:
        item = QTreeWidgetItem([
          proc["pid"], proc["user"], f"{proc['cpu']:.1f}", f"{proc['mem']:.1f}",
          proc["state"], proc["time"], proc["command"],
        ])
        item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
        item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
        item.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
        tree.addTopLevelItem(item)
      tree.setSortingEnabled(True)
      tree.sortItems(2, Qt.DescendingOrder)

      count = len(snapshot["processes"])
      self.process_status.setText(
        f"Live · {count} processes shown · updated {time.strftime('%H:%M:%S')}"
      )
      self.process_status.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 11px;")

  def _on_process_error(self, err: str):
      self.process_status.setText(f"Process monitor unavailable: {err}")
      self.process_status.setStyleSheet(f"color: {T['WARNING']}; font-size: 11px;")

  def _process_worker_done(self):
      self._process_busy = False

  def _refresh(self, background=False):
      """Start one dashboard collection cycle.
  
      Host and Kubernetes collection run in parallel, but a new cycle is
      blocked until BOTH workers have finished. This prevents a slow
      kubectl snapshot from overlapping the next timer tick.

      background=True is the unfocused cadence: the identical collection
      runs, but the result is cached instead of rendered (see
      _worker_finished) and the same _busy guard prevents overlap.
      """
      if self.ftp and not self.ssh:
        if not background:
          self._refresh_ftp()
        return
      if not self.ssh or self._busy or (not self._active and not background):
        return
  
      self._busy = True
      self._cycle_background = background
      self._host_done = False
      self._k8s_done = False
      self._podmap_done = False
      self._host_snapshot = None
      self._k8s_snapshot = None
      self._podmap_snapshot = None
      self._snapshot_generation += 1
      generation = self._snapshot_generation
      self._refresh_started_at = time.monotonic()
      self._update_live_label()

      if self._remote_os == "windows":
        self._start_windows_collection(generation)
      elif self._remote_os is None:
        self._probe_remote_os(generation)
      else:
        self._start_posix_collection(generation)


  def _probe_remote_os(self, generation: int):
      """Detect Windows once per SSH connection (result cached in
      self._remote_os, reset by set_ssh) and then continue the cycle."""
      worker = CommandWorker(self.ssh, _winm.WINDOWS_PROBE_CMD, timeout=10, raw=True)

      def _done(out, _err, _code, g=generation):
        if g != self._snapshot_generation:
          return  # connection/cycle changed while probing
        self._remote_os = "windows" if _winm.is_windows_probe_output(out) else "posix"
        self._refresh_started_at = time.monotonic()
        (self._start_windows_collection if self._remote_os == "windows"
         else self._start_posix_collection)(g)

      def _error(err, g=generation):
        if g != self._snapshot_generation:
          return
        # Not cached: retry next cycle. Fall back to the existing POSIX path
        # for this one so Linux/macOS behaviour is unchanged.
        _log.warning("remote OS probe failed (%s); using POSIX collection this cycle", err)
        self._start_posix_collection(g)

      worker.result.connect(_done)
      worker.error.connect(_error)
      track_worker(self._workers, worker)
      worker.start()


  def _start_windows_collection(self, generation: int):
      worker = CommandWorker(self.ssh, _winm.build_windows_host_command(), timeout=25, raw=True)
      worker.done.connect(lambda out, g=generation: self._on_windows_host_stats(out) if g == self._snapshot_generation else None)
      worker.error.connect(lambda err, g=generation: self._on_host_error(err) if g == self._snapshot_generation else None)
      track_worker(self._workers, worker)
      worker.start()

      # The Kubernetes / pod-placement collectors are POSIX shell scripts.
      # Report that honestly instead of running them against cmd.exe.
      self._k8s_snapshot = None
      self._k8s_error = "not collected for Windows SSH hosts"
      self._worker_finished("k8s")
      self._podmap_snapshot = None
      self._podmap_error = "not collected for Windows SSH hosts"
      self._worker_finished("podmap")


  def _on_windows_host_stats(self, raw: str):
      try:
        normalized = _winm.normalize_host_output(raw)
      except Exception as e:
        _log.exception("windows host metric normalization failed")
        self._on_host_error(f"Windows metrics parse error: {e}")
        return
      if not _split_sections(normalized):
        self._on_host_error("Windows metrics unavailable (no metric could be collected; is PowerShell available over SSH?)")
        return
      self._on_host_stats(normalized)


  def _start_posix_collection(self, generation: int):
      host_worker = CommandWorker(self.ssh, _HOST_CMD)
      host_worker.done.connect(lambda out, g=generation: self._on_host_stats(out) if g == self._snapshot_generation else None)
      host_worker.error.connect(lambda err, g=generation: self._on_host_error(err) if g == self._snapshot_generation else None)
      track_worker(self._workers, host_worker)
      host_worker.start()
  
      k8s_worker = CommandWorker(self.ssh, self._contextual_k8s_command())
      k8s_worker.done.connect(lambda out, g=generation: self._on_k8s_stats(out) if g == self._snapshot_generation else None)
      k8s_worker.error.connect(lambda err, g=generation: self._on_k8s_error(err) if g == self._snapshot_generation else None)
      track_worker(self._workers, k8s_worker)
      k8s_worker.start()
  
      # Run pod placement independently. The main Kubernetes snapshot can take
      # several seconds because it also collects workloads, services, events,
      # metrics, and detailed pod state. Node pod counts must not wait on that.
      podmap_worker = CommandWorker(
        self.ssh,
        self._contextual_command(_PODMAP_CMD),
        timeout=15,
      )
      podmap_worker.done.connect(
        lambda out, g=generation: self._on_podmap_stats(out)
        if g == self._snapshot_generation else None
      )
      podmap_worker.error.connect(
        lambda err, g=generation: self._on_podmap_error(err)
        if g == self._snapshot_generation else None
      )
      track_worker(self._workers, podmap_worker)
      podmap_worker.start()
  
  
  def _worker_finished(self, worker_name: str):
      """Finish one side of the current cycle and atomically publish both."""
      if worker_name == "host":
        self._host_done = True
      elif worker_name == "k8s":
        self._k8s_done = True
      elif worker_name == "podmap":
        self._podmap_done = True
  
      if not (self._host_done and self._k8s_done and self._podmap_done):
        return
  
      # Both outputs now belong to exactly this refresh generation.
      elapsed = (
        time.monotonic() - self._refresh_started_at
        if self._refresh_started_at else 0.0
      )
      host_out = self._host_snapshot
      k8s_out = self._k8s_snapshot
      k8s_error = getattr(self, "_k8s_error", None)
      self._k8s_error = None
      if k8s_out is not None and self._podmap_snapshot is not None:
        k8s_out += "\n" + self._podmap_snapshot

      if self._cycle_background:
        # Unfocused: keep the newest result for when the tab is re-opened;
        # don't touch (or rebuild) any Dashboard widgets.
        self._bg_cache = {
          "host": host_out, "k8s": k8s_out, "k8s_error": k8s_error,
          "elapsed": elapsed, "at": time.monotonic(),
          "kube_context": self._kube_context,
        }
        self._busy = False
        return

      self._bg_cache = None
      self._publish_snapshot(host_out, k8s_out, k8s_error, elapsed)
      self._busy = False
      self._update_live_label()
      self.status_msg.emit("Dashboard updated")
  
  
  def _publish_snapshot(self, host_out, k8s_out, k8s_error, elapsed):
      """Render one complete collected cycle as a single transaction. No
      visible dashboard section is changed until the whole cycle exists."""
      if host_out is not None:
        self._render_host_stats(host_out)
      else:
        self._on_host_render_error()
  
      if k8s_out is not None:
        self._render_k8s_stats(k8s_out)
      else:
        self._render_k8s_error(k8s_error or "snapshot collection failed")
  
      self.updated_lbl.setText(
        f"Updated {time.strftime('%H:%M:%S')} · snapshot {elapsed:.1f}s"
      )
  
  
  def _on_host_render_error(self):
      # Keep the previous visible host values on a failed cycle. The cycle
      # is still considered complete, so the next live refresh can proceed.
      pass
  
  
  def _render_k8s_error(self, err: str):
      self._clear_node_grid()
      self.k8s_note.setText(f"Kubernetes data unavailable: {err}")
      self.k8s_note.show()
  
  
  def _on_host_stats(self, out: str):
      # Collection callback: do not touch visible widgets yet. The host
      # snapshot is committed only when the matching K8s snapshot arrives.
      self._host_snapshot = out
      self._worker_finished("host")
  
  
  def _render_host_stats(self, out: str):
      sec = _split_sections(out)
  
      def first(key, default=""):
        lines = sec.get(key, [])
        return lines[0].strip() if lines else default
  
      self._vm_fields["hostname"].setText(first("HOST", "—"))
      self._vm_fields["os"].setText(first("OS", "Unknown"))
      self._vm_fields["kernel"].setText(first("KERNEL", "—"))
      self._vm_fields["uptime"].setText(first("UPTIME", "—"))
      # /proc/loadavg contains: 1m 5m 15m runnable/total last-pid.
      # Show only the actual load averages in the dashboard; the trailing
      # scheduler/PID fields are not load-average values.
      load_raw = first("LOAD", "")
      load_parts = load_raw.split()
      if len(load_parts) >= 3:
        self._vm_fields["load"].setText(
          f"{load_parts[0]} {load_parts[1]} {load_parts[2]}"
        )
      else:
        self._vm_fields["load"].setText(load_raw or "—")
  
      cpu_lines = [l for l in sec.get("CPU", []) if l.strip()]
      cores = cpu_lines[0].strip() if cpu_lines else "?"
      model = cpu_lines[1].strip() if len(cpu_lines) > 1 else "Unknown CPU"
      self._vm_fields["cpu_model"].setText(f"{model} ({cores} cores)")
  
      # CPU % busy. Linux: from `vmstat 1 2`'s last line — its final two
      # columns (wa, st) plus 'id' (idle) are what's left after us+sy;
      # 100-idle is simplest and matches what most monitoring tools call
      # "CPU used". macOS: `top -l 1 -n 0` prints a single summary line
      # like "CPU usage: 5.26% user, 10.52% sys, 84.21% idle" instead.
      cpupct_lines = [l for l in sec.get("CPUPCT", []) if l.strip()]
      cpu_pct = None
      if cpupct_lines:
        last = cpupct_lines[-1]
        if "CPU usage" in last:
          m = re.search(r"([\d.]+)%\s*idle", last)
          if m:
            try:
              cpu_pct = max(0.0, 100.0 - float(m.group(1)))
            except ValueError:
              pass
        else:
          parts = last.split()
          if len(parts) >= 15:
            try:
              cpu_pct = max(0.0, 100.0 - float(parts[14]))
            except ValueError:
              pass
      if cpu_pct is not None:
        self._style_ring(self.cpu_ring["ring"], cpu_pct)
        self.cpu_ring["spark"].set_color(_pct_color(cpu_pct))
        self.cpu_ring["spark"].push(cpu_pct)
        self.cpu_ring["val_lbl"].setText(f"{cpu_pct:.0f}% busy")
        self.cpu_ring["ring"].setToolTip(
          f"CPU usage: {cpu_pct:.1f}% busy\n"
          f"Cores: {cores}\n"
          f"Model: {model}"
        )
      else:
        # Reset the ring too, not just the label — otherwise a ring that
        # was populated by an earlier successful refresh keeps showing
        # that stale value forever once this stat becomes unavailable.
        self.cpu_ring["ring"].setValue(0)
        self.cpu_ring["val_lbl"].setText("unavailable")
        self.cpu_ring["ring"].setToolTip("CPU usage unavailable")
  
      # Memory — Linux `free -m` is:
      #  Mem: total used free shared buff/cache available
      # The previous parser accidentally treated `used` as `free`, which
      # inflated the displayed usage (e.g. ~96% instead of ~58%). Parse
      # the actual used column directly.
      mem_line = first("MEM", "")
      if mem_line:
        parts = mem_line.split()
        try:
          total_mb = float(parts[1])
          used_mb = float(parts[2])
          free_mb = float(parts[3])
          mem_pct = (used_mb / total_mb * 100.0) if total_mb else 0.0
          self._style_ring(self.mem_ring["ring"], mem_pct)
          self.mem_ring["spark"].set_color(_pct_color(mem_pct))
          self.mem_ring["spark"].push(mem_pct)
          self.mem_ring["val_lbl"].setText(
            f"{used_mb/1024:.1f} / {total_mb/1024:.1f} GB"
          )
          tip_lines = [
            f"Memory usage: {mem_pct:.1f}%",
            f"Total: {total_mb/1024:.2f} GB",
            f"Used: {used_mb/1024:.2f} GB",
            f"Free: {free_mb/1024:.2f} GB",
          ]
          # shared/buff-cache/available are only meaningful on the
          # Linux `free -m` branch — the macOS branch always fills
          # those columns with 0, so skip them there.
          if len(parts) >= 7:
            shared_mb, buffcache_mb, avail_mb = (
              float(parts[4]), float(parts[5]), float(parts[6])
            )
            if shared_mb or buffcache_mb:
              tip_lines.append(f"Buff/cache: {buffcache_mb/1024:.2f} GB")
              tip_lines.append(f"Available: {avail_mb/1024:.2f} GB")
          self.mem_ring["ring"].setToolTip("\n".join(tip_lines))
        except (ValueError, IndexError):
          self.mem_ring["ring"].setValue(0)
          self.mem_ring["val_lbl"].setText("unavailable")
          self.mem_ring["ring"].setToolTip("Memory usage unavailable")
      else:
        self.mem_ring["ring"].setValue(0)
        self.mem_ring["val_lbl"].setText("unavailable")
        self.mem_ring["ring"].setToolTip("Memory usage unavailable")
  
      # Disk mounts — also accumulated into an overall storage total/used
      # (in bytes, parsed back out of df's human-readable Size/Used
      # columns) so the storage ring reflects everything in the table
      # below it rather than just one arbitrarily-chosen mount.
      self.disk_tree.clear()
      total_bytes = used_bytes = 0.0
      for line in sec.get("DISK", []):
        parts = line.split(None, 5)
        if len(parts) < 6:
          continue
        fs, size, used, avail, use_pct, mounted = parts
        item = QTreeWidgetItem([fs, size, used, avail, use_pct, mounted])
        try:
          pct = float(use_pct.strip("%"))
          item.setForeground(4, QColor(_pct_color(pct)))
        except ValueError:
          pass
        self.disk_tree.addTopLevelItem(item)
  
        size_b, used_b = _parse_df_size(size), _parse_df_size(used)
        if size_b is not None and used_b is not None:
          total_bytes += size_b
          used_bytes += used_b
  
      # The shell may emit one exact "total_bytes used_bytes" line. On Linux
      # this is deduplicated by filesystem/device; on macOS APFS volumes are
      # deduplicated using the shared size/availability pair.
      stor_lines = [l for l in sec.get("STORAGE", []) if l.strip()]
      if stor_lines:
        try:
          st_total, st_used = (float(x) for x in stor_lines[0].split()[:2])
          if st_total > 0:
            total_bytes, used_bytes = st_total, max(0.0, min(st_used, st_total))
        except ValueError:
          pass
  
      if total_bytes > 0:
        storage_pct = min(100.0, used_bytes / total_bytes * 100.0)
        self._style_ring(self.storage_ring["ring"], storage_pct)
        self.storage_ring["spark"].set_color(_pct_color(storage_pct))
        self.storage_ring["spark"].push(storage_pct)
        self.storage_ring["val_lbl"].setText(
          f"{size_fmt(used_bytes)} / {size_fmt(total_bytes)}"
        )
        avail_bytes = max(0.0, total_bytes - used_bytes)
        self.storage_ring["ring"].setToolTip(
          f"Storage usage: {storage_pct:.1f}%\n"
          f"Total: {size_fmt(total_bytes)}\n"
          f"Used: {size_fmt(used_bytes)}\n"
          f"Available: {size_fmt(avail_bytes)}\n"
          f"From {self.disk_tree.topLevelItemCount()} listed mount(s)"
        )
      else:
        self.storage_ring["ring"].setValue(0)
        self.storage_ring["val_lbl"].setText("unavailable")
        self.storage_ring["ring"].setToolTip("Storage usage unavailable")
  
      self.status_msg.emit("Dashboard updated")
  
  
  def _on_host_error(self, err: str):
      self._host_snapshot = None
      self.status_msg.emit(f"Dashboard: host stats error — {err}")
      self._worker_finished("host")
  
  
  def _on_podmap_stats(self, out: str):
      self._podmap_snapshot = out
      self._worker_finished("podmap")
  
  
  def _on_podmap_error(self, err: str):
      self._podmap_snapshot = None
      self._podmap_error = err
      self._worker_finished("podmap")
  
  
  def _on_k8s_stats(self, out: str):
      # Collection callback: hold the raw snapshot until the host snapshot
      # for this same cycle is also complete.
      self._k8s_snapshot = out
      self._worker_finished("k8s")
  
  
  def _render_k8s_stats(self, out: str):
      sec = _split_sections(out)
  
      raw_node_lines = [l for l in sec.get("NODES", []) if l.strip()]
      # _K8S_CMD redirects kubectl's stderr to /dev/null rather than
      # merging it into stdout, so a normally-behaving cluster never puts
      # log noise here. This shape check is kept anyway as a second line
      # of defense: some kubectl builds print retry/deprecation warnings
      # to stdout, e.g.:
      #  E0729 12:40:28.372865  48153 memcache.go:87] couldn't get...
      # which has 5+ whitespace-separated tokens — the same shape as a
      # real "NAME STATUS ROLES AGE VERSION" row — so a check that only
      # looked at line count would treat it as a node. Validate the
      # STATUS column instead: it's always one of a known small set of
      # words for a real row, never true for a log line.
      node_lines = [l for l in raw_node_lines if _looks_like_node_row(l)]
      no_cluster = not node_lines
      if no_cluster:
        self._clear_node_grid()
        self._pods_by_node_cache = {}
        self._pods_parse_error = None
        for win in self._node_windows.values():
          win.update_pods([], {}) # clear stale data rather than leave it showing
        joined_raw = "\n".join(raw_node_lines).lower()
        if "not found" in joined_raw or "command not found" in joined_raw:
          msg = "No Kubernetes cluster detected on this instance (kubectl not installed)."
        else:
          msg = "No Kubernetes cluster detected on this instance (kubectl unavailable or no nodes)."
        self.k8s_note.setText(msg)
        self.k8s_note.show()
        self.k8s_summary_card["frame"].hide()
        self.workloads_card["frame"].hide()
        self.services_card["frame"].hide()
        self.events_card["frame"].hide()
        self.workloads_tree.clear()
        self.services_tree.clear()
        self.events_tree.clear()
        return
      self.k8s_note.hide()
      self.k8s_summary_card["frame"].show()
  
      # Conditions + node metadata come from one API request.
      cond = {}
      node_info = {}
      for line in sec.get("NODEDETAILS", []):
        if "|" not in line:
          continue
        bits = line.split("|")
        bits += [""] * (16 - len(bits))
        (name, ready, mem_p, disk_p, pid_p, kubelet, os_image, kernel,
         runtime, ip, cpu_capacity, mem_capacity, pod_capacity,
         cpu_allocatable, mem_allocatable, pod_allocatable) = bits[:16]
        name = name.strip()
        if not name:
          continue
        cond[name] = {
          "ready": ready.strip(), "mem": mem_p.strip(),
          "disk": disk_p.strip(), "pid": pid_p.strip(),
        }
        node_info[name] = {
          "kubelet": kubelet.strip(), "os": os_image.strip(),
          "kernel": kernel.strip(), "runtime": runtime.strip(),
          "ip": ip.strip(), "cpu_capacity": cpu_capacity.strip(),
          "mem_capacity": mem_capacity.strip(), "pod_capacity": pod_capacity.strip(),
          "cpu_allocatable": cpu_allocatable.strip(),
          "mem_allocatable": mem_allocatable.strip(),
          "pod_allocatable": pod_allocatable.strip(),
        }
  
      # CPU%/MEM% from `kubectl top nodes`, keyed by node name. Absent
      # entirely (older cluster, no metrics-server) just means we show
      # "n/a" instead of a bar — never an error state.
      top = {}
      for line in sec.get("TOP", []):
        parts = line.split()
        if len(parts) >= 5 and parts[2].endswith("%") and parts[4].endswith("%"):
          top[parts[0]] = {
            "cpu_pct": parts[2].rstrip("%"), "mem_pct": parts[4].rstrip("%"),
            "cpu_cores": parts[1], "mem_bytes": parts[3],
          }
  
      # Full pod inventory via jsonpath, pipe-delimited (see the note below
      # this file's _K8S_CMD for why this replaced the old `-o wide` text
      # table): namespace|name|phase|node|podIP|hostIP|qos|created|reason|
      # ready-flags|restart-counts|message, with message last and unsplit so
      # embedded "|" in a message can never shift the fields before it.
      pods_by_node = {}
      pod_lines = [l for l in sec.get("PODS", []) if l.strip()]
      self._pods_parse_error = None
      malformed_pod_rows = 0
      pod_meta = {}
  
      for line in sec.get("PODOWNERS", []):
        parts = line.split()
        if len(parts) >= 4:
          pod_meta[(parts[0], parts[1])] = f"{parts[2]}/{parts[3]}".strip("/")
  
      for line in pod_lines:
        parts = line.split("|", 11)
        if len(parts) < 11:
          malformed_pod_rows += 1
          continue
        (ns, pname, phase, node, pod_ip, host_ip, qos, created, reason,
         ready_raw, restart_raw) = parts[:11]
        message = parts[11] if len(parts) > 11 else ""
        if not pname:
          malformed_pod_rows += 1
          continue
        ready_flags = [x for x in ready_raw.split(",") if x]
        restart_counts = [x for x in restart_raw.split(",") if x]
        ready_count = sum(1 for x in ready_flags if x == "true")
        restarts = sum(int(x) for x in restart_counts if x.lstrip("-").isdigit())
        ready = f"{ready_count}/{len(ready_flags)}" if ready_flags else "-"
        owner = pod_meta.get((ns, pname), "")
        pods_by_node.setdefault(node.strip() or "(unscheduled)", []).append({
          "namespace": ns.strip(), "name": pname.strip(),
          "phase": phase.strip() or "Unknown",
          "restarts": restarts,
          "ready": ready,
          "reason": reason.strip(), "message": message.strip(),
          "pod_ip": pod_ip.strip(),
          "host_ip": host_ip.strip(), "qos": qos.strip(), "created": created.strip(),
          "owner": owner,
          "waiting": "",
        })
  
      if malformed_pod_rows:
        self._pods_parse_error = (
          f"Pod inventory returned {malformed_pod_rows} malformed row(s); "
          f"valid pod rows are still shown."
        )
      if not pod_lines:
        # An empty response is not automatically the same thing as a cluster
        # with zero pods. Keep the previous successful snapshot instead of
        # replacing every node's pod list with an apparently healthy zero.
        previous = getattr(self, "_pods_by_node_cache", {}) or {}
        if previous:
          pods_by_node = {k: list(v) for k, v in previous.items()}
          self._pods_parse_error = (
            "Pod inventory was empty during this refresh; showing the "
            "previous successful pod snapshot."
          )
  
      # Use the lightweight node map as the authoritative source for
      # node pod counts. Most importantly, distinguish a successful empty
      # result from a failed/interrupted kubectl request. A failed request
      # must never be rendered as "0 pods".
      node_map = {}
      for line in sec.get("PODNODEMAP", []):
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() == "NAMESPACE":
          continue
        ns, pname = parts[0], parts[1]
        node = " ".join(parts[2:]).strip()
        if not pname:
          continue
        node_map.setdefault(node or "(unscheduled)", []).append((ns, pname))
  
      status_lines = [x.strip() for x in sec.get("PODNODEMAP_STATUS", []) if x.strip()]
      try:
        pod_map_status = int(status_lines[-1]) if status_lines else None
      except ValueError:
        pod_map_status = None
  
      detailed = {}
      for node, items in pods_by_node.items():
        for pod in items:
          detailed[(pod["namespace"], pod["name"])] = pod
  
      if pod_map_status == 0 and node_map:
        merged = {}
        for node, items in node_map.items():
          rows = []
          for ns, pname in items:
            pod = detailed.get((ns, pname))
            if pod is None:
              pod = {
                "namespace": ns, "name": pname, "phase": "Unknown",
                "restarts": 0, "ready": "-", "reason": "", "message": "",
                "pod_ip": "", "host_ip": "", "qos": "", "created": "",
                "owner": "", "waiting": "",
              }
            rows.append(pod)
          merged[node] = rows
        pods_by_node = merged
        # Only a successful kubectl response is allowed to replace the
        # dashboard's last known-good pod snapshot.
        self._last_good_pods_by_node = {
          k: list(v) for k, v in pods_by_node.items()
        }
        self._pods_parse_error = None if detailed else (
          "Using compact Kubernetes pod inventory; detailed pod status "
          "was unavailable for this refresh."
        )
      else:
        # Empty output is not treated as a real zero-pod cluster when we
        # already have a known-good snapshot. This is important because
        # kubectl can exit 0 while returning no rows during a transient
        # API/SSH response problem.
        previous = getattr(self, "_last_good_pods_by_node", {}) or {}
        if previous:
          pods_by_node = {k: list(v) for k, v in previous.items()}
          self._pods_parse_error = (
            "Pod inventory returned no rows; showing the previous successful "
            "pod snapshot."
          )
        # Non-zero status (or no status marker, for compatibility) means the
        # pod query did not complete successfully. Keep the previous snapshot
        # rather than displaying a false zero-pod state.
        previous = getattr(self, "_last_good_pods_by_node", {}) or {}
        if previous:
          pods_by_node = {k: list(v) for k, v in previous.items()}
          self._pods_parse_error = (
            "Pod inventory refresh failed; showing the previous successful "
            "pod snapshot."
          )
        elif pod_map_status is not None:
          pods_by_node = {}
          self._pods_parse_error = (
            f"Pod inventory query failed (kubectl exit {pod_map_status})."
          )
  
      # Per-pod CPU/memory usage from `kubectl top pods`, keyed by
      # (namespace, name). Missing entirely (no metrics-server) just
      # means every pod row shows "n/a" instead of a bar — same
      # graceful-degradation rule as the node-level CPU/Memory columns.
      # Same defense-in-depth as NODES above (stderr is suppressed, but
      # validate anyway): CPU/MEM columns are checked by shape (e.g.
      # "23m", "128Mi") rather than just trusting any line with 4+ tokens.
      pod_usage = {}
      for line in sec.get("PODTOP", []):
        parts = line.split()
        if len(parts) >= 4 and _CPU_VAL_RE.match(parts[2]) and _MEM_VAL_RE.match(parts[3]):
          pod_usage[(parts[0], parts[1])] = {"cpu": parts[2], "mem": parts[3]}
  
      # ── Phase 2: workloads/services/endpoints ───────────
      workloads = []
      for line in sec.get("WORKLOADS", []):
        if "|" not in line:
          continue
        bits = line.split("|")
        bits += [""] * (16 - len(bits))
        (kind, ns, name, created, replicas, ready, available, current,
         updated, unavailable, desired, ds_desired, ds_ready, ds_available,
         ds_updated, ds_unavailable) = bits[:16]
        kind, ns, name = kind.strip(), ns.strip(), name.strip()
        if not name:
          continue
        if kind == "DaemonSet":
          replicas, ready, available = ds_desired, ds_ready, ds_available
          updated, unavailable, desired = ds_updated, ds_unavailable, ds_desired
        elif kind == "StatefulSet":
          # StatefulSet has no native availableReplicas/unavailableReplicas
          # fields. Use readyReplicas as the displayed available count and
          # derive unavailable from the desired replica count.
          available = ready
          try:
            unavailable = str(max(int(desired or 0) - int(ready or 0), 0))
          except ValueError:
            unavailable = "0"
        workloads.append({
          "kind": kind, "namespace": ns, "name": name,
          "created": created.strip(), "replicas": replicas.strip() or "0",
          "ready": ready.strip() or "0", "available": available.strip() or "0",
          "updated": updated.strip() or "0", "unavailable": unavailable.strip() or "0",
          "desired": desired.strip() or replicas.strip() or "0",
        })
  
      endpoint_counts = {}
      services = []
      for line in sec.get("SERVICES_ENDPOINTS", []):
        if "|" not in line:
          continue
        bits = line.split("|")
        bits += [""] * (11 - len(bits))
        kind, ns, name, svc_type, cluster_ip, ext_ip, ext_host, ports, created, ready_csv, notready_csv = bits[:11]
        kind, ns, name = kind.strip(), ns.strip(), name.strip()
        if not name:
          continue
        if kind == "Endpoints":
          endpoint_counts[(ns, name)] = (
            sum(1 for x in ready_csv.split(";") if x.strip()),
            sum(1 for x in notready_csv.split(";") if x.strip()),
          )
        elif kind == "Service":
          services.append({
            "namespace": ns, "name": name,
            "type": svc_type or "ClusterIP",
            "cluster_ip": cluster_ip or "—",
            "external": ext_ip.strip() or ext_host.strip() or "—",
            "ports": ports.strip().rstrip(",") or "—",
            "endpoints": "0",
            "created": created.strip(),
          })
  
      for svc in services:
        ready_n, notready_n = endpoint_counts.get((svc["namespace"], svc["name"]), (0, 0))
        svc["endpoints"] = f"{ready_n}" + (f" (+{notready_n} not ready)" if notready_n else "")
  
      # ── Phase 2: recent events ─────────────────────────
      events = []
      for line in sec.get("EVENTS", []):
        if "|" not in line:
          continue
        bits = line.split("|", 6)
        if len(bits) < 7:
          continue
        ts, etype, reason, obj_kind, ns, obj_name, message = [x.strip() for x in bits]
        if not reason and not message:
          continue
        events.append({
          "timestamp": ts, "type": etype or "Normal", "reason": reason or "—",
          "object": f"{obj_kind}/{obj_name}" if obj_kind and obj_name else obj_name,
          "namespace": ns or "—", "message": message or "—",
        })
      events = events[-50:]
  
      # NOTE: no _clear_node_grid() here — cards are now reused across
      # refreshes (see _get_or_create_node_card / _reflow_node_grid) so
      # their CircularProgress rings only animate when a value actually
      # changes, instead of replaying 0%→value on every refresh tick.
      self._pods_by_node_cache = {}
      self._pod_usage_cache  = pod_usage
      self._node_order = []
  
      self._pressure_names = {"mem": "MemoryPressure", "disk": "DiskPressure", "pid": "PIDPressure"}
  
      # Cluster-level summary. Keep this derived from the same snapshot so
      # the overview and node/pod cards always describe the same moment.
      all_pods = [p for node_pods in pods_by_node.values() for p in node_pods]
      # Pods already associated with known nodes are still in pods_by_node
      # at this point; pop() below happens afterwards.
      total_pods = len(all_pods)
      running_pods = sum(1 for p in all_pods if p["phase"].lower() == "running")
      pending_pods = sum(1 for p in all_pods if p["phase"].lower() == "pending")
      failed_pods = sum(1 for p in all_pods if p["phase"].lower() == "failed")
      namespace_lines = [x.strip() for x in sec.get("NAMESPACES", []) if x.strip()]
      if namespace_lines:
        namespaces = len(set(namespace_lines))
      else:
        # Fallback only when namespace listing was denied/unavailable.
        namespaces = len({p["namespace"] for p in all_pods if p["namespace"]})
      pressure_nodes = 0
      for node_name in cond:
        c = cond.get(node_name, {})
        if any(c.get(k) == "True" for k in ("mem", "disk", "pid")):
          pressure_nodes += 1
      ready_nodes = sum(1 for line in node_lines if len(line.split()) >= 2 and line.split()[1].lower().split(",", 1)[0] == "ready")
      node_names = [line.split()[0] for line in node_lines if line.split()]
      if not top:
        metrics_state = "Unavailable"
      elif all(name in top for name in node_names):
        metrics_state = "Available"
      else:
        metrics_state = "Partial"
      self._k8s_summary["nodes"].setText(f"{ready_nodes}/{len(node_lines)}")
      self._k8s_summary["pods"].setText(str(total_pods))
      self._k8s_summary["running"].setText(str(running_pods))
      self._k8s_summary["pending"].setText(str(pending_pods))
      self._k8s_summary["failed"].setText(str(failed_pods))
      self._k8s_summary["namespaces"].setText(str(namespaces))
      self._k8s_summary["pressure"].setText(str(pressure_nodes))
      self._k8s_summary["metrics"].setText(metrics_state)
      metrics_color = T["SUCCESS"] if metrics_state == "Available" else T["WARNING"]
      self._k8s_summary["metrics"].setStyleSheet(
        f"color: {metrics_color}; font-size: 17px; font-weight: 700;"
      )
  
      # Render workloads.
      self.workloads_tree.clear()
      for w in sorted(workloads, key=lambda x: (x["namespace"], x["kind"], x["name"])):
        desired = w["desired"]
        ready = w["ready"]
        available = w["available"]
        updated = w["updated"]
        unavailable = w["unavailable"]
        item = QTreeWidgetItem([
          w["kind"], w["namespace"], w["name"],
          f"{ready}/{desired}", desired, available, updated, unavailable,
          _short_age(w["created"])
        ])
        try:
          ready_n, desired_n = int(ready), int(desired)
          healthy = desired_n > 0 and ready_n >= desired_n
          item.setForeground(3, QColor(T["SUCCESS"] if healthy else T["WARNING"]))
        except ValueError:
          pass
        if unavailable not in ("", "0"):
          item.setForeground(7, QColor(T["DANGER"]))
        self.workloads_tree.addTopLevelItem(item)
      for col in range(9):
        self.workloads_tree.resizeColumnToContents(col)
  
      # Render services.
      self.services_tree.clear()
      for s in sorted(services, key=lambda x: (x["namespace"], x["name"])):
        item = QTreeWidgetItem([
          s["name"], s["namespace"], s["type"], s["cluster_ip"],
          s["external"], s["ports"], s["endpoints"], _short_age(s["created"])
        ])
        ep = s["endpoints"]
        if ep.startswith("0"):
          item.setForeground(6, QColor(T["DANGER"]))
        elif "not ready" in ep:
          item.setForeground(6, QColor(T["WARNING"]))
        else:
          item.setForeground(6, QColor(T["SUCCESS"]))
        self.services_tree.addTopLevelItem(item)
      for col in range(8):
        self.services_tree.resizeColumnToContents(col)
  
      # Render events. Keep warnings/errors visually prominent.
      self.events_tree.clear()
      for e in reversed(events):
        item = QTreeWidgetItem([
          e["timestamp"][11:19] if len(e["timestamp"]) >= 19 else e["timestamp"],
          e["type"], e["reason"], e["object"], e["namespace"], e["message"]
        ])
        event_type = e["type"].lower()
        item.setForeground(1, QColor(
          T["DANGER"] if event_type == "warning" else T["SUCCESS"]
        ))
        item.setToolTip(5, f"{e['reason']}: {e['message']}")
        self.events_tree.addTopLevelItem(item)
      for col in range(6):
        self.events_tree.resizeColumnToContents(col)
      if self.events_tree.topLevelItemCount() > 0:
        self.events_tree.scrollToTop()
  
      self._record_history(node_lines, top, pods_by_node, events)
  
      for line in node_lines:
        parts = line.split()
        if len(parts) < 5:
          continue
        name, status, roles = parts[0], parts[1], parts[2]
        node_pods = pods_by_node.pop(name, [])
        self._pods_by_node_cache[name] = node_pods
  
        c = cond.get(name, {})
        pressures = [label for key, label in self._pressure_names.items() if c.get(key) == "True"]
        pressure_unknown = [
          label for key, label in self._pressure_names.items()
          if c.get(key) not in ("True", "False", "")
        ]
        if pressures:
          pressure_text = ", ".join(pressures)
          pressure_ok = False
        elif pressure_unknown:
          pressure_text = "Unknown"
          pressure_ok = False
        else:
          pressure_text = "OK"
          pressure_ok = True
  
        t = top.get(name)
        cpu_pct = mem_pct = None
        cpu_tip = mem_tip = None
        if t is not None:
          try:
            cpu_pct = float(t["cpu_pct"])
            cpu_tip = f"CPU usage: {cpu_pct:.1f}%\nCores used: {t['cpu_cores']}"
          except ValueError:
            cpu_pct = None
          try:
            mem_pct = float(t["mem_pct"])
            mem_tip = f"Memory usage: {mem_pct:.1f}%\nUsed: {t['mem_bytes']}"
          except ValueError:
            mem_pct = None
  
        card = self._get_or_create_node_card(name)
        card.update_data(status, roles, cpu_pct, mem_pct,
                 len(node_pods), pressure_text, pressure_ok,
                 cpu_tip, mem_tip, node_info.get(name, {}))
        self._node_order.append(name)
  
      # Anything left in pods_by_node belongs to a node that either
      # wasn't in the NODES list or is the synthetic "(unscheduled)"
      # bucket — surface it as its own card (double-clickable, same as
      # any other node) rather than silently dropping those pods.
      # Rendered as a bucket card (see NodeCard) rather than update_data(),
      # so it can't be mistaken for a real node reporting 0% usage.
      for node_name, node_pods in pods_by_node.items():
        label = "Unscheduled / other" if node_name == "(unscheduled)" else node_name
        self._pods_by_node_cache[label] = node_pods
        card = self._get_or_create_node_card(label, is_bucket=True)
        card.update_pod_count(len(node_pods))
        self._node_order.append(label)
  
      self._reflow_node_grid(self._node_order)
  
      # Push fresh data into any node detail windows that are still open,
      # instead of leaving them showing a stale snapshot until re-clicked.
      for node_name, win in self._node_windows.items():
        win.update_pods(self._pods_by_node_cache.get(node_name, []), self._pod_usage_cache,
                 parse_error=self._pods_parse_error)
  
  
      self.status_msg.emit("Dashboard updated")
  
  
  def _on_k8s_error(self, err: str):
      self._k8s_snapshot = None
      self._k8s_error = err
      self._worker_finished("k8s")