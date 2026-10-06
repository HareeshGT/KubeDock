"""Windows host metrics for the Dashboard (SSH to a Windows machine).

The Linux/macOS collector is a POSIX shell script whose output is split into
``__SECTION__`` blocks and rendered by ``DashboardRefreshMixin._render_host_stats``.
Windows cannot run that script (cmd.exe / PowerShell have no ``uname``,
``free``, ``df``, ``export`` ...), so this module provides the Windows half:

    PowerShell/CIM  ->  JSON per metric  ->  normalize_host_output()
                    ->  the SAME ``__SECTION__`` text Linux produces
                    ->  existing renderer / widgets (unchanged)

Each metric is collected in its own try/catch, so one failing metric leaves
only that widget "unavailable" instead of blanking the whole Dashboard.
This module deliberately has no Qt dependency so it can be unit-tested alone.
"""
import base64
import json
import logging

log = logging.getLogger("kubedock.dashboard.windows")

# Exec'd first (raw, no POSIX preamble). `cmd /c echo` prints the marker under
# cmd.exe and PowerShell; on Linux/macOS `cmd` doesn't exist, so it is absent.
WINDOWS_PROBE_CMD = "cmd /c echo KUBEDOCK_WINDOWS_PROBE"
WINDOWS_PROBE_MARKER = "KUBEDOCK_WINDOWS_PROBE"

_PS_SCRIPT = r"""
$ProgressPreference='SilentlyContinue';$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding $false
function M($n,[scriptblock]$b){Write-Output "__${n}__";try{$r=& $b;ConvertTo-Json -InputObject $r -Compress -Depth 3}catch{Write-Output "__ERR_${n}__";Write-Output ($_.Exception.Message -replace '\s+',' ')}}
M HOST {[System.Net.Dns]::GetHostName()}
M OS {$o=Get-CimInstance Win32_OperatingSystem;@{caption=$o.Caption;version=$o.Version;build=$o.BuildNumber;uptime_s=[int64]((Get-Date)-$o.LastBootUpTime).TotalSeconds}}
M CPU {@(Get-CimInstance Win32_Processor|ForEach-Object{@{name=$_.Name;logical=$_.NumberOfLogicalProcessors}})}
M CPUPCT {$l=(Get-CimInstance Win32_Processor|Measure-Object LoadPercentage -Average).Average;if($null -eq $l){$l=(Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples[0].CookedValue};[double]$l}
M MEM {$o=Get-CimInstance Win32_OperatingSystem;@{total_kb=[int64]$o.TotalVisibleMemorySize;free_kb=[int64]$o.FreePhysicalMemory}}
M DISK {@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|ForEach-Object{@{id=$_.DeviceID;size=$_.Size;free=$_.FreeSpace}})}
"""


def build_windows_host_command() -> str:
    """One command line that works from cmd.exe *and* PowerShell default shells.

    -EncodedCommand (base64 of UTF-16LE) sidesteps every quoting/escaping
    problem ($vars, quotes, pipes) regardless of which shell sshd launches.
    """
    script = "\n".join(l for l in _PS_SCRIPT.strip().splitlines())
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded}"
    )


def is_windows_probe_output(out: str) -> bool:
    return WINDOWS_PROBE_MARKER in (out or "")


# ---------------------------------------------------------------- parsing

def _sections(text: str) -> dict:
    sec, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("__") and s.endswith("__") and len(s) > 4 and " " not in s:
            cur = s.strip("_")
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(line)
    return sec


def _json(lines):
    body = "\n".join(lines).strip()
    if not body:
        raise ValueError("empty output")
    return json.loads(body)


def _as_list(v):
    # ConvertTo-Json yields a bare object (not a 1-element array) for one item.
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _hum(n: float) -> str:
    """Humanize bytes like the Linux `df -h` column ("237G", "1.8T")."""
    units = "BKMGTP"
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(v + 0.5)}B"
    return f"{int(v + 0.5)}{units[i]}" if v >= 10 else f"{v:.1f}{units[i]}"


