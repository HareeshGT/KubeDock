"""utils.py — File-type helpers, size formatting, and recent-instances CSV."""

import csv
import mimetypes
import os
import stat
import shlex
from PyQt5.QtGui import QTextCursor, QFont, QFontDatabase, QFontInfo, QTextCharFormat
from workers import managed_exec_command
# ─── Paths ───────────────────────────────────────────────────
APP_DIR     = os.path.join(os.path.expanduser("~"), ".vm_visualizer")
RECENT_FILE = os.path.join(APP_DIR, "recent.csv")  # legacy migration source
os.makedirs(APP_DIR, exist_ok=True)

from db import connect, initialize

RECENT_MAX    = 50
RECENT_FIELDS = ["host", "port", "user", "pem", "alias", "protocol"]   # alias is 5th column


# ─── Recent instances ─────────────────────────────────────────
def _migrate_recent_csv():
    """Import legacy recent.csv once; leave the original file as a backup."""
    initialize()
    with connect() as db:
        done = db.execute("SELECT value_json FROM app_settings WHERE setting_key='migration.recent_csv'").fetchone()
        if done:
            return
        if os.path.exists(RECENT_FILE):
            try:
                with open(RECENT_FILE, newline="", encoding="utf-8") as f:
                    legacy_rows = list(csv.reader(f))
            except (OSError, csv.Error):
                # Do not mark complete on read failure; a later run can retry.
                return
            # recent.csv is newest-first, but recent_instances is read back
            # ordered by (last_used DESC, id DESC) and every imported row gets
            # the same last_used.  Insert oldest-first so ids grow with
            # recency; otherwise the list is reversed and, past RECENT_MAX,
            # the newest entries are the ones truncated/pruned.
            for row in reversed(legacy_rows):
                if len(row) < 4:
                    continue
                host, port, user, pem = row[:4]
                alias = row[4].strip() if len(row) > 4 else ""
                protocol = (row[5].strip().lower() if len(row) > 5 else "ssh") or "ssh"
                db.execute("""INSERT OR IGNORE INTO recent_instances
                    (host,port,user,pem,alias,protocol) VALUES(?,?,?,?,?,?)""",
                    (host, port or "22", user, pem, alias, protocol))
        db.execute("INSERT OR REPLACE INTO app_settings(setting_key,value_json) VALUES('migration.recent_csv','true')")


def load_recent_instances() -> list:
    _migrate_recent_csv()
    with connect() as db:
        rows = db.execute("""SELECT host,port,user,pem,alias,protocol
            FROM recent_instances ORDER BY last_used DESC, id DESC LIMIT ?""", (RECENT_MAX,)).fetchall()
    return [dict(row) for row in rows]


