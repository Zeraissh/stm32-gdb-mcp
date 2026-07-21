from __future__ import annotations

import argparse
import glob
import sys
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_PARTS = {
    ".claude",
    ".codegraph",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vs",
    "__pycache__",
    "build",
    "dist",
    "venv",
}


def _parts(name: str) -> tuple[str, ...]:
    return tuple(part for part in name.replace("\\", "/").split("/") if part)


def _is_forbidden(name: str) -> bool:
    parts = _parts(name)
    # Archives normally contain a top-level project directory; inspect everything under it.
    return any(part in FORBIDDEN_PARTS for part in parts[1:])


def _archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    raise ValueError(f"Unsupported distribution archive: {path}")


def expand_archive_args(paths: list[Path]) -> list[Path]:
    expanded = []
    for path in paths:
        text = str(path)
        if any(char in text for char in "*?["):
            matches = sorted(Path(match) for match in glob.glob(text))
            expanded.extend(matches or [path])
        else:
            expanded.append(path)
    return expanded


def find_forbidden_members(paths: list[Path]) -> dict[str, list[str]]:
    found = {}
    for path in paths:
        forbidden = [name for name in _archive_members(path) if _is_forbidden(name)]
        if forbidden:
            found[str(path)] = sorted(forbidden)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit built distributions for local workspace state.")
    parser.add_argument("archives", nargs="+", type=Path, help="sdist/wheel archives to inspect")
    args = parser.parse_args(argv)

    forbidden = find_forbidden_members(expand_archive_args(args.archives))
    if not forbidden:
        print("Distribution content audit passed.")
        return 0

    print("Distribution content audit failed:", file=sys.stderr)
    for archive, members in forbidden.items():
        print(f"- {archive}", file=sys.stderr)
        for member in members:
            print(f"  {member}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
