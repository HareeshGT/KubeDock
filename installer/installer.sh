#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/HareeshGT/KubeDock.git"
DIR="VM-Visualizer"

OS="$(uname -s)"

echo "Detected OS: $OS"

# --------------------------------------------------
# Privilege helper
# --------------------------------------------------
#
# Cloud/VM instances and containers are frequently accessed (and this
# script frequently run) as root already — a common case on freshly
# provisioned EC2/GCE/Azure instances and inside Docker images — and
# many of those minimal images don't even ship a `sudo` binary. Calling
# bare `sudo` there fails with "command not found" and aborts the whole
# install. $SUDO resolves to nothing when already root, to `sudo` when
# available, and only errors out (with a clear message) if elevated
# privileges are genuinely required and there is no way to get them.
if [ "$(id -u)" = "0" ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
  echo "NOTE: not running as root and 'sudo' was not found."
  echo "      Package-manager steps that need elevated privileges may fail below."
fi

# --------------------------------------------------
# macOS .pkg execution mode
# --------------------------------------------------
#
# macOS Installer runs package scripts as root. KubeDock's normal
# installer intentionally uses the logged-in user's Homebrew toolchain.
# In .pkg mode we therefore keep the overall install elevated (so the
# final application can be copied into /Applications), while executing
# Homebrew commands as the active GUI user.
KUBEDECK_PKG_MODE="${KUBEDECK_PKG_MODE:-0}"

if [[ "$OS" == "Darwin" && "$KUBEDECK_PKG_MODE" == "1" && "$(id -u)" = "0" ]]; then
  KUBEDECK_INSTALL_USER="$(/usr/bin/stat -f '%Su' /dev/console 2>/dev/null || true)"

  if [ -z "$KUBEDECK_INSTALL_USER" ] || [ "$KUBEDECK_INSTALL_USER" = "root" ] || [ "$KUBEDECK_INSTALL_USER" = "loginwindow" ]; then
    echo
    echo "ERROR: Could not determine the logged-in macOS user."
    echo "Run the KubeDock package from an active macOS GUI session."
    exit 1
  fi

  KUBEDECK_INSTALL_HOME="$(
    /usr/bin/dscl . -read "/Users/$KUBEDECK_INSTALL_USER" NFSHomeDirectory 2>/dev/null |
      awk '{print $2}'
  )"

  if [ -z "$KUBEDECK_INSTALL_HOME" ]; then
    echo
    echo "ERROR: Could not determine the home directory for:"
    echo "$KUBEDECK_INSTALL_USER"
    exit 1
  fi

  KUBEDECK_BREW_BIN=""
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then
      KUBEDECK_BREW_BIN="$candidate"
      break
    fi
  done

  if [ -z "$KUBEDECK_BREW_BIN" ]; then
    echo
    echo "ERROR: Homebrew was not found."
    echo "Install Homebrew first, then run the KubeDock .pkg again."
    exit 1
  fi

  # Wrapper keeps every existing `brew ...` call in this installer
  # unchanged while ensuring Homebrew itself never runs as root.
  brew() {
    local command_line="$KUBEDECK_BREW_BIN"
    local arg quoted
    for arg in "$@"; do
      printf -v quoted '%q' "$arg"
      command_line+=" $quoted"
    done
    /usr/bin/su - "$KUBEDECK_INSTALL_USER" -c "$command_line"
  }

  # Use the logged-in user's Homebrew environment for Python/pip paths and
  # HOME-based application configuration, while retaining root privileges
  # for the final /Applications installation step.
  export HOME="$KUBEDECK_INSTALL_HOME"
  export USER="$KUBEDECK_INSTALL_USER"
  export LOGNAME="$KUBEDECK_INSTALL_USER"
  export KUBEDECK_PKG_INSTALL_USER="$KUBEDECK_INSTALL_USER"

  KUBEDECK_BREW_PREFIX="$(brew --prefix)"
  export PATH="$KUBEDECK_BREW_PREFIX/bin:$PATH"

  echo
  echo "Package mode: running elevated as root."
  echo "Homebrew user: $KUBEDECK_INSTALL_USER"
  echo "Homebrew: $KUBEDECK_BREW_PREFIX"
