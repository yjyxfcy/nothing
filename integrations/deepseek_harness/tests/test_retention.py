"""Tests for the local-state retention policy and the idle write fence.

The policy this file pins down:

* the activity log can run in ``full``, ``metadata`` (no source snapshots) or
  ``off`` mode, and none of those modes changes projection, preview, delivery or
  recovery;
* activity retention removes only rows written by this plugin, so a shared
  database keeps its MCP history;
* saved previous versions are bounded per session and globally, but the newest
  entry of a session - the only one Restore can reach - is never removed;
* an undelivered draft is never removed by ordinary pruning or by an automatic
  clean, survives the idle release of the write fence, and still refuses a
  delivery after the target drifted on disk;
* ``maintenance`` defaults to a read-only report and requires explicit opt-ins
  to touch a draft.

Run:  <python> tests/test_retention.py -v
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"
PYTHON = sys.executable

TYPST = "= Retention\n\nThe prototype is *ready for review*.\n"
TYPST_EDIT = "\nA line the reviewer added.\n"


def load_bridge():
    spec = importlib.util.spec_from_file_location("rosetta_bridge_retention", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def setUp(self):
        base = INTEGRATION / "acceptance-workspaces"
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / f"retention-{uuid.uuid4().hex[:12]}"
        self.ws = self.root / "ws"
        self.ws.mkdir(parents=True, exist_ok=False)
        self.state_dir = self.ws / ".rosetta"
        self.store_path = self.state_dir / "harness-sessions.sqlite3"
        self.events_path = self.state_dir / "events.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def call(self, command: dict, extra: list[str] | None = None) -> dict:
        argv = [PYTHON, str(BRIDGE), "--allow-root", str(self.ws)]
        if extra:
            argv += extra
        proc = subprocess.run(
            argv,
            input=json.dumps(command, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=300,
        )
        lines = [
            line
            for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines()
            if line.strip()
        ]
        if not lines:
            self.fail(
                f"bridge produced no JSON for {command.get('command')}: "
                f"stderr={proc.stderr.decode('utf-8', 'replace')[-800:]}"
            )
        return json.loads(lines[-1])

    def target(self, name: str = "report.typ") -> Path:
        path = self.ws / name
        path.write_text(TYPST, encoding="utf-8")
        return path

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with sqlite3.connect(str(self.events_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def session_row(self, session: str) -> dict | None:
        with sqlite3.connect(str(self.store_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM sessions WHERE session = ?", (session,)
            ).fetchone()
        return dict(row) if row else None

    def age_session(self, session: str, seconds: float) -> None:
        with sqlite3.connect(str(self.store_path)) as db:
            db.execute(
                "UPDATE sessions SET updated_at = ? WHERE session = ?",
                (time.time() - seconds, session),
            )
            db.commit()

    def count_history(self) -> int:
        directory = self.state_dir / "commit-history"
        return len(list(directory.glob("*.json"))) if directory.is_dir() else 0

    def test_activity_log_default_is_metadata_only(self):
        self.assertEqual(self.bridge.DEFAULT_ACTIVITY_MODE, "metadata")
        self.assertEqual(self.bridge.normalise_activity_mode("invalid"), "metadata")

    def age_history(self, session: str, days: float) -> None:
        """Backdate every saved-previous-version entry of one session."""
        directory = self.state_dir / "commit-history"
        for meta in directory.glob("*.json"):
            try:
                entry = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if entry.get("session") != session:
                continue
            entry["createdAt"] = time.time() - days * 86400
            meta.write_text(json.dumps(entry), encoding="utf-8")

    # -- activity log modes --------------------------------------------------

    def test_metadata_mode_keeps_the_step_and_status_without_source_text(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "meta", "pair": "LaTeX / Typst",
             "path": str(target)},
            ["--activity-mode", "metadata"],
        )
        self.assertTrue(opened["ok"], opened)
        self.call(
            {"command": "write", "session": "meta", "content": opened["agentView"] + TYPST_EDIT},
            ["--activity-mode", "metadata"],
        )
        preview = self.call(
            {"command": "preview", "session": "meta"}, ["--activity-mode", "metadata"]
        )
        self.assertTrue(preview["ok"], preview)

        stored = self.rows("SELECT * FROM events ORDER BY id")
        self.assertTrue(stored, "metadata mode must still record the steps")
        actions = {row["action"] for row in stored}
        self.assertLessEqual({"open", "write", "preview"}, actions)
        self.assertTrue(all(row["status"] == "success" for row in stored))
        self.assertTrue(all(row["agent_view"] is None for row in stored))
        self.assertTrue(all(row["target_view"] is None for row in stored))
        self.assertTrue(all(row["detail"] for row in stored), "the step text must survive")
        self.assertTrue(
            all(row["detail"] in set(self.bridge.ACTION_DETAILS.values()) for row in stored),
            "metadata mode must store only the fixed step summary, never notes/error text",
        )
        self.assertTrue(all(row["target_path"] for row in stored), "the target must survive")
        # The session store, not the activity log, is what delivery reads.
        session = self.session_row("meta")
        self.assertIn("A line the reviewer added.", session["agent_view"])

    def test_unspecified_activity_mode_logs_metadata_only(self):
        marker = "DEFAULT-LOG-PRIVATE-BODY-6f2a"
        target = self.ws / "default-mode.typ"
        target.write_text(f"= Private\n\n{marker}\n", encoding="utf-8")
        opened = self.call(
            {"command": "open", "session": "default-mode",
             "pair": "LaTeX / Typst", "path": str(target)}
        )
        self.assertTrue(opened["ok"], opened)
        rows = self.rows("SELECT * FROM events ORDER BY id")
        self.assertTrue(rows, "the default still records workflow metadata")
        self.assertTrue(all(row["agent_view"] is None for row in rows))
        self.assertTrue(all(row["target_view"] is None for row in rows))
        self.assertNotIn(marker, json.dumps(rows, ensure_ascii=False))
        self.assertTrue(all(row["detail"] for row in rows))

    def test_off_mode_writes_no_rows_and_delivery_still_works(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "quiet", "pair": "LaTeX / Typst",
             "path": str(target)},
            ["--activity-mode", "off"],
        )
        self.assertTrue(opened["ok"], opened)
        written = self.call(
            {"command": "write", "session": "quiet",
             "content": opened["agentView"] + "\nOffline delivery.\n"},
            ["--activity-mode", "off"],
        )
        self.assertTrue(written["ok"], written)
        committed = self.call(
            {"command": "commit", "session": "quiet"}, ["--activity-mode", "off"]
        )
        self.assertTrue(committed["ok"], committed)
        self.assertIn("Offline delivery.", target.read_text(encoding="utf-8"))
        if self.events_path.is_file():
            with sqlite3.connect(str(self.events_path)) as db:
                count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(count, 0, "activityMode=off must not write rows")

    def test_activity_retention_prunes_only_this_plugins_rows(self):
        target = self.target()
        # Seed enough rows to exceed the cap, plus one aged row and two rows from
        # another writer (the MCP source) that must survive untouched.
        from rosetta_studio.event_store import EventStore

        self.state_dir.mkdir(parents=True, exist_ok=True)
        store = EventStore(self.events_path)
        common = dict(
            session_id="seed", pair_name="LaTeX / Typst", action="read",
            status="success", target_path=str(target),
            agent_view="view", target_view="target", detail="seeded",
        )
        for index in range(120):
            store.record(source="Harness", **common)
        store.record(source="Rosetta", **common)
        store.record(source="Rosetta", **common)
        with sqlite3.connect(str(self.events_path)) as db:
            db.execute(
                "UPDATE events SET created_at = ? WHERE source = 'Harness' AND id <= 10",
                ("2020-01-01T00:00:00+08:00",),
            )
            db.execute(
                "UPDATE events SET created_at = ? WHERE source = 'Rosetta' AND id = ("
                " SELECT MIN(id) FROM events WHERE source = 'Rosetta')",
                ("2020-01-01T00:00:00+08:00",),
            )
            db.commit()
        before_mcp = self.rows("SELECT id FROM events WHERE source = 'Rosetta' ORDER BY id")
        self.assertEqual(len(before_mcp), 2)

        # One ordinary call is enough: recording a row runs retention.
        payload = self.call(
            {"command": "pairs"},
            ["--activity-retention-days", "30", "--activity-max-events", "50"],
        )
        self.assertTrue(payload["ok"], payload)

        harness_rows = self.rows("SELECT id, created_at FROM events WHERE source = 'Harness'")
        self.assertEqual(len(harness_rows), 50, "the count ceiling must be enforced")
        self.assertTrue(
            all(not row["created_at"].startswith("2020-") for row in harness_rows),
            "rows older than the retention window must be removed",
        )
        mcp_rows = self.rows("SELECT id FROM events WHERE source = 'Rosetta' ORDER BY id")
        self.assertEqual(
            [row["id"] for row in mcp_rows],
            [row["id"] for row in before_mcp],
            "retention must not delete another writer's rows from a shared database",
        )
        self.assertEqual(
            harness_rows[-1]["id"],
            max(row["id"] for row in harness_rows),
            "the newest row (this call) must be kept",
        )

    # -- saved previous versions --------------------------------------------

    def test_commit_history_keeps_the_newest_entries_per_session(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "hist", "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        self.assertTrue(opened["ok"], opened)
        view = opened["agentView"]
        before_last = b""
        for index in range(3):
            view = view + f"\nRevision {index}.\n"
            written = self.call({"command": "write", "session": "hist", "content": view})
            self.assertTrue(written["ok"], written)
            if index == 2:
                before_last = target.read_bytes()
            committed = self.call(
                {"command": "commit", "session": "hist"},
                ["--history-keep", "2"],
            )
            self.assertTrue(committed["ok"], committed)
        self.assertEqual(
            self.count_history(), 2,
            "older duplicate versions must be trimmed to the configured keep count",
        )
        restored = self.call({"command": "restore_last", "session": "hist"})
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(
            target.read_bytes(), before_last,
            "the newest saved version must remain the one Restore returns",
        )

    def test_commit_history_global_cap_never_drops_a_sessions_newest_backup(self):
        directory = self.state_dir / "commit-history"
        history = self.bridge.CommitHistory(
            directory, keep=3, max_files=10, max_bytes=64 * 1024 * 1024
        )
        for session in ("a", "b", "c", "d"):
            for index in range(3):
                history.record(
                    {
                        "session": session,
                        "pair": "LaTeX / Typst",
                        "targetPath": str(self.ws / f"{session}.typ"),
                        "newSha256": f"{session}-{index}",
                        "previousSha256": None,
                    },
                    f"{session}-{index}".encode("utf-8"),
                )
        self.assertEqual(len(list(directory.glob("*.json"))), 10, "file ceiling")
        for session in ("a", "b", "c", "d"):
            latest = history.latest(session)
            self.assertIsNotNone(
                latest, f"session {session} lost the only backup Restore can reach"
            )
            self.assertEqual(latest[1]["newSha256"], f"{session}-2")

    def test_commit_history_byte_cap_also_spares_the_newest_backup(self):
        directory = self.state_dir / "commit-history"
        history = self.bridge.CommitHistory(
            directory, keep=5, max_files=100, max_bytes=1024 * 1024
        )
        for index in range(5):
            history.record(
                {"session": "big", "pair": "LaTeX / Typst",
                 "targetPath": str(self.ws / "big.typ"),
                 "newSha256": f"n{index}", "previousSha256": None},
                b"x" * (300 * 1024),
            )
        entries = list(directory.glob("*.json"))
        self.assertLess(len(entries), 5, "the byte ceiling must trim old duplicates")
        self.assertIsNotNone(history.latest("big"))

    # -- drafts and the idle fence ------------------------------------------

    def test_pruning_never_deletes_a_dirty_draft_and_maintenance_needs_opt_in(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "draft", "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        self.assertTrue(opened["ok"], opened)
        self.assertTrue(
            self.call(
                {"command": "write", "session": "draft",
                 "content": opened["agentView"] + "\nKeep this draft.\n"}
            )["ok"]
        )
        # Older than both the session TTL and the maintenance age threshold, so
        # only the "never delete a draft" rule can explain why it survives.
        self.age_session("draft", 40 * 86400)
        # Any transaction prunes; the dirty record must survive it.
        self.call({"command": "state"})
        self.assertIsNotNone(self.session_row("draft"), "pruning deleted an undelivered draft")

        report = self.call({"command": "maintenance", "action": "report"})
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["applied"])
        self.assertGreaterEqual(report["drafts"]["stale"], 1)
        self.assertEqual(report["drafts"]["wouldRemove"], 0)

        clean = self.call({"command": "maintenance", "action": "clean", "apply": True})
        self.assertTrue(clean["ok"], clean)
        self.assertTrue(clean["applied"])
        self.assertIsNotNone(
            self.session_row("draft"),
            "an applied clean without includeDrafts must keep the draft",
        )

        dry = self.call(
            {"command": "maintenance", "action": "clean", "apply": False,
             "include_drafts": True}
        )
        self.assertFalse(dry["applied"])
        self.assertIsNotNone(self.session_row("draft"), "a dry run must not delete")

        opt_in = self.call(
            {"command": "maintenance", "action": "clean", "apply": True,
             "include_drafts": True, "older_than_days": 1}
        )
        self.assertTrue(opt_in["ok"], opt_in)
        self.assertEqual(opt_in["drafts"]["removed"], 1)
        self.assertIsNone(self.session_row("draft"))

    def test_recent_drafts_are_not_removed_even_with_opt_in(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "fresh", "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        self.assertTrue(
            self.call(
                {"command": "write", "session": "fresh",
                 "content": opened["agentView"] + "\nFresh draft.\n"}
            )["ok"]
        )
        result = self.call(
            {"command": "maintenance", "action": "clean", "apply": True,
             "include_drafts": True, "older_than_days": 30}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["drafts"]["removed"], 0)
        self.assertIsNotNone(self.session_row("fresh"))

    def test_idle_fence_release_keeps_the_draft_and_delivery_detects_drift(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "idle", "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        self.assertTrue(opened["ok"], opened)
        self.assertTrue(
            self.call(
                {"command": "write", "session": "idle",
                 "content": opened["agentView"] + "\nUnsubmitted change.\n"}
            )["ok"]
        )
        self.age_session("idle", self.bridge.FENCE_IDLE_SECONDS + 60)

        state = self.call({"command": "state"})["sessions"]
        entry = next(item for item in state if item["session"] == "idle")
        self.assertFalse(
            entry["active"],
            "an idle session must release the Harness write/edit fence",
        )
        self.assertTrue(entry["dirty"], "the draft must still be reported as dirty")
        record = self.session_row("idle")
        self.assertIn("Unsubmitted change.", record["agent_view"])

        # A shell, another plugin or an editor writes the file while the fence is
        # released. Delivery must refuse rather than overwrite that content.
        target.write_text(TYPST + "\nWritten by an external editor.\n", encoding="utf-8")
        self.age_session("idle", self.bridge.FENCE_IDLE_SECONDS + 60)
        preview = self.call({"command": "preview", "session": "idle"})
        self.assertTrue(preview["ok"], preview)
        self.assertTrue(preview["targetDrift"]["drifted"])

        refused = self.call({"command": "commit", "session": "idle"})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "TARGET_CHANGED")
        self.assertIn(
            "Written by an external editor.",
            target.read_text(encoding="utf-8"),
            "a refused delivery must not overwrite the external change",
        )
        self.assertIn(
            "Unsubmitted change.",
            self.session_row("idle")["agent_view"],
            "the draft must survive the refused delivery",
        )

    # -- metadata mode privacy ------------------------------------------------

    def test_metadata_mode_never_stores_error_or_note_source_snippets(self):
        """A message, note, or identifier can quote the artifact; metadata mode may not."""
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "privacy", "pair": "LaTeX / Typst",
             "path": str(target)},
            ["--activity-mode", "off"],
        )
        self.assertTrue(opened["ok"], opened)
        # Give the session a real edit so the rich row has something to leak.
        self.call(
            {"command": "write", "session": "privacy",
             "content": opened["agentView"] + TYPST_EDIT},
            ["--activity-mode", "off"],
        )

        marker = "SOURCE_SNIPPET_MUST_NOT_APPEAR"
        roots = self.bridge.Roots([str(self.ws)])
        store = self.bridge.Store(self.store_path)
        try:
            app = self.bridge.Rosetta(roots, store, {})
            events = self.bridge.EventLog(self.events_path, mode="metadata")

            failed = self.bridge._event_fields(
                app,
                "preview",
                {"session": "privacy"},
                {"notes": [f"a note quoting {marker}"]},
                f"an adapter error quoting {marker}",
                "DELIVERY_FAILED",
            )
            events.record(**failed)
            succeeded = self.bridge._event_fields(
                app,
                "write",
                {"session": "privacy"},
                {"notes": [f"a round-trip note quoting {marker}"]},
                "",
            )
            events.record(**succeeded)
            # An identifier supplied by a caller is not automatically safe.
            hostile = f"{marker} with spaces"
            rejected = self.bridge._event_fields(
                app,
                "open",
                {
                    "session": hostile,
                    "pair": hostile,
                    "path": str(self.root / f"{hostile}.typ"),
                },
                None,
                f"refused: {marker}",
                "UNKNOWN_PAIR",
            )
            events.record(**rejected)
            # A direct caller that omits the safe variant must still not leak the
            # rich detail into metadata mode.
            events.record(
                source=self.bridge.EVENT_SOURCE,
                session_id=None,
                pair_name=None,
                action="write",
                status="success",
                target_path=None,
                agent_view=f"view quoting {marker}",
                target_view=f"target quoting {marker}",
                detail=f"free text quoting {marker}",
            )

            rows = self.rows("SELECT * FROM events ORDER BY id")
            self.assertEqual(len(rows), 4)
            for row in rows:
                blob = " | ".join(
                    str(value) for value in row.values() if isinstance(value, str)
                )
                self.assertNotIn(
                    marker, blob,
                    "metadata mode stored a source/error/note fragment",
                )
                self.assertIsNone(row["agent_view"])
                self.assertIsNone(row["target_view"])

            failed_row = rows[0]
            self.assertEqual(failed_row["status"], "error")
            self.assertEqual(failed_row["action"], "preview")
            self.assertIn("DELIVERY_FAILED", failed_row["detail"])
            self.assertNotIn("adapter error", failed_row["detail"])
            succeeded_row = rows[1]
            self.assertEqual(succeeded_row["detail"], self.bridge.ACTION_DETAILS["write"])
            rejected_row = rows[2]
            self.assertIsNone(rejected_row["session_id"])
            self.assertIsNone(rejected_row["pair_name"])
            self.assertIsNone(rejected_row["target_path"])
            self.assertEqual(
                rows[3]["detail"],
                "Step recorded; free-text diagnostics are not stored in metadata mode.",
            )

            # Full mode is the contrast: it keeps the rich text by design, so
            # the test above is actually measuring the metadata filter.
            full = self.bridge._event_fields(
                app, "preview", {"session": "privacy"}, None,
                f"an adapter error quoting {marker}", "DELIVERY_FAILED",
            )
            self.bridge.EventLog(self.events_path, mode="full").record(**full)
            full_rows = self.rows(
                "SELECT * FROM events WHERE detail LIKE ?", (f"%{marker}%",)
            )
            self.assertEqual(len(full_rows), 1, "full mode should retain the rich detail")
        finally:
            store._conn.close()

    # -- saved previous versions across sessions ------------------------------

    def test_history_over_cap_never_removes_a_live_sessions_only_restore_point(self):
        directory = self.state_dir / "commit-history"
        history = self.bridge.CommitHistory(
            directory, keep=1, max_files=10, max_bytes=64 * 1024 * 1024
        )
        for index in range(12):
            session = f"s{index}"
            history.record(
                {
                    "session": session,
                    "pair": "LaTeX / Typst",
                    "targetPath": str(self.ws / f"{session}.typ"),
                    "newSha256": f"{session}-new",
                    "previousSha256": None,
                },
                f"{session}-previous".encode("utf-8"),
            )
        plan = history.plan()
        self.assertEqual(plan["remove"], [], "automatic pruning must not drop a newest entry")
        self.assertTrue(plan["overCap"], "the ceiling must be reported, not forced")
        self.assertEqual(plan["kept"], 12)
        for index in range(12):
            self.assertIsNotNone(history.latest(f"s{index}"))

        # End two of the sessions. Their backups are no longer reachable by
        # ros_restore_last, so an explicit, aged cleanup may remove them.
        self.age_history("s0", 40)
        self.age_history("s1", 40)
        live = {f"s{index}" for index in range(2, 12)}
        orphans = history.orphan_plan(live, 30)
        self.assertEqual(orphans["entries"], 2)
        self.assertEqual({entry["session"] for entry in orphans["sessions"]}, {"s0", "s1"})
        applied = history.apply_orphans(orphans)
        self.assertEqual(applied["removedEntries"], 2)
        self.assertEqual(len(list(directory.glob("*.json"))), 10)
        self.assertIsNone(history.latest("s0"))
        for index in range(2, 12):
            self.assertIsNotNone(history.latest(f"s{index}"))

    def test_maintenance_orphan_history_is_previewed_and_keeps_live_restore(self):
        target = self.target()
        opened = self.call(
            {"command": "open", "session": "hist-live", "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        self.assertTrue(opened["ok"], opened)
        self.call(
            {"command": "write", "session": "hist-live",
             "content": opened["agentView"] + "\nRestorable change.\n"}
        )
        before_last = target.read_bytes()
        committed = self.call({"command": "commit", "session": "hist-live"})
        self.assertTrue(committed["ok"], committed)

        history = self.bridge.CommitHistory(
            self.state_dir / "commit-history",
            keep=3,
            max_files=200,
            max_bytes=128 * 1024 * 1024,
        )
        for session in ("ended-1", "ended-2"):
            history.record(
                {
                    "session": session,
                    "pair": "LaTeX / Typst",
                    "targetPath": str(self.ws / f"{session}.typ"),
                    "newSha256": f"{session}-new",
                    "previousSha256": None,
                },
                f"{session}-previous".encode("utf-8"),
            )
            self.age_history(session, 40)
        before_files = sorted(
            path.name for path in (self.state_dir / "commit-history").glob("*")
        )

        report = self.call({"command": "maintenance", "action": "report"})
        self.assertTrue(report["ok"], report)
        result = report["commitHistory"]
        self.assertGreaterEqual(result["orphanSessions"], 2)
        self.assertGreaterEqual(result["orphanEntries"], 2)
        self.assertGreaterEqual(result["liveSessionsWithHistory"], 1)
        self.assertEqual(
            result["wouldRemoveOrphans"], 0,
            "the report must not claim an orphan removal without the opt-in",
        )
        self.assertTrue(
            any("includeOrphanHistory" in note for note in report["notes"]),
            report["notes"],
        )

        # Dry run, then an applied clean without the orphan opt-in: files stay.
        self.call(
            {"command": "maintenance", "action": "clean", "apply": False,
             "include_orphan_history": True, "older_than_days": 30}
        )
        self.assertEqual(
            sorted(path.name for path in (self.state_dir / "commit-history").glob("*")),
            before_files,
        )
        self.call(
            {"command": "maintenance", "action": "clean", "apply": True,
             "older_than_days": 30}
        )
        self.assertEqual(
            sorted(path.name for path in (self.state_dir / "commit-history").glob("*")),
            before_files,
            "an applied clean without includeOrphanHistory must keep orphaned backups",
        )

        applied = self.call(
            {"command": "maintenance", "action": "clean", "apply": True,
             "include_orphan_history": True, "older_than_days": 30}
        )
        self.assertTrue(applied["ok"], applied)
        self.assertGreaterEqual(applied["commitHistory"]["removedOrphanEntries"], 2)
        remaining = sorted(
            path.name for path in (self.state_dir / "commit-history").glob("*")
        )
        self.assertFalse(any(name.startswith("ended-") for name in remaining))
        self.assertTrue(any(name.startswith("hist-live-") for name in remaining))
        self.assertEqual(applied["journal"]["pending"], 0)

        restored = self.call({"command": "restore_last", "session": "hist-live"})
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(
            target.read_bytes(), before_last,
            "removing orphaned backups must not break a live session's undo",
        )

    def test_maintenance_rejects_an_unknown_action(self):
        payload = self.call({"command": "maintenance", "action": "delete-everything"})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "BAD_MAINTENANCE_ACTION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
