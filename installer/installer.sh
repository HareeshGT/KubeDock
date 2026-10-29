#!/usr/bin/env bash
set -euo pipefail

# KubeDock installer: prefer the prebuilt rolling main bundle for a fast install.
# If no compatible bundle is published (or a branch is explicitly requested),
# fall back to the existing branch-selecting source build with PyInstaller.
#
#   ./installer.sh                 # fast prebuilt bundle, when available
#   ./installer.sh <branch>        # build the requested branch from source
#
# Environment:
#   KUBEDOCK_INSTALL_MODE          auto (default), release (fast-only), or source.
#   KUBEDOCK_ENABLE_QWEN_TTS       1/0: include (or skip) the optional Qwen3-TTS runtime.
#   KUBEDOCK_KEEP_BUILD            1: keep the temporary build directory (debugging).
#   KUBEDECK_PKG_MODE              Set by the macOS .pkg wrapper (installer/build_pkg.sh).
#\n
REPO="https://github.com/HareeshGT/KubeDock.git"

OS="$(uname -s)"

# Never block on a Git credential prompt (CI, cloud-init, curl | bash).
export GIT_TERMINAL_PROMPT=0

# pip: skip the PyPI self-update check (one extra network round trip per call)
# and never prompt.
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1

# Homebrew: an implicit `brew update` before every install can take minutes and
# post-install cleanup is wasted work for a build machine. brew_install() below
# falls back to an explicit update if a formula is not found in the local index.
if [[ "$OS" == "Darwin" ]]; then
  export HOMEBREW_NO_AUTO_UPDATE=1
  export HOMEBREW_NO_INSTALL_CLEANUP=1
  export HOMEBREW_NO_ENV_HINTS=1
fi

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

