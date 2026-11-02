import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALLER_DIR = Path(__file__).resolve().parents[1] / "installer"
sys.path.insert(0, str(INSTALLER_DIR))

from console_ui import ConsoleUI


class InstallerConsoleUITests(unittest.TestCase):
    def test_banner_and_phase_render_in_plain_text_mode(self):
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, interactive=False, color=False)
        ui.banner("Test installation")
        ui.phase(2, 4, "Installing dependencies", "Installing Python packages.")
        rendered = stream.getvalue()
        self.assertIn("KubeDock Installer", rendered)
        self.assertIn("Test installation", rendered)
        self.assertIn("[02/04]", rendered)
        self.assertIn("Installing dependencies", rendered)
        self.assertIn("Progress: 50%", rendered)
        self.assertNotIn("\033[", rendered)

    def test_ascii_wordmark_spells_kubedock_and_fits_its_frame(self):
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, interactive=False, color=False)
        ui.banner()
        rows = stream.getvalue().splitlines()
        frame = [
            line for line in rows
            if line.startswith("  ╭") or line.startswith("  ╰") or line.startswith("  │")
        ]
        self.assertEqual(len(frame), 9)
        self.assertEqual({len(line) for line in frame}, {75})
        logo_rows = [line for line in frame if "██" in line or "╚═╝" in line]
        self.assertEqual(len(logo_rows), 6)
        self.assertTrue(logo_rows[-1].endswith("╚═╝  ╚═╝ │"))

        shell_ui = (INSTALLER_DIR / "console_ui.sh").read_text(encoding="utf-8")
        self.assertIn("██████╗ ██╗  ██╗ │", shell_ui)
        self.assertIn("╚═════╝ ╚═╝  ╚═╝ │", shell_ui)

    def test_status_messages_are_consistent(self):
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, interactive=False, color=False)
        ui.info("Checking Python")
        ui.success("Python ready")
        ui.warning("Optional voice disabled")
        ui.error("Build failed")
        rendered = stream.getvalue()
        self.assertIn("  i  Checking Python", rendered)
        self.assertIn("  ✓  Python ready", rendered)
        self.assertIn("  !  Optional voice disabled", rendered)
        self.assertIn("  ✗  Build failed", rendered)

    def test_progress_is_readable_without_a_tty(self):
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, interactive=False, color=False)
        ui.progress("Downloading", 60, 60, 100)
        self.assertIn("Downloading: 60%", stream.getvalue())

    def test_command_runner_preserves_noninteractive_subprocess_behavior(self):
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, interactive=False, color=False)
        result = ui.run([sys.executable, "-c", "print('installer-check')"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("\n> ", stream.getvalue())

    def test_interactive_command_runner_shows_spinner_and_success(self):
        stream = io.StringIO()
        ui = ConsoleUI(
            stream=stream,
            interactive=True,
            color=False,
            sleep_fn=lambda _: __import__("time").sleep(0.01),
        )
        result = ui.run(
            [sys.executable, "-c", "print('installer-check')"],
            label="Build KubeDock",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("installer-check", result.stdout)
        self.assertIn("Build KubeDock", stream.getvalue())
        self.assertIn("✓", stream.getvalue())

    def test_interactive_command_failure_keeps_last_log_lines(self):
        stream = io.StringIO()
        ui = ConsoleUI(
            stream=stream,
            interactive=True,
            color=False,
            sleep_fn=lambda _: __import__("time").sleep(0.01),
        )
        result = ui.run(
            [sys.executable, "-c", "print('step-log'); raise SystemExit(3)"],
            check=False,
            label="Install package",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("Install package failed", stream.getvalue())
        self.assertIn("step-log", stream.getvalue())

    def test_no_color_environment_disables_ansi_even_when_requested(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            ui = ConsoleUI(stream=stream, interactive=True, color=True)
        ui.success("Ready")
        self.assertFalse(ui.color)
        self.assertNotIn("\033[", stream.getvalue())


    def test_dumb_terminal_disables_ansi(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=False):
            ui = ConsoleUI(stream=stream, interactive=True)
        ui.success("Ready")
        self.assertFalse(ui.color)
        self.assertNotIn("\033[", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
