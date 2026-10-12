"""KubeDock SVG icon helpers.

Icons are stored under assets/icons and rendered with Qt's SVG renderer.
The helpers below preserve the public API used by the existing application.

Rendering notes
----------------
Every icon in the app funnels through ``icon()`` / ``icon_pixmap()`` /
``set_icon()`` below, so this is the single place that controls how crisp
icons look. Two things used to make icons look blurry on modern (HiDPI)
displays:

1. Icons were rasterised once at the exact *logical* pixel size requested
   (e.g. 18x18) with no ``devicePixelRatio`` set on the resulting
   ``QPixmap``. On a 2x/3x display, Qt then had to stretch that small
   bitmap to fill the physical pixels, which is what produced the blur.
2. The renderer never set ``Antialiasing`` / ``SmoothPixmapTransform``
   hints, so even 1x renders had rough, aliased edges.

``icon()`` now renders every SVG at the highest device pixel ratio among
the app's screens (so it stays crisp if the window is dragged to a
higher-DPI monitor), tags the pixmap with that ratio so Qt paints it at
native resolution, and caches the result so repeated lookups (the same
icon/color/size is requested constantly while building the UI) are cheap.
"""

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, QSize, QRectF, QTimer
from PyQt5.QtGui import QColor, QIcon, QPixmap, QPainter
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QApplication, QPushButton, QTabWidget, QWidget

try:
    from themes import T, on_color
except Exception:
    T = {
        "TEXT_PRIMARY": "#E8EAF0",
        "TEXT_DIM": "#A7ACB8",
        "ACCENT": "#A855F7",
        "DANGER": "#F87171",
    }

    def on_color(hex_color):
        return "#ffffff"

ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"

# Cache of rendered QIcons, keyed by (path, color-as-string, size, dpr).
# Small and bounded in practice: a fixed icon set x a handful of theme
# colors x a handful of sizes x the screen(s) in use.
_ICON_CACHE: dict = {}
_MAX_CACHE_ENTRIES = 512


def _to_qcolor(value, fallback="#E8EAF0") -> QColor:
    """Convert a color string/QColor into a valid QColor."""
    if isinstance(value, QColor):
        return QColor(value)

    color = QColor(str(value if value is not None else fallback))
    if not color.isValid():
        color = QColor(fallback)

    return color


def default_icon_color(widget=None) -> str:
    """Theme-aware tint for icons that were not given an explicit color.

    Every SVG under assets/icons is drawn with a hardcoded white stroke (it
    was designed for dark themes), so an untinted icon is invisible on a
    light theme such as Lavender Light. Tinting with the *live* theme
    palette keeps them readable everywhere:

    * ``primary`` buttons (solid accent fill) -> the readable on-accent color
    * ``danger`` buttons                      -> the danger color
    * checked buttons (solid accent fill)     -> the readable on-accent color
    * everything else                         -> the primary text color
    """
    try:
        if widget is not None:
            name = widget.objectName()
            if name == "primary":
                return on_color(T["ACCENT"])
            if name == "danger":
                return T["DANGER"]
            if (
                hasattr(widget, "isCheckable")
                and widget.isCheckable()
                and widget.isChecked()
            ):
                return on_color(T["ACCENT"])
    except RuntimeError:
        # Underlying C++ widget already deleted.
        pass

    return T.get("TEXT_PRIMARY", "#E8EAF0")


def _device_pixel_ratio() -> float:
    """Highest devicePixelRatio across the app's screens (>= 1.0).

    Rendering at this ratio, rather than always 1.0, is what makes icons
    render crisp instead of blurry on HiDPI/Retina displays. Using the
    *highest* screen ratio (rather than just the primary screen) keeps
    icons sharp even if the window is later moved to a higher-DPI
    monitor in a mixed-DPI multi-monitor setup.
    """
    app = QApplication.instance()
    if app is None:
        return 1.0

    try:
        ratios = [s.devicePixelRatio() for s in app.screens() if s is not None]
        ratios = [r for r in ratios if r and r > 0]
        if ratios:
            return max(ratios)
    except Exception:
        pass

    try:
        dpr = app.devicePixelRatio()
        if dpr and dpr > 0:
            return dpr
    except Exception:
        pass

    return 1.0


