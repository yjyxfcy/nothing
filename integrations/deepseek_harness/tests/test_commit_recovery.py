"""Fault tests for the delivery path: what happens when the two steps disagree.

A delivery is two steps, not one transaction:

1. publish the artifact (``os.replace``), and
2. update the session record in SQLite.

A fault injected between them used to leave the artifact **new** and the database
holding the **old** hash, with nothing recording that it had happened - the commit
simply looked like it had failed, over a file that had already changed.

These tests inject exactly that fault and assert the two things that must now hold:
the caller is told the truth (the artifact *was* written), and the mismatch is
recoverable rather than silent. They also pin the opposite direction: when the
publish itself fails, nothing is claimed and no recovery record is left behind.

Run:  <python> -m unittest tests.test_commit_recovery -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"

# The bridge needs a writable scratch directory, which the sandbox denies under
# the system temp directory, so the fixtures live for real under the plugin's own
# workspace-relative scratch area.
BASE = INTEGRATION / "demo_workspace"

TYPST = "= Fault fixture\n\nBody text.\n\n#show heading: set text(fill: blue)\n"


def load_bridge():
    spec = importlib.util.spec_from_file_location("rosetta_bridge_faults", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CommitFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def setUp(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.ws = Path(__import__("tempfile").mkdtemp(prefix="fault-", dir=str(BASE)))
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
        shutil.rmtree(self.ws, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def db_hash(self, session="s"):
        return self.store.get(session)["targetSha256"]

    def file_hash(self):
        return self.bridge.sha256_file(self.target)

    def pending(self):
        return self.app.journal.pending()

    # -- 1. the reported defect: publish succeeds, the store update does not ---

    def test_store_update_failure_after_replace_reports_the_truth(self):
        """The exact measured failure: file changed, database still old."""
        before_db = self.db_hash()
        before_file = self.file_hash()
        original = self.bridge._Transaction.update
        calls = {"n": 0}

        def failing_update(self_tx, session_id, **fields):
            calls["n"] += 1
            # Record the intent, then fail before the row is written: the same
            # observable state as a fault injected into the UPDATE statement.
            raise sqlite3.OperationalError("disk I/O error (injected)")

        self.bridge._Transaction.update = failing_update
        try:
            result = self.app.cmd_commit("s")
        finally:
            self.bridge._Transaction.update = original

        self.assertTrue(result["written"], "the artifact really was published")
        self.assertFalse(result["stateApplied"])
        self.assertTrue(result["stateUpdateFailed"])
        self.assertIn("OperationalError", result["stateUpdateError"]["type"])
        self.assertIn("not one atomic transaction", " ".join(result["notes"]))
        self.assertGreater(calls["n"], 0, "the fault was never reached")

        # The defect itself: the file moved on, the record did not.
        self.assertNotEqual(self.file_hash(), before_file, "the artifact was not replaced")
        self.assertEqual(
            self.db_hash(), before_db,
            "the session record should still hold the pre-delivery hash here - that is "
            "the stale state the journal exists to repair",
        )

        # ...and it is recoverable rather than silent.
        self.assertEqual(len(self.pending()), 1, "no recovery record was kept")

    def test_recovery_replays_the_journalled_delivery(self):
        before_db = self.db_hash()
        original = self.bridge._Transaction.update
        self.bridge._Transaction.update = lambda self_tx, session_id, **fields: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk I/O error (injected)")
        )
        try:
            self.app.cmd_commit("s")
        finally:
            self.bridge._Transaction.update = original
        self.assertEqual(len(self.pending()), 1)

        # A later bridge call replays the entry, without the user doing anything.
        report = self.app.recover_pending_commits()
        self.assertEqual(len(report["applied"]), 1, json.dumps(report)[:400])
        self.assertEqual(report["applied"][0]["session"], "s")
        self.assertEqual(self.pending(), [], "the entry should be consumed once applied")
        self.assertEqual(
            self.db_hash(), self.file_hash(),
            "the session record must end up agreeing with the artifact",
        )
        self.assertNotEqual(self.db_hash(), before_db, "the record was not actually updated")

    def test_recovery_is_idempotent(self):
        original = self.bridge._Transaction.update
        self.bridge._Transaction.update = lambda self_tx, session_id, **fields: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected")
        )
        try:
            self.app.cmd_commit("s")
        finally:
            self.bridge._Transaction.update = original
        self.app.recover_pending_commits()
        settled = self.db_hash()
        second = self.app.recover_pending_commits()
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["discarded"], [])
        self.assertEqual(self.db_hash(), settled, "a second replay changed the record")

    def test_recovery_discards_an_entry_whose_publish_never_landed(self):
        """An entry is applied from the artifact's hash, not from its own claim."""
        journal = self.app.journal
        journal.record(
            {
                "session": "s",
                "pair": "LaTeX / Typst",
                "targetPath": str(self.target),
                # A hash the file does not have: the publish cannot have landed.
                "newSha256": "0" * 64,
                "deliveredTarget": "= Never published\n",
                "approvedView": self.store.get("s")["agentView"],
            }
        )
        before = self.db_hash()
        report = self.app.recover_pending_commits()
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(report["discarded"]), 1, json.dumps(report)[:400])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.db_hash(), before, "an unlanded entry must not change the record")

    def test_recovery_keeps_a_pending_edit(self):
        """Recovery repairs the target side only; a later edit must survive."""
        original = self.bridge._Transaction.update
        self.bridge._Transaction.update = lambda self_tx, session_id, **fields: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected")
        )
        try:
            self.app.cmd_commit("s")
        finally:
            self.bridge._Transaction.update = original

        # The agent edits again after the failed commit.
        view = self.app.cmd_read("s")["agentView"]
        self.app.cmd_write("s", view + "\n\\subsection{ Later}\n\nmore.\n")
        edited = self.store.get("s")["agentView"]

        self.app.recover_pending_commits()
        record = self.store.get("s")
        self.assertEqual(record["agentView"], edited, "the pending edit was clobbered")
        self.assertNotEqual(
            record["baselineView"], edited,
            "recovery re-baselined a view it does not own, which would hide unsaved edits",
        )
        self.assertEqual(record["targetSha256"], self.file_hash())

    # -- 2. the publish itself failing: nothing is claimed, nothing is left ----

    def test_publish_failure_raises_and_leaves_no_recovery_record(self):
        before_db = self.db_hash()
        before_file = self.file_hash()
        original = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            raise OSError(13, "injected replace failure")

        os.replace = failing_replace
        try:
            with self.assertRaises(Exception) as caught:
                self.app.cmd_commit("s")
        finally:
            os.replace = original

        self.assertNotIn("stateUpdateFailed", str(caught.exception))
        self.assertEqual(self.pending(), [], "a publish that never landed left a record")
        self.assertEqual(self.file_hash(), before_file, "the target changed")
        self.assertEqual(self.db_hash(), before_db, "the record changed")
        # No scratch file may be left beside the artifact.
        leftovers = [p.name for p in self.ws.glob(".doc.typ.rosetta-*.tmp")]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_an_unwritable_journal_refuses_before_writing(self):
        """No recovery record means no publish: the refusal happens first."""
        before_file = self.file_hash()
        before_db = self.db_hash()
        original = self.bridge.CommitJournal.record

        def failing_record(self_journal, entry):
            raise OSError(28, "injected journal failure")

        self.bridge.CommitJournal.record = failing_record
        try:
            with self.assertRaises(self.bridge.BridgeError) as caught:
                self.app.cmd_commit("s")
        finally:
            self.bridge.CommitJournal.record = original

        self.assertEqual(caught.exception.code, "JOURNAL_UNAVAILABLE")
        self.assertEqual(self.file_hash(), before_file, "the target was written anyway")
        self.assertEqual(self.db_hash(), before_db)
        self.assertIn("nothing was written", caught.exception.message)

    # -- 3. pre-write refusals keep their promise ----------------------------

    def test_a_pre_write_refusal_leaves_no_journal_entry(self):
        gated = self.bridge.Rosetta(
            self.roots,
            self.store,
            dict(os.environ),
            require_stable_round_trip=True,
        )
        before_file = self.file_hash()
        with self.assertRaises(self.bridge.BridgeError) as caught:
            gated.cmd_commit("s")
        self.assertEqual(caught.exception.code, "ROUND_TRIP_NOT_STABLE")
        self.assertEqual(self.pending(), [], "a pre-write refusal journalled an intent")
        self.assertEqual(self.file_hash(), before_file)

    def test_a_drift_refusal_leaves_no_journal_entry(self):
        before = self.file_hash()
        self.target.write_text("= Someone else wrote this\n", encoding="utf-8")
        with self.assertRaises(self.bridge.BridgeError) as caught:
            self.app.cmd_commit("s")
        self.assertEqual(caught.exception.code, "TARGET_CHANGED")
        self.assertEqual(self.pending(), [])
        self.assertNotEqual(self.file_hash(), before)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), "= Someone else wrote this\n"
        )

    # -- 4. the happy path does not claim more than it did -------------------

    def test_a_successful_commit_reports_the_state_as_applied(self):
        result = self.app.cmd_commit("s")
        self.assertTrue(result["written"])
        self.assertTrue(result["stateApplied"])
        self.assertFalse(result["stateUpdateFailed"])
        self.assertEqual(self.pending(), [], "a successful commit left a journal entry")
        self.assertEqual(result["targetSha256"], self.file_hash())
        self.assertEqual(self.db_hash(), self.file_hash())

    def test_doctor_reports_the_journal(self):
        doctor = self.app.cmd_doctor()
        journal = doctor["commitJournal"]
        self.assertEqual(journal["pending"], 0)
        self.assertIn("commit-journal", journal["directory"])
        self.assertIn("not one atomic transaction", journal["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