fi

# --------------------------------------------------
# Windows: Relaunch as Administrator if needed
# --------------------------------------------------
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then

  if ! net session >/dev/null 2>&1; then
    echo
    echo "Administrator privileges are required."
    echo "Requesting elevation..."
    echo

    SCRIPT="$(cygpath -w "$0")"

    powershell.exe -NoProfile -ExecutionPolicy Bypass \
      -Command "Start-Process 'C:\Program Files\Git\bin\bash.exe' -ArgumentList '\"$SCRIPT\"' -Verb RunAs"

    exit 0
  fi
fi

# --------------------------------------------------
# Select GitHub Branch
# --------------------------------------------------

# You can optionally pass a branch directly:
#
#  ./build.sh <branch>
#
# Examples:
#
#  ./build.sh main
#  ./build.sh dev
#  ./build.sh release/v1.2.0
#
# If no branch is supplied, the script retrieves the available
# remote branches from GitHub and lets the user select one.

SELECTED_BRANCH="${1:-}"

echo
echo "=========================================="
echo "KubeDock GitHub Branch Selection"
echo "=========================================="
echo

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required but was not found."
  exit 1
fi

# Retrieve remote branches from GitHub.
echo "Fetching available branches from GitHub..."
echo

BRANCH_LIST="$(
  git ls-remote --heads "$REPO" 2>/dev/null |
  sed 's#^[^[:space:]]*[[:space:]]*refs/heads/##' |
  grep -v '/$' |
  sort
)"

if [ -z "$BRANCH_LIST" ]; then
  echo "ERROR: Could not retrieve branches from:"
  echo "$REPO"
  echo
  echo "Check your internet connection and verify that the repository is accessible."
  exit 1
fi

# If a branch was supplied as an argument, validate it.
if [ -n "$SELECTED_BRANCH" ]; then
  if ! printf '%s\n' "$BRANCH_LIST" | grep -Fxq "$SELECTED_BRANCH"; then
    echo "ERROR: Branch '$SELECTED_BRANCH' does not exist in the repository."
    echo
    echo "Available branches:"
    printf '%s\n' "$BRANCH_LIST" | sed 's/^/ - /'
    exit 1
  fi
elif [ ! -t 0 ]; then
  # No branch argument and no interactive terminal attached (piped from
  # curl on a freshly provisioned instance, driven by cloud-init/CI,
  # run over a non-interactive SSH command, etc.) — a `read` prompt
  # here would just hang forever with no one able to answer it. Fall
  # back to the repository's default branch.
  echo "No TTY detected and no branch given — defaulting to the default branch."

  DEFAULT_BRANCH="$(
    git ls-remote --symref "$REPO" HEAD 2>/dev/null |
    awk '/^ref:/ {sub("refs/heads/", "", $2); print $2}'
  )"

  if [ -n "$DEFAULT_BRANCH" ] && printf '%s\n' "$BRANCH_LIST" | grep -Fxq "$DEFAULT_BRANCH"; then
    SELECTED_BRANCH="$DEFAULT_BRANCH"
  elif printf '%s\n' "$BRANCH_LIST" | grep -Fxq "main"; then
    SELECTED_BRANCH="main"
  else
    SELECTED_BRANCH="$(printf '%s\n' "$BRANCH_LIST" | head -n 1)"
  fi
else
  # Build a numbered branch list.
  BRANCH_COUNT=0
  while IFS= read -r BRANCH; do
    BRANCH_COUNT=$((BRANCH_COUNT + 1))
    BRANCHES[$BRANCH_COUNT]="$BRANCH"
  done <<< "$BRANCH_LIST"

  echo "Available branches:"
  echo

  for ((i=1; i<=BRANCH_COUNT; i++)); do
    printf " [%d] %s\n" "$i" "${BRANCHES[$i]}"
  done

  echo
  printf "Select branch [1-%d]: " "$BRANCH_COUNT"
  read -r BRANCH_SELECTION

  if ! [[ "$BRANCH_SELECTION" =~ ^[0-9]+$ ]] ||
    [ "$BRANCH_SELECTION" -lt 1 ] ||
    [ "$BRANCH_SELECTION" -gt "$BRANCH_COUNT" ]; then
    echo
    echo "ERROR: Invalid branch selection."
    exit 1
  fi

  SELECTED_BRANCH="${BRANCHES[$BRANCH_SELECTION]}"