if [[ "$OS" == "Darwin" && "$KUBEDECK_PKG_MODE" == "1" && "$(id -u)" = "0" && "${KUBEDOCK_INSTALL_MODE:-source}" == "source" ]]; then
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
  # unchanged while ensuring Homebrew itself never runs as root. `su -`
  # starts a clean login environment, so the HOMEBREW_* switches exported at
  # the top of this script are forwarded explicitly.
  brew() {
    local command_line="$KUBEDECK_BREW_BIN"
    local arg quoted name
    for name in HOMEBREW_NO_AUTO_UPDATE HOMEBREW_NO_INSTALL_CLEANUP HOMEBREW_NO_ENV_HINTS; do
      if [ -n "${!name:-}" ]; then
        command_line="$name=${!name} $command_line"
      fi
    done
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

# --------------------------------------------------
# Fast path: install a prebuilt rolling main bundle
# --------------------------------------------------
# Normal installs should not need Git, Python/pip or a local PyInstaller build.
# The rolling release is published by .github/workflows/fast-installer-bundles.yml.
# Pass a branch explicitly or set KUBEDOCK_INSTALL_MODE=source to keep the
# developer flow that builds the selected branch locally.
SELECTED_BRANCH="${1:-${KUBEDECK_BRANCH:-${KUBEDOCK_BRANCH:-}}}"
KUBEDOCK_INSTALL_MODE="${KUBEDOCK_INSTALL_MODE:-auto}"

if [[ "$KUBEDOCK_INSTALL_MODE" != "auto" && "$KUBEDOCK_INSTALL_MODE" != "release" && "$KUBEDOCK_INSTALL_MODE" != "source" ]]; then
  echo "ERROR: KUBEDOCK_INSTALL_MODE must be auto, release, or source."
  exit 1
fi

install_prebuilt_bundle() {
  local asset="" arch="" base url tmp archive checksum_file expected_hash actual_hash extracted target staged backup archive_win tmp_win
  base="https://github.com/HareeshGT/KubeDock/releases/download/fast-installer"

  case "$OS" in
    Darwin)
      arch="$(uname -m)"
      case "$arch" in
        arm64|aarch64) asset="KubeDock-macos-arm64.tar.gz" ;;
        x86_64|amd64)  asset="KubeDock-macos-x86_64.tar.gz" ;;
        *) echo "No prebuilt macOS bundle for architecture: $arch" >&2; return 1 ;;
      esac
      ;;
    Linux)
      arch="$(uname -m)"
      case "$arch" in
        x86_64|amd64) asset="KubeDock-linux-x86_64.tar.gz" ;;
        aarch64|arm64)
          echo "A prebuilt Linux ARM64 bundle is not published yet; the auto installer can fall back to a source build." >&2
          return 1
          ;;
        *) echo "No prebuilt Linux bundle for architecture: $arch" >&2; return 1 ;;
      esac
      ;;
    MINGW*|MSYS*|CYGWIN*)
      arch="${PROCESSOR_ARCHITECTURE:-AMD64}"
      case "$arch" in
        AMD64|amd64|x86_64) asset="KubeDock-windows-x86_64.zip" ;;
        *) echo "No prebuilt Windows bundle for architecture: $arch" >&2; return 1 ;;
      esac
      ;;
    *)
      return 1
      ;;
  esac

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to download the prebuilt bundle." >&2
    return 1
  fi
  if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
    if ! command -v powershell.exe >/dev/null 2>&1 || ! command -v cygpath >/dev/null 2>&1; then
      echo "PowerShell and cygpath are required to install the Windows prebuilt bundle." >&2
      return 1
    fi
  elif ! command -v tar >/dev/null 2>&1; then
    echo "tar is required to extract the prebuilt bundle." >&2
    return 1
  fi

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/kubedock-prebuilt.XXXXXX")" || return 2
  archive="$tmp/$asset"
  url="$base/$asset"

  echo
  echo "Fast install: downloading prebuilt KubeDock bundle..."
  echo "Platform asset: $asset"
  if ! curl -fL --retry 2 --retry-delay 2 --connect-timeout 10 --max-time 900 "$url" -o "$archive"; then
    echo "Prebuilt bundle is not available yet (or could not be downloaded)." >&2
    rm -rf "$tmp"
    return 1
  fi
  if [ ! -s "$archive" ]; then
    echo "Downloaded prebuilt bundle is empty." >&2
    rm -rf "$tmp"
    return 2
  fi

  checksum_file="$tmp/SHA256SUMS"
  if ! curl -fL --retry 2 --retry-delay 1 --connect-timeout 10 --max-time 60 "$base/SHA256SUMS" -o "$checksum_file"; then
    echo "Could not download the prebuilt bundle checksum file." >&2
    rm -rf "$tmp"
    return 1
  fi
  expected_hash="$(awk -v name="$asset" '$2 == name { print $1; exit }' "$checksum_file")"
  if ! [[ "$expected_hash" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo "The release checksum file does not contain a valid digest for $asset." >&2
    rm -rf "$tmp"
    return 2
  fi

  if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
    archive_win="$(cygpath -w "$archive")"
    KUBEDOCK_ARCHIVE_WIN="$archive_win" actual_hash="$(powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '$path = $env:KUBEDOCK_ARCHIVE_WIN; (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash' 2>/dev/null | tr -d '\r\n' | tr '[:upper:]' '[:lower:]')"
  elif command -v sha256sum >/dev/null 2>&1; then
    actual_hash="$(sha256sum "$archive" | awk '{ print $1 }' | tr '[:upper:]' '[:lower:]')"
  elif command -v shasum >/dev/null 2>&1; then
    actual_hash="$(shasum -a 256 "$archive" | awk '{ print $1 }' | tr '[:upper:]' '[:lower:]')"
  else
    echo "No SHA-256 utility was found; cannot verify the downloaded bundle." >&2
    rm -rf "$tmp"
    return 1
  fi
  expected_hash="$(printf '%s' "$expected_hash" | tr '[:upper:]' '[:lower:]')"
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    echo "SHA-256 verification failed for $asset." >&2
    rm -rf "$tmp"
    return 2
  fi
  echo "SHA-256 checksum verified."

  if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
    archive_win="$(cygpath -w "$archive")"
    tmp_win="$(cygpath -w "$tmp")"
    if ! KUBEDOCK_ARCHIVE_WIN="$archive_win" KUBEDOCK_TMP_WIN="$tmp_win" powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '$archive = $env:KUBEDOCK_ARCHIVE_WIN; $destination = $env:KUBEDOCK_TMP_WIN; Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force'; then
      echo "Downloaded Windows prebuilt bundle could not be extracted." >&2
      rm -rf "$tmp"
      return 2
    fi
  elif ! tar -xf "$archive" -C "$tmp"; then
    echo "Downloaded prebuilt bundle could not be extracted." >&2
    rm -rf "$tmp"
    return 2
  fi

  case "$OS" in
    Darwin)
      extracted="$tmp/KubeDock.app"
      target="/Applications/KubeDock.app"
      staged="/Applications/KubeDock.app.new"
      backup="/Applications/KubeDock.app.old"
      if [ ! -d "$extracted/Contents" ]; then
        echo "Prebuilt macOS archive did not contain KubeDock.app." >&2
        rm -rf "$tmp"
        return 2
      fi
      $SUDO rm -rf "$staged"
      if ! $SUDO ditto "$extracted" "$staged"; then
        $SUDO rm -rf "$staged"
        rm -rf "$tmp"
        return 2
      fi
      # Re-sign after extraction/copy so the installed bundle has a consistent
      # ad-hoc signature even on systems that strip archive extended attributes.
      if ! $SUDO codesign --deep --force --sign - "$staged"; then
        $SUDO rm -rf "$staged"
        rm -rf "$tmp"
        return 2
      fi
      ;;
    Linux)
      extracted="$tmp/KubeDock"
      target="/opt/KubeDock"
      staged="/opt/KubeDock.new"
      backup="/opt/KubeDock.old"
      if [ ! -x "$extracted/KubeDock" ]; then
        echo "Prebuilt Linux archive did not contain the KubeDock executable." >&2
        rm -rf "$tmp"
        return 2
      fi
      $SUDO mkdir -p /opt
      $SUDO rm -rf "$staged"
      if ! $SUDO cp -a "$extracted" "$staged"; then
        $SUDO rm -rf "$staged"
        rm -rf "$tmp"
        return 2
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      extracted="$tmp/KubeDock"
      target="/c/Program Files/KubeDock"
      staged="/c/Program Files/KubeDock.new"
      backup="/c/Program Files/KubeDock.old"
      if [ ! -f "$extracted/KubeDock.exe" ]; then
        echo "Prebuilt Windows archive did not contain KubeDock.exe." >&2
        rm -rf "$tmp"
        return 2
      fi
      rm -rf "$staged"
      if ! cp -R "$extracted" "$staged"; then
        rm -rf "$staged" "$tmp"
        return 2
      fi
      ;;
  esac

  # Atomic-ish replace: move the current install aside first and restore it if
  # the new staged directory cannot be moved into place.
  $SUDO rm -rf "$backup"
  if [ -e "$target" ]; then
    if ! $SUDO mv "$target" "$backup"; then
      $SUDO rm -rf "$staged"
      rm -rf "$tmp"
      return 2
    fi
  fi
  if ! $SUDO mv "$staged" "$target"; then
    if [ -e "$backup" ]; then
      $SUDO mv "$backup" "$target" || true
    fi
    $SUDO rm -rf "$staged"
    rm -rf "$tmp"
    return 2
  fi
  $SUDO rm -rf "$backup"

  if [[ "$OS" == "Linux" ]]; then
    if [ -d "/usr/local/bin" ] || $SUDO mkdir -p "/usr/local/bin" 2>/dev/null; then
      $SUDO ln -sf "/opt/KubeDock/KubeDock" "/usr/local/bin/kubedock" 2>/dev/null || true
      $SUDO ln -sf "/opt/KubeDock/KubeDock" "/usr/local/bin/kubedeck" 2>/dev/null || true
    fi
    if $SUDO mkdir -p "/usr/share/applications" 2>/dev/null; then
      local desktop_tmp
      desktop_tmp="$(mktemp)"
      cat > "$desktop_tmp" <<'EOF'
