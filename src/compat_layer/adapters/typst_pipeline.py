"""Typst translation pipeline with multi-engine fallback.

Pipeline:
  1. Preprocess (clean agent-generated LaTeX)
  2. Tylax (Rust AST — best quality when it works)
  3. PandocTypst (AST-based — handles nesting correctly)
  4. TypstAdapter (regex — comprehensive rule coverage)

For each document, returns the first output that compiles with `typst compile`.
Includes content-based caching to avoid re-translating.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..interface import ToolAdapter, TranslationResult

TYLAX_BIN = Path(os.environ.get("ROSETTA_TYLAX", "") or shutil.which("t2l") or "t2l")
PANDOC_BIN = Path(os.environ.get("ROSETTA_PANDOC", "") or shutil.which("pandoc") or "pandoc")


# ============================================================================
# pass-through ("原样保留"): a Typst-only construct the agent cannot express in
# LaTeX (e.g. `#show`) is written verbatim as LaTeX comments between two
# markers; the layer preserves it as raw Typst rather than translating (or
# refusing) it.
#
#   \documentclass{article}
#   % @TYPST-PASSTHROUGH
#   % #show heading: set text(fill: blue)
#   % @END-TYPST-PASSTHROUGH
#   \begin{document} ...
#
# Each block line is `% <typst>`; the `% ` prefix is stripped to recover the
# Typst. The block is replaced by a plain alphanumeric sentinel word
# (`TYPSTPASSTHROUGH<i>MARK`) that survives every engine (Tylax / Pandoc /
# regex) and the 21 post-process fixes untouched, then spliced back.
# ============================================================================

_PT_START = re.compile(r"^\s*%\s*@TYPST-PASSTHROUGH\s*$")
_PT_END = re.compile(r"^\s*%\s*@END-TYPST-PASSTHROUGH\s*$")
_PT_LINE = re.compile(r"^\s*%\s?")
_SENTINEL = re.compile(r"^\s*TYPSTPASSTHROUGH(\d+)MARK\s*$")
_ANY_SENTINEL = re.compile(r"TYPSTPASSTHROUGH(\d+)MARK")
_BULLET = re.compile(r"^\s*(?:[-+*]|\d+\.)\s+$")
_SENTINEL_TEXT = "TYPSTPASSTHROUGH"


class ConversionError(ValueError):
    """A pass-through block cannot be delivered without losing or misplacing it.

    Raised (rather than returned as a failed TranslationResult) so a caller that
    only reads ``result.content`` cannot write out a document whose preserved
    new-tool code was silently dropped, duplicated or moved.
    """


def _extract_passthrough(latex: str) -> tuple[str, list[str]]:
    """Split `% @TYPST-PASSTHROUGH` blocks out of a LaTeX source.

    Returns ``(latex_with_sentinels, typst_blocks)``. Each block is replaced by a
    ``TYPSTPASSTHROUGH<i>MARK`` line that all translation engines carry through
    verbatim, and which we splice back into after the surrounding LaTeX is
    translated. The recovered Typst is stripped of trailing whitespace but
    otherwise verbatim.

    Two ways this could lose code silently, both refused rather than delivered:

    * **unterminated block** -- a missing ``@END`` marker made the reader
      swallow the rest of the document into the block.  The swallowed LaTeX is
      then delivered as raw Typst text; because Typst renders unrecognized text
      literally and ``typst compile`` still succeeds, the run looks like a
      normal success.
    * **sentinel collision** -- the sentinel is an ordinary word, so a document
      that already contained ``TYPSTPASSTHROUGH0MARK`` would have that line
      replaced by a duplicate of block 0.  The marker namespace is reserved:
      any occurrence outside a block is an error rather than a duplicated block.
    """
    lines = latex.splitlines()
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _PT_START.match(lines[i]):
            typ_lines: list[str] = []
            i += 1
            while i < n and not _PT_END.match(lines[i]):
                typ_lines.append(_PT_LINE.sub("", lines[i]))
                i += 1
            if i >= n:
                raise ConversionError(
                    "unterminated `% @TYPST-PASSTHROUGH` block: the `% "
                    "@END-TYPST-PASSTHROUGH` marker is missing, so the rest of "
                    "the document would have been delivered as raw Typst text")
            out.append(f"TYPSTPASSTHROUGH{len(blocks)}MARK")
            blocks.append("\n".join(typ_lines).strip())
            i += 1  # consume the END marker
            continue
        if _SENTINEL_TEXT in lines[i]:
            raise ConversionError(
                f"reserved marker text {_SENTINEL_TEXT!r} appears outside a "
                f"pass-through block (line {i + 1}); it was not written by the "
                f"layer and would collide with a preserved block")
        out.append(lines[i])
        i += 1
    return "\n".join(out), blocks


def _leading_passthrough_indices(latex: str) -> set[int]:
    """Identify blocks that precede all document body content."""
    lines = latex.splitlines()
    begin = re.compile(r"^\s*\\begin\s*\{document\}")
    has_document = any(begin.match(line) for line in lines)
    in_body = not has_document
    body_started = False
    indices: set[int] = set()
    index = 0
    block_index = 0
    while index < len(lines):
        line = lines[index]
        if _PT_START.match(line):
            if not body_started:
                indices.add(block_index)
            block_index += 1
            index += 1
            while index < len(lines) and not _PT_END.match(lines[index]):
                index += 1
            index += 1
            continue
        if not in_body:
            match = begin.match(line)
            if match:
                in_body = True
                if line[match.end():].split('%', 1)[0].strip():
                    body_started = True
        elif line.strip() and not line.lstrip().startswith('%'):
            body_started = True
        index += 1
    return indices


def _splice_absorbed(line: str, blocks: list[str], found: set[int],
                     tail: list[str] | None = None) -> list[str]:
    """Put an absorbed sentinel's block back where the author wrote it.

    A sentinel is a bare word on its own LaTeX line, so a translator may
    legitimately merge it into what follows.  The common real case is a block
    written *inside a list item* -- which is where a per-item construct
    belongs::

        \\begin{itemize}
          \\item
          % @TYPST-PASSTHROUGH
          % #mark()
          % @END-TYPST-PASSTHROUGH
          Schema version bumped to 14.

    Refusing there would refuse a correctly written document, so the block goes
    back at the sentinel's own column: the first line continues the prefix
    (keeping whatever the engine put there -- a list bullet, usually), the
    remaining lines are re-indented to that column, and the trailing text is
    appended to the block's LAST line rather than emitted as a new one.

    ``tail`` is the remaining source, used for the second shape this has to
    handle: when the engine split the item into ``-  MARK`` and the item text
    as the *next* paragraph (tylax does this), a bullet-prefixed sentinel pulls
    that text back onto its own line, which is what a list item has to look
    like.  Without it the item holds only the marker and its text is silently
    dropped outside the list.
    """
    out: list[str] = []
    while True:
        m = _ANY_SENTINEL.search(line)
        if not m:
            break
        idx = int(m.group(1))
        if idx >= len(blocks):
            raise ConversionError(
                f"translated output carries sentinel {idx}, but the layer "
                f"only issued {len(blocks)} pass-through block(s)")
        if idx in found:
            raise ConversionError(
                f"pass-through sentinel {idx} appears more than once in the "
                f"translated output; splicing it would duplicate block {idx}")
        found.add(idx)
        prefix, suffix = line[:m.start()], line[m.end():]
        pad = " " * len(prefix)
        block_lines = blocks[idx].split("\n")
        out.append(prefix + block_lines[0])
        for bl in block_lines[1:]:
            out.append(pad + bl if bl.strip() else "")
        # The trailing text goes back on the block's LAST line, not on a new
        # one: a one-line block written inside a list item would otherwise emit
        # `-  #mark()` and the item text as two lines, and the text lands
        # outside the item -- the list is silently emptied even though the
        # document still compiles.
        if suffix.strip():
            out[-1] = out[-1] + suffix
        # Bullet-prefixed with no trailing text: the item's text is the next
        # non-blank line (tylax splits them).  Pull it back onto this line.
        elif tail and _BULLET.match(prefix):
            j = 0
            while j < len(tail) and not tail[j].strip():
                j += 1
            if j < len(tail):
                out[-1] = out[-1] + " " + tail[j].strip()
                del tail[:j + 1]
        line = ""
    if line:
        out.append(line)
    return out


def _drop_engine_template_preamble(typst: str) -> str:
    """Remove the document template the external engine injects on delivery.

    ``t2l`` prepends its own defaults to everything it converts::

        #set page(paper: "a4")
        #set heading(numbering: "1.")
        #set math.equation(numbering: "(1)")

    None of those are in the author's document, and they are not cosmetic: they
    force A4, number every heading and number every equation for a file that asked
    for none of it. When the author *did* set one of these, the projection moved
    that line into a ``// @TYPST-PASSTHROUGH`` block, so it arrives later in the
    file and still wins - but the injected line remains as noise, and it is what
    stopped the first two project/deliver rounds from being byte-stable.

    ``#set`` has no LaTeX counterpart, so on this path an author's own directive
    is always carried by a preserved block. Bare leading template directives
    are therefore the engine's, even when a block carries identical text.
    Dropping stops at the first non-template line, including a block marker;
    the author's directives inside those blocks retain their text and position.
    """
    lines = typst.splitlines()
    out: list[str] = []
    index = 0
    dropped = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            # Blank lines inside the leading run belong to the preamble only if
            # something after them is still dropped; they are appended and the
            # run is trimmed again below.
            out.append(lines[index])
            index += 1
            continue
        if _ENGINE_TEMPLATE_SET.match(stripped):
            dropped += 1
            index += 1
            continue
        break
    if dropped == 0:
        return typst
    # Drop the blank separator the engine left between its template and the body.
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out + lines[index:]).lstrip("\n")


def _deliver_passthrough(typst: str, blocks: list[str],
                         leading_blocks: set[int] | None = None) -> str:
    """Splice preserved blocks back into a delivery, without duplicating them.

    ``t2l`` unconditionally prepends its own default template preamble
    (``#set page(paper: "a4")`` and friends) to everything it converts. When the
    author's own ``#set`` directives are carried back verbatim as pass-through
    blocks, that preamble repeats them, so the artifact gained a duplicate copy
    of every preserved directive on each project/deliver round and grew without
    bound.

    A leading directive line whose text already appears inside a preserved block
    is therefore dropped: whatever the author wrote is still present in its
    block, so nothing is lost, and the round trip becomes idempotent. Directives
    the blocks do not carry are then handled by
    :func:`_drop_engine_template_preamble`, which removes the engine's own
    template shapes.
    """
    spliced = _splice_passthrough(typst, blocks, leading_blocks)

    preserved = {
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip()
    }

    # 1. A leading directive duplicated by a block is dropped.
    if preserved:
        lines = spliced.splitlines()
        index = 0
        dropped = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped:
                index += 1
                continue
            if stripped.startswith("#") and stripped in preserved:
                dropped += 1
                index += 1
                continue
            break
        if dropped:
            # Everything skipped was a duplicated directive, so the blank
            # separator the engine wrote between its preamble and the body goes
            # with it.
            spliced = "\n".join(lines[index:])

    # 2. Whatever template shapes remain at the top are the engine's own.
    return _drop_engine_template_preamble(spliced)



def _splice_passthrough(typst: str, blocks: list[str],
                        leading_blocks: set[int] | None = None) -> str:
    """Replace each ``TYPSTPASSTHROUGH<i>MARK`` line with the raw Typst block.

    Tylax can discard sentinels in the LaTeX preamble. A missing prefix of
    blocks known to precede body content can be restored at the top; a missing
    block elsewhere has no reliable anchor and is rejected.

    That fallback is only safe while "no anchor" really means "the engine
    dropped the line".  A sentinel the engine *absorbed into a paragraph* is
    still present in the output, just no longer alone on its line -- see
    :func:`_splice_absorbed`, which now restores it in place instead of
    refusing (refusing a correctly written document is worse than splicing it).
    Marker text that is present but NOT a sentinel shape is still an error, as
    is an index the engine produced twice (which would splice one block in two
    places).
    """
    found: set[int] = set()
    out: list[str] = []
    lines = typst.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _SENTINEL.match(line)
        if m:
            idx = int(m.group(1))
            if idx >= len(blocks):
                raise ConversionError(
                    f"translated output carries sentinel {idx}, but the layer "
                    f"only issued {len(blocks)} pass-through block(s)")
            if idx in found:
                raise ConversionError(
                    f"pass-through sentinel {idx} appears more than once in the "
                    f"translated output; splicing it would duplicate block {idx}")
            found.add(idx)
            out.append(_TYPST_MARK_START)
            out.extend(blocks[idx].splitlines())
            out.append(_TYPST_MARK_END)
            i += 1
            continue
        if _ANY_SENTINEL.search(line):
            # ``tail`` is a view of what has not been emitted yet, so an
            # absorbed sentinel can pull the item text the engine moved onto
            # the next paragraph back into its own list item.
            tail = lines[i + 1:]
            out.extend(_splice_absorbed(line, blocks, found, tail))
            i = len(lines) - len(tail)
            continue
        if _SENTINEL_TEXT in line:
            raise ConversionError(
                "reserved marker text appears in the translated output in a "
                f"shape the layer did not issue ({line.strip()[:80]!r}); its "
                f"block cannot be placed where the author wrote it")
        out.append(line)
        i += 1
    missing = [i for i in range(len(blocks)) if i not in found]
    if missing:
        allowed = leading_blocks or set()
        # A missing prefix can be placed at the document start without moving
        # it across any earlier native block. Other missing locations are not
        # reconstructible and must not be silently relocated or discarded.
        if any(i not in allowed for i in missing) or missing != list(range(len(missing))):
            raise ConversionError(
                "a target-native block lost its position during translation; "
                "refusing to relocate or discard it"
            )
        body = _drop_engine_template_preamble("\n".join(out))
        prefix = ["\n".join((_TYPST_MARK_START, blocks[i], _TYPST_MARK_END))
                  for i in missing]
        return "\n\n".join(prefix + [body])
    return "\n".join(out)


# The reverse direction (Typst → LaTeX) uses the same mechanism, mirrored: a
# Typst-only construct is marked with Typst line comments and preserved verbatim
# as a LaTeX comment block. This makes the round-trip symmetric — ``old_to_new``
# consumes the same block ``new_to_old`` emits.

_PT_START_TYP = re.compile(r"^\s*//\s*@TYPST-PASSTHROUGH\s*$")
_PT_END_TYP = re.compile(r"^\s*//\s*@END-TYPST-PASSTHROUGH\s*$")

_TYPST_MARK_START = "// @TYPST-PASSTHROUGH"
_TYPST_MARK_END = "// @END-TYPST-PASSTHROUGH"

# The document-template directives the external engine prepends to a delivery.
# Matched by shape and only in the bare leading preamble, never inside an
# author's pass-through block; see
# :func:`_drop_engine_template_preamble`.
_ENGINE_TEMPLATE_SET = re.compile(
    r"^#set\s+(?:"
    r"page\s*\(\s*paper\s*:"
    r"|heading\s*\(\s*numbering\s*:"
    r"|math\.equation\s*\(\s*numbering\s*:"
    r")"
)

# Typst-only code that has no LaTeX counterpart and that the LaTeX engines
# silently omit. Matched only at the start of a line, so a directive mentioned
# inside prose is never mistaken for one.
_TYPST_ONLY_DIRECTIVE = re.compile(
    r"^\s*#(?:set|show|let|import|include|context|bibliography|cite|figure|image)\b"
)


def _directive_end(lines: list[str], start: int) -> int:
    """Bounded delimiter scan; ambiguous/raw directive syntax is refused."""
    stack: list[str] = []
    quoted = False
    escaped = False
    comments = 0
    size = 0
    for index in range(start, min(len(lines), start + 256)):
        line = lines[index]
        size += len(line)
        if size > 65536:
            break
        code = ""
        j = 0
        while j < len(line):
            char = line[j]
            pair = line[j:j + 2]
            if comments:
                if pair == "/*":
                    comments += 1
                    j += 2
                elif pair == "*/":
                    comments -= 1
                    j += 2
                else:
                    j += 1
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                j += 1
                continue
            if pair == "//":
                break
            if pair == "/*":
                comments = 1
                j += 2
                continue
            code += char
            if char == '"':
                quoted = True
            elif char == "`":
                raise ConversionError("raw syntax inside a directive is unsupported")
            elif char in "([{":
                stack.append(char)
            elif char in ")]}":
                if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                    raise ConversionError("mismatched delimiter in Typst directive")
            j += 1
        if not stack and not quoted and not comments:
            if re.search(r"(?:=>|[:=,])\s*$", code):
                continue
            if index == start and re.fullmatch(r"\s*#(?:set|show|let|context)\s*", code):
                continue
            if index + 1 < len(lines) and re.match(r"\s*(?:[([]|=>|:|\.)", lines[index + 1]):
                raise ConversionError("ambiguous directive continuation is unsupported")
            return index + 1
    raise ConversionError("unterminated or oversized Typst directive (limit: 256 lines/64 KiB)")


_RAW_WORD = "ROSETTARAWBLOCK"
_RAW_MARKER = re.compile(re.escape(_RAW_WORD) + r"(\d+)MARK")


def _extract_raw(text: str, *, latex: bool) -> tuple[str, list[str]]:
    """Protect standalone raw blocks from engines and typography rewrites."""
    lines = text.splitlines()
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.fullmatch(r"(`{3,})([\w-]*)\s*", line)
        verbatim = latex and line.strip() == r"\begin{verbatim}"
        if verbatim or fence:
            if line != line.lstrip():
                raise ConversionError("indented raw blocks are unsupported")
            close = r"\end{verbatim}" if verbatim else fence[1]
            j = i + 1
            while j < len(lines) and lines[j] != close:
                j += 1
            if j == len(lines):
                raise ConversionError("unterminated raw block")
            body = "\n".join(lines[i + 1:j])
            if latex:
                width = max([3] + [len(m[0]) + 1 for m in re.finditer(r"`+", body)])
                delimiter = "`" * width
                block = delimiter + "\n" + body + "\n" + delimiter
            else:
                if r"\end{verbatim}" in body:
                    raise ConversionError("raw block contains a LaTeX verbatim terminator")
                block = "\\begin{verbatim}\n" + body + "\n\\end{verbatim}"
            out.append(f"{_RAW_WORD}{len(blocks)}MARK")
            blocks.append(block)
            i = j + 1
            continue
        if _RAW_WORD in line:
            raise ConversionError("reserved raw-block marker appears in source")
        if "```" in line or (latex and re.search(r"\\(?:begin|end)\{verbatim\}", line)):
            raise ConversionError("only standalone raw blocks are supported")
        out.append(line)
        i += 1
    return "\n".join(out), blocks


def _restore_raw(text: str, blocks: list[str]) -> str:
    output: list[str] = []
    cursor = 0
    found: set[int] = set()
    for match in _RAW_MARKER.finditer(text):
        index = int(match.group(1))
        if index >= len(blocks) or index in found:
            raise ConversionError("raw block was lost, duplicated or moved into another construct")
        line_start = max(text.rfind("\n", 0, match.start()),
                         text.rfind("\r", 0, match.start())) + 1
        newline_positions = [position for position in (
            text.find("\n", match.end()), text.find("\r", match.end())
        ) if position >= 0]
        line_end = min(newline_positions) if newline_positions else len(text)
        if text[line_start:match.start()].strip(" \t") or text[match.end():line_end].strip(" \t"):
            raise ConversionError("raw block was lost, duplicated or moved into another construct")
        found.add(index)
        output.extend((text[cursor:line_start], blocks[index]))
        cursor = line_end
    if len(found) != len(blocks):
        raise ConversionError("raw block was lost, duplicated or moved into another construct")
    output.append(text[cursor:])
    restored = "".join(output)
    if _RAW_WORD in restored:
        raise ConversionError("unrecognized raw-block marker in translated output")
    return restored


def _check_demo_subset(text: str, *, latex: bool) -> None:
    if latex:
        if "--" in text:
            raise ConversionError("double-hyphen commands must be in a standalone verbatim block")
    elif re.search(r"#(?:enum|list)\s*\(", text):
        raise ConversionError("function-form #enum/#list was not isolated as a complete list block")
    elif re.search(r"(?m)^[ \t]*(?:[+-]|\d+\.)[ \t]+\S", text):
        raise ConversionError("a Typst list remained outside a supported list fragment")


def _list_cache_suffix(fragments) -> str:
    if not fragments:
        return ""
    digest = hashlib.sha256()
    for fragment in sorted(fragments, key=lambda item: item.marker):
        digest.update(fragment.marker.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"tight\0" if fragment.tight else b"loose\0")
        digest.update(fragment.content.encode("utf-8"))
        digest.update(b"\0")
    return "\nROSETTA-LIST-CACHE:" + digest.hexdigest()


def _extract_passthrough_typst(typst: str) -> tuple[str, list[str]]:
    """Split ``// @TYPST-PASSTHROUGH`` blocks out of a Typst source.

    Returns ``(typst_with_sentinels, typst_blocks)``. The block lines are raw
    Typst (not comments); only the start/end markers are comments.

    An unterminated block is refused for the same reason as in the forward
    direction: the swallow would deliver the rest of the Typst document as a
    comment block in the LaTeX result, i.e. silently delete it.
    """
    lines = typst.splitlines()
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _PT_START_TYP.match(lines[i]):
            typ_lines: list[str] = []
            i += 1
            while i < n and not _PT_END_TYP.match(lines[i]):
                typ_lines.append(lines[i])
                i += 1
            if i >= n:
                raise ConversionError(
                    "unterminated `// @TYPST-PASSTHROUGH` block: the `// "
                    "@END-TYPST-PASSTHROUGH` marker is missing, so the rest of "
                    "the document would have been delivered as a LaTeX comment")
            # Tylax merges adjacent prose lines; keep opaque regions in separate paragraphs.
            out.extend(("", f"TYPSTPASSTHROUGH{len(blocks)}MARK", ""))
            blocks.append("\n".join(typ_lines).strip())
            i += 1  # consume the END marker
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), blocks


def _splice_passthrough_to_latex(latex: str, blocks: list[str]) -> str:
    """Replace each ``TYPSTPASSTHROUGH<i>MARK`` line with a LaTeX comment block.

    Unlike the forward direction there is no positional fallback to fall back
    on: a sentinel this direction fails to find means the preserved construct is
    simply gone from the delivered LaTeX, with nothing in the output to show it
    was ever written.  That is refused rather than delivered.
    """
    found: set[int] = set()
    out: list[str] = []
    for line in latex.splitlines():
        m = _SENTINEL.match(line)
        if m:
            idx = int(m.group(1))
            if idx >= len(blocks):
                raise ConversionError(
                    f"translated output carries sentinel {idx}, but the layer "
                    f"only issued {len(blocks)} pass-through block(s)")
            if idx in found:
                raise ConversionError(
                    f"pass-through sentinel {idx} appears more than once; "
                    f"splicing it would duplicate block {idx}")
            found.add(idx)
            out.append("% @TYPST-PASSTHROUGH")
            for tl in blocks[idx].splitlines():
                out.append("% " + tl)
            out.append("% @END-TYPST-PASSTHROUGH")
            continue
        out.append(line)
    lost = [i for i in range(len(blocks)) if i not in found]
    if lost:
        raise ConversionError(
            f"pass-through block(s) {lost} had no sentinel left in the "
            f"translated output, so their preserved Typst would have been "
            f"silently dropped from the delivered LaTeX")
    return "\n".join(out)


# ============================================================================
# Cache
# ============================================================================


class _Cache:
    """Content-hash → (typst_output, engine_name) cache."""

    def __init__(self, max_size: int = 512):
        self._data: dict[str, tuple[str, str]] = {}
        self._max = max_size

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]

    def get(self, content: str) -> tuple[str, str] | None:
        return self._data.get(self._hash(content))

    def put(self, content: str, typst: str, engine: str):
        key = self._hash(content)
        if key not in self._data and len(self._data) >= self._max:
            # Evict oldest
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = (typst, engine)

    def stats(self) -> dict:
        return {"size": len(self._data), "max": self._max}


# ============================================================================
# Compilation check
# ============================================================================


def _typst_compiles(typst: str, timeout: float = 15.0) -> bool:
    """Check if Typst code compiles successfully."""
    if not typst or not typst.strip():
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "test.typ"
            tf.write_text(typst, encoding="utf-8")
            r = subprocess.run(
                ["typst", "compile", str(tf)],
                capture_output=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return r.returncode == 0
    except Exception:
        return False


# ============================================================================
# Engine 1: Tylax (Rust AST)
# ============================================================================


_HEADING_LINE = re.compile(r"^(=+)(?=\s|$)")
_FENCE_LINE = re.compile(r"^\s*```")


def _fence_mask(text: str) -> list[bool]:
    """Per-line flags marking the lines that sit inside a ``` fenced block.

    The heading-ladder promotion must not touch fenced blocks: a line
    starting with ``=`` inside a code listing is literal content.
    """
    mask: list[bool] = []
    inside = False
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            mask.append(True)
            inside = not inside
        else:
            mask.append(inside)
    return mask


def _promote_headings(typst: str) -> str:
    """Lift Tylax's heading ladder so its shallowest heading is top level.

    Tylax numbers headings one level too deep when the document has no
    ``\\chapter``: ``\\section`` arrives as ``==``, so every title renders as
    ``0.1.`` rather than ``1.``.  Promote by exactly the amount that puts the
    shallowest heading at ``=`` -- a book-class document, where ``\\chapter``
    already maps to ``=``, is therefore left untouched.
    """
    lines = typst.splitlines()
    fenced = _fence_mask(typst)
    heads: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        m = _HEADING_LINE.match(line)
        if m:
            heads.append((i, len(m.group(1))))
    if not heads:
        return typst

    shift = min(level for _, level in heads) - 1
    if shift <= 0:
        return typst

    # shift < min level, so every promoted line keeps at least one "=".
    for i, level in heads:
        lines[i] = "=" * (level - shift) + lines[i][level:]
    return "\n".join(lines)


def _translate_tylax(latex: str, timeout: float = 30.0) -> str:
    """Translate via Tylax CLI. Returns Typst string or empty on failure."""
    if not TYLAX_BIN.exists():
        return ""
    try:
        args = [str(TYLAX_BIN), "--direction", "l2t"]
        if len(latex) > 200 or "\\documentclass" in latex:
            args.insert(1, "--full-document")
        r = subprocess.run(
            args,
            input=latex,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            return ""
        output = r.stdout.strip()
        if not output:
            return ""

        # Fixes for known Tylax bugs
        output = re.sub(r"(={2,4}) \*$", r"\1 ", output, flags=re.MULTILINE)
        output = re.sub(r"^/ ", "- ", output, flags=re.MULTILINE)
        output = re.sub(r"\n// Conversion warnings:.*$", "", output, flags=re.DOTALL)
        output = _promote_headings(output)
        output = _remove_tylax_paragraph_prefix(output)
        output = re.sub(r"\n{3,}", "\n\n", output)

        return output.strip()
    except Exception:
        return ""


def _remove_tylax_paragraph_prefix(output: str) -> str:
    """Remove Tylax's single formatting space at prose paragraph starts.

    Tylax emits a leading space on the first prose line after a blank line.
    That space is not present in the LaTeX input and is ignored by Typst, but
    it becomes an unwanted textual change in every delivered artifact. Limit
    the repair to a single space at a paragraph boundary; preserve additional
    indentation and all lines inside fenced code blocks.
    """
    lines = output.splitlines()
    in_fence = False
    previous_blank = True
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            previous_blank = False
            continue
        if (
            not in_fence
            and previous_blank
            and line.startswith(" ")
            and not line.startswith("  ")
            and stripped
            and (stripped[0].isalnum() or stripped.startswith(("\"", "'", "“", "‘", "(", "[", "—", "–")))
            and not stripped.startswith(("- ", "+ ", "* "))
        ):
            lines[index] = line[1:]
        previous_blank = not line.strip()
    return "\n".join(lines)


# ============================================================================
# Engine 2: PandocTypst (AST-based)
# ============================================================================


def _translate_pandoc_ast(latex: str, timeout: float = 30.0) -> str:
    """Translate via Pandoc AST (pandoc -f latex -t json → PandocTypst).

    Uses the pandoc_typst.py AST walker which handles nesting correctly.
    """
    if not PANDOC_BIN.exists():
        return ""

    try:
        from .pandoc_typst import pandoc_latex_to_typst
        result = pandoc_latex_to_typst(latex)
        if result and len(result.strip()) > 10:
            return result.strip()
    except Exception:
        pass

    return ""


def _translate_pandoc_direct(latex: str, timeout: float = 30.0) -> str:
    """Translate via Pandoc direct mode (pandoc -f latex -t typst).

    Fallback if AST mode doesn't work.
    """
    if not PANDOC_BIN.exists():
        return ""
    try:
        r = subprocess.run(
            [str(PANDOC_BIN), "-f", "latex", "-t", "typst", "--standalone"],
            input=latex,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            return ""
        output = r.stdout.strip()
        if output:
            # Apply light post-processing
            from .typst import TypstAdapter
            adapter = TypstAdapter()
            output = adapter._cleanup(output)
        return output if len(output) > 10 else ""
    except Exception:
        return ""


# ============================================================================
# Typst post-processing — fix common translation artifacts
# ============================================================================

_ACCENTS = {
    "`a": "à", "`e": "è", "`i": "ì", "`o": "ò", "`u": "ù",
    "'a": "á", "'e": "é", "'i": "í", "'o": "ó", "'u": "ú",
    '"a': "ä", '"e': "ë", '"i': "ï", '"o': "ö", '"u': "ü",
    "~a": "ã", "~n": "ñ", "~o": "õ",
    "=a": "ā", "=e": "ē", "=i": "ī", "=o": "ō", "=u": "ū",
    ".z": "ż",
}


def _extract_bibitems(latex: str) -> dict[str, str]:
    """Map citation key → raw LaTeX entry text from \\bibitem blocks."""
    bib: dict[str, str] = {}
    if "\\bibitem" not in latex and "thebibliography" not in latex:
        return bib
    for m in re.finditer(
        r"\\bibitem(?:\[[^\]]*\])?\s*\{([^}]*)\}\s*(.*?)(?=\\bibitem|\\end\{thebibliography\}|$)",
        latex,
        flags=re.DOTALL,
    ):
        key = m.group(1).strip()
        entry = m.group(2).strip()
        if key and entry:
            bib[key] = entry
    return bib


def _clean_bib_entry(entry: str) -> str:
    """Convert a raw LaTeX \\bibitem entry into readable Typst text."""
    text = re.sub(r"\s+", " ", entry).strip()
    text = text.replace("~", " ")
    # \c{c} → ç (before the generic accent rule)
    text = re.sub(r"\\c\{([a-zA-Z])\}", lambda m: {"c": "ç", "C": "Ç"}.get(m.group(1), m.group(1)), text)
    # \emph{...}/\textit{...} → _..._ ; \textbf{...} → *...* ; \texttt{...} → `...`
    text = re.sub(r"\\(?:emph|textit)\{([^{}]*)\}", r"_\1_", text)
    text = re.sub(r"\\textbf\{([^{}]*)\}", r"*\1*", text)
    text = re.sub(r"\\texttt\{([^{}]*)\}", r"`\1`", text)
    # accents: \`{e}, \'e, \"o, \~n, \=a, \.z
    text = re.sub(r"\\([`'\"~=.])\s*\{?([a-zA-Z])\}?",
                  lambda m: _ACCENTS.get(m.group(1) + m.group(2).lower(), m.group(2)), text)
    # strip any remaining LaTeX commands and stray braces
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_cite_keys(latex: str) -> set[str]:
    """Citation keys referenced by \\cite / \\citep / \\citet etc."""
    keys: set[str] = set()
    for m in re.finditer(r"\\cite[a-z]*\*?\{([^}]*)\}", latex):
        for k in m.group(1).split(","):
            k = k.strip()
            if k:
                keys.add(k)
    return keys


