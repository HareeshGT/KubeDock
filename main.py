"""main.py — Application entry point for KubeDock."""

import logging
import os
import sys

from PyQt5.QtWidgets import QApplication, QStyleFactory
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtCore import Qt, QPropertyAnimation, QEasingCurve

from themes import T, build_qss, load_settings
from file_stream_lifecycle import install as _install_file_stream_lifecycle
from main_window import KubeDockWindow
from splash import SplashScreen
from webapp import app as webapp_app


# Install the file/preview worker lifecycle fix before any UI instance can
# create a FileStreamReadWorker. This keeps existing editor/preview call
# sites unchanged while ensuring a cancelled reader has stopped before the
# next reader starts.
_install_file_stream_lifecycle()


def _build_palette() -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(T["BG_DARK"]))
    pal.setColor(QPalette.WindowText, QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Base, QColor(T["BG_PANEL"]))
    pal.setColor(QPalette.AlternateBase, QColor(T["BG_ITEM"]))
    pal.setColor(QPalette.Text, QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Button, QColor(T["BG_ITEM"]))
    pal.setColor(QPalette.ButtonText, QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Highlight, QColor(T["ACCENT"]))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor(T["BG_PANEL"]))
    pal.setColor(QPalette.ToolTipText, QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.PlaceholderText, QColor(T["TEXT_MUTED"]))
    return pal


def main() -> int:
    # Keep routine launches quiet, but make the resource-loading timing
    # diagnostics easy to enable without editing the source. DEBUG messages
    # include operation labels and durations only, never command output.
    log_level = getattr(logging, os.environ.get("KUBEDOCK_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName("KubeDock")
    app.setOrganizationName("KubeDock")

    if "Fusion" in QStyleFactory.keys():
        app.setStyle(QStyleFactory.create("Fusion"))

    app.setStyleSheet(build_qss())
    app.setPalette(_build_palette())

    splash = SplashScreen()
    window_ref = {}

    # The web server lives with the desktop process. It receives a callback
    # to the current KubeDockWindow.ssh object, so reconnecting in KubeDock
    # automatically changes the SSH connection used by the web app too.
    app.aboutToQuit.connect(webapp_app.stop_server)

    def _begin_zoom():
        window = KubeDockWindow()
        window_ref["window"] = window
        if load_settings().get("webapp_enabled", False):
            webapp_app.start_server(lambda: getattr(window, "ssh", None))

        window.setWindowOpacity(0.0)
        window.showFullScreen()

        fade_in = QPropertyAnimation(window, b"windowOpacity", window)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setDuration(SplashScreen.ZOOM_MS)
        fade_in.setEasingCurve(QEasingCurve.InOutCubic)

        splash.zoom_into(None, on_done=_finish, companions=[fade_in])

    def _finish():
        window_ref["window"].setWindowOpacity(1.0)
        window_ref["window"].raise_()
        window_ref["window"].activateWindow()
        splash.close()
        window_ref["window"].check_initial_lock()

    splash.finished.connect(_begin_zoom)
    splash.show()
    splash.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