[Desktop Entry]
Type=Application
Name=KubeDock
Comment=Kubernetes / VM infrastructure workspace
Exec=/opt/KubeDock/KubeDock
Icon=application-x-executable
Terminal=false
Categories=Development;Utility;
EOF
      $SUDO cp "$desktop_tmp" "/usr/share/applications/kubedock.desktop" 2>/dev/null || true
      rm -f "$desktop_tmp"
    fi
    if ! command -v flac >/dev/null 2>&1; then
      echo "NOTE: FLAC is not installed; some speech-recognition workflows may be unavailable."
    fi
    if ! command -v ffmpeg >/dev/null 2>&1; then
      echo "NOTE: FFmpeg is not installed; uncommon remote media formats may not play."
    fi
  fi

  echo
  echo "=========================================="
  echo "KubeDock installed from prebuilt bundle!"
  echo "=========================================="
  case "$OS" in
    Darwin) echo "Installed: /Applications/KubeDock.app" ;;
    Linux) echo "Installed: /opt/KubeDock"; echo "Launch with: kubedock" ;;
    MINGW*|MSYS*|CYGWIN*) echo "Installed: C:\\Program Files\\KubeDock" ;;
  esac
  if [ -f "$target/KubeDock-BUILD-METADATA.json" ]; then
    echo "Build metadata:"
    cat "$target/KubeDock-BUILD-METADATA.json"
  elif [ -f "$target/Contents/Resources/KubeDock-BUILD-METADATA.json" ]; then
    echo "Build metadata:"
    cat "$target/Contents/Resources/KubeDock-BUILD-METADATA.json"
  fi
  echo
  echo "Optional Qwen3-TTS is not included in the fast bundle. Set KUBEDOCK_INSTALL_MODE=source to build a custom branch/runtime."
  rm -rf "$tmp"
  return 0
}

if [[ "$KUBEDOCK_INSTALL_MODE" == "release" && -n "$SELECTED_BRANCH" ]]; then
  echo "ERROR: KUBEDOCK_INSTALL_MODE=release cannot be combined with an explicit branch. Omit the branch for a fast install or use KUBEDOCK_INSTALL_MODE=source." >&2
  exit 1
fi

if [[ "$KUBEDOCK_INSTALL_MODE" != "source" && -z "$SELECTED_BRANCH" ]]; then
  if install_prebuilt_bundle; then
    exit 0
  else
    PREBUILT_RC=$?
    if [[ "$KUBEDOCK_INSTALL_MODE" == "release" || "$PREBUILT_RC" -ne 1 ]]; then
      echo "ERROR: Prebuilt installation failed. No source build was attempted." >&2
      exit 1
    fi
    echo
    echo "Prebuilt bundle unavailable; falling back to the existing source installer."
    echo "The source build can take significantly longer because it installs dependencies and runs PyInstaller."
    echo
  fi
