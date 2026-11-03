"""Dashboard telemetry collection backed by the shared command service."""

from __future__ import annotations

import re
import shlex

from utils import size_fmt

from .lifecycle import activate_lifecycle


class DashboardService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    def contextual_command(self, command, context):
        """Pin every top-level kubectl invocation to the selected context."""
        value = str(context or "").strip()
        if not value:
            return command
        flag = f"--context={shlex.quote(value)}"
        return re.sub(
            r"(^\s*)kubectl(?!\s+--context(?:=|\s))(?=\s)",
            lambda match: match.group(1) + "kubectl " + flag,
            command,
            flags=re.MULTILINE,
        )

    def create_worker(
        self, worker_pool, command, *, timeout=None, raw=False,
        operation_key=None,
    ):
        activate_lifecycle(self)
        return self.commands.create_worker(
            worker_pool, command, timeout=timeout, raw=raw,
            operation_key=operation_key,
        )


    @staticmethod
    def _split_sections(output):
        sections, current = {}, None
        for line in (output or "").splitlines():
            marker = line.strip()
            if marker.startswith("__") and marker.endswith("__") and len(marker) > 4 and " " not in marker:
                current = marker.strip("_")
                sections[current] = []
                continue
            if current is not None:
                sections[current].append(line)
        return sections

    @staticmethod
    def parse_process_snapshot(out: str):
        """Parse Linux/macOS `top` output into stable dashboard fields."""
        lines = DashboardService._split_sections(out).get("TOP_PROCESS", [])
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
