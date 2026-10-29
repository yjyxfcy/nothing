"""Rosetta <-> DeepSeek Harness bridge.

A thin, stdlib-only JSON control plane in front of the *installed*
``rosetta_studio`` package. It owns no translation logic: every projection and
delivery is delegated to ``rosetta_studio.core.available_pairs()`` /
``experimental_pairs()``, which are exactly the adapters shipped in
``compat_layer``.

The bridge exists because the Harness Host plugin is JavaScript. Instead of
re-implementing four translators in JS, the plugin spawns this script as a
short-lived, controlled local process and speaks JSON with it.

Contract
--------
* Invocation::

      python rosetta_harness_bridge.py --allow-root <dir> [--allow-root <dir>]
                                       [--store <path>] [--import-root <dir>]

  A single JSON command object is read from stdin (or from the file named by
  ``--command-file``). One JSON result object is written to stdout as the last
  line. Diagnostics never go to stdout.

* Every result is ``{"ok": true, ...}`` or ``{"ok": false, "error": {...}}``.
  A refusal is a structured error with a stable ``code``, never a silent
  rewrite and never a partial write.

Safety properties enforced here (the Host plugin re-checks what it can):

1. **Workspace containment.** Every artifact path and the session store must
   resolve inside one of the ``--allow-root`` directories. Symlinks are
   resolved before the check, so a link cannot escape a root.
2. **No plugin-owned state inside a target.** Paths whose components include a
   ``.rosetta`` directory are rejected as artifacts.
3. **Concurrent-modification detection.** A session records the SHA-256 of the
   target at open/create. Preview reports drift; commit refuses to write when
   the file changed on disk since the session's last accepted state.
4. **Atomic publication.** Commit writes a temporary file in the target's own
   directory, fsyncs it, re-checks the hash, then ``os.replace``s it.
5. **Explicit failure.** A projection or delivery that fails returns
   ``ok: false`` with the adapter's errors. Nothing is written.
6. **Provenance allow-list.** ``sys.path`` is restricted to the configured
   import roots before ``rosetta_studio`` is imported, so the bridge cannot
   import benchmark, scorer, gold-answer or experiment code even by accident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
STORE_ENV = "ROSETTA_HARNESS_STORE"
MAX_ARTIFACT_BYTES = 2_000_000
# How long an abandoned session record is retained. Retention and fencing are
# separate on purpose: a record may be kept for a day while it stops fencing the
# moment it can no longer act - see FENCE_IDLE_SECONDS.
SESSION_TTL_SECONDS = 24 * 3600
MAX_SESSIONS = 200
STATE_DIR_NAME = ".rosetta"

# ---------------------------------------------------------------------------
# Local-state retention
# ---------------------------------------------------------------------------
#
# Every value below is a *ceiling*, not a target. The policy deliberately keeps
# three things out of reach of automatic pruning:
#
#   * an undelivered draft (agentView differs from baselineView),
#   * any preview the user has not delivered yet,
#   * the newest saved previous version of a session that still exists.
#
# Those are the only copies of user work Rosetta holds, and a silent delete
# would turn a recoverable state into data loss. Volume is bounded instead by
# explicit limits plus an opt-in maintenance command that reports what it would
# remove before it removes anything.
#
# Activity rows are pruned *by source*: the Harness plugin only deletes rows it
# wrote (`source = 'Harness'`), so a shared database that also holds MCP history
# keeps that history untouched.

EVENT_SOURCE = "Harness"

ACTIVITY_MODES = ("full", "metadata", "off")
DEFAULT_ACTIVITY_MODE = "metadata"
DEFAULT_ACTIVITY_RETENTION_DAYS = 30
DEFAULT_ACTIVITY_MAX_EVENTS = 5000
DEFAULT_COMMIT_HISTORY_KEEP = 3
DEFAULT_COMMIT_HISTORY_MAX_FILES = 200
DEFAULT_COMMIT_HISTORY_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_STALE_DRAFT_DAYS = 30

MIN_ACTIVITY_RETENTION_DAYS, MAX_ACTIVITY_RETENTION_DAYS = 1, 3650
MIN_ACTIVITY_MAX_EVENTS, MAX_ACTIVITY_MAX_EVENTS = 50, 1_000_000
MIN_COMMIT_HISTORY_KEEP, MAX_COMMIT_HISTORY_KEEP = 1, 50
MIN_COMMIT_HISTORY_MAX_FILES, MAX_COMMIT_HISTORY_MAX_FILES = 10, 10_000
MIN_COMMIT_HISTORY_MAX_BYTES, MAX_COMMIT_HISTORY_MAX_BYTES = (
    1024 * 1024,
    2 * 1024 * 1024 * 1024,
)
MIN_STALE_DRAFT_DAYS, MAX_STALE_DRAFT_DAYS = 1, 3650


def clamp_int(value, default: int, low: int, high: int) -> int:
    """Coerce a configured limit into range, falling back instead of failing.

    Configuration is hand-normalised by the Host plugin, which already turns an
    unknown value into its documented default. This second pass protects direct
    command-line use and keeps every limit inside a range the retention code
    can reason about (for example ``keep=0`` would delete the only backup).
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def normalise_activity_mode(value) -> str:
    """Accept ``full|metadata|off`` plus the legacy boolean spelling.

    ``true`` keeps the historical behaviour (full snapshots); ``false`` is the
    explicit opt-out. An unknown string falls back to ``full`` rather than
    silently disabling the log the Studio GUI reads.
    """
    if value is False:
        return "off"
    if value is True:
        return "full"
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in ("metadata-only", "metadata_only", "meta"):
            mode = "metadata"
        if mode in ACTIVITY_MODES:
            return mode
    return DEFAULT_ACTIVITY_MODE

# How long a *finished* session keeps its target fenced after the last touch.
#
# The fence exists so a native write cannot race a recent Rosetta edit. Any
# untouched session, including one with a saved draft or preview, releases the
# live fence after this window. The draft remains in the session store and later
# delivery still checks the target hash; this avoids locking a file indefinitely
# while retaining the user's pending work.
FENCE_IDLE_SECONDS = 30 * 60

# Module path fragments that must never be importable from this process. The
# check is a belt-and-braces guard on top of the sys.path restriction, because
# a leaked translation is worse than a failed one.
FORBIDDEN_MODULE_PARTS = (
    "experiments",
    "benchmark",
    "benchmarks",
    "gold",
    "scorer",
    "scoring",
    "grader",
    "grading",
    "judge",
    "rubric",
)


class BridgeError(Exception):
    """A refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------


def _canonical(path: Path) -> Path:
    """Resolve a path's parent chain even when the leaf does not exist yet."""
    try:
        return path.resolve(strict=True)
    except (OSError, FileNotFoundError):
        pass
    parent = path.parent
    if parent == path:
        return path
    return _canonical(parent) / path.name


