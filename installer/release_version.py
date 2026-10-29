#!/usr/bin/env python3
"""Generate sequential SemVer tags for immutable KubeDock installer releases."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

_VERSION_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version_tag(tag: str) -> tuple[int, int, int] | None:
    """Parse a stable vMAJOR.MINOR.PATCH tag; ignore legacy/random tags."""
    match = _VERSION_RE.fullmatch(tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def next_version(tags: list[str], bump: str = "patch") -> str:
    """Return the next immutable release version, starting with v1.0.0."""
    if bump not in {"patch", "minor", "major"}:
        raise ValueError("bump must be patch, minor, or major")

    parsed = [version for tag in tags if (version := parse_version_tag(tag)) is not None]
    if not parsed:
        return "v1.0.0"

    major, minor, patch = max(parsed)
    if bump == "major":
        return f"v{major + 1}.0.0"
    if bump == "minor":
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def get_existing_tags() -> list[str]:
    """Read local git tags after CI has fetched repository history and tags."""
    result = subprocess.run(
        ["git", "tag", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default=os.environ.get("VERSION_BUMP", "patch"),
    )
    args = parser.parse_args()

    version = next_version(get_existing_tags(), args.bump)
    tag = version
    print(f"Next KubeDock installer release: {tag}")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as output:
            output.write(f"version={version.removeprefix('v')}\n")
            output.write(f"tag={tag}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
