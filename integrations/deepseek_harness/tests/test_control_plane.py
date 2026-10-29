"""Control-plane tests for the two protections the demo depends on.

Both are about *not* acting on something the layer already knows is bad:

1. a **failed preview** must not leave the previous preview in place. The commit
   path reuses a stored preview whenever the view has not changed since, so a
   stale preview would be committed as though it were the delivery the caller just
   asked for - immediately after a preview that refused to produce one.

2. a delivery the adapters report as **not compiling** must never reach the disk,
   and the verdict must still be visible when the commit runs from a stored
   preview, where no fresh translation happens.

Run:  <python> -m unittest tests.test_control_plane -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"
BASE = INTEGRATION / "demo_workspace"

TYPST = "= Control plane\n\nBody text.\n\n#show heading: set text(fill: blue)\n"

# The delivery result type comes from the installed distribution, not from the
# bridge module: the bridge imports the conversion layer lazily and deliberately
# does not re-export its types.
from compat_layer.interface import TranslationResult  # noqa: E402


def load_bridge():
    spec = importlib.util.spec_from_file_location("rosetta_bridge_control", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProtocolUnicodeTests(unittest.TestCase):
    def test_json_wire_output_preserves_cjk_independent_of_console_encoding(self):
        bridge = load_bridge()
        expected = "\u7f57\u5854\u5192\u70df\u9a8c\u6536\uff1a\u4e2d\u6587\u6b63\u5e38"
        output = io.StringIO()
        with redirect_stdout(output):
            bridge.emit_json({"agentView": expected})

        wire = output.getvalue()
        self.assertTrue(wire.isascii(), "bridge protocol output must not depend on Windows codepage")
        self.assertEqual(json.loads(wire)["agentView"], expected)

    def test_subprocess_stdin_decodes_utf8_json_without_windows_codepage_damage(self):
        with tempfile.TemporaryDirectory(prefix="protocol-unicode-", dir=str(BASE)) as root:
            command = json.dumps({"command": "\u4e2d\u6587-probe"}, ensure_ascii=False)
            store = Path(root) / ".rosetta" / "sessions.sqlite3"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE),
                    "--allow-root",
                    root,
                    "--store",
                    str(store),
                    "--no-events",
                ],
                input=command.encode("utf-8"),
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 3, completed.stderr.decode("utf-8", "replace"))
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "UNKNOWN_COMMAND")
        self.assertIn("Unknown bridge command '中文-probe'.", payload["error"]["message"])


class ControlPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def setUp(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.ws = Path(tempfile.mkdtemp(prefix="control-", dir=str(BASE)))
        self.roots = self.bridge.Roots([str(self.ws)])
        self.bridge.ensure_usable_temp(self.roots)
        self.target = self.ws / "doc.typ"
        self.target.write_text(TYPST, encoding="utf-8")
        self.store = self.bridge.Store(self.ws / ".rosetta" / "s.sqlite3")
        self.app = self.bridge.Rosetta(self.roots, self.store, dict(os.environ))
        self.app.cmd_open("s", "LaTeX / Typst", str(self.target))
        view = self.app.cmd_read("s")["agentView"]
        self.app.cmd_write("s", view + "\n\\section{ Added}\n\nnew prose.\n")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.ws, ignore_errors=True)

    def record(self):
        return self.store.get("s")

    def patch_deliver(self, replacement):
        """Replace this session's delivery engine for the duration of one test.

        Patched on the pair *instance* rather than its class: the bridge builds
        pairs through `rosetta_studio.core`, and reaching for the class by name
        would couple this test to an internal the bridge deliberately does not
        import. The instance is cached by the bridge, so the replacement is what
        every command in the test sees.
        """
        pair = self.app.pair("LaTeX / Typst")
        original = pair.deliver
        pair.deliver = replacement
        self.addCleanup(lambda: setattr(pair, "deliver", original))

    @staticmethod
    def failing_delivery(message="simulated delivery failure"):
        def deliver(content, filepath=""):
            return TranslationResult.fail([message])

        return deliver

    @staticmethod
    def raising_delivery(message="simulated adapter crash"):
        def deliver(content, filepath=""):
            raise RuntimeError(message)

        return deliver

    @staticmethod
    def delivery_reporting(compiles, method="pipeline:stub", passthrough=None, text="= Stub delivery.\n"):
        def deliver(content, filepath=""):
            kwargs = {"method": method, "compiles": compiles}
            if passthrough is not None:
                kwargs["passthrough"] = passthrough
            return TranslationResult.ok(text, **kwargs)

        return deliver

    @unittest.skipUnless(shutil.which('typst'), 'requires Typst compiler')
    def test_project_relative_include_can_be_previewed_without_writing_target(self):
        (self.ws / 'chapter.typ').write_text('= Chapter\nProject content.\n', encoding='utf-8')
        candidate = '#include "chapter.typ"\n\n= Updated\nBody.\n'
        self.patch_deliver(self.delivery_reporting(False, text=candidate))
        before = self.target.read_bytes()
        preview = self.app.cmd_preview('s')
        self.assertIn('#include "chapter.typ"', preview['targetContent'])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(self.record()['previewMetadata']['compiles'])
        self.assertEqual(list(self.ws.glob('.rosetta-check-*.typ')), [])

    @unittest.skipUnless(shutil.which('typst'), 'requires Typst compiler')
    def test_missing_project_include_remains_refused(self):
        self.patch_deliver(self.delivery_reporting(False, text='#include "missing.typ"\n'))
        before = self.target.read_bytes()
        with self.assertRaises(self.bridge.BridgeError) as raised:
            self.app.cmd_preview('s')
        self.assertEqual(raised.exception.code, 'DELIVERY_DOES_NOT_COMPILE')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.ws.glob('.rosetta-check-*.typ')), [])

    # -- 1. a failed preview must not leave a commit token --------------------

    def test_a_failed_preview_clears_the_previous_preview(self):
        ok = self.app.cmd_preview("s")
        self.assertTrue(ok["targetContent"])
        self.assertIsNotNone(self.record()["preview"], "the first preview was not stored")

        # Now make the delivery fail, the way an unsupported construct does.
        self.patch_deliver(self.failing_delivery())
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_preview("s")
        self.assertEqual(caught.exception.code, "DELIVERY_FAILED")

        record = self.record()
        self.assertIsNone(
            record["preview"],
            "a failed preview left the previous preview in place, which the next "
            "commit would have reused as the approved delivery",
        )
        self.assertIsNone(record["previewViewSha256"], "the reuse token was not cleared")
        self.assertEqual(record["previewMetadata"], {})

    def test_a_failed_preview_cannot_be_committed_as_the_old_result(self):
        self.app.cmd_preview("s")
        stale_preview = self.record()["preview"]
        self.assertIsNotNone(stale_preview)

        before = self.target.read_text(encoding="utf-8")
        self.patch_deliver(self.failing_delivery())
        with self.assertRaises(self.bridge.BridgeError):
            self.app.cmd_preview("s")

        # The commit must not fall back to the discarded preview.
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "DELIVERY_FAILED")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), before,
            "the discarded preview reached the disk",
        )
        self.assertNotEqual(self.target.read_text(encoding="utf-8"), stale_preview)

    def test_an_adapter_exception_is_a_structured_delivery_refusal(self):
        """A converter exception must not surface as a bridge crash.

        Some reverse adapters raise instead of returning a failed result. The
        bridge used to pass that through as ``BRIDGE_CRASH``, which reads like
        the bridge died and hides the fact that the target was untouched.
        """
        before = self.target.read_text(encoding="utf-8")
        self.patch_deliver(self.raising_delivery())
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_preview("s")
        self.assertEqual(caught.exception.code, "DELIVERY_FAILED")
        message = str(caught.exception)
        self.assertIn("RuntimeError", message)
        self.assertIn("Nothing was written", message)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), before,
            "an adapter exception reached the target",
        )
        self.assertIsNone(self.record()["preview"], "the preview token was not cleared")

    # -- 2. a known compile failure must not be written -----------------------

    def test_a_known_compile_failure_is_not_written(self):
        before = self.target.read_text(encoding="utf-8")
        self.patch_deliver(self.delivery_reporting(False, text="= Compiles? no.\n"))
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "DELIVERY_DOES_NOT_COMPILE")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), before,
            "a delivery the adapters reported as not compiling was written",
        )
        self.assertIsNone(self.record()["preview"], "the preview was not discarded")

    def test_a_known_compile_failure_is_refused_at_preview_too(self):
        self.patch_deliver(self.delivery_reporting(False, text="= Compiles? no.\n"))
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_preview("s")
        self.assertEqual(caught.exception.code, "DELIVERY_DOES_NOT_COMPILE")

    def test_the_commit_sees_the_verdict_stored_with_the_preview(self):
        """The reuse path runs no fresh translation, so it must read the verdict.

        Stored with compiles=True, then flipped to False in the store: if the
        commit re-read the metadata it will refuse, which is the property under
        test - the verdict travels with the preview rather than being lost.
        """
        self.patch_deliver(self.delivery_reporting(True, passthrough=1, text="= Delivered by the stub.\n"))
        preview = self.app.cmd_preview("s")
        self.assertTrue(preview["targetContent"])
        self.assertEqual(self.record()["previewMetadata"].get("compiles"), True)

        # Flip only the stored verdict: no re-translation happens on the commit
        # path, so a commit that refuses can only have read it from the store.
        with self.store.transaction() as tx:
            tx.update("s", previewMetadata={"method": "pipeline:stub", "compiles": False})
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "DELIVERY_DOES_NOT_COMPILE")

    def test_a_reused_preview_reports_the_stored_backend(self):
        self.patch_deliver(self.delivery_reporting(True, passthrough=2, text="= Delivered by the stub.\n"))
        self.app.cmd_preview("s")
        result = self.app.cmd_commit("s")
        notes = " ".join(result["notes"])
        self.assertIn(
            "pipeline:stub", notes,
            "the commit from a stored preview lost the backend the preview reported",
        )
        self.assertIn("2 pass-through", notes)
        self.assertTrue(result["written"])

    def test_the_unknown_verdict_is_not_treated_as_failure(self):
        """`compiles` absent means 'not reported', not 'failed'."""
        self.assertIsNone(self.bridge._compile_status({}))
        self.assertIsNone(self.bridge._compile_status({"method": "pipeline:x"}))
        self.assertIs(self.bridge._compile_status({"compiles": False}), False)
        self.assertIs(self.bridge._compile_status({"compiles": True}), True)
        # The nested shape a wrapper may produce is read too.
        self.assertIs(self.bridge._compile_status({"metadata": {"compiles": False}}), False)
        normal = self.app.cmd_commit("s")
        self.assertTrue(normal["written"], "a delivery without a verdict was refused")


class UsabilityTests(unittest.TestCase):
    """Guards for the two behaviours that decide whether a newcomer can use this.

    Both came out of a usability review rather than a bug report:

    * a commit that was never previewed rewrote a file the user had not seen, even
      though every tool description said to preview first;
    * a finished-but-forgotten session kept its target fenced for the whole
      retention period, so the user's own editor was refused on a file Rosetta was
      no longer doing anything with.
    """

    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def setUp(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.ws = Path(tempfile.mkdtemp(prefix="usability-", dir=str(BASE)))
        self.roots = self.bridge.Roots([str(self.ws)])
        self.bridge.ensure_usable_temp(self.roots)
        self.target = self.ws / "doc.typ"
        self.target.write_text(TYPST, encoding="utf-8")
        self.store = self.bridge.Store(self.ws / ".rosetta" / "s.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.ws, ignore_errors=True)

    def app(self, **kwargs):
        return self.bridge.Rosetta(self.roots, self.store, dict(os.environ), **kwargs)

    # -- preview-before-write is enforced, not just documented ----------------

    def test_a_commit_without_a_preview_is_refused(self):
        app = self.app(require_preview=True)
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore text.\n")
        before = self.target.read_text(encoding="utf-8")
        with self.assertRaises(self.bridge.BridgeError) as caught:
            app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "PREVIEW_REQUIRED")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), before,
            "a commit that was never previewed still wrote the file",
        )

    def test_a_preview_of_a_stale_view_does_not_authorise_the_commit(self):
        app = self.app(require_preview=True)
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        view = app.cmd_read("s")["agentView"]
        app.cmd_write("s", view + "\nfirst edit.\n")
        app.cmd_preview("s")
        # Edit again: the stored preview no longer describes the approved view.
        app.cmd_write("s", view + "\nsecond edit.\n")
        with self.assertRaises(self.bridge.BridgeError) as caught:
            app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "PREVIEW_REQUIRED")

    def test_preview_then_commit_is_accepted(self):
        app = self.app(require_preview=True)
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore text.\n")
        app.cmd_preview("s")
        self.assertTrue(app.cmd_state("s", include_content=True)["sessions"][0]["hasPreview"])
        result = app.cmd_commit("s")
        self.assertTrue(result["written"])
        self.assertIn("more text.", self.target.read_text(encoding="utf-8"))
        delivered = app.cmd_state("s", include_content=True)["sessions"][0]
        self.assertFalse(delivered["hasPreview"])
        self.assertIsNone(delivered["preview"])

    def test_write_rejects_lone_surrogates_without_changing_view_or_target(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        before_view = app.cmd_read("s")["agentView"]
        before_target = self.target.read_bytes()
        with self.assertRaises(self.bridge.BridgeError) as caught:
            app.cmd_write("s", before_view + "\udc9a")
        self.assertEqual(caught.exception.code, "INVALID_UNICODE")
        self.assertIn("no changes were saved", caught.exception.message)
        self.assertEqual(app.cmd_read("s")["agentView"], before_view)
        self.assertEqual(self.target.read_bytes(), before_target)

    def test_write_accepts_chinese_and_emoji(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        before_view = app.cmd_read("s")["agentView"]
        updated = before_view + "\n中文内容正常；emoji: 😀\n"
        result = app.cmd_write("s", updated)
        self.assertTrue(result["stored"])
        self.assertEqual(app.cmd_read("s")["agentView"], updated)

    def test_discard_preview_keeps_the_edit_and_removes_the_candidate(self):
        app = self.app(require_preview=True)
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nkeep editing.\n")
        app.cmd_preview("s")
        before = self.target.read_text(encoding="utf-8")
        discarded = app.cmd_discard_preview("s")
        self.assertTrue(discarded["discarded"])
        state = app.cmd_state("s", include_content=True)["sessions"][0]
        self.assertFalse(state["hasPreview"])
        self.assertIsNone(state["preview"])
        self.assertTrue(state["dirty"], "discarding the candidate dropped the view edit too")
        self.assertEqual(self.target.read_text(encoding="utf-8"), before)
        with self.assertRaises(self.bridge.BridgeError) as caught:
            app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "PREVIEW_REQUIRED")

    def test_a_delivery_can_be_restored_from_saved_previous_bytes(self):
        app = self.app(require_preview=True)
        original = self.target.read_text(encoding="utf-8")
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore text.\n")
        app.cmd_preview("s")
        app.cmd_commit("s")
        self.assertIn("more text.", self.target.read_text(encoding="utf-8"))
        state = app.cmd_state("s", include_content=False)["sessions"][0]
        self.assertTrue(state["restoreAvailable"])
        restored = app.cmd_restore_last("s")
        self.assertTrue(restored["restored"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), original)
        with self.assertRaises(self.bridge.BridgeError):
            app.cmd_state("s", include_content=False)

    def test_pruning_never_deletes_an_undelivered_draft(self):
        app = self.app()
        app.cmd_open("dirty", "LaTeX / Typst", str(self.target))
        app.cmd_write("dirty", app.cmd_read("dirty")["agentView"] + "\nkeep me.\n")
        clean_target = self.ws / "clean.typ"
        clean_target.write_text(TYPST, encoding="utf-8")
        app.cmd_open("clean", "LaTeX / Typst", str(clean_target))
        stale = time.time() - self.bridge.SESSION_TTL_SECONDS - 60
        with self.store.transaction() as tx:
            tx.update("dirty", updatedAt=stale)
            tx.update("clean", updatedAt=stale)
        self.store._prune()
        sessions = app.cmd_state(None)["sessions"]
        self.assertTrue(any(entry["session"] == "dirty" for entry in sessions))
        self.assertFalse(any(entry["session"] == "clean" for entry in sessions))

    def test_the_gate_is_off_by_default_for_scripted_use(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore text.\n")
        self.assertTrue(app.cmd_commit("s")["written"])

    # -- a finished session stops fencing ------------------------------------

    def test_a_dirty_session_stays_active(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nunsaved.\n")
        self.assertTrue(
            self.bridge.session_active(self.store.get("s")),
            "a session with unsaved edits must keep its target fenced",
        )

    def test_a_pending_preview_stays_active(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore.\n")
        app.cmd_preview("s")
        self.assertTrue(
            self.bridge.session_active(self.store.get("s")),
            "a session with a pending preview must keep its target fenced",
        )

    def test_an_abandoned_draft_and_preview_release_the_fence(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nunsaved.\n")
        app.cmd_preview("s")
        aged = dict(self.store.get("s"))
        aged["updatedAt"] = 0
        self.assertFalse(self.bridge.session_active(aged))
        self.assertIsNotNone(aged["preview"], "the draft is retained for recovery")
        self.assertNotEqual(aged["agentView"], aged["baselineView"])

    def test_a_finished_idle_session_stops_fencing(self):
        """The trap: a committed, forgotten session used to block the user's editor."""
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        app.cmd_write("s", app.cmd_read("s")["agentView"] + "\nmore.\n")
        app.cmd_preview("s")
        app.cmd_commit("s")
        record = self.store.get("s")
        # After a commit the session still holds the preview it just delivered;
        # age it past the idle window as a forgotten session would be.
        aged = dict(record)
        aged["updatedAt"] = 0
        self.assertFalse(
            self.bridge.session_active(aged),
            "an idle, clean, previewless session must stop fencing its target",
        )
        self.assertGreater(
            self.bridge.FENCE_IDLE_SECONDS, 0,
            "the idle window must be finite and positive",
        )
        self.assertLess(
            self.bridge.FENCE_IDLE_SECONDS, 24 * 3600,
            "the idle window must be well below the retention period, or the original "
            "problem (a file unwritable for a day) is unchanged",
        )

    def test_status_and_state_report_active(self):
        app = self.app()
        app.cmd_open("s", "LaTeX / Typst", str(self.target))
        fence = {entry["session"]: entry for entry in app.cmd_status(None)["sessions"]}
        self.assertIn("active", fence["s"], "the fence payload must carry the active flag")
        self.assertTrue(fence["s"]["active"])
        state = app.cmd_state(None)["sessions"]
        self.assertTrue(any("active" in entry for entry in state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