def save_recent_instances(instances: list):
    _migrate_recent_csv()
    try:
        with connect() as db:
            db.execute("DELETE FROM recent_instances")
            for inst in instances[:RECENT_MAX]:
                db.execute("""INSERT OR REPLACE INTO recent_instances
                    (host,port,user,pem,alias,protocol,last_used)
                    VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (inst.get("host", ""), str(inst.get("port", "22")),
                     inst.get("user", ""), inst.get("pem", ""),
                     inst.get("alias", ""), inst.get("protocol", "ssh") or "ssh"))
    except Exception:
        pass


def add_recent_instance(host: str, port: int, user: str, pem: str, alias: str = "", protocol: str = "ssh"):
    _migrate_recent_csv()
    try:
        with connect() as db:
            db.execute("""INSERT INTO recent_instances(host,port,user,pem,alias,protocol,last_used)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(host,port,user) DO UPDATE SET
                pem=excluded.pem, alias=excluded.alias, protocol=excluded.protocol,
                last_used=CURRENT_TIMESTAMP""",
                (host, str(port), user, pem or "", alias or "", protocol or "ssh"))
            db.execute("""DELETE FROM recent_instances WHERE id NOT IN
                (SELECT id FROM recent_instances ORDER BY last_used DESC,id DESC LIMIT ?)""", (RECENT_MAX,))
    except Exception:
        pass


# ─── File type helpers ────────────────────────────────────────
FILE_ICONS = {
    "folder": "📁", "image": "🖼️", "video": "🎬", "audio": "🎵",
    "text": "📄", "code": "💻", "pdf": "📕", "archive": "📦",
    "link": "🔗", "exec": "⚙️", "unknown": "📎", "key": "🔑",
}

# Explicit media extensions keep audio/video classification reliable even
# when the host's mimetypes database is incomplete (common on minimal Linux
# installs and some Windows Python distributions).
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".amr", ".mid", ".midi", ".mka",
}

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".mpeg",
    ".mpg", ".m2ts", ".ts", ".3gp", ".ogv",
}

CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".sh", ".bash", ".yaml", ".yml", ".json", ".toml",
    ".rs", ".go", ".c", ".cpp", ".h", ".java", ".rb",
    ".php", ".swift", ".kt", ".sql", ".xml", ".md",
    ".env", ".cfg", ".ini", ".conf", ".out", ".pem", ".privkey",
}


def classify(name: str, is_dir: bool, mode: int) -> str:
    if is_dir:
        return "folder"
    if stat.S_ISLNK(mode):
        return "link"
    ext = os.path.splitext(name)[1].lower()
    if ext in {".pem", ".privkey"}:
        return "key"
    if ext in {".out", ".log", ".txt"}:
        return "text"
    if ext in CODE_EXTS:
        return "code"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    mime, _ = mimetypes.guess_type(name)
    if mime:
        if mime.startswith("image"):   return "image"
        if mime.startswith("video"):   return "video"
        if mime.startswith("audio"):   return "audio"
        if mime == "application/pdf":  return "pdf"
        if mime.startswith("text"):    return "text"
    if ext in {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}:
        return "archive"
    if mode and (mode & 0o111):
        return "exec"
    return "unknown"


def icon_for(kind: str) -> str:
    return FILE_ICONS.get(kind, FILE_ICONS["unknown"])


def size_fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


# ─── Verified fixed-pitch font ─────────────────────────────────
# The app previously just asked for QFont("Cascadia Code", N) everywhere a
# terminal-ish/code widget needed a monospace look. Cascadia Code ships with
# Windows Terminal but isn't installed by default on macOS or most Linux
# distros — when it's missing, Qt silently substitutes *some* font by name
# similarity, and that substitute is not guaranteed to be fixed-pitch. Since
# kubectl/ls/ps align their columns purely with padding spaces, a proportional
# substitute font makes every row's columns land at a different pixel offset
# depending on how wide that row's text happens to render — the "zigzag"
# misalignment. monospace_font() picks (and verifies via QFontInfo) an actual
# fixed-pitch font installed on the current machine, falling back to Qt's own
# guaranteed-monospace system font as a last resort.
_MONOSPACE_CANDIDATES = [
    "Cascadia Code", "Cascadia Mono", "SF Mono", "Menlo",
    "Consolas", "Fira Code", "DejaVu Sans Mono", "Courier New",
]

_cached_monospace_family = None


def _pick_monospace_family() -> str:
    global _cached_monospace_family
    if _cached_monospace_family is not None:
        return _cached_monospace_family
    db = QFontDatabase()
    for name in _MONOSPACE_CANDIDATES:
        if name in db.families() and QFontInfo(QFont(name)).fixedPitch():
            _cached_monospace_family = name
            return name
    # Last resort: whatever Qt considers the platform's fixed-pitch font —
    # unfamiliar name, but guaranteed monospace.
    _cached_monospace_family = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
    return _cached_monospace_family


def monospace_font(point_size: int = 11, bold: bool = False) -> QFont:
    """A QFont that's actually verified fixed-pitch on this machine."""
    font = QFont(_pick_monospace_family(), point_size)
    font.setStyleHint(QFont.Monospace)
    font.setFixedPitch(True)
    if bold:
        font.setBold(True)
    return font


# ─── Terminal-style QTextEdit helpers ─────────────────────────
# These helpers force the verified monospace font (above) directly onto the
# inserted character format, rather than relying on the widget's own font or
# the app style sheet to resolve correctly — so terminal-style output stays
# column-aligned even if something upstream substitutes a proportional font.
def _terminal_font_css(point_size: int = 11) -> str:
    fam = _pick_monospace_family()
    return f"font-family:'{fam}'; font-size:{point_size}pt; white-space:pre;"


def html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


def append_terminal_html(widget, html: str, point_size: int = 11):
    """Append raw HTML (e.g. a colored '$ cmd' echo line) to a terminal QTextEdit."""
    cursor = widget.textCursor()
    cursor.movePosition(cursor.End)
    widget.setTextCursor(cursor)
    css = _terminal_font_css(point_size)
    widget.insertHtml(f'<div style="{css}">{html}</div><br>')


def append_terminal_text(widget, text: str, point_size: int = 11):
    """Append terminal text preserving line breaks and column alignment."""
    cursor = widget.textCursor()
    cursor.movePosition(QTextCursor.End)
    widget.setTextCursor(cursor)

    fmt = QTextCharFormat()
    fmt.setFont(monospace_font(point_size))
    cursor.setCharFormat(fmt)
    cursor.insertText(text, fmt)

    widget.setTextCursor(cursor)
    widget.ensureCursorVisible()

# Default path of the CSV on the connected VM describing tunnelable
# services. Each row:
#   service_name, local_port, container_port, namespace
# container_port is optional — if omitted, it defaults to local_port
# (i.e. 'kubectl port-forward svc/name PORT:PORT'). A header row is
# optional and auto-detected (skipped if the 2nd column on the first row
# isn't numeric). Adjust this path to your actual location.
REMOTE_TUNNEL_CSV_PATH = "~/.tunnel/tunnel_services.csv"


def remote_shell_path(path: str) -> str:
    """Return `path` as a shell-safe word for a command run on the remote VM.

    The tunnel CSV lives on the VM, so '~' must expand to the *remote*
    user's $HOME (the local Path.expanduser() would give the wrong home).
    shlex.quote() alone suppresses tilde expansion, so a leading '~' or
    '~/...' is rewritten to "$HOME" (double-quoted, still expanded by the
    remote shell) followed by the quoted remainder. Absolute and relative
    paths are only quoted, so their behavior is unchanged.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    if path.startswith("$HOME/"):
        return '"$HOME"/' + shlex.quote(path[6:])
    return shlex.quote(path)


def load_tunnel_services(ssh, path: str = None) -> list:
    """Load service tunnel definitions from a CSV file on the connected VM,
    read over the existing SSH connection (not the local filesystem).

    Expected columns per row: service_name, local_port, container_port,
    namespace. The 4th column (namespace) is optional; if a row only has 3
    columns, namespace is left blank.

    Returns a list of dicts:
        {"name": str, "port": int, "namespace": str, "container_port": int}
    Malformed rows (missing name, non-numeric ports) are skipped.
    A missing/unreadable file, or no ssh connection, simply yields an
    empty list rather than raising.
    """
    path = path or REMOTE_TUNNEL_CSV_PATH
    services = []
    if ssh is None:
        return services
    try:
        with managed_exec_command(ssh, "cat {} 2>/dev/null".format(remote_shell_path(path))) as (_stdin, stdout, _stderr):
            raw = stdout.read().decode(errors="replace")
    except Exception:
        return services

    if not raw.strip():
        return services

    rows = list(csv.reader(raw.splitlines()))
    for i, row in enumerate(rows):
        row = [c.strip() for c in row]
        if len(row) < 3 or not row[0]:
            continue
        name, port_s, container_port_s = row[0], row[1], row[2]
        if i == 0 and not port_s.isdigit():
            continue  # header row
        if not port_s.isdigit():
            continue
        if not container_port_s.isdigit():
            continue
        namespace = row[3] if len(row) > 3 else ""
        services.append({
            "name": name,
            "port": int(port_s),
            "namespace": namespace,
            "container_port": int(container_port_s),
        })
    return services