"""Abstract interface for tool compatibility adapters.

This module defines the contract that every new-tool developer must implement
to make their tool compatible with LLM agents trained on older tools.

The key insight: instead of training agents on every new tool (which is
fundamentally impossible due to training-data sparsity), we define a
standard interface that tool developers implement ONCE, and every agent
can then use the new tool through its familiar old-tool interface.
"""

from __future__ import annotations

import warnings as _warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# TranslationResult
# ---------------------------------------------------------------------------


@dataclass
class TranslationResult:
    """Result of a single translation between old and new tool formats.

    Attributes:
        success: Whether the translation completed without fatal errors.
        content: The translated content (empty string on failure).
        warnings: Non-fatal issues (e.g., "math expression simplified").
        errors: Fatal errors that prevented translation.
        metadata: Arbitrary extra info (e.g., round-trip confidence score).
    """

    success: bool
    content: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, warnings: list[str] | None = None, **metadata) -> "TranslationResult":
        """Factory for successful translations."""
        return cls(success=True, content=content, warnings=warnings or [], metadata=metadata)

    @classmethod
    def fail(cls, errors: list[str], warnings: list[str] | None = None) -> "TranslationResult":
        """Factory for failed translations."""
        return cls(success=False, content="", errors=errors, warnings=warnings or [])


# ---------------------------------------------------------------------------
# ToolAdapter — the core abstraction
# ---------------------------------------------------------------------------


class ToolAdapter(ABC):
    """Abstract base class for a new-tool compatibility adapter.

    This is the central abstraction of the compatibility layer. A tool
    developer implements this class ONCE to make their new tool available
    to every LLM agent that knows the corresponding old tool.

    The two core methods are:

        new_to_old(content) → TranslationResult
            Translate from the new format (human-facing) to the old format
            (agent-facing). Called when setting up the shadow directory or
            when the human modifies a real file.

        old_to_new(content) → TranslationResult
            Translate from the old format (agent-written) to the new format
            (human-facing). Called when the agent writes to a shadow file
            and the change needs to be persisted to the real file.

    Usage example::

        class MyToolAdapter(ToolAdapter):
            tool_name = "MyTool"
            old_tool_name = "OldTool"
            old_extension = ".old"
            new_extension = ".new"

            def new_to_old(self, content, filepath=""):
                # MyTool → OldTool translation
                ...

            def old_to_new(self, content, filepath=""):
                # OldTool → MyTool translation
                ...
    """

    # ------------------------------------------------------------------
    # Subclasses MUST override these properties
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """Human-readable name of the NEW tool (e.g. 'Typst', 'PRQL')."""
        ...

    @property
    @abstractmethod
    def old_tool_name(self) -> str:
        """Human-readable name of the OLD tool (e.g. 'LaTeX', 'SQL')."""
        ...

    @property
    @abstractmethod
    def old_extension(self) -> str:
        """File extension the AGENT sees (e.g. '.tex', '.sql', '.sh').

        MUST include the leading dot.
        """
        ...

    @property
    @abstractmethod
    def new_extension(self) -> str:
        """File extension for the REAL file (e.g. '.typ', '.prql', '.nu').

        MUST include the leading dot.
        """
        ...

    # ------------------------------------------------------------------
    # Subclasses MUST override these translation methods
    # ------------------------------------------------------------------

    @abstractmethod
    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate agent-written old-format content to new-format content.

        This is the critical path — called every time the agent writes a file.
        Latency matters here because it blocks the human from seeing results.

        Args:
            content: The content in the OLD format (e.g. LaTeX source).
            filepath: Optional path of the file being translated (may be
                      the shadow path or the real path; adapters should
                      treat this as a hint only).

        Returns:
            TranslationResult with the translated content in the new format.
        """
        ...

    @abstractmethod
    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate human-written new-format content to old-format content.

        This is called during shadow directory setup or when the human
        modifies a real file that needs to be visible to the agent.

        Args:
            content: The content in the NEW format (e.g. Typst source).
            filepath: Optional path hint.

        Returns:
            TranslationResult with the translated content in the old format.
        """
        ...

    # ------------------------------------------------------------------
    # Subclasses MAY override these optional methods
    # ------------------------------------------------------------------

    def validate_old(self, content: str) -> bool:
        """Check whether old-format content is syntactically valid.

        Override to provide fast validation (e.g. parse without executing).
        Default: always returns True (skip validation).
        """
        return True

    def validate_new(self, content: str) -> bool:
        """Check whether new-format content is syntactically valid.

        Override to provide fast validation.
        Default: always returns True (skip validation).
        """
        return True

    def file_filter(self, filename: str) -> bool:
        """Determine whether a file should be translated by this adapter.

        The default implementation matches by extension (case-insensitive).
        Override for more sophisticated filtering (e.g. glob patterns,
        directory-based rules, etc.).

        Args:
            filename: Just the file name (not full path), e.g. 'paper.typ'.

        Returns:
            True if this adapter should handle the file.
        """
        return filename.lower().endswith(self.new_extension.lower())

    def mark_unrepresentable(self, content: str) -> tuple[str, list[str]]:
        """Mark new-format-only content as an explicit pass-through block.

        A projection maps the new format onto the old one, so constructs that
        exist only in the new format have no old-format equivalent. They must
        not be translated, and they must not be dropped: dropping them is a
        silent edit of the artifact, which is the one outcome the layer
        promises never to produce.

        This hook runs before ``new_to_old`` during a projection. An adapter
        that knows which of its constructs are new-format-only overrides it,
        wraps each such region in its own pass-through marker pair, and returns
        the annotated content together with a human-readable description of
        what was preserved. ``old_to_new`` then carries those regions back
        verbatim, so the round trip is lossless rather than lossy.

        The default implementation marks nothing and is correct for adapters
        whose new format is fully expressible in the old one.

        Args:
            content: The content in the NEW format, as read from the target.

        Returns:
            ``(annotated_content, preserved_descriptions)``. Returning the
            input unchanged with an empty list means "nothing to preserve".
        """
        return content, []

    def get_old_path(self, new_path: str | Path) -> str:
        """Convert a real (new-format) path to a shadow (old-format) path.

        Example: 'project/paper.typ' → 'project/.shadow/paper.tex'
        """
        p = Path(new_path)
        if p.suffix.lower() == self.new_extension.lower():
            return str(p.with_suffix(self.old_extension))
        return str(p)

    def get_new_path(self, old_path: str | Path) -> str:
        """Convert a shadow (old-format) path to a real (new-format) path.

        Example: 'project/.shadow/paper.tex' → 'project/paper.typ'
        """
        p = Path(old_path)
        if p.suffix.lower() == self.old_extension.lower():
            return str(p.with_suffix(self.new_extension))
        return str(p)

    # ------------------------------------------------------------------
    # Magic methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"{self.tool_name}↔{self.old_tool_name}, "
            f"{self.old_extension}↔{self.new_extension})"
        )

    def __str__(self) -> str:
        return f"[{self.old_tool_name} → {self.tool_name}]"