def _post_process_citations(typst: str, original_latex: str) -> str:
    """Recover citations that Tylax drops.

    Tylax preserves the preprocessor's ``@key`` in the body, but the
    bibliography environment is stripped, leaving orphan ``@key`` references.
    Typst cannot reference a plain-text label, so instead we escape ``@key`` to
    literal text (``\\@key``) and re-append a References section, preserving the
    citation content without relying on Typst's bibliography machinery.
    """
    if not original_latex or not typst:
        return typst
    cite_keys = _extract_cite_keys(original_latex)
    bib = _extract_bibitems(original_latex)
    if not cite_keys and not bib:
        return typst

    # Escape orphan @key references to literal text so they compile (longest
    # keys first to avoid partial-overlap replacement).
    for key in sorted(cite_keys, key=len, reverse=True):
        typst = re.sub(r"(?<!\\)@" + re.escape(key), r"\\@" + key, typst)

    # Re-append a References section if the document cites something.
    if bib and not re.search(r"(?im)^=+\s*references?", typst):
        lines = ["", "= References", ""]
        for key, entry in bib.items():
            lines.append(_clean_bib_entry(entry))
        typst = typst.rstrip() + "\n" + "\n".join(lines) + "\n"

    return typst


_MATH_SPAN = re.compile(r"(?<!\\)\$((?:[^$\\]|\\.)*)\$", re.DOTALL)


