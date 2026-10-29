"""Fail acceptance when the selected interpreter has a stale Rosetta install."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PACKAGES = ("compat_layer", "rosetta_studio")
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
        and path.suffix not in IGNORED_SUFFIXES
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_installed_sync.py RELEASE_ROOT", file=sys.stderr)
        return 2

    release = Path(sys.argv[1]).resolve()
    failures: list[str] = []
    for package in PACKAGES:
        source = release / "src" / package
        spec = importlib.util.find_spec(package)
        if spec is None or spec.origin is None:
            failures.append(f"{package}: not importable in {sys.executable}")
            continue
        installed = Path(spec.origin).resolve().parent
        if not source.is_dir():
            failures.append(f"{package}: source directory missing: {source}")
            continue

        source_files = files_under(source)
        installed_files = files_under(installed)
        if source_files.keys() != installed_files.keys():
            missing = sorted(source_files.keys() - installed_files.keys())
            extra = sorted(installed_files.keys() - source_files.keys())
            failures.append(
                f"{package}: file list differs (missing installed={missing[:5]}, "
                f"extra installed={extra[:5]})"
            )
            continue
        changed = sorted(
            name for name, data in source_files.items()
            if installed_files[name] != data
        )
        if changed:
            failures.append(f"{package}: installed bytes differ: {changed[:8]}")
        else:
            print(f"[SYNC] {package}: {len(source_files)} files match source")

    if failures:
        for failure in failures:
            print(f"[STALE] {failure}", file=sys.stderr)
        return 1
    print(f"[SYNC] selected interpreter: {sys.executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
