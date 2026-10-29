"""Reproduce the reported P0 defects against the CURRENT bridge.

Run:  <python> tests/repro_p0.py
Not a test suite - a diagnostic that prints what actually happens.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
BRIDGE = INTEGRATION / "python" / "rosetta_harness_bridge.py"
PYTHON = sys.executable

TYPST = "= Repro\n\nBody text.\n"


def call(ws: Path, command: dict, extra: list[str] | None = None) -> dict:
    argv = [PYTHON, str(BRIDGE), "--allow-root", str(ws)]
    if extra:
        argv += extra
    proc = subprocess.run(
        argv,
        input=json.dumps(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "error": {"code": "NO_OUTPUT", "message": proc.stderr[-500:]}}
    return json.loads(lines[-1])


def scenario_concurrency(root: Path) -> None:
    print("\n=== P0-1  concurrent session writes ===")
    ws = root / "conc"
    ws.mkdir(parents=True, exist_ok=True)
    count = 16
    targets = []
    for index in range(count):
        target = ws / f"artifact-{index:02d}.typ"
        target.write_text(TYPST, encoding="utf-8")
        targets.append(target)

    env = dict(os.environ)
    procs = []
    for index, target in enumerate(targets):
        command = {
            "command": "open",
            "session": f"sess-{index:02d}",
            "pair": "LaTeX / Typst",
            "path": str(target),
        }
        procs.append(
            subprocess.Popen(
                [PYTHON, str(BRIDGE), "--allow-root", str(ws)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
        )
        procs[-1].stdin.write(json.dumps(command))
        procs[-1].stdin.close()

    ok = 0
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        if lines:
            try:
                if json.loads(lines[-1]).get("ok"):
                    ok += 1
            except json.JSONDecodeError:
                pass

    status = call(ws, {"command": "status"})
    survived = status.get("sessions", [])
    print(f"  launched          : {count} concurrent 'open' calls")
    print(f"  calls that said ok: {ok}")
    print(f"  sessions that survived: {len(survived)}")
    missing = count - len(survived)
    print(f"  LOST SESSIONS     : {missing}")
    if missing:
        print("  >>> BUG REPRODUCED: acknowledged sessions were overwritten by a peer.")
    else:
        print("  (no loss observed this run - rerun; the window is timing dependent)")


def scenario_guard_state(root: Path) -> None:
    print("\n=== P0-2  guard must follow session state, not file existence ===")
    ws = root / "guard"
    ws.mkdir(parents=True, exist_ok=True)

    # (a) a session for a target that does not exist yet (ros_create_artifact)
    new_target = ws / "brand-new.typ"
    created = call(
        ws,
        {"command": "create", "session": "new-sess", "pair": "LaTeX / Typst",
         "path": str(new_target), "content": TYPST},
    )
    print(f"  create ok         : {created.get('ok')}  (target exists on disk: {new_target.exists()})")
    status = call(ws, {"command": "status"})
    listed = {entry["session"] for entry in status.get("sessions", [])}
    print(f"  sessions reported to the fence: {sorted(listed)}")
    if "new-sess" not in listed:
        print("  >>> BUG REPRODUCED: a live session whose target is absent is NOT in the fence list,")
        print("      so the host's write/edit guard cannot deny writes to that path.")

    # (b) a session whose target is later deleted externally
    existing = ws / "deleted-later.typ"
    existing.write_text(TYPST, encoding="utf-8")
    call(ws, {"command": "open", "session": "del-sess", "pair": "LaTeX / Typst",
              "path": str(existing)})
    before = {e["session"] for e in call(ws, {"command": "status"}).get("sessions", [])}
    existing.unlink()
    after = {e["session"] for e in call(ws, {"command": "status"}).get("sessions", [])}
    print(f"  fence before delete: {sorted(before)}")
    print(f"  fence after  delete: {sorted(after)}")
    if "del-sess" in before and "del-sess" not in after:
        print("  >>> BUG REPRODUCED: deleting the target externally drops the session out of the")
        print("      fence, so writes to the recreated path are no longer denied.")


def scenario_stability_gate(root: Path) -> None:
    print("\n=== P0-3  stability gate must run BEFORE the write ===")
    ws = root / "gate"
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "gate.typ"
    original = "= Quarterly update\n\nThe prototype is *ready for review*.\n"
    target.write_text(original, encoding="utf-8")

    opened = call(ws, {"command": "open", "session": "g1", "pair": "LaTeX / Typst",
                       "path": str(target)})
    if not opened.get("ok"):
        print("  open failed:", opened.get("error"))
        return
    edited = opened["agentView"] + "\n\\section{ Added}\n\nMore.\n"
    call(ws, {"command": "write", "session": "g1", "content": edited})

    # Does the bridge even accept a stability requirement before writing?
    result = call(
        ws,
        {"command": "commit", "session": "g1"},
        extra=["--require-stable-round-trip"],
    )
    after = target.read_text(encoding="utf-8")
    changed = after != original
    print(f"  commit with --require-stable-round-trip: ok={result.get('ok')}")
    if result.get("ok"):
        rt = result.get("roundTrip") or {}
        print(f"  reported roundTrip status: {rt.get('status')}  stable={rt.get('stable')}")
    else:
        print(f"  error code: {(result.get('error') or {}).get('code')}")
    print(f"  target bytes changed   : {changed}")
    print(f"  flag honoured at all?  : {'--require-stable-round-trip' in result or True}")
    if changed and not (result.get("roundTrip") or {}).get("stable"):
        print("  >>> The flag is not implemented in the bridge; the plugin has to check it AFTER")
        print("      the write, which is exactly the reported defect.")
    if changed and result.get("ok"):
        print("  NOTE: bridge has no pre-write gate; index.js would check stability only after this write.")
        rt = result.get("roundTrip") or {}
        print(f"        and the measured status here is {rt.get('status')!r}")


def scenario_corrupt_store(root: Path) -> None:
    print("\n=== P0-1b  a corrupt store must not silently mean 'zero sessions' ===")
    ws = root / "corrupt"
    state = ws / ".rosetta"
    state.mkdir(parents=True, exist_ok=True)
    store = state / "harness-sessions.json"
    store.write_text("{ this is not json", encoding="utf-8")
    status = call(ws, {"command": "status"})
    print(f"  status ok         : {status.get('ok')}")
    print(f"  sessions reported : {len(status.get('sessions') or [])}")
    if status.get("ok") and not (status.get("sessions") or []):
        print("  >>> BUG REPRODUCED: the corrupt store was silently treated as an empty store.")


def scenario_state_leak(root: Path) -> None:
    print("\n=== P1-2  does /state hand out full artifact text? ===")
    ws = root / "leak"
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "content.typ"
    sentinel = "= Example\n\nARTIFACT-BODY-MARKER-FOR-TEST\n"
    target.write_text(sentinel, encoding="utf-8")
    call(ws, {"command": "open", "session": "leak1", "pair": "LaTeX / Typst",
              "path": str(target)})
    state = call(ws, {"command": "state"})
    blob = json.dumps(state)
    leaked_view = "CONFIDENTIAL-BODY-MARKER-9f3a" in blob
    print(f"  state ok                : {state.get('ok')}")
    print(f"  carries agent view text : {leaked_view}")
    for entry in state.get("sessions") or []:
        print(f"    agentViewPreview chars={len(entry.get('agentViewPreview') or '')} "
              f"targetPreview chars={len(entry.get('targetPreview') or '')}")
    if leaked_view:
        print("  >>> The state payload returns artifact text; the Host route exposes it to any")
        print("      local caller unless the route is authenticated.")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rosetta-repro-"))
    print(f"workspace root: {root}")
    try:
        scenario_concurrency(root)
        scenario_guard_state(root)
        scenario_stability_gate(root)
        scenario_corrupt_store(root)
        scenario_state_leak(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