def _fix_in_math(text: str, pairs: list[tuple[str, str]]) -> str:
    """Apply command-fragment repairs inside ``$...$`` spans only.

    These repairs exist because a regex translation can drop the backslash and
    one letter of a math command, leaving ``an`` where ``\\tan`` was.  Spread
    over the whole document the same rules rewrite ordinary prose -- ``an`` is
    one of the commonest words in English, and repairing it turns every ``an``
    in the body into ``tan``.  Confining them to math keeps the repair and
    drops the collateral damage.
    """
    def repair(span: str) -> str:
        for wrong, correct in pairs:
            span = re.sub(rf"\b{re.escape(wrong)}\b", correct, span)
        return span

    return _MATH_SPAN.sub(lambda m: "$" + repair(m.group(1)) + "$", text)


def _protect_mask(text: str) -> list[bool]:
    """Characters the quote repair must leave alone.

    Raw blocks and math give `` `` `` and ``''`` a meaning of their own: a lone
    backtick delimits a raw block, and in math ``x''`` is a double prime.  A
    blanket rewrite there would damage real content instead of fixing a
    quotation, so both are masked out before the repair runs.
    """
    n = len(text)
    mask = [False] * n
    for m in _MATH_SPAN.finditer(text):
        for i in range(m.start(), m.end()):
            mask[i] = True

    pos = 0
    fenced = False
    for line in text.split("\n"):
        end = pos + len(line)
        if _FENCE_LINE.match(line):
            for i in range(pos, end):
                mask[i] = True
            fenced = not fenced
        elif fenced:
            for i in range(pos, end):
                mask[i] = True
        else:
            # Inline raw opens at a backtick that touches no other backtick and
            # runs to the next such backtick.  LaTeX's `` and '' arrive as
            # backtick pairs, which this test deliberately refuses to pair up.
            i = pos
            while i < end:
                if (text[i] == "`" and not mask[i]
                        and (i == 0 or text[i - 1] != "`")
                        and (i + 1 >= n or text[i + 1] != "`")):
                    j = i + 1
                    while j < end and text[j] != "`":
                        j += 1
                    if j < end:
                        for k in range(i, j + 1):
                            mask[k] = True
                        i = j + 1
                        continue
                i += 1
        pos = end + 1
    return mask