class Roots:
    """The configured workspace allow-list."""

    def __init__(self, raw_roots: list[str]):
        if not raw_roots:
            raise BridgeError(
                "NO_WORKSPACE_ROOT",
                "No workspace root configured; refusing to touch any path.",
            )
        self.roots: list[Path] = []
        for raw in raw_roots:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                raise BridgeError(
                    "WORKSPACE_ROOT_NOT_ABSOLUTE",
                    f"Workspace root must be absolute: {raw}",
                )
            resolved = _canonical(candidate)
            if not resolved.is_dir():
                raise BridgeError(
                    "WORKSPACE_ROOT_MISSING",
                    f"Workspace root is not an existing directory: {resolved}",
                )
            if resolved not in self.roots:
                self.roots.append(resolved)

    @property
    def primary(self) -> Path:
        return self.roots[0]

    def resolve_artifact(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise BridgeError("PATH_REQUIRED", "An artifact path is required.")
        candidate = Path(raw_path.strip())
        if not candidate.is_absolute():
            candidate = self.primary / candidate
        resolved = _canonical(candidate)
        if STATE_DIR_NAME in resolved.parts:
            raise BridgeError(
                "PATH_IS_PLUGIN_STATE",
                f"Refusing to treat plugin state inside '{STATE_DIR_NAME}' as an artifact.",
                path=str(resolved),
            )
        for root in self.roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved
        raise BridgeError(
            "PATH_OUTSIDE_WORKSPACE",
            "Target path is outside every configured workspace root.",
            path=str(resolved),
            allowed_roots=[str(r) for r in self.roots],
        )

    def resolve_state(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        resolved = _canonical(candidate)
        for root in self.roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved
        raise BridgeError(
            "PATH_OUTSIDE_WORKSPACE",
            "Plugin state path is outside every configured workspace root.",
            path=str(resolved),
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def restrict_import_paths(import_roots: list[str]) -> dict:
    """Install the bare ``sys.path`` the bridge is allowed to import from.

    ``rosetta_studio`` lives in a virtual environment's ``site-packages``; the
    environment root is the intended import root. Anything else - most
    importantly sibling experiment and benchmark trees - is removed before the
    first ``rosetta_studio`` import happens.
    """
    allowed: list[str] = []
    for raw in import_roots:
        resolved = _canonical(Path(raw).expanduser())
        if resolved.is_dir():
            allowed.append(str(resolved))
    # Keep only the standard library locations plus the explicitly allowed
    # roots. ``sys.prefix``/``sys.base_prefix`` cover the running interpreter.
    always = {
        str(_canonical(Path(sys.prefix))),
        str(_canonical(Path(sys.base_prefix))),
    }
    allowed.extend(sorted(always - set(allowed)))

    rejected: list[str] = []
    kept: list[str] = []
    for entry in sys.path:
        if not entry:
            kept.append(entry)
            continue
        resolved = str(_canonical(Path(entry)))
        if any(resolved == a or resolved.startswith(a + os.sep) for a in allowed):
            kept.append(entry)
        else:
            rejected.append(entry)
    sys.path[:] = kept
    return {"kept": kept, "rejected": rejected, "allowed_roots": allowed}


def assert_clean_module(module) -> None:
    """Refuse to use an adapter whose code came from a forbidden tree."""
    origin = str(getattr(module, "__file__", "") or "")
    lowered = origin.replace("/", os.sep).lower()
    for part in FORBIDDEN_MODULE_PARTS:
        if f"{os.sep}{part}{os.sep}" in lowered + os.sep:
            raise BridgeError(
                "FORBIDDEN_MODULE_ORIGIN",
                f"Refusing to use adapter code loaded from a forbidden tree: {origin}",
                module=getattr(module, "__name__", "?"),
                origin=origin,
            )


def module_origin_is_forbidden(origin: str) -> str | None:
    """Return the offending path segment when an origin lies in a forbidden tree."""
    lowered = (origin or "").replace("/", os.sep).lower() + os.sep
    for part in FORBIDDEN_MODULE_PARTS:
        if f"{os.sep}{part}{os.sep}" in lowered:
            return part
    return None


def audit_modules() -> dict:
    """Report every imported module, flagging any loaded from a forbidden tree.

    This is evidence, not a promise: it lists what the process actually
    imported after a command ran, so a leaked benchmark, scorer, or gold-answer
    import would show up by name and path instead of being asserted away.
    """
    modules = []
    violations = []
    for name, module in sorted(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        offender = module_origin_is_forbidden(origin)
        entry = {"module": name, "origin": origin}
        modules.append(entry)
        if offender:
            violations.append({**entry, "forbiddenSegment": offender})
    return {
        "moduleCount": len(modules),
        "violations": violations,
        "clean": not violations,
    }


# ---------------------------------------------------------------------------
# Converter configuration and diagnostics
# ---------------------------------------------------------------------------

CONVERTER_ENV = {
    "t2l": "ROSETTA_TYLAX",
    "pandoc": "ROSETTA_PANDOC",
    "sv2v": "ROSETTA_SV2V",
    "typst": "ROSETTA_TYPST",
    "nu": "ROSETTA_NUSHELL",
}

# Which external executables each pair needs. ``required`` decides usability.
# ``prqlc`` is the Python binding shipped as a dependency, so it is reported
# separately from the PATH-resolved executables.
PAIR_BACKENDS = {
    "LaTeX / Typst": {"required": [], "preferred": ["t2l", "pandoc", "typst"]},
    "Verilog / SystemVerilog": {"required": ["sv2v"], "preferred": ["sv2v"]},
    "PRQL / SQL": {"required": [], "preferred": []},
    "Bash / Nushell (experimental)": {"required": [], "preferred": ["nu"]},
}


def _which(name: str, env: dict) -> str | None:
    override = env.get(CONVERTER_ENV.get(name, ""))
    if override:
        candidate = Path(override).expanduser()
        return str(candidate) if candidate.is_file() else None
    return shutil.which(name)


def _python_backend(name: str, module_name: str) -> dict:
    try:
        module = __import__(module_name)
    except Exception as exc:  # missing dependency is a diagnosis, not a crash
        return {"found": False, "path": None, "python_module": module_name,
                "error": f"{type(exc).__name__}: {exc}"}
    return {
        "found": True,
        "path": getattr(module, "__file__", None),
        "python_module": module_name,
    }


def converter_report(env: dict) -> dict:
    report = {name: {**_which_entry(name, env)} for name in CONVERTER_ENV}
    report["prqlc"] = _python_backend("prqlc", "prqlc")
    return report


def _which_entry(name: str, env: dict) -> dict:
    found = _which(name, env)
    return {
        "found": found is not None,
        "path": found,
        "env_var": CONVERTER_ENV[name],
        "env_override": env.get(CONVERTER_ENV[name]) or None,
    }


def diagnose_backends(pairs: dict, env: dict) -> dict:
    converters = converter_report(env)
    blocked: list[dict] = []
    for name, pair in pairs.items():
        spec = PAIR_BACKENDS.get(name, {"required": [], "preferred": []})
        missing = [b for b in spec["required"] if not converters.get(b, {}).get("found")]
        missing_preferred = [
            b for b in spec["preferred"] if not converters.get(b, {}).get("found")
        ]
        if missing:
            blocked.append(
                {
                    "pair": name,
                    "missing_required": missing,
                    "hint": _missing_hint(name, missing),
                }
            )
        elif missing_preferred:
            blocked.append(
                {
                    "pair": name,
                    "missing_optional": missing_preferred,
                    "degraded": True,
                    "hint": _missing_hint(name, missing_preferred),
                }
            )
    return {"converters": converters, "pair_diagnostics": blocked}


def _missing_hint(pair: str, missing: list[str]) -> str:
    names = ", ".join(missing)
    vars_ = ", ".join(f"{CONVERTER_ENV.get(m, 'ROSETTA_?')}" for m in missing)
    if pair == "Verilog / SystemVerilog":
        return (
            f"{names} was not found, so this pair cannot project. Install it on PATH "
            f"or set {vars_} to its absolute executable path, then restart the Harness "
            f"or re-run ros_doctor."
        )
    return (
        f"{names} was not found. Rosetta falls back to its built-in engine, which "
        f"covers less syntax; install {names} on PATH or set {vars_} for full coverage."
    )


# ---------------------------------------------------------------------------
# Compiled-artifact verification (diagnostic only, never a translator)
# ---------------------------------------------------------------------------

PAIR_VERIFIERS = {
    "LaTeX / Typst": "typst",
    "Bash / Nushell (experimental)": "nu",
    "PRQL / SQL": "prqlc",
    "Verilog / SystemVerilog": "sv2v",
}

# Verifiers that are Python modules rather than PATH executables.
MODULE_VERIFIERS = {"prqlc"}


def verify_artifact(pair_name: str, path: Path, text: str, env: dict) -> dict:
    """Independently check a delivered artifact with its own toolchain.

    This never translates anything; it only asks the target toolchain whether
    the file Rosetta just wrote is acceptable to it.

    Verification is a courtesy, never a precondition. Any failure to run it -
    a missing binary, a full or unusable system temp directory, a toolchain
    crash - is reported as ``checked: False`` with the reason, and must never
    turn a successful delivery into a failed one.
    """
    try:
        return _verify_artifact_inner(pair_name, path, env)
    except Exception as exc:  # noqa: BLE001 - a verifier must not break delivery
        return {
            "checked": False,
            "reason": f"independent verification could not run: {type(exc).__name__}: {exc}",
        }


def _verify_artifact_inner(pair_name: str, path: Path, env: dict) -> dict:
    tool = PAIR_VERIFIERS.get(pair_name)
    if tool is None:
        return {"checked": False, "reason": f"no independent verifier for {pair_name}"}
    if tool in MODULE_VERIFIERS:
        return _verify_prql(path)
    binary = _which(tool, env)
    if not binary:
        return {
            "checked": False,
            "reason": f"{tool} not found on PATH; set {CONVERTER_ENV.get(tool, tool)} "
            f"to verify deliveries independently",
        }
    if tool == "sv2v":
        # sv2v is the *same* converter the projection used, so this is a parse
        # check with the tool that already accepted the file once - not a second
        # opinion. Reported as such, and no stronger claim is made: a second
        # implementation (Verilator, Icarus, Surelog) was not available on this
        # machine, and `installed` records that so the report cannot be read as
        # independent verification.
        result = _run_check([binary, str(path)], path.parent, tool)
        result["independent"] = False
        result["note"] = (
            "sv2v is the same converter used for the projection; this only shows "
            "the delivered SystemVerilog parses with it, not that a second "
            "implementation agrees."
        )
        return result
    if tool == "typst":
        # The scratch directory goes beside the artifact, not in the system temp
        # directory: that directory is the one place we already know this process
        # can write (the artifact lives there), and on this platform it is the
        # same filesystem, so the compiler's output stays local. The system temp
        # directory can be missing, unwritable, or fenced off by a sandbox, and
        # delivery must not depend on it.
        out_dir = _scratch_dir_beside(path)
        try:
            return _run_check(
                [
                    binary,
                    "compile",
                    "--root",
                    str(path.parent),
                    str(path),
                    str(out_dir / "check.pdf"),
                ],
                path.parent,
                tool,
            )
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
    if tool == "nu":
        return _run_check([binary, "--no-config-file", str(path)], path.parent, tool)
    return {"checked": False, "reason": f"unsupported verifier {tool}"}


def _verify_prql(path: Path) -> dict:
    """Compile the delivered PRQL with the real prqlc compiler.

    Unlike the other checks this one is genuinely a second implementation: the
    projection goes through sqlglot, and prqlc is the reference PRQL compiler, so
    agreement between them is meaningful evidence rather than a round trip
    through the same code.
    """
    try:
        import prqlc
    except Exception as exc:  # noqa: BLE001
        return {
            "checked": False,
            "reason": f"prqlc is not importable ({type(exc).__name__}: {exc})",
        }
    try:
        sql = prqlc.compile(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "checked": True,
            "tool": "prqlc",
            "ok": False,
            "independent": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "checked": True,
        "tool": "prqlc",
        "ok": True,
        # prqlc is the reference PRQL compiler while the projection goes through
        # sqlglot, so agreement between them is evidence from a second
        # implementation rather than a round trip through the same code.
        "independent": True,
        "sql": (sql or "")[:400],
    }


def _scratch_dir_beside(path: Path) -> Path:
    """Create a unique, self-cleaning scratch directory beside ``path``."""
    for attempt in range(64):
        candidate = path.parent / f".{path.name}.rosetta-verify-{os.getpid()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"could not create a scratch directory beside {path}")


def _run_check(argv: list[str], cwd: Path, tool: str) -> dict:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"checked": True, "tool": tool, "ok": False, "error": f"{tool} timed out"}
    except OSError as exc:
        return {"checked": True, "tool": tool, "ok": False, "error": str(exc)}
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    return {
        "checked": True,
        "tool": tool,
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": out[-2000:],
        "stderr": err[-2000:],
    }


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


STORE_SCHEMA_VERSION = 4
LEGACY_STORE_NAME = "harness-sessions.json"


class Store:
    """Session records in SQLite, so concurrent bridge processes cannot lose work.

    The previous JSON implementation loaded the whole file into a dict and
    replaced the file on save. The plugin spawns one bridge process per tool
    call, so two overlapping calls both read the same snapshot and the later
    writer silently deleted the earlier session. Measured before the change: 16
    concurrent ``open`` calls all reported success and **1** session survived.

    SQLite provides a real cross-process transaction. Every mutation runs inside
    ``BEGIN IMMEDIATE`` with a busy timeout, so a read-modify-write is atomic
    against peers instead of racing with them.

    A store that cannot be read is an error, never an empty store: silently
    reporting "no sessions" would make the host's write fence disappear and let
    a delivery overwrite a file the agent still believes it is editing.
    """

    def __init__(self, path: Path):
        if path.suffix.lower() == ".json":
            raise BridgeError(
                "STORE_PATH_IS_JSON",
                "The session store is now a SQLite database; point --store at a "
                "database path (for example <dir>/harness-sessions.sqlite3).",
                path=str(path),
            )
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(path), timeout=30.0, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise BridgeError(
                "STORE_UNREADABLE",
                f"Could not open the session store: {exc}",
                path=str(path),
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._assert_integrity()
            self._create_schema()
            self.migrated_from = self._migrate_legacy_json()
        except BridgeError:
            self.close()
            raise
        except sqlite3.DatabaseError as exc:
            self.close()
            raise BridgeError(
                "STORE_CORRUPT",
                "The session store is not a usable database and will not be "
                f"treated as empty: {exc}",
                path=str(path),
            ) from exc
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the session database connection; safe to call more than once."""
        conn = getattr(self, "_conn", None)
        if conn is None:
            return
        self._conn = None
        conn.close()

    # -- schema and integrity ------------------------------------------------

    def _assert_integrity(self) -> None:
        try:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise BridgeError(
                "STORE_CORRUPT",
                f"The session store failed its integrity check: {exc}",
                path=str(self.path),
            ) from exc
        verdict = (row[0] if row else "unknown")
        if str(verdict).lower() != "ok":
            raise BridgeError(
                "STORE_CORRUPT",
                f"The session store failed its integrity check: {verdict}",
                path=str(self.path),
            )

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session             TEXT PRIMARY KEY,
                pair                TEXT NOT NULL,
                target_path         TEXT NOT NULL,
                target_sha256       TEXT,
                target_existed      INTEGER NOT NULL,
                agent_view          TEXT NOT NULL,
                baseline_view       TEXT NOT NULL,
                delivered_target    TEXT,
                preview             TEXT,
                preview_view_sha256 TEXT,
                preview_metadata    TEXT,
                projection_metadata TEXT NOT NULL,
                owner_session_id    TEXT,
                created_at          REAL NOT NULL,
                updated_at          REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated_at)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # Schema 3 adds `preview_metadata`. A delivery is previewed once and then
        # committed from that stored preview, so without the engine's own metadata
        # travelling with it, the commit cannot see the `compiles` verdict: no
        # fresh translation runs on that path. Added by ALTER so an existing store
        # is upgraded in place rather than needing to be discarded.
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "preview_metadata" not in existing:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN preview_metadata TEXT")
        # Schema 4 records which Harness chat opened the session. It is nullable:
        # a session opened by a build that could not name its chat stays
        # explicitly unattributed rather than being guessed into the current one.
        if "owner_session_id" not in existing:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN owner_session_id TEXT")
        self._conn.execute(
            "INSERT OR REPLACE INTO store_meta(key, value) VALUES('schemaVersion', ?)",
            (str(STORE_SCHEMA_VERSION),),
        )

    def _migrate_legacy_json(self) -> str | None:
        """Import a legacy JSON store once, or refuse loudly.

        An unreadable legacy file is reported rather than skipped: it is the only
        copy of sessions the agent may still be editing, and quietly starting
        empty would drop the write fence those sessions justify.
        """
        legacy = self.path.parent / LEGACY_STORE_NAME
        if not legacy.is_file():
            return None
        if self._count() > 0:
            return None
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            sessions = raw["sessions"]
            if not isinstance(sessions, dict):
                raise ValueError("'sessions' is not an object")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "STORE_CORRUPT",
                f"The legacy session store {legacy} could not be read, so its "
                f"sessions cannot be migrated; refusing to start with an empty "
                f"store: {exc}",
                path=str(legacy),
            ) from exc
        imported = 0
        with self.transaction() as tx:
            for session_id, record in sessions.items():
                if not isinstance(record, dict) or "targetPath" not in record:
                    continue
                tx.upsert(session_id, record)
                imported += 1
        backup = legacy.with_suffix(legacy.suffix + ".migrated")
        try:
            legacy.replace(backup)
        except OSError:
            backup = None
        return f"imported {imported} session(s) from {legacy.name}" + (
            f"; kept a copy at {backup.name}" if backup else ""
        )

    # -- transactions --------------------------------------------------------

    @contextmanager
    def transaction(self):
        """One cross-process atomic read-modify-write unit."""
        self._conn.execute("BEGIN IMMEDIATE")
        tx = _Transaction(self._conn)
        try:
            yield tx
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        self._prune()

    # -- reads ---------------------------------------------------------------

    def _count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def all_sessions(self) -> list[tuple[str, dict]]:
        """Every session, newest first.

        Deliberately **not** filtered by whether the target file exists: a session
        created for a not-yet-written artifact, or one whose target was deleted
        underneath it, is still live and must still fence the path.
        """
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [(row["session"], _record_from_row(row)) for row in rows]

    def get(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session = ?", (session_id,)
        ).fetchone()
        return _record_from_row(row) if row else None

    def resolve_id(self, prefix: str) -> str:
        if not isinstance(prefix, str) or not prefix:
            raise BridgeError("SESSION_ID_REQUIRED", "A session id is required.")
        if self.get(prefix) is not None:
            return prefix
        matches = [
            row["session"]
            for row in self._conn.execute(
                "SELECT session FROM sessions WHERE session LIKE ? || '%' ORDER BY session",
                (prefix,),
            )
        ]
        if not matches:
            raise BridgeError(
                "UNKNOWN_SESSION",
                "Unknown or expired Rosetta session; open the artifact again.",
                session=prefix,
            )
        if len(matches) > 1:
            raise BridgeError(
                "AMBIGUOUS_SESSION",
                "That session id prefix matches more than one session.",
                session=prefix,
                matches=matches,
            )
        return matches[0]

    def _prune(self) -> None:
        # Never silently delete an undelivered draft or a pending candidate.
        # Expiry is for finished/clean records; a dirty view or a stored preview
        # is user work, and the GUI must keep offering a way back to it.
        self.prune_expired_sessions()

    # -- retention -----------------------------------------------------------

    _CLEAN = "agent_view = baseline_view AND preview IS NULL"

    def prune_expired_sessions(self) -> int:
        """Remove finished records that no longer need a session.

        A *dirty* record - an undelivered view edit or a stored preview - is
        never matched here, no matter how old or how far over ``MAX_SESSIONS``
        the store is. The overflow pass therefore removes clean records only and
        can legitimately leave the count above the limit; that is reported by
        ``storage_counts`` instead of being "fixed" by deleting user work.
        """
        cutoff = time.time() - SESSION_TTL_SECONDS
        deleted = self._conn.execute(
            f"DELETE FROM sessions WHERE updated_at < ? AND {self._CLEAN}",
            (cutoff,),
        ).rowcount
        overflow = self._count() - MAX_SESSIONS
        if overflow > 0:
            deleted += self._conn.execute(
                "DELETE FROM sessions WHERE session IN ("
                "  SELECT session FROM sessions "
                f" WHERE {self._CLEAN} ORDER BY updated_at ASC LIMIT ?"
                ")",
                (overflow,),
            ).rowcount
        return max(0, deleted)

    def storage_counts(self, stale_days: int = DEFAULT_STALE_DRAFT_DAYS) -> dict:
        """Counts for diagnostics; never mutates the store."""
        now = time.time()
        stale_cutoff = now - stale_days * 86400
        expired_cutoff = now - SESSION_TTL_SECONDS
        row = self._conn.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(agent_view != baseline_view), 0) AS dirty,"
            " COALESCE(SUM(agent_view != baseline_view AND updated_at < ?), 0) AS stale_dirty,"
            " COALESCE(SUM(agent_view = baseline_view AND preview IS NULL"
            "            AND updated_at < ?), 0) AS expired_clean"
            " FROM sessions",
            (stale_cutoff, expired_cutoff),
        ).fetchone()
        oldest_dirty = self._conn.execute(
            "SELECT MIN(updated_at) FROM sessions WHERE agent_view != baseline_view"
        ).fetchone()[0]
        total = int(row["total"] or 0)
        expired_clean = int(row["expired_clean"] or 0)
        clean = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE {self._CLEAN}"
            ).fetchone()[0]
        )
        # What one pruning pass would remove: the expired clean records first,
        # then the oldest remaining clean records above MAX_SESSIONS. Dirty
        # records are excluded from both steps and can keep the store above the
        # limit, which is why the count is reported rather than forced.
        remaining_total = max(0, total - expired_clean)
        overflow_after = max(0, remaining_total - MAX_SESSIONS)
        removable_clean = expired_clean + min(
            max(0, clean - expired_clean), overflow_after
        )
        return {
            "total": total,
            "dirty": int(row["dirty"] or 0),
            "clean": clean,
            "staleDirty": int(row["stale_dirty"] or 0),
            "expiredClean": expired_clean,
            "overSessionLimit": max(0, total - MAX_SESSIONS),
            "removableClean": removable_clean,
            "oldestDirtyAgeSeconds": (
                max(0.0, now - float(oldest_dirty)) if oldest_dirty is not None else None
            ),
        }

    def stale_draft_sessions(self, stale_days: int) -> list[dict]:
        """Undelivered drafts older than ``stale_days``; metadata only.

        Used by the explicit maintenance report/clean path. The caller decides
        whether to delete; ordinary use never calls this.
        """
        cutoff = time.time() - stale_days * 86400
        rows = self._conn.execute(
            "SELECT session, pair, target_path, updated_at FROM sessions"
            " WHERE agent_view != baseline_view AND updated_at < ?"
            " ORDER BY updated_at ASC",
            (cutoff,),
        ).fetchall()
        return [
            {
                "session": row["session"],
                "pair": row["pair"],
                "targetPath": row["target_path"],
                "updatedAt": row["updated_at"],
                "idleDays": round((time.time() - row["updated_at"]) / 86400, 1),
            }
            for row in rows
        ]

    def maintenance_clean(self, stale_days: int, *, include_drafts: bool) -> dict:
        """Apply the explicit cleanup a user asked for, and report the effect."""
        removed_clean = self.prune_expired_sessions()
        removed_drafts = 0
        if include_drafts:
            cutoff = time.time() - stale_days * 86400
            removed_drafts = self._conn.execute(
                "DELETE FROM sessions WHERE agent_view != baseline_view AND updated_at < ?",
                (cutoff,),
            ).rowcount
        return {
            "removedClean": max(0, removed_clean),
            "removedDrafts": max(0, removed_drafts),
        }


class _Transaction:
    """Mutations inside one already-open `BEGIN IMMEDIATE` transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session = ?", (session_id,)
        ).fetchone()
        return _record_from_row(row) if row else None

    def resolve_id(self, prefix: str) -> str:
        if not isinstance(prefix, str) or not prefix:
            raise BridgeError("SESSION_ID_REQUIRED", "A session id is required.")
        row = self._conn.execute(
            "SELECT session FROM sessions WHERE session = ?", (prefix,)
        ).fetchone()
        if row:
            return prefix
        matches = [
            entry["session"]
            for entry in self._conn.execute(
                "SELECT session FROM sessions WHERE session LIKE ? || '%' ORDER BY session",
                (prefix,),
            )
        ]
        if not matches:
            raise BridgeError(
                "UNKNOWN_SESSION",
                "Unknown or expired Rosetta session; open the artifact again.",
                session=prefix,
            )
        if len(matches) > 1:
            raise BridgeError(
                "AMBIGUOUS_SESSION",
                "That session id prefix matches more than one session.",
                session=prefix,
                matches=matches,
            )
        return matches[0]

    def upsert(self, session_id: str, record: dict) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                session, pair, target_path, target_sha256, target_existed,
                agent_view, baseline_view, delivered_target, preview,
                preview_view_sha256, projection_metadata, owner_session_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                record["pair"],
                record["targetPath"],
                record.get("targetSha256"),
                1 if record.get("targetExisted") else 0,
                record.get("agentView") or "",
                record.get("baselineView") or "",
                record.get("deliveredTarget"),
                record.get("preview"),
                record.get("previewViewSha256"),
                json.dumps(record.get("projectionMetadata") or {}, ensure_ascii=False),
                record.get("ownerSessionId"),
                float(record.get("createdAt") or time.time()),
                float(record.get("updatedAt") or time.time()),
            ),
        )

    def delete(self, session_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM sessions WHERE session = ?", (session_id,)
        )
        return cursor.rowcount > 0

    def update(self, session_id: str, **fields) -> None:
        """Patch named record fields (camelCase) on one session."""
        columns = {
            "pair": "pair",
            "targetPath": "target_path",
            "targetSha256": "target_sha256",
            "targetExisted": "target_existed",
            "agentView": "agent_view",
            "baselineView": "baseline_view",
            "deliveredTarget": "delivered_target",
            "preview": "preview",
            "previewViewSha256": "preview_view_sha256",
            "ownerSessionId": "owner_session_id",
            "updatedAt": "updated_at",
        }
        assignments = []
        values = []
        for key, value in fields.items():
            if key in ("projectionMetadata", "previewMetadata"):
                column = (
                    "projection_metadata"
                    if key == "projectionMetadata"
                    else "preview_metadata"
                )
                assignments.append(f"{column} = ?")
                values.append(json.dumps(value or {}, ensure_ascii=False))
                continue
            column = columns.get(key)
            if column is None:
                raise BridgeError("STORE_BAD_FIELD", f"Unknown session field {key!r}.")
            if key == "targetExisted":
                value = 1 if value else 0
            assignments.append(f"{column} = ?")
            values.append(value)
        if not assignments:
            return
        values.append(session_id)
        self._conn.execute(
            f"UPDATE sessions SET {', '.join(assignments)} WHERE session = ?", values
        )


def _record_from_row(row: sqlite3.Row) -> dict:
    return {
        "_key": row["session"],
        "pair": row["pair"],
        "targetPath": row["target_path"],
        "targetSha256": row["target_sha256"],
        "targetExisted": bool(row["target_existed"]),
        "agentView": row["agent_view"],
        "baselineView": row["baseline_view"],
        "deliveredTarget": row["delivered_target"],
        "preview": row["preview"],
        "previewViewSha256": row["preview_view_sha256"],
        "previewMetadata": json.loads(row["preview_metadata"] or "{}"),
        "projectionMetadata": json.loads(row["projection_metadata"] or "{}"),
        # Null means "opened without a chat identity". It must not be treated as
        # belonging to whichever chat happens to poll next.
        "ownerSessionId": row["owner_session_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def session_active(record: dict, now: float | None = None) -> bool:
    """Whether this session should still fence its target.

    Idle sessions release the native write fence even when a draft remains.
    The draft stays in the session store until retention expires. A later native
    write makes a subsequent Rosetta commit fail its target-drift check.
    """
    touched = record.get("updatedAt") or 0
    return ((now if now is not None else time.time()) - touched) <= FENCE_IDLE_SECONDS



# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def emit_json(payload: dict) -> None:
    """Write protocol JSON using ASCII-only bytes, independent of console codepage.

    On Windows, redirected stdout may use the active legacy codepage (for
    example GBK), while the JavaScript host correctly decodes child output as
    UTF-8. Escaping non-ASCII JSON characters keeps the wire representation
    portable; JSON.parse restores the original Unicode strings on the host.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise BridgeError(
            "ARTIFACT_TOO_LARGE",
            f"Target exceeds the {MAX_ARTIFACT_BYTES}-byte limit.",
            bytes=len(raw),
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BridgeError("NOT_UTF8", "Only UTF-8 target files are supported.") from exc


def project_with_preservation(pair_obj, content: str) -> tuple[object, list[str]]:
    """Project a target to its view and report what had to be preserved.

    ``rosetta_studio.core.Pair.project`` already runs the adapter's
    ``mark_unrepresentable`` hook and records the result in
    ``metadata['preserved_regions']``. Marking must therefore **not** be repeated
    here: an adapter that is not perfectly idempotent would wrap the same region
    twice, and a second pass over already-marked content reports nothing
    preserved, which would hide the real list from the caller. The bridge reads
    the product's metadata instead of duplicating the responsibility.
    """
    result = pair_obj.project(content)
    preserved = list((result.metadata or {}).get("preserved_regions") or [])
    return result, preserved


# View-side pass-through marker pairs. Each adapter owns its own comment syntax;
# this table recognises them for validation and comparison, never to translate.
# It mirrors `rosetta_studio.mcp_server.VIEW_MARKERS` on purpose: the MCP server
# and this plugin must agree on what a pass-through block is, and a test asserts
# the two parsers give identical results on a shared corpus.
VIEW_MARKERS = {
    "% @TYPST-PASSTHROUGH": "% @END-TYPST-PASSTHROUGH",
    "// @TYPST-PASSTHROUGH": "// @END-TYPST-PASSTHROUGH",
    "// @SV-PASSTHROUGH": "// @END-SV-PASSTHROUGH",
    "# @NU-PASSTHROUGH": "# @END-NU-PASSTHROUGH",
}


class MalformedView(BridgeError):
    def __init__(self, message: str):
        super().__init__("MALFORMED_VIEW", message)


def preserved_regions(view: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Read complete pass-through blocks without interpreting their contents.

    Returns ``(marker, body_lines)`` per block, in document order and with
    multiplicity, which is what the comparison and the write guard need. Body
    lines keep their line endings so the tuples compare byte-for-byte and so the
    result is identical to ``rosetta_studio.mcp_server.preserved_regions``. A
    nested block, an unterminated block, or a stray end marker is refused rather
    than guessed at: those shapes mean the agent edited inside a preserved
    region, and guessing would deliver the wrong bytes.
    """
    lines = view.splitlines(keepends=True)
    regions: list[tuple[str, tuple[str, ...]]] = []
    index = 0
    while index < len(lines):
        marker = lines[index].strip()
        if marker in VIEW_MARKERS:
            end = VIEW_MARKERS[marker]
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != end:
                if lines[index].strip() in VIEW_MARKERS:
                    raise MalformedView("Nested pass-through block in the agent view.")
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise MalformedView("Unterminated pass-through block in the agent view.")
            regions.append((marker, tuple(body)))
        elif marker in VIEW_MARKERS.values():
            raise MalformedView("Unexpected pass-through end marker in the agent view.")
        index += 1
    return tuple(regions)


def _comparison_form(text: str) -> str:
    """The text as the stability comparison sees it: file-level normalisation only.

    Exactly two things are normalized, and neither is document content in any of
    the four formats:

    * the **line-ending style** - ``\\r\\n`` and ``\\r`` mean the same line break
      as ``\\n``, so an agent editing on Windows otherwise produces a spurious
      difference on every line;
    * the **single optional terminator at end of file** - all four formats accept
      a final newline or its absence, and it carries no meaning.

    Everything else is still compared byte for byte. Indentation, alignment, blank
    lines, and spaces inside a string literal or a code token remain significant,
    because folding them cannot distinguish formatting from a real edit - which is
    the mistake this contract exists to avoid. Measured motivation: without this
    step, one trailing newline made **all four** pairs report ``unstable`` for a
    view that was in fact correct.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalised.endswith("\n"):
        normalised = normalised[:-1]
    return normalised


_LATEX_HEADING_LINE = re.compile(
    r"(?m)^(?P<heading>[ \t]*\\(?:part|chapter|section|subsection|"
    r"subsubsection|paragraph|subparagraph)\*?(?:\[[^\]\n]*\])?"
    r"\{[^\n]*\}\n)\n"
)


def _latex_heading_spacing_form(text: str) -> str:
    """Remove one blank line immediately following a standalone LaTeX heading.

    This is used only to *diagnose* a known formatter difference. It does not
    participate in the stability verdict: a heading-spacing-only mismatch is
    still reported as ``unstable`` and is never accepted by the strict gate.
    """
    return _LATEX_HEADING_LINE.sub(r"\g<heading>", text)


def retains_regions(view: str, required: tuple[tuple[str, tuple[str, ...]], ...]) -> bool:
    """True when every required region still occurs, in order, with multiplicity.

    Order matters and duplicates matter: swapping two preserved blocks or
    deleting one of two identical blocks changes the artifact even though the
    *set* of regions is unchanged.
    """
    if not required:
        return True
    current = list(preserved_regions(view))
    position = 0
    for expected in required:
        while position < len(current) and current[position] != expected:
            position += 1
        if position >= len(current):
            return False
        position += 1
    return True


def _translated_text(view: str) -> str:
    """The view with its pass-through blocks removed, everything else verbatim.

    Used only to *locate* a difference for the report. The stability verdict
    never rests on this text being "equivalent" under normalization; it rests on
    the whole view being byte-identical.
    """
    lines = view.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        end = VIEW_MARKERS.get(lines[index].strip())
        if end is None:
            out.append(lines[index])
            index += 1
            continue
        index += 1
        while index < len(lines) and lines[index].strip() != end:
            index += 1
        index += 1  # consume the end marker
    return "\n".join(out)


def _remove_generated_typst_wrappers(target: str, record: dict) -> str:
    """Remove only the Typst comments added to protect raw directives in a view.

    The compatibility adapter uses comment wrappers to carry Typst-only code
    through LaTeX. They are transport syntax, not part of the authored target.
    Strip them only when every wrapper in the delivered target exactly matches
    the automatically protected regions recorded when the session was opened.
    Hand-authored, mixed, or changed wrappers are left untouched.
    """
    expected = (record.get("projectionMetadata") or {}).get("preservedRegions") or []
    if not expected:
        return target

    try:
        regions = preserved_regions(_comparison_form(target))
    except MalformedView:
        return target

    start = "// @TYPST-PASSTHROUGH"
    end = "// @END-TYPST-PASSTHROUGH"
    if len(regions) != len(expected) or any(marker != start for marker, _ in regions):
        return target
    if any(
        _comparison_form("".join(body)) != _comparison_form(str(source))
        for (_, body), source in zip(regions, expected)
    ):
        return target

    lines = target.splitlines(keepends=True)
    output: list[str] = []
    expected_index = 0
    index = 0
    while index < len(lines):
        if lines[index].strip() != start:
            output.append(lines[index])
            index += 1
            continue
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != end:
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            return target
        # The complete ordered set was checked above; keep a defensive check in
        # the rewrite loop so malformed future formats fail closed.
        if _comparison_form("".join(body)) != _comparison_form(str(expected[expected_index])):
            return target
        output.extend(body)
        expected_index += 1
        index += 1
    return "".join(output) if expected_index == len(expected) else target


def owner_session_id(value: str | None) -> str | None:
    """The Harness chat a session belongs to, or ``None`` when unknown.

    The Host passes ``exec.agent.session.id`` for tool calls and the slot's
    standard ``sessionId`` prop for browser actions. A value that is absent or
    blank stays absent: filling it in from the polling context would invent an
    ownership relation the plugin does not actually have.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:200]


def has_convertible_content(view: str) -> bool:
    """Whether the view contains anything outside its pass-through blocks.

    A document the reverse adapter had to carry whole (for example a Nushell
    pipeline outside the recognized subset) projects to a view made *entirely*
    of ``@NU-PASSTHROUGH`` markers. The compatibility layer can then neither
    edit nor verify that work: a delivery is only the original text carried
    back. The UI must not call that "preview ready" or an edit, so the status
    payload reports this distinction explicitly instead of inferring success
    from the mere existence of a stored preview.

    The check uses the same marker parser as delivery, so malformed markers are
    still refused by the existing validation rather than silently classified.
    """
    try:
        return bool(_translated_text(view or "").strip())
    except MalformedView:
        return False


def _public_metadata(metadata: dict | None) -> dict:
    """Session metadata safe to publish over an HTTP route.

    ``protectedRegions`` holds verbatim copies of the target-only blocks and
    ``preservedRegions`` holds the source lines that were marked, so both carry
    artifact text. Only their size and the marker names are published; the
    bodies stay in the session record, where the agent-facing tool results can
    still reach them.
    """
    public: dict = {}
    for key, value in (metadata or {}).items():
        if key == "protectedRegions":
            public["protectedRegionMarkers"] = [entry[0] for entry in value]
            continue
        if key == "preservedRegions":
            public["preservedRegionCount"] = len(value)
            continue
        public[key] = value
    return public


def _compile_status(metadata: dict) -> bool | None:
    """The adapters' own ``compiles`` verdict, or ``None`` when they did not say.

    ``TranslationResult`` carries it at the top level of its metadata, but the same
    value also arrives nested when a wrapper has round-tripped the result, so both
    shapes are read rather than assuming one. ``None`` means "not reported" and must
    never be treated as success: only an explicit ``False`` is a known failure, and
    only an explicit ``True`` is a claim that the output compiled.
    """
    candidates = [metadata]
    nested = metadata.get("metadata") if isinstance(metadata, dict) else None
    if isinstance(nested, dict):
        candidates.append(nested)
    for source in candidates:
        if isinstance(source, dict):
            status = source.get("compiles")
            if status is not None:
                return bool(status)
    return None


def _stability_note(stability: dict, committed: bool = False) -> list[str]:
    status = stability.get("status")
    where = "the delivered artifact" if committed else "this delivery"
    if status == "exact":
        return [f"Round-trip check: {where} re-projects to the approved view byte for byte."]
    if status == "unknown":
        return [
            f"Round-trip check: not proved - {stability.get('reason', 'the comparison could not run')}"
        ]
    return [
        "Round-trip check: unstable - "
        + stability.get("reason", "the re-projection differs from the approved view")
    ]




COMMIT_JOURNAL_DIR_NAME = "commit-journal"


class CommitJournal:
    """A write-ahead record of a delivery whose session update has not landed yet.

    The artifact write and the session store update are **not** one atomic
    transaction, and pretending otherwise is what produced a measured failure:
    after ``os.replace`` published the new artifact, a fault injected into the
    SQLite update left the database holding the *old* hash. The file was new, the
    record was stale, and nothing said so.

    What is guaranteed instead:

    * every publish is **preceded** by a durable journal entry describing exactly
      what was published and what the session should become, so the intent
      survives a crash between the two steps;
    * if the session update then fails, the entry is **kept** - recovery is
      possible - and the caller is told the artifact *was* written rather than
      being given a false failure;
    * if the publish never happened, the entry is removed, because there is
      nothing to recover;
    * the next bridge call replays any entry whose recorded hash matches the
      target on disk, which is how a stale database is repaired.

    The journal is a plain directory of small JSON files beside the store, so it
    stays usable precisely when the store is what is broken.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.last_error: str | None = None

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict) -> Path:
        """Durably record an intent to publish. Raises if it cannot be recorded."""
        self._ensure()
        entry = dict(entry)
        entry.setdefault("version", 1)
        entry.setdefault("startedAt", time.time())
        token = uuid.uuid4().hex[:12]
        safe_session = re.sub(r"[^A-Za-z0-9._-]", "_", str(entry.get("session") or "session"))
        path = self.dir / f"{safe_session}-{token}.json"
        blob = json.dumps(entry, ensure_ascii=False, indent=2).encode("utf-8")
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=str(self.dir), prefix=".journal-", suffix=".tmp", delete=False
        )
        temp_name = handle.name
        try:
            with handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise
        return path

    def resolve(self, path: Path | None) -> None:
        """Drop an entry: either it was applied, or nothing was published."""
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            self.last_error = f"could not remove journal entry {path}: {exc}"

    def pending(self) -> list[tuple[Path, dict]]:
        """Every unresolved entry, oldest first. Unreadable entries are kept."""
        if not self.dir.is_dir():
            return []
        found: list[tuple[Path, dict]] = []
        for candidate in sorted(self.dir.glob("*.json")):
            try:
                found.append((candidate, json.loads(candidate.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as exc:
                self.last_error = f"unreadable journal entry {candidate.name}: {exc}"
        found.sort(key=lambda item: item[1].get("startedAt") or 0)
        return found

    def count(self) -> int:
        try:
            return len(self.pending())
        except OSError:
            return 0


class CommitHistory:
    """Recoverable previous target bytes for an explicit undo.

    The journal records *what* was published so a crash can be repaired; this
    directory keeps the *old bytes* so a user can undo a successful delivery.
    It is intentionally separate from the journal: a resolved journal entry must
    not remove the only copy of the previous artifact.

    Retention is per session plus a global ceiling. The **newest** entry of every
    session is never removed automatically, because ``cmd_restore_last`` only
    reaches that one: deleting it would silently remove the user's only undo
    point. Older duplicate versions of the same session are superseded by the
    newer backup, so they are safe to trim. A global byte/file ceiling then trims
    the oldest superseded entries first; if every remaining entry is the newest
    of its session, the ceiling is reported as exceeded instead of forced.
    """

    def __init__(
        self,
        directory: Path,
        *,
        keep: int = DEFAULT_COMMIT_HISTORY_KEEP,
        max_files: int = DEFAULT_COMMIT_HISTORY_MAX_FILES,
        max_bytes: int = DEFAULT_COMMIT_HISTORY_MAX_BYTES,
    ):
        self.dir = Path(directory)
        self.keep = clamp_int(
            keep, DEFAULT_COMMIT_HISTORY_KEEP,
            MIN_COMMIT_HISTORY_KEEP, MAX_COMMIT_HISTORY_KEEP,
        )
        self.max_files = clamp_int(
            max_files, DEFAULT_COMMIT_HISTORY_MAX_FILES,
            MIN_COMMIT_HISTORY_MAX_FILES, MAX_COMMIT_HISTORY_MAX_FILES,
        )
        self.max_bytes = clamp_int(
            max_bytes, DEFAULT_COMMIT_HISTORY_MAX_BYTES,
            MIN_COMMIT_HISTORY_MAX_BYTES, MAX_COMMIT_HISTORY_MAX_BYTES,
        )
        self.last_error: str | None = None
        self.last_pruned: dict | None = None

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict, previous: bytes | None) -> Path | None:
        if previous is None:
            return None
        self._ensure()
        token = uuid.uuid4().hex[:12]
        safe_session = re.sub(r"[^A-Za-z0-9._-]", "_", str(entry.get("session") or "session"))
        backup = self.dir / f"{safe_session}-{token}.bak"
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=str(self.dir), prefix=".history-", suffix=".tmp", delete=False
        )
        temp_name = handle.name
        try:
            with handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, backup)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        metadata = dict(entry)
        metadata["backupFile"] = str(backup)
        metadata["createdAt"] = time.time()
        try:
            meta_path = self.dir / f"{safe_session}-{token}.json"
            meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        # Pruning happens *after* the new entry is durable, and a failure here
        # must never fail the delivery: the delivery has already saved its undo
        # point, and losing a trim is harmless while failing the commit is not.
        try:
            self.last_pruned = self.apply(self.plan())
        except OSError as exc:
            self.last_error = f"could not prune commit history: {exc}"
        return meta_path

    def _load(self) -> tuple[list[tuple[Path, dict]], list[Path]]:
        """Valid metadata entries, newest first, plus unreadable ones.

        An unreadable metadata file is *reported*, never deleted: it may be the
        only pointer to a backup whose bytes are still the user's last copy.
        """
        entries: list[tuple[Path, dict]] = []
        unreadable: list[Path] = []
        if not self.dir.is_dir():
            return entries, unreadable
        for candidate in sorted(self.dir.glob("*.json")):
            try:
                entry = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(entry, dict):
                    raise ValueError("metadata is not an object")
            except (OSError, ValueError):
                unreadable.append(candidate)
                continue
            entries.append((candidate, entry))
        entries.sort(key=lambda item: item[1].get("createdAt") or 0, reverse=True)
        return entries, unreadable

    @staticmethod
    def _entry_bytes(entry: dict) -> int:
        total = 0
        for value in (entry.get("backupFile"),):
            if not value:
                continue
            try:
                path = Path(str(value))
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def plan(self) -> dict:
        """Decide what would be removed, without touching the filesystem."""
        entries, unreadable = self._load()
        newest_by_session: set[Path] = set()
        seen_sessions: set[str] = set()
        for meta, entry in entries:
            session = str(entry.get("session") or "")
            if session not in seen_sessions:
                seen_sessions.add(session)
                newest_by_session.add(meta)

        removals: list[tuple[Path, dict]] = []
        removed_meta: set[Path] = set()
        per_session: dict[str, int] = {}
        for meta, entry in entries:
            session = str(entry.get("session") or "")
            per_session[session] = per_session.get(session, 0) + 1
            if per_session[session] > self.keep:
                removals.append((meta, entry))
                removed_meta.add(meta)

        remaining = [(meta, entry) for meta, entry in entries if meta not in removed_meta]
        total_bytes = sum(self._entry_bytes(entry) for _, entry in remaining)
        over_cap = False
        while len(remaining) > self.max_files or total_bytes > self.max_bytes:
            # `entries` is newest-first, so `remaining` reversed is oldest-first.
            # The newest entry of each session is skipped: it is the only point
            # `cmd_restore_last` can reach.
            candidate = next(
                ((meta, entry) for meta, entry in reversed(remaining)
                 if meta not in newest_by_session),
                None,
            )
            if candidate is None:
                over_cap = True
                break
            remaining.remove(candidate)
            removals.append(candidate)
            total_bytes -= self._entry_bytes(candidate[1])

        return {
            "remove": removals,
            "unreadable": unreadable,
            "kept": len(remaining),
            "keptBytes": total_bytes,
            "overCap": over_cap or len(remaining) > self.max_files
            or total_bytes > self.max_bytes,
        }

    def orphan_plan(self, live_sessions: set[str], older_than_days: int) -> dict:
        """Entries whose session no longer exists, older than the window.

        ``cmd_restore_last`` can only reach a session that still exists in the
        store, so a backup for a closed or expired session is no longer a usable
        recovery point. It is still *user data*, so this plan is only ever
        produced for the explicit maintenance command, and only for sessions
        whose newest delivery is older than ``older_than_days``. The report is
        printed before anything is deleted.
        """
        entries, unreadable = self._load()
        now = time.time()
        cutoff = now - older_than_days * 86400
        by_session: dict[str, list[tuple[Path, dict]]] = {}
        for meta, entry in entries:
            by_session.setdefault(str(entry.get("session") or ""), []).append((meta, entry))

        removals: list[tuple[Path, dict]] = []
        sessions: list[dict] = []
        for session, items in by_session.items():
            if session in live_sessions:
                continue
            newest = max(items, key=lambda item: float(item[1].get("createdAt") or 0))
            newest_at = float(newest[1].get("createdAt") or 0)
            if newest_at >= cutoff:
                continue
            size = sum(self._entry_bytes(entry) for _, entry in items)
            removals.extend(items)
            sessions.append({
                "session": session,
                "entries": len(items),
                "bytes": size,
                "idleDays": round((now - newest_at) / 86400, 1),
                "targetPath": newest[1].get("targetPath")
                or (items[0][1].get("targetPath") if items else None),
            })
        sessions.sort(key=lambda item: item["idleDays"], reverse=True)
        return {
            "remove": removals,
            "sessions": sessions,
            "entries": len(removals),
            "bytes": sum(self._entry_bytes(entry) for _, entry in removals),
            "unreadable": len(unreadable),
            "olderThanDays": older_than_days,
        }

    def apply_orphans(self, plan: dict) -> dict:
        """Execute an :meth:`orphan_plan`; returns per-entry and per-file counts."""
        removed_files = 0
        removed_entries = 0
        for meta, entry in plan.get("remove") or []:
            removed_files += self._delete_backup(entry)
            try:
                meta.unlink(missing_ok=True)
                removed_files += 1
                removed_entries += 1
            except OSError as exc:
                self.last_error = f"could not remove history entry {meta.name}: {exc}"
        return {
            "removedEntries": removed_entries,
            "removedFiles": removed_files,
            "removedBytes": plan.get("bytes", 0),
            "sessions": plan.get("sessions") or [],
        }

    def _delete_backup(self, entry: dict) -> int:
        raw = str(entry.get("backupFile") or "")
        if not raw:
            return 0
        backup = Path(raw)
        try:
            # Only delete inside this directory: metadata is data, and a
            # hand-edited or corrupted file must not turn maintenance into an
            # arbitrary-file delete.
            if not backup.is_file() or backup.resolve().parent != self.dir.resolve():
                return 0
            backup.unlink()
            return 1
        except OSError as exc:
            self.last_error = f"could not remove backup {backup.name}: {exc}"
            return 0

    def apply(self, plan: dict) -> dict:
        """Execute a plan produced by :meth:`plan`."""
        removed = 0
        for meta, entry in plan.get("remove") or []:
            removed += self._delete_backup(entry)
            try:
                meta.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                self.last_error = f"could not remove history entry {meta.name}: {exc}"
        return {
            "removedFiles": removed,
            "kept": plan.get("kept"),
            "keptBytes": plan.get("keptBytes"),
            "overCap": bool(plan.get("overCap")),
            "unreadable": len(plan.get("unreadable") or []),
        }

    def stats(self) -> dict:
        entries, unreadable = self._load()
        sessions = {str(entry.get("session") or "") for _, entry in entries}
        total_bytes = sum(self._entry_bytes(entry) for _, entry in entries)
        return {
            "directory": str(self.dir),
            "entries": len(entries),
            "sessions": len(sessions),
            "bytes": total_bytes,
            "unreadable": len(unreadable),
            "keepPerSession": self.keep,
            "maxFiles": self.max_files,
            "maxBytes": self.max_bytes,
            "overCap": len(entries) > self.max_files or total_bytes > self.max_bytes,
            "lastError": self.last_error,
        }

    def latest(self, session: str) -> tuple[Path, dict, Path] | None:
        if not self.dir.is_dir():
            return None
        found = []
        for candidate in self.dir.glob("*.json"):
            try:
                entry = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if entry.get("session") != session:
                continue
            backup = Path(str(entry.get("backupFile") or ""))
            if not backup.is_file():
                continue
            found.append((candidate, entry, backup))
        found.sort(key=lambda item: item[1].get("createdAt") or 0, reverse=True)
        return found[0] if found else None

    def available(self, session: str) -> bool:
        return self.latest(session) is not None


class Rosetta:
    def __init__(self, roots: Roots, store: Store, env: dict, *,
                 require_stable_round_trip: bool = False,
                 require_preview: bool = False,
                 activity_log: "EventLog | None" = None,
                 commit_history_keep: int = DEFAULT_COMMIT_HISTORY_KEEP,
                 commit_history_max_files: int = DEFAULT_COMMIT_HISTORY_MAX_FILES,
                 commit_history_max_bytes: int = DEFAULT_COMMIT_HISTORY_MAX_BYTES,
                 stale_draft_days: int = DEFAULT_STALE_DRAFT_DAYS):
        self.roots = roots
        self.store = store
        self.env = env
        self.require_stable_round_trip = require_stable_round_trip
        # When set, a commit must be backed by a preview of the *current* view.
        # The Host plugin enables this, because the user-facing contract already
        # says "preview before you write"; without enforcement a model that skips
        # the preview silently rewrites a file the user never saw.
        self.require_preview = require_preview
        self.events = activity_log
        self.stale_draft_days = clamp_int(
            stale_draft_days, DEFAULT_STALE_DRAFT_DAYS,
            MIN_STALE_DRAFT_DAYS, MAX_STALE_DRAFT_DAYS,
        )
        self.journal = CommitJournal(Path(store.path).parent / COMMIT_JOURNAL_DIR_NAME)
        self.history = CommitHistory(
            Path(store.path).parent / "commit-history",
            keep=commit_history_keep,
            max_files=commit_history_max_files,
            max_bytes=commit_history_max_bytes,
        )
        self._pairs = None

    @property
    def pairs(self) -> dict:
        if self._pairs is None:
            # compat_layer.adapters resolves t2l/pandoc/sv2v from the
            # environment at import time, so the overrides must be installed
            # before the first import of the adapter package.
            os.environ.update(
                {k: v for k, v in self.env.items() if k.startswith("ROSETTA_") and v}
            )
            from rosetta_studio import core as rosetta_core

            assert_clean_module(rosetta_core)
            from compat_layer import adapters as adapter_pkg

            assert_clean_module(adapter_pkg)
            self._pairs = rosetta_core.available_pairs() | rosetta_core.experimental_pairs()
        return self._pairs

    def pair(self, name: str):
        if not isinstance(name, str) or name not in self.pairs:
            raise BridgeError(
                "UNKNOWN_PAIR",
                f"Unknown pair {name!r}. Call ros_list_pairs for the exact names.",
                known=sorted(self.pairs),
            )
        return self.pairs[name]

    # -- commands ---------------------------------------------------------

    def cmd_pairs(self) -> dict:
        diagnostics = diagnose_backends(self.pairs, self.env)
        blocked = {entry["pair"]: entry for entry in diagnostics["pair_diagnostics"]}
        return {
            "pairs": [
                {
                    "name": pair.name,
                    "targetExtension": pair.target_extension,
                    "viewExtension": pair.view_extension,
                    "experimental": pair.experimental,
                    "backend": blocked.get(pair.name, {}),
                }
                for pair in self.pairs.values()
            ],
            "converters": diagnostics["converters"],
        }

    def cmd_doctor(self) -> dict:
        diagnostics = diagnose_backends(self.pairs, self.env)
        activity = self.events.report() if self.events is not None else {
            "path": None,
            "mode": "off",
            "enabled": False,
            "reason": "no activity log configured for this process",
        }
        return {
            "python": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
                "prefix": sys.prefix,
            },
            "imports": self._import_report(),
            "converters": diagnostics["converters"],
            "pairDiagnostics": diagnostics["pair_diagnostics"],
            "workspaceRoots": [str(r) for r in self.roots.roots],
            "store": str(self.store.path),
            "commitJournal": {
                "directory": str(self.journal.dir),
                "pending": self.journal.count(),
                "lastError": self.journal.last_error,
                "note": (
                    "A delivery publishes the artifact and updates the session store "
                    "as two separate steps, not one atomic transaction. A pending "
                    "entry means the artifact was written but the session record was "
                    "not updated; the next ros_* call replays it from the artifact's "
                    "hash."
                ),
            },
            # The two facts users most often conflate. They are reported next to
            # each other on purpose: "the fence is released" never means "the
            # draft was discarded".
            "fence": {
                "idleSeconds": FENCE_IDLE_SECONDS,
                "idleMinutes": round(FENCE_IDLE_SECONDS / 60),
                "note": (
                    "Any session - including one with an undelivered draft or a "
                    "pending preview - stops fencing Harness write/edit after this "
                    "much inactivity. The draft stays in the session store, and a "
                    "later delivery still refuses when the target changed on disk. "
                    "Shell commands, other plugins and external editors are never "
                    "fenced; Rosetta detects their writes as drift before delivery."
                ),
            },
            "retention": {
                "activityMode": activity.get("mode"),
                "activityRetentionDays": activity.get("retentionDays"),
                "activityMaxEvents": activity.get("maxEvents"),
                "activityPrunesOwnRowsOnly": True,
                "commitHistoryKeepPerSession": self.history.keep,
                "commitHistoryMaxFiles": self.history.max_files,
                "commitHistoryMaxBytes": self.history.max_bytes,
                "sessionTtlSeconds": SESSION_TTL_SECONDS,
                "maxSessions": MAX_SESSIONS,
                "staleDraftDays": self.stale_draft_days,
                "note": (
                    "Automatic pruning never removes an undelivered draft, a pending "
                    "preview, the newest saved previous version of a live session, or "
                    "a pending commit-journal entry. Activity rows are pruned only "
                    "for this plugin's own source, so MCP history in a shared "
                    "database is untouched. Backups whose session has closed or "
                    "expired are reported separately and removed only by an explicit "
                    "ros_maintenance clean with includeOrphanHistory. Run "
                    "ros_maintenance to see or change what is retained."
                ),
            },
            "storage": self.storage_report(activity),
            "limits": {
                "maxArtifactBytes": MAX_ARTIFACT_BYTES,
                "sessionTtlSeconds": SESSION_TTL_SECONDS,
                "maxSessions": MAX_SESSIONS,
                "fenceIdleSeconds": FENCE_IDLE_SECONDS,
                "activityMaxEvents": activity.get("maxEvents"),
                "activityRetentionDays": activity.get("retentionDays"),
                "commitHistoryKeepPerSession": self.history.keep,
                "commitHistoryMaxFiles": self.history.max_files,
                "commitHistoryMaxBytes": self.history.max_bytes,
            },
        }

    @staticmethod
    def _path_bytes(path: Path) -> int:
        total = 0
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                if candidate.is_file():
                    total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def storage_report(self, activity: dict | None = None) -> dict:
        """Sizes and counts of everything this plugin keeps locally.

        Metadata only: no artifact text and no file contents are read. A failure
        to inspect one area is reported in place rather than taking the whole
        doctor call down.
        """
        if activity is None:
            activity = self.events.report() if self.events is not None else {}
        history_stats = self.history.stats()
        try:
            live = {key for key, _record in self.store.all_sessions()}
            orphans = self.history.orphan_plan(live, self.stale_draft_days)
            history_stats["liveSessions"] = len(live)
            history_stats["orphanSessions"] = len(orphans.get("sessions") or [])
            history_stats["orphanEntries"] = int(orphans.get("entries") or 0)
            history_stats["orphanBytes"] = int(orphans.get("bytes") or 0)
        except (sqlite3.Error, OSError) as exc:
            history_stats["orphanError"] = f"{type(exc).__name__}: {exc}"
        report: dict = {
            "stateDir": str(Path(self.store.path).parent),
            "sessionStore": {
                "path": str(self.store.path),
                "bytes": self._path_bytes(Path(self.store.path)),
            },
            "commitHistory": history_stats,
            "commitJournal": {
                "directory": str(self.journal.dir),
                "pending": self.journal.count(),
            },
            "activityLog": {
                "path": activity.get("path"),
                "mode": activity.get("mode"),
                "enabled": activity.get("enabled"),
                "bytes": (
                    self._path_bytes(Path(activity["path"]))
                    if activity.get("path")
                    else 0
                ),
                "rows": activity.get("rows"),
                "wouldPrune": activity.get("wouldPrune"),
                "error": activity.get("retentionError") or activity.get("reason"),
            },
        }
        try:
            report["sessions"] = self.store.storage_counts(self.stale_draft_days)
        except sqlite3.Error as exc:
            report["sessions"] = {"error": f"{type(exc).__name__}: {exc}"}
        return report

    def _import_report(self) -> dict:
        report = {}
        for module_name in ("rosetta_studio", "compat_layer"):
            module = sys.modules.get(module_name)
            report[module_name] = getattr(module, "__file__", None) if module else None
        report["sys.path"] = list(sys.path)
        return report

    def cmd_maintenance(self, action: str = "report", apply: bool = False,
                        older_than_days: int | None = None,
                        include_drafts: bool = False,
                        include_orphan_history: bool = False,
                        harness_session: str | None = None) -> dict:
        """Report or apply local-state cleanup.

        ``report`` is read-only. ``clean`` is a **dry run** unless ``apply`` is
        exactly ``True``. Undelivered drafts are never touched unless
        ``includeDrafts`` is also exactly ``True`` and the draft is older than
        ``olderThanDays``; both are separate, explicit opt-ins because the
        session store holds the only copy of that work.

        Saved previous versions follow the same rule. The newest backup of every
        session that still exists is always kept, because ``ros_restore_last``
        can reach it. Backups whose session has closed or expired are reported
        separately; they are removed only with ``includeOrphanHistory=True``,
        ``apply=True``, and after their newest delivery is older than
        ``olderThanDays``.

        The commit journal is only *reported*, never cleaned: a pending entry is
        a delivery that must still be replayed, not stale bookkeeping.
        """
        if action not in ("report", "clean"):
            raise BridgeError(
                "BAD_MAINTENANCE_ACTION",
                "`action` must be 'report' or 'clean'.",
                action=action,
            )
        days = clamp_int(
            older_than_days if older_than_days is not None else self.stale_draft_days,
            self.stale_draft_days,
            MIN_STALE_DRAFT_DAYS,
            MAX_STALE_DRAFT_DAYS,
        )
        include = include_drafts is True
        include_orphans = include_orphan_history is True
        applied = action == "clean" and apply is True

        sessions = self.store.storage_counts(days)
        stale_drafts = self.store.stale_draft_sessions(days)
        history_plan = self.history.plan()
        live_sessions = {key for key, _record in self.store.all_sessions()}
        orphan_plan = self.history.orphan_plan(live_sessions, days)
        entries_by_session = {
            str(entry.get("session") or "")
            for _meta, entry in (self.history._load()[0])
        }
        activity_counts = (
            self.events.retention_counts()
            if self.events is not None and self.events.enabled
            else None
        )

        activity_result = {
            "mode": self.events.mode if self.events is not None else "off",
            "enabled": bool(self.events is not None and self.events.enabled),
            "source": EVENT_SOURCE,
            "rows": (activity_counts or {}).get("rows"),
            "wouldDelete": (activity_counts or {}).get("wouldDelete", 0),
            "deleted": 0,
            "prunesOwnRowsOnly": True,
        }
        history_result = {
            "entries": len(history_plan.get("remove") or []) + int(history_plan.get("kept") or 0),
            "bytes": history_plan.get("keptBytes"),
            "wouldRemove": len(history_plan.get("remove") or []),
            "removedFiles": 0,
            "keepPerSession": self.history.keep,
            "maxFiles": self.history.max_files,
            "maxBytes": self.history.max_bytes,
            "overCap": bool(history_plan.get("overCap")),
            # Distinguishing these two is the point: one is a usable undo, the
            # other is a backup no Rosetta command can reach any more.
            "liveSessionsWithHistory": len(entries_by_session & live_sessions),
            "includeOrphanHistory": include_orphans,
            "orphanSessions": len(orphan_plan.get("sessions") or []),
            "orphanEntries": int(orphan_plan.get("entries") or 0),
            "orphanBytes": int(orphan_plan.get("bytes") or 0),
            "wouldRemoveOrphans": int(orphan_plan.get("entries") or 0) if include_orphans else 0,
            "removedOrphanEntries": 0,
            "removedOrphanFiles": 0,
            "examples": (orphan_plan.get("sessions") or [])[:10],
        }
        drafts_result = {
            "includeDrafts": include,
            "olderThanDays": days,
            "stale": len(stale_drafts),
            "wouldRemove": len(stale_drafts) if include else 0,
            "removed": 0,
            "examples": [
                {
                    "session": entry["session"],
                    "targetPath": entry["targetPath"],
                    "idleDays": entry["idleDays"],
                }
                for entry in stale_drafts[:10]
            ],
        }
        notes: list[str] = []
        if action == "clean" and not applied:
            notes.append("apply was not true, so nothing was removed (dry run).")
        if stale_drafts and not include:
            notes.append(
                f"{len(stale_drafts)} undelivered draft(s) are older than {days} day(s) "
                "and were kept; pass includeDrafts=true to include them in an applied clean."
            )
        if orphan_plan.get("entries") and not include_orphans:
            notes.append(
                f"{orphan_plan['entries']} saved previous version(s) belong to "
                f"{len(orphan_plan.get('sessions') or [])} session(s) that no longer exist "
                f"and are older than {days} day(s); they were kept. They cannot be reached by "
                "ros_restore_last. Pass includeOrphanHistory=true to include them in an "
                "applied clean."
            )

        if applied:
            if self.events is not None and self.events.enabled:
                enforced = self.events.enforce_retention()
                if enforced is not None:
                    activity_result["deleted"] = int(enforced.get("deleted") or 0)
                    activity_result["rows"] = enforced.get("rows")
            history_applied = self.history.apply(history_plan)
            history_result["removedFiles"] = history_applied.get("removedFiles", 0)
            history_result["bytes"] = history_applied.get("keptBytes")
            history_result["overCap"] = bool(history_applied.get("overCap"))
            if include_orphans and orphan_plan.get("remove"):
                orphan_applied = self.history.apply_orphans(orphan_plan)
                history_result["removedOrphanEntries"] = int(
                    orphan_applied.get("removedEntries") or 0
                )
                history_result["removedOrphanFiles"] = int(
                    orphan_applied.get("removedFiles") or 0
                )
            session_applied = self.store.maintenance_clean(days, include_drafts=include)
            drafts_result["removed"] = session_applied.get("removedDrafts", 0)
            notes.append(
                f"removed {session_applied.get('removedClean', 0)} finished session record(s) "
                f"and {session_applied.get('removedDrafts', 0)} draft(s)."
            )
            if history_result.get("overCap"):
                notes.append(
                    "The commit-history ceiling is still exceeded. Every remaining entry is "
                    "the newest saved version of a session that still exists, so it is kept: "
                    "ros_restore_last can still reach it. Close those sessions (or let them "
                    "expire) and run the clean again with includeOrphanHistory=true."
                )
            if self.events is not None and self.events.retention_error:
                notes.append(
                    "Activity retention could not run: " + self.events.retention_error
                )

        return {
            "action": action,
            "applied": applied,
            "olderThanDays": days,
            "activity": activity_result,
            "commitHistory": history_result,
            "sessions": sessions,
            "drafts": drafts_result,
            "journal": {
                "pending": self.journal.count(),
                "note": "Pending entries are replayed, not cleaned: they are unfinished deliveries.",
            },
            "storage": self.storage_report(self.events.report() if self.events else None),
            "notes": notes,
        }

    def cmd_open(self, session: str, pair: str, path: str,
                 harness_session: str | None = None) -> dict:
        pair_obj = self.pair(pair)
        target = self.roots.resolve_artifact(path)
        if not target.is_file():
            raise BridgeError(
                "TARGET_NOT_FOUND",
                f"Target file does not exist: {target}",
                path=str(target),
            )
        if target.suffix.lower() != pair_obj.target_extension.lower():
            raise BridgeError(
                "EXTENSION_MISMATCH",
                f"Pair {pair!r} requires target files ending in "
                f"{pair_obj.target_extension}.",
                path=str(target),
            )
        content = read_text(target)
        digest = sha256_file(target)
        return self._prime_session(
            session, pair_obj, target, content, digest, True, "open",
            owner=owner_session_id(harness_session),
        )

    def cmd_create(self, session: str, pair: str, path: str, content: str,
                   harness_session: str | None = None) -> dict:
        pair_obj = self.pair(pair)
        target = self.roots.resolve_artifact(path)
        if target.exists():
            raise BridgeError(
                "TARGET_EXISTS",
                f"Refusing to create over an existing path: {target}",
                path=str(target),
            )
        if not target.parent.is_dir():
            raise BridgeError(
                "PARENT_MISSING",
                "The target's parent directory must already exist.",
                path=str(target.parent),
            )
        if target.suffix.lower() != pair_obj.target_extension.lower():
            raise BridgeError(
                "EXTENSION_MISMATCH",
                f"Pair {pair!r} requires target files ending in "
                f"{pair_obj.target_extension}.",
                path=str(target),
            )
        if content is None:
            content = ""
        if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise BridgeError("ARTIFACT_TOO_LARGE", "Initial content exceeds the limit.")
        return self._prime_session(
            session, pair_obj, target, content, None, False, "create",
            owner=owner_session_id(harness_session),
        )

    def cmd_create_from_view(self, session: str, pair: str, path: str,
                             agent_view: str,
                             harness_session: str | None = None) -> dict:
        """Create a target from the familiar representation the agent knows.

        The initial view is deliberately the pair's *view* language, not its
        target language. New artifacts therefore use the same adaptation path
        as edits to existing artifacts. Target-native pass-through blocks cannot
        be bootstrapped from this entry point because no target source exists to
        establish their contents; create such a file natively, then open it.
        """
        pair_obj = self.pair(pair)
        target = self.roots.resolve_artifact(path)
        if target.exists():
            raise BridgeError(
                "TARGET_EXISTS",
                f"Refusing to create over an existing path: {target}",
                path=str(target),
            )
        if not target.parent.is_dir():
            raise BridgeError(
                "PARENT_MISSING",
                "The target's parent directory must already exist.",
                path=str(target.parent),
            )
        if target.suffix.lower() != pair_obj.target_extension.lower():
            raise BridgeError(
                "EXTENSION_MISMATCH",
                f"Pair {pair!r} requires target files ending in "
                f"{pair_obj.target_extension}.",
                path=str(target),
            )
        try:
            agent_view.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(
                "INVALID_UNICODE",
                "The initial familiar view contains an invalid Unicode surrogate.",
                position=exc.start,
            ) from None
        if len(agent_view.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise BridgeError("ARTIFACT_TOO_LARGE", "Initial content exceeds the limit.")
        try:
            regions = preserved_regions(_comparison_form(agent_view))
        except MalformedView as exc:
            raise BridgeError(
                "PROJECTION_MALFORMED",
                f"The initial familiar view has malformed pass-through blocks: {exc.message}",
                pair=pair_obj.name,
                path=str(target),
            ) from exc
        if regions:
            raise BridgeError(
                "INITIAL_VIEW_PASSTHROUGH",
                "A new target has no existing target-native block to preserve. "
                "Remove the pass-through markers from the familiar-view draft, or "
                "create a native target file and open it with Rosetta.",
                pair=pair_obj.name,
                path=str(target),
            )
        return self._prime_session(
            session, pair_obj, target, "", None, False, "create-from-view",
            owner=owner_session_id(harness_session),
            initial_agent_view=agent_view,
            initial_baseline_view="",
        )

    def _prime_session(
        self,
        session: str,
        pair_obj,
        target: Path,
        target_content: str,
        digest: str | None,
        existed: bool,
        origin: str,
        owner: str | None = None,
        initial_agent_view: str | None = None,
        initial_baseline_view: str | None = None,
    ) -> dict:
        if initial_agent_view is not None:
            agent_view = initial_agent_view
            notes = [
                "Started from the familiar view; the new target remains absent until delivery."
            ]
            metadata: dict = {}
            preserved: list[str] = []
            baseline_view = (
                initial_baseline_view
                if initial_baseline_view is not None
                else initial_agent_view
            )
        elif not target_content.strip():
            agent_view = ""
            notes: list[str] = []
            metadata: dict = {}
            preserved: list[str] = []
            baseline_view = agent_view
        else:
            try:
                result, preserved = project_with_preservation(pair_obj, target_content)
            except BridgeError:
                raise
            except Exception as exc:  # noqa: BLE001 - a projection refusal, not a crash
                raise BridgeError(
                    "PROJECTION_FAILED",
                    f"The {pair_obj.name} adapter raised {type(exc).__name__} while "
                    f"projecting the target: {exc}.",
                    pair=pair_obj.name,
                    path=str(target),
                    origin=origin,
                    errorType=type(exc).__name__,
                ) from exc
            notes, metadata = self._notes(result)
            if not result.success:
                raise BridgeError(
                    "PROJECTION_FAILED",
                    "Projection failed: "
                    + "; ".join(result.errors or ["no detail reported"]),
                    pair=pair_obj.name,
                    path=str(target),
                    errors=list(result.errors),
                    origin=origin,
                )
            agent_view = result.content
            baseline_view = agent_view
            if preserved:
                metadata["preservedRegions"] = preserved
                notes.append(
                    f"{len(preserved)} new-format-only region(s) were marked as "
                    f"pass-through blocks instead of being translated or dropped: "
                    + "; ".join(preserved[:5])
                    + (" ..." if len(preserved) > 5 else "")
                )
            if not agent_view.strip():
                # A silent empty view is how a dropped document reads as success:
                # the agent would see nothing to edit and could commit that
                # nothing back over the author's file.
                raise BridgeError(
                    "PROJECTION_EMPTY",
                    "Projection produced an empty agent view for a nonempty target; "
                    "refusing to open a session that shows nothing.",
                    pair=pair_obj.name,
                    path=str(target),
                    origin=origin,
                )

        # The blocks the agent may not edit are recorded from the view just
        # produced, so later writes can refuse to remove or reorder them.
        #
        # Recorded in comparison form (LF line endings, no final terminator) so
        # that an agent editing on Windows - whose editor may write CRLF - is not
        # accused of changing a region's contents. Line-ending style is a file
        # convention, not region content; see _comparison_form.
        try:
            protected = preserved_regions(_comparison_form(agent_view))
        except MalformedView as exc:
            raise BridgeError(
                "PROJECTION_MALFORMED",
                f"The projected view has malformed pass-through blocks: {exc.message}",
                pair=pair_obj.name,
                path=str(target),
            ) from exc
        metadata["protectedRegions"] = [
            [marker, list(body)] for marker, body in protected
        ]

        now = time.time()
        record = {
            "pair": pair_obj.name,
            "targetPath": str(target),
            "targetSha256": digest,
            "targetExisted": existed,
            "agentView": agent_view,
            "baselineView": baseline_view,
            "deliveredTarget": target_content if existed else None,
            "projectionMetadata": metadata,
            "preview": None,
            "previewViewSha256": None,
            "ownerSessionId": owner,
            "createdAt": now,
            "updatedAt": now,
        }
        with self.store.transaction() as tx:
            tx.upsert(session, record)
        return {
            "session": session,
            "pair": pair_obj.name,
            "targetPath": str(target),
            "viewExtension": pair_obj.view_extension,
            "targetExtension": pair_obj.target_extension,
            "agentView": agent_view,
            "characters": len(agent_view),
            "protectedRegions": len(protected),
            "ownerSessionId": owner,
            "notes": notes,
            "created": not existed,
        }

    @staticmethod
    def _notes(result) -> tuple[list[str], dict]:
        notes = list(result.warnings)
        metadata = dict(result.metadata or {})
        if metadata.get("experimental"):
            notes.append(
                "Experimental Bash/Nushell adapter: a documented syntax subset only. "
                "Read the delivery back and run it before trusting it."
            )
        if _compile_status(metadata) is False:
            notes.append("The remaining engines produced Typst that did not compile.")
        method = metadata.get("method") or metadata.get("engine")
        if method:
            notes.append(f"Backend: {method}")
        if metadata.get("passthrough"):
            notes.append(
                f"{metadata['passthrough']} pass-through block(s) preserved verbatim."
            )
        return notes, metadata

    def _session(self, session: str) -> dict:
        key = self.store.resolve_id(session)
        record = self.store.get(key)
        if record is None:
            raise BridgeError(
                "UNKNOWN_SESSION",
                "Unknown or expired Rosetta session; open the artifact again.",
                session=session,
            )
        return record

    def _deliver_or_refuse(self, pair_obj, view: str, *, session: str, phase: str):
        """Turn an adapter exception into a structured refusal.

        Some reverse adapters raise a converter-specific exception instead of
        returning a failed ``TranslationResult``. Letting that reach ``main``
        produced ``BRIDGE_CRASH``, which sounds like the bridge died and hides
        the fact that nothing was written. The type and message are preserved;
        only the error class becomes the normal delivery refusal.
        """
        try:
            result = pair_obj.deliver(view)
            if (result.success and pair_obj.name == "LaTeX / Typst"
                    and _compile_status(result.metadata) is False
                    and re.search(r'#(?:include|import|bibliography)\b|\bimage\s*\(', result.content)):
                record = self._session(session)
                if self._typst_compiles_in_workspace(result.content, record["targetPath"]):
                    result.metadata["compiles"] = True
                    result.metadata["compile_context"] = "workspace"
                    result.warnings = [w for w in result.warnings if w != "Output does not compile with typst"]
                    result.warnings.append(
                        "Typst compiled with the target project's files; inspect the preview for semantic changes."
                    )
            return result
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter failures are refusals here
            raise BridgeError(
                "DELIVERY_FAILED",
                f"The {pair_obj.name} adapter raised {type(exc).__name__} while "
                f"translating the view: {exc}. Nothing was written.",
                pair=pair_obj.name,
                session=session,
                phase=phase,
                errorType=type(exc).__name__,
            ) from exc

    def _typst_compiles_in_workspace(self, content: str, target_path: str) -> bool:
        """Retry a failed isolated check with project-relative files available.

        The target is never overwritten. A temporary sibling preserves relative
        import resolution; Typst's root is limited to an allowed workspace.
        Any missing compiler, timeout or compilation failure remains a refusal.
        """
        target = self.roots.resolve_artifact(target_path)
        roots = [root for root in self.roots.roots if target.is_relative_to(root)]
        if not roots or not target.parent.is_dir():
            return False
        executable = self.env.get("ROSETTA_TYPST") or shutil.which("typst")
        if not executable:
            return False
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".typ",
                                             prefix=".rosetta-check-", dir=target.parent,
                                             delete=False) as source:
                temp_path = Path(source.name)
                source.write(content)
            with tempfile.TemporaryDirectory() as scratch:
                outcome = subprocess.run(
                    [str(executable), "compile", "--root", str(roots[0]), str(temp_path),
                     str(Path(scratch) / "check.pdf")],
                    cwd=target.parent, capture_output=True, timeout=30, check=False,
                )
                return outcome.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _owner_matches(record: dict, harness_session: str | None) -> bool:
        """Whether a chat may see or act on this session.

        ``None`` is the direct-bridge/scripted path and keeps working for local
        diagnostics. The Harness Host never relies on this: every agent tool and
        browser action passes the trusted chat id, so a model cannot see another
        chat's session merely by omitting it.
        """
        if harness_session is None:
            return True
        requested = owner_session_id(harness_session)
        return bool(requested) and record.get("ownerSessionId") == requested

    def _require_owner(self, record: dict, harness_session: str | None) -> None:
        """Refuse a tool or browser action aimed at another chat's session.

        A session with no recorded owner cannot be claimed by any chat, because
        the plugin cannot prove the relation.
        """
        if harness_session is None:
            return
        requested = owner_session_id(harness_session)
        owner = record.get("ownerSessionId")
        if not requested or owner != requested:
            raise BridgeError(
                "SESSION_OWNERSHIP_MISMATCH",
                "This Rosetta session belongs to another chat in this workspace, "
                "or it was opened without a chat identity. Nothing was changed. "
                "Open the artifact in the current chat to create a session that "
                "this status strip may act on.",
                session=record.get("_key"),
                requestedOwner=requested,
                actualOwner=owner,
            )

    @staticmethod
    def _protected(record: dict) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """The pass-through blocks this session must keep, in order."""
        raw = (record.get("projectionMetadata") or {}).get("protectedRegions") or []
        return tuple((marker, tuple(body)) for marker, body in raw)

    def _require_regions_intact(self, record: dict, view: str) -> None:
        required = self._protected(record)
        if not required:
            return
        try:
            # Compared in comparison form, matching how the regions were recorded:
            # a CRLF view from a Windows editor is not a changed region.
            intact = retains_regions(_comparison_form(view), required)
        except MalformedView as exc:
            raise BridgeError(
                "PASSTHROUGH_MALFORMED",
                f"The agent view has malformed pass-through blocks: {exc.message}",
                session=record.get("_key"),
            ) from exc
        if not intact:
            raise BridgeError(
                "PASSTHROUGH_CHANGED",
                "A pass-through region was removed, reordered or duplicated. Those "
                "regions carry target-only content that cannot be reconstructed; "
                "edit the familiar region instead and leave the marked blocks as "
                "they are.",
                session=record.get("_key"),
                protectedRegions=len(required),
            )

    def cmd_read(self, session: str, harness_session: str | None = None) -> dict:
        record = self._session(session)
        self._require_owner(record, harness_session)
        return {
            "session": record["_key"],
            "pair": record["pair"],
            "targetPath": record["targetPath"],
            "agentView": record["agentView"],
            "dirty": record["agentView"] != record["baselineView"],
        }

    def cmd_write(self, session: str, content: str,
                  harness_session: str | None = None) -> dict:
        if content is None:
            raise BridgeError("CONTENT_REQUIRED", "The new agent view is required.")
        try:
            content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(
                "INVALID_UNICODE",
                "The agent view contains an invalid Unicode surrogate; no changes were saved. "
                "Ask the agent to re-read the Rosetta view and retry the edit.",
                position=exc.start,
            ) from None
        if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise BridgeError("ARTIFACT_TOO_LARGE", "Agent view exceeds the limit.")
        with self.store.transaction() as tx:
            key = tx.resolve_id(session)
            record = tx.get(key)
            if record is None:
                raise BridgeError(
                    "UNKNOWN_SESSION",
                    "Unknown or expired Rosetta session; open the artifact again.",
                    session=session,
                )
            record["_key"] = key
            self._require_owner(record, harness_session)
            self._require_regions_intact(record, content)
            tx.update(
                key,
                agentView=content,
                preview=None,
                previewViewSha256=None,
                updatedAt=time.time(),
            )
            baseline = record["baselineView"]
        return {
            "session": key,
            "stored": True,
            "characters": len(content),
            "dirty": content != baseline,
        }

    def _drift(self, record: dict) -> dict:
        target = Path(record["targetPath"])
        current = sha256_file(target)
        if not record["targetExisted"]:
            if current is None:
                return {"drifted": False, "reason": "target still absent"}
            return {
                "drifted": True,
                "reason": "target appeared on disk after the session was created",
                "expectedSha256": None,
                "actualSha256": current,
            }
        if current is None:
            return {
                "drifted": True,
                "reason": "target was deleted by another process",
                "expectedSha256": record["targetSha256"],
                "actualSha256": None,
            }
        if record["targetSha256"] and current != record["targetSha256"]:
            return {
                "drifted": True,
                "reason": "target changed on disk since the session last accepted it",
                "expectedSha256": record["targetSha256"],
                "actualSha256": current,
            }
        return {"drifted": False, "actualSha256": current}

    def _would_erase(self, record: dict, target: Path) -> bool:
        """True when writing an empty delivery would destroy existing content."""
        if (record.get("agentView") or "").strip():
            return True
        if (record.get("deliveredTarget") or "").strip():
            return True
        try:
            if target.is_file() and read_text(target).strip():
                return True
        except BridgeError:
            # An unreadable target is not evidence of emptiness; refuse the
            # empty delivery rather than guess.
            return True
        return False

    def _clear_preview(self, session_key: str, strict: bool = False) -> None:
        """Drop a stored preview and its commit token.

        A failed preview must not leave the previous one in place. The commit path
        reuses a stored preview whenever the view has not changed since, so a stale
        preview would be committed as though it were the delivery the caller just
        asked for - after a preview that explicitly refused to produce one. The
        preview and the view hash that authorises its reuse are therefore cleared
        together, and the engine metadata with them.
        """
        try:
            with self.store.transaction() as tx:
                tx.update(
                    session_key,
                    preview=None,
                    previewViewSha256=None,
                    previewMetadata={},
                    updatedAt=time.time(),
                )
        except BridgeError:
            # The caller is already handling a refusal; failing to tidy up must not
            # replace that reason with a bookkeeping error. An explicit user
            # discard, however, must not claim success when the clear failed.
            if strict:
                raise

    def cmd_discard_preview(self, session: str, harness_session: str | None = None) -> dict:
        """Drop the candidate delivery but keep the familiar-view edit.

        Releasing the whole session is a much bigger action: it also discards
        the agent view. A user who previewed, looked at the diff, and decided
        not to deliver usually wants to keep editing instead.
        """
        record = self._session(session)
        self._require_owner(record, harness_session)
        self._clear_preview(record["_key"], strict=True)
        return {
            "session": record["_key"],
            "discarded": True,
            "preview": False,
        }

    def cmd_preview(self, session: str, harness_session: str | None = None) -> dict:
        record = self._session(session)
        self._require_owner(record, harness_session)
        pair_obj = self.pair(record["pair"])
        self._require_regions_intact(record, record["agentView"])
        # Any refusal below invalidates whatever was previewed before, so the old
        # preview is dropped first rather than left as a commit token.
        self._clear_preview(record["_key"])
        result = self._deliver_or_refuse(
            pair_obj, record["agentView"],
            session=record["_key"], phase="preview",
        )
        notes, metadata = self._notes(result)
        if not result.success:
            raise BridgeError(
                "DELIVERY_FAILED",
                "Delivery preview failed: "
                + "; ".join(result.errors or ["no detail reported"])
                + " The previous preview, if any, has been discarded; there is "
                "nothing pending to commit.",
                pair=record["pair"],
                errors=list(result.errors),
            )
        if _compile_status(metadata) is False:
            raise BridgeError(
                "DELIVERY_DOES_NOT_COMPILE",
                "The translation engines produced output that does not compile, and "
                "the adapters report no complete translation: "
                + "; ".join(result.errors or result.warnings or ["no detail reported"])
                + " Nothing was written and nothing is pending to commit.",
                pair=record["pair"],
                metadata=_public_metadata(metadata),
            )
        preview_content = result.content
        if pair_obj.name == "LaTeX / Typst":
            preview_content = _remove_generated_typst_wrappers(preview_content, record)
        if not result.content.strip() and self._would_erase(
            record, Path(record["targetPath"])
        ):
            raise BridgeError(
                "DELIVERY_EMPTY",
                "Delivery produced an empty artifact while the target holds "
                "content; refusing to preview a delivery that would erase it.",
                pair=record["pair"],
            )
        convertible = has_convertible_content(record["agentView"])
        dirty = record["agentView"] != record["baselineView"]
        view_edited = dirty and convertible
        if not convertible:
            notes.append(
                "No convertible content: the familiar view is pass-through only, so "
                "the delivery below is the target text carried back, not a translation "
                "of the requested edit."
            )
        if not dirty:
            notes.append(
                "The familiar view has not changed since it was opened, so this "
                "preview contains no edit and does not verify that the requested "
                "change was made."
            )
        stability = self._stability(record, pair_obj, preview_content)
        with self.store.transaction() as tx:
            tx.update(
                record["_key"],
                preview=preview_content,
                previewViewSha256=sha256_bytes(record["agentView"].encode("utf-8")),
                previewMetadata=metadata,
                updatedAt=time.time(),
            )
        drift = self._drift(record)
        delivered = record.get("deliveredTarget")
        return {
            "session": record["_key"],
            "pair": record["pair"],
            "targetPath": record["targetPath"],
            "targetContent": preview_content,
            "previewSha256": sha256_bytes(preview_content.encode("utf-8")),
            "changedFromDelivered": delivered is None or delivered != preview_content,
            "targetDrift": drift,
            "roundTrip": stability,
            "dirty": dirty,
            # A preview is always a candidate. `exact` round trip proves the text
            # survives re-projection; it cannot prove the requested change was
            # actually expressed, so no field here claims verification.
            "candidateOnly": True,
            "requestVerified": False,
            "hasConvertibleContent": convertible,
            "viewEdited": view_edited,
            "conversion": "translatable" if convertible else "passthrough-only",
            "ownerSessionId": record.get("ownerSessionId"),
            "notes": notes + _stability_note(stability),
        }

    def cmd_restore_last(self, session: str, harness_session: str | None = None) -> dict:
        """Restore the bytes that preceded the most recent delivery."""
        record = self._session(session)
        self._require_owner(record, harness_session)
        latest = self.history.latest(record["_key"])
        if latest is None:
            raise BridgeError(
                "RESTORE_UNAVAILABLE",
                "No previous version is available for this session. The artifact may "
                "have been new, or the delivery history was not retained.",
                session=record["_key"],
            )
        _, entry, backup = latest
        target = Path(record["targetPath"])
        current = sha256_file(target)
        if current is None or current != entry.get("newSha256"):
            raise BridgeError(
                "RESTORE_TARGET_CHANGED",
                "The target is no longer the version this delivery wrote; refusing to "
                "restore over newer content.",
                path=str(target),
                expectedSha256=entry.get("newSha256"),
                actualSha256=current,
            )
        payload = backup.read_bytes()
        temp_name = None
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(target.parent),
                prefix=f".{target.name}.restore-",
                suffix=".tmp",
                delete=False,
            )
            temp_name = handle.name
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
        with self.store.transaction() as tx:
            tx.delete(record["_key"])
        return {
            "session": record["_key"],
            "restored": True,
            "targetPath": str(target),
            "restoredSha256": sha256_bytes(payload),
            "closed": True,
        }

    def cmd_commit(self, session: str, harness_session: str | None = None,
                   expected_preview_sha256: str | None = None,
                   expected_view_sha256: str | None = None,
                   expected_target_sha256: str | None = None) -> dict:
        """Write the delivered artifact, gating every refusal before the write.

        Ordering is the point. The stability gate, the region check, the drift
        check and the empty-delivery check all run while the target is still
        untouched, so a refusal can promise "nothing was written" and mean it.
        Previously the plugin evaluated stability *after* the bridge had already
        replaced the file, which let it report a failed commit over a modified
        artifact.

        The delivery written is the one that was previewed whenever the view has
        not changed since, so what the user approved is byte-for-byte what lands.
        """
        record = self._session(session)
        self._require_owner(record, harness_session)
        pair_obj = self.pair(record["pair"])

        # The UI confirmation names the exact candidate the user saw. If the
        # agent edited the view, replaced the preview, or another process wrote
        # the target while the confirmation was open, all three digests stop
        # matching and the commit is refused before any write.
        mismatches = []
        if expected_preview_sha256 is not None:
            preview = record.get("preview")
            actual_preview = (
                sha256_bytes(preview.encode("utf-8")) if preview is not None else None
            )
            if actual_preview != expected_preview_sha256:
                mismatches.append("preview")
        if expected_view_sha256 is not None:
            actual_view = sha256_bytes((record["agentView"] or "").encode("utf-8"))
            if actual_view != expected_view_sha256:
                mismatches.append("view")
        if expected_target_sha256 is not None:
            try:
                actual_target = sha256_file(Path(record["targetPath"]))
            except OSError:
                actual_target = None
            if actual_target != expected_target_sha256:
                mismatches.append("target")
        if mismatches:
            raise BridgeError(
                "CONFIRMATION_STALE",
                "Nothing was written: the candidate, the familiar view, or the target "
                "changed after the user confirmed. Preview the current view again and "
                "confirm that new candidate.",
                session=record["_key"],
                changed=mismatches,
            )

        self._require_regions_intact(record, record["agentView"])

        convertible = has_convertible_content(record["agentView"])
        view_edited = convertible and record["agentView"] != record["baselineView"]

        # Re-use the previewed bytes when the view is unchanged since preview;
        # otherwise translate once, here, and gate on that exact result. The
        # engine metadata is carried with the preview so this path can still see
        # the adapter's own `compiles` verdict - no fresh translation runs here.
        view_sha = sha256_bytes(record["agentView"].encode("utf-8"))
        preview_is_current = (
            record.get("preview") is not None
            and record.get("previewViewSha256") == view_sha
        )
        if self.require_preview and not preview_is_current:
            raise BridgeError(
                "PREVIEW_REQUIRED",
                "Nothing was written: the current view has not been previewed. "
                "Run ros_preview_delivery first so the delivery can be shown and "
                "reviewed, then commit that preview. If the view changed after the "
                "preview, preview again.",
                path=str(record["targetPath"]),
            )
        if preview_is_current:
            delivered = record["preview"]
            metadata = dict(record.get("previewMetadata") or {})
            notes = self._notes_from_metadata(metadata, pair_obj)
        else:
            result = self._deliver_or_refuse(
                pair_obj, record["agentView"],
                session=record["_key"], phase="commit",
            )
            if not result.success:
                self._clear_preview(record["_key"])
                raise BridgeError(
                    "DELIVERY_FAILED",
                    "Delivery failed: "
                    + "; ".join(result.errors or ["no detail reported"]),
                    pair=record["pair"],
                    errors=list(result.errors),
                )
            delivered = result.content
            if pair_obj.name == "LaTeX / Typst":
                delivered = _remove_generated_typst_wrappers(delivered, record)
            notes, metadata = self._notes(result)

        if not convertible:
            notes.append(
                "No convertible content: the target is pass-through text carried "
                "back unchanged. The write below is not evidence that the requested "
                "edit was translated or applied."
            )
        if not view_edited:
            notes.append(
                "The familiar view was unchanged since it was opened, so this "
                "delivery contains no edit. The target may be written, but no "
                "requested change was delivered."
            )

        # A known compile failure must never reach the disk, whether it came from a
        # fresh translation or from the metadata stored with a preview. Nothing is
        # written, and the preview is dropped so it cannot be committed later.
        if _compile_status(metadata) is False:
            self._clear_preview(record["_key"])
            raise BridgeError(
                "DELIVERY_DOES_NOT_COMPILE",
                "The adapters report that this delivery does not compile "
                "(metadata.compiles is false), so nothing was written. Compiling is "
                "not proof that the content survived, but a known failure is proof "
                "that it did not; re-run ros_preview_delivery after changing the "
                "view, or fix the construct the adapter named.",
                pair=record["pair"],
                path=str(record["targetPath"]),
                metadata=_public_metadata(metadata),
            )

        target = Path(record["targetPath"])
        # Emptiness is judged against what is on disk, not only against the view:
        # a whitespace-only view strips to nothing, so gating on the view alone
        # let an empty delivery overwrite a real artifact.
        if not delivered.strip() and self._would_erase(record, target):
            raise BridgeError(
                "DELIVERY_EMPTY",
                "Delivery produced an empty artifact while the target holds "
                "content; refusing to overwrite it with nothing.",
                pair=record["pair"],
                path=str(target),
            )
        payload = delivered.encode("utf-8")
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise BridgeError("ARTIFACT_TOO_LARGE", "Delivered artifact exceeds the limit.")

        self.roots.resolve_artifact(str(target))

        # ---- every refusal below happens before the target is touched --------
        drift = self._drift(record)
        if drift["drifted"]:
            raise BridgeError(
                "TARGET_CHANGED",
                f"Refusing to overwrite: {drift['reason']}. Reopen the artifact to "
                f"pick up the newer content before delivering again.",
                path=str(target),
                expectedSha256=drift.get("expectedSha256"),
                actualSha256=drift.get("actualSha256"),
            )

        stability = self._stability(record, pair_obj, delivered)

        if self.require_stable_round_trip and not stability["stable"]:
            raise BridgeError(
                "ROUND_TRIP_NOT_STABLE",
                "requireStableRoundTrip is enabled and this delivery does not "
                "provably re-project to the approved view "
                f"({stability['status']}: {stability.get('reason', 'no detail')}). "
                "Nothing was written; the target, the session and the write fence "
                "are unchanged.",
                path=str(target),
                status=stability["status"],
                roundTrip=stability,
            )

        # ---- the one write ---------------------------------------------------
        #
        # The publish and the session update are two separate steps, and this
        # code does not pretend they are one atomic transaction. What it does
        # guarantee, in order:
        #
        #   1. every pre-write gate above has already run, so a refusal here
        #      means nothing was written;
        #   2. the session is re-read inside the store transaction, so a
        #      concurrent edit still refuses before anything is published;
        #   3. a durable journal entry describing the publish is written BEFORE
        #      os.replace, so the intent survives a crash between the steps;
        #   4. if the store update then fails, the entry is KEPT and the caller
        #      is told the artifact was written - not given a false failure.
        published = False
        digest = sha256_bytes(payload)
        journal_path: Path | None = None
        state_update_error: BaseException | None = None
        try:
            with self.store.transaction() as tx:
                fresh = tx.get(record["_key"])
                if fresh is None:
                    raise BridgeError(
                        "UNKNOWN_SESSION",
                        "The session disappeared before the delivery was written.",
                        session=record["_key"],
                    )
                if fresh["agentView"] != record["agentView"]:
                    raise BridgeError(
                        "SESSION_CHANGED",
                        "The agent view changed while the delivery was being prepared; "
                        "nothing was written. Preview again.",
                        session=record["_key"],
                    )
                fresh["_key"] = record["_key"]

                # Keep the old bytes before publishing. This is a user-facing
                # undo, not crash recovery (that is the journal's job): a
                # successful delivery must remain reversible.
                previous_bytes = None
                try:
                    if target.is_file():
                        previous_bytes = target.read_bytes()
                    self.history.record(
                        {
                            "session": record["_key"],
                            "pair": record["pair"],
                            "targetPath": str(target),
                            "newSha256": digest,
                            "previousSha256": (
                                sha256_bytes(previous_bytes)
                                if previous_bytes is not None
                                else None
                            ),
                        },
                        previous_bytes,
                    )
                except OSError as exc:
                    raise BridgeError(
                        "HISTORY_UNAVAILABLE",
                        f"Could not save the previous version for undo ({exc}); "
                        "nothing was written.",
                        path=str(target),
                    ) from exc

                # The journal must exist before the artifact is published.
                # Without a recovery record the failure this whole path exists
                # for would be silent, so an unwritable journal is a pre-write
                # refusal rather than something to shrug off.
                try:
                    journal_path = self.journal.record(
                        {
                            "session": record["_key"],
                            "pair": record["pair"],
                            "targetPath": str(target),
                            "newSha256": digest,
                            "previousSha256": record.get("targetSha256"),
                            "deliveredTarget": delivered,
                            "approvedView": record["agentView"],
                            "bytes": len(payload),
                        }
                    )
                except OSError as exc:
                    raise BridgeError(
                        "JOURNAL_UNAVAILABLE",
                        f"Could not record the delivery in the commit journal ({exc}); "
                        "nothing was written. A delivery is only published when the "
                        "intent to publish has been recorded, because the artifact write "
                        "and the session update are separate steps.",
                        path=str(target),
                    ) from exc

                temp_name = None
                try:
                    handle = tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=str(target.parent),
                        prefix=f".{target.name}.rosetta-",
                        suffix=".tmp",
                        delete=False,
                    )
                    temp_name = handle.name
                    with handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Re-check right before publication so a peer writer cannot
                    # slip in between the drift check and the replace.
                    recheck = self._drift(fresh)
                    if recheck["drifted"]:
                        raise BridgeError(
                            "TARGET_CHANGED_DURING_COMMIT",
                            "The target changed while the delivery was being written; "
                            "nothing was published.",
                            path=str(target),
                        )
                    os.replace(temp_name, target)
                    temp_name = None
                    published = True
                except BaseException:
                    if temp_name and os.path.exists(temp_name):
                        os.unlink(temp_name)
                    raise

                tx.update(
                    record["_key"],
                    targetSha256=digest,
                    targetExisted=True,
                    deliveredTarget=delivered,
                    preview=delivered,
                    previewViewSha256=view_sha,
                    baselineView=record["agentView"],
                    updatedAt=time.time(),
                )
        except BaseException as exc:
            if not published:
                # Nothing reached the target, so there is nothing to recover and
                # the caller's "nothing was written" promise holds. Drop the
                # intent record so it cannot be mistaken for a real write later.
                self.journal.resolve(journal_path)
                raise
            # The artifact IS new. Keep the record and report the truth below
            # instead of raising a failure over a file that has already changed.
            state_update_error = exc

        if state_update_error is not None:
            store_error = {
                "type": type(state_update_error).__name__,
                "message": str(state_update_error),
            }
            return {
                "session": record["_key"],
                "written": True,
                "targetPath": str(target),
                "targetSha256": digest,
                "bytes": len(payload),
                "stateApplied": False,
                "stateUpdateFailed": True,
                "stateUpdateError": store_error,
                "requestVerified": False,
                "hasConvertibleContent": convertible,
                "viewEdited": view_edited,
                "conversion": "translatable" if convertible else "passthrough-only",
                "ownerSessionId": record.get("ownerSessionId"),
                "recovery": {
                    "journal": str(journal_path) if journal_path else None,
                    "recoverable": journal_path is not None,
                    "action": (
                        "The artifact was written; only the session record is stale. "
                        "Any later ros_* call replays the journal entry from the "
                        "artifact's hash, or run ros_doctor to see the pending count."
                    ),
                },
                "notes": notes
                + [
                    "The artifact was written, but the session record could not be "
                    f"updated ({store_error['type']}: {store_error['message']}). "
                    "The file and the session store are separate steps and are not "
                    "one atomic transaction; a journal entry was kept so the record "
                    "can be replayed from the artifact."
                ]
                + _stability_note(stability, committed=True),
                "roundTrip": stability,
                "verification": verify_artifact(record["pair"], target, delivered, self.env),
            }

        self.journal.resolve(journal_path)
        verification = verify_artifact(record["pair"], target, delivered, self.env)
        return {
            "session": record["_key"],
            "written": True,
            "targetPath": str(target),
            "targetSha256": digest,
            "bytes": len(payload),
            "stateApplied": True,
            "stateUpdateFailed": False,
            "requestVerified": False,
            "hasConvertibleContent": convertible,
            "viewEdited": view_edited,
            "conversion": "translatable" if convertible else "passthrough-only",
            "ownerSessionId": record.get("ownerSessionId"),
            "notes": notes + _stability_note(stability, committed=True),
            "roundTrip": stability,
            "verification": verification,
        }

    def _notes_from_metadata(self, metadata: dict, pair_obj) -> list[str]:
        """Notes for a delivery that was already translated (previewed) earlier.

        Built from the metadata stored with the preview rather than from nothing,
        so the commit reports the same backend and the same warnings the preview
        did - including a `compiles` verdict that the caller is about to rely on.
        """
        notes: list[str] = []
        if pair_obj.experimental:
            notes.append(
                "Experimental Bash/Nushell adapter: a documented syntax subset only. "
                "Read the delivery back and run it before trusting it."
            )
        if _compile_status(metadata) is False:
            notes.append("The remaining engines produced Typst that did not compile.")
        method = metadata.get("method") or metadata.get("engine")
        if method:
            notes.append(f"Backend: {method}")
        if metadata.get("passthrough"):
            notes.append(
                f"{metadata['passthrough']} pass-through block(s) preserved verbatim."
            )
        for warning in metadata.get("warnings") or []:
            notes.append(str(warning))
        return notes

    def recover_pending_commits(self) -> dict:
        """Replay journalled deliveries whose session update did not land.

        Called before the first command of every bridge process, so a database
        left stale by a failed update repairs itself on the next Rosetta call
        without the user having to do anything.

        The decision is made from the artifact, not from the journal's own claim:
        an entry is applied only when the target's *current* hash equals the hash
        the entry says was published. If it does not match, the publish never
        landed and the entry is dropped - applying it would record a delivery
        that the file does not contain.
        """
        entries = self.journal.pending()
        report = {"pending": len(entries), "applied": [], "discarded": [], "undecided": []}
        for path, entry in entries:
            session = entry.get("session")
            target_raw = entry.get("targetPath")
            new_sha = entry.get("newSha256")
            if not target_raw or not new_sha:
                self.journal.resolve(path)
                report["discarded"].append({"journal": path.name, "reason": "entry is incomplete"})
                continue
            target = Path(target_raw)
            try:
                actual = sha256_file(target) if target.is_file() else None
            except (OSError, BridgeError) as exc:
                # Cannot decide without reading the artifact. Keep the entry.
                report["undecided"].append(
                    {"journal": path.name, "reason": f"target unreadable: {exc}"}
                )
                continue

            if actual != new_sha:
                self.journal.resolve(path)
                report["discarded"].append(
                    {
                        "journal": path.name,
                        "session": session,
                        "reason": "the artifact does not hold the published bytes, so the "
                        "write never landed; the session record was already correct",
                    }
                )
                continue

            try:
                with self.store.transaction() as tx:
                    record = tx.get(session) if session else None
                    if record is None:
                        # The session is gone; there is no record to repair.
                        report["discarded"].append(
                            {"journal": path.name, "session": session,
                             "reason": "the session no longer exists"}
                        )
                    else:
                        updates = {
                            "targetSha256": new_sha,
                            "targetExisted": True,
                            "deliveredTarget": entry.get("deliveredTarget"),
                            "preview": None,
                            "previewViewSha256": None,
                            "updatedAt": time.time(),
                        }
                        # Only re-baseline the view when the agent has not edited
                        # since; otherwise the pending edit must survive.
                        approved = entry.get("approvedView")
                        if approved is not None and record["agentView"] == approved:
                            updates["baselineView"] = approved
                        tx.update(session, **updates)
                        report["applied"].append(
                            {"journal": path.name, "session": session,
                             "targetSha256": new_sha, "pair": record["pair"]}
                        )
                self.journal.resolve(path)
            except (BridgeError, sqlite3.Error, OSError) as exc:
                # Still broken. Keep the entry so the next call can try again.
                report["undecided"].append(
                    {"journal": path.name, "session": session,
                     "reason": f"{type(exc).__name__}: {exc}"}
                )
        return report

    def _stability(self, record: dict, pair_obj, delivered: str) -> dict:
        """Measure whether delivering ``delivered`` provably re-projects to the approved view.

        The contract is deliberately weak and explicit, because an overstated
        stability claim is worse than no claim:

        ``exact``
            Re-projecting the delivery reproduces the approved view **byte for
            byte**. This is the only status that sets ``stable``.
        ``unstable``
            A definite, located difference was measured. The report names which
            part differs: the ordered pass-through regions, or the translated
            text.
        ``unknown``
            The comparison could not be carried out at all (re-projection raised
            or failed, or a view's markers are malformed). Treated as not stable.

        Two earlier shortcuts are gone on purpose:

        * regions were compared as a **set**, which ignored how many times a block
          occurred and in what order - so swapping two preserved blocks, or
          deleting one of two identical blocks, read as stable;
        * the translated text was compared after **collapsing all whitespace**,
          which cannot distinguish formatting differences from a real change
          inside a string literal or a code token.

        Neither can be repaired by normalizing harder, so the bridge now reports
        only what it can prove: a byte fixed point. Where a translation engine
        legitimately injects its own preamble on the first delivery, the honest
        answer is ``unstable`` for that round - and the next round, measured on
        the artifact that now carries those directives, is ``exact``.
        """
        approved = record["agentView"]
        try:
            reprojected, _ = project_with_preservation(pair_obj, delivered)
        except Exception as exc:  # noqa: BLE001 - report, never guess
            return {
                "status": "unknown",
                "stable": False,
                "reason": f"re-projection raised {type(exc).__name__}: {exc}",
            }
        if not reprojected.success:
            return {
                "status": "unknown",
                "stable": False,
                "reason": "re-projection failed: "
                + "; ".join(reprojected.errors or ["no detail reported"]),
            }

        # File-level normalisation only: line-ending style and the optional final
        # terminator. Interior whitespace is still byte-significant. See
        # _comparison_form for why, and for the measurement behind it.
        compared_approved = _comparison_form(approved)
        compared_current = _comparison_form(reprojected.content)
        normalisation = "line-ending style and the final end-of-file terminator only"

        if compared_current == compared_approved:
            return {
                "status": "exact",
                "stable": True,
                "preservedRegionsExact": True,
                "translatedByteIdentical": True,
                "normalisation": normalisation,
            }

        # Locate the difference so the report is actionable rather than a verdict.
        try:
            approved_regions = preserved_regions(compared_approved)
            current_regions = preserved_regions(compared_current)
        except MalformedView as exc:
            return {
                "status": "unknown",
                "stable": False,
                "reason": f"a view has malformed pass-through markers: {exc.message}",
            }

        regions_exact = approved_regions == current_regions
        approved_translated = _translated_text(compared_approved)
        current_translated = _translated_text(compared_current)
        translated_identical = approved_translated == current_translated

        detail = {
            "status": "unstable",
            "stable": False,
            "preservedRegionsExact": regions_exact,
            "translatedByteIdentical": translated_identical,
            "normalisation": normalisation,
            "approvedRegionCount": len(approved_regions),
            "reprojectedRegionCount": len(current_regions),
        }
        if not regions_exact:
            detail["reason"] = (
                "the pass-through regions of the re-projected view differ from the "
                "approved ones in content, order or multiplicity"
            )
            detail["approvedRegions"] = [m for m, _ in approved_regions]
            detail["reprojectedRegions"] = [m for m, _ in current_regions]
            return detail
        if not translated_identical:
            # The Typst fallback emits a blank separator after section commands
            # in the familiar LaTeX view; converting that view back to Typst and
            # re-projecting can omit it. Identify this exact, narrow formatting
            # difference for users, but keep the strict verdict unchanged.
            if (
                pair_obj.name == "LaTeX / Typst"
                and _latex_heading_spacing_form(compared_approved)
                == _latex_heading_spacing_form(compared_current)
            ):
                detail["formattingDifference"] = "blank_line_after_latex_heading"
                detail["reason"] = (
                    "the views differ only by one blank line immediately after a "
                    "standalone LaTeX heading; the text check remains inconclusive"
                )
                return detail
            detail["reason"] = (
                "the translated text of the re-projected view is not byte-identical "
                "to the approved view; no whitespace normalization is applied because "
                "it cannot distinguish formatting from a change inside a string or "
                "code token"
            )
            detail["approvedTranslated"] = approved_translated[:2000]
            detail["reprojectedTranslated"] = current_translated[:2000]
            return detail
        detail["reason"] = (
            "the regions and the translated text both match, but the re-projected "
            "view is not byte-identical to the approved view (marker placement or "
            "blank lines differ, beyond line-ending style and the final terminator)"
        )
        return detail


    def cmd_reproject(self, session: str) -> dict:
        """Re-project the current on-disk target, discarding the pending view."""
        record = self._session(session)
        pair_obj = self.pair(record["pair"])
        target = Path(record["targetPath"])
        self.roots.resolve_artifact(str(target))
        if not target.is_file():
            raise BridgeError("TARGET_NOT_FOUND", f"Target file is gone: {target}")
        content = read_text(target)
        digest = sha256_file(target)
        fresh = self._prime_session(
            record["_key"], pair_obj, target, content, digest, True, "reproject",
            owner=record.get("ownerSessionId"),
        )
        fresh["discardedPendingEdits"] = True
        return fresh

    def cmd_close(self, session: str, harness_session: str | None = None,
                  release_foreign: bool = False) -> dict:
        with self.store.transaction() as tx:
            key = tx.resolve_id(session)
            record = tx.get(key)
            if record is None:
                raise BridgeError("UNKNOWN_SESSION", "Unknown Rosetta session.", session=session)
            if not release_foreign:
                self._require_owner(record, harness_session)
            tx.delete(key)
        return {
            "session": key,
            "closed": True,
            "discardedPendingEdits": record["agentView"] != record["baselineView"],
        }

    def cmd_status(self, session: str | None, harness_session: str | None = None) -> dict:
        if session:
            record = self._session(session)
            self._require_owner(record, harness_session)
            entry = self._describe(record, record["_key"])
            entry["targetDrift"] = self._drift(record)
            return {"sessions": [entry]}
        return {
            "sessions": [
                self._fence_entry(record, key)
                for key, record in self._live()
                if self._owner_matches(record, harness_session)
            ]
        }

    def cmd_state(self, session: str | None, include_content: bool = False,
                  view_limit: int = 4000, harness_session: str | None = None,
                  content_owner_session: str | None = None) -> dict:
        """Snapshot for the Harness UI status strip.

        Metadata only unless ``include_content`` is set. The payload is returned
        by an HTTP route, so it must not carry artifact bodies by default: a
        status strip needs to say what is open and whether it is dirty, not to
        republish the document to every local caller.
        """
        if session:
            record = self._session(session)
            self._require_owner(record, harness_session)
            records = [(record["_key"], record)]
        else:
            records = [
                (key, record)
                for key, record in self._live()
                if self._owner_matches(record, harness_session)
            ]
        sessions = []
        content_included = False
        for key, record in records:
            entry = self._describe(
                record,
                key,
                include_digests=bool(include_content or record.get("preview") is not None),
            )
            entry["targetDrift"] = self._drift(record)
            entry["restoreAvailable"] = self.history.available(key)
            may_include_content = (
                include_content
                and (content_owner_session is None
                     or self._owner_matches(record, content_owner_session))
            )
            if may_include_content:
                entry["preview"] = record.get("preview") if entry["hasPreview"] else None
                entry["agentViewPreview"] = (record["agentView"] or "")[:view_limit]
                entry["targetPreview"] = (record.get("deliveredTarget") or "")[:view_limit]
                content_included = True
            sessions.append(entry)
        return {
            "sessions": sessions,
            "count": len(sessions),
            "contentIncluded": content_included,
        }

    def _live(self) -> list:
        """Every live session, **including** ones whose target is absent.

        Deliberately not filtered by ``targetPath.is_file()``. Two real cases
        need the session to stay visible to the host's write fence:

        * ``ros_create_artifact`` opens a session for an artifact that does not
          exist on disk yet - filtering it out left the new path completely
          unfenced, so the agent could write it directly and then deliver over
          its own edit;
        * a target deleted underneath the session must keep being fenced,
          otherwise recreating that path bypasses the fence entirely.

        The fence is about what an open session claims, not about what happens to
        exist on disk this instant.
        """
        return self.store.all_sessions()

    @staticmethod
    def _describe(record: dict, key: str, include_digests: bool = False) -> dict:
        view = record["agentView"] or ""
        dirty = view != record["baselineView"]
        convertible = has_convertible_content(view)
        preview = record.get("preview")
        try:
            target_sha256 = (
                sha256_file(Path(record["targetPath"])) if include_digests else None
            )
        except OSError:
            # A target that cannot be read is not a reason to take down the
            # whole status strip; the commit path will refuse it separately.
            target_sha256 = None
        # A preview is a *candidate* while it differs from the last delivered
        # bytes, or while the view has an edit. After a commit the stored bytes
        # equal `deliveredTarget` and the session is clean, so a delivered file
        # must not keep looking like a pending candidate forever. The previous
        # test also treated "preview differs from delivered" alone as pending,
        # which made a no-edit preview of a carried-whole document light up as
        # "preview ready" merely because the adapter trims a final newline.
        preview_candidate = preview is not None and (
            dirty or preview != record.get("deliveredTarget")
        )
        # A stored preview only authorises a delivery for an actual view edit.
        pending_preview = preview_candidate and dirty
        return {
            "session": key,
            "pair": record["pair"],
            "targetPath": record["targetPath"],
            "dirty": dirty,
            # Null means the session predates chat attribution, or the Host could
            # not name the chat. It is never filled in from the polling context.
            "ownerSessionId": record.get("ownerSessionId"),
            "hasConvertibleContent": convertible,
            "viewEdited": dirty and convertible,
            "previewCandidate": preview_candidate,
            "requestVerified": False,
            "conversion": "translatable" if convertible else "passthrough-only",
            "hasPreview": pending_preview,
            "previewSha256": (
                sha256_bytes(preview.encode("utf-8")) if preview is not None else None
            ),
            "viewSha256": sha256_bytes(view.encode("utf-8")),
            "targetSha256": target_sha256,
            "targetExisted": record["targetExisted"],
            "targetPresent": Path(record["targetPath"]).is_file(),
            "active": session_active(record),
            "protectedRegionCount": len(
                (record.get("projectionMetadata") or {}).get("protectedRegions") or []
            ),
            "projectionMetadata": _public_metadata(record.get("projectionMetadata")),
            "updatedAt": record["updatedAt"],
            "createdAt": record["createdAt"],
        }

    @staticmethod
    def _fence_entry(record: dict, key: str) -> dict:
        """The minimum the host needs to fence a path, with no artifact text."""
        return {
            "session": key,
            "pair": record["pair"],
            "targetPath": record["targetPath"],
            "dirty": record["agentView"] != record["baselineView"],
            "targetPresent": Path(record["targetPath"]).is_file(),
            "active": session_active(record),
        }



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


COMMANDS = {
    "pairs": ("cmd_pairs", []),
    "doctor": ("cmd_doctor", []),
    "open": ("cmd_open", ["session", "pair", "path", "harness_session"]),
    "create": ("cmd_create", ["session", "pair", "path", "content", "harness_session"]),
    "create_from_view": ("cmd_create_from_view", ["session", "pair", "path", "agent_view", "harness_session"]),
    "read": ("cmd_read", ["session", "harness_session"]),
    "write": ("cmd_write", ["session", "content", "harness_session"]),
    "discard_preview": ("cmd_discard_preview", ["session", "harness_session"]),
    "restore_last": ("cmd_restore_last", ["session", "harness_session"]),
    "preview": ("cmd_preview", ["session", "harness_session"]),
    "commit": ("cmd_commit", [
        "session", "harness_session", "expected_preview_sha256",
        "expected_view_sha256", "expected_target_sha256",
    ]),
    "reproject": ("cmd_reproject", ["session"]),
    "close": ("cmd_close", ["session", "harness_session", "release_foreign"]),
    "status": ("cmd_status", ["session", "harness_session"]),
    "state": ("cmd_state", [
        "session", "include_content", "view_limit", "harness_session",
        "content_owner_session",
    ]),
    "maintenance": ("cmd_maintenance", [
        "action", "apply", "older_than_days", "include_drafts",
        "include_orphan_history", "harness_session",
    ]),
}

# ---------------------------------------------------------------------------
# Shared Rosetta activity log
# ---------------------------------------------------------------------------
#
# The Studio GUI is not an MCP client. It polls one SQLite table that any
# Rosetta process appends to (`activity_window.py` calls `EventStore.since()`
# every 350 ms and never filters by `source`). Appending the same rows here is
# therefore what makes the *existing* GUI display Harness plugin activity,
# instead of building a second viewer.
#
# The schema comes from the installed `rosetta_studio.event_store.EventStore`,
# not from a copy, so the two writers cannot drift apart.

DEFAULT_EVENTS_ENV = "ROSETTA_EVENTS_DB"


def default_events_db(roots: "Roots") -> Path:
    """Where the shared activity history lives by default.

    Inside a workspace root, not in the home directory. The Harness Windows
    sandbox grants the ``CodexSandboxUsers`` group access to the workspace but
    not to ``~/.rosetta`` (that directory carries no ACE for the group), so a
    plugin subprocess may be denied there.

    Keeping the history with the workspace also means it travels with the
    artifacts it describes. Point the Studio GUI at the same file with
    ``ROSETTA_EVENTS_DB`` (see README section 10).
    """
    return roots.primary / STATE_DIR_NAME / "events.sqlite3"


# Bridge commands mapped onto the vocabulary the GUI already labels. `doctor`,
# `status`, `state` and `reproject` are intentionally absent: `state` is polled
# every few seconds by the status strip and would flood the history, and the
# others are diagnostics rather than agent steps.
RECORDED_ACTIONS = {
    "pairs": "list_pairs",
    "open": "open",
    "create": "create",
    "read": "read",
    "write": "write",
    "preview": "preview",
    "commit": "commit",
    "discard_preview": "discard_preview",
    "close": "close",
}

ACTION_DETAILS = {
    "list_pairs": "Available tool pairs listed.",
    "open": "Target opened and projected to the agent view.",
    "create": "New artifact session started.",
    "read": "Agent view read.",
    "write": "Edited agent view stored in the session.",
    "preview": "Delivery translated for preview; target file unchanged.",
    "commit": "Delivered target file written.",
    "discard_preview": "Candidate delivery discarded; the familiar-view edit remains.",
    "close": "Session closed.",
}

# The only pair names metadata mode may store. Anything else is an attempted
# name supplied by the caller and could be arbitrary text.
KNOWN_PAIR_NAMES = frozenset({
    "LaTeX / Typst",
    "Verilog / SystemVerilog",
    "PRQL / SQL",
    "Bash / Nushell (experimental)",
})

# Metadata mode stores a small, fixed vocabulary instead of free text. A
# message, note, or exception string can quote the user's source, so none of it
# survives this filter.
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_EXCEPTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def safe_error_code(value: str | None) -> str:
    """Reduce an error identifier to a fixed-shape token, never a message."""
    if isinstance(value, str):
        if _SAFE_CODE_RE.match(value):
            return value
        if _SAFE_EXCEPTION_RE.match(value):
            return "EXCEPTION_" + value.upper()
    return "UNKNOWN_ERROR"



class EventLog:
    """Append this bridge's steps to the shared Rosetta activity database.

    Logging is strictly best-effort: a read-only, locked, or unreachable
    database must never turn a successful projection or delivery into a failure.
    Every problem here is reported on stderr and then dropped, matching how the
    MCP server treats its own activity log.

    Two independent switches exist because they answer different concerns:

    * ``mode='metadata'`` keeps every step and its status but stores no
      ``agent_view`` / ``target_view`` text, so the Studio timeline still shows
      what happened without keeping a copy of the user's source.
    * ``mode='off'`` writes nothing at all.

    Neither affects projection, preview, delivery, recovery, or the status
    strip: those read the session store, never this database.

    Retention deletes only rows whose ``source`` is this plugin. A shared
    database that also holds MCP history keeps every MCP row.
    """

    def __init__(
        self,
        path: Path | None,
        mode: str = DEFAULT_ACTIVITY_MODE,
        retention_days: int = DEFAULT_ACTIVITY_RETENTION_DAYS,
        max_events: int = DEFAULT_ACTIVITY_MAX_EVENTS,
    ):
        self.path = path
        self.mode = normalise_activity_mode(mode)
        self.retention_days = clamp_int(
            retention_days, DEFAULT_ACTIVITY_RETENTION_DAYS,
            MIN_ACTIVITY_RETENTION_DAYS, MAX_ACTIVITY_RETENTION_DAYS,
        )
        self.max_events = clamp_int(
            max_events, DEFAULT_ACTIVITY_MAX_EVENTS,
            MIN_ACTIVITY_MAX_EVENTS, MAX_ACTIVITY_MAX_EVENTS,
        )
        self.store = None
        self.reason = ""
        self.retention_error = ""
        self.last_retention: dict | None = None
        if path is None or self.mode == "off":
            self.reason = "disabled by configuration"
            return
        try:
            from rosetta_studio.event_store import EventStore

            assert_clean_module(sys.modules["rosetta_studio.event_store"])
            self.store = EventStore(path)
        except Exception as exc:  # noqa: BLE001 - logging must not break delivery
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"Rosetta activity log unavailable ({path}): {self.reason}", file=sys.stderr)

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def record(self, **fields) -> None:
        # Metadata-only variants are always removed before the store sees the
        # dict; they exist only so the mode can choose between the rich and the
        # safe value. `full` keeps the rich fields exactly as before.
        metadata_detail = fields.pop("metadata_detail", None)
        metadata_session = fields.pop("metadata_session_id", None)
        metadata_pair = fields.pop("metadata_pair_name", None)
        metadata_target = fields.pop("metadata_target_path", None)
        if self.store is None:
            return
        if self.mode == "metadata":
            # The step, status, error code, and validated identifiers survive;
            # every free-text field and both source snapshots are replaced. The
            # fallback matters for a direct caller that did not build the safe
            # variant: never keep the rich `detail` just because it is missing.
            fields["agent_view"] = None
            fields["target_view"] = None
            fields["detail"] = (
                metadata_detail
                if metadata_detail is not None
                else "Step recorded; free-text diagnostics are not stored in metadata mode."
            )
            fields["session_id"] = metadata_session
            fields["pair_name"] = metadata_pair
            fields["target_path"] = metadata_target
        try:
            self.store.record(**fields)
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"Rosetta activity log write failed: {self.reason}", file=sys.stderr)
        self.enforce_retention()

    # -- retention -----------------------------------------------------------

    def _cutoff_text(self) -> str:
        return (
            datetime.now().astimezone() - timedelta(days=self.retention_days)
        ).isoformat(timespec="seconds")

    def retention_counts(self) -> dict:
        """How many of this plugin's rows retention would delete, without deleting."""
        cutoff = self._cutoff_text()
        with closing(sqlite3.connect(str(self.path), timeout=30.0)) as db:
            rows = int(
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE source = ?", (EVENT_SOURCE,)
                ).fetchone()[0]
            )
            too_old = int(
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE source = ?"
                    " AND julianday(created_at) < julianday(?)",
                    (EVENT_SOURCE, cutoff),
                ).fetchone()[0]
            )
            # Count the union, not the sum: an old row can also be over the
            # count limit, and reporting it twice would overstate the effect.
            would_delete = int(
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE source = ? AND ("
                    " julianday(created_at) < julianday(?) OR id NOT IN ("
                    "  SELECT id FROM events WHERE source = ?"
                    "  ORDER BY id DESC LIMIT ?))",
                    (EVENT_SOURCE, cutoff, EVENT_SOURCE, self.max_events),
                ).fetchone()[0]
            )
        return {
            "source": EVENT_SOURCE,
            "rows": rows,
            "olderThanCutoff": too_old,
            "overMaxEvents": max(0, rows - self.max_events),
            "wouldDelete": would_delete,
            "cutoff": cutoff,
            "retentionDays": self.retention_days,
            "maxEvents": self.max_events,
        }

    def report(self) -> dict:
        counts = None
        if self.enabled:
            try:
                counts = self.retention_counts()
            except sqlite3.Error as exc:
                self.retention_error = f"{type(exc).__name__}: {exc}"
        return {
            "path": str(self.path) if self.path else None,
            "mode": self.mode,
            "enabled": self.enabled,
            "reason": self.reason or None,
            "retentionDays": self.retention_days,
            "maxEvents": self.max_events,
            "rows": (counts or {}).get("rows"),
            "wouldPrune": (counts or {}).get("wouldDelete"),
            "retentionError": self.retention_error or None,
        }

    def enforce_retention(self) -> dict | None:
        """Delete this plugin's rows past the count or age ceiling.

        Never raises: a logging-side failure must not fail the tool call that
        triggered it. Returns the post-delete counts, or ``None`` when the log
        is disabled or could not be pruned.
        """
        if self.store is None:
            return None
        try:
            cutoff = self._cutoff_text()
            with closing(sqlite3.connect(str(self.path), timeout=30.0)) as db:
                deleted_age = db.execute(
                    "DELETE FROM events WHERE source = ?"
                    " AND julianday(created_at) < julianday(?)",
                    (EVENT_SOURCE, cutoff),
                ).rowcount
                # Keep the newest `max_events` rows for this source. `id` is the
                # insertion order, which is what the GUI's "follow latest" also
                # uses, and unlike `created_at` it is exact across time zones.
                deleted_count = db.execute(
                    "DELETE FROM events WHERE source = ? AND id NOT IN ("
                    " SELECT id FROM events WHERE source = ?"
                    " ORDER BY id DESC LIMIT ?)",
                    (EVENT_SOURCE, EVENT_SOURCE, self.max_events),
                ).rowcount
                db.commit()
            counts = self.retention_counts()
            counts["deleted"] = max(0, deleted_age) + max(0, deleted_count)
            self.last_retention = counts
            return counts
        except sqlite3.Error as exc:
            self.retention_error = f"{type(exc).__name__}: {exc}"
            print(
                f"Rosetta activity retention skipped: {self.retention_error}",
                file=sys.stderr,
            )
            return None


def _lookup_session(app: "Rosetta", session_ref: str | None) -> dict | None:
    """Best-effort session lookup for the activity row; never raises."""
    if not session_ref:
        return None
    try:
        return app.store.get(app.store.resolve_id(session_ref))
    except BridgeError:
        return None


def _event_fields(app: "Rosetta", command_name: str, command: dict,
                  result: dict | None, error: str,
                  error_code: str = "") -> dict | None:
    """Derive one shared-log row for a bridge call, mirroring the MCP server.

    Returns both the rich row used by ``full`` mode and a metadata-only variant.
    The metadata variant never carries a message, a note, or a source snapshot:
    adapter errors and round-trip notes routinely quote the artifact, so only
    the action, the status, a fixed-shape error code, and identifiers validated
    against the workspace/known-pair/session shape are kept.
    """
    action = RECORDED_ACTIONS.get(command_name)
    if action is None:
        return None

    session_id = command.get("session")
    record = _lookup_session(app, session_id)
    if record is None and isinstance(result, dict) and result.get("session"):
        record = _lookup_session(app, result["session"])
        if record is not None:
            session_id = result["session"]

    pair_name = (record or {}).get("pair") or command.get("pair")
    target_path = (record or {}).get("targetPath") or command.get("path")
    agent_view = (record or {}).get("agentView")
    target_view = (record or {}).get("preview") or (record or {}).get("deliveredTarget")
    if action == "preview" and isinstance(result, dict):
        target_view = result.get("targetContent", target_view)

    status = "error" if error else "success"
    detail = error or ACTION_DETAILS.get(action, action)
    if not error and isinstance(result, dict) and result.get("notes"):
        detail += " " + " ".join(result["notes"])

    # -- metadata-only variant ------------------------------------------------
    code = safe_error_code(error_code)
    if status == "error":
        safe_detail = (
            f"Step {action} failed with {code}. Free-text diagnostics and source "
            "snapshots are not recorded in metadata mode."
        )
    else:
        safe_detail = ACTION_DETAILS.get(action, action)
    safe_session = (
        session_id
        if isinstance(session_id, str) and _SAFE_SESSION_RE.match(session_id)
        else None
    )
    safe_pair = pair_name if isinstance(pair_name, str) and pair_name in KNOWN_PAIR_NAMES else None
    safe_target = None
    if target_path:
        try:
            # Re-run the same containment check the command performed. A path
            # supplied by a caller for a refused open is not automatically a
            # workspace path, and must not become free text in the log.
            app.roots.resolve_artifact(str(target_path))
            safe_target = str(target_path)
        except Exception:  # noqa: BLE001 - metadata mode must not fail a call
            safe_target = None

    return {
        "source": EVENT_SOURCE,
        "session_id": session_id,
        "pair_name": pair_name,
        "action": action,
        "status": status,
        "target_path": str(target_path) if target_path else None,
        "agent_view": agent_view,
        "target_view": target_view,
        "detail": detail,
        "metadata_detail": safe_detail,
        "metadata_session_id": safe_session,
        "metadata_pair_name": safe_pair,
        "metadata_target_path": safe_target,
    }


def dispatch(app: Rosetta, command: dict, events: "EventLog | None" = None) -> dict:
    name = command.get("command")
    entry = COMMANDS.get(name)
    if entry is None:
        raise BridgeError(
            "UNKNOWN_COMMAND",
            f"Unknown bridge command {name!r}.",
            known=sorted(COMMANDS),
        )
    method_name, params = entry
    kwargs = {}
    for param in params:
        kwargs[param] = command.get(param)

    # Snapshot the session first: a `close` pops it, and the row should still
    # carry the pair and target the session was about.
    if events is not None and events.enabled:
        try:
            result = getattr(app, method_name)(**kwargs)
        except BridgeError as exc:
            fields = _event_fields(app, name, command, None, exc.message, exc.code)
            if fields:
                events.record(**fields)
            raise
        except Exception as exc:  # noqa: BLE001 - record then re-raise unchanged
            fields = _event_fields(
                app, name, command, None,
                f"{type(exc).__name__}: {exc}", type(exc).__name__,
            )
            if fields:
                events.record(**fields)
            raise
        payload = {"ok": True, "schemaVersion": SCHEMA_VERSION, **result}
        fields = _event_fields(app, name, command, payload, "")
        if fields:
            events.record(**fields)
        return payload

    result = getattr(app, method_name)(**kwargs)
    return {"ok": True, "schemaVersion": SCHEMA_VERSION, **result}


def default_store(roots: Roots) -> Path:
    return roots.primary / STATE_DIR_NAME / "harness-sessions.sqlite3"


def ensure_usable_temp(roots: "Roots") -> str:
    """Point this process's temporary directory at a writable location.

    The Harness sandbox does not grant the plugin subprocess access to the
    system temp directory, so ``tempfile`` reports "No usable temporary
    directory found" for every candidate it knows. Two real failures followed
    from that, both measured before this fix:

    * ``compat_layer.adapters.eda`` allocates its sv2v scratch directory with
      ``tempfile.TemporaryDirectory()``, so projecting SystemVerilog raised
      ``FileNotFoundError`` before any translation ran.
    * ``compat_layer.adapters.typst_pipeline._typst_compiles`` wraps the same
      call in ``except Exception: return False``, so *every* Typst delivery
      reported "Output does not compile with typst" even when the artifact
      compiled cleanly - a misleading diagnostic rather than a crash.

    A workspace-local directory is writable by construction and keeps scratch
    files beside the artifacts they belong to. It is set on the environment (so
    the external converters inherit it) and directly on ``tempfile``, whose
    answer is otherwise cached after the first lookup.

    Every configured root is tried in turn, and a failure is reported as a
    ``TEMP_UNAVAILABLE`` refusal rather than letting the raw ``PermissionError``
    escape. Measured: an unwritable workspace root produced an unhandled
    traceback with no JSON on stdout, so the Host could only report "the bridge
    exited with code 1 and produced no JSON" - which hides the real cause from
    exactly the diagnostic that exists to explain it.
    """
    attempts: list[str] = []
    for root in roots.roots:
        candidate = Path(root) / STATE_DIR_NAME / "tmp"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # The probe name must be unique per process. The Host runs several
            # bridge processes at once (activation's fence seed alongside a tool
            # call), and a shared probe filename made them race: one process
            # unlinked the file the other had just written, and the loser
            # concluded the workspace was unusable.
            probe = candidate / f".rosetta-temp-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
        except OSError as exc:
            attempts.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        for var in ("TEMP", "TMP", "TMPDIR"):
            os.environ[var] = str(candidate)
        tempfile.tempdir = str(candidate)
        return str(candidate)

    raise BridgeError(
        "TEMP_UNAVAILABLE",
        "No configured workspace root can hold the scratch directory the "
        "converters need (looked for <root>/.rosetta/tmp). Nothing can be "
        "translated until one is writable.",
        attempts=attempts,
        workspaceRoots=[str(r) for r in roots.roots],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rosetta <-> DeepSeek Harness bridge")
    parser.add_argument("--allow-root", action="append", default=[], dest="allow_roots")
    parser.add_argument("--store", default=None)
    parser.add_argument(
        "--events-db",
        default=None,
        help="Shared Rosetta activity database the Studio GUI polls. Defaults to "
        "$ROSETTA_EVENTS_DB, or <workspace root>/.rosetta/events.sqlite3.",
    )
    parser.add_argument(
        "--no-events",
        action="store_true",
        help="Do not append to the shared activity database. Equivalent to "
        "--activity-mode off, and kept so older profiles keep working.",
    )
    parser.add_argument(
        "--activity-mode",
        default=DEFAULT_ACTIVITY_MODE,
        choices=list(ACTIVITY_MODES),
        help="full: record steps with agent/target snapshots; metadata: record "
        "steps and status but no source text; off: record nothing.",
    )
    parser.add_argument(
        "--activity-retention-days",
        type=int,
        default=DEFAULT_ACTIVITY_RETENTION_DAYS,
        help="Delete this plugin's activity rows older than N days (1-3650).",
    )
    parser.add_argument(
        "--activity-max-events",
        type=int,
        default=DEFAULT_ACTIVITY_MAX_EVENTS,
        help="Keep at most N activity rows written by this plugin (50-1000000).",
    )
    parser.add_argument(
        "--history-keep",
        type=int,
        default=DEFAULT_COMMIT_HISTORY_KEEP,
        help="Saved previous versions to keep per session (1-50). The newest is "
        "never removed automatically.",
    )
    parser.add_argument(
        "--history-max-files",
        type=int,
        default=DEFAULT_COMMIT_HISTORY_MAX_FILES,
        help="Global ceiling on saved previous-version entries (10-10000).",
    )
    parser.add_argument(
        "--history-max-bytes",
        type=int,
        default=DEFAULT_COMMIT_HISTORY_MAX_BYTES,
        help="Global ceiling on saved previous-version bytes (1 MiB - 2 GiB).",
    )
    parser.add_argument(
        "--stale-draft-days",
        type=int,
        default=DEFAULT_STALE_DRAFT_DAYS,
        help="Default age for the explicit maintenance cleanup of abandoned "
        "drafts (1-3650). Drafts are never removed automatically.",
    )
    parser.add_argument(
        "--require-stable-round-trip",
        action="store_true",
        help="Refuse a delivery that does not provably re-project to the approved "
        "view. The refusal happens before the target is written.",
    )
    parser.add_argument(
        "--require-preview",
        action="store_true",
        help="Refuse a commit unless the current view was previewed. The Host "
        "plugin enables this so a delivery cannot be written without the user "
        "having been shown what will be written.",
    )
    parser.add_argument("--import-root", action="append", default=[], dest="import_roots")
    parser.add_argument("--command-file", default=None)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--print-defaults", action="store_true")
    parser.add_argument(
        "--audit-imports",
        action="store_true",
        help="Include the list of imported modules in the result, so a caller "
        "can verify no benchmark/scorer/gold code was loaded.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_defaults:
        print(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "stateDirName": STATE_DIR_NAME,
                    "maxArtifactBytes": MAX_ARTIFACT_BYTES,
                    "converterEnv": CONVERTER_ENV,
                    "activityModes": list(ACTIVITY_MODES),
                    "activityRetentionDays": DEFAULT_ACTIVITY_RETENTION_DAYS,
                    "activityMaxEvents": DEFAULT_ACTIVITY_MAX_EVENTS,
                    "commitHistoryKeep": DEFAULT_COMMIT_HISTORY_KEEP,
                    "commitHistoryMaxFiles": DEFAULT_COMMIT_HISTORY_MAX_FILES,
                    "commitHistoryMaxBytes": DEFAULT_COMMIT_HISTORY_MAX_BYTES,
                }
            )
        )
        return 0

    try:
        roots = Roots(args.allow_roots)
    except BridgeError as exc:
        emit_json({"ok": False, "error": exc.as_dict()})
        return 2

    import_roots = list(args.import_roots)
    if not import_roots:
        import_roots = [str(Path(sys.executable).parent.parent)]
    provenance = restrict_import_paths(import_roots)

    if args.self_test:
        emit_json({"ok": True, "provenance": provenance})
        return 0

    store_path = (
        roots.resolve_state(args.store) if args.store else default_store(roots)
    )

    # Before anything imports an adapter or shells out to a converter: the
    # sandbox denies the system temp directory, and both depend on it. A
    # workspace with no writable scratch space is reported as a refusal, so the
    # Host sees TEMP_UNAVAILABLE instead of a bare non-zero exit.
    try:
        temp_dir = ensure_usable_temp(roots)
    except BridgeError as exc:
        emit_json({"ok": False, "error": exc.as_dict()})
        return 3

    env = dict(os.environ)
    # Defaults to a workspace-local file because the sandbox denies the plugin
    # subprocess access to ~/.rosetta. An explicit --events-db or
    # ROSETTA_EVENTS_DB still wins, so the history can be pointed anywhere the
    # running process may actually write.
    if args.no_events:
        events_path: Path | None = None
    elif args.events_db:
        events_path = Path(args.events_db)
    elif os.environ.get(DEFAULT_EVENTS_ENV):
        events_path = Path(os.environ[DEFAULT_EVENTS_ENV])
    else:
        events_path = default_events_db(roots)
    mode = "off" if args.no_events else normalise_activity_mode(args.activity_mode)
    events = EventLog(
        events_path,
        mode=mode,
        retention_days=clamp_int(
            args.activity_retention_days,
            DEFAULT_ACTIVITY_RETENTION_DAYS,
            MIN_ACTIVITY_RETENTION_DAYS,
            MAX_ACTIVITY_RETENTION_DAYS,
        ),
        max_events=clamp_int(
            args.activity_max_events,
            DEFAULT_ACTIVITY_MAX_EVENTS,
            MIN_ACTIVITY_MAX_EVENTS,
            MAX_ACTIVITY_MAX_EVENTS,
        ),
    )
    store: Store | None = None
    try:
        store = Store(store_path)
        app = Rosetta(
            roots,
            store,
            env,
            require_stable_round_trip=args.require_stable_round_trip,
            require_preview=args.require_preview,
            activity_log=events,
            commit_history_keep=args.history_keep,
            commit_history_max_files=args.history_max_files,
            commit_history_max_bytes=args.history_max_bytes,
            stale_draft_days=args.stale_draft_days,
        )
        # Before the first command runs, replay any delivery whose session update
        # did not land. This is what repairs a database left stale by a failed
        # update, without the user having to know it happened.
        recovery = app.recover_pending_commits()
        if args.command_file:
            # A Windows shell writing this file may prepend a UTF-8 BOM, and a
            # command file is data rather than code, so tolerating the BOM is
            # strictly better than refusing to start.
            raw = Path(args.command_file).read_text(encoding="utf-8-sig")
        else:
            # stdin carries UTF-8 JSON bytes. Reading it through sys.stdin's text
            # wrapper used the host's console encoding (GBK here) with
            # surrogateescape, so a valid UTF-8 payload became mojibake plus lone
            # surrogates before json.loads ever saw it. Decode the bytes
            # explicitly: the wire format is UTF-8 on every host, and strict
            # decoding refuses invalid input instead of replacing characters.
            data = sys.stdin.buffer.read()
            try:
                raw = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise BridgeError(
                    "INVALID_ENCODING",
                    "The command payload on stdin is not valid UTF-8; nothing was run.",
                    position=exc.start,
                ) from None
        command = json.loads(raw or "{}")
        if not isinstance(command, dict):
            raise BridgeError("BAD_COMMAND", "The command payload must be a JSON object.")
        result = dispatch(app, command, events)
        # Surface a repaired commit, and keep a persistent failure visible rather
        # than silently retrying forever.
        if recovery["applied"] or recovery["undecided"]:
            result["commitRecovery"] = recovery
        if args.audit_imports:
            result["moduleAudit"] = audit_modules()
        if events.enabled:
            result["activityLog"] = {
                "path": str(events.path),
                "source": EVENT_SOURCE,
                "mode": events.mode,
                "retentionDays": events.retention_days,
                "maxEvents": events.max_events,
            }
        elif events.reason:
            result["activityLog"] = {
                "path": str(events.path) if events.path else None,
                "disabled": events.reason,
                "mode": events.mode,
            }
        result["tempDir"] = temp_dir
    except BridgeError as exc:
        emit_json({"ok": False, "error": exc.as_dict()})
        return 3
    except Exception as exc:  # adapter bugs surface as explicit refusals
        emit_json(
            {
                "ok": False,
                "error": {
                    "code": "BRIDGE_CRASH",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        )
        return 4
    finally:
        if store is not None:
            store.close()

    emit_json({"provenance": provenance, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
