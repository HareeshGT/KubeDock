import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote

try:
    from console_ui import ConsoleUI
except ModuleNotFoundError as import_error:
    if import_error.name != "console_ui":
        raise
    import importlib.util

    console_ui_path = Path(__file__).with_name("console_ui.py")
    console_ui_spec = importlib.util.spec_from_file_location("console_ui", console_ui_path)
    if console_ui_spec is None or console_ui_spec.loader is None:
        raise RuntimeError(f"Installer console UI helper is missing: {console_ui_path}")
    console_ui_module = importlib.util.module_from_spec(console_ui_spec)
    sys.modules["console_ui"] = console_ui_module
    console_ui_spec.loader.exec_module(console_ui_module)
    ConsoleUI = console_ui_module.ConsoleUI

UI = ConsoleUI()

REPO = "https://github.com/HareeshGT/KubeDock.git"
APP = "KubeDock"
ICON = "KubeDock.ico"
BUILD_ID = "WINDOWS-FAST-PREBUILT-RELEASE-2026-10-29-R1"
RECOMMENDED_PYTHON = "3.13"

LATEST_RELEASE_API = "https://api.github.com/repos/HareeshGT/KubeDock/releases/latest"
RELEASE_DOWNLOAD_BASE = "https://github.com/HareeshGT/KubeDock/releases/download"

# Small, Apache-2.0 licensed CPU model; immutable revision and verified SHA-256.
EMBEDDED_MODEL_URL = (
    "https://huggingface.co/bartowski/SmolLM2-360M-Instruct-GGUF/"
    "resolve/ab928a97ee49f3a015f35194879f68211291d6ca/"
    "SmolLM2-360M-Instruct-Q4_K_M.gguf"
)
EMBEDDED_MODEL_SHA256 = "2fa3f013dcdd7b99f9b237717fa0b12d75bbb89984cc1274be1471a465bac9c2"
EMBEDDED_MODEL_FILENAME = "SmolLM2-360M-Instruct-Q4_K_M.gguf"
EMBEDDED_MODEL_MAX_BYTES = 500 * 1024 * 1024