def _fix_quotes(typst: str) -> str:
    """Render LaTeX's double-quote ligatures as the characters they stand for.

    LaTeX writes an opening double quote as ```` `` ```` and a closing one as
    ``''``.  Left untouched they reach Typst as backticks, and a backtick there
    opens a raw block -- so the page shows a stray backtick and an apostrophe
    where the quotation should be.

    Only the doubled forms are rewritten.  A single backtick is left alone on
    purpose: in the Typst output it is just as likely to be the remains of an
    accent -- LaTeX's ``Nicol\\`o`` arrives as a bare backtick before the ``o``
    -- and rewriting that would put a quote mark inside a name.
    """
    mask = _protect_mask(typst)
    out: list[str] = []
    i, n = 0, len(typst)
    while i < n:
        if not mask[i]:
            if typst.startswith("``", i):
                out.append("“")
                i += 2
                continue
            if typst.startswith("''", i):
                out.append("”")
                i += 2
                continue
        out.append(typst[i])
        i += 1
    return "".join(out)


def _post_process_typst(typst: str, original_latex: str = "") -> str:
    """Fix common artifacts in translated Typst that prevent compilation.

    These bugs come from regex-based translation where LaTeX commands
    are partially stripped, leaving orphaned fragments.
    """
    if not typst:
        return ""

    # 0. Recover citations before rule 21 strips orphan @refs.
    typst = _post_process_citations(typst, original_latex)

    # 0b. LaTeX quote ligatures, before anything else can look like a backtick.
    typst = _fix_quotes(typst)

    # 1. Collapse excessive blank lines
    typst = re.sub(r'\n{3,}', '\n\n', typst)

    # 2. Fix orphaned Greek letter fragments
    # Use \b (word boundary) for safe matching
    greek_fixes = [
        ('heta', 'theta'), ('igma', 'sigma'), ('psilon', 'epsilon'),
        ('ambda', 'lambda'), ('lpha', 'alpha'), ('amma', 'gamma'),
        ('elta', 'delta'), ('mega', 'omega'),
    ]
    typst = _fix_in_math(typst, greek_fixes)

    # 3. Fix orphaned math fragments
    # (\frac → rac when \f prefix is stripped)
    # Use \b (word boundary) for safer matching — avoids matching inside words
    math_fixes = [
        ('rac', 'frac'), ('qrt', 'sqrt'),
    ]
    typst = _fix_in_math(typst, math_fixes)

    # 3b. More math fragments
    short_fixes = [('um', 'sum'), ('im', 'lim'), ('og', 'log'),
                   ('xp', 'exp'), ('os', 'cos'), ('an', 'tan')]
    typst = _fix_in_math(typst, short_fixes)

    # 4. Fix \label → kind: in non-figure contexts
    # "kind:" should only appear inside #figure()
    # If it appears as a standalone line, it's a mis-translated \label
    typst = re.sub(r'^\s*kind:\s*"([^"]*)"\s*$', r'<\1>', typst, flags=re.MULTILINE)

    # 5. Fix unclosed lr() delimiters
    # Count lr( and ) to find unclosed ones
    # lr( without matching ) → add )
    lines = typst.split('\n')
    for i, line in enumerate(lines):
        lr_count = line.count('lr(')
        # Simple check: if lr( exists without closing ), remove the lr(
        # This is conservative but safe
        if lr_count > 0:
            depth = 0
            fixed = []
            j = 0
            while j < len(line):
                if line[j:j+3] == 'lr(':
                    fixed.append('(')  # Replace lr( with (
                    j += 3
                    continue
                elif line[j:j+3] == 'lr(':  # Won't match but safe
                    fixed.append(line[j])
                else:
                    fixed.append(line[j])
                j += 1
            lines[i] = ''.join(fixed)
    typst = '\n'.join(lines)

    # 6. Auto-add math import if math font commands are used
    math_fonts = ['math.bb(', 'math.cal(', 'math.scr(', 'math.frak(', 'math.bold(',
                  'math.italic(', 'math.upright(', 'math.sans(', 'math.mono(']
    if any(f in typst for f in math_fonts):
        if '#import math:' not in typst:
            needed = []
            if 'math.bb(' in typst: needed.append('bb')
            if 'math.cal(' in typst: needed.append('cal')
            if 'math.scr(' in typst: needed.append('scr')
            if 'math.frak(' in typst: needed.append('frak')
            if 'math.bold(' in typst: needed.append('bold')
            if 'math.italic(' in typst: needed.append('italic')
            if 'math.upright(' in typst: needed.append('upright')
            if 'math.sans(' in typst: needed.append('sans')
            if 'math.mono(' in typst: needed.append('mono')
            if needed:
                funcs = ", ".join(needed)
                import_line = f'#import math: {funcs}\n'
                typst = import_line + typst

    # 7. Fix nested #figure() — collapse double nesting
    typst = re.sub(
        r'#figure\(\s*\n\s*caption:\s*(\[[^\]]+\]),\s*\n\s*#figure\(',
        r'#figure(\n  caption: \1,\n  ',
        typst,
    )

    # 8. Fix unclosed brackets in lr() patterns
    # Replace remaining lr(X with just X (Typst auto-sizes anyway)
    typst = re.sub(r'lr\(([^)]*)$', r'\1', typst, flags=re.MULTILINE)

    # 9. Strip stray backslash-command fragments
    typst = re.sub(r'\\(small|tiny|footnotesize|large|Large|LARGE|huge|Huge)\b', '', typst)
    typst = re.sub(r'\\(centering|raggedright|raggedleft|noindent|indent|newpage|pagebreak|clearpage|linebreak)\b', '', typst)
    typst = re.sub(r'\\[a-z]+\s*\{\s*\}', '', typst)  # empty commands

    # 10. Fix concatenated words from stripped commands
    for pair in ['xdx', 'ydy', 'zdz', 'udu', 'vdv', 'df', 'dt', 'dx', 'dy', 'dz', 'du', 'dv', 'dw', 'dr']:
        typst = re.sub(rf'\b{pair}\b', f'{pair[0]} {pair[1:]}', typst)

    # 11. Fix exp(lr( → exp(  (lr is redundant inside exp)
    typst = re.sub(r'exp\(lr\(([^)]*)\)\)', r'exp(\1)', typst)
    typst = re.sub(r'exp\(lr\(([^)]*)\)([^)])', r'exp(\1))\2', typst)

    # 12. Fix leftover LaTeX environment tags
    typst = re.sub(r'\\begin\{[a-zA-Z]+\*?\}\s*', '', typst)
    typst = re.sub(r'\\end\{[a-zA-Z]+\*?\}\s*', '', typst)

    # 13. Fix escaped # that should remain
    typst = re.sub(r'(?<!\\)\\#', '#', typst)

    # 14. Fix Tylax label issues — only strip URL/domain labels
    # Tylax creates <label> for URLs and citation keys which Typst can't resolve
    # Be CONSERVATIVE: only fix URL-like labels, keep everything else
    typst = re.sub(r'<(https?://[^>]+)>', r'\1', typst)
    typst = re.sub(r'<(www\.[^>]+)>', r'\1', typst)
    typst = re.sub(r'<(ftp://[^>]+)>', r'\1', typst)
    typst = re.sub(r'<(mailto:[^>]+)>', r'\1', typst)
    # Domain-looking labels: example.org, example.com, etc.
    # Only strip if it looks like a domain (contains dot with TLD)
    typst = re.sub(r'<([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})>', r'\1', typst)
    # Citation keys that Tylax converts to labels: <authorYear>
    # Convert to Typst citation @authorYear
    typst = re.sub(r'<([a-zA-Z][a-zA-Z0-9]{2,30}[0-9]{4}[a-z]?)>', r'@\1', typst)

    # 15. Fix Tylax citation bug: #cite(@key) → @key  (@ is invalid in code mode)
    typst = re.sub(r'#cite\(((?:@[a-zA-Z0-9:_-]+,?\s*)+)\)',
                   lambda m: ' '.join(k.strip() for k in m.group(1).split(',')), typst)
    # Fix bibliography entries: "]\n@key" → "]\n<key>" (Tylax emits citation key
    # as a reference, but it should be a label definition)
    typst = re.sub(r'\]\s+@([a-zA-Z0-9:_-]+)', r'] <\1>', typst)

    # 16a. Fix @ in #link text: Typst misparses @domain in [text] as reference
    def _escape_link_text(m):
        url = m.group(1)
        text = m.group(2).replace('@', '\\@')
        return f'#link("{url}")[{text}]'
    typst = re.sub(r'#link\("([^"]+)"\)\[([^\]]*)\]', _escape_link_text, typst)

    # 15. Fix standalone \nonumber remnants that Tylax leaves
    typst = re.sub(r'\bnonumber\b', '', typst)

    # 16. Remove stray | on lines (Tylax leaves pipe from \verb|...|)
    # Only on otherwise empty lines to avoid breaking math mode |
    typst = re.sub(r'^\s*\|\s*$', '', typst, flags=re.MULTILINE)
    # Also strip | that appears as a standalone character between words
    typst = re.sub(r'\|\s+\|', ' ', typst)
    typst = re.sub(r'(?<=\s)\|(?=\s)', ' ', typst)

    # 17. Fix stray ^ and _ outside math mode
    # Tylax sometimes leaves these from \^{} or \_{}
    # Remove isolated ^ that's followed by whitespace/punctuation (artifacts)
    typst = re.sub(r'\^(?=\s|$|[.,;:!?)\]}"])', ' ', typst)

    # 18. Fix `\*` in text mode (Tylax leaves starred section markers like === \*)
    typst = re.sub(r'(={2,4}) \*$', r'\1 ', typst, flags=re.MULTILINE)

    # 19. Fix \label{} translated as kind: outside #figure
    typst = re.sub(r'^\s*kind:\s*"([^"]*)"\s*$', r'<\1>', typst, flags=re.MULTILINE)

    # 20. Fix bibliography remnants — strip \bibliography{}, \bibliographystyle{}, \bibitem
    typst = re.sub(r'\\bibliography\s*\{[^}]*\}', '', typst)
    typst = re.sub(r'\\bibliographystyle\s*\{[^}]*\}', '', typst)

    # 21. Fix orphan references: @key with no <key> label → plain text
    # Tylax sometimes emits @ref to a label it never defined (e.g. figure
    # sub-captions). Strip the @ so it compiles instead of erroring.
    # Do NOT strip an already-escaped \@ (literal text, added by citation recovery).
    # Do NOT strip an @ that follows a word character: that one is an email
    # address (info@example.com), not a reference.  A reference is only a
    # reference where Typst would parse one, and Typst requires the @ to start
    # at a word boundary, so this also matches Typst's own reading.
    defined_labels = set(re.findall(r'<([a-zA-Z0-9:_-]+)>', typst))
    def _fix_orphan_ref(m):
        key = m.group(1)
        if key not in defined_labels:
            return key  # strip @ for orphan refs
        return m.group(0)
    typst = re.sub(r'(?<![\w\\])@([a-zA-Z0-9:_-]+)', _fix_orphan_ref, typst)

    return typst.strip()


