"""Public API of the Bash → Nushell compat layer."""
from __future__ import annotations

import re

from .bash_to_ir import bash_to_ir
from .ir import ConversionError
from .ir_to_bash import _PT_MARK_END, _PT_MARK_START, ir_to_bash
from .ir_to_nu import ir_to_nu
from .nu_to_ir import nu_to_ir


# ---------------------------------------------------------------------------
# pass-through ("原样保留"): a Nushell-only step the agent cannot express in
# Bash is written verbatim as Bash comments between two markers; the layer
# preserves it as raw Nu rather than translating (or refusing) it.
#
#   mkdir "out" && cp orders.tsv out/orders.txt
#   # @NU-PASSTHROUGH
#   # open --raw "orders.tsv" | ... | group-by {|r| ($r.column2 | str substring 0..0)}
#   # @END-NU-PASSTHROUGH
#
# Each block line is `# <nu>`; the `# ` prefix is stripped to recover the Nu.
# ---------------------------------------------------------------------------
_PT_START = re.compile(r"^\s*#\s*" + re.escape(_PT_MARK_START) + r"\s*$")
_PT_END = re.compile(r"^\s*#\s*" + re.escape(_PT_MARK_END) + r"\s*$")
_PT_LINE = re.compile(r"^\s*#\s?")
_SENTINEL = re.compile(r'print\s*"__NU_PASSTHROUGH_(\d+)__"')
_SENTINEL_TEXT = "__NU_PASSTHROUGH_"


def _extract_passthrough(bash: str):
    """Split `# @NU-PASSTHROUGH` blocks out of a Bash source.

    Returns ``(bash_with_sentinels, nu_blocks)``.  Each block is replaced by a
    ``echo __NU_PASSTHROUGH_i__`` statement that translates to a
    ``print "__NU_PASSTHROUGH_i__"`` line (a real Nu statement), which we splice
    back into after the surrounding Bash is translated.  The recovered Nu is
    stripped of trailing whitespace but otherwise verbatim.

    Two ways this used to lose code silently, both now refused:

    * **unterminated block** -- a missing ``@END`` marker made the reader
      swallow the rest of the script into the block, so ordinary Bash lines
      were delivered as raw Nu.  The deliverable still ran (``echo`` happens to
      be a Nu command), which is exactly what makes it dangerous.
    * **sentinel collision** -- the sentinel is ordinary text, so an agent that
      wrote ``echo __NU_PASSTHROUGH_0__`` itself would have that line replaced
      by a duplicate of block 0.  The marker namespace is reserved: any
      occurrence outside a block is an error rather than a duplicated block.
    """
    lines = bash.splitlines()
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _PT_START.match(lines[i]):
            nu_lines: list[str] = []
            i += 1
            while i < n and not _PT_END.match(lines[i]):
                nu_lines.append(_PT_LINE.sub("", lines[i]))
                i += 1
            if i >= n:
                raise ConversionError(
                    "unterminated `# @NU-PASSTHROUGH` block: the `# "
                    "@END-NU-PASSTHROUGH` marker is missing, so the rest of "
                    "the script would have been delivered as raw Nushell")
            out.append(f"echo __NU_PASSTHROUGH_{len(blocks)}__")
            blocks.append("\n".join(nu_lines).strip())
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


def _splice_passthrough(nu: str, blocks: list[str]) -> str:
    """Replace each ``print "__NU_PASSTHROUGH_i__"`` line with the raw Nu block.

    The sentinel line's own indentation is re-applied to every line of the
    block, so a block written inside a translated `if`/`for` body stays inside
    it and stays readable at its nesting depth.  (Nushell delimits blocks with
    braces rather than indentation, so an unindented body still ran -- it just
    made the delivered artifact look as if the preserved code had escaped its
    block.)
    """
    out: list[str] = []
    found: set[int] = set()
    for line in nu.splitlines():
        m = _SENTINEL.fullmatch(line.strip())
        if m:
            index = int(m.group(1))
            if index >= len(blocks) or index in found:
                raise ConversionError("Nushell pass-through marker is invalid or duplicated")
            found.add(index)
            indent = line[:len(line) - len(line.lstrip())]
            block = blocks[index]
            out.append("\n".join(indent + bl if bl.strip() else bl
                                 for bl in block.split("\n")))
            continue
        if _SENTINEL_TEXT in line:
            raise ConversionError("Nushell pass-through marker was altered or moved")
        out.append(line)
    if len(found) != len(blocks):
        raise ConversionError("Nushell pass-through marker was lost during translation")
    return "\n".join(out)


def bash_to_nu(bash: str) -> str:
    """Deterministically translate a Bash script to Nu 0.114.1.

    ``# @NU-PASSTHROUGH`` … ``# @END-NU-PASSTHROUGH`` blocks are preserved
    verbatim (the Nushell-only "new feature" code) rather than translated;
    everything else is a pure AST walk + IR render.  Raises ``ConversionError``
    for anything outside the controlled subset.  LLM is never consulted.
    """
    bash, blocks = _extract_passthrough(bash)
    script = bash_to_ir(bash)
    nu = ir_to_nu(script)
    if blocks:
        nu = _splice_passthrough(nu, blocks)
    return nu


def nu_to_bash(nu: str) -> str:
    """Deterministically translate Nu 0.114.1 back to Bash.

    The reverse leg of ``bash_to_nu``: the two directions are exact duals
    through one IR (``Nu -> IR -> Bash``), so the layer has a single source of
    truth for what a program means and no hand-written second translator to
    drift.  What comes back is the IR's *normal form* in Bash, not the original
    spelling — several Bash sources collapse into one IR, and a capture's
    verbatim source (``$(...)``) is canonicalised through ``ir_to_bash``.

    Pass-through is the one place the legs are duals by *not* translating.
    ``bash_to_nu`` preserves a ``# @NU-PASSTHROUGH`` block as raw Nu because raw
    Nu is not in ``ir_to_nu``'s image; here the mirror case is a statement that
    is not in the *recogniser*'s vocabulary — Nu-only work the layer can name
    but not express in Bash.  It is carried verbatim as an opaque region and
    rendered back into the marker block, so the Nu survives untouched across it
    — ``bash_to_nu(nu_to_bash(x)) == x`` on the corpus, and, where the document
    was carried whole, the document again up to the ends the block's reader
    trims; ``nu_to_bash(bash_to_nu(nu_to_bash(x))) == nu_to_bash(x)`` always, the
    carried text having been normalised to the form the block's own reader
    returns before it was stored.  Which fragments those are is reported by
    ``_nu_to_bash_opaque.py``.

    What is *never* done is a half-reading: a statement is translated whole or
    carried whole.  Raises ``ConversionError`` for a lexically broken document
    and for a step the layer can neither translate nor carry.  LLM is never
    consulted.

    This is the *projection*, so it asks the recogniser for ``carry_whole``: a
    document in which nothing classifies -- hand-written Nu built out of
    commands the layer does not know, or a file that is nothing but comments --
    is delivered as a single opaque region, which is the only Bash view that
    preserves it.  ``nu_to_ir`` on its own still refuses that document, since a
    reading is what *it* promises and there is no reading to be had.
    """
    return ir_to_bash(nu_to_ir(nu, carry_whole=True))