def icon_path(name: str) -> str:
    """Return the absolute path to an SVG icon."""
    name = str(name or "").strip()

    if not name:
        return ""

    if name.lower().endswith(".svg"):
        filename = name
    else:
        filename = f"{name}.svg"

    return str(ICON_DIR / filename)


def _render_pixmap(path: str, color: Optional[str], size: int) -> Optional[QPixmap]:
    """Rasterise one SVG at the screen's device pixel ratio, cached.

    Shared by ``icon()`` and ``icon_pixmap()`` so both go through the same
    crisp, antialiased, HiDPI-aware path — the pixmap this returns already
    carries the correct ``devicePixelRatio``, so callers can hand it
    straight to ``QLabel.setPixmap`` or ``QIcon.addPixmap`` without any
    further scaling (extra scaling is exactly what reintroduces blur).
    """
    color_key = None
    if color is not None:
        color_key = color.name() if isinstance(color, QColor) else str(color)

    dpr = _device_pixel_ratio()
    cache_key = ("pixmap", path, color_key, size, round(dpr, 2))

    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    renderer = QSvgRenderer(path)
    if not renderer.isValid():
        return None

    # Rasterise at size * dpr physical pixels, then tell the pixmap what
    # ratio that corresponds to. Qt then paints it 1:1 on a matching
    # HiDPI screen instead of upscaling a low-res bitmap (the source of
    # the blur).
    physical = max(1, round(size * dpr))
    pixmap = QPixmap(physical, physical)
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(dpr)

    painter = QPainter(pixmap)

    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # Target rect is in logical (device-independent) coordinates —
        # QPainter already accounts for the pixmap's devicePixelRatio.
        renderer.render(painter, QRectF(0, 0, size, size))

        if color is not None:
            painter.setCompositionMode(
                QPainter.CompositionMode_SourceIn
            )
            painter.fillRect(
                QRectF(0, 0, size, size),
                _to_qcolor(color),
            )
    finally:
        painter.end()

    if len(_ICON_CACHE) >= _MAX_CACHE_ENTRIES:
        _ICON_CACHE.clear()
    _ICON_CACHE[cache_key] = pixmap

    return pixmap


def icon(
    name: str,
    color: Optional[str] = None,
    size: int = 18,
) -> QIcon:
    """Load and optionally tint an SVG icon.

    Rendered at the screen's device pixel ratio with antialiasing so the
    result is crisp (not blurry) on both standard and HiDPI displays, and
    cached so repeated calls for the same icon/color/size are free.
    """
    size = max(1, int(size))
    path = icon_path(name)

    if not path or not Path(path).is_file():
        return QIcon()

    if color is None:
        color = default_icon_color()

    pixmap = _render_pixmap(path, color, size)
    if pixmap is None:
        return QIcon()

    result = QIcon()
    result.addPixmap(pixmap)
    return result


def set_icon(
    widget,
    name: str,
    color: Optional[str] = None,
    size: int = 18,
) -> None:
    """Set an SVG icon on any Qt widget supporting setIcon().

    When ``color`` is omitted the icon follows the active theme (see
    ``default_icon_color``) and is re-tinted automatically on theme change
    via ``refresh_theme_icons()``.
    """
    if widget is None:
        return

    # Remember how this icon was requested so it can be re-tinted when the
    # theme changes. An explicit color is the caller's choice and is kept.
    try:
        widget.setProperty("kd_icon_name", name)
        widget.setProperty("kd_icon_size", int(size))
        widget.setProperty("kd_icon_color", color if color is not None else "")
    except Exception:
        pass

    tint = color if color is not None else default_icon_color(widget)

    qt_icon = icon(
        name,
        color=tint,
        size=size,
    )

    widget.setIcon(qt_icon)

    if hasattr(widget, "setIconSize"):
        widget.setIconSize(QSize(size, size))

    if color is None:
        # Callers often set objectName("primary") / checkable state right
        # after creating the button, i.e. after this call. Re-resolve once
        # the current event-loop turn finishes so the tint matches the final
        # widget state.
        _schedule_retint(widget)