def _uptime_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    for val, unit in ((d, "day"), (h, "hour"), (m, "minute")):
        if val:
            parts.append(f"{val} {unit}{'' if val == 1 else 's'}")
    return "up " + (", ".join(parts) if parts else "less than a minute")


def normalize_host_output(raw: str) -> str:
    """Turn the PowerShell JSON output into Linux-format ``__SECTION__`` text.

    Metrics that failed to collect or parse are simply omitted (the existing
    renderer shows those widgets as "unavailable") and the reason is logged.
    Nothing is defaulted or invented.
    """
    # CommandWorker appends "\n[stderr]\n<stderr>" to stdout when stderr exists.
    out, _, stderr = (raw or "").partition("\n[stderr]\n")
    if stderr.strip():
        log.warning("windows host metrics stderr: %s", stderr.strip()[:300])
    sec = _sections(out)
    res = []

    for name in list(sec):
        if name.startswith("ERR_"):
            log.warning("windows metric %s failed on remote: %s",
                        name[4:], " ".join(sec[name]).strip()[:300])

    def metric(name, fn):
        """Run one parser; on any failure log it and omit that metric only."""
        if name not in sec or "ERR_" + name in sec:
            if name not in sec:
                log.warning("windows metric %s missing from output", name)
            return
        try:
            lines = fn(_json(sec[name]))
        except Exception as e:  # parse failure must not blank the dashboard
            log.warning("windows metric %s could not be parsed: %s", name, e)
            return
        res.extend(lines)

    def host(v):
        v = str(v).strip()
        if not v:
            raise ValueError("empty hostname")
        return ["__HOST__", v]

    def os_(v):
        caption = str(v.get("caption") or "Windows").strip()
        version = str(v.get("version") or "").strip()
        lines = ["__OS__", caption, "__KERNEL__", version or str(v.get("build") or "—")]
        if v.get("uptime_s") is not None:  # uptime missing != OS info missing
            lines += ["__UPTIME__", _uptime_text(v["uptime_s"])]
        return lines

    def cpu(v):
        procs = [p for p in _as_list(v) if isinstance(p, dict)]
        if not procs:
            raise ValueError("no processors reported")
        cores = sum(int(p.get("logical") or 0) for p in procs)
        if cores <= 0:
            raise ValueError("no logical processor count")
        model = " ".join(str(procs[0].get("name") or "Unknown CPU").split())
        return ["__CPU__", str(cores), model]

    def cpupct(v):
        pct = float(v)
        if not 0.0 <= pct <= 100.0:
            raise ValueError(f"out of range: {pct}")
        # Windows has no user/sys split here, so report only what we measured.
        return ["__CPUPCT__", f"CPU usage: {100.0 - pct:.1f}% idle"]

    def mem(v):
        total_kb, free_kb = int(v["total_kb"]), int(v["free_kb"])
        if total_kb <= 0 or not 0 <= free_kb <= total_kb:
            raise ValueError(f"invalid memory figures {total_kb}/{free_kb}")
        total_mb, free_mb = total_kb // 1024, free_kb // 1024
        used_mb = total_mb - free_mb
        return ["__MEM__", f"Mem: {total_mb} {used_mb} {free_mb} 0 0 {free_mb}"]

    def disk(v):
        rows, tot, used_sum = [], 0, 0
        for d in _as_list(v):
            if d.get("size") in (None, "") or d.get("free") in (None, ""):
                continue  # e.g. drive present but not ready
            size, free = int(d["size"]), int(d["free"])
            if size <= 0:
                continue
            used = max(0, size - free)
            rows.append(
                f"{d['id']} {_hum(size)} {_hum(used)} {_hum(free)} "
                f"{int(used / size * 100 + 0.5)}% {d['id']}\\"
            )
            tot += size
            used_sum += used
        if not rows:
            raise ValueError("no readable fixed disks")
        return ["__DISK__"] + rows + ["__STORAGE__", f"{tot} {used_sum}"]

    metric("HOST", host)
    metric("OS", os_)
    metric("CPU", cpu)
    metric("CPUPCT", cpupct)
    metric("MEM", mem)
    metric("DISK", disk)
    # LOAD is intentionally never emitted: Windows has no load average.
    return "\n".join(res) + "\n"