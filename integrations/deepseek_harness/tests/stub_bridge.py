"""A scriptable stand-in for the bridge, used by the fence tests.

The fence's failure modes are about *timing and failure*, not about translation,
so the tests need to decide exactly what the `status` command answers and how
long it takes. A real bridge cannot be told "answer the first query slowly with a
stale list, then answer the next one immediately with a fresh list", which is the
ordering case that has to be proved.

Two ways to script the `status` answer:

**Queue** - ``spec["status"]`` is a list, each call consuming the next entry. Good
for fixed sequences such as "activation sees nothing, then one live session".

**Live answer file** - if ``<spec path>.answer`` exists, its contents are read
*fresh on every call* and used instead. That is what makes the ordering test
deterministic: the test can start one query, wait until the stub has read the
answer (``ROSETTA_STUB_MARKER`` gains a line), then change the answer before
starting the second query. Without that handshake the two short-lived processes
could consume the script out of order, and the test would prove nothing.

Spec shape (path in ``ROSETTA_STUB_SPEC``)::

    {
      "status": [
        {"delayMs": 0,   "sessions": []},
        {"delayMs": 900, "sessions": [{"session": "old", "targetPath": "..."}]}
      ],
      "default": {"ok": true},
      "exitCode": 0,
      "garbage": false,
      "okFalse": false
    }
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note_marker() -> None:
    """Record that a call has read the script, so a test can sequence itself."""
    marker = os.environ.get("ROSETTA_STUB_MARKER", "")
    if not marker:
        return
    try:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("called\n")
    except OSError:
        pass


def next_index(counter_path: str) -> int:
    if not counter_path:
        return 0
    path = Path(counter_path)
    try:
        index = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        index = 0
    try:
        path.write_text(str(index + 1), encoding="utf-8")
    except OSError:
        pass
    return index


def status_answer(spec: dict) -> dict:
    """The answer for this call: the live file when present, else the queue."""
    spec_path = os.environ.get("ROSETTA_STUB_SPEC", "")
    if spec_path:
        live = Path(f"{spec_path}.answer")
        if live.exists():
            answer = read_json(live)
            if answer:
                return answer

    queue = spec.get("status") or []
    if isinstance(queue, dict):
        return queue
    index = next_index(os.environ.get("ROSETTA_STUB_COUNTER", ""))
    return queue[index] if index < len(queue) else (queue[-1] if queue else {"sessions": []})


def main() -> int:
    try:
        raw = sys.stdin.read().lstrip("\ufeff")
        command = json.loads(raw or "{}")
    except ValueError:
        command = {}
    name = command.get("command") if isinstance(command, dict) else None

    spec = read_json(Path(os.environ["ROSETTA_STUB_SPEC"])) if os.environ.get("ROSETTA_STUB_SPEC") else {}
    note_marker()

    exit_code = spec.get("exitCode")
    if isinstance(exit_code, int) and exit_code != 0:
        print("stub bridge: refusing to start", file=sys.stderr)
        return exit_code

    if spec.get("garbage"):
        # Not JSON at all: what a wrong interpreter or a crashed process looks like.
        print("this is not json")
        return 0

    if spec.get("okFalse"):
        print(json.dumps({"ok": False, "error": {"code": "STORE_CORRUPT", "message": "stubbed corrupt store"}}))
        return 0

    if name == "status":
        entry = status_answer(spec)
        delay = entry.get("delayMs") or 0
        if delay:
            time.sleep(delay / 1000.0)
        # Per-answer failure modes, so a script can make the FIRST read succeed
        # and a LATER one fail - which is how "a refresh failure must not drop an
        # already-good fence" is expressed.
        if entry.get("okFalse"):
            print(json.dumps({"ok": False, "error": {"code": "STORE_CORRUPT", "message": "stubbed failure on this read"}}))
            return 0
        if entry.get("garbage"):
            print("this is not json")
            return 0
        if entry.get("exitCode"):
            print("stub bridge: refusing this read", file=sys.stderr)
            return int(entry["exitCode"])
        payload = {"ok": True, "sessions": entry.get("sessions") or []}
        if entry.get("omitOk"):
            payload.pop("ok", None)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(json.dumps(spec.get("default") or {"ok": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
