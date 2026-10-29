from __future__ import annotations

import tempfile
import os
import subprocess
import sys
import unittest
import warnings
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from compat_layer.interface import TranslationResult
from compat_layer.projector import FileProjector


class TinyAdapter:
    old_extension = ".old"
    new_extension = ".new"

    def file_filter(self, filename: str) -> bool:
        return filename.lower().endswith(self.new_extension)

    def get_old_path(self, path: str | Path) -> str:
        return str(Path(path).with_suffix(self.old_extension))

    def get_new_path(self, path: str | Path) -> str:
        return str(Path(path).with_suffix(self.new_extension))

    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        if "unsupported" in content:
            return TranslationResult.fail(["unsupported construct"])
        return TranslationResult.ok("old: " + content)

    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        return TranslationResult.ok(content.removeprefix("old: "))


class FileProjectorSafetyTests(unittest.TestCase):
    def symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

    def junction_or_skip(self, target: Path, link: Path) -> None:
        if os.name != "nt":
            self.skipTest("Windows junctions are only available on Windows")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not link.exists():
            self.skipTest(f"Windows junctions are unavailable: {result.stderr or result.stdout}")

    def test_failed_projection_is_not_fabricated_as_an_old_language_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "report.new"
            target.write_text("unsupported construct", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root)

            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                available = projector.setup()

            shadow = root / ".shadow" / "report.old"
            self.assertFalse(shadow.exists())
            self.assertNotIn(str(shadow), available)
            self.assertNotIn(str(shadow), projector.list_mappings())
            self.assertEqual(projector.setup_failures, [(str(target), ["unsupported construct"])])
            self.assertTrue(any("omitted from the agent view" in str(item.message) for item in seen))

    def test_force_refresh_preserves_unmanaged_shadow_files_and_skips_shadow_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "report.new"
            target.write_text("first", encoding="utf-8")
            shadow_root = root / "agent-view"
            shadow_root.mkdir()
            note = shadow_root / "agent-notes.txt"
            note.write_text("keep my notes", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root, shadow_root)

            projector.setup(force=True)
            target.write_text("second", encoding="utf-8")
            projector.setup(force=True)

            self.assertEqual((shadow_root / "report.old").read_text(encoding="utf-8"), "old: second")
            self.assertEqual(note.read_text(encoding="utf-8"), "keep my notes")
            self.assertEqual(len(projector.list_mappings()), 1)

    def test_shadow_directory_cannot_be_the_real_directory_or_its_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "must not be the real directory or its parent"):
                FileProjector(TinyAdapter(), root, root)
            with self.assertRaisesRegex(ValueError, "must not be the real directory or its parent"):
                FileProjector(TinyAdapter(), root, root.parent)

    def test_removed_targets_are_not_recreated_by_sync_all(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "report.new"
            target.write_text("first", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root)
            projector.setup()
            target.unlink()

            projector.setup(force=True)
            results = projector.sync_all_to_real()

            self.assertEqual(results, [])
            self.assertFalse(target.exists())

    def test_sync_to_real_rejects_a_shadow_path_outside_the_shadow_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.old"
            outside.write_text("old: changed", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root)

            result = projector.sync_to_real(outside)

            self.assertFalse(result.success)
            self.assertIn("outside the shadow directory", " ".join(result.errors))
            self.assertFalse(outside.with_suffix(".new").exists())

    def test_sync_to_shadow_rejects_a_real_path_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.new"
            outside.write_text("outside", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root)

            result = projector.sync_to_shadow(outside)

            self.assertFalse(result.success)
            self.assertIn("outside the workspace", " ".join(result.errors))
            self.assertEqual(projector.list_mappings(), {})

    def test_setup_omits_source_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            link = root / "linked.txt"
            self.symlink_or_skip(outside, link)
            projector = FileProjector(TinyAdapter(), root)

            available = projector.setup()

            self.assertNotIn(str(root / ".shadow" / "linked.txt"), available)
            self.assertNotIn(str(root / ".shadow" / "linked.txt"), projector.list_mappings())
            self.assertEqual(projector.setup_failures[0][0], str(link))
            self.assertEqual(outside.read_text(encoding="utf-8"), "private")

    def test_setup_does_not_recurse_into_a_windows_junction_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            source_dir = root / "src"
            source_dir.mkdir(parents=True)
            (source_dir / "main.new").write_text("source", encoding="utf-8")
            junction = source_dir / "loop"
            self.junction_or_skip(root, junction)
            projector = FileProjector(TinyAdapter(), root)

            try:
                available = projector.setup()

                expected = str(root / ".shadow" / "src" / "main.old")
                self.assertEqual(available, [expected])
                self.assertEqual(list(projector.list_mappings()), [expected])
            finally:
                os.rmdir(junction)

    def test_setup_does_not_copy_through_a_shadow_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            (root / "report.new").write_text("source", encoding="utf-8")
            shadow = root / ".shadow"
            shadow.mkdir()
            outside = Path(temp) / "outside.old"
            outside.write_text("keep", encoding="utf-8")
            self.symlink_or_skip(outside, shadow / "report.old")
            projector = FileProjector(TinyAdapter(), root, shadow)

            projector.setup()

            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertEqual(projector.list_mappings(), {})
            self.assertTrue(projector.setup_failures)

    def test_sync_refuses_a_real_target_replaced_by_an_external_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            target = root / "report.new"
            target.write_text("original", encoding="utf-8")
            projector = FileProjector(TinyAdapter(), root)
            projector.setup()
            shadow = root / ".shadow" / "report.old"
            shadow.write_text("updated", encoding="utf-8")

            target.unlink()
            outside = Path(temp) / "outside.new"
            outside.write_text("must stay unchanged", encoding="utf-8")
            self.symlink_or_skip(outside, target)

            result = projector.sync_to_real(shadow)

            self.assertFalse(result.success)
            self.assertIn("outside the workspace", " ".join(result.errors))
            self.assertEqual(outside.read_text(encoding="utf-8"), "must stay unchanged")


if __name__ == "__main__":
    unittest.main()
