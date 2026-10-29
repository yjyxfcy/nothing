from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .core import available_pairs, experimental_pairs
from .event_store import DEFAULT_DB, EventStore

MAX_ARTIFACT_BYTES = 2_000_000
VIEW_MARKERS = {
    "% @TYPST-PASSTHROUGH": "% @END-TYPST-PASSTHROUGH",
    "// @TYPST-PASSTHROUGH": "// @END-TYPST-PASSTHROUGH",
    "// @SV-PASSTHROUGH": "// @END-SV-PASSTHROUGH",
    "# @NU-PASSTHROUGH": "# @END-NU-PASSTHROUGH",
}


def preserved_regions(view: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Read complete pass-through blocks without interpreting their contents."""
    lines = view.splitlines(keepends=True)
    regions = []
    i = 0
    while i < len(lines):
        marker = lines[i].strip()
        if marker in VIEW_MARKERS:
            end = VIEW_MARKERS[marker]
            body = []
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                if lines[i].strip() in VIEW_MARKERS:
                    raise ToolError("Nested pass-through block in agent view.")
                body.append(lines[i])
                i += 1
            if i == len(lines):
                raise ToolError("Unterminated pass-through block in agent view.")
            regions.append((marker, tuple(body)))
        elif marker in VIEW_MARKERS.values():
            raise ToolError("Unexpected pass-through end marker in agent view.")
        i += 1
    return tuple(regions)


def retains_regions(view: str, required: tuple[tuple[str, tuple[str, ...]], ...]) -> bool:
    current = iter(preserved_regions(view))
    return all(any(region == expected for region in current) for expected in required)


@dataclass
class Session:
    pair_name: str
    target_path: Path
    agent_view: str
    target_view: str
    preview_view: str | None
    preview_notes: list[str] | None
    original_hash: str | None
    original_exists: bool
    protected_regions: tuple[tuple[str, tuple[str, ...]], ...]


class RosettaMCP:
    def __init__(self, workspace: Path, events_db: Path = DEFAULT_DB):
        self.workspace = workspace.resolve()
        self.pairs = available_pairs() | experimental_pairs()
        self.sessions: dict[str, Session] = {}
        self.events = EventStore(events_db)
        self.server = FastMCP(
            "rosetta",
            instructions=(
                "Use Rosetta for artifacts whose requested target format differs from the agent's familiar view. "
                "Call rosetta_list_pairs, then open or create a session. Read and edit only the returned agent view. "
                "Keep existing pass-through blocks unchanged. "
                "Preview delivery before committing. Commit only when the user requested the artifact be written. "
                "This server does not intercept the host's ordinary file tools. "
                "Paths are restricted to the configured workspace. Bash/Nushell is experimental."
            ),
        )
        self._register_tools()

    def _observed(self, action: str):
        def decorate(fn):
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                bound = inspect.signature(fn).bind(*args, **kwargs)
                supplied = bound.arguments
                session_id = supplied.get("session_id")
                previous = self.sessions.get(session_id)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    self._record_event(action, "error", session_id, previous,
                                       supplied, None, str(exc))
                    raise
                if isinstance(result, dict):
                    session_id = result.get("session_id", session_id)
                current = self.sessions.get(session_id) or previous
                self._record_event(action, "success", session_id, current,
                                   supplied, result, "")
                return result
            return wrapped
        return decorate

    def _record_event(self, action: str, status: str, session_id: str | None,
                      session: Session | None, supplied: dict, result, error: str) -> None:
        details = {
            "list_pairs": "Available tool pairs listed.",
            "open": "Target opened and projected to the agent view.",
            "create": "New artifact session started.",
            "read": "Agent view read.",
            "write": "Edited agent view stored in the session.",
            "preview": "Delivery translated for preview; target file unchanged.",
            "commit": "Delivered target file written.",
            "close": "Session closed.",
        }
        pair_name = session.pair_name if session else supplied.get("pair_name")
        target_path = str(session.target_path) if session else supplied.get("target_path")
        agent_view = session.agent_view if session else None
        target_view = (session.preview_view or session.target_view) if session else None
        if action == "preview" and isinstance(result, dict):
            target_view = result.get("target_content", target_view)
        detail = error if status == "error" else details[action]
        if status == "success" and isinstance(result, dict) and result.get("notes"):
            detail += " " + " ".join(result["notes"])
        try:
            self.events.record(
                source="MCP", session_id=session_id, pair_name=pair_name,
                action=action, status=status, target_path=target_path,
                agent_view=agent_view, target_view=target_view, detail=detail,
            )
        except sqlite3.Error as exc:
            print(f"Rosetta activity log unavailable: {exc}", file=sys.stderr)

    def _target_path(self, raw_path: str, extension: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError("Target path must be inside the configured workspace.") from exc
        if resolved.suffix.lower() != extension.lower():
            raise ToolError(f"This pair requires target files with extension {extension}.")
        return resolved

    def _pair(self, name: str):
        pair = self.pairs.get(name)
        if pair is None:
            raise ToolError(f"Unknown pair {name!r}. Call rosetta_list_pairs first.")
        return pair

    def _session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError("Unknown or expired Rosetta session.")
        return session

    @staticmethod
    def _notes(result) -> list[str]:
        notes = list(result.warnings)
        preserved = result.metadata.get("preserved_regions", [])
        if preserved:
            notes.append(f"{len(preserved)} target-only region(s) preserved as pass-through blocks.")
        if result.metadata.get("experimental"):
            notes.append("Experimental converter: inspect the target output and semantics.")
        if result.metadata.get("compiles") is False:
            notes.append("The converted artifact was not confirmed to compile.")
        return notes

    def _start_session(self, pair_name: str, target_path: Path, target_content: str,
                       original_hash: str | None, original_exists: bool) -> dict:
        pair = self._pair(pair_name)
        expected_preserved = len(preserved_regions(target_content))
        if not target_content.strip():
            agent_view = ""
            notes = []
        else:
            projected = pair.project(target_content)
            if not projected.success:
                raise ToolError("Projection failed: " + "; ".join(projected.errors))
            agent_view = projected.content
            if not agent_view.strip():
                raise ToolError("Projection produced an empty agent view for a nonempty target.")
            if len(agent_view.encode("utf-8")) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Agent view exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            notes = self._notes(projected)
            expected_preserved += len(projected.metadata.get("preserved_regions", []))
        session_id = uuid.uuid4().hex
        protected = preserved_regions(agent_view)
        if len(protected) < expected_preserved:
            raise ToolError("Projection dropped a target-only region; refusing to open the session.")
        self.sessions[session_id] = Session(
            pair_name=pair_name,
            target_path=target_path,
            agent_view=agent_view,
            target_view=target_content,
            preview_view=None,
            preview_notes=None,
            original_hash=original_hash,
            original_exists=original_exists,
            protected_regions=protected,
        )
        return {
            "session_id": session_id,
            "pair": pair_name,
            "target_path": str(target_path),
            "agent_extension": pair.view_extension,
            "agent_view": agent_view,
            "notes": notes,
        }

    def _register_tools(self) -> None:
        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        @self._observed("list_pairs")
        def rosetta_list_pairs() -> list[dict]:
            """List installed Rosetta pairs and their target/agent extensions."""
            return [
                {
                    "name": pair.name,
                    "target_extension": pair.target_extension,
                    "agent_extension": pair.view_extension,
                    "experimental": pair.experimental,
                }
                for pair in self.pairs.values()
            ]

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        @self._observed("open")
        def rosetta_open_artifact(pair_name: str, target_path: str) -> dict:
            """Open an existing target artifact and return a session's familiar agent view."""
            pair = self._pair(pair_name)
            path = self._target_path(target_path, pair.target_extension)
            if not path.is_file():
                raise ToolError(f"Target file does not exist: {path}")
            raw = path.read_bytes()
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Target exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ToolError("Only UTF-8 target files are supported.") from exc
            return self._start_session(
                pair_name, path, content, hashlib.sha256(raw).hexdigest(), True
            )

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        @self._observed("create")
        def rosetta_create_artifact(pair_name: str, target_path: str,
                                   initial_target_content: str) -> dict:
            """Create an in-memory session for a new target file; disk is unchanged until commit."""
            pair = self._pair(pair_name)
            path = self._target_path(target_path, pair.target_extension)
            if path.exists():
                raise ToolError(f"Refusing to create over an existing path: {path}")
            if not path.parent.is_dir():
                raise ToolError("The target's parent directory must already exist.")
            if len(initial_target_content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Target exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            return self._start_session(pair_name, path, initial_target_content, None, False)

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        @self._observed("read")
        def rosetta_read_agent_view(session_id: str) -> str:
            """Read the current familiar-format working view for a Rosetta session."""
            return self._session(session_id).agent_view

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        @self._observed("write")
        def rosetta_write_agent_view(session_id: str, content: str) -> dict:
            """Replace a session's familiar-format view with the agent's edited content."""
            session = self._session(session_id)
            if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Agent view exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            if not content.strip():
                raise ToolError("Agent view is empty; supply content. The previous view is unchanged.")
            if not retains_regions(content, session.protected_regions):
                raise ToolError("Pass-through regions must remain unchanged; edit the familiar region instead.")
            session.agent_view = content
            session.preview_view = None
            session.preview_notes = None
            return {"session_id": session_id, "characters": len(content), "stored": True}

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        @self._observed("preview")
        def rosetta_preview_delivery(session_id: str) -> dict:
            """Translate the current agent view back to target format without writing any file."""
            session = self._session(session_id)
            session.preview_view = None
            session.preview_notes = None
            if not session.agent_view.strip():
                raise ToolError("Agent view is empty; write content before previewing delivery.")
            if not retains_regions(session.agent_view, session.protected_regions):
                raise ToolError("Pass-through regions changed since the session opened.")
            result = self.pairs[session.pair_name].deliver(session.agent_view)
            if not result.success:
                raise ToolError("Delivery preview failed: " + "; ".join(result.errors))
            metadata = result.metadata
            compile_status = metadata.get("compiles", metadata.get("metadata", {}).get("compiles"))
            if compile_status is False:
                raise ToolError("Delivery did not pass the target compiler; refusing to prepare a commit.")
            if not result.content.strip():
                raise ToolError("Delivery produced an empty target for a nonempty agent view.")
            if len(result.content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Delivered artifact exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            session.preview_view = result.content
            session.preview_notes = self._notes(result)
            return {
                "session_id": session_id,
                "target_path": str(session.target_path),
                "target_content": result.content,
                "notes": session.preview_notes,
            }

        @self.server.tool(annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False
        ))
        @self._observed("commit")
        def rosetta_commit_delivery(session_id: str) -> dict:
            """Write the previewed output after checking the target has not changed."""
            session = self._session(session_id)
            if session.preview_view is None:
                raise ToolError("Preview the current agent view before committing it.")
            if not session.preview_view.strip():
                raise ToolError("Delivery preview is empty; refusing to clear the target.")
            if not retains_regions(session.agent_view, session.protected_regions):
                raise ToolError("Pass-through regions changed since the session opened.")
            path = session.target_path
            if session.original_exists:
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != session.original_hash:
                    raise ToolError("Target changed since the session opened; reopen it to avoid overwriting newer edits.")
            elif path.exists():
                raise ToolError("Target path was created by another process; refusing to overwrite it.")
            payload = session.preview_view.encode("utf-8")
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise ToolError(f"Delivered artifact exceeds the {MAX_ARTIFACT_BYTES}-byte MCP artifact limit.")
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=f".{path.name}.rosetta-", delete=False
                ) as temp:
                    temp_name = temp.name
                    temp.write(payload)
                    temp.flush()
                    os.fsync(temp.fileno())
                if session.original_exists:
                    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    if current_hash != session.original_hash:
                        raise ToolError("Target changed during commit; reopen it to avoid overwriting newer edits.")
                elif path.exists():
                    raise ToolError("Target path appeared during commit; refusing to overwrite it.")
                os.replace(temp_name, path)
            except ToolError:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
                raise
            except OSError as exc:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
                raise ToolError(f"Could not atomically write target file: {exc}") from exc
            session.original_hash = hashlib.sha256(payload).hexdigest()
            session.original_exists = True
            session.target_view = session.preview_view
            notes = session.preview_notes or []
            session.preview_view = None
            session.preview_notes = None
            return {
                "session_id": session_id,
                "written": True,
                "target_path": str(path),
                "notes": notes,
            }

        @self.server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
        @self._observed("close")
        def rosetta_close_session(session_id: str) -> dict:
            """Discard a Rosetta session without writing its working view."""
            if self.sessions.pop(session_id, None) is None:
                raise ToolError("Unknown or expired Rosetta session.")
            return {"session_id": session_id, "closed": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Rosetta MCP server (stdio)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--events-db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    app = RosettaMCP(args.workspace, args.events_db)
    app.server.run(transport="stdio")


if __name__ == "__main__":
    main()