# ============================================================================
# Engine 3: TypstAdapter (regex-based v2 rules)
# ============================================================================


def _translate_regex(latex: str) -> str:
    """Translate via TypstAdapter (comprehensive regex rules)."""
    try:
        from .typst import TypstAdapter
        adapter = TypstAdapter()
        result = adapter.old_to_new(latex)
        if result.success and len(result.content.strip()) > 10:
            return _post_process_typst(result.content.strip())
    except Exception:
        pass
    return ""


# ============================================================================
# Pipeline
# ============================================================================


def _ensure_latex_skeleton(tex: str) -> str:
    """Wrap a reverse-translated LaTeX fragment in a compilable document
    skeleton. Tylax `t2l` and the regex reverse adapter emit bare fragments
    without \\documentclass / \\begin{document}, which xelatex rejects."""
    if not tex or "\\documentclass" in tex:
        return tex
    return (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"{tex}\n"
        "\\end{document}\n"
    )


def _restore_default_matrix_delimiters(typst: str, latex: str) -> str:
    """Tylax renders Typst's default parenthesized mat as bare LaTeX matrix.

    Restore the delimiter only when matrix expressions and translated matrix
    environments have a one-to-one correspondence. Explicit Typst delimiters
    are already represented by Tylax and are left alone.
    """
    matrices = []
    for math in _MATH_SPAN.finditer(typst):
        matrices.extend(re.finditer(r'(?<![\w])mat\s*\(\s*(?P<explicit>delim\s*:)?', math.group(1)))
    if not matrices or all(m.group('explicit') for m in matrices):
        return latex
    env = re.compile(r'\\begin\{(matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix)\}(.*?)\\end\{\1\}', re.S)
    found = list(env.finditer(latex))
    if len(found) != len(matrices):
        raise ConversionError('Cannot reliably match Typst matrices to their translated LaTeX environments.')
    parts = []
    pos = 0
    for matrix, converted in zip(matrices, found):
        parts.append(latex[pos:converted.start()])
        if not matrix.group('explicit') and converted.group(1) == 'matrix':
            parts.append('\\begin{pmatrix}' + converted.group(2) + '\\end{pmatrix}')
        else:
            parts.append(converted.group(0))
        pos = converted.end()
    return ''.join(parts) + latex[pos:]


