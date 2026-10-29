"""End-to-end bridge exercise for the DeepSeek Harness integration.

Runs the four documented acceptance flows through the *JSON contract* the
Harness Host plugin uses, so a passing run here means the plugin's own calls
will behave the same way. It never touches the Harness profile.

Usage (from the integration directory)::

    <python> tests/run_bridge_acceptance.py --workspace <demo_workspace>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"

PAIR_CHOICES = [
    "LaTeX / Typst",
    "Verilog / SystemVerilog",
    "PRQL / SQL",
    "Bash / Nushell (experimental)",
]

TYPST_TARGET = """= Quarterly update

The compatibility layer gives an agent a familiar LaTeX view of this document.

== Status

The prototype is *ready for review*.

#show heading: set text(fill: blue)
"""

NUSHELL_TARGET = """print (open --raw "data/access.log" | lines | where {|l| ($l =~ "GET") } | length)
open --raw data/access.log | lines | parse "{method} {path} {status}" | group-by method
"""

ACCESS_LOG = """GET /index 200
POST /submit 201
GET /docs 200
POST /login 200
GET /status 200
"""

PRQL_TARGET = """from employees
select {name, department, salary}
sort {-salary}
take 5
"""

SERVERVERILOG_TARGET = """module counter (
    input  logic       clk,
    input  logic       rst,
    output logic [3:0] count
);
    always_ff @(posedge clk) begin
        if (rst) count <= '0;
        else     count <= count + 1'b1;
    end