fi

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

SELECTED_BRANCH="${1:-${KUBEDECK_BRANCH:-${KUBEDOCK_BRANCH:-}}}"

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

# A single network round trip returns both the branch list and the default
# branch (HEAD's symref). The `ref:` line is excluded from the branch list.
REMOTE_REFS="$(git ls-remote --symref "$REPO" HEAD 'refs/heads/*' 2>/dev/null || true)"

BRANCH_LIST="$(
  printf '%s\n' "$REMOTE_REFS" |
  awk '$1 != "ref:" && $2 ~ /^refs\/heads\// { sub("^refs/heads/", "", $2); print $2 }' |
  grep -v '/$' |
  sort || true
)"

DEFAULT_BRANCH="$(
  printf '%s\n' "$REMOTE_REFS" |
  awk '$1 == "ref:" { sub("^refs/heads/", "", $2); print $2 }'
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
  BRANCHES=()
  while IFS= read -r BRANCH; do
    BRANCH_COUNT=$((BRANCH_COUNT + 1))
    BRANCHES[BRANCH_COUNT]="$BRANCH"
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
# Optional local Qwen3-TTS bundle
# --------------------------------------------------

# Keep the default build lightweight. Set KUBEDOCK_ENABLE_QWEN_TTS=1 to opt
# into the large local PyTorch/Qwen runtime; the voice model weights download
# on first use and are intentionally not embedded into the app bundle.
ENABLE_QWEN_TTS="${KUBEDOCK_ENABLE_QWEN_TTS:-}"
case "$ENABLE_QWEN_TTS" in
  1|y|Y|yes|YES|Yes|true|TRUE|True|on|ON|On) ENABLE_QWEN_TTS=1 ;;
  0|n|N|no|NO|No|false|FALSE|False|off|OFF|Off) ENABLE_QWEN_TTS=0 ;;
  *)
    if [ -t 0 ]; then
      printf "Include local Qwen3-TTS voice support? This adds several GB to the app build [y/N]: "
      read -r QWEN_ANSWER
      case "$QWEN_ANSWER" in y|Y|yes|YES|Yes) ENABLE_QWEN_TTS=1 ;; *) ENABLE_QWEN_TTS=0 ;; esac
    else
      ENABLE_QWEN_TTS=0
      echo "Non-interactive install: skipping optional Qwen3-TTS bundle. Set KUBEDOCK_ENABLE_QWEN_TTS=1 to enable it."
    fi
    ;;
esac
if [[ "$ENABLE_QWEN_TTS" == "1" ]]; then
  echo "Qwen3-TTS bundle: ENABLED (Python 3.12 required; model weights download on first launch)."
else
  echo "Qwen3-TTS bundle: disabled (small default build)."
fi
echo

# --------------------------------------------------
# Clone Repository (shallow, into a throw-away directory)
# --------------------------------------------------

# Everything is built in a private temporary directory that is removed on exit
# (success or failure): a failed run no longer leaves a multi-GB tree behind,
# and an unrelated directory in the current folder is never reset or deleted.
# Set KUBEDOCK_KEEP_BUILD=1 to keep it for debugging.
_tmp_base="${TMPDIR:-/tmp}"
BUILD_ROOT="$(mktemp -d "${_tmp_base%/}/kubedock-build.XXXXXX")"
DIR="$BUILD_ROOT/src"

cleanup() {
  local rc=$?
  cd / 2>/dev/null || true
  if [ "${KUBEDOCK_KEEP_BUILD:-0}" = "1" ]; then
    echo "Build directory kept: $BUILD_ROOT" >&2
  else
    rm -rf "$BUILD_ROOT"
    if [ "$rc" -ne 0 ]; then
      echo "Install failed (exit $rc). Re-run with KUBEDOCK_KEEP_BUILD=1 to keep the build directory." >&2
    fi
  fi
}
trap cleanup EXIT

# Only the tip of the selected branch is needed to build.
echo "Cloning KubeDock branch '$SELECTED_BRANCH' (shallow)..."
git clone --depth 1 --branch "$SELECTED_BRANCH" --single-branch "$REPO" "$DIR"
cd "$DIR"

echo
echo "Git branch:"
git branch --show-current

