"""Fixed, deterministic model catalog and local hardware detection for the installer."""
from __future__ import annotations
from dataclasses import dataclass
import os, platform, shutil, subprocess, sys
from pathlib import Path

@dataclass(frozen=True)
class Model:
    id: str
    name: str
    filename: str
    url: str
    sha256: str
    size_mb: int
    min_ram_gb: int
    recommended_ram_gb: int
    min_disk_gb: int
    min_cpu_cores: int
    description: str

MODELS = (
    Model("smollm2-360m-q4", "SmolLM2 360M Instruct (Q4_K_M)",
          "SmolLM2-360M-Instruct-Q4_K_M.gguf",
          "https://huggingface.co/bartowski/SmolLM2-360M-Instruct-GGUF/resolve/ab928a97ee49f3a015f35194879f68211291d6ca/SmolLM2-360M-Instruct-Q4_K_M.gguf",
          "2fa3f013dcdd7b99f9b237717fa0b12d75bbb89984cc1274be1471a465bac9c2",
          271, 4, 8, 1, 2, "Lightweight model for lower-memory machines."),
    Model("qwen3-06b-q4km", "Qwen3 0.6B Instruct (Q4_K_M)",
          "Qwen_Qwen3-0.6B-Q4_K_M.gguf",
          "https://huggingface.co/bartowski/Qwen_Qwen3-0.6B-GGUF/resolve/f9184d8b4c371cb52af5b8cb3d38e55e3cf0f4a0/Qwen_Qwen3-0.6B-Q4_K_M.gguf",
          "9acfc1e001311f34b4252001b626f2e466d592a42065f66571bff3790d4e1b14",
          484, 8, 12, 2, 4, "Stronger general-purpose model; requires more memory."),
)

@dataclass(frozen=True)
class Hardware:
    os: str
    architecture: str
    cpu: str
    cpu_cores: int
    ram_gb: int
    available_ram_gb: int
    gpu: str
    vram_gb: int
    free_disk_gb: int

def detect_hardware(install_dir=None) -> Hardware:
    cpu = platform.processor() or platform.machine() or "Unknown CPU"
    cores = max(1, os.cpu_count() or 1)
    total = available = 0
    try:
        if sys.platform == "win32":
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                    ("page_total", ctypes.c_ulonglong), ("page_avail", ctypes.c_ulonglong),
                    ("virtual_total", ctypes.c_ulonglong), ("virtual_avail", ctypes.c_ulonglong),
                    ("extended", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total, available = status.total, status.avail
        elif sys.platform == "darwin":
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
            vm = subprocess.check_output(["vm_stat"], text=True)
            page_size = int(subprocess.check_output(["sysctl", "-n", "hw.pagesize"], text=True).strip())
            pages = vm.split("Pages free:")[1].splitlines()[0].strip().replace(".", "")
            available = int(pages) * page_size
        else:
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                values[key] = int(rest.strip().split()[0]) * 1024
            total, available = values.get("MemTotal", 0), values.get("MemAvailable", values.get("MemFree", 0))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    gpu, vram = "Not detected", 0
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            lines = subprocess.check_output([nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], text=True, timeout=3).strip().splitlines()
            if lines:
                name, _, mem = lines[0].partition(",")
                gpu, vram = name.strip(), int(mem.strip()) // 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if gpu == "Not detected" and sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        gpu = "Apple Silicon integrated GPU (unified memory)"
    path = Path(install_dir or Path.cwd())
    try:
        disk = shutil.disk_usage(path if path.exists() else path.parent).free
    except OSError:
        disk = 0
    gib = 1024 ** 3
    return Hardware(platform.system(), platform.machine(), cpu, cores, total // gib,
                    available // gib, gpu, vram, disk // gib)

def compatible_models(hw: Hardware):
    return [m for m in MODELS if hw.ram_gb >= m.min_ram_gb and
            hw.cpu_cores >= m.min_cpu_cores and hw.free_disk_gb >= m.min_disk_gb]

def recommended_model(hw: Hardware):
    available = compatible_models(hw)
    if not available:
        return MODELS[0]
    qwen = next((m for m in available if m.id == "qwen3-06b-q4km"), None)
    return qwen if qwen and hw.available_ram_gb >= 6 else available[0]
