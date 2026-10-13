#!/bin/bash

set -e

# --------------------------------------------------
# Configuration
# --------------------------------------------------

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: Build this package on macOS."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

INSTALLER="$SCRIPT_DIR/installer.sh"

VERSION="${1:-1.0.0}"
OUTPUT="${2:-$ROOT/dist/KubeDock-$VERSION.pkg}"

REPO_OWNER="HareeshGT"
REPO_NAME="KubeDock"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
BRANCH_API="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/branches?per_page=100"

# --------------------------------------------------
# Validate dependencies
# --------------------------------------------------

if [ ! -f "$INSTALLER" ]; then
  echo "ERROR: installer.sh not found: $INSTALLER"
  exit 1
fi

for cmd in pkgbuild curl python3 osascript; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $cmd"
    exit 1
  fi
done

# --------------------------------------------------
# Prepare temporary package scripts directory
# --------------------------------------------------

TMP="$(mktemp -d /tmp/kubedock-pkg.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

SCRIPTS="$TMP/scripts"

mkdir -p "$SCRIPTS" "$(dirname "$OUTPUT")"

# --------------------------------------------------
# Create postinstall script
# --------------------------------------------------

cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash

set -e

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

REPO_OWNER="HareeshGT"
REPO_NAME="KubeDock"
BRANCH_API="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/branches?per_page=100"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
WORK="$(mktemp -d /tmp/kubedock-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

INSTALLER="$WORK/installer.sh"

if [ ! -f "$SCRIPT_DIR/installer.sh" ]; then
  echo "ERROR: installer.sh is missing from the package."
  exit 1
fi

cp "$SCRIPT_DIR/installer.sh" "$INSTALLER"
chmod 755 "$INSTALLER"

# --------------------------------------------------
# Fetch current GitHub branches at install time
# --------------------------------------------------

echo "Fetching available KubeDock branches..."

BRANCHES_JSON="$(/usr/bin/curl -fsSL \
  --connect-timeout 10 \
  --max-time 30 \
  -H "Accept: application/vnd.github+json" \
  "$BRANCH_API")" || {
    echo "ERROR: Unable to fetch branches from GitHub."
    exit 1
  }

BRANCHES_FILE="$WORK/branches.txt"

printf '%s' "$BRANCHES_JSON" |
  /usr/bin/python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("ERROR: Invalid GitHub API response.", file=sys.stderr)
    sys.exit(1)

if not isinstance(data, list):
    print("ERROR: Unexpected GitHub API response.", file=sys.stderr)
    sys.exit(1)

branches = sorted({
    item["name"]
    for item in data
    if isinstance(item, dict)
    and isinstance(item.get("name"), str)
    and item["name"]
})

if not branches:
    print("ERROR: No GitHub branches found.", file=sys.stderr)
    sys.exit(1)

for branch in branches:
    print(branch)
' > "$BRANCHES_FILE" || {
    echo "ERROR: Failed to parse GitHub branches."
    exit 1
}

if [ ! -s "$BRANCHES_FILE" ]; then
  echo "ERROR: No branches available."
  exit 1
fi

# --------------------------------------------------
# Display dynamic branch selection dialog
# --------------------------------------------------

BRANCH="$(/usr/bin/osascript - "$BRANCHES_FILE" <<'APPLESCRIPT'
on run argv
    set branchFile to item 1 of argv
    set branchNames to paragraphs of (read POSIX file branchFile as «class utf8»)

    if (count of branchNames) is 0 then
        error "No branches available."
    end if

    set defaultBranch to {item 1 of branchNames}

    if branchNames contains "main" then
        set defaultBranch to {"main"}
    end if

    set selectedBranch to choose from list branchNames ¬
        with title "KubeDock Installer" ¬
        with prompt "Select the Git branch to install:" ¬
        default items defaultBranch ¬
        OK button name "Continue" ¬
        cancel button name "Cancel" ¬
        without multiple selections allowed

    if selectedBranch is false then
        error number -128
    end if

    return item 1 of selectedBranch
end run
APPLESCRIPT
)" || {
    echo "Installation cancelled or branch selection failed."
    exit 1
}

if [ -z "$BRANCH" ]; then
  echo "ERROR: No branch selected."
  exit 1
fi

echo "Selected branch: $BRANCH"

# --------------------------------------------------
# Run the actual installer
# --------------------------------------------------

export KUBEDECK_PKG_MODE=1
export KUBEDECK_BRANCH="$BRANCH"

# Ensure installer.sh uses KUBEDECK_BRANCH when cloning.
/bin/bash "$INSTALLER"
POSTINSTALL

# --------------------------------------------------
# Include installer.sh in package scripts
# --------------------------------------------------

cp "$INSTALLER" "$SCRIPTS/installer.sh"

chmod 755 \
  "$SCRIPTS/postinstall" \
  "$SCRIPTS/installer.sh"

# --------------------------------------------------
# Build package
# --------------------------------------------------

rm -f "$OUTPUT"

ARGS=(
  --nopayload
  --scripts "$SCRIPTS"
  --identifier com.hareeshgt.kubedock.installer
  --version "$VERSION"
)

if [ -n "${PKG_SIGN_IDENTITY:-}" ]; then
  ARGS+=(--sign "$PKG_SIGN_IDENTITY")
fi

pkgbuild "${ARGS[@]}" "$OUTPUT"

echo
echo "=========================================="
echo " KubeDock package created successfully"
echo "=========================================="
echo
echo "Version : $VERSION"
echo "Package : $OUTPUT"
echo
echo "Available branches are fetched at install time."