fi

echo
echo "Selected branch:"
echo " $SELECTED_BRANCH"
echo

# --------------------------------------------------
# Clone / Update Repository
# --------------------------------------------------

if [ -d "$DIR/.git" ]; then
  echo "Repository already exists. Updating..."
  cd "$DIR"

  git fetch --prune origin

  # Make sure the selected branch exists locally.
  if git show-ref --verify --quiet "refs/remotes/origin/$SELECTED_BRANCH"; then
    git checkout -B "$SELECTED_BRANCH" "origin/$SELECTED_BRANCH"
  else
    echo "ERROR: Remote branch origin/$SELECTED_BRANCH was not found."
    exit 1
  fi

  git reset --hard "origin/$SELECTED_BRANCH"

  echo
  echo "Git branch:"
  git branch --show-current

  echo
  echo "Git commit:"
  git log -1 --oneline
else
  echo "Cloning KubeDock branch '$SELECTED_BRANCH'..."
  git clone --branch "$SELECTED_BRANCH" --single-branch "$REPO" "$DIR"
  cd "$DIR"

  echo
  echo "Git branch:"
  git branch --show-current

  echo
  echo "Git commit:"
  git log -1 --oneline
fi

echo
echo "=========================================="
echo "Building from branch: $SELECTED_BRANCH"
echo "=========================================="
echo

# --------------------------------------------------
# Find / Install Python
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

  echo
  echo "Checking Homebrew..."

  if ! command -v brew >/dev/null 2>&1; then
    echo
    echo "Homebrew is required on macOS."
    echo
    echo "Install Homebrew from:"
    echo "https://brew.sh/"
    echo
    exit 1
  fi

  echo "Homebrew: $(brew --version | head -n 1)"

  # Always use Homebrew Python 3.14.
  if ! brew list --formula python@3.14 >/dev/null 2>&1; then
    echo
    echo "Installing Python 3.14..."
    brew install python@3.14
  else
    echo
    echo "Python 3.14 already installed."
  fi

  PYTHON="$(brew --prefix python@3.14)/bin/python3"

  if [ ! -x "$PYTHON" ]; then
    echo
    echo "ERROR: Homebrew Python 3.14 was not found:"
    echo "$PYTHON"
    exit 1
  fi

  # Make Homebrew tools available to child processes as well.
  export PATH="$(brew --prefix python@3.14)/bin:$(brew --prefix)/bin:$PATH"

elif command -v python3 >/dev/null 2>&1; then

  PYTHON="$(command -v python3)"

elif command -v python >/dev/null 2>&1; then

  PYTHON="$(command -v python)"

else

  echo
  echo "Python not found."
  exit 1

fi

echo
echo "Using Python:"
echo "$PYTHON"
"$PYTHON" --version

# --------------------------------------------------
# Install Native Dependencies
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

  echo
  echo "Installing macOS native dependencies..."

  # PyAudio -> PortAudio
  if ! brew list --formula portaudio >/dev/null 2>&1; then
    echo "Installing PortAudio..."
    brew install portaudio
  else
    echo "PortAudio already installed."
  fi

  # Media playback fallback -> FFmpeg
  # QtMultimedia cannot decode every MKV codec on every macOS backend.
  # KubeDock uses FFmpeg to convert unsupported remote video to a
  # broadly-compatible H.264/AAC MP4 when direct playback fails.
  if ! brew list --formula ffmpeg >/dev/null 2>&1; then
    echo "Installing FFmpeg..."
    brew install ffmpeg
  else
    echo "FFmpeg already installed."
  fi

  # SpeechRecognition -> FLAC
  #
  # This is especially important on Apple Silicon.
  # SpeechRecognition can otherwise fall back to its bundled flac-mac
  # executable, which can be Intel-only.
  if ! brew list --formula flac >/dev/null 2>&1; then
    echo "Installing FLAC..."
    brew install flac
  else
    echo "FLAC already installed."
  fi

  export PATH="$(brew --prefix)/bin:$PATH"

  # Help PyAudio find Homebrew PortAudio headers/libraries.
  export CPPFLAGS="${CPPFLAGS:-} -I$(brew --prefix portaudio)/include"
  export LDFLAGS="${LDFLAGS:-} -L$(brew --prefix portaudio)/lib"
  export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:$(brew --prefix portaudio)/lib/pkgconfig"