echo
echo "Git commit:"
git log -1 --oneline

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

  # Reuse the prefix already resolved in .pkg mode; otherwise ask Homebrew once
  # (every brew call is a separate, slow process, and a `su` hop in .pkg mode).
  BREW_PREFIX="${KUBEDECK_BREW_PREFIX:-$(brew --prefix)}"

  # `brew install`, retrying once after an explicit `brew update` if the local
  # formula index is stale (implicit auto-update is disabled above for speed).
  brew_install() {
    if ! brew install "$@"; then
      echo
      echo "brew install failed; updating Homebrew and retrying once..."
      brew update
      brew install "$@"
    fi
  }

  # One `brew list` for all checks instead of one process per formula.
  INSTALLED_FORMULAE="$(brew list --formula -1 2>/dev/null || true)"
  formula_installed() {
    case $'\n'"$INSTALLED_FORMULAE"$'\n' in
      *$'\n'"$1"$'\n'*) return 0 ;;
      *) return 1 ;;
    esac
  }

  # Python 3.14 remains the default build runtime; the Qwen ML stack is
  # built with Python 3.12, the upstream-recommended environment.
  if [[ "$ENABLE_QWEN_TTS" == "1" ]]; then
    PYTHON_MINOR="3.12"
  else
    PYTHON_MINOR="3.14"
  fi
  PYTHON_FORMULA="python@$PYTHON_MINOR"

  # Python plus the native libraries needed later, installed in ONE brew call:
  #   portaudio -> PyAudio
  #   ffmpeg    -> media playback fallback. QtMultimedia cannot decode every MKV
  #                codec on every macOS backend, so KubeDock converts unsupported
  #                remote video to a broadly-compatible H.264/AAC MP4.
  #   flac      -> SpeechRecognition. Without it, SpeechRecognition can fall back
  #                to its bundled flac-mac executable, which can be Intel-only on
  #                Apple Silicon.
  FORMULAE_TO_INSTALL=()
  for formula in "$PYTHON_FORMULA" portaudio ffmpeg flac; do
    if formula_installed "$formula"; then
      echo "$formula already installed."
    else
      FORMULAE_TO_INSTALL+=("$formula")
    fi
  done

  if [ "${#FORMULAE_TO_INSTALL[@]}" -gt 0 ]; then
    echo
    echo "Installing Homebrew packages: ${FORMULAE_TO_INSTALL[*]}"
    brew_install "${FORMULAE_TO_INSTALL[@]}"
  fi

  FORMULA_PREFIX="$(brew --prefix "$PYTHON_FORMULA")"
  PYTHON=""
  if [[ "$PYTHON_MINOR" == "3.12" ]]; then
    PYTHON_CANDIDATES=(
      "$FORMULA_PREFIX/bin/python3.12"
      "$BREW_PREFIX/bin/python3.12"
      "$FORMULA_PREFIX/libexec/bin/python3.12"
      "$FORMULA_PREFIX/bin/python3"
      "$FORMULA_PREFIX/libexec/bin/python3"
    )
  else
    PYTHON_CANDIDATES=(
      "$FORMULA_PREFIX/bin/python3.14"
      "$BREW_PREFIX/bin/python3.14"
      "$FORMULA_PREFIX/libexec/bin/python3.14"
      "$FORMULA_PREFIX/bin/python3"
      "$FORMULA_PREFIX/libexec/bin/python3"
    )
  fi
  for candidate in "${PYTHON_CANDIDATES[@]}"; do
    if [ -x "$candidate" ] && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (${PYTHON_MINOR/./, }) else 1)" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON" ]; then
    echo
    echo "ERROR: Could not find a working Python $PYTHON_MINOR executable for Homebrew $PYTHON_FORMULA."
    echo "Tried:"
    printf "  %s\n" "${PYTHON_CANDIDATES[@]}"
    echo "Please run: brew reinstall $PYTHON_FORMULA"
    exit 1
  fi

  # Make Homebrew tools available to child processes as well. Homebrew may
  # expose only the versioned executable (python3.12) on this machine.
  export PATH="$FORMULA_PREFIX/bin:$FORMULA_PREFIX/libexec/bin:$BREW_PREFIX/bin:$PATH"

elif [[ "$ENABLE_QWEN_TTS" == "1" ]] && [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then

  # On Windows/MSYS use the Python Launcher to select exactly Python 3.12.
  if command -v py >/dev/null 2>&1; then
    WINDOWS_PYTHON="$(py -3.12 -c "import sys; print(sys.executable)" 2>/dev/null || true)"
    if [ -n "$WINDOWS_PYTHON" ]; then
      if command -v cygpath >/dev/null 2>&1; then
        PYTHON="$(cygpath -u "$WINDOWS_PYTHON")"
      else
        PYTHON="$WINDOWS_PYTHON"
      fi
    fi
  fi
  if [ -z "${PYTHON:-}" ] && command -v python3.12 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.12)"
  fi
  if [ -z "${PYTHON:-}" ]; then
    echo "ERROR: Qwen3-TTS packaging requires 64-bit Python 3.12. Install it and rerun."
    exit 1
  fi

elif [[ "$ENABLE_QWEN_TTS" == "1" ]] && command -v python3.12 >/dev/null 2>&1; then

  PYTHON="$(command -v python3.12)"

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
PYTHON_MINOR="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$ENABLE_QWEN_TTS" == "1" && "$PYTHON_MINOR" != "3.12" ]]; then
  echo
  echo "ERROR: Qwen3-TTS packaging requires Python 3.12; selected $PYTHON_MINOR ($PYTHON)."
  echo "Install Python 3.12 and rerun, or leave Qwen3-TTS disabled."
  exit 1
fi

