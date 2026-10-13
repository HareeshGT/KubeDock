import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/HareeshGT/KubeDock.git"
APP = "KubeDock"
ICON = "KubeDock.ico"
BUILD_ID = "WINDOWS-PYAUDIO-FIX-2026-09-20-R3"
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

def run(cmd, cwd=None, check=True):
    print("\n>", " ".join(map(str, cmd)))
    return subprocess.run(cmd, cwd=cwd, check=check)


def python_version(python):
    return subprocess.check_output(
        [python, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()


def find_python():
    """Find a real 64-bit CPython supported by the Windows PyAudio build.

    This intentionally DOES NOT accept Python 3.14. The project requirements can
    otherwise cause pip to fall back to building PyAudio from source, which requires
    MSVC. We prefer the Python Launcher because it can select an exact installed
    minor version independently of PATH order.
    """
    py_launcher = shutil.which("py")
    candidates = []

    if py_launcher:
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

            if bits == "64" and v in {"3.13", "3.12", "3.11"}:
                return p
        except Exception:
            continue

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


def install_requirements(python, repo):
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


def verify(python):
    code = (
        "import importlib; "
        "mods=['PyQt5','paramiko','speech_recognition','pyaudio','PyInstaller']; "
        "[print('[OK]', m) or importlib.import_module(m) for m in mods]"
    )
    run([python, "-c", code])


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


def build(python, repo):
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


def main():
    print("==========================================")
    print(f"KubeDock Installer: {BUILD_ID}")
    print("==========================================")

    if platform.system() != "Windows":
        raise RuntimeError("This installer is Windows-only. Do not run it inside WSL.")

    ensure_admin()

    python = find_python()
    selected = python_version(python)
    print("\nFINAL PYTHON SELECTED:", python)
    print("FINAL PYTHON VERSION:", selected)
    if selected != RECOMMENDED_PYTHON:
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
        install_requirements(python, repo)
        verify(python)
        sync_webapp(repo)
        output = build(python, repo)

        destination = install_application(output)

        print("\n==========================================")
        print("KubeDock installed successfully!")
        print("==========================================")
        print("Installed to:", destination)
        print("Executable:", destination / "KubeDock.exe")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nERROR:", exc)
        input("\nPress Enter to exit...")
        sys.exit(1)