elif [[ "$OS" == "Linux" ]]; then

  echo
  echo "Checking Linux native dependencies..."

  # PyAudio needs PortAudio development headers. Covering apt/dnf/yum/
  # pacman/zypper/apk means this works unmodified on Debian/Ubuntu,
  # Fedora/RHEL/Amazon Linux 2023, older RHEL/Amazon Linux 2, Arch,
  # openSUSE, and Alpine — i.e. whatever base image the "instance"
  # happens to be running, not just one distro family.
  if command -v apt-get >/dev/null 2>&1; then

    echo "Using apt-get..."

    $SUDO apt-get update
    $SUDO apt-get install -y \
      portaudio19-dev \
      libportaudiocpp0 \
      flac \
      ffmpeg

  elif command -v dnf >/dev/null 2>&1; then

    echo "Using dnf..."

    $SUDO dnf install -y \
      portaudio-devel \
      flac \
      ffmpeg

  elif command -v yum >/dev/null 2>&1; then

    echo "Using yum..."

    $SUDO yum install -y \
      portaudio-devel \
      flac \
      ffmpeg

  elif command -v pacman >/dev/null 2>&1; then

    echo "Using pacman..."

    $SUDO pacman -Sy --noconfirm \
      portaudio \
      flac \
      ffmpeg

  elif command -v zypper >/dev/null 2>&1; then

    echo "Using zypper..."

    $SUDO zypper --non-interactive install \
      portaudio-devel \
      flac \
      ffmpeg

  elif command -v apk >/dev/null 2>&1; then

    echo "Using apk..."

    $SUDO apk add --no-cache \
      portaudio-dev \
      flac \
      ffmpeg

  else

    echo
    echo "WARNING: Could not determine Linux package manager."
    echo "Make sure PortAudio and FLAC are installed manually."

  fi