# --------------------------------------------------
# Install Native Dependencies
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

  echo
  echo "macOS native dependencies (PortAudio, FFmpeg, FLAC): handled by the Homebrew step above."

  export PATH="$BREW_PREFIX/bin:$PATH"

  # Help PyAudio find Homebrew PortAudio headers/libraries.
  PORTAUDIO_PREFIX="$(brew --prefix portaudio)"
  export CPPFLAGS="${CPPFLAGS:-} -I$PORTAUDIO_PREFIX/include"
  export LDFLAGS="${LDFLAGS:-} -L$PORTAUDIO_PREFIX/lib"
  export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:$PORTAUDIO_PREFIX/lib/pkgconfig"

elif [[ "$OS" == "Linux" ]]; then

  echo
  echo "Checking Linux native dependencies..."

  # PyAudio needs PortAudio development headers. Covering apt/dnf/yum/
  # pacman/zypper/apk means this works unmodified on Debian/Ubuntu,
  # Fedora/RHEL/Amazon Linux 2023, older RHEL/Amazon Linux 2, Arch,
  # openSUSE, and Alpine — i.e. whatever base image the "instance"
  # happens to be running, not just one distro family.
  #
  # Only what is actually missing gets installed, so a re-run on a prepared
  # machine does no package-manager work at all (no `apt-get update`, no sudo
  # prompt). Presence is checked by what the build needs - the flac and ffmpeg
  # executables and the PortAudio header - rather than by package name, which
  # differs between distros (e.g. Fedora ships ffmpeg-free).
  have_portaudio() {
    { command -v pkg-config >/dev/null 2>&1 && pkg-config --exists portaudio-2.0; } ||
      [ -f /usr/include/portaudio.h ] || [ -f /usr/local/include/portaudio.h ]
  }

  LINUX_PM=""
  for pm in apt-get dnf yum pacman zypper apk; do
    if command -v "$pm" >/dev/null 2>&1; then
      LINUX_PM="$pm"
      break
    fi
  done

  if [ -z "$LINUX_PM" ]; then

    echo
    echo "WARNING: Could not determine Linux package manager."
    echo "Make sure PortAudio and FLAC are installed manually."

  else

    case "$LINUX_PM" in
      apt-get)        PORTAUDIO_PKGS=(portaudio19-dev libportaudiocpp0) ;;
      dnf|yum|zypper) PORTAUDIO_PKGS=(portaudio-devel) ;;
      pacman)         PORTAUDIO_PKGS=(portaudio) ;;
      apk)            PORTAUDIO_PKGS=(portaudio-dev) ;;
    esac

    LINUX_PKGS=()
    have_portaudio || LINUX_PKGS+=("${PORTAUDIO_PKGS[@]}")
    command -v flac >/dev/null 2>&1 || LINUX_PKGS+=(flac)
    command -v ffmpeg >/dev/null 2>&1 || LINUX_PKGS+=(ffmpeg)

    if [ "${#LINUX_PKGS[@]}" -eq 0 ]; then

      echo "Native dependencies (PortAudio, FLAC, FFmpeg) already installed."

    else

      echo "Using $LINUX_PM to install: ${LINUX_PKGS[*]}"

      case "$LINUX_PM" in
        apt-get)
          # Install first without refreshing the package lists; refresh only if
          # that fails (fresh images have no lists, old lists can 404).
          APT_ENV=(env DEBIAN_FRONTEND=noninteractive)
          if ! $SUDO "${APT_ENV[@]}" apt-get install -y "${LINUX_PKGS[@]}"; then
            echo
            echo "Install failed; refreshing package lists and retrying once..."
            $SUDO "${APT_ENV[@]}" apt-get update
            $SUDO "${APT_ENV[@]}" apt-get install -y "${LINUX_PKGS[@]}"
          fi
          ;;
        dnf)    $SUDO dnf install -y "${LINUX_PKGS[@]}" ;;
        yum)    $SUDO yum install -y "${LINUX_PKGS[@]}" ;;
        pacman) $SUDO pacman -Sy --noconfirm "${LINUX_PKGS[@]}" ;;
        zypper) $SUDO zypper --non-interactive install "${LINUX_PKGS[@]}" ;;
        apk)    $SUDO apk add --no-cache "${LINUX_PKGS[@]}" ;;
      esac

    fi

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

# Flags shared by every pip call below.
#   --prefer-binary: take a ready-made wheel instead of compiling a newer sdist
#                    (source builds are the slow, fragile path).
PIP_ARGS=(--prefer-binary)

# PEP 668: Homebrew Python and recent Debian/Ubuntu/Fedora mark their system
# Python as "externally managed", which makes a plain `pip install` fail. Add
# the override only when that marker exists and we are not inside a venv, so it
# is never passed to a pip that does not know the flag. (Previously macOS always
# passed it and Linux never did, so Linux failed on PEP 668 distributions.)
if "$PYTHON" -c 'import os, sys, sysconfig; sys.exit(0 if sys.prefix == sys.base_prefix and os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)'; then
  PIP_ARGS+=(--break-system-packages)
