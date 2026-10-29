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

REPO = "https://github.com/HareeshGT/KubeDock.git"
APP = "KubeDock"
ICON = "KubeDock.ico"
BUILD_ID = "WINDOWS-FAST-PREBUILT-RELEASE-2026-10-29-R1"
RECOMMENDED_PYTHON = "3.13"



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
        print("\nAdministrator permissions are required to install KubeDock into Program Files.")
        print("Requesting Windows UAC elevation...\n")
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
        print("Non-interactive install: skipping optional Qwen3-TTS. Set KUBEDOCK_ENABLE_QWEN_TTS=1 to include it.")
        return False
    answer = input("Include local Qwen3-TTS voice support? This adds several GB to the app build [y/N]: ").strip().lower()
    return answer in {"y", "yes"}

def run(cmd, cwd=None, check=True):
    print("\n>", " ".join(map(str, cmd)))
    return subprocess.run(cmd, cwd=cwd, check=check)


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

            print(f"Candidate Python: {p} | version={v} | bits={bits}")

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
    print("\n==========================================")
    print("KubeDock GitHub Branch Selection")
    print("==========================================\n")
    for i, branch in enumerate(branches, 1):
        print(f" [{i}] {branch}")

    while True:
        try:
            number = int(input(f"\nSelect branch [1-{len(branches)}]: "))
            if 1 <= number <= len(branches):
                return branches[number - 1]
        except ValueError:
            pass
        print("Invalid selection.")


def install_requirements(python, repo, include_qwen: bool = False):
    req = Path(repo) / "requirements.txt"
    if not req.exists():
        raise RuntimeError(f"requirements.txt not found: {req}")

    v = python_version(python)
    bits = subprocess.check_output(
        [python, "-c", "import struct; print(struct.calcsize('P') * 8)"],
        text=True,
    ).strip()
    print(f"\nSELECTED PYTHON: {python}")
    print(f"PYTHON VERSION: {v}")
    print(f"PYTHON ARCH: {bits}-bit")

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

    print(f"\nInstalling Windows requirements from: {generated}")
    print("PyAudio entries removed from generated requirements: YES")
    run([python, "-m", "pip", "install", "-r", str(generated)])

    print("\nInstalling PyAudio as a binary wheel only...")
    run([python, "-m", "pip", "install", "--only-binary=:all:", "PyAudio==0.2.14"])

    if include_qwen:
        qwen_req = Path(repo) / "requirements-qwen-tts.txt"
        if not qwen_req.is_file():
            raise RuntimeError("The selected branch does not include requirements-qwen-tts.txt.")
        print("\nInstalling optional local Qwen3-TTS runtime. This can download several GB of Python packages.")
        run([python, "-m", "pip", "install", "-r", str(qwen_req)])
        # Fail before PyInstaller if the Qwen package or the pinned Transformers
        # compatibility stack cannot actually import in this Python environment.
        run([python, "-c", "import torch; from qwen_tts import Qwen3TTSModel; print('[OK] Qwen3-TTS import; PyTorch', torch.__version__)"])


def verify(python, include_qwen: bool = False):
    code = (
        "import importlib; "
        "mods=['PyQt5','paramiko','speech_recognition','pyaudio','PyInstaller']; "
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
    print("[OK] Web UI fallback synchronized")


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
    url = f"https://github.com/HareeshGT/KubeDock/releases/download/fast-installer/{asset}"
    temp_dir = Path(tempfile.mkdtemp(prefix="KubeDockFastInstall-"))
    archive = temp_dir / asset
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "KubeDock-Installer"})
        print(f"Downloading prebuilt bundle: {url}")
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
                        print(f"  Download: {percent}%")
                        last_percent = percent
                elif downloaded // (50 * 1024 * 1024) != (downloaded - len(chunk)) // (50 * 1024 * 1024):
                    print(f"  Downloaded {downloaded / (1024 * 1024):.0f} MiB")
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError("The downloaded KubeDock bundle is empty.")

        extract_root = temp_dir / "extracted"
        extract_root.mkdir()
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
            print("\\nPrebuilt bundle metadata:")
            print(metadata_path.read_text(encoding="utf-8"))

        destination = install_application(output_dir)
        print("\\n==========================================")
        print("KubeDock installed from prebuilt bundle!")
        print("==========================================")
        print("Installed to:", destination)
        print("Executable:", destination / "KubeDock.exe")
        print("Optional Qwen3-TTS is not included in the fast bundle.")
        return destination
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    print("==========================================")
    print(f"KubeDock Installer: {BUILD_ID}")
    print("==========================================")

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
            print("\\n[INFO] Prebuilt bundle unavailable; falling back to the source build.")
            print("       The source path installs Python requirements and runs PyInstaller.")

    include_qwen = choose_qwen_tts()

    python = find_python(include_qwen=include_qwen)
    selected = python_version(python)
    print("\nFINAL PYTHON SELECTED:", python)
    print("FINAL PYTHON VERSION:", selected)
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

    branches = get_branches()
    branch = choose_branch(branches)

    root = Path(tempfile.mkdtemp(prefix="KubeDeckInstaller-"))
    repo = root / "VM-Visualizer"

    try:
        run(["git", "clone", "--branch", branch, "--single-branch", REPO, str(repo)])
        install_requirements(python, repo, include_qwen=include_qwen)
        verify(python, include_qwen=include_qwen)
        sync_webapp(repo)
        output = build(python, repo, include_qwen=include_qwen)

        destination = install_application(output)

        print("\n==========================================")
        print("KubeDock installed successfully!")
        print("==========================================")
        print("Installed to:", destination)
        print("Executable:", destination / "KubeDock.exe")
        print("Qwen3-TTS local voice included:", "YES" if include_qwen else "NO (default lightweight build)")
        if include_qwen:
            print("Note: Qwen model weights download on first launch; model weights are not embedded in the installer.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nERROR:", exc)
        input("\nPress Enter to exit...")
        sys.exit(1)