def offer_embedded_model_download() -> None:
    """Offer an optional verified local-model download after installation."""
    choice = os.environ.get("KUBEDOCK_DOWNLOAD_EMBEDDED_MODEL", "").strip().lower()
    if choice not in {"1", "0", "yes", "no", "true", "false"}:
        if not UI.interactive:
            UI.info("Skipping optional Embedded AI model download in non-interactive mode.")
            UI.info("Set KUBEDOCK_DOWNLOAD_EMBEDDED_MODEL=1 to opt in.")
            return
        try:
            choice = input(
                "\nDownload the optional Embedded AI model now (~271 MB)? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = ""
    if choice not in {"1", "yes", "y", "true"}:
        UI.info("Embedded AI model download skipped.")
        return

    data_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    model_dir = data_root / "KubeDock" / "models"
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
        destination = model_dir / EMBEDDED_MODEL_FILENAME
        if destination.is_file():
            existing_digest = hashlib.sha256()
            with destination.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    existing_digest.update(chunk)
            if existing_digest.hexdigest() == EMBEDDED_MODEL_SHA256:
                UI.success(f"Embedded AI model already installed: {destination}")
                return
            destination.unlink()
    except OSError as exc:
        UI.warning(f"Could not prepare the model cache: {exc}")
        UI.info("KubeDock itself is installed; skipping the optional model download.")
        return

    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    UI.phase(1, 1, "Downloading Embedded AI model", "CPU model · maximum 500 MiB")
    try:
        request = urllib.request.Request(
            EMBEDDED_MODEL_URL, headers={"User-Agent": "KubeDock-Installer"}
        )
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > EMBEDDED_MODEL_MAX_BYTES:
                    raise RuntimeError("The model download exceeded the 500 MiB limit.")
                output.write(chunk)
                digest.update(chunk)
                if response.headers.get("Content-Length"):
                    total = int(response.headers["Content-Length"])
                    UI.progress("Downloading model", min(100, received * 100 // max(total, 1)), received, total)
        if digest.hexdigest() != EMBEDDED_MODEL_SHA256:
            raise RuntimeError("Model SHA-256 verification failed; the file was rejected.")
        os.replace(temporary, destination)
        UI.clear_progress()
        UI.success(f"Verified Embedded AI model saved to: {destination}")
        UI.info("The model download is complete; the app must select Embedded Local Model to use it.")
    except Exception as exc:
        UI.clear_progress()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        UI.warning(f"Could not download the Embedded AI model: {exc}")
        UI.info("KubeDock itself is installed. You can retry by rerunning the installer and opting in.")


def get_latest_release_tag() -> str:
    """Resolve the latest published stable release once per installation."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "KubeDock-Installer",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or not tag.strip() or "/" in tag or "\\" in tag:
        raise RuntimeError("GitHub did not return a valid latest KubeDock release tag.")
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("GitHub's latest KubeDock release is not a published stable release.")
    return tag


def release_asset_url(tag: str, asset: str) -> str:
    """Build a download URL pinned to one immutable release tag."""
    if (
        not isinstance(asset, str)
        or not asset
        or asset in {".", ".."}
        or "/" in asset
        or "\\" in asset
    ):
        raise ValueError("Release asset must be a simple filename.")
    if not isinstance(tag, str) or not tag.strip() or "/" in tag or "\\" in tag:
        raise ValueError("Release tag is invalid.")
    return f"{RELEASE_DOWNLOAD_BASE}/{quote(tag, safe='')}/{quote(asset, safe='')}"





def is_admin():
    """Return True when the current Windows process has administrator rights."""
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Relaunch this installer with a UAC prompt when not already elevated."""
    import ctypes

    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        params = subprocess.list2cmdline([str(Path(__file__).resolve()), *sys.argv[1:]])

    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        params,
        str(Path.cwd()),
        1,
    )
    if rc <= 32:
        raise RuntimeError("Administrator elevation was cancelled or failed.")


def ensure_admin():
    if not is_admin():
        UI.warning("Administrator permissions are required to install KubeDock into Program Files.")
        UI.info("Requesting Windows UAC elevation...")
        relaunch_as_admin()
        raise SystemExit(0)

def choose_qwen_tts() -> bool:
    """Opt into the large local Qwen runtime via prompt or environment variable."""
    value = os.environ.get("KUBEDOCK_ENABLE_QWEN_TTS", "").strip().lower()
    if value in {"1", "y", "yes", "true", "on"}:
        return True
    if value in {"0", "n", "no", "false", "off"}:
        return False
    if not sys.stdin.isatty():
        UI.info("Non-interactive install: skipping optional Qwen3-TTS. Set KUBEDOCK_ENABLE_QWEN_TTS=1 to include it.")
        return False
    answer = input("Include local Qwen3-TTS voice support? This adds several GB to the app build [y/N]: ").strip().lower()
    return answer in {"y", "yes"}

def run(cmd, cwd=None, check=True):
    return UI.run(cmd, cwd=cwd, check=check)


def python_version(python):
    return subprocess.check_output(
        [python, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()


def find_python(include_qwen: bool = False):
    """Find a real 64-bit CPython supported by the Windows PyAudio build.

    This intentionally DOES NOT accept Python 3.14. The project requirements can
    otherwise cause pip to fall back to building PyAudio from source, which requires
    MSVC. We prefer the Python Launcher because it can select an exact installed
    minor version independently of PATH order.
    """
    py_launcher = shutil.which("py")
    candidates = []

    if py_launcher:
        # Qwen model inference currently uses the conservative Python 3.12 environment.
        if include_qwen:
            candidates.extend([(py_launcher, "-3.12-64"), (py_launcher, "-3.12")])
        else:
            # Explicit architecture suffix avoids accidentally selecting a 32-bit build.
            candidates.extend([(py_launcher, "-3.13-64"), (py_launcher, "-3.12-64"), (py_launcher, "-3.11-64")])
            # Fallback for launchers that do not accept the -64 suffix.
            candidates.extend([(py_launcher, "-3.13"), (py_launcher, "-3.12"), (py_launcher, "-3.11")])

    # VIRTUAL_ENV/PATH candidates are checked only AFTER exact launcher selections.
    # This prevents an activated Python 3.14 environment from being selected.
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(str(Path(venv) / "Scripts" / "python.exe"))

    for exe in ("python.exe", "python", "python3.exe", "python3"):
        found = shutil.which(exe)
        if found:
            candidates.append(found)

    seen = set()
    for candidate in candidates:
        try:
            if isinstance(candidate, tuple):
                launcher, selector = candidate
                p = subprocess.check_output(
                    [launcher, selector, "-c", "import sys; print(sys.executable)"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            else:
                p = str(candidate)

            if not p:
                continue
            p = str(Path(p).resolve())
            if p in seen:
                continue
            seen.add(p)
            if not Path(p).exists():
                continue

            # Never use the frozen installer executable as Python.
            if Path(p).resolve() == Path(sys.executable).resolve():
                continue

            v = python_version(p)
            bits = subprocess.check_output(
                [p, "-c", "import struct; print(struct.calcsize('P') * 8)"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()

            UI.info(f"Python candidate: {p} | version={v} | architecture={bits}-bit")

            if bits == "64" and ((include_qwen and v == "3.12") or (not include_qwen and v in {"3.13", "3.12", "3.11"})):
                return p
        except Exception:
            continue

    if include_qwen:
        raise RuntimeError(
            "Qwen3-TTS packaging requires an installed 64-bit Python 3.12. "
            "Install Python 3.12 and verify it with 'py --list', or rerun without Qwen support."
        )
    raise RuntimeError(
        "SUPPORTED PYTHON NOT FOUND. KubeDock Windows installation requires "
        "64-bit Python 3.13 (preferred), 3.12, or 3.11. Python 3.14 is intentionally "
        "rejected because PyAudio 0.2.14 does not provide a CPython 3.14 Windows wheel. "
        "Run 'py --list' to see installed versions."
    )


def get_branches():
    if not shutil.which("git"):
        raise RuntimeError("Git for Windows is required and was not found in PATH.")

    result = subprocess.run(
        ["git", "ls-remote", "--heads", REPO],
        capture_output=True,
        text=True,
        check=True,
    )
    branches = sorted(
        {
            line.split("refs/heads/", 1)[1].strip()
            for line in result.stdout.splitlines()
            if "refs/heads/" in line
        }
    )
    if not branches:
        raise RuntimeError("No Git branches were found in the repository.")
    return branches


def choose_branch(branches):
    UI.phase(4, 8, "Selecting source branch", "Choose the Git branch to package.")
    UI.info("Available Git branches:")
    for i, branch in enumerate(branches, 1):
        UI.menu_item(i, branch)

    while True:
        try:
            number = int(input(f"\nSelect branch [1-{len(branches)}]: "))
            if 1 <= number <= len(branches):
                return branches[number - 1]
        except ValueError:
            pass
        UI.warning("Invalid selection. Enter one of the listed numbers.")


def install_requirements(python, repo, include_qwen: bool = False):
    req = Path(repo) / "requirements.txt"
    if not req.exists():
        raise RuntimeError(f"requirements.txt not found: {req}")

    v = python_version(python)
    bits = subprocess.check_output(
        [python, "-c", "import struct; print(struct.calcsize('P') * 8)"],
        text=True,
    ).strip()
    UI.success(f"Selected Python {v} ({bits}-bit): {python}")

    # Absolute guard: never allow a future/stale build to run pip under 3.14+.
    if v not in {"3.13", "3.12", "3.11"} or bits != "64":
        raise RuntimeError(
            f"Wrong Python selected: {python} ({v}, {bits}-bit). "
            "This installer only permits 64-bit Python 3.13/3.12/3.11."
        )

    # Exclude PyAudio from the project requirements. It is installed separately
    # with --only-binary so pip can NEVER silently compile it from source.
    lines = req.read_text(encoding="utf-8").splitlines()
    filtered = [
        line for line in lines
        if not line.strip().lower().startswith(("pyaudio", "pyaudiowpatch"))
    ]
    generated = Path(repo) / "requirements.windows.generated.txt"
    generated.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    UI.phase(5, 8, "Installing Python dependencies", "Installing core, build and optional feature requirements.")
    UI.info(f"Installing Windows requirements from: {generated}")
    UI.success("PyAudio source-build entries excluded; a binary wheel is installed separately.")
    run([python, "-m", "pip", "install", "-r", str(generated)])

    # PyInstaller is needed only for source packaging, not as a KubeDock runtime dependency.
    build_req = Path(repo) / "requirements-build.txt"
    if build_req.is_file():
        run([python, "-m", "pip", "install", "-r", str(build_req)])
    else:
        run([python, "-m", "pip", "install", "PyInstaller>=6.0"])

    # Keep the microphone packages optional, but never let pip compile PyAudio.
    voice_req = Path(repo) / "requirements-voice.txt"
    if voice_req.is_file():
        voice_lines = voice_req.read_text(encoding="utf-8").splitlines()
        filtered_voice = [
            line for line in voice_lines
            if not line.strip().lower().startswith(("pyaudio", "pyaudiowpatch"))
        ]
        generated_voice = Path(repo) / "requirements.windows.voice.generated.txt"
        generated_voice.write_text("\n".join(filtered_voice) + "\n", encoding="utf-8")
        run([python, "-m", "pip", "install", "-r", str(generated_voice)])

    if os.environ.get("KUBEDOCK_ENABLE_WEBENGINE", "").strip().lower() in {"1", "true", "yes", "on"}:
        editor_req = Path(repo) / "requirements-editor.txt"
        if not editor_req.is_file():
            raise RuntimeError("KUBEDOCK_ENABLE_WEBENGINE was set but requirements-editor.txt is missing.")
        UI.info("Installing optional Qt WebEngine/Monaco editor. This increases package size.")
        run([python, "-m", "pip", "install", "-r", str(editor_req)])
    else:
        UI.info("Skipping optional Qt WebEngine; the built-in editor remains available.")

    UI.info("Installing PyAudio as a binary wheel only...")
    run([python, "-m", "pip", "install", "--only-binary=:all:", "PyAudio==0.2.14"])

    if include_qwen:
        qwen_req = Path(repo) / "requirements-qwen-tts.txt"
        if not qwen_req.is_file():
            raise RuntimeError("The selected branch does not include requirements-qwen-tts.txt.")
        UI.warning("Installing optional local Qwen3-TTS runtime. This can download several GB of Python packages.")
        run([python, "-m", "pip", "install", "-r", str(qwen_req)])
        # Fail before PyInstaller if the Qwen package or the pinned Transformers
        # compatibility stack cannot actually import in this Python environment.
        run([python, "-c", "import torch; from qwen_tts import Qwen3TTSModel; print('[OK] Qwen3-TTS import; PyTorch', torch.__version__)"])


def verify(python, include_qwen: bool = False):
    code = (
        "import importlib; "
        "mods=['PyQt5','paramiko','speech_recognition','pyaudio','pyttsx3','PyInstaller']; "
        "[print('[OK]', m) or importlib.import_module(m) for m in mods]"
    )
    run([python, "-c", code])
    if include_qwen:
        run([python, "-c", "import torch; from qwen_tts import Qwen3TTSModel; print('[OK] Qwen3-TTS runtime; PyTorch', torch.__version__)"])


def sync_webapp(repo):
    """Generate webapp/static_content.py directly; avoids fragile nested quoting."""
    source = Path(repo) / "webapp" / "static" / "index.html"
    target = Path(repo) / "webapp" / "static_content.py"

    if not source.exists():
        raise RuntimeError(f"Web UI source not found: {source}")

    import base64
    import gzip

    encoded = base64.b64encode(gzip.compress(source.read_bytes(), 9)).decode("ascii")
    target.write_text(
        "# Auto-generated fallback.\n"
        "import base64\n"
        "import gzip\n\n"
        "INDEX_HTML = gzip.decompress(base64.b64decode(\n"
        f"    {encoded!r}\n"
        ")).decode(\"utf-8\")\n",
        encoding="utf-8",
    )
    UI.success("Web UI fallback synchronized")


def build(python, repo, include_qwen: bool = False):
    shutil.rmtree(Path(repo) / "build", ignore_errors=True)
    shutil.rmtree(Path(repo) / "dist" / APP, ignore_errors=True)
    (Path(repo) / f"{APP}.spec").unlink(missing_ok=True)

    cmd = [
        python,
        "-m",
        "PyInstaller",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        APP,
        "--hidden-import=paramiko",
        "--collect-all=paramiko",
        "--hidden-import=webapp",
        "--hidden-import=webapp.server",
        "--hidden-import=ai_assist",
        "--hidden-import=k8s_ai_ops",
        "--hidden-import=speech_recognition",
        "--hidden-import=pyaudio",
        "--add-data",
        "assets;assets",
        "--add-data",
        "webapp/static;webapp/static",
        "main.py",
    ]

    if include_qwen:
        cmd[-1:-1] = [
            "--collect-all=qwen_tts",
            "--collect-all=torch",
            "--collect-all=torchaudio",
            "--collect-all=transformers",
            "--collect-all=accelerate",
            "--collect-all=tokenizers",
            "--collect-all=librosa",
            "--collect-all=soundfile",
            "--collect-all=onnxruntime",
            "--collect-all=einops",
            "--collect-all=sox",
        ]

    icon = Path(repo) / ICON
    if icon.exists():
        cmd[3:3] = ["--icon", str(icon)]

    run(cmd, cwd=repo)

    output = Path(repo) / "dist" / APP
    if not output.is_dir():
        raise RuntimeError("PyInstaller did not create dist\\KubeDock")
    return output


def stop_running_kubedeck():
    """Stop an existing KubeDock process before replacing its files."""
    if platform.system() != "Windows":
        return

    subprocess.run(
        ["taskkill", "/F", "/IM", "KubeDock.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def remove_windows_directory(path):
    """Remove a Windows directory robustly, including read-only leftovers."""
    path = Path(path)
    if not path.exists():
        return

    stop_running_kubedeck()

    # Clear common read-only/system/hidden attributes. Using the wildcard form
    # avoids assumptions about localized Windows group names.
    subprocess.run(
        ["attrib", "-R", "-S", "-H", str(path), "/S", "/D"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    # Ensure the local Administrators group can remove/update a previous install.
    subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-5-32-544:F", "/T", "/C"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    result = subprocess.run(
        ["cmd", "/c", "rmdir", "/S", "/Q", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    if path.exists():
        detail = (result.stderr or result.stdout or "").strip()
        raise PermissionError(
            f"Could not remove previous installation directory: {path}. "
            f"{detail or 'The directory may still be in use or blocked by Windows security policy.'}"
        )


def copy_windows_bundle(source, destination):
    """Copy the PyInstaller onedir bundle using Windows robocopy."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "robocopy",
            str(source),
            str(destination),
            "/E",
            "/COPY:DAT",
            "/DCOPY:DAT",
            "/R:2",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    # Robocopy uses 0-7 for success/non-fatal differences; 8+ is failure.
    if result.returncode >= 8:
        raise RuntimeError(
            "Robocopy failed while installing KubeDock "
            f"(exit code {result.returncode}).\n{result.stdout}\n{result.stderr}"
        )


def install_application(output):
    """Install into Program Files, with a per-user fallback if Windows blocks it."""
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / APP
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / APP

    # First choice: preserve the original installer behavior.
    try:
        remove_windows_directory(program_files)
        copy_windows_bundle(output, program_files)
        return program_files
    except (PermissionError, OSError, RuntimeError) as exc:
        print("\n[WARN] Program Files installation was blocked:")
        print("      ", exc)
        print("[INFO] Falling back to the current user's local application directory.")

    remove_windows_directory(local_appdata)
    copy_windows_bundle(output, local_appdata)
    return local_appdata



def install_prebuilt_release() -> Path:
    """Download and install the ready-to-run Windows bundle without pip/PyInstaller."""
    asset = "KubeDock-windows-x86_64.zip"
    temp_dir = Path(tempfile.mkdtemp(prefix="KubeDockFastInstall-"))
    UI.phase(1, 4, "Checking release", "Resolving the latest stable version for Windows.")
    archive = temp_dir / asset
    try:
        # Resolve latest once so the binary and SHA256SUMS always come from
        # exactly the same immutable release, rather than an outdated moving tag.
        release_tag = get_latest_release_tag()
        url = release_asset_url(release_tag, asset)
        archive.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "KubeDock-Installer"})
        UI.phase(2, 4, "Downloading KubeDock release", f"Release: {release_tag} · Windows x86-64 bundle")
        with urllib.request.urlopen(request, timeout=45) as response, archive.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0") or 0)
            downloaded = 0
            last_percent = -1
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent >= last_percent + 10:
                        UI.progress("Downloading", percent, downloaded, total)
                        last_percent = percent
                elif downloaded // (50 * 1024 * 1024) != (downloaded - len(chunk)) // (50 * 1024 * 1024):
                    UI.info(f"Downloaded {downloaded / (1024 * 1024):.0f} MiB")
        UI.clear_progress()
        UI.success("Release bundle downloaded")
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError("The downloaded KubeDock bundle is empty.")

        checksum_url = release_asset_url(release_tag, "SHA256SUMS")
        checksum_request = urllib.request.Request(checksum_url, headers={"User-Agent": "KubeDock-Installer"})
        with urllib.request.urlopen(checksum_request, timeout=30) as response:
            checksum_lines = response.read().decode("utf-8").splitlines()
        expected_hash = next(
            (line.split()[0].lower() for line in checksum_lines
             if len(line.split()) >= 2 and line.split()[1].lstrip("*") == asset),
            None,
        )
        if not expected_hash or len(expected_hash) != 64:
            raise RuntimeError("The release checksum file has no valid hash for the Windows bundle.")
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise RuntimeError("SHA-256 verification failed for the downloaded Windows bundle.")
        UI.phase(3, 4, "Verifying package integrity", "Checking the downloaded archive against the release SHA-256 manifest.")
        UI.success("SHA-256 checksum verified")

        extract_root = temp_dir / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)
        root_resolved = extract_root.resolve()
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                destination = (extract_root / member.filename).resolve()
                try:
                    inside_root = os.path.commonpath([str(root_resolved), str(destination)]) == str(root_resolved)
                except ValueError:
                    inside_root = False
                if not inside_root:
                    raise RuntimeError("The prebuilt archive contains an unsafe path.")
            package.extractall(extract_root)

        output_dir = extract_root / APP
        executable = output_dir / "KubeDock.exe"
        if not executable.is_file():
            raise RuntimeError("The downloaded archive does not contain KubeDock.exe.")
        metadata_path = output_dir / "KubeDock-BUILD-METADATA.json"
        if metadata_path.is_file():
            UI.info("Build metadata:")
            UI._write(metadata_path.read_text(encoding="utf-8").rstrip())

        UI.phase(4, 4, "Installing application", "Placing the verified app bundle in its destination.")
        destination = install_application(output_dir)
        UI.complete("KubeDock installed from the verified prebuilt bundle")
        UI.success(f"Installed to: {destination}")
        UI.info(f"Executable: {destination / 'KubeDock.exe'}")
        UI.info("Optional Qwen3-TTS is not included in the fast bundle.")
        offer_embedded_model_download()
        return destination
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    UI.banner(f"Windows installer · {BUILD_ID}")
    UI.phase(1, 8, "Checking platform and permissions", "Confirming Windows support and Administrator access.")

    if platform.system() != "Windows":
        raise RuntimeError("This installer is Windows-only. Do not run it inside WSL.")

    ensure_admin()
    install_mode = os.environ.get("KUBEDOCK_INSTALL_MODE", "auto").strip().lower()
    if install_mode not in {"auto", "release", "source"}:
        raise RuntimeError("KUBEDOCK_INSTALL_MODE must be auto, release, or source.")

    if install_mode != "source":
        try:
            install_prebuilt_release()
            return
        except Exception as exc:
            if install_mode == "release":
                raise RuntimeError(f"Prebuilt installation failed: {exc}") from exc
            UI.warning(f"Prebuilt bundle unavailable; falling back to source build: {exc}")
            UI.info("The source path installs Python requirements and runs PyInstaller.")

    UI.phase(2, 8, "Selecting install options", "Choose the lightweight installation or optional local Qwen3-TTS runtime.")
    include_qwen = choose_qwen_tts()

    UI.phase(3, 8, "Detecting Python runtime", "Selecting a supported 64-bit Python version.")
    python = find_python(include_qwen=include_qwen)
    selected = python_version(python)
    UI.success(f"Python runtime selected: {selected} · {python}")
    expected_python = "3.12" if include_qwen else RECOMMENDED_PYTHON
    if selected != expected_python:
        if include_qwen:
            raise RuntimeError(
                f"The Qwen-enabled installer selected Python {selected}. Local Qwen3-TTS builds require 64-bit Python 3.12. "
                "Install/use Python 3.12 or rerun without Qwen support."
            )
        raise RuntimeError(
            f"The installer selected Python {selected}. This Windows installer requires "
            "Python 3.13 to guarantee the PyAudio 0.2.14 wheel. Please install/use 64-bit Python 3.13."
        )
    subprocess.run([python, "--version"], check=True)

    UI.info("Connecting to GitHub to retrieve available branches...")
    branches = get_branches()
    branch = choose_branch(branches)
    UI.success(f"Source branch selected: {branch}")

    root = Path(tempfile.mkdtemp(prefix="KubeDeckInstaller-"))
    repo = root / "VM-Visualizer"

    try:
        run(["git", "clone", "--branch", branch, "--single-branch", REPO, str(repo)])
        install_requirements(python, repo, include_qwen=include_qwen)
        UI.phase(6, 8, "Verifying application", "Checking required imports and synchronizing the embedded web UI.")
        verify(python, include_qwen=include_qwen)
        sync_webapp(repo)
        UI.phase(7, 8, "Building KubeDock", "Packaging the desktop app. This can take several minutes.")
        output = build(python, repo, include_qwen=include_qwen)

        UI.phase(8, 8, "Installing application", "Copying the completed build into its destination.")
        destination = install_application(output)

        UI.complete("KubeDock installed successfully")
        UI.success(f"Installed to: {destination}")
        UI.info(f"Executable: {destination / 'KubeDock.exe'}")
        UI.info("Qwen3-TTS local voice included: " + ("YES" if include_qwen else "NO (default lightweight build)"))
        if include_qwen:
            UI.info("Qwen model weights download on first launch; model weights are not embedded in the installer.")
        offer_embedded_model_download()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        UI.failed(str(exc))
        if sys.stdin.isatty():
            input("\nPress Enter to exit...")
        sys.exit(1)