fi

pip_install() {
  "$PYTHON" -m pip install "${PIP_ARGS[@]}" "$@"
}

# --------------------------------------------------
# Install Python Requirements
# --------------------------------------------------

if [ ! -f requirements.txt ]; then
  echo
  echo "ERROR: requirements.txt not found."
  exit 1
fi

echo
echo "Installing Python requirements..."

pip_install -r requirements.txt

# Optional voice input (PyAudio). Failure must not abort the install; the
# app runs without it and just disables microphone input.
if [ -f requirements-voice.txt ]; then
  echo
  echo "Installing optional voice-input requirements..."
  pip_install -r requirements-voice.txt \
    || echo "WARNING: voice-input packages failed to install; voice input will be unavailable."
fi

# Optional local Qwen runtime. This is deliberately opt-in because PyTorch,
# Transformers and Qwen increase both install time and bundle size.
if [[ "$ENABLE_QWEN_TTS" == "1" ]]; then
  if [ ! -f requirements-qwen-tts.txt ]; then
    echo "ERROR: this branch does not contain requirements-qwen-tts.txt."
    exit 1
  fi
  echo
  echo "Installing optional Qwen3-TTS runtime (Python 3.12; PyTorch/Transformers)..."
  pip_install -r requirements-qwen-tts.txt
  "$PYTHON" -c "import torch, qwen_tts; print('[OK] Qwen3-TTS runtime:', getattr(qwen_tts, '__version__', 'installed')); print('[OK] PyTorch:', torch.__version__)"
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
  ("PyInstaller", "PyInstaller"),
]

# Voice input is optional (see requirements-voice.txt): report it, never fail on it.
optional = [
  ("PyAudio", "pyaudio"),
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

for label, module in optional:
  try:
    imported = __import__(module)
    version = getattr(imported, "__version__", "installed")
    print(f"[OK] {label}: {version}")
  except Exception as exc:
    print(f"[WARN] {label}: {exc} (voice input will be unavailable)")

if failed:
  print()
  print("Missing/broken dependencies:")
  for name in failed:
    print(" -", name)
  sys.exit(1)

print()
print("All required Python dependencies are available.")
PY

# Remember whether PyAudio is importable so the build and the final summary
# reflect what is actually installed.
HAVE_PYAUDIO=0
if "$PYTHON" -c "import pyaudio" >/dev/null 2>&1; then
  HAVE_PYAUDIO=1
fi

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

# (PyInstaller comes from requirements.txt and is verified above, so the former
# separate "Install PyInstaller" step could never run and was removed.)

# --------------------------------------------------
# Prepare Web App Static Assets
# --------------------------------------------------

echo
echo "Preparing KubeDock Web UI assets..."

WEBAPP_INDEX="webapp/static/index.html"

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

ICON=""          # passed to PyInstaller (macOS .icns / Windows .ico)
DESKTOP_ICON=""  # copied next to the install for the Linux .desktop entry

case "$OS" in

  Darwin)
    if [ -f KubeDock.icns ]; then
      ICON="KubeDock.icns"
    fi
    ;;

  MINGW*|MSYS*|CYGWIN*)
    if [ -f KubeDock.ico ]; then
      ICON="KubeDock.ico"
    fi
    ;;

  Linux)
    # PyInstaller does not use --icon on Linux, but the .desktop entry still
    # needs an icon file (it used to point at one that was never installed).
    if [ -f KubeDock.ico ]; then
      DESKTOP_ICON="KubeDock.ico"
    fi
    ;;

esac

# (There is no "clean previous build" step: every run builds in a fresh clone.)

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

# --------------------------------------------------
# Build
# --------------------------------------------------

echo
echo "Building application..."

# PyInstaller separates --add-data source and destination with ";" on Windows
# and ":" everywhere else.
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
  DATA_SEP=";"
else
  DATA_SEP=":"
fi

# Flags are collected in one array and main.py is appended last (this replaces
# four rounds of re-slicing a CMD array to splice flags in before main.py).
PYI_ARGS=(
  --noconfirm
  --windowed
  --onedir
  --name
  "KubeDock"

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

  # Bundle KubeDock SVG icons into the PyInstaller application.
  "--add-data=assets${DATA_SEP}assets"

  # Bundle the actual Web App HTML so the packaged application always uses
  # the same UI as webapp/static/index.html in the selected GitHub branch.
  "--add-data=webapp/static${DATA_SEP}webapp/static"
)

# A stable bundle identifier (also re-applied to Info.plist below). The flag is
# macOS-only, so it is no longer passed on Linux/Windows.
if [[ "$OS" == "Darwin" ]]; then
  PYI_ARGS+=(--osx-bundle-identifier "com.hareeshgt.ec2manager")
fi

# PyAudio is optional; only declare it as a hidden import when it is installed.
if [[ "$HAVE_PYAUDIO" == "1" ]]; then
  PYI_ARGS+=(--hidden-import=pyaudio)
