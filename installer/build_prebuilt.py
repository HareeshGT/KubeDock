#!/usr/bin/env python3
"""Build the KubeDock onedir bundle used by rolling fast-installer releases.

Dependencies must already be installed into the current Python environment.
The script intentionally does not install system packages or Python packages.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import importlib.util
import json
import os
import platform
import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "KubeDock"
MAC_BUNDLE_ID = "com.hareeshgt.ec2manager"
MICROPHONE_DESCRIPTION = "KubeDock uses the microphone for Kubernetes voice commands."
PACKAGES = (
    "dialogs_parts",
    "kubernetes_tab_parts",
    "dashboard_tab_parts",
    "main_window_parts",
    "workers_parts",
    "k8s_ai_ops_parts",
)


def sync_web_ui() -> None:
    source = ROOT / "webapp" / "static" / "index.html"
    target = ROOT / "webapp" / "static_content.py"
    if not source.is_file():
        raise RuntimeError(f"Web UI source not found: {source}")
    encoded = base64.b64encode(gzip.compress(source.read_bytes(), compresslevel=9)).decode("ascii")
    target.write_text(
        "# Auto-generated PyInstaller fallback.\n"
        "import base64\n"
        "import gzip\n\n"
        "INDEX_HTML = gzip.decompress(base64.b64decode(\n"
        f"    {encoded!r}\n"
        ")).decode(\"utf-8\")\n",
        encoding="utf-8",
    )
    print(f"[OK] Synchronized web UI fallback ({source.stat().st_size:,} bytes)")


def build_args(include_qwen: bool) -> list[str]:
    sep = ";" if os.name == "nt" else ":"
    args = [
        "--noconfirm",
        "--windowed",
        "--onedir",
        "--name",
        APP_NAME,
        "--hidden-import=paramiko",
        "--collect-all=paramiko",
        "--hidden-import=webapp",
        "--hidden-import=webapp.server",
        "--hidden-import=ai_assist",
        "--hidden-import=k8s_ai_ops",
        "--collect-submodules=dialogs_parts",
        "--collect-submodules=kubernetes_tab_parts",
        "--collect-submodules=dashboard_tab_parts",
        "--collect-submodules=main_window_parts",
        "--collect-submodules=workers_parts",
        "--collect-submodules=k8s_ai_ops_parts",
        "--hidden-import=speech_recognition",
        f"--add-data=assets{sep}assets",
        f"--add-data=webapp/static{sep}webapp/static",
    ]
    if importlib.util.find_spec("pyaudio") is not None:
        args.append("--hidden-import=pyaudio")
    if platform.system() == "Darwin":
        args.append(f"--osx-bundle-identifier={MAC_BUNDLE_ID}")
    icon = ROOT / ("KubeDock.ico" if os.name == "nt" else "KubeDock.icns")
    if icon.is_file():
        args.append(f"--icon={icon}")
    if include_qwen:
        args.extend(
            [
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
        )
    return args


def write_build_metadata(bundle: Path, *, include_qwen: bool) -> None:
    source_sha = os.environ.get("GITHUB_SHA", "").strip()
    source_branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not source_sha:
        try:
            source_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            source_sha = "unknown"
    if not source_branch:
        try:
            source_branch = subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            source_branch = "unknown"

    metadata = {
        "application": APP_NAME,
        "source_sha": source_sha,
        "source_branch": source_branch,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "qwen_tts": bool(include_qwen),
        "bundle_format": "onedir",
    }
    if platform.system() == "Darwin":
        resource_dir = bundle / "Contents" / "Resources"
        resource_dir.mkdir(parents=True, exist_ok=True)
        target = resource_dir / "KubeDock-BUILD-METADATA.json"
        plist_path = bundle / "Contents" / "Info.plist"
        if plist_path.is_file():
            with plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            info["CFBundleIdentifier"] = MAC_BUNDLE_ID
            info["NSMicrophoneUsageDescription"] = MICROPHONE_DESCRIPTION
            with plist_path.open("wb") as handle:
                plistlib.dump(info, handle)
    else:
        target = bundle / "KubeDock-BUILD-METADATA.json"
    target.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Build metadata: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-qwen", action="store_true", help="include the optional large Qwen/Torch stack")
    args = parser.parse_args()

    if not (ROOT / "main.py").is_file():
        raise SystemExit(f"Run from a valid KubeDock checkout; main.py not found under {ROOT}")
    for package in PACKAGES:
        if not (ROOT / package / "__init__.py").is_file():
            raise SystemExit(f"Missing application package: {package}")

    sync_web_ui()
    command = [sys.executable, "-m", "PyInstaller", *build_args(args.include_qwen), str(ROOT / "main.py")]
    print("Building KubeDock bundle:")
    print(" ", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)

    if platform.system() == "Darwin":
        bundle = ROOT / "dist" / f"{APP_NAME}.app"
    else:
        bundle = ROOT / "dist" / APP_NAME
    if not bundle.exists():
        raise RuntimeError(f"PyInstaller did not produce the expected bundle: {bundle}")

    write_build_metadata(bundle, include_qwen=args.include_qwen)
    if platform.system() == "Darwin":
        subprocess.run(["codesign", "--deep", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    print(f"[OK] Bundle ready: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