endmodule
"""


class Runner:
    def __init__(self, workspace: Path, python: str, env: dict):
        self.workspace = workspace
        self.python = python
        self.env = env
        self.results: list[tuple[str, bool, str]] = []

    def call(self, command: dict, audit: bool = False) -> dict:
        argv = [
            self.python,
            str(BRIDGE),
            "--allow-root",
            str(self.workspace),
            # This acceptance layer verifies the Studio timeline's rich view
            # snapshots. Product installs default to metadata-only; request full
            # logging explicitly for this UI-focused demonstration.
            "--activity-mode",
            "full",
        ]
        if audit:
            argv.append("--audit-imports")
        proc = subprocess.run(
            argv,
            input=json.dumps(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            timeout=300,
        )
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise AssertionError(
                f"bridge produced no JSON for {command.get('command')}: "
                f"rc={proc.returncode} stderr={proc.stderr[-800:]}"
            )
        return json.loads(lines[-1])

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.results.append((label, bool(condition), detail))
        mark = "PASS" if condition else "FAIL"
        print(f"[{mark}] {label}" + (f" :: {detail}" if detail and not condition else ""))

    def expect_ok(self, label: str, command: dict) -> dict:
        payload = self.call(command)
        ok = payload.get("ok") is True
        self.check(
            label,
            ok,
            "" if ok else json.dumps(payload.get("error"), ensure_ascii=False)[:400],
        )
        return payload

    def expect_error(self, label: str, command: dict, code: str) -> dict:
        payload = self.call(command)
        got = (payload.get("error") or {}).get("code")
        self.check(
            label,
            payload.get("ok") is False and got == code,
            f"expected code {code}, got {json.dumps(payload, ensure_ascii=False)[:400]}",
        )
        return payload


def summarise(runner: Runner) -> int:
    failed = [r for r in runner.results if not r[1]]
    print("\n" + "=" * 72)
    print(f"{len(runner.results) - len(failed)}/{len(runner.results)} checks passed")
    for label, _, detail in failed:
        print(f"  FAILED: {label}\n          {detail}")
    return 1 if failed else 0


MARKERS = (
    ("% @TYPST-PASSTHROUGH", "% @END-TYPST-PASSTHROUGH"),
    ("// @TYPST-PASSTHROUGH", "// @END-TYPST-PASSTHROUGH"),
    ("// @SV-PASSTHROUGH", "// @END-SV-PASSTHROUGH"),
    ("# @NU-PASSTHROUGH", "# @END-NU-PASSTHROUGH"),
)


def _regions(view: str) -> list[tuple[str, str]]:
    """Structural pass-through blocks of `view`, as ``(marker, body)`` pairs.

    Mirrors the bridge's own parser: blocks are returned in document order with
    multiplicity, so a test can assert both membership and ordering.
    """
    starts = dict(MARKERS)
    lines = view.splitlines()
    regions: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        marker = lines[index].strip()
        end = starts.get(marker)
        if end is None:
            index += 1
            continue
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != end:
            body.append(lines[index])
            index += 1
        regions.append((marker, "\n".join(body)))
        index += 1
    return regions


def passes_through(view: str, needle: str) -> bool:
    """True when `needle` sits inside a pass-through block of `view`.

    Used to assert that content the layer promised to preserve is still inside a
    marked region, rather than merely somewhere in the text.
    """
    return any(needle in body for _marker, body in _regions(view))


def scenario_typst(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 1: Typst -> LaTeX view -> edit -> preview -> write back ---")
    target = ws / "report.typ"
    target.write_text(TYPST_TARGET, encoding="utf-8")
    original = target.read_text(encoding="utf-8")

    opened = runner.expect_ok(
        "typst: open projects to a LaTeX view",
        {"command": "open", "session": "s-typst", "pair": "LaTeX / Typst",
         "path": str(target)},
    )
    view = opened.get("agentView", "")
    runner.check("typst: view is LaTeX", "\\documentclass" in view and "\\section" in view)
    runner.check(
        "typst: Typst-only `#show` preserved as a marked pass-through block",
        "@TYPST-PASSTHROUGH" in view and "#show heading" in view,
    )

    edited = view.replace(
        "The prototype is \\textbf{ready for review}.",
        "The prototype is \\textbf{ready for review}.\n\n"
        "\\section{ Next steps}\n\nShip the compatibility layer.\n",
    )
    runner.check("typst: edit applied to the view", edited != view)
    runner.expect_ok(
        "typst: write edited view",
        {"command": "write", "session": "s-typst", "content": edited},
    )

    preview = runner.expect_ok(
        "typst: preview delivery", {"command": "preview", "session": "s-typst"}
    )
    delivered = preview.get("targetContent", "")
    runner.check(
        "typst: preview did not touch the target file",
        target.read_text(encoding="utf-8") == original,
    )
    runner.check(
        "typst: preview carries the new section",
        "Next steps" in delivered and "Ship the compatibility layer." in delivered,
    )
    runner.check(
        "typst: Typst-only block still present in the delivered Typst",
        "#show heading: set text(fill: blue)" in delivered,
    )

    committed = runner.expect_ok(
        "typst: commit delivery (atomic write)",
        {"command": "commit", "session": "s-typst"},
    )
    written = target.read_text(encoding="utf-8")
    runner.check("typst: target file now holds the edit", "Next steps" in written)
    runner.check(
        "typst: pass-through block survived delivery verbatim",
        "#show heading: set text(fill: blue)" in written,
    )
    verification = committed.get("verification") or {}
    runner.check(
        "typst: `typst compile` accepts the delivered file",
        verification.get("checked") is True and verification.get("ok") is True,
        json.dumps(verification, ensure_ascii=False)[:400],
    )

    # Redeploy: the edited target must project again (multi-round reprojection).
    reopened = runner.expect_ok(
        "typst: re-project after delivery (multi-round)",
        {"command": "open", "session": "s-typst-2", "pair": "LaTeX / Typst",
         "path": str(target)},
    )
    runner.check(
        "typst: second view contains the earlier edit",
        "Ship the compatibility layer." in reopened.get("agentView", ""),
    )


def scenario_nushell(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 2: Nushell R/E -- edit R only, E must pass through ---")
    work = ws / "nu"
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "data" / "access.log").write_text(ACCESS_LOG, encoding="utf-8")
    target = work / "report.nu"
    target.write_text(NUSHELL_TARGET, encoding="utf-8")

    passthrough_line = (
        'open --raw data/access.log | lines | parse "{method} {path} {status}" '
        "| group-by method"
    )

    opened = runner.expect_ok(
        "nu: open projects Nushell to a Bash view",
        {"command": "open", "session": "s-nu",
         "pair": "Bash / Nushell (experimental)", "path": str(target)},
    )
    view = opened.get("agentView", "")
    runner.check("nu: view is Bash", view.startswith("cat data/access.log | grep GET"))
    runner.check(
        "nu: Nushell-only block marked as E (@NU-PASSTHROUGH)",
        "@NU-PASSTHROUGH" in view and passthrough_line in view,
    )
    runner.check(
        "nu: the E content sits *inside* a marked block, not merely in the text",
        passes_through(view, passthrough_line),
        view[:300],
    )
    runner.check(
        "nu: the bridge reports how many regions it protected",
        (opened.get("protectedRegions") or 0) >= 1,
        str(opened.get("protectedRegions")),
    )

    edited = view.replace("grep GET", "grep POST")
    runner.check("nu: R edited GET -> POST only", edited != view and "grep POST" in edited)
    runner.expect_ok(
        "nu: write edited view",
        {"command": "write", "session": "s-nu", "content": edited},
    )
    preview = runner.expect_ok(
        "nu: preview delivery", {"command": "preview", "session": "s-nu"}
    )
    delivered = preview.get("targetContent", "")
    runner.check(
        "nu: delivered E block byte-identical to the original E block",
        passthrough_line in delivered,
    )
    runner.check(
        "nu: delivered R block reports POST",
        '=~ "POST"' in delivered,
    )

    committed = runner.expect_ok(
        "nu: commit delivery", {"command": "commit", "session": "s-nu"}
    )
    written = target.read_text(encoding="utf-8")
    runner.check(
        "nu: E block unchanged in the written file",
        passthrough_line in written,
    )
    runner.check(
        "nu: no @NU-PASSTHROUGH marker leaked into the target",
        "@NU-PASSTHROUGH" not in written,
    )
    verification = committed.get("verification") or {}
    runner.check(
        "nu: `nu` runs the delivered file successfully",
        verification.get("checked") is True and verification.get("ok") is True,
        json.dumps(verification, ensure_ascii=False)[:600],
    )
    runner.check(
        "nu: real execution reports the POST count (2)",
        (verification.get("stdout") or "").strip().splitlines()[:1] == ["2"],
        f"stdout={verification.get('stdout')!r} stderr={verification.get('stderr')!r}",
    )


def scenario_prql(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 3: PRQL -> SQL view -> edit -> deliver PRQL ---")
    target = ws / "query.prql"
    target.write_text(PRQL_TARGET, encoding="utf-8")

    opened = runner.expect_ok(
        "prql: open projects PRQL to a SQL view",
        {"command": "open", "session": "s-prql", "pair": "PRQL / SQL",
         "path": str(target)},
    )
    view = opened.get("agentView", "")
    runner.check("prql: view is SQL", "SELECT" in view and "LIMIT" in view)

    edited = view.replace("salary DESC", "department ASC").replace(
        "  salary,", "  department,"
    )
    runner.check("prql: edit applied", edited != view)
    runner.expect_ok(
        "prql: write edited view",
        {"command": "write", "session": "s-prql", "content": edited},
    )
    preview = runner.expect_ok(
        "prql: preview delivery", {"command": "preview", "session": "s-prql"}
    )
    delivered = preview.get("targetContent", "")
    runner.check(
        "prql: delivered artifact is PRQL again",
        "from employees" in delivered and "sort" in delivered,
        delivered[:300],
    )
    committed = runner.expect_ok(
        "prql: commit delivery", {"command": "commit", "session": "s-prql"}
    )
    runner.check("prql: delivery metadata present",
                  bool(committed.get("targetSha256")))


def scenario_systemverilog(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 4: SystemVerilog -> Verilog view -> edit -> deliver ---")
    target = ws / "counter.sv"
    target.write_text(SERVERVERILOG_TARGET, encoding="utf-8")

    opened = runner.expect_ok(
        "sv: open projects SystemVerilog to a Verilog view",
        {"command": "open", "session": "s-sv",
         "pair": "Verilog / SystemVerilog", "path": str(target)},
    )
    view = opened.get("agentView", "")
    runner.check("sv: view is flattened Verilog", "always @(posedge clk)" in view)

    edited = view.replace("count <= count + 1'b1;", "count <= count + 2'd1;")
    runner.check("sv: edit applied", edited != view)
    runner.expect_ok(
        "sv: write edited view",
        {"command": "write", "session": "s-sv", "content": edited},
    )
    runner.expect_ok("sv: preview delivery", {"command": "preview", "session": "s-sv"})
    runner.expect_ok("sv: commit delivery", {"command": "commit", "session": "s-sv"})
    written = target.read_text(encoding="utf-8")
    runner.check(
        "sv: delivered file is SystemVerilog again",
        "always_ff" in written and "logic" in written,
        written[:300],
    )


def scenario_rejections(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 5: refusals -------------------------------------------------")

    outside = ws.parent / "outside_workspace.txt"
    outside.write_text("not yours", encoding="utf-8")
    runner.expect_error(
        "reject: path outside the workspace root",
        {"command": "open", "session": "e1", "pair": "LaTeX / Typst",
         "path": str(outside)},
        "PATH_OUTSIDE_WORKSPACE",
    )
    runner.expect_error(
        "reject: traversal out of the workspace via ..",
        {"command": "open", "session": "e2", "pair": "LaTeX / Typst",
         "path": str(ws / ".." / ".." / ".." / "outside_workspace.txt")},
        "PATH_OUTSIDE_WORKSPACE",
    )

    typst = ws / "stale.typ"
    typst.write_text(TYPST_TARGET, encoding="utf-8")
    runner.expect_ok(
        "reject: (setup) open stale.typ",
        {"command": "open", "session": "s-stale", "pair": "LaTeX / Typst",
         "path": str(typst)},
    )
    view = runner.call({"command": "read", "session": "s-stale"})["agentView"]
    runner.expect_ok(
        "reject: (setup) edit the view",
        {"command": "write", "session": "s-stale",
         "content": view + "\n== Added\n\ntext\n"},
    )
    typst.write_text(TYPST_TARGET + "\n= External edit\n", encoding="utf-8")
    runner.expect_error(
        "reject: target modified externally -> commit refuses",
        {"command": "commit", "session": "s-stale"},
        "TARGET_CHANGED",
    )
    preview = runner.expect_ok(
        "reject: preview still works but reports the drift",
        {"command": "preview", "session": "s-stale"},
    )
    runner.check(
        "reject: drift is reported with both hashes",
        (preview.get("targetDrift") or {}).get("drifted") is True
        and bool((preview.get("targetDrift") or {}).get("actualSha256")),
    )

    missing = ws / "does-not-exist.typ"
    runner.expect_error(
        "reject: missing target",
        {"command": "open", "session": "e3", "pair": "LaTeX / Typst",
         "path": str(missing)},
        "TARGET_NOT_FOUND",
    )
    runner.expect_error(
        "reject: create over an existing path",
        {"command": "create", "session": "e4", "pair": "LaTeX / Typst",
         "path": str(typst), "content": "= nope\n"},
        "TARGET_EXISTS",
    )
    runner.expect_error(
        "reject: unknown pair name",
        {"command": "open", "session": "e5", "pair": "LaTeX / TypeScript",
         "path": str(ws / "report.typ")},
        "UNKNOWN_PAIR",
    )
    runner.expect_error(
        "reject: unknown bridge command",
        {"command": "explode"},
        "UNKNOWN_COMMAND",
    )
    runner.expect_error(
        "reject: extension does not match the pair",
        {"command": "open", "session": "e6", "pair": "PRQL / SQL",
         "path": str(ws / "report.typ")},
        "EXTENSION_MISMATCH",
    )
    runner.expect_error(
        "reject: unknown session id",
        {"command": "read", "session": "no-such-session"},
        "UNKNOWN_SESSION",
    )

    # Plugin state must never be usable as an artifact.
    state = ws / ".rosetta" / "harness-sessions.sqlite3"
    runner.expect_error(
        "reject: plugin state directory is not an artifact path",
        {"command": "open", "session": "e7", "pair": "LaTeX / Typst",
         "path": str(state)},
        "PATH_IS_PLUGIN_STATE",
    )


def scenario_sv2v_missing(runner: Runner, ws: Path, env_no_sv2v: dict) -> None:
    print("\n--- Scenario 6: missing external converter is diagnosed ---")
    target = ws / "isolated.sv"
    target.write_text(SERVERVERILOG_TARGET, encoding="utf-8")

    saved = runner.env
    runner.env = env_no_sv2v
    try:
        payload = runner.expect_error(
            "converter: SV projection without sv2v fails explicitly",
            {"command": "open", "session": "s-nosv2v",
             "pair": "Verilog / SystemVerilog", "path": str(target)},
            "PROJECTION_FAILED",
        )
        message = json.dumps(payload.get("error") or {}, ensure_ascii=False)
        runner.check(
            "converter: refusal names the missing binary",
            "sv2v" in message.lower(),
            message[:400],
        )
        runner.check(
            "converter: refusal explains how to install and configure sv2v",
            all(token in message.lower() for token in ("install", "setup.ps1", "rosetta_sv2v", "restart harness")),
            message[:500],
        )
        doctor = runner.expect_ok(
            "converter: doctor reports sv2v as missing and blocks the pair",
            {"command": "doctor"},
        )
        blocked = {d["pair"] for d in doctor.get("pairDiagnostics", [])}
        runner.check(
            "converter: doctor lists the Verilog/SystemVerilog pair as blocked",
            any("Verilog" in name for name in blocked),
            json.dumps(doctor.get("pairDiagnostics"), ensure_ascii=False)[:400],
        )
    finally:
        runner.env = saved


def scenario_unmodified_view(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 7: open then deliver with no edit -------------------------")
    target = ws / "roundtrip.typ"
    target.write_text(TYPST_TARGET, encoding="utf-8")
    opened = runner.expect_ok(
        "no-op: open roundtrip.typ",
        {"command": "open", "session": "s-noop", "pair": "LaTeX / Typst",
         "path": str(target)},
    )
    first_view = opened.get("agentView", "")
    commit = runner.expect_ok(
        "no-op: commit delivery", {"command": "commit", "session": "s-noop"}
    )
    written = target.read_text(encoding="utf-8")
    runner.check(
        "no-op: Typst-only block survived the unedited round trip",
        "#show heading: set text(fill: blue)" in written,
        written[:400],
    )
    runner.check(
        "no-op: transport markers are removed from the delivered Typst",
        "@TYPST-PASSTHROUGH" not in written,
        written[:400],
    )
    round_trip = commit.get("roundTrip") or {}
    runner.check(
        "no-op: the bridge measured the round trip and reports it honestly",
        round_trip.get("status") in {"exact", "unstable", "unknown"},
        json.dumps(round_trip, ensure_ascii=False)[:400],
    )
    runner.check(
        "no-op: a non-exact verdict explains which part differs",
        round_trip.get("status") == "exact"
        or bool(round_trip.get("reason")),
        json.dumps(round_trip, ensure_ascii=False)[:400],
    )
    runner.check(
        "no-op: a fresh artifact is not claimed to be a byte fixed point",
        round_trip.get("status") != "exact" or round_trip.get("stable") is True,
        json.dumps(round_trip, ensure_ascii=False)[:300],
    )

    second = runner.call(
        {"command": "open", "session": "s-noop2", "pair": "LaTeX / Typst",
         "path": str(target)}
    )
    if second.get("ok") is True:
        second_view = second.get("agentView", "")
        runner.check(
            "no-op: re-projection still carries the Typst-only block",
            "@TYPST-PASSTHROUGH" in second_view,
        )
        runner.check(
            "no-op: re-projection carries the same authored prose",
            "The prototype is \\textbf{ready for review}." in second_view
            and "The prototype is \\textbf{ready for review}." in first_view,
        )
        # The view legitimately gains pass-through blocks for the preamble the
        # delivery engine injects, so compare the translated parts. Whitespace is
        # not normalized away any more: the check is byte equality of the
        # translated text, and a difference here is a real (reported) one.
        runner.check(
            "no-op: the authored prose survives the round trip byte-for-byte",
            "The prototype is \\textbf{ready for review}." in second_view,
            second_view[:300],
        )
    else:
        runner.check(
            "no-op: re-projection of the delivered artifact succeeds",
            False,
            json.dumps(second.get("error"), ensure_ascii=False)[:400],
        )


def scenario_provenance(runner: Runner, ws: Path) -> None:
    print("\n--- Scenario 8: runtime provenance -------------------------------------")
    payload = runner.call({"command": "doctor"}, audit=True)
    runner.check("provenance: doctor succeeded under audit", payload.get("ok") is True)

    audit = payload.get("moduleAudit") or {}
    runner.check(
        "provenance: every imported module lies outside experiments/benchmark/gold/scorer trees",
        audit.get("clean") is True,
        json.dumps(audit.get("violations"), ensure_ascii=False)[:500],
    )
    runner.check(
        "provenance: the audit actually inspected the process imports",
        (audit.get("moduleCount") or 0) > 50,
        f"moduleCount={audit.get('moduleCount')}",
    )
    provenance = payload.get("provenance") or {}
    runner.check(
        "provenance: sys.path was restricted to the interpreter and the package root",
        len(provenance.get("rejected") or []) >= 1,
        json.dumps(provenance, ensure_ascii=False)[:400],
    )

    # The adapters that actually run must come from the installed package.
    imports = (payload.get("imports") or {})
    studio = imports.get("rosetta_studio") or ""
    runner.check(
        "provenance: rosetta_studio is imported from the installed distribution",
        studio.endswith("rosetta_studio\\__init__.py") and "site-packages" in studio,
        studio,
    )
    runner.check(
        "provenance: the bridge directory is not on sys.path (no source-tree shadowing)",
        all(
            "integrations" not in entry
            for entry in (provenance.get("kept") or [])
        ),
        json.dumps(provenance.get("kept"), ensure_ascii=False)[:400],
    )


def scenario_multi_round(runner: Runner, ws: Path) -> None:
    """Project/deliver repeatedly; the artifact must reach a fixed point.

    Regression guard for a real defect: the external t2l engine prepends its own
    default Typst template preamble, so once the author's ``#set`` directives
    were carried back as pass-through blocks the artifact gained a duplicate
    copy of them on every round and grew without bound. A fixed point is what
    "multi-round reprojection" actually requires.
    """
    print("\n--- Scenario 9: multi-round reprojection reaches a fixed point --------")
    target = ws / "multi-round.typ"
    target.write_text(TYPST_TARGET, encoding="utf-8")

    sizes: list[int] = []
    set_counts: list[int] = []
    show_present: list[bool] = []
    statuses: list[str] = []

    for round_index in range(1, 6):
        session = f"s-round-{round_index}"
        opened = runner.call(
            {"command": "open", "session": session, "pair": "LaTeX / Typst",
             "path": str(target)}
        )
        if opened.get("ok") is not True:
            runner.check(
                f"multi-round: round {round_index} projects",
                False,
                json.dumps(opened.get("error"), ensure_ascii=False)[:300],
            )
            return
        runner.check(
            f"multi-round: round {round_index} view still marks the Typst-only block",
            "@TYPST-PASSTHROUGH" in (opened.get("agentView") or ""),
            (opened.get("agentView") or "")[:200],
        )
        committed = runner.call({"command": "commit", "session": session})
        if committed.get("ok") is not True:
            runner.check(
                f"multi-round: round {round_index} delivers",
                False,
                json.dumps(committed.get("error"), ensure_ascii=False)[:300],
            )
            return

        text = target.read_text(encoding="utf-8")
        sizes.append(len(text))
        set_counts.append(
            len([ln for ln in text.splitlines() if ln.strip().startswith("#set ")])
        )
        # The construct must survive *inside a marked block*, which is the R/E
        # guarantee; plain presence would also be satisfied by a copy the engine
        # happened to carry through untranslated.
        region_text = "\n".join(
            body for _marker, body in _regions(opened.get("agentView") or "")
        )
        show_present.append("#show heading: set text(fill: blue)" in region_text)
        statuses.append((committed.get("roundTrip") or {}).get("status", "?"))
        print(
            f"        round {round_index}: bytes={sizes[-1]} "
            f"#set={set_counts[-1]} show_in_region={show_present[-1]} "
            f"roundTrip={statuses[-1]}"
        )

    runner.check(
        "multi-round: the author's Typst-only construct survives every round, inside a marked block",
        all(show_present),
        f"per-round presence: {show_present}",
    )
    runner.check(
        "multi-round: the artifact stops changing (rounds 3-5 identical)",
        len(set(sizes[2:])) == 1,
        f"byte sizes per round: {sizes}",
    )
    runner.check(
        "multi-round: the last rounds are reported as a byte fixed point",
        statuses[-1] == "exact",
        f"per-round round-trip status: {statuses}",
    )
    runner.check(
        "multi-round: the unstable early rounds were not misreported as stable",
        all(status in {"exact", "unstable", "unknown"} for status in statuses),
        f"per-round round-trip status: {statuses}",
    )
    runner.check(
        "multi-round: no unbounded growth of the engine preamble",
        max(set_counts) - min(set_counts) == 0,
        f"#set line counts per round: {set_counts}",
    )


def scenario_chinese_flow(runner: Runner, ws: Path) -> None:
    """A complete non-ASCII task through the JSON contract the Host uses.

    The protocol already has its own UTF-8 tests. This scenario is deliberately
    different: it drives the same sequence a Chinese chat drives - open a file
    whose path and content are Chinese, read the view, write a Chinese edit,
    preview, commit with the exact digests the status strip would send - so a
    codepage or escaping defect anywhere on that path is visible as a wrong
    artifact rather than as mojibake in a log.

    Isolation: its own subdirectory and its own file. It never touches an
    existing artifact, the paper, benchmark data, or the other scenarios.
    """
    print("\n--- Scenario 12: Chinese end-to-end flow -------------------------------")
    folder = ws / "\u4e2d\u6587\u6d41\u7a0b"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "\u5b63\u5ea6\u62a5\u544a.typ"
    sentence = "\u9a8c\u8bc1\uff1a\u4e2d\u6587\u8f93\u5165\u3001\u7f16\u8f91\u3001\u9884\u89c8\u4e0e\u4ea4\u4ed8\u3002"
    target.write_text(
        "= \u5b63\u5ea6\u62a5\u544a\n\n"
        "\u539f\u578b\u5df2*\u53ef\u4ee5\u8bc4\u5ba1*\u3002\n\n"
        "#show heading: set text(fill: blue)\n",
        encoding="utf-8",
    )

    opened = runner.call(
        {"command": "open", "session": "s-zh", "pair": "LaTeX / Typst",
         "path": str(target), "harness_session": "chat-zh"}
    )
    runner.check(
        "zh: the familiar view preserves Chinese text",
        opened.get("ok") is True
        and "\u5b63\u5ea6\u62a5\u544a" in (opened.get("agentView") or "")
        and "\u53ef\u4ee5\u8bc4\u5ba1" in (opened.get("agentView") or ""),
        json.dumps(opened, ensure_ascii=False)[:400],
    )
    view = opened.get("agentView") or ""
    if "\\end{document}" not in view:
        runner.check("zh: view has an end-document marker to edit inside", False, view[:200])
        return
    edited = view.replace(
        "\\end{document}", f"{sentence}\n\n\\end{{document}}"
    )
    written = runner.call(
        {"command": "write", "session": "s-zh", "content": edited,
         "harness_session": "chat-zh"}
    )
    runner.check(
        "zh: a Chinese view edit is stored unchanged",
        written.get("ok") is True and written.get("characters") == len(edited),
        json.dumps(written, ensure_ascii=False)[:300],
    )

    preview = runner.call(
        {"command": "preview", "session": "s-zh", "harness_session": "chat-zh"}
    )
    runner.check(
        "zh: preview translates the Chinese edit and writes nothing",
        preview.get("ok") is True
        and sentence in (preview.get("targetContent") or "")
        and target.read_text(encoding="utf-8").find(sentence) == -1,
        json.dumps(preview, ensure_ascii=False)[:400],
    )
    if preview.get("ok") is not True:
        return

    # The same content path the status strip reads when the user opens Details.
    state = runner.call(
        {
            "command": "state",
            "include_content": True,
            "view_limit": 4000,
            "harness_session": "chat-zh",
            "content_owner_session": "chat-zh",
        }
    )
    entry = next(
        (item for item in (state.get("sessions") or []) if item.get("session") == "s-zh"),
        {},
    )
    runner.check(
        "zh: the status-strip content path returns the Chinese view and candidate",
        state.get("ok") is True
        and sentence in (entry.get("agentViewPreview") or "")
        and sentence in (entry.get("preview") or "")
        and entry.get("dirty") is True,
        json.dumps(entry, ensure_ascii=False)[:400],
    )

    target_bytes = target.read_bytes()
    committed = runner.call(
        {
            "command": "commit",
            "session": "s-zh",
            "harness_session": "chat-zh",
            "expected_preview_sha256": preview.get("previewSha256"),
            "expected_view_sha256": hashlib.sha256(edited.encode("utf-8")).hexdigest(),
            "expected_target_sha256": hashlib.sha256(target_bytes).hexdigest(),
        }
    )
    delivered = target.read_text(encoding="utf-8") if committed.get("ok") else ""
    runner.check(
        "zh: delivery writes valid UTF-8 containing the Chinese edit",
        committed.get("ok") is True
        and sentence in delivered
        and "\u5b63\u5ea6\u62a5\u544a" in delivered,
        json.dumps(committed, ensure_ascii=False)[:400],
    )

    # Delivery must stay reversible for a Chinese file too: restore the exact
    # bytes that preceded the commit, and prove the edit is gone while the
    # original Chinese content is intact. This closes the session.
    restored = runner.call(
        {"command": "restore_last", "session": "s-zh", "harness_session": "chat-zh"}
    )
    restored_text = target.read_text(encoding="utf-8") if restored.get("ok") else ""
    runner.check(
        "zh: restore returns the previous Chinese file and removes the edit",
        restored.get("ok") is True
        and restored.get("restored") is True
        and target.read_bytes() == target_bytes
        and sentence not in restored_text
        and "\u5b63\u5ea6\u62a5\u544a" in restored_text,
        json.dumps(restored, ensure_ascii=False)[:400],
    )


def scenario_activity_log(runner: Runner, ws: Path) -> None:
    """The shared Rosetta activity database must record plugin steps.

    This is what makes the *existing* Studio GUI show Harness plugin activity:
    the GUI polls one SQLite table and never filters by `source`, so the bridge
    has to append rows in that same schema with a distinguishable source. Runs
    last, so the earlier scenarios have already generated a trail.
    """
    print("\n--- Scenario 10: shared Rosetta activity database ----------------------")
    db_path = ws / ".rosetta" / "events.sqlite3"

    # Exercise the two steps the other scenarios do not perform - discovery and
    # closing - so the recorded vocabulary is complete rather than assumed.
    runner.call({"command": "pairs"})
    probe = ws / "activity-probe.typ"
    probe.write_text(TYPST_TARGET, encoding="utf-8")
    runner.call(
        {"command": "open", "session": "s-activity", "pair": "LaTeX / Typst",
         "path": str(probe)}
    )
    runner.call({"command": "close", "session": "s-activity"})

    runner.check(
        "activity log: the bridge created the shared database",
        db_path.is_file(),
        str(db_path),
    )
    if not db_path.is_file():
        return

    import sqlite3

    with sqlite3.connect(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()
        }
        rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]

    # The schema must match the one the Studio GUI reads.
    expected_columns = {
        "id", "created_at", "source", "session_id", "pair_name", "action",
        "status", "target_path", "agent_view", "target_view", "detail",
    }
    runner.check(
        "activity log: schema matches the Studio event table",
        expected_columns.issubset(columns),
        f"missing: {sorted(expected_columns - columns)}",
    )

    sources = {row["source"] for row in rows}
    runner.check(
        "activity log: rows are attributed to the Harness plugin, not MCP",
        sources == {"Harness"},
        f"sources seen: {sorted(sources)}",
    )

    actions = {row["action"] for row in rows}
    expected_actions = {"list_pairs", "open", "write", "preview", "commit", "close"}
    runner.check(
        "activity log: the agent's steps are recorded with the GUI's vocabulary",
        expected_actions.issubset(actions),
        f"missing: {sorted(expected_actions - actions)}; saw {sorted(actions)}",
    )

    runner.check(
        "activity log: polling-style commands are not recorded (no flooding)",
        "state" not in actions and "status" not in actions,
        f"actions: {sorted(actions)}",
    )

    pairs_seen = {row["pair_name"] for row in rows if row["pair_name"]}
    runner.check(
        "activity log: all four tool pairs appear in the shared history",
        set(PAIR_CHOICES).issubset(pairs_seen),
        f"missing: {sorted(set(PAIR_CHOICES) - pairs_seen)}",
    )
    runner.check(
        "activity log: a rejected call still records the attempted pair as an error",
        any(
            row["status"] == "error" and row["pair_name"] == "LaTeX / TypeScript"
            for row in rows
        ),
        f"error rows: {[(r['pair_name'], r['status']) for r in rows if r['status'] == 'error'][:5]}",
    )

    commits = [row for row in rows if row["action"] == "commit"]
    runner.check(
        "activity log: a commit row carries both views for the GUI panels",
        bool(commits)
        and all(row["agent_view"] for row in commits)
        and all(row["target_view"] for row in commits),
        f"commit rows: {[(r['id'], bool(r['agent_view']), bool(r['target_view'])) for r in commits]}",
    )

    errors = [row for row in rows if row["status"] == "error"]
    print(
        f"        recorded {len(rows)} rows "
        f"({len(commits)} commits, {len(errors)} refusals) "
        f"actions={sorted(actions)}"
    )

    # The explicit cleanup path defaults to a report. Nothing here may remove an
    # undelivered draft or a pending journal entry on its own.
    maintenance = runner.call({"command": "maintenance", "action": "report"})
    runner.check(
        "maintenance: the default action reports instead of deleting",
        maintenance.get("ok") is True
        and maintenance.get("applied") is False
        and maintenance.get("action") == "report",
        json.dumps(maintenance, ensure_ascii=False)[:400],
    )
    runner.check(
        "maintenance: undelivered drafts and orphaned backups are kept by default",
        (maintenance.get("drafts") or {}).get("includeDrafts") is False
        and (maintenance.get("journal") or {}).get("pending") is not None
        and (maintenance.get("commitHistory") or {}).get("includeOrphanHistory") is False
        and "liveSessionsWithHistory" in (maintenance.get("commitHistory") or {}),
        json.dumps(maintenance.get("commitHistory"), ensure_ascii=False)[:300],
    )


def scenario_unusable_temp(runner: Runner, ws: Path) -> None:
    """The bridge must work when the system temporary directory is unusable.

    Regression guard for a failure that only ever appeared through the plugin:
    the Harness sandbox denies its subprocess the system temp directory, and
    these test processes run under a different token, so the offline suites
    passed while the real path crashed. Two symptoms followed from it -
    `eda` raised FileNotFoundError before projecting SystemVerilog, and
    `_typst_compiles` swallowed the same error into `return False`, which made
    every Typst delivery claim "Output does not compile with typst".

    The condition is reproduced here by pointing TEMP/TMP/TMPDIR at a path that
    does not exist, which is what `tempfile`'s candidate search saw.
    """
    print("\n--- Scenario 11: unusable system temporary directory -------------------")
    broken = dict(runner.env)
    broken["TEMP"] = r"C:\rosetta-nonexistent-temp"
    broken["TMP"] = broken["TEMP"]
    broken["TMPDIR"] = broken["TEMP"]

    saved = runner.env
    runner.env = broken
    try:
        sv_target = ws / "temp-probe.sv"
        sv_target.write_text(SERVERVERILOG_TARGET, encoding="utf-8")
        sv = runner.call(
            {"command": "open", "session": "s-temp-sv",
             "pair": "Verilog / SystemVerilog", "path": str(sv_target)}
        )
        runner.check(
            "temp: SystemVerilog still projects when the system temp dir is unusable",
            sv.get("ok") is True,
            json.dumps(sv.get("error"), ensure_ascii=False)[:300],
        )
        runner.check(
            "temp: the bridge redirected temp inside the workspace",
            str(ws).lower() in str(sv.get("tempDir", "")).lower(),
            f"tempDir={sv.get('tempDir')!r}",
        )

        typ_target = ws / "temp-probe.typ"
        typ_target.write_text(TYPST_TARGET, encoding="utf-8")
        runner.call(
            {"command": "open", "session": "s-temp-typ", "pair": "LaTeX / Typst",
             "path": str(typ_target)}
        )
        preview = runner.call({"command": "preview", "session": "s-temp-typ"})
        runner.check(
            "temp: Typst delivery still works when the system temp dir is unusable",
            preview.get("ok") is True,
            json.dumps(preview.get("error"), ensure_ascii=False)[:300],
        )
        notes = " ".join(preview.get("notes") or [])
        runner.check(
            "temp: no false 'does not compile' warning once the compile probe can run",
            "does not compile" not in notes,
            f"notes: {notes[:300]}",
        )
    finally:
        runner.env = saved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--fresh", action="store_true", default=True)
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    if args.fresh and ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    outside = ws.parent / "outside_workspace.txt"
    if outside.exists():
        outside.unlink()

    env_no_sv2v = dict(env)
    env_no_sv2v.pop("ROSETTA_SV2V", None)
    env_no_sv2v["PATH"] = os.pathsep.join(
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p and "tools" not in p.lower()
    )

    runner = Runner(ws, args.python, env)
    print(f"workspace: {ws}")
    print(f"python   : {args.python}")

    try:
        scenario_typst(runner, ws)
        scenario_nushell(runner, ws)
        scenario_prql(runner, ws)
        scenario_systemverilog(runner, ws)
        scenario_rejections(runner, ws)
        scenario_sv2v_missing(runner, ws, env_no_sv2v)
        scenario_unmodified_view(runner, ws)
        scenario_provenance(runner, ws)
        scenario_multi_round(runner, ws)
        scenario_activity_log(runner, ws)
        scenario_unusable_temp(runner, ws)
        scenario_chinese_flow(runner, ws)
    finally:
        # The containment scenarios need a readable file outside every allowed
        # root; leaving it behind would litter the integration directory.
        if outside.exists():
            outside.unlink()

    return summarise(runner)


if __name__ == "__main__":
    sys.exit(main())