def _retint_widget(widget) -> None:
    """Re-apply a widget's remembered themed icon with the live theme."""
    try:
        name = widget.property("kd_icon_name")
        if not name or widget.property("kd_icon_color"):
            return  # no icon, or the caller chose an explicit color

        size = int(widget.property("kd_icon_size") or 16)
        widget.setIcon(icon(name, color=default_icon_color(widget), size=size))
    except RuntimeError:
        # Widget deleted before the deferred call ran.
        pass


def _schedule_retint(widget) -> None:
    try:
        QTimer.singleShot(0, lambda w=widget: _retint_widget(w))
    except Exception:
        pass


def _tab_pixmap_icon(name: str, size: int) -> QIcon:
    """Tab icon: dim when idle, full-strength when the tab is selected."""
    path = icon_path(name)
    if not path or not Path(path).is_file():
        return QIcon()

    normal = _render_pixmap(path, T.get("TEXT_DIM", "#A7ACB8"), size)
    active = _render_pixmap(path, T.get("TEXT_PRIMARY", "#E8EAF0"), size)

    result = QIcon()
    if normal is not None:
        result.addPixmap(normal, QIcon.Normal)
    if active is not None:
        result.addPixmap(active, QIcon.Selected)
    return result


def refresh_theme_icons(root=None) -> None:
    """Re-tint every themed icon after a theme change.

    Walks ``root`` (or every widget in the application) and re-applies the
    icons that were created without an explicit color: button/label-less
    icons registered through ``set_icon`` and tab icons registered through
    ``add_icon_tab``. Call this after ``apply_theme_vars()``.
    """
    app = QApplication.instance()
    if root is None:
        widgets = app.allWidgets() if app is not None else []
    else:
        widgets = [root] + root.findChildren(QWidget)

    for w in widgets:
        try:
            if w.property("kd_icon_name"):
                _retint_widget(w)

            if isinstance(w, QTabWidget):
                for i in range(w.count()):
                    page = w.widget(i)
                    name = page.property("kd_tab_icon") if page is not None else None
                    if name:
                        size = int(page.property("kd_tab_icon_size") or 16)
                        w.setTabIcon(i, _tab_pixmap_icon(name, size))
        except RuntimeError:
            continue


def icon_pixmap(
    name: str,
    color: Optional[str] = None,
    size: int = 18,
) -> QPixmap:
    """Return a rendered SVG icon as a QPixmap.

    Renders directly at the screen's device pixel ratio (see
    ``_render_pixmap``) rather than asking a ``QIcon`` for a pixmap of a
    given logical size — that round trip can hand back a pixmap tagged
    with the wrong devicePixelRatio, which is what caused labels using
    ``setPixmap(icon_pixmap(...))`` to look soft on HiDPI screens.
    """
    size = max(1, int(size))
    path = icon_path(name)

    if not path or not Path(path).is_file():
        return QPixmap()

    if color is None:
        color = default_icon_color()

    pixmap = _render_pixmap(path, color, size)
    return pixmap if pixmap is not None else QPixmap()


# ============================================================
# Semantic label mapping
# ============================================================

