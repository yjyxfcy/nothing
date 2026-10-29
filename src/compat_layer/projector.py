"""File projection engine — maintains bidirectional sync between
real (new-format) and shadow (old-format) directories.

This is the core mechanism that makes the compatibility layer work:

    REAL DIRECTORY (human)          SHADOW DIRECTORY (agent)
    ┌─────────────────────┐         ┌──────────────────────────┐
    │ paper.typ           │ ←new2old │ paper.tex               │
    │ query.prql          │ ───────→ │ query.sql               │
    │ script.nu           │ ←old2new │ script.sh               │
    │ figures/            │         │ figures/  (mirrored)     │
    │ refs.bib            │         │ refs.bib  (mirrored)     │
    └─────────────────────┘         └──────────────────────────┘

The agent works entirely within the shadow directory. It never sees the
new-format files and never needs to know the new tool exists.

The human works entirely within the real directory. They never see the
old-format shadow files (which are in a hidden .shadow/ subdirectory).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Callable

from .interface import ToolAdapter, TranslationResult


def _resolves_under(path: Path, root: Path) -> bool:
    """Return whether a path, including existing links, stays under ``root``."""
    try:
        resolved_root = root.resolve()
        return path.resolve(strict=False).is_relative_to(resolved_root)
    except (OSError, RuntimeError):
        return False


def _is_linklike(path: Path) -> bool:
    """Detect symlinks and Windows junctions across supported Python versions."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    # Path.is_junction() was added after the minimum supported Python version.
    # On Windows 3.10/3.11, os.walk can still descend into directory junctions
    # with followlinks=False, so inspect the reparse tag directly. Restrict this
    # fallback to the mount-point tag used by junctions; cloud placeholders are
    # reparse points too, but should remain ordinary workspace files.
    if os.name == "nt":
        try:
            return getattr(path.lstat(), "st_reparse_tag", None) == 0xA0000003
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# FileProjector
# ---------------------------------------------------------------------------