fi

# Add the icon if available.
if [ -n "$ICON" ]; then
  PYI_ARGS+=("--icon=$ICON")
fi

# Collect dynamic Qwen/Torch modules only when the local TTS runtime is opted in.
# This makes the onedir bundle much larger; downloaded model weights remain in the
# Hugging Face cache and are not copied into the installer.
if [[ "$ENABLE_QWEN_TTS" == "1" ]]; then
  PYI_ARGS+=(
    --collect-all=qwen_tts
    --collect-all=torch
    --collect-all=torchaudio
    --collect-all=transformers
    --collect-all=accelerate
    --collect-all=tokenizers
    --collect-all=librosa
    --collect-all=soundfile
    --collect-all=onnxruntime
    --collect-all=einops
    --collect-all=sox
  )
fi

"$PYTHON" -m PyInstaller "${PYI_ARGS[@]}" main.py

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

  if [ -x "$BREW_PREFIX/bin/flac" ]; then
    echo
    echo "Using native Homebrew FLAC:"
    echo "$BREW_PREFIX/bin/flac"
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

# Replace <target> with <staged> without ever leaving the machine with no
# working install: the old copy is renamed aside first and restored if the final
# rename fails. (Previously the old install was deleted BEFORE the new one was
# copied, so a failed or interrupted copy left no application at all.)
swap_install() {
  local staged="$1" target="$2" backup="$2.old"

  $SUDO rm -rf "$backup"
  if [ -e "$target" ]; then
    $SUDO mv "$target" "$backup"
  fi
  if ! $SUDO mv "$staged" "$target"; then
    if [ -e "$backup" ]; then
      $SUDO mv "$backup" "$target"
    fi
    echo "ERROR: could not move the new install into place: $target" >&2
    return 1
  fi
  $SUDO rm -rf "$backup"
}

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

  STAGED_APP="/Applications/KubeDock.app.new"
  $SUDO rm -rf "$STAGED_APP"
  # ditto is Apple's recommended way to copy an .app: it keeps bundle metadata
  # and the code signature applied above intact.
  $SUDO ditto "$APP_PATH" "$STAGED_APP"
  swap_install "$STAGED_APP" "/Applications/KubeDock.app"

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

  # Build the new tree next to the old one, then swap (see swap_install).
  STAGED_DIR="/opt/KubeDock.new"
  $SUDO mkdir -p "/opt"
  $SUDO rm -rf "$STAGED_DIR"
  $SUDO cp -a "dist/KubeDock" "$STAGED_DIR"

  # The icon goes into the staged tree so it is swapped in together with the app.
  if [ -n "$DESKTOP_ICON" ] && [ -f "$DESKTOP_ICON" ]; then
    $SUDO mkdir -p "$STAGED_DIR/icon" 2>/dev/null || true
    $SUDO cp "$DESKTOP_ICON" "$STAGED_DIR/icon/KubeDock.ico" 2>/dev/null || true
  fi

  swap_install "$STAGED_DIR" "/opt/KubeDock"

  if [ -d "/usr/local/bin" ] || $SUDO mkdir -p "/usr/local/bin" 2>/dev/null; then
    $SUDO ln -sf "/opt/KubeDock/KubeDock" "/usr/local/bin/kubedeck" 2>/dev/null || true
  fi

  # Only reference an icon file that really exists.
  if [ -f "/opt/KubeDock/icon/KubeDock.ico" ]; then
    DESKTOP_ICON_LINE="Icon=/opt/KubeDock/icon/KubeDock.ico"
  else
    DESKTOP_ICON_LINE="Icon=application-x-executable"
  fi

  if $SUDO mkdir -p "/usr/share/applications" 2>/dev/null; then
    DESKTOP_FILE="$(mktemp)"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=KubeDock
Comment=Kubernetes / VM visualizer and manager
Exec=/opt/KubeDock/KubeDock
$DESKTOP_ICON_LINE
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

  STAGED_DIR="$INSTALL_DIR.new"
  rm -rf "$STAGED_DIR"
  cp -R "dist/KubeDock" "$STAGED_DIR"
  swap_install "$STAGED_DIR" "$INSTALL_DIR"

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

# The temporary build directory is removed by the EXIT trap (cleanup), on
# success and on failure alike.

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
if [[ "$HAVE_PYAUDIO" == "1" ]]; then
  echo " [OK] PyAudio microphone support"
else
  echo " [INFO] PyAudio not installed - microphone voice input is unavailable"
fi
echo " [OK] Native FLAC support"
echo " [OK] AI operation history"
echo " [OK] Current Web App UI"
echo " [OK] Embedded Web App static assets"
if [[ "$ENABLE_QWEN_TTS" == "1" ]]; then
  echo " [OK] Qwen3-TTS runtime bundled (weights download on first launch)"
else
  echo " [INFO] Qwen3-TTS not bundled (set KUBEDOCK_ENABLE_QWEN_TTS=1 to opt in)"
fi
echo