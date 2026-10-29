# KubeDock Fast Installer

## What changes for users

The standard installer now prefers the rolling prebuilt release named
`fast-installer`. A compatible install downloads and copies a built application
bundle instead of installing Python dependencies and running PyInstaller on
the target computer.

The source-build path remains available for users who need a specific branch or
the optional Qwen3-TTS/Torch runtime. PyInstaller is installed from
`requirements-build.txt`, not the core runtime dependency list.

## Install modes

- `KUBEDOCK_INSTALL_MODE=auto` (default): try the prebuilt release, then use the source installer if no compatible bundle is published.
- `KUBEDOCK_INSTALL_MODE=release`: require the prebuilt release; do not fall back to a slow source build.
- `KUBEDOCK_INSTALL_MODE=source`: select/build a branch with the existing installer.

Passing a branch argument to `installer/installer.sh` selects source mode for
that branch. The Windows Python installer follows the same environment variable.
The macOS package wizard presents fast install first and retains an explicit
source-build option.

## Published bundles

`.github/workflows/fast-installer-bundles.yml` builds the application for:

- Linux x86-64
- macOS Apple Silicon
- macOS Intel
- Windows x86-64

Scheduled or manually triggered deployments publish immutable, sequential
semantic-version releases: `v1.0.0`, `v1.0.1`, `v1.0.2`, and so on. Run IDs
and commit hashes are no longer embedded in release tags. The first version is
`v1.0.0` when no semantic releases exist; subsequent runs increment the
patch version by default.

For a manual workflow dispatch, choose a `patch`, `minor`, or `major` bump.
For example, a minor bump after `v1.2.4` produces `v1.3.0`, and a major bump
produces `v2.0.0`. Releases are immutable: if publishing a version fails, do
not reuse or overwrite its tag; correct the failure and run the workflow again
so it allocates the next version. The workflow marks the newly published
version as the latest stable release, so the installers' `releases/latest`
downloads keep working.

Existing historical `fast-installer-<hash>-<run-id>` releases are left intact.
New deployments no longer create releases with those random-looking names.
The optional workflow schedules are retained; each successful scheduled or
manual deployment receives the next semantic version.

Linux ARM64 does not currently have a prebuilt bundle because PyQt5 falls back to a source build without an available wheel. In `auto` mode, Linux ARM64 therefore uses the existing source-build fallback; `release` mode exits with an explanatory message.

Assets include build metadata and a checksum file. These are application
bundles, not signed/notarized consumer installers. macOS bundles are ad-hoc
signed, not Developer ID signed/notarized.

## SQLite data directory

KubeDock continues to use the existing `~/.kubedock/kubedock.db` location when
it can be opened. If that directory is unavailable or read-only in a frozen
install, the application tries the platform's per-user application-data path.
When possible, it copies the existing SQLite database to the fallback location
before continuing. Legacy `settings.json` migration continues to read from the
original profile.

To choose an explicit writable directory, set `KUBEDOCK_DATA_DIR` before
launching KubeDock. The directory should belong to the current user; do not
point multiple users at the same database.

## Optional features

The fast bundle excludes Qwen3-TTS, PyTorch, Transformers, downloaded model
weights, and Qt WebEngine/Chromium. The normal built-in remote editor remains
available without WebEngine; the Monaco editor can be installed optionally with
`pip install -r requirements-editor.txt`, or included in a source build with
`KUBEDOCK_ENABLE_WEBENGINE=1`.

Users who require the large local Qwen model runtime should use source mode with
`KUBEDOCK_ENABLE_QWEN_TTS=1`. Some media fallback workflows may need native
`ffmpeg` and `flac` executables; the fast installer warns if those are absent
on Linux rather than blocking the app installation.

## Validation

The fast-installer workflow runs the bundle builder on each platform and checks
the metadata file exists before publishing. A separate syntax job validates
the shell and Python installer files and runs focused builder-flag tests.
GitHub Actions is the first place to verify actual platform packaging; the
target OS still needs a smoke test before publishing a stable version.