class TypstPipeline(ToolAdapter):
    """Multi-engine Typst translation with fallback.

    Engine order:
      1. Tylax (Rust AST, best quality)
      2. Pandoc AST (handles nesting/delimiters correctly)
      3. Pandoc direct (pandoc -t typst)
      4. Regex adapter (comprehensive rules, always succeeds)

    For each document, the first engine whose output compiles is used.
    All results are cached by content hash.
    """

    tool_name = "Typst"
    old_tool_name = "LaTeX"
    old_extension = ".tex"
    new_extension = ".typ"

    def __init__(self, cache_size: int = 512, verbose: bool = True):
        self._cache = _Cache(max_size=cache_size)
        self.verbose = verbose

        # Statistics
        self.stats = {
            "total": 0,
            "cache_hits": 0,
            "preprocess_fixes": 0,
            "tylax_wins": 0,
            "pandoc_ast_wins": 0,
            "pandoc_direct_wins": 0,
            "regex_wins": 0,
            "all_failed": 0,
            "compile_success": 0,
            "compile_failure": 0,
        }

    # ------------------------------------------------------------------
    # Core translation
    # ------------------------------------------------------------------

    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        """LaTeX → Typst with preprocessing + multi-engine fallback."""
        self.stats["total"] += 1

        # Extract `% @TYPST-PASSTHROUGH` blocks before caching/translation; they
        # carry raw Typst that no engine should touch. The sentinel survives the
        # whole pipeline and is spliced back on every return path below.
        leading_blocks = _leading_passthrough_indices(content)
        content, blocks = _extract_passthrough(content)
        from .typst_lists import extract_latex_lists, translate_fragments
        content, list_fragments = extract_latex_lists(content)
        # Lists are isolated before standalone raw blocks so code fences or
        # verbatim environments nested in a list are translated as part of
        # that list by Pandoc instead of being rejected as non-standalone.
        content, raw_blocks = _extract_raw(content, latex=True)
        _check_demo_subset(content, latex=True)
        cache_key = "l2t:" + content + _list_cache_suffix(list_fragments)

        # Check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            typst, engine = cached
            typst = translate_fragments(typst, list_fragments, PANDOC_BIN, "latex", "typst")
            # Always run the delivery post-pass: even when nothing was
            # preserved, the engine's own template preamble still has to go.
            typst = _deliver_passthrough(typst, blocks, leading_blocks)
            typst = _restore_raw(typst, raw_blocks)
            compiles = _typst_compiles(typst)
            self.stats["compile_success" if compiles else "compile_failure"] += 1
            return TranslationResult.ok(
                typst,
                method=f"pipeline:{engine}",
                warnings=[] if compiles else ["Output does not compile with typst"],
                engine=engine, cached=True, compiles=compiles, passthrough=len(blocks),
            )

        # Preprocess
        from .latex_preprocessor import preprocess
        pre_result = preprocess(content, verbose=False)
        latex_clean = pre_result.content
        self.stats["preprocess_fixes"] += pre_result.fix_count

        if self.verbose and pre_result.fix_count > 0:
            print(f"  [pipeline] {filepath or 'input'}: {pre_result.fix_count} preprocess fixes")

        # Try engines in order
        engines = [
            ("tylax", lambda: _translate_tylax(latex_clean)),
            ("pandoc_ast", lambda: _translate_pandoc_ast(latex_clean)),
            ("pandoc_direct", lambda: _translate_pandoc_direct(latex_clean)),
            ("regex", lambda: _translate_regex(content)),  # Original content for regex
        ]

        best_result = None
        best_engine = "none"
        anchor_errors: list[str] = []

        for engine_name, engine_fn in engines:
            try:
                typst_output = engine_fn()
            except Exception:
                typst_output = ""

            if not typst_output:
                continue

            # Apply post-processing to fix common translation artifacts
            typst_output = _post_process_typst(typst_output, content)
            if r"\begin{itemize}" in content:
                typst_output = re.sub(r"(?m)^[ \t]*-[ \t]+", "- ", typst_output)

            # Validate exactly what will be delivered, including preserved code.
            translated_lists = translate_fragments(
                typst_output, list_fragments, PANDOC_BIN, "latex", "typst"
            )
            try:
                delivered = _restore_raw(
                    _deliver_passthrough(translated_lists, blocks, leading_blocks), raw_blocks)
            except ConversionError as exc:
                anchor_errors.append(f"{engine_name}: {exc}")
                continue
            if best_result is None:
                best_result = typst_output
                best_engine = engine_name
            if _typst_compiles(delivered):
                if self.verbose:
                    print(f"  [pipeline] {filepath or 'input'}: {engine_name} COMPILES")
                self._cache.put(cache_key, typst_output, engine_name)
                self.stats[f"{engine_name}_wins"] += 1
                self.stats["compile_success"] += 1
                return TranslationResult.ok(
                    delivered,
                    method=f"pipeline:{engine_name}",
                    engine=engine_name,
                    preprocess_fixes=pre_result.fix_count,
                    compiles=True,
                    passthrough=len(blocks),
                    list_fragments=len(list_fragments),
                )

        # No engine produced compilable output — return best available
        if best_result is not None:
            self.stats["compile_failure"] += 1
            if self.verbose:
                print(f"  [pipeline] {filepath or 'input'}: {best_engine} (no compile)")
            self._cache.put(cache_key, best_result, best_engine)
            best_result = translate_fragments(
                best_result, list_fragments, PANDOC_BIN, "latex", "typst"
            )
            best_result = _restore_raw(
                _deliver_passthrough(best_result, blocks, leading_blocks), raw_blocks)
            return TranslationResult.ok(
                best_result,
                method=f"pipeline:{best_engine}",
                warnings=["Output does not compile with typst"],
                engine=best_engine,
                preprocess_fixes=pre_result.fix_count,
                compiles=False,
                passthrough=len(blocks),
                list_fragments=len(list_fragments),
            )

        # Never report untranslated LaTeX as a successful Typst delivery.
        self.stats["all_failed"] += 1
        return TranslationResult.fail(anchor_errors or ["All translation engines failed"])

    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        """Typst → LaTeX. Use Tylax reverse direction.

        Symmetric with ``old_to_new``: a Typst-only construct (e.g. ``#show``)
        written between ``// @TYPST-PASSTHROUGH`` markers is extracted before
        translation and re-inserted as a LaTeX comment block after, so the layer
        preserves it verbatim instead of silently dropping it.
        """
        content, blocks = _extract_passthrough_typst(content)
        from .typst_lists import extract_typst_lists, translate_fragments
        content, list_fragments = extract_typst_lists(content)
        # A fenced code block nested in a list belongs to the Pandoc list
        # fragment; only raw blocks outside lists use Rosetta's verbatim path.
        content, raw_blocks = _extract_raw(content, latex=False)
        _check_demo_subset(content, latex=False)
        cache_key = "t2l:" + content + _list_cache_suffix(list_fragments)

        # Cache check (keyed on the extracted content)
        cached = self._cache.get(cache_key)
        if cached is not None:
            latex, engine = cached
            latex = translate_fragments(latex, list_fragments, PANDOC_BIN, "typst", "latex")
            if blocks:
                latex = _splice_passthrough_to_latex(latex, blocks)
            latex = _restore_raw(latex, raw_blocks)
            return TranslationResult.ok(latex, method="pipeline:cached",
                                        engine=engine, cached=True, passthrough=len(blocks))

        # Try Tylax reverse
        if TYLAX_BIN.exists():
            try:
                r = subprocess.run(
                    [str(TYLAX_BIN), "--direction", "t2l"],
                    input=content,
                    capture_output=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                if r.returncode == 0 and r.stdout.strip():
                    output = _ensure_latex_skeleton(re.sub(r"\n{3,}", "\n\n", r.stdout.strip()))
                    output = _restore_default_matrix_delimiters(content, output)
                    self._cache.put(cache_key, output, "tylax_reverse")
                    output = translate_fragments(output, list_fragments, PANDOC_BIN, "typst", "latex")
                    if blocks:
                        output = _splice_passthrough_to_latex(output, blocks)
                    output = _restore_raw(output, raw_blocks)
                    return TranslationResult.ok(output, method="pipeline:tylax_reverse",
                                                engine="tylax_reverse", passthrough=len(blocks),
                                                list_fragments=len(list_fragments))
            except ConversionError:
                raise
            except Exception:
                pass

        # Fallback: TypstAdapter reverse
        try:
            from .typst import TypstAdapter
            adapter = TypstAdapter()
            result = adapter.new_to_old(content)
            if result.success:
                output = _ensure_latex_skeleton(result.content)
                self._cache.put(cache_key, output, "regex_reverse")
                output = translate_fragments(output, list_fragments, PANDOC_BIN, "typst", "latex")
                if blocks:
                    output = _splice_passthrough_to_latex(output, blocks)
                output = _restore_raw(output, raw_blocks)
                return TranslationResult.ok(output, method="regex_reverse",
                                                engine="regex_reverse", passthrough=len(blocks),
                                                list_fragments=len(list_fragments))
        except ConversionError:
            raise
        except Exception:
            pass

        return TranslationResult.fail(["Reverse translation failed"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def mark_unrepresentable(self, content: str) -> tuple[str, list[str]]:
        """Mark Typst-only directives so the projection cannot drop them.

        ``#set``, ``#show``, ``#let``, ``#import`` and friends are Typst code
        with no LaTeX counterpart. The LaTeX engines silently omit them, so an
        unguarded projection edits the author's document: the directive is
        simply gone from the delivered Typst, and nothing in either view says
        so.

        Wrapping each complete directive in ``// @TYPST-PASSTHROUGH`` makes the loss
        mode impossible. ``new_to_old`` turns the block into
        ``% @TYPST-PASSTHROUGH`` comments, and ``old_to_new`` recognizes that
        same form and restores the original Typst verbatim, so the directive
        survives a project/deliver round trip unchanged.

        Only a *line-initial* directive is marked. Typst code embedded in prose
        (``The value is #calc``) is left alone, because marking it would need
        the engines' own tokenizer to find the end of the expression, and a
        wrong guess would move the author's text into a comment.

        An already-marked block is left untouched, so this is idempotent.
        """
        lines = content.splitlines()
        out: list[str] = []
        preserved: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            fence = re.fullmatch(r"(`{3,})([\w-]*)\s*", line)
            if fence:
                end = i + 1
                while end < n and lines[end] != fence[1]:
                    end += 1
                if end == n:
                    raise ConversionError("unterminated raw block")
                out.extend(lines[i:end + 1])
                i = end + 1
                continue
            if _PT_START_TYP.match(line):
                # Copy an existing marked block through unchanged.
                out.append(line)
                i += 1
                while i < n:
                    out.append(lines[i])
                    if _PT_END_TYP.match(lines[i]):
                        i += 1
                        break
                    i += 1
                continue
            if re.fullmatch(r"[ \t]*#block\[[ \t]*", line):
                next_nonblank = i + 1
                while next_nonblank < n and not lines[next_nonblank].strip():
                    next_nonblank += 1
                if next_nonblank < n and re.match(r"[ \t]*#set\s+enum\s*\(", lines[next_nonblank]):
                    from .typst_lists import _generated_enum_block
                    end = _directive_end(lines, i)
                    if _generated_enum_block(lines, i, end):
                        # This set rule scopes the generated ordered list; keep
                        # it with the list fragment so Pandoc can translate the
                        # numbering into the familiar LaTeX list.
                        out.extend(lines[i:end])
                        i = end
                        continue
            if _TYPST_ONLY_DIRECTIVE.match(line):
                end = _directive_end(lines, i)
                preserved.append("\n".join(lines[i:end]))
                out.append(_TYPST_MARK_START)
                out.extend(lines[i:end])
                out.append(_TYPST_MARK_END)
                i = end
                continue
            out.append(line)
            i += 1
        if not preserved:
            return content, []
        return "\n".join(out), preserved

    def validate_new(self, content: str) -> bool:
        return _typst_compiles(content)

    def validate_old(self, content: str) -> bool:
        return bool(re.search(r"\\[a-zA-Z]+", content))

    def stats_summary(self) -> dict:
        total = max(self.stats["total"], 1)
        return {
            **self.stats,
            "compile_rate": f"{self.stats['compile_success']}/{self.stats['total']} "
                            f"({100*self.stats['compile_success']/total:.1f}%)",
            "engine_breakdown": {
                "tylax": self.stats["tylax_wins"],
                "pandoc_ast": self.stats["pandoc_ast_wins"],
                "pandoc_direct": self.stats["pandoc_direct_wins"],
                "regex": self.stats["regex_wins"],
                "all_failed": self.stats["all_failed"],
            },
            "cache": self._cache.stats(),
        }

    @staticmethod
    def compile_check(typst: str) -> bool:
        """Public method: check if Typst compiles."""
        return _typst_compiles(typst)