elif [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then

  echo
  echo "Windows detected."
  echo "Python wheels will provide the required Python dependencies."

fi

# --------------------------------------------------
# Pip Configuration
# --------------------------------------------------

echo
echo "Checking pip..."

"$PYTHON" -m pip --version

# --------------------------------------------------
# Install Python Requirements
# --------------------------------------------------

if [ -f requirements.txt ]; then

  echo
  echo "Installing Python requirements..."

  if [[ "$OS" == "Darwin" ]]; then

    "$PYTHON" -m pip install \
      --break-system-packages \
      -r requirements.txt

  else

    "$PYTHON" -m pip install \
      -r requirements.txt

  fi

else

  echo
  echo "ERROR: requirements.txt not found."
  exit 1

fi

# --------------------------------------------------
# Verify Critical Dependencies
# --------------------------------------------------

echo
echo "Verifying Python dependencies..."

"$PYTHON" - <<'PY'
import sys

print("Python:", sys.executable)

required = [
  ("PyQt5", "PyQt5"),
  ("paramiko", "paramiko"),
  ("SpeechRecognition", "speech_recognition"),
  ("PyAudio", "pyaudio"),
  ("PyInstaller", "PyInstaller"),
]

failed = []

for label, module in required:
  try:
    imported = __import__(module)
    version = getattr(imported, "__version__", "installed")
    print(f"[OK] {label}: {version}")
  except Exception as exc:
    print(f"[ERROR] {label}: {exc}")
    failed.append(label)

if failed:
  print()
  print("Missing/broken dependencies:")
  for name in failed:
    print(" -", name)
  sys.exit(1)

print()
print("All Python dependencies are available.")
PY

# --------------------------------------------------
# Verify FLAC on macOS/Linux
# --------------------------------------------------

if [[ "$OS" == "Darwin" || "$OS" == "Linux" ]]; then

  echo
  echo "Verifying FLAC..."

  if ! command -v flac >/dev/null 2>&1; then
    echo
    echo "ERROR: FLAC executable was not found."
    exit 1
  fi

  echo "FLAC: $(command -v flac)"
  flac --version | head -n 1

fi

# --------------------------------------------------
# Install PyInstaller
# --------------------------------------------------

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then

  echo
  echo "Installing PyInstaller..."

  if [[ "$OS" == "Darwin" ]]; then

    "$PYTHON" -m pip install \
      --break-system-packages \
      "pyinstaller>=6.0"

  else

    "$PYTHON" -m pip install \
      "pyinstaller>=6.0"

  fi

fi

# --------------------------------------------------
# Prepare Web App Static Assets
# --------------------------------------------------

echo
echo "Preparing KubeDock Web UI assets..."

WEBAPP_INDEX="webapp/static/index.html"
WEBAPP_FALLBACK="webapp/static_content.py"

if [ ! -f "$WEBAPP_INDEX" ]; then
  echo
  echo "ERROR: KubeDock Web UI was not found:"
  echo "$WEBAPP_INDEX"
  exit 1
fi

# Keep the PyInstaller fallback synchronized with the real index.html.
# Normal builds package webapp/static directly; the fallback remains useful
# when the static directory cannot be resolved from a frozen application.
"$PYTHON" - <<'PY'
from pathlib import Path
import base64
import gzip

source = Path("webapp/static/index.html")
target = Path("webapp/static_content.py")

html = source.read_bytes()
encoded = base64.b64encode(gzip.compress(html, compresslevel=9)).decode("ascii")
target.write_text(
    "# Auto-generated fallback used when KubeDock runs from a PyInstaller bundle.\n"
    "# Source: webapp/static/index.html\n"
    "import base64\n"
    "import gzip\n\n"
    "INDEX_HTML = gzip.decompress(base64.b64decode(\n"
    "    '''" + encoded + "'''\n"
    ")).decode(\"utf-8\")\n",
    encoding="utf-8",
)
print(f"[OK] Web UI source: {source} ({len(html):,} bytes)")
print(f"[OK] Web UI fallback synchronized: {target}")
PY

# --------------------------------------------------
# Select Icon
# --------------------------------------------------

ICON=""

case "$OS" in

  Darwin)
    if [ -f VM_Visualizer.icns ]; then
      ICON="VM_Visualizer.icns"
    fi
    ;;

  MINGW*|MSYS*|CYGWIN*)
    if [ -f VM_Visualizer.ico ]; then
      ICON="VM_Visualizer.ico"
    fi
    ;;

esac

# --------------------------------------------------
# Clean Previous Build
# --------------------------------------------------

echo
echo "Cleaning previous PyInstaller build..."

rm -rf build
rm -rf "dist/KubeDock.app"
rm -rf "dist/KubeDock"

# Remove stale spec so the generated build always reflects
# the current project state.
rm -f "KubeDock.spec"

# --------------------------------------------------
# --------------------------------------------------
# Verify Refactored Python Packages
# --------------------------------------------------

echo
echo "Verifying refactored Python packages..."

REFACTORED_PACKAGES=(
  dialogs_parts
  kubernetes_tab_parts
  dashboard_tab_parts
  main_window_parts
  workers_parts
  k8s_ai_ops_parts
)

for package in "${REFACTORED_PACKAGES[@]}"; do
  if [ ! -f "$package/__init__.py" ]; then
    echo "ERROR: Missing refactored package: $package"
    exit 1
  fi
  echo "[OK] $package"
done

# Build
# --------------------------------------------------

echo
echo "Building application..."