class FileProjector:
    """Maintains a shadow directory that projects old-format files
    onto new-format real files.

    The shadow directory is always a subdirectory of the real directory
    (default: `<real_dir>/.shadow/`). This keeps the relative path structure
    identical, so the agent sees the same directory tree as the human —
    just with different file extensions and translated content.

    Usage::

        adapter = PRQLAdapter()
        projector = FileProjector(
            adapter=adapter,
            real_dir=Path("./workspace"),
        )

        # Step 1: Set up the shadow directory
        projector.setup()
        # Now ./workspace/.shadow/ contains .sql versions

        # Step 2: Agent works in the shadow directory
        agent.run(projector.shadow_dir)
        # Agent reads query.sql, writes query.sql, validates with sqlite3

        # Step 3: Sync agent's changes back to real files
        results = projector.sync_all_to_real()
        # Now query.prql in the real directory contains the translated PRQL
    """

    # Name of the shadow subdirectory
    SHADOW_DIR_NAME = ".shadow"

    def __init__(
        self,
        adapter: ToolAdapter,
        real_dir: str | Path,
        shadow_dir: str | Path | None = None,
        *,
        on_before_translate: Callable[[str, str], None] | None = None,
        on_after_translate: Callable[[str, TranslationResult], None] | None = None,
    ):
        """Initialize a file projector.

        Args:
            adapter: The tool adapter that translates between formats.
            real_dir: Root directory containing real (new-format) files.
            shadow_dir: Where to create the shadow directory. Defaults to
                        `<real_dir>/.shadow/`.
            on_before_translate: Optional callback(path, direction) called
                before each translation. Useful for logging/profiling.
            on_after_translate: Optional callback(path, result) called
                after each translation.
        """
        self.adapter = adapter
        self.real_dir = Path(real_dir).resolve()
        self.shadow_dir = Path(shadow_dir or self.real_dir / self.SHADOW_DIR_NAME).resolve()

        # The shadow tree may live beside or below the real tree, but must
        # never be the real tree itself or one of its parents. In particular,
        # ``setup(force=True)`` must not be able to remove or populate the
        # user's source directory by mistake.
        if self.shadow_dir == self.real_dir or self.shadow_dir in self.real_dir.parents:
            raise ValueError("shadow_dir must not be the real directory or its parent")

        # shadow_path (str) → real_path (str)
        self._file_map: dict[str, str] = {}

        # Hooks
        self._on_before_translate = on_before_translate
        self._on_after_translate = on_after_translate

        # Statistics
        self.stats = ProjectorStats()
        # Failed projections are omitted from the agent view rather than
        # mirrored under a familiar extension, which would misrepresent target
        # syntax as valid input for the old tool.
        self.setup_failures: list[tuple[str, list[str]]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setup(self, *, force: bool = False) -> list[str]:
        """Initialize the shadow directory.

        Scans the real directory recursively:
        - Files matching `adapter.file_filter()` are translated (new→old)
          and written to the shadow directory with the old extension.
        - All other files are mirrored (copied) as-is.
        - Directory structure is preserved.

        Args:
            force: If True, re-translate all files even if shadow copies
                   already exist. This overwrites managed shadow projections
                   and mirrored dependency files, so save any agent edits to
                   those files first. Files not managed by this projector are
                   left untouched; the shadow directory itself is never
                   recursively deleted.

        Returns:
            List of shadow file paths that are available to the agent. Files
            whose projection failed are excluded and recorded in
            ``setup_failures``.
        """
        created: list[str] = []
        self.setup_failures = []
        self._file_map.clear()

        self.shadow_dir.mkdir(parents=True, exist_ok=True)

        for root, dirs, files in os.walk(self.real_dir):
            # Never recurse into the selected shadow directory. Comparing the
            # resolved path also handles a custom nested directory and symlink
            # aliases; hard-coding the default name missed both cases.
            root_path = Path(root).resolve()
            if root_path == self.shadow_dir:
                dirs[:] = []
                continue
            dirs[:] = [
                name for name in dirs
                if not _is_linklike(root_path / name)
                and _resolves_under(root_path / name, self.real_dir)
                and (root_path / name).resolve() != self.shadow_dir
            ]

            # Skip common VCS / cache directories
            for skip_dir in (".git", "__pycache__", ".venv", "node_modules", ".claude"):
                if skip_dir in dirs:
                    dirs.remove(skip_dir)

            for filename in files:
                real_path = Path(root) / filename
                if _is_linklike(real_path) or not _resolves_under(real_path, self.real_dir):
                    self.setup_failures.append((
                        str(real_path),
                        ["Symbolic links and paths resolving outside the workspace are omitted"],
                    ))
                    continue
                rel_path = real_path.relative_to(self.real_dir)

                if self.adapter.file_filter(filename):
                    # ---- Translatable file (new format) ----
                    shadow_path = self._make_shadow_path(rel_path)

                    if not _resolves_under(shadow_path, self.shadow_dir):
                        self.setup_failures.append((
                            str(real_path),
                            ["The shadow destination resolves outside the shadow directory"],
                        ))
                        continue

                    if not force and shadow_path.exists():
                        created.append(str(shadow_path))
                        self._file_map[str(shadow_path)] = str(real_path)
                        continue

                    result = self._translate_file(
                        real_path, shadow_path, direction="new_to_old"
                    )
                    if result.success:
                        created.append(str(shadow_path))
                    else:
                        # Never label target-language source as a familiar
                        # language file. A plausible-looking but invalid shadow
                        # file can be edited by an agent and later translated
                        # back as if it were valid old-language input.
                        self._file_map.pop(str(shadow_path), None)
                        self.setup_failures.append((str(real_path), list(result.errors)))
                        _warn(
                            f"new_to_old failed for {real_path}: {result.errors}; "
                            "the file was omitted from the agent view"
                        )
                else:
                    # ---- Non-translatable file (images, data, etc.) ----
                    shadow_path = self.shadow_dir / rel_path
                    if not _resolves_under(shadow_path, self.shadow_dir):
                        self.setup_failures.append((
                            str(real_path),
                            ["The shadow destination resolves outside the shadow directory"],
                        ))
                        continue
                    if not shadow_path.exists() or force:
                        if not self._mirror_file(real_path, rel_path, shadow_path):
                            self.setup_failures.append((
                                str(real_path),
                                ["The source or shadow destination resolves outside its workspace"],
                            ))
                            continue
                    created.append(str(shadow_path))
                    self._file_map[str(shadow_path)] = str(real_path)

        return created

    def sync_to_real(self, shadow_path: str | Path) -> TranslationResult:
        """Sync a single shadow file's changes back to its real counterpart.

        Reads the shadow file (old format), translates it to new format,
        and writes the result to the real file.

        Args:
            shadow_path: Path to the shadow file (absolute or relative to cwd).

        Returns:
            TranslationResult indicating success/failure.
        """
        sp = Path(shadow_path)
        if not sp.is_absolute():
            sp = Path.cwd() / sp
        sp = Path(os.path.abspath(sp))
        if not _resolves_under(sp, self.shadow_dir):
            return TranslationResult.fail([f"Shadow path is outside the shadow directory: {shadow_path}"])
        if not sp.exists():
            return TranslationResult.fail([f"Shadow file not found: {shadow_path}"])
        if _is_linklike(sp):
            return TranslationResult.fail(["Symbolic links are not accepted as shadow artifacts"])

        real_path_str = self._file_map.get(str(sp))
        if real_path_str is None:
            try:
                real_path_str = self._infer_real_path(sp)
            except ValueError as exc:
                return TranslationResult.fail([str(exc)])
        rp = Path(real_path_str)
        if _is_linklike(rp) or not _resolves_under(rp, self.real_dir):
            return TranslationResult.fail([f"Real target resolves outside the workspace: {rp}"])

        old_content = sp.read_text(encoding="utf-8")

        # Notify hooks
        if self._on_before_translate:
            self._on_before_translate(str(sp), "old_to_new")

        result = self.adapter.old_to_new(old_content, str(sp))

        if self._on_after_translate:
            self._on_after_translate(str(sp), result)

        if result.success:
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(result.content, encoding="utf-8")
            self.stats.synced_to_real += 1
            self.stats.bytes_translated += len(old_content)
        else:
            self.stats.failed_syncs += 1

        return result

    def sync_all_to_real(self) -> list[TranslationResult]:
        """Sync ALL shadow files back to real files.

        Walks the entire shadow directory and calls sync_to_real() on
        every file that has a registered mapping.

        Returns:
            List of TranslationResult, one per file.
        """
        results: list[TranslationResult] = []
        for root, _dirs, files in os.walk(self.shadow_dir):
            for filename in files:
                sp = Path(root) / filename
                if str(sp) in self._file_map:
                    results.append(self.sync_to_real(str(sp)))
        return results

    def sync_to_shadow(self, real_path: str | Path) -> TranslationResult:
        """Sync a real file's changes back to its shadow counterpart.

        Use this when the human modifies a real file and the agent
        needs to see the latest version.

        Args:
            real_path: Path to the real file.

        Returns:
            TranslationResult indicating success/failure.
        """
        rp = Path(real_path)
        if not rp.is_absolute():
            rp = Path.cwd() / rp
        rp = Path(os.path.abspath(rp))
        if _is_linklike(rp) or not _resolves_under(rp, self.real_dir):
            return TranslationResult.fail([f"Real path is outside the workspace or is a link: {real_path}"])
        if not rp.exists():
            return TranslationResult.fail([f"Real file not found: {real_path}"])

        # Find or infer the shadow path
        shadow_path_str: str | None = None
        for s, r in self._file_map.items():
            if r == str(rp):
                shadow_path_str = s
                break

        if shadow_path_str is None:
            # Try to infer from extension
            rel = rp.relative_to(self.real_dir)
            shadow_path = self._make_shadow_path(rel)
        else:
            shadow_path = Path(shadow_path_str)

        result = self._translate_file(rp, shadow_path, direction="new_to_old")
        if result.success:
            self._file_map[str(shadow_path)] = str(rp)
        return result

    def get_real_path(self, shadow_path: str | Path) -> str:
        """Look up the real path corresponding to a shadow path."""
        path = Path(shadow_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = Path(os.path.abspath(path))
        if not _resolves_under(path, self.shadow_dir):
            raise ValueError(f"Shadow path is outside the shadow directory: {shadow_path}")
        real_path = self._file_map.get(str(path))
        if real_path is not None:
            if not _resolves_under(Path(real_path), self.real_dir):
                raise ValueError(f"Mapped real path is outside the workspace: {real_path}")
            return real_path
        return self._infer_real_path(path)

    def list_mappings(self) -> dict[str, str]:
        """Return a copy of the shadow→real file mapping."""
        return dict(self._file_map)

    def diff(self) -> list[str]:
        """List shadow files that differ from their real counterparts.

        Returns paths (relative to shadow_dir) of files that need syncing.
        """
        changed: list[str] = []
        for shadow_str, real_str in self._file_map.items():
            sp = Path(shadow_str)
            rp = Path(real_str)
            if not _resolves_under(sp, self.shadow_dir) or not _resolves_under(rp, self.real_dir):
                changed.append(str(sp.relative_to(self.shadow_dir)))
                continue
            if not sp.exists() or not rp.exists():
                changed.append(str(sp.relative_to(self.shadow_dir)))
                continue

            # For translatable files, compare old content vs re-translated
            if sp.suffix.lower() == self.adapter.old_extension.lower():
                old_content = sp.read_text(encoding="utf-8")
                real_content = rp.read_text(encoding="utf-8")
                # Translate real→old and compare with shadow
                re_translated = self.adapter.new_to_old(real_content, str(rp))
                if re_translated.success and re_translated.content != old_content:
                    changed.append(str(sp.relative_to(self.shadow_dir)))
            else:
                # For mirrored files, compare directly
                if sp.read_bytes() != rp.read_bytes():
                    changed.append(str(sp.relative_to(self.shadow_dir)))
        return changed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_shadow_path(self, rel_path: Path) -> Path:
        """Convert a relative real path to its shadow equivalent."""
        old_name = self.adapter.get_old_path(rel_path.name)
        return self.shadow_dir / rel_path.parent / Path(old_name).name

    def _translate_file(
        self,
        real_path: Path,
        shadow_path: Path,
        direction: str,
    ) -> TranslationResult:
        """Translate content between a real file and its shadow counterpart."""
        if not _resolves_under(real_path, self.real_dir):
            self.stats.failed_translations += 1
            return TranslationResult.fail([f"Source path resolves outside the workspace: {real_path}"])
        if not _resolves_under(shadow_path, self.shadow_dir):
            self.stats.failed_translations += 1
            return TranslationResult.fail([f"Shadow destination resolves outside the shadow directory: {shadow_path}"])
        content = real_path.read_text(encoding="utf-8")

        if self._on_before_translate:
            self._on_before_translate(str(real_path), direction)

        if direction == "new_to_old":
            result = self.adapter.new_to_old(content, str(real_path))
        else:
            result = self.adapter.old_to_new(content, str(real_path))

        if self._on_after_translate:
            self._on_after_translate(str(real_path), result)

        if result.success:
            shadow_path.parent.mkdir(parents=True, exist_ok=True)
            shadow_path.write_text(result.content, encoding="utf-8")
            self._file_map[str(shadow_path)] = str(real_path)
            self.stats.files_translated += 1
            self.stats.bytes_translated += len(content)
        else:
            self.stats.failed_translations += 1

        return result

    def _mirror_file(self, real_path: Path, rel_path: Path, shadow_path: Path) -> bool:
        """Copy a non-translatable file as-is to the shadow directory."""
        if not _resolves_under(real_path, self.real_dir):
            return False
        if not _resolves_under(shadow_path, self.shadow_dir):
            return False
        shadow_path.parent.mkdir(parents=True, exist_ok=True)
        # For large files, use copy2 to preserve metadata
        if not shadow_path.exists() or (
            real_path.stat().st_mtime > shadow_path.stat().st_mtime
        ):
            shutil.copy2(real_path, shadow_path)
        self._file_map[str(shadow_path)] = str(real_path)
        self.stats.files_mirrored += 1
        return True

    def _infer_real_path(self, shadow_path: Path) -> str:
        """Infer the real path from a shadow path when no mapping exists."""
        if not _resolves_under(shadow_path, self.shadow_dir):
            raise ValueError(f"Shadow path is outside the shadow directory: {shadow_path}")
        try:
            rel = shadow_path.relative_to(self.shadow_dir)
        except ValueError:
            raise ValueError(f"Shadow path is outside the shadow directory: {shadow_path}")

        new_name = self.adapter.get_new_path(rel.name)
        real_path = self.real_dir / rel.parent / Path(new_name).name
        if not _resolves_under(real_path, self.real_dir):
            raise ValueError(f"Inferred real path is outside the workspace: {real_path}")
        return str(real_path)


# ---------------------------------------------------------------------------
# ProjectorStats
# ---------------------------------------------------------------------------


class ProjectorStats:
    """Accumulated statistics for a FileProjector instance."""

    def __init__(self):
        self.files_translated: int = 0
        self.files_mirrored: int = 0
        self.failed_translations: int = 0
        self.synced_to_real: int = 0
        self.failed_syncs: int = 0
        self.bytes_translated: int = 0

    @property
    def total_files(self) -> int:
        return self.files_translated + self.files_mirrored

    def reset(self):
        for attr in self.__dict__:
            setattr(self, attr, 0)

    def __repr__(self) -> str:
        return (
            f"ProjectorStats(translated={self.files_translated}, "
            f"mirrored={self.files_mirrored}, "
            f"failed_trans={self.failed_translations}, "
            f"synced={self.synced_to_real}, "
            f"failed_sync={self.failed_syncs}, "
            f"bytes={self.bytes_translated})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _warn(msg: str):
    """Emit a warning without pulling in the warnings module."""
    import warnings
    warnings.warn(msg, stacklevel=3)
