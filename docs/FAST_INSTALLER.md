# KubeDock Fast Installer

## What changes for users

The standard installer now prefers the rolling prebuilt release named
`fast-installer`. A compatible install downloads and copies a built application
bundle instead of installing Python dependencies and running PyInstaller on
the target computer.

The source-build path remains available for users who need a specific branch or
the optional Qwen3-TTS/Torch runtime.

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

On pushes to `main`, successful platform builds are assembled and published as
assets on the rolling GitHub prerelease `fast-installer`. The tag is moved to
the commit used for the latest assets. Pull requests build and validate the
platform bundles but do not publish or update the release.

Linux ARM64 does not currently have a prebuilt bundle because PyQt5 falls back to a source build without an available wheel. In `auto` mode, Linux ARM64 therefore uses the existing source-build fallback; `release` mode exits with an explanatory message.\n\nAssets include build metadata and a checksum file. These are application
bundles, not signed/notarized consumer installers. macOS bundles are ad-hoc
signed, not Developer ID signed/notarized.

## Optional features

The fast bundle excludes Qwen3-TTS, PyTorch, Transformers, and downloaded model
weights. Users who require this large local model runtime should use source mode
with `KUBEDOCK_ENABLE_QWEN_TTS=1`. Some media fallback workflows may need native
`ffmpeg` and `flac` executables; the fast installer warns if those are absent
on Linux rather than blocking the app installation.

## Validation

The fast-installer workflow runs the bundle builder on each platform and checks
the metadata file exists before publishing. A separate syntax job validates
the shell and Python installer files and runs focused builder-flag tests.
GitHub Actions is the first place to verify actual platform packaging; the
target OS still needs a smoke test before publishing a stable version.