CMD=(
  "$PYTHON"
  -m
  PyInstaller
  --windowed
  --onedir
  --name
  "KubeDock"
  --osx-bundle-identifier
  "com.hareeshgt.ec2manager"

  # Existing SSH/Paramiko support
  --hidden-import=paramiko
  --collect-all=paramiko

  # Embedded Web App
  --hidden-import=webapp
  --hidden-import=webapp.server

  # AI provider support
  --hidden-import=ai_assist

  # Kubernetes Ops Mind
  --hidden-import=k8s_ai_ops

  # Refactored KubeDock packages
  --collect-submodules=dialogs_parts
  --collect-submodules=kubernetes_tab_parts
  --collect-submodules=dashboard_tab_parts
  --collect-submodules=main_window_parts
  --collect-submodules=workers_parts
  --collect-submodules=k8s_ai_ops_parts

  # Voice input
  --hidden-import=speech_recognition
  --hidden-import=pyaudio

  main.py
)

# Add the icon if available.
if [ -n "$ICON" ]; then
  CMD=(
    "${CMD[@]:0:${#CMD[@]}-1}"
    "--icon=$ICON"
    "main.py"
  )
fi

# Bundle KubeDock SVG icons into the PyInstaller application.
# PyInstaller uses ":" on macOS/Linux and ";" on Windows.
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
  CMD=(
    "${CMD[@]:0:${#CMD[@]}-1}"
    "--add-data=assets;assets"
    "main.py"
  )
else
  CMD=(
    "${CMD[@]:0:${#CMD[@]}-1}"
    "--add-data=assets:assets"
    "main.py"
  )
fi

# Bundle the actual Web App HTML so the packaged application always uses
# the same UI as webapp/static/index.html in the selected GitHub branch.
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
  CMD=(
    "${CMD[@]:0:${#CMD[@]}-1}"
    "--add-data=webapp/static;webapp/static"
    "main.py"
  )
else
  CMD=(
    "${CMD[@]:0:${#CMD[@]}-1}"
    "--add-data=webapp/static:webapp/static"
    "main.py"
  )
fi

"${CMD[@]}"

# --------------------------------------------------
# macOS privacy + Apple Silicon voice support
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

  APP_PATH="dist/KubeDock.app"
  APP_PLIST="$APP_PATH/Contents/Info.plist"

  # Finder-launched apps need an explicit microphone usage description.
  # Without NSMicrophoneUsageDescription, macOS may not present the
  # microphone permission prompt and the application may not appear under
  # System Settings -> Privacy & Security -> Microphone.
  echo
  echo "Configuring macOS microphone permission..."

  if [ ! -f "$APP_PLIST" ]; then
    echo
    echo "ERROR: App Info.plist not found:"
    echo "$APP_PLIST"
    exit 1
  fi

  /usr/libexec/PlistBuddy \
    -c "Delete :NSMicrophoneUsageDescription" \
    "$APP_PLIST" 2>/dev/null || true

  /usr/libexec/PlistBuddy \
    -c "Add :NSMicrophoneUsageDescription string 'KubeDock uses the microphone for Kubernetes voice commands.'" \
    "$APP_PLIST"

  # Give the application a stable bundle identifier.
  /usr/libexec/PlistBuddy \
    -c "Delete :CFBundleIdentifier" \
    "$APP_PLIST" 2>/dev/null || true

  /usr/libexec/PlistBuddy \
    -c "Add :CFBundleIdentifier string 'com.hareeshgt.ec2manager'" \
    "$APP_PLIST"

  echo "Microphone usage description added."
  echo "Bundle identifier: com.hareeshgt.ec2manager"

  # PyInstaller may have signed the bundle before Info.plist was changed.
  # Re-sign the completed bundle so the final application has a consistent
  # code signature after the privacy metadata update.
  echo
  echo "Re-signing macOS application..."

  codesign \
    --deep \
    --force \
    --sign - \
    "$APP_PATH"

  echo "Application re-signed."

  if [ -x "$(brew --prefix)/bin/flac" ]; then
    echo
    echo "Using native Homebrew FLAC:"
    echo "$(brew --prefix)/bin/flac"
  else
    echo
    echo "WARNING: Homebrew FLAC was not found."
  fi

  # Verify the privacy key survived the final bundle/signing step.
  MICROPHONE_DESC=$(
    /usr/libexec/PlistBuddy \
      -c "Print :NSMicrophoneUsageDescription" \
      "$APP_PLIST" 2>/dev/null || true
  )

  if [ -z "$MICROPHONE_DESC" ]; then
    echo
    echo "ERROR: NSMicrophoneUsageDescription was not added."
    exit 1
  fi

  echo
  echo "Microphone permission metadata verified:"
  echo "$MICROPHONE_DESC"