LABEL_TO_ICON = {
    # Main UI
    "dashboard": "dashboard",
    "home": "home",
    "file manager": "folder",
    "files": "file",
    "settings": "settings",
    "terminal": "terminal",
    "list view": "list",
    "grid view": "grid",
    "security": "security",

    # Infrastructure
    "kubernetes": "kubernetes",
    "pods": "pods",
    "deployments": "deployment",
    "statefulsets": "statefulsets",
    "daemonsets": "daemonsets",
    "hpa": "hpa",
    "services": "services",
    "service": "service",
    "ingress": "ingress",
    "jobs": "jobs",
    "cronjobs": "jobs",
    "storage": "storage",
    "configmaps": "config",
    "secrets": "lock",
    "events": "events",
    "tunnels": "tunnel",
    "tunnel": "tunnel",
    "ftp": "ftp",
    "ftps": "ftp",
    "instance": "instance",
    "server": "server",
    "database": "database",
    "network": "network",
    "docker": "docker",
    "cloud": "cloud",
    "monitor": "monitor",
    "analytics": "chart",

    # Connection
    "connect": "connect",
    "disconnect": "disconnect",
    "switch user": "switch_user",
    "exit sudo": "lock",

    # Actions
    "search": "search",
    "filter": "search",
    "describe": "describe",
    "logs": "logs",
    "log": "log",
    "exec": "exec",
    "exec shell": "exec",
    "delete": "delete",
    "remove": "delete",
    "new folder": "plus",
    "add service": "plus",
    "add a new service": "plus",
    "save": "save",
    "save to vm": "save",
    "refresh": "refresh",
    "clear": "clear",
    "view": "eye",
    "open": "folder",
    "upload": "upload",
    "download": "download",
    "kill port": "stop",
    "select all": "check",

    # Voice / security
    "voice": "mic",
    "speak": "volume_high",
    "mute": "volume_high",
    "locked": "lock",
    "your session is protected": "shield",
    "pin": "key",
    "google web speech": "mic",

    # AI
    "ai": "ai",
    "ops mind": "ai",
    "run with ai": "ai",
    "analyze with ai": "ai",
    "ai troubleshooting": "ai",
    "explain": "ai",

    # File-system locations
    "root": "folder",
    "tmp": "folder",
    "etc": "folder",
    "var": "folder",
    "opt": "folder",
    "srv": "folder",
    "usr/local": "folder",
    "applications": "folder",
    "library": "folder",
    "users": "user",
    "volumes": "database",
    "claims (pvc)": "file",
    "volumes (pv)": "database",

    # Status
    "warnings only": "warning",
}

_LABEL_KEYS = sorted(LABEL_TO_ICON, key=len, reverse=True)


def split_icon_text(text: str):
    """Return (icon_name, cleaned_text) using semantic label matching."""
    if not isinstance(text, str):
        return None, text

    cleaned = text.strip()

    if not cleaned:
        return None, text

    lower = cleaned.lower()

    for key in _LABEL_KEYS:
        if (
            lower == key
            or lower.startswith(key + " ")
            or lower.startswith(key + "…")
            or lower.startswith(key + " (")
            or lower.startswith(key + ":")
        ):
            return LABEL_TO_ICON[key], cleaned

    # Fallback for short labels containing a known semantic phrase.
    for key in _LABEL_KEYS:
        if key in lower and len(cleaned) <= 60:
            return LABEL_TO_ICON[key], cleaned

    return None, cleaned


def apply_text_icon(
    widget,
    text: str,
    size: int = 16,
    color: Optional[str] = None,
):
    """Set widget text and attach a matching SVG icon."""
    name, cleaned = split_icon_text(text)

    if hasattr(widget, "setText"):
        widget.setText(cleaned)

    if name:
        set_icon(
            widget,
            name,
            color=color,
            size=size,
        )

    return cleaned


def icon_button(
    text: str,
    parent=None,
    size: int = 16,
):
    """Create a QPushButton with a semantic SVG icon."""
    button = QPushButton(parent) if parent is not None else QPushButton()

    apply_text_icon(
        button,
        text,
        size=size,
    )

    return button


def add_icon_tab(
    tab_widget,
    widget,
    text: str,
    size: int = 16,
):
    """Add a tab with an SVG icon when a semantic icon exists."""
    name, cleaned = split_icon_text(text)

    if name:
        try:
            widget.setProperty("kd_tab_icon", name)
            widget.setProperty("kd_tab_icon_size", int(size))
        except Exception:
            pass

        return tab_widget.addTab(
            widget,
            _tab_pixmap_icon(name, size),
            cleaned,
        )

    return tab_widget.addTab(
        widget,
        cleaned,
    )


# Compatibility aliases
get_icon = icon
load_icon = icon


__all__ = [
    "ICON_DIR",
    "LABEL_TO_ICON",
    "icon_path",
    "default_icon_color",
    "refresh_theme_icons",
    "icon",
    "set_icon",
    "icon_pixmap",
    "split_icon_text",
    "apply_text_icon",
    "icon_button",
    "add_icon_tab",
    "get_icon",
    "load_icon",
]
