"""Regression tests for the reported bridge defects.

Each test names the defect it guards and is written to fail against the previous
implementation. They run without the Harness host: the bridge is exercised as the
plugin drives it, one short-lived process per command.

Run:  <python> -m unittest discover -s tests -p "test_plugin_regressions.py" -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"
PYTHON = sys.executable

TYPST = "= Quarterly update\n\nThe prototype is *ready for review*.\n"
TYPST_WITH_RULE = (
    "= Quarterly update\n"
    "\n"
    "The prototype is *ready for review*.\n"
    "\n"
    "#show heading: set text(fill: blue)\n"
)
NU = (
    'print (open --raw "data/access.log" | lines | where {|l| ($l =~ "GET") } | length)\n'
    'open --raw data/access.log | lines | parse "{method} {path} {status}" | group-by method\n'
)

# The Nushell pipeline from the real demo. It is outside the converter's
# recognized subset, so the projection carries the whole document as one
# pass-through region and the delivery is the original text, unchanged.
NU_ALL_PASSTHROUGH = (
    'open data/requests.csv\n'
    '| where status == "open"\n'
    '| get owner\n'
    '| uniq\n'
    '| length\n'
)


class ReleaseConfigTests(unittest.TestCase):
    def test_checked_in_profile_patch_has_no_unexpanded_path_placeholders(self):
        patch = (INTEGRATION / "cordis.patch.yml").read_text(encoding="utf-8")
        placeholders = (
            "${WORKSPACE_ROOT}",
            "${ROSETTA_PYTHON}",
            "${T2L_PATH}",
            "${PANDOC_PATH}",
            "${SV2V_PATH}",
        )
        for placeholder in placeholders:
            with self.subTest(placeholder=placeholder):
                self.assertNotIn(placeholder, patch)
        self.assertIn("id: rosetta-compat", patch)


def load_bridge():
    """Import the bridge module for unit-level checks."""
    spec = importlib.util.spec_from_file_location("rosetta_bridge_under_test", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BridgeHarness(unittest.TestCase):
    """Shared workspace plumbing. Every test gets a fresh isolated directory."""

    @classmethod
    def setUpClass(cls):
        cls.bridge_module = load_bridge()

    def setUp(self):
        # `mkdtemp` creates a directory whose explicit owner-only DACL the
        # Harness sandbox's restricted token cannot descend into on Windows.
        # A unique directory created with the parent's inheritable ACL works
        # in both the sandbox and an ordinary shell, so the same tests can run
        # in either.
        self._tmp = Path(tempfile.gettempdir()) / f"rosetta-regress-{uuid.uuid4().hex[:12]}"
        self._tmp.mkdir(parents=True, exist_ok=False)
        self.ws = self._tmp / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def call(self, command: dict, extra: list[str] | None = None) -> dict:
        argv = [PYTHON, str(BRIDGE), "--allow-root", str(self.ws)]
        if extra:
            argv += extra
        proc = subprocess.run(
            argv,
            input=json.dumps(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            self.fail(f"bridge produced no JSON for {command.get('command')}: {proc.stderr[-600:]}")
        return json.loads(lines[-1])

    def code(self, payload: dict) -> str:
        return (payload.get("error") or {}).get("code", "")

    def spawn(self, command: dict):
        """Start a bridge call without waiting, so calls can overlap."""
        proc = subprocess.Popen(
            [PYTHON, str(BRIDGE), "--allow-root", str(self.ws)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(command))
        proc.stdin.close()
        return proc

    @staticmethod
    def collect(proc) -> dict:
        out, _ = proc.communicate(timeout=600)
        lines = [line for line in (out or "").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else {}

    def write_target(self, name: str, text: str = TYPST) -> Path:
        target = self.ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def sessions(self) -> list[dict]:
        return self.call({"command": "status"}).get("sessions") or []


# ---------------------------------------------------------------------------
# P0-1  session store concurrency and corruption
# ---------------------------------------------------------------------------


class StoreConcurrencyTests(BridgeHarness):
    def test_concurrent_opens_all_survive(self):
        """P0-1: overlapping bridge processes must not overwrite each other."""
        count = 12
        procs = []
        for index in range(count):
            target = self.write_target(f"artifact-{index:02d}.typ")
            procs.append(
                self.spawn(
                    {
                        "command": "open",
                        "session": f"sess-{index:02d}",
                        "pair": "LaTeX / Typst",
                        "path": str(target),
                    }
                )
            )
        acknowledged = sum(1 for proc in procs if self.collect(proc).get("ok"))
        surviving = {entry["session"] for entry in self.sessions()}
        self.assertEqual(acknowledged, count, "every open should have been acknowledged")
        self.assertEqual(
            len(surviving),
            count,
            f"acknowledged sessions were lost by a peer writer: {sorted(surviving)}",
        )

    def test_concurrent_updates_keep_every_session(self):
        """P0-1: two live sessions edited concurrently both keep their own view."""
        first = self.write_target("one.typ", "= One\n\nfirst body.\n")
        second = self.write_target("two.typ", "= Two\n\nsecond body.\n")
        self.assertTrue(
            self.call({"command": "open", "session": "s-one", "pair": "LaTeX / Typst",
                       "path": str(first)}).get("ok")
        )
        self.assertTrue(
            self.call({"command": "open", "session": "s-two", "pair": "LaTeX / Typst",
                       "path": str(second)}).get("ok")
        )
        view_one = self.call({"command": "read", "session": "s-one"})["agentView"]
        view_two = self.call({"command": "read", "session": "s-two"})["agentView"]

        procs = [
            self.spawn({"command": "write", "session": "s-one",
                        "content": view_one + "\n\\section{ ONE-EDIT}\n\nx.\n"}),
            self.spawn({"command": "write", "session": "s-two",
                        "content": view_two + "\n\\section{ TWO-EDIT}\n\ny.\n"}),
        ]
        for proc in procs:
            self.assertTrue(self.collect(proc).get("ok"))

        surviving = {entry["session"] for entry in self.sessions()}
        self.assertEqual(surviving, {"s-one", "s-two"}, "a peer write dropped a session")
        one = self.call({"command": "read", "session": "s-one"})["agentView"]
        two = self.call({"command": "read", "session": "s-two"})["agentView"]
        self.assertIn("ONE-EDIT", one)
        self.assertIn("TWO-EDIT", two)
        self.assertNotIn("TWO-EDIT", one, "a session's view was overwritten by its peer")

    def test_legacy_json_store_is_migrated(self):
        """P0-1: an existing JSON store is imported, not abandoned."""
        state = self.ws / ".rosetta"
        state.mkdir(parents=True, exist_ok=True)
        legacy = state / "harness-sessions.json"
        target = self.write_target("legacy.typ")
        legacy.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "sessions": {
                        "old-session": {
                            "pair": "LaTeX / Typst",
                            "targetPath": str(target),
                            "targetSha256": None,
                            "targetExisted": True,
                            "agentView": "\\documentclass{article}\n",
                            "baselineView": "\\documentclass{article}\n",
                            "preview": None,
                            "projectionMetadata": {},
                            "createdAt": time.time(),
                            "updatedAt": time.time(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        keys = {entry["session"] for entry in self.sessions()}
        self.assertIn("old-session", keys, "the legacy session was not migrated")
        self.assertFalse(legacy.exists(), "the legacy file should be retired")
        self.assertTrue((state / "harness-sessions.json.migrated").exists())

    def test_corrupt_legacy_store_refuses_instead_of_reporting_zero(self):
        """P0-1: an unreadable store must not silently mean 'no sessions'."""
        state = self.ws / ".rosetta"
        state.mkdir(parents=True, exist_ok=True)
        (state / "harness-sessions.json").write_text("{ not json at all", encoding="utf-8")
        payload = self.call({"command": "status"})
        self.assertFalse(payload.get("ok"), "a corrupt store must be reported, not ignored")
        self.assertEqual(self.code(payload), "STORE_CORRUPT")

    def test_corrupt_database_refuses_instead_of_reporting_zero(self):
        """P0-1: a damaged SQLite store must not silently mean 'no sessions'."""
        state = self.ws / ".rosetta"
        state.mkdir(parents=True, exist_ok=True)
        (state / "harness-sessions.sqlite3").write_bytes(b"this is not a database" * 64)
        payload = self.call({"command": "status"})
        self.assertFalse(payload.get("ok"))
        self.assertEqual(self.code(payload), "STORE_CORRUPT")

    def test_json_store_path_is_refused(self):
        """P0-1: the store is a database now; a .json path is a configuration error."""
        payload = self.call(
            {"command": "status"}, extra=["--store", str(self.ws / "sessions.json")]
        )
        self.assertFalse(payload.get("ok"))
        self.assertEqual(self.code(payload), "STORE_PATH_IS_JSON")


# ---------------------------------------------------------------------------
# P0-2  the write fence follows session state, not file existence
# ---------------------------------------------------------------------------


class GuardStateTests(BridgeHarness):
    def test_create_session_is_fenced_before_the_target_exists(self):
        """P0-2: a created-but-unwritten artifact must already be fenced."""
        target = self.ws / "brand-new.typ"
        created = self.call(
            {"command": "create", "session": "new", "pair": "LaTeX / Typst",
             "path": str(target), "content": TYPST}
        )
        self.assertTrue(created.get("ok"), created)
        self.assertFalse(target.exists())
        self.assertIn(
            "new",
            {entry["session"] for entry in self.sessions()},
            "the fence list omitted a live session whose target does not exist yet",
        )

    def test_session_stays_fenced_after_the_target_is_deleted(self):
        """P0-2: deleting the target must not open a hole in the fence."""
        target = self.write_target("goes-away.typ")
        self.assertTrue(
            self.call({"command": "open", "session": "doomed", "pair": "LaTeX / Typst",
                       "path": str(target)}).get("ok")
        )
        target.unlink()
        self.assertIn(
            "doomed",
            {entry["session"] for entry in self.sessions()},
            "the fence forgot a session whose target was deleted externally",
        )

    def test_status_reports_target_presence_without_hiding_the_session(self):
        target = self.write_target("present.typ")
        self.call({"command": "open", "session": "here", "pair": "LaTeX / Typst",
                   "path": str(target)})
        entry = next(e for e in self.sessions() if e["session"] == "here")
        self.assertTrue(entry["targetPresent"])


# ---------------------------------------------------------------------------
# P0-3  the stability gate runs before the write
# ---------------------------------------------------------------------------


class StabilityGateTests(BridgeHarness):
    def _open_and_edit(self, name: str, session: str = "gate") -> Path:
        target = self.write_target(name)
        opened = self.call({"command": "open", "session": session,
                            "pair": "LaTeX / Typst", "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        edited = opened["agentView"] + "\n\\section{ Added}\n\nmore text.\n"
        self.assertTrue(
            self.call({"command": "write", "session": session, "content": edited}).get("ok")
        )
        return target

    def test_gate_refuses_before_writing_and_leaves_everything_intact(self):
        """P0-3: with the gate on, a refusal must not have touched the target."""
        target = self._open_and_edit("gated.typ")
        before = target.read_bytes()
        status_before = self.call({"command": "status"})

        payload = self.call(
            {"command": "commit", "session": "gate"}, extra=["--require-stable-round-trip"]
        )
        self.assertFalse(payload.get("ok"), "an unstable delivery must be refused")
        self.assertEqual(self.code(payload), "ROUND_TRIP_NOT_STABLE")
        self.assertEqual(target.read_bytes(), before, "the target was modified by a refusal")

        after = self.call({"command": "status"})
        self.assertEqual(
            [e["session"] for e in status_before["sessions"]],
            [e["session"] for e in after["sessions"]],
            "the fence state changed across a refused delivery",
        )
        read = self.call({"command": "read", "session": "gate"})
        self.assertTrue(read.get("ok"), "the session must survive a refused delivery")
        self.assertTrue(read["dirty"], "the pending edit must survive a refused delivery")

    def test_gate_off_writes_the_delivery(self):
        target = self._open_and_edit("ungated.typ")
        payload = self.call({"command": "commit", "session": "gate"})
        self.assertTrue(payload.get("ok"), payload)
        self.assertTrue(payload.get("written"))
        self.assertIn("Added", target.read_text(encoding="utf-8"))

    def test_stable_fixed_point_satisfies_the_gate(self):
        """P1-1: a delivery is a byte fixed point, and the gate accepts it.

        Measured after the engine-template fix: a document that carries a
        target-only directive (or one with a blank line after the heading)
        converges on the **first** delivery, because the external engine no
        longer injects a preamble that the next projection would absorb as new
        preserved regions. The test pins the current measured shape: within a
        couple of rounds the status is ``exact``, and any pre-convergence round is
        reported honestly rather than as stable.
        """
        target = self.write_target("fixed-point.typ", TYPST_WITH_RULE)
        observed = []
        converged_at = None
        for round_index in range(1, 5):
            session = f"fp-{round_index}"
            opened = self.call({"command": "open", "session": session,
                                "pair": "LaTeX / Typst", "path": str(target)})
            self.assertTrue(opened.get("ok"), opened)
            payload = self.call(
                {"command": "commit", "session": session},
                extra=["--require-stable-round-trip"],
            )
            if payload.get("ok"):
                self.assertEqual(payload["roundTrip"]["status"], "exact")
                converged_at = round_index
                break
            self.assertEqual(self.code(payload), "ROUND_TRIP_NOT_STABLE")
            observed.append(payload["error"]["details"]["status"])
            # Advance ungated so the next round sees the delivered artifact; a
            # refused delivery writes nothing and so cannot converge on its own.
            advance = self.call({"command": "commit", "session": session})
            self.assertTrue(advance.get("ok"), advance)

        self.assertIsNotNone(
            converged_at, f"no byte fixed point within 4 rounds; saw {observed}"
        )
        self.assertTrue(
            all(status in {"unstable", "unknown"} for status in observed),
            f"a pre-convergence round was not reported honestly: {observed}",
        )

        # The fixed point must hold, not oscillate: the gate keeps accepting.
        for attempt in range(2):
            session = f"fp-stable-{attempt}"
            self.assertTrue(
                self.call({"command": "open", "session": session,
                           "pair": "LaTeX / Typst", "path": str(target)}).get("ok")
            )
            gated = self.call(
                {"command": "commit", "session": session},
                extra=["--require-stable-round-trip"],
            )
            self.assertTrue(gated.get("ok"), gated)
            self.assertEqual(gated["roundTrip"]["status"], "exact")

    def test_a_typical_fresh_document_is_stable_on_the_first_delivery(self):
        """The engine template no longer makes round one artificially unstable."""
        target = self.write_target("fresh-stable.typ", TYPST_WITH_RULE)
        self.assertTrue(
            self.call({"command": "open", "session": "fresh", "pair": "LaTeX / Typst",
                       "path": str(target)}).get("ok")
        )
        payload = self.call(
            {"command": "commit", "session": "fresh"},
            extra=["--require-stable-round-trip"],
        )
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload["roundTrip"]["status"], "exact")

    def test_a_refused_delivery_cannot_advance_the_artifact(self):
        """The one remaining bootstrap case is narrow and reported, not hidden.

        A projection that injects structure the source did not have (a blank line
        after the heading) is not a byte fixed point on the first delivery. The
        gate refuses, and because a refusal writes nothing the artifact cannot
        advance on its own - so the same status repeats. Documented here so the
        behaviour is a known contract rather than a surprise.
        """
        target = self.write_target("bootstrap.typ", "= Q\nText.\n")
        statuses = []
        for round_index in range(2):
            session = f"boot-{round_index}"
            self.assertTrue(
                self.call({"command": "open", "session": session,
                           "pair": "LaTeX / Typst", "path": str(target)}).get("ok")
            )
            payload = self.call(
                {"command": "commit", "session": session},
                extra=["--require-stable-round-trip"],
            )
            self.assertFalse(payload.get("ok"))
            self.assertEqual(self.code(payload), "ROUND_TRIP_NOT_STABLE")
            statuses.append(payload["error"]["details"]["status"])
        self.assertEqual(statuses, ["unstable", "unstable"])

        # One ungated delivery advances the artifact, and then the gate accepts.
        advance = self.call({"command": "commit", "session": "boot-0"})
        self.assertTrue(advance.get("ok"), advance)
        self.assertTrue(
            self.call({"command": "open", "session": "boot-after",
                       "pair": "LaTeX / Typst", "path": str(target)}).get("ok")
        )
        after = self.call(
            {"command": "commit", "session": "boot-after"},
            extra=["--require-stable-round-trip"],
        )
        self.assertTrue(after.get("ok"), after)
        self.assertEqual(after["roundTrip"]["status"], "exact")



# ---------------------------------------------------------------------------
# P1-1  the stability verdict must be conservative
# ---------------------------------------------------------------------------


class StabilityVerdictTests(BridgeHarness):
    """Unit-level negatives: every one of these used to read as 'stable'."""

    def setUp(self):
        super().setUp()
        self.store = self.bridge_module.Store(self.ws / ".rosetta" / "regress.sqlite3")
        self.app = self.bridge_module.Rosetta(
            self.bridge_module.Roots([str(self.ws)]),
            self.store,
            dict(os.environ),
        )
        self.pair = self.app.pairs["LaTeX / Typst"]

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def _status(self, approved: str, delivered: str) -> dict:
        record = {"agentView": approved}
        return self.app._stability(record, self.pair, delivered)

    def test_duplicate_preserved_block_is_not_stable(self):
        """Two identical blocks must not collapse into one via a set comparison."""
        block = "% @TYPST-PASSTHROUGH\n% #show heading: set text(fill: blue)\n% @END-TYPST-PASSTHROUGH\n"
        approved = "\\documentclass{article}\n" + block + block + "\\begin{document}\n\\end{document}\n"
        delivered = (
            "// @TYPST-PASSTHROUGH\n#show heading: set text(fill: blue)\n// @END-TYPST-PASSTHROUGH\n"
            "= T\n\n// @TYPST-PASSTHROUGH\n#show heading: set text(fill: blue)\n"
            "// @END-TYPST-PASSTHROUGH\n"
        )
        # Same set of regions, different multiplicity: must not be called stable.
        verdict = self._status(approved, delivered)
        self.assertFalse(verdict["stable"], verdict)
        self.assertIn(verdict["status"], {"unstable", "unknown"})

    def test_reordered_preserved_blocks_are_not_stable(self):
        one = "% @TYPST-PASSTHROUGH\n% #set page(paper: \"a4\")\n% @END-TYPST-PASSTHROUGH\n"
        two = "% @TYPST-PASSTHROUGH\n% #set heading(numbering: \"1.\")\n% @END-TYPST-PASSTHROUGH\n"
        approved = "\\documentclass{article}\n" + one + two + "\\begin{document}\n\\end{document}\n"
        real_one = "// @TYPST-PASSTHROUGH\n#set page(paper: \"a4\")\n// @END-TYPST-PASSTHROUGH\n"
        real_two = "// @TYPST-PASSTHROUGH\n#set heading(numbering: \"1.\")\n// @END-TYPST-PASSTHROUGH\n"
        delivered = real_two + real_one + "= T\n"
        verdict = self._status(approved, delivered)
        self.assertFalse(verdict["stable"], verdict)

    def test_file_level_normalisation_only(self):
        """EOL changes add no differences beyond the known heading separator.

        Measured motivation: before this step, one trailing newline made **all
        four** pairs report `unstable` for a view that was in fact correct, which
        would have made a gate left on refuse ordinary work.
        """
        for label, transform in (
            ("one trailing newline", lambda v: v + "\n"),
            ("CRLF line endings", lambda v: v.replace("\n", "\r\n")),
            ("CR line endings", lambda v: v.replace("\n", "\r")),
        ):
            with self.subTest(label=label):
                target = self.write_target(f"norm-{abs(hash(label))}.typ", TYPST_WITH_RULE)
                opened = self.call({"command": "open", "session": "norm",
                                    "pair": "LaTeX / Typst", "path": str(target)})
                self.assertTrue(opened.get("ok"), opened)
                baseline = self.call({"command": "preview", "session": "norm"})
                self.assertTrue(baseline.get("ok"), baseline)
                self.assertTrue(
                    self.call({"command": "write", "session": "norm",
                               "content": transform(opened["agentView"])}).get("ok")
                )
                preview = self.call({"command": "preview", "session": "norm"})
                self.assertTrue(preview.get("ok"), preview)
                self.assertEqual(
                    preview["roundTrip"]["status"], baseline["roundTrip"]["status"],
                    f"{label} must not add a round-trip difference",
                )
                self.assertEqual(
                    preview["roundTrip"].get("formattingDifference"),
                    baseline["roundTrip"].get("formattingDifference"),
                    f"{label} must not change the diagnosed difference",
                )
                self.assertEqual(
                    self.bridge_module._comparison_form(transform(opened["agentView"])),
                    self.bridge_module._comparison_form(opened["agentView"]),
                )
                self.call({"command": "close", "session": "norm"})

    def test_blank_line_added_to_the_view_is_still_unstable(self):
        """A blank line is document content, not a file convention."""
        target = self.write_target("blank-line.typ", TYPST_WITH_RULE)
        opened = self.call({"command": "open", "session": "blank",
                            "pair": "LaTeX / Typst", "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        self.call({"command": "write", "session": "blank",
                   "content": opened["agentView"] + "\n\n"})
        preview = self.call({"command": "preview", "session": "blank"})
        self.assertTrue(preview.get("ok"), preview)
        self.assertFalse(
            preview["roundTrip"]["stable"],
            "an added blank line must not be normalised away",
        )

    def test_typst_heading_separator_difference_is_reported_without_being_accepted(self):
        """Name the known heading-spacing difference without weakening the gate."""
        approved = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\section{Q}\n\nText.\n\\end{document}"
        )
        reprojected = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\section{Q}\nText.\n\\end{document}"
        )
        projected = SimpleNamespace(success=True, content=reprojected, errors=[])
        with patch.object(
            self.bridge_module, "project_with_preservation", return_value=(projected, [])
        ):
            verdict = self.app._stability(
                {"agentView": approved}, self.pair, "= Q\nText.\n"
            )
        self.assertEqual(verdict["status"], "unstable")
        self.assertFalse(verdict["stable"])
        self.assertEqual(
            verdict.get("formattingDifference"), "blank_line_after_latex_heading"
        )
        self.assertIn("text check remains inconclusive", verdict["reason"])

    def test_whitespace_change_inside_content_is_still_unstable(self):
        """A space inside the document is content, and is not folded away."""
        target = self.write_target("string-ws.typ", TYPST_WITH_RULE)
        opened = self.call({"command": "open", "session": "strws",
                            "pair": "LaTeX / Typst", "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        view = opened["agentView"]
        mutated = view.replace("Quarterly update", "Quarterly  update")
        self.assertNotEqual(mutated, view, "the fixture did not contain the expected text")
        self.call({"command": "write", "session": "strws", "content": mutated})
        preview = self.call({"command": "preview", "session": "strws"})
        self.assertTrue(preview.get("ok"), preview)
        self.assertFalse(
            preview["roundTrip"]["stable"],
            "a whitespace change inside the document was folded away instead of reported",
        )

    def test_metadata_is_scrubbed_of_artifact_text(self):
        metadata = {
            "method": "pipeline",
            "protectedRegions": [["% @TYPST-PASSTHROUGH", ["% #show secret()"]]],
            "preservedRegions": ["#show secret()"],
        }
        public = self.bridge_module._public_metadata(metadata)
        blob = json.dumps(public)
        self.assertNotIn("secret", blob, f"artifact text leaked into the public payload: {blob}")
        self.assertEqual(public["preservedRegionCount"], 1)
        self.assertEqual(public["protectedRegionMarkers"], ["% @TYPST-PASSTHROUGH"])
        self.assertEqual(public["method"], "pipeline")

    def test_regions_parser_agrees_with_the_mcp_server(self):
        """The plugin and the MCP front end must not disagree about a block."""
        from rosetta_studio import mcp_server

        corpus = [
            "\\documentclass{article}\n\\begin{document}\n\\end{document}\n",
            "% @TYPST-PASSTHROUGH\n% #set page(paper: \"a4\")\n% @END-TYPST-PASSTHROUGH\n",
            "a\n% @TYPST-PASSTHROUGH\n% one\n% @END-TYPST-PASSTHROUGH\n"
            "b\n% @TYPST-PASSTHROUGH\n% two\n% @END-TYPST-PASSTHROUGH\nc\n",
            "# @NU-PASSTHROUGH\n# open x | group-by m\n# @END-NU-PASSTHROUGH\n",
            "// @SV-PASSTHROUGH\n// logic d;\n// @END-SV-PASSTHROUGH\n",
        ]
        for text in corpus:
            with self.subTest(text=text[:40]):
                self.assertEqual(
                    self.bridge_module.preserved_regions(text),
                    mcp_server.preserved_regions(text),
                )

    def test_malformed_views_are_reported_not_guessed(self):
        for bad in (
            "% @TYPST-PASSTHROUGH\n% #set page(width: 1cm)\n",          # unterminated
            "% @END-TYPST-PASSTHROUGH\n",                                # stray end
            "% @TYPST-PASSTHROUGH\n% @TYPST-PASSTHROUGH\n% @END-TYPST-PASSTHROUGH\n",
        ):
            with self.subTest(view=bad[:40]):
                with self.assertRaises(self.bridge_module.MalformedView):
                    self.bridge_module.preserved_regions(bad)

    def test_write_refuses_to_edit_inside_a_preserved_region(self):
        """R/E: the body of a marked block must not be editable through the view."""
        target = self.write_target("pass-edit.typ", TYPST_WITH_RULE)
        opened = self.call({"command": "open", "session": "pass-edit",
                            "pair": "LaTeX / Typst", "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        view = opened["agentView"]
        self.assertGreater(opened.get("protectedRegions", 0), 0, view)

        # Change the directive *inside* the block: the region no longer matches.
        tampered = view.replace(
            "% #show heading: set text(fill: blue)",
            "% #show heading: set text(fill: red)",
        )
        self.assertNotEqual(tampered, view)
        refusal = self.call(
            {"command": "write", "session": "pass-edit", "content": tampered}
        )
        self.assertFalse(refusal.get("ok"), "editing a preserved body must be refused")
        self.assertEqual(self.code(refusal), "PASSTHROUGH_CHANGED")

    def test_write_refuses_a_stray_end_marker(self):
        """A marker shape the layer never issued is refused, not guessed at."""
        target = self.write_target("pass-stray.typ", TYPST_WITH_RULE)
        opened = self.call({"command": "open", "session": "pass-stray",
                            "pair": "LaTeX / Typst", "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        view = opened["agentView"]
        stray = view.replace("% @TYPST-PASSTHROUGH\n", "", 1)
        refusal = self.call(
            {"command": "write", "session": "pass-stray", "content": stray}
        )
        self.assertFalse(refusal.get("ok"))
        self.assertEqual(self.code(refusal), "PASSTHROUGH_MALFORMED")

    def test_projection_marks_a_region_exactly_once(self):
        """The bridge must not re-run marking on top of Pair.project()."""
        target = self.write_target("once.typ", TYPST_WITH_RULE)
        content = target.read_text(encoding="utf-8")
        result, preserved = self.bridge_module.project_with_preservation(
            self.pair, content
        )
        self.assertTrue(result.success)
        self.assertEqual(len(preserved), 1, preserved)
        self.assertEqual(
            result.content.count("% @TYPST-PASSTHROUGH"),
            1,
            f"the region was marked more than once:\n{result.content}",
        )
        self.assertEqual(
            len(result.metadata.get("preserved_regions") or []),
            1,
            "the product's own preserved_regions metadata was lost",
        )

    def test_write_refuses_to_delete_a_preserved_region(self):
        """R/E: a marked block may not be removed or reordered through the view."""
        target = self.write_target("pass.typ", TYPST_WITH_RULE)
        opened = self.call({"command": "open", "session": "pass", "pair": "LaTeX / Typst",
                            "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        view = opened["agentView"]
        self.assertGreater(opened.get("protectedRegions", 0), 0, view)

        # Drop the whole block: start marker, body and end marker.
        kept: list[str] = []
        inside = False
        for line in view.splitlines():
            stripped = line.strip()
            if stripped == "% @TYPST-PASSTHROUGH":
                inside = True
                continue
            if stripped == "% @END-TYPST-PASSTHROUGH":
                inside = False
                continue
            if not inside:
                kept.append(line)
        removed = "\n".join(kept)
        self.assertNotIn("@TYPST-PASSTHROUGH", removed)

        refusal = self.call({"command": "write", "session": "pass", "content": removed})
        self.assertFalse(refusal.get("ok"), "removing a preserved region must be refused")
        self.assertEqual(self.code(refusal), "PASSTHROUGH_CHANGED")

        # Leaving a stray end marker is a different malformed shape, refused too.
        stray = view.replace("% @TYPST-PASSTHROUGH\n", "", 1)
        stray_refusal = self.call(
            {"command": "write", "session": "pass", "content": stray}
        )
        self.assertFalse(stray_refusal.get("ok"))
        self.assertIn(
            self.code(stray_refusal),
            {"PASSTHROUGH_MALFORMED", "PASSTHROUGH_CHANGED"},
        )

    def test_write_refuses_to_reorder_two_preserved_regions(self):
        """Order matters: swapping two blocks changes the artifact."""
        source = (
            "= Doc\n"
            "\n"
            "Text.\n"
            "\n"
            "#set page(paper: \"a4\")\n"
            "\n"
            "#show heading: set text(fill: blue)\n"
        )
        target = self.write_target("order.typ", source)
        opened = self.call({"command": "open", "session": "order", "pair": "LaTeX / Typst",
                            "path": str(target)})
        self.assertTrue(opened.get("ok"), opened)
        view = opened["agentView"]
        blocks = [
            block for block in view.split("% @END-TYPST-PASSTHROUGH")
            if "% @TYPST-PASSTHROUGH" in block
        ]
        if len(blocks) < 2:
            self.skipTest(f"document produced {len(blocks)} preserved block(s); needs 2")
        # Rebuild the view with the two blocks swapped, keeping everything else.
        first_start = view.index("% @TYPST-PASSTHROUGH")
        first_end = view.index("% @END-TYPST-PASSTHROUGH", first_start) + len(
            "% @END-TYPST-PASSTHROUGH"
        )
        second_start = view.index("% @TYPST-PASSTHROUGH", first_end)
        second_end = view.index("% @END-TYPST-PASSTHROUGH", second_start) + len(
            "% @END-TYPST-PASSTHROUGH"
        )
        block_a = view[first_start:first_end]
        block_b = view[second_start:second_end]
        swapped = (
            view[:first_start] + block_b + view[first_end:second_start]
            + block_a + view[second_end:]
        )
        self.assertNotEqual(swapped, view)
        refusal = self.call({"command": "write", "session": "order", "content": swapped})
        self.assertFalse(refusal.get("ok"), "reordering preserved regions must be refused")

    def test_delivery_of_nonempty_view_cannot_be_empty(self):
        target = self.write_target("noempty.typ")
        self.call({"command": "open", "session": "noempty", "pair": "LaTeX / Typst",
                   "path": str(target)})
        payload = self.call({"command": "write", "session": "noempty", "content": "   \n"})
        # An empty view for a nonempty target is allowed to be stored, but the
        # delivery it produces must be refused rather than erase the artifact.
        if payload.get("ok"):
            preview = self.call({"command": "preview", "session": "noempty"})
            self.assertFalse(
                preview.get("ok") and not (preview.get("targetContent") or "").strip(),
                "an empty delivery was accepted for a nonempty view",
            )


class TypstArtifactCleanlinessTests(BridgeHarness):
    def test_generated_transport_wrappers_do_not_leak_into_delivered_typst(self):
        target = self.write_target("clean-delivery.typ", TYPST_WITH_RULE)
        opened = self.call({
            "command": "open", "session": "clean", "pair": "LaTeX / Typst",
            "path": str(target),
        })
        self.assertTrue(opened.get("ok"), opened)
        edited = opened["agentView"] + "\n\\section{Release checklist}\nShip it.\n"
        stored = self.call({"command": "write", "session": "clean", "content": edited})
        self.assertTrue(stored.get("ok"), stored)

        preview = self.call({"command": "preview", "session": "clean"})
        self.assertTrue(preview.get("ok"), preview)
        candidate = preview["targetContent"]
        self.assertIn("#show heading: set text(fill: blue)", candidate)
        self.assertIn("Release checklist", candidate)
        self.assertNotIn("@TYPST-PASSTHROUGH", candidate)

        committed = self.call({"command": "commit", "session": "clean"})
        self.assertTrue(committed.get("ok"), committed)
        delivered = target.read_text(encoding="utf-8")
        self.assertEqual(delivered, candidate)
        self.assertNotIn("@TYPST-PASSTHROUGH", delivered)

        reopened = self.call({
            "command": "open", "session": "clean-again", "pair": "LaTeX / Typst",
            "path": str(target),
        })
        self.assertTrue(reopened.get("ok"), reopened)
        self.assertIn("@TYPST-PASSTHROUGH", reopened["agentView"])
        self.assertIn("Release checklist", reopened["agentView"])

    def test_hand_authored_typst_wrappers_are_not_stripped(self):
        source = (
            "= Note\n\n"
            "// @TYPST-PASSTHROUGH\n"
            "#show heading: set text(fill: blue)\n"
            "// @END-TYPST-PASSTHROUGH\n"
        )
        target = self.write_target("authored-wrapper.typ", source)
        opened = self.call({
            "command": "open", "session": "authored", "pair": "LaTeX / Typst",
            "path": str(target),
        })
        self.assertTrue(opened.get("ok"), opened)
        preview = self.call({"command": "preview", "session": "authored"})
        self.assertTrue(preview.get("ok"), preview)
        self.assertIn("// @TYPST-PASSTHROUGH", preview["targetContent"])
        self.assertIn("#show heading: set text(fill: blue)", preview["targetContent"])


# ---------------------------------------------------------------------------
# P2  the workspace allow-list is per profile, not per session
# ---------------------------------------------------------------------------


class WorkspaceIsolationTests(BridgeHarness):
    """Documents and pins the isolation the plugin actually provides.

    The Host exposes only `Agent.id`, so the plugin cannot read a session's
    working directory and cannot derive a per-session root. `workspaceRoots` is
    therefore one process/profile-wide allow-list shared by every session. These
    tests state what that does and does not isolate, so the boundary is a
    measured contract rather than an assumption.
    """

    def test_two_sessions_in_different_subtrees_coexist_and_are_both_fenced(self):
        first_root = self.ws / "alpha"
        second_root = self.ws / "beta"
        first_root.mkdir(parents=True, exist_ok=True)
        second_root.mkdir(parents=True, exist_ok=True)
        first = first_root / "one.typ"
        second = second_root / "two.typ"
        first.write_text("= Alpha\n\nfirst.\n", encoding="utf-8")
        second.write_text("= Beta\n\nsecond.\n", encoding="utf-8")

        self.assertTrue(
            self.call({"command": "open", "session": "s-alpha", "pair": "LaTeX / Typst",
                       "path": str(first)}).get("ok")
        )
        self.assertTrue(
            self.call({"command": "open", "session": "s-beta", "pair": "LaTeX / Typst",
                       "path": str(second)}).get("ok")
        )
        keys = {entry["session"] for entry in self.sessions()}
        self.assertEqual(keys, {"s-alpha", "s-beta"})

        alpha_view = self.call({"command": "read", "session": "s-alpha"})["agentView"]
        self.call({"command": "write", "session": "s-alpha",
                   "content": alpha_view + "\n\\section{ ALPHA-EDIT}\n\nx.\n"})
        beta_view = self.call({"command": "read", "session": "s-beta"})["agentView"]
        self.assertNotIn("ALPHA-EDIT", beta_view, "one session's edit leaked into another")

        self.assertTrue(self.call({"command": "commit", "session": "s-alpha"}).get("ok"))
        self.assertIn("ALPHA-EDIT", first.read_text(encoding="utf-8"))
        self.assertNotIn("ALPHA-EDIT", second.read_text(encoding="utf-8"))
        # The untouched session still works afterwards.
        self.assertTrue(
            self.call({"command": "commit", "session": "s-beta"}).get("ok")
        )

    def test_a_second_session_on_the_same_target_is_fenced_and_commit_ordered(self):
        """Two sessions may name one target; the fence covers it and delivery is
        serialised by drift detection rather than by both writing blindly."""
        target = self.write_target("shared.typ")
        self.assertTrue(
            self.call({"command": "open", "session": "first", "pair": "LaTeX / Typst",
                       "path": str(target)}).get("ok")
        )
        self.assertTrue(
            self.call({"command": "open", "session": "second", "pair": "LaTeX / Typst",
                       "path": str(target)}).get("ok")
        )
        self.assertEqual(
            {entry["session"] for entry in self.sessions()}, {"first", "second"}
        )
        view = self.call({"command": "read", "session": "first"})["agentView"]
        self.call({"command": "write", "session": "first",
                   "content": view + "\n\\section{ FIRST}\n\nx.\n"})
        self.assertTrue(self.call({"command": "commit", "session": "first"}).get("ok"))

        # The second session opened before that write, so its recorded hash is
        # stale and it must refuse rather than clobber the newer content.
        second_view = self.call({"command": "read", "session": "second"})["agentView"]
        self.call({"command": "write", "session": "second",
                   "content": second_view + "\n\\section{ SECOND}\n\ny.\n"})
        refusal = self.call({"command": "commit", "session": "second"})
        self.assertFalse(refusal.get("ok"), "a stale session overwrote newer content")
        self.assertEqual(self.code(refusal), "TARGET_CHANGED")
        self.assertNotIn("SECOND", target.read_text(encoding="utf-8"))

    def test_a_target_outside_every_root_is_refused(self):
        """The allow-list, not the caller, decides what is reachable."""
        outside = self._tmp / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        stray = outside / "stray.typ"
        stray.write_text("= Stray\n\nbody.\n", encoding="utf-8")
        payload = self.call(
            {"command": "open", "session": "stray", "pair": "LaTeX / Typst",
             "path": str(stray)}
        )
        self.assertFalse(payload.get("ok"))
        self.assertEqual(self.code(payload), "PATH_OUTSIDE_WORKSPACE")

    def test_a_workspace_link_cannot_expose_a_target_outside_every_root(self):
        """Containment follows filesystem links, not just the supplied spelling."""
        outside = self._tmp / "outside-linked"
        outside.mkdir(parents=True, exist_ok=True)
        stray = outside / "linked.typ"
        stray.write_text("= Outside\n\nbody.\n", encoding="utf-8")
        link = self.ws / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks are unavailable in this environment: {exc}")

        payload = self.call(
            {"command": "open", "session": "linked-stray",
             "pair": "LaTeX / Typst", "path": str(link / stray.name)}
        )
        self.assertFalse(payload.get("ok"))
        self.assertEqual(self.code(payload), "PATH_OUTSIDE_WORKSPACE")


# ---------------------------------------------------------------------------
# P1-2  the state interface must not republish artifact text by default
# ---------------------------------------------------------------------------


class StatePayloadTests(BridgeHarness):
    def test_state_omits_artifact_text_unless_requested(self):
        marker = "CONFIDENTIAL-BODY-MARKER-9f3a"
        target = self.write_target("secret.typ", f"= Secret\n\n{marker}\n")
        self.call({"command": "open", "session": "leak", "pair": "LaTeX / Typst",
                   "path": str(target)})

        default = json.dumps(self.call({"command": "state"}))
        self.assertNotIn(marker, default, "the state payload republished artifact text")

        explicit = self.call({"command": "state", "include_content": True})
        self.assertTrue(explicit.get("contentIncluded"))
        blob = json.dumps(explicit)
        self.assertIn(marker, blob, "the explicit content request should carry the view")
        # Metadata-only fields must still be present for the status strip.
        entry = explicit["sessions"][0]
        self.assertIn("pair", entry)
        self.assertIn("dirty", entry)

    def test_chat_scoped_content_request_does_not_load_another_chats_artifact_body(self):
        target_a = self.write_target("chat-a.typ", "= A\n\nCHAT-A-PRIVATE-BODY\n")
        target_b = self.write_target("chat-b.typ", "= B\n\nCHAT-B-PRIVATE-BODY\n")
        for session, owner, target in (
            ("session-a", "chat-A", target_a),
            ("session-b", "chat-B", target_b),
        ):
            opened = self.call({
                "command": "open",
                "session": session,
                "pair": "LaTeX / Typst",
                "path": str(target),
                "harness_session": owner,
            })
            self.assertTrue(opened.get("ok"), opened)

        scoped = self.call({
            "command": "state",
            "include_content": True,
            "content_owner_session": "chat-A",
        })
        sessions = {entry["session"]: entry for entry in scoped["sessions"]}
        self.assertIn("CHAT-A-PRIVATE-BODY", sessions["session-a"].get("agentViewPreview", ""))
        self.assertNotIn("agentViewPreview", sessions["session-b"])
        self.assertNotIn("CHAT-B-PRIVATE-BODY", json.dumps(scoped))
        self.assertTrue(scoped["contentIncluded"])


# ---------------------------------------------------------------------------
# P1-3  chat ownership and candidate-preview meaning
# ---------------------------------------------------------------------------


class ChatScopedPreviewTests(BridgeHarness):
    """The status payload must distinguish ownership and preview from success.

    Two defects were reproduced in the real Harness:

    * a fresh chat showed a pending PRQL/SV preview left by another chat;
    * a Nushell pipeline outside the converter subset showed "preview ready"
      even though the projection was entirely pass-through and no requested
      edit could be translated.
    """

    def _open_nu(self, session: str = "nu", owner: str | None = "chat-A") -> Path:
        target = self.write_target("request_summary.nu", NU_ALL_PASSTHROUGH)
        command = {
            "command": "open",
            "session": session,
            "pair": "Bash / Nushell (experimental)",
            "path": str(target),
        }
        if owner is not None:
            command["harness_session"] = owner
        opened = self.call(command)
        self.assertTrue(opened.get("ok"), opened)
        return target

    def test_open_records_the_harness_chat_owner(self):
        self._open_nu("owned", "chat-A")
        state = self.call({"command": "state", "session": "owned"})
        entry = state["sessions"][0]
        self.assertEqual(entry["ownerSessionId"], "chat-A")

    def test_all_passthrough_preview_is_a_candidate_not_an_edit(self):
        target = self._open_nu("passthrough", "chat-A")
        before = target.read_bytes()
        preview = self.call({"command": "preview", "session": "passthrough"})
        self.assertTrue(preview.get("ok"), preview)
        self.assertTrue(preview["candidateOnly"])
        self.assertFalse(preview["requestVerified"])
        self.assertFalse(preview["hasConvertibleContent"])
        self.assertFalse(preview["viewEdited"])
        self.assertEqual(preview["conversion"], "passthrough-only")
        self.assertTrue(
            any("No convertible content" in note for note in preview["notes"]),
            preview["notes"],
        )
        self.assertTrue(
            any("has not changed since it was opened" in note for note in preview["notes"]),
            preview["notes"],
        )
        self.assertEqual(target.read_bytes(), before, "a preview wrote the target")

        state = self.call({"command": "state", "session": "passthrough"})
        entry = state["sessions"][0]
        self.assertFalse(
            entry["hasPreview"],
            "a no-edit, all-pass-through preview must not be a pending delivery",
        )
        self.assertTrue(entry["previewCandidate"], "the candidate itself is still reportable")
        self.assertFalse(entry["hasConvertibleContent"])
        self.assertFalse(entry["viewEdited"])
        self.assertEqual(entry["conversion"], "passthrough-only")

    def test_edited_translatable_preview_is_edited_but_never_verified(self):
        # PRQL/SQL has a deterministic engine and no external compiler gate, so
        # this test measures the status semantics rather than whether typst is
        # installed on the test machine.
        target = self.write_target("query.prql", "from employees\nselect {name, age}\n")
        opened = self.call(
            {
                "command": "open",
                "session": "edited",
                "pair": "PRQL / SQL",
                "path": str(target),
                "harness_session": "chat-A",
            }
        )
        self.assertTrue(opened.get("ok"), opened)
        edited = opened["agentView"].replace(
            "FROM\n  employees;", "FROM\n  employees\nWHERE age > 30;"
        )
        self.assertNotEqual(edited, opened["agentView"], "the fixture did not produce an edit")
        self.call(
            {
                "command": "write",
                "session": "edited",
                "content": edited,
            }
        )
        preview = self.call({"command": "preview", "session": "edited"})
        self.assertTrue(preview.get("ok"), preview)
        self.assertTrue(preview["hasConvertibleContent"])
        self.assertTrue(preview["viewEdited"])
        self.assertFalse(preview["requestVerified"])
        state = self.call({"command": "state", "session": "edited"})
        entry = state["sessions"][0]
        self.assertTrue(entry["hasPreview"])
        self.assertTrue(entry["previewCandidate"])
        self.assertTrue(entry["viewEdited"])
        self.assertFalse(entry["requestVerified"])
        self.assertEqual(entry["conversion"], "translatable")

        # A committed delivery must not keep looking like a pending candidate.
        self.assertTrue(self.call({"command": "commit", "session": "edited"}).get("ok"))
        delivered = self.call({"command": "state", "session": "edited"})["sessions"][0]
        self.assertFalse(delivered["hasPreview"])
        self.assertFalse(delivered["previewCandidate"])

    def test_a_different_chat_cannot_preview_or_commit_the_session(self):
        target = self._open_nu("foreign", "chat-A")
        before = target.read_bytes()
        foreign_preview = self.call(
            {"command": "preview", "session": "foreign", "harness_session": "chat-B"}
        )
        self.assertFalse(foreign_preview.get("ok"))
        self.assertEqual(self.code(foreign_preview), "SESSION_OWNERSHIP_MISMATCH")
        foreign_commit = self.call(
            {"command": "commit", "session": "foreign", "harness_session": "chat-B"}
        )
        self.assertFalse(foreign_commit.get("ok"))
        self.assertEqual(self.code(foreign_commit), "SESSION_OWNERSHIP_MISMATCH")
        self.assertEqual(target.read_bytes(), before, "a foreign action wrote the target")

    def test_an_unattributed_session_cannot_be_claimed_by_a_chat(self):
        target = self._open_nu("unattributed", owner=None)
        state = self.call({"command": "state", "session": "unattributed"})
        self.assertIsNone(state["sessions"][0]["ownerSessionId"])
        refusal = self.call(
            {"command": "preview", "session": "unattributed", "harness_session": "chat-A"}
        )
        self.assertFalse(refusal.get("ok"))
        self.assertEqual(self.code(refusal), "SESSION_OWNERSHIP_MISMATCH")
        # The agent path (no chat parameter) still works; only browser actions
        # are required to prove ownership.
        preview = self.call({"command": "preview", "session": "unattributed"})
        self.assertTrue(preview.get("ok"), preview)


if __name__ == "__main__":
    unittest.main(verbosity=2)
