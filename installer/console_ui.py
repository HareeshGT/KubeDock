"""Dependency-free terminal UI shared by KubeDock's Python installers."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, TextIO


class ConsoleUI:
    """Branded console output with a safe plain-text fallback for CI and pipes."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interactive: bool | None = None,
        color: bool | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        verbose: bool | None = None,
    ):
        self.stream = stream or sys.stdout
        self.sleep = sleep_fn
        self.verbose = (
            os.environ.get("KUBEDOCK_INSTALL_VERBOSE", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ) if verbose is None else bool(verbose)
        if interactive is None:
            try:
                interactive = bool(self.stream.isatty())
            except Exception:
                interactive = False
        self.interactive = bool(interactive)
        no_color = bool(os.environ.get("NO_COLOR"))
        self.color = (
            self.interactive and not no_color
        ) if color is None else bool(color and not no_color)
        self._enable_windows_vt()

    def _enable_windows_vt(self) -> None:
        if not self.color or os.name != "nt":
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            # Plain text remains fully functional if the console lacks VT support.
            self.color = False

    def _write(self, value: str, *, end: str = "\n", flush: bool = True) -> None:
        try:
            self.stream.write(value + end)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            safe = (value + end).encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.stream.write(safe)
        if flush:
            try:
                self.stream.flush()
            except Exception:
                pass

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def banner(self, subtitle: str = "Secure infrastructure workspace installer") -> None:
        lines = [
            "  ╭──────────────────────────────────────────────────────────────╮",
            "  │                                                              │",
            "  │   ██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗  ██████╗  ██████╗ │",
            "  │   ██║ ██╔╝██║   ██║██╔══██╗██╔════╝██╔══██╗██╔═══██╗██╔════╝ │",
            "  │   █████╔╝ ██║   ██║██████╔╝█████╗  ██║  ██║██║   ██║██║      │",
            "  │   ██╔═██╗ ██║   ██║██╔══██╗██╔══╝  ██║  ██║██║   ██║██║      │",
            "  │   ██║  ██╗╚██████╔╝██║  ██║███████╗██████╔╝╚██████╔╝╚██████╗ │",
            "  │   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝  ╚═════╝ │",
            "  │                                                              │",
            "  ╰──────────────────────────────────────────────────────────────╯",
        ]
        for line in lines:
            self._write(self._paint(line, "38;5;141"))
        self._write(self._paint("  KubeDock Installer", "1;37"))
        self._write(self._paint(f"  {subtitle}", "90"))
        self._write("")

    def phase(self, current: int, total: int, title: str, detail: str = "") -> None:
        total = max(1, int(total))
        current = max(0, min(int(current), total))
        percent = int(current * 100 / total)
        width = 26
        filled = int(width * current / total)
        bar = "█" * filled + "░" * (width - filled)
        self._write(self._paint(f"  [{current:02d}/{total:02d}] ", "38;5;141")
                    + self._paint(title, "1;37"))
        if detail:
            self._write(self._paint(f"          {detail}", "90"))
        if self.interactive:
            self._write(
                self._paint(f"          [{bar}]", "38;5;141")
                + self._paint(f" {percent:3d}%", "90")
            )
        else:
            self._write(f"          Progress: {percent}%")
        self._write("")

    def info(self, message: str) -> None:
        self._write(self._paint("  i  ", "36") + str(message))

    def success(self, message: str) -> None:
        self._write(self._paint("  ✓  ", "32;1") + str(message))

    def warning(self, message: str) -> None:
        self._write(self._paint("  !  ", "33;1") + str(message))

    def error(self, message: str) -> None:
        self._write(self._paint("  ✗  ", "31;1") + str(message))

    def progress(self, label: str, percent: int, downloaded: int = 0, total: int = 0) -> None:
        percent = max(0, min(100, int(percent)))
        if self.interactive:
            width = 22
            filled = int(width * percent / 100)
            bar = "█" * filled + "░" * (width - filled)
            size = ""
            if total > 0:
                size = f"  {downloaded / (1024 * 1024):.0f}/{total / (1024 * 1024):.0f} MiB"
            self._write(
                self._paint(f"\r  {label} ", "36")
                + self._paint(f"[{bar}] {percent:3d}%", "38;5;141")
                + self._paint(size, "90"),
                end="",
            )
        else:
            self._write(f"  {label}: {percent}%")

    def clear_progress(self) -> None:
        if self.interactive:
            self._write("", end="\r\033[2K")

    @staticmethod
    def command_label(command) -> str:
        tokens = [str(item).lower() for item in command]
        joined = " ".join(tokens)
        if "clone" in tokens and "git" in tokens:
            return "Clone KubeDock source"
        if "pip" in tokens and "install" in tokens:
            return "Install Python packages"
        if "pyinstaller" in joined:
            return "Build KubeDock application"
        if "--only-binary=:all:" in joined:
            return "Install optional microphone support"
        if "importlib" in joined or "import torch" in joined:
            return "Verify Python dependencies"
        return "Run installer command"

    def run(
        self,
        command,
        *,
        cwd=None,
        check: bool = True,
        label: str | None = None,
    ) -> subprocess.CompletedProcess:
        command = [str(item) for item in command]
        display = subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)
        label = label or self.command_label(command)

        if not self.interactive:
            self._write(f"\n> {display}")
            return subprocess.run(command, cwd=cwd, check=check)

        with tempfile.TemporaryFile(mode="w+b") as log:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            frames = "|/-\\"
            frame_index = 0
            while process.poll() is None:
                self._write(
                    self._paint(f"\r  {frames[frame_index % len(frames)]}", "38;5;141")
                    + f"  {label}",
                    end="",
                )
                frame_index += 1
                self.sleep(0.12)
            return_code = process.returncode
            self.clear_progress()
            log.seek(0)
            output = log.read().decode("utf-8", errors="replace")

        if return_code == 0:
            self.success(label)
            if self.verbose and output.strip():
                self._write(output.rstrip())
        else:
            self.error(f"{label} failed (exit code {return_code})")
            if output.strip():
                self._write("\n".join(output.rstrip().splitlines()[-28:]))
            if check:
                raise subprocess.CalledProcessError(return_code, command, output=output)
        return subprocess.CompletedProcess(command, return_code, stdout=output)

    def complete(self, message: str = "Installation completed successfully") -> None:
        self._write("")
        self._write(self._paint("  ╭──────────────────────────────────────────────────────────────╮", "32;1"))
        self._write(self._paint(f"  │   ✓  {message}", "32;1"))
        self._write(self._paint("  ╰──────────────────────────────────────────────────────────────╯", "32;1"))
        self._write("")

    def failed(self, message: str) -> None:
        self._write("")
        self.error(message)
        self.info("Review the error above and retry. Existing KubeDock installation was preserved where possible.")