fi

# --------------------------------------------------
# Install
# --------------------------------------------------

case "$OS" in

Darwin)

  echo
  echo "Installing on macOS..."

  APP_PATH="dist/KubeDock.app"

  if [ ! -d "$APP_PATH" ]; then
    echo
    echo "ERROR: PyInstaller did not create:"
    echo "$APP_PATH"
    exit 1
  fi

  $SUDO rm -rf "/Applications/KubeDock.app"
  $SUDO cp -R "$APP_PATH" "/Applications/"

  echo
  echo "Installed:"
  echo "/Applications/KubeDock.app"
  ;;

Linux)

  echo
  echo "Installing on Linux..."

  if [ ! -d "dist/KubeDock" ]; then
    echo
    echo "ERROR: PyInstaller did not create:"
    echo "dist/KubeDock"
    exit 1
  fi

  $SUDO rm -rf "/opt/KubeDock"
  $SUDO mkdir -p "/opt/KubeDock"
  $SUDO cp -R "dist/KubeDock/." "/opt/KubeDock/"

  if [ -d "/usr/local/bin" ] || $SUDO mkdir -p "/usr/local/bin" 2>/dev/null; then
    $SUDO ln -sf "/opt/KubeDock/KubeDock" "/usr/local/bin/kubedeck" 2>/dev/null || true
  fi

  if [ -n "$ICON" ] && [ -f "$ICON" ]; then
    $SUDO mkdir -p "/opt/KubeDock/icon" 2>/dev/null || true
    $SUDO cp "$ICON" "/opt/KubeDock/icon/KubeDock.ico" 2>/dev/null || true
  fi

  if $SUDO mkdir -p "/usr/share/applications" 2>/dev/null; then
    DESKTOP_FILE="$(mktemp)"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=KubeDock
Comment=Kubernetes / VM visualizer and manager
Exec=/opt/KubeDock/KubeDock
Icon=/opt/KubeDock/icon/KubeDock.ico
Terminal=false
Categories=Development;Utility;
EOF
    $SUDO cp "$DESKTOP_FILE" "/usr/share/applications/kubedeck.desktop" 2>/dev/null || true
    rm -f "$DESKTOP_FILE"
  fi

  echo
  echo "Installed:"
  echo "/opt/KubeDock"
  echo "Run from any terminal with: kubedeck"
  ;;

MINGW*|MSYS*|CYGWIN*)

  echo
  echo "Installing on Windows..."

  INSTALL_DIR="/c/Program Files/KubeDock"

  if [ ! -d "dist/KubeDock" ]; then
    echo
    echo "ERROR: PyInstaller did not create:"
    echo "dist/KubeDock"
    exit 1
  fi

  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"

  cp -R "dist/KubeDock/." "$INSTALL_DIR/"

  echo
  echo "Installed to:"
  echo "$INSTALL_DIR"
  ;;

*)

  echo
  echo "Unsupported operating system:"
  echo "$OS"
  exit 1
  ;;

esac

# --------------------------------------------------
# Cleanup
# --------------------------------------------------

cd ..

rm -rf "$DIR"

echo
echo "=========================================="
echo "KubeDock installed successfully!"
echo "=========================================="
echo
echo "Built from GitHub branch:"
echo " $SELECTED_BRANCH"
echo
echo "Included:"
echo " [OK] Kubernetes Ops Mind"
echo " [OK] Google Web Speech voice input"
echo " [OK] PyAudio microphone support"
echo " [OK] Native FLAC support"
echo " [OK] AI operation history"
echo " [OK] Current Web App UI"
echo " [OK] Embedded Web App static assets"
echo