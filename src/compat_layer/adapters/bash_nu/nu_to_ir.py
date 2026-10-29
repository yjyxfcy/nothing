"""Nu 0.114.1 → shell IR recognizer.

The reverse leg of the compat layer, and the structural dual of ``ir_to_nu``:
where the renderer walks IR ops and emits Nu text, this module scans a Nu
pipeline, splits it into pipeline segments, and classifies each segment against
the vocabulary ``ir_to_nu`` can emit.  With it the layer is bidirectional over
the controlled subset (Bash ⇄ IR ⇄ Nu), so a Nushell script — hand-written by an
agent or rendered by the forward leg — can be mapped back to the one IR that
Bash and Nu both denote, without a second, hand-written source of truth.

The recovered IR is a *normal form*, not a spelling: several Bash sources collapse
into one IR, so the reverse leg is free to return any IR whose Nu image is the
text it was given.  Where two IRs share one Nu image the text cannot choose
between them; each such collision is marked ``AMBIGUITY``/``IMAGE COLLISION``
below, together with the convention this module adopts and why.

Refusal contract: emitting an IR that is subtly wrong is the one unacceptable
outcome, so anything outside the recognisable subset raises ``ConversionError``.
Classification is structural — a real scanner resolves quotes, brackets and
``|`` boundaries before anything is matched, so a ``|`` inside a closure or a
literal is never read as a pipeline boundary — and every field this module
*derives* rather than reads is checked against the renderer's own derivation
rules before it is returned.  The stream/column bookkeeping is literally the
renderer's: ``ir_to_nu._Builder`` is replayed step for step, so the two legs
cannot drift apart.  The derived fields are a source's ``kind`` (re-derived from
the path exactly as ``ir_to_bash`` does), a ``find`` listing's print order, a
group's print token list, an ``uniq_reorder`` column order and a sort's column
index — each is confirmed against the columns the pipeline's own render template
actually prints.  Where a param could only be recovered by *inventing* a value
the Nu does not carry (a ``cut`` delimiter on a record stream, an
``awk_program`` spec, a jq ``group_by`` template, the non-normal-form Bash text
of a captured ``$(...)``), the recognizer refuses rather than guesses.

Opaque regions.  The dual of ``converter``'s ``# @NU-PASSTHROUGH`` block, and of
the same kind: a fragment the recogniser cannot classify is *carried*, not
translated.  A statement whose classification fails becomes a
``Stmt("passthrough", {"text": ...})`` holding the Nu verbatim; ``ir_to_bash``
renders it back into the marker block a compat agent would have written by hand,
and ``bash_to_nu`` extracts it and splices the identical Nu through.  Nothing is
read on either side, so the round trip is exact and the fidelity contract is
untouched -- this is preservation, never a guess.

The line between carrying and refusing is drawn so that the opaque path can
never become a silent catch-all:

  * **the whole document may not be opaque**, unless the caller is projecting
    rather than reading.  A pass-through region is a *supplement* to a
    translation -- the deliverable is still a Bash program with Nu-only work
    attached.  If no statement in the document classifies, there is no Bash view
    of it at all and the first failure is raised unchanged, so input that is
    merely malformed is refused rather than wrapped wholesale.  The one
    exception is ``carry_whole``, which ``converter.nu_to_bash`` sets: a
    *projection* has to preserve the document, and a hand-written document that
    uses nothing the layer knows has no other Bash view than a carried one.  A
    reading (``nu_to_ir`` by default) still refuses it.
  * **lexical failures are never opaque.**  ``_statements`` runs first and keeps
    its refusals: an unbalanced bracket or an unterminated string is a broken
    *document*, not an unrecognised step, and is not carried.  A Nu comment is
    neither -- it is ordinary Nu -- so it is carried with the statement it
    touches (see ``_statements``).
  * **classification is still all-or-nothing per statement.**  A fragment that
    fails is carried *whole*; there is no partial reading of a statement, so a
    half-understood pipeline is never rendered from the half that parsed.

Arguments are read in either spelling.  ``mkdir out`` and ``mkdir "out"`` are one
Nushell program, so every position where ``ir_to_nu`` writes a quoted path reads
a bare word too and stores the content rather than the spelling (``_ARG``); the
recovered op is the same op either way.  Everything else still matches the
renderer's own shapes, so the recogniser stays as wide as -- and no wider than --
the meaning it can recover.

What the carried fragments *are* is a measurement rather than an implementation
detail -- ``_nu_to_bash_opaque.py`` reports the set per task, and
``_nu_to_ir_generality.py`` scores the hand-written documents that motivated the
widening.
"""
from __future__ import annotations

import re

from .bash_to_ir import _file_ext_kind
from .ir import ConversionError, Op, Pipeline, Script, Stmt
from .ir_to_bash import _SHAPE_KEEPING, ir_to_bash
from .ir_to_nu import _Builder


# =====================================================================
# scanner
#
# A recogniser that only worked on 25 remembered strings would be worthless, so
# the text is first tokenised with a real scanner: a quoted literal in either
# style becomes one token, bracket groups nest, and `|` is a boundary only where
# it is one.  Matching then runs on a *canonical* rendering of those tokens
# (`_ser_tok`), which is whitespace-blind and — except for one literal whose
# spelling is semantic (`_m_each_split`) — quote-blind.
# =====================================================================

_OPEN = {"(": ")", "{": "}", "[": "]"}
_CLOSE = {v: k for k, v in _OPEN.items()}
_BREAK = frozenset("|,;(){}[]")

_JOIN_TSV = "str join (char nl)"   # the tsv/csv re-join before `from tsv -n`


class _Tok:
    """One lexical token: a bare word, a quoted literal, a bracket group, or a
    ``,``/``;``/``|`` separator."""

    __slots__ = ("kind", "text", "quote", "prefix", "items", "pos", "end")

    def __init__(self, kind, text="", quote="", prefix="", items=None,
                 pos=0, end=0):
        self.kind = kind        # word | str | group | op
        self.text = text        # word/op text, or a group's opening bracket
        self.quote = quote      # "'" or '"' on a string token
        self.prefix = prefix    # "$" on an interpolated string
        self.items = items      # group children
        self.pos = pos          # offset of this token in the source text
        self.end = end          # offset just past it (group: past the closer)


def _read_str(s: str, i: int, q: str) -> tuple:
    """Read the quoted literal whose opening quote is ``s[i]``.

    An escape pair survives verbatim (``ir_to_nu._q`` escapes only ``"``), so
    the recovered text is unescaped by exactly the inverse of the renderer.
    """
    start = i
    i += 1
    buf: list = []
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            buf.append(c)
            buf.append(s[i + 1])
            i += 2
            continue
        if c == q:
            return "".join(buf), i + 1, start
        buf.append(c)
        i += 1
    raise ConversionError(f"unterminated string literal near {s[-24:]!r}")


def _tokenize(s: str, i: int = 0, closer: str | None = None) -> tuple:
    toks: list = []
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if closer is not None and c == closer:
            return toks, i + 1
        if c in _BREAK:
            if c in _CLOSE:
                raise ConversionError(f"unbalanced {c!r} in Nu text")
            if c in _OPEN:
                inner = i + 1
                sub, j = _tokenize(s, inner, _OPEN[c])
                toks.append(_Tok("group", text=c, items=sub, pos=inner,
                                 end=j - 1))
                i = j
                continue
            toks.append(_Tok("op", text=c, pos=i, end=i + 1))
            i += 1
            continue
        if c in "'\"":
            text, j, start = _read_str(s, i, c)
            toks.append(_Tok("str", text=text, quote=c, pos=start, end=j))
            i = j
            continue
        if c == "$" and i + 1 < n and s[i + 1] == '"':
            text, j, _ = _read_str(s, i + 1, '"')
            toks.append(_Tok("str", text=text, quote='"', prefix="$",
                             pos=i, end=j))
            i = j
            continue
        j = i
        while j < n and not s[j].isspace() and s[j] not in _BREAK \
                and s[j] not in "'\"":
            j += 1
        if j == i:
            raise ConversionError(f"cannot tokenize Nu text at {s[i:i + 24]!r}")
        toks.append(_Tok("word", text=s[i:j], pos=i, end=j))
        i = j
    if closer is not None:
        raise ConversionError(f"unterminated {closer!r} group in Nu text")
    return toks, i


def _ser_group(t: _Tok) -> str:
    """A bracket group in the renderer's own spelling.

    A closure is written ``{|k,v| body}`` — the parameter list welded to its two
    delimiters — everywhere ``ir_to_nu`` emits one, so the canonical form welds
    it too and every pattern below can be written the way the renderer writes.
    """
    items = t.items
    if items and items[0].kind == "op" and items[0].text == "|":
        end = next((k for k in range(1, len(items))
                    if items[k].kind == "op" and items[k].text == "|"), None)
        if end is not None:
            params = ",".join(_ser_toks([x]) for x in items[1:end]
                              if x.kind in ("word", "str"))
            body = _ser_toks(items[end + 1:])
            return (t.text + "|" + params + "|" + (" " + body if body else "")
                    + _OPEN[t.text])
    return t.text + _ser_toks(items) + _OPEN[t.text]


def _ser_tok(t: _Tok) -> str:
    r"""A token in canonical spelling.

    Literals are canonicalised to double quotes, because Nu's two quote styles
    denote the same string — with one exception: a literal holding a single
    space keeps the style it was written in.  That is the *one* literal whose
    spelling separates two ops (awk's fixed ``' '`` field split from a `cut`
    delimiter; see `_m_each_split`).  Everything else matches on content,
    never on spelling.
    """
    if t.kind == "str":
        if t.text == " ":
            return t.prefix + t.quote + t.text + t.quote
        return t.prefix + '"' + t.text + '"'
    if t.kind == "group":
        return _ser_group(t)
    return t.text


def _ser_toks(toks: list) -> str:
    """Canonical single-space rendering of a token run.

    Matching happens on this form, so collapsing or expanding whitespace in the
    input cannot change the recovery (whitespace *inside* a literal is part of
    that literal's token and is preserved).
    """
    out = ""
    for t in toks:
        s = _ser_tok(t)
        if out and s not in (",", ";") and out[-1] not in "([{":
            out += " "
        out += s
    return out


def _ser(seg: list) -> str:
    return _ser_toks(seg)


def _segments(toks: list) -> list:
    segs: list = []
    cur: list = []
    for t in toks:
        if t.kind == "op" and t.text == "|":
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    segs.append(cur)
    if any(not s for s in segs):
        raise ConversionError("empty pipeline segment (a stray `|`)")
    return segs


def _starts_comment(text: str, i: int) -> bool:
    """True when ``text[i]`` is a ``#`` Nushell reads as a *comment*.

    Nu starts a comment where a token can start — after whitespace, after a
    statement separator, or at the head of the file; ``mkdir d#1`` makes a
    directory called ``d#1`` rather than commenting anything out (verified
    against nu 0.114.1, mid-word ``#`` and a ``#`` after ``)`` included).  The
    rule is read the conservative way round on purpose: a ``#`` mistaken for
    code leaves the statement whole, so it is carried or refused — loud — while
    a ``#`` mistaken for a comment would cut a statement short, and a truncated
    pipeline reads from the half that parsed.
    """
    return i == 0 or text[i - 1].isspace() or text[i - 1] == ";"


def _lex(chunk: str):
    """Refuse a chunk that does not even tokenise.

    An unterminated literal or an unbalanced bracket is *damage*, not an
    unrecognised step, and the two are not the same outcome: a step the layer
    cannot read is carried verbatim, while a broken document is refused -- by
    the recogniser and by the projection alike (see the module docstring).
    Tokenising here is what keeps that split true, since the tokeniser is where
    bracket and quote balance is actually decided.
    """
    _tokenize(chunk)


def _is_comment(chunk: str) -> bool:
    """True for a chunk that is *only* a comment line.

    A chunk begins just after a depth-0 newline, so bracket depth is 0 at its
    first character: a ``#`` there is a comment running to that line's end, and
    a comment line cannot open a bracket, so the chunk is exactly that line.
    """
    return chunk.lstrip().startswith("#")


def _statements(text: str) -> list:
    """Split Nu source into top-level statements.

    Nushell separates statements by newlines and delimits blocks with braces, so
    a newline at bracket depth 0 ends a statement — except a continuation line
    opening with ``|`` (``ir_to_nu._for_to_nu`` writes its trailing ``| ignore``
    on a line of its own).

    A comment is not a lexical failure.  It is not stripped either: it stays in
    the text of the statement it touches, which is then outside the image of
    ``ir_to_nu`` wherever it sits, so it is *carried* — verbatim, by the same
    path every other unclassifiable fragment takes (see the module docstring).
    A comment on a line of its own is a statement of its own, and a run of them
    becomes one opaque region.  Stripping a comment out of a statement instead
    would split a multi-line pipeline across two statements, and a pipeline
    rendered from the stages before the cut is a silently wrong reading.
    """
    chunks: list = []
    cur: list = []
    depth = 0
    q: str | None = None
    i = 0
    while i < len(text):
        c = text[i]
        if q is not None:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
        elif c == "#" and _starts_comment(text, i):
            # nothing inside a comment is code, so its brackets and quotes are
            # not counted and its newline ends it
            j = text.find("\n", i)
            j = len(text) if j == -1 else j
            cur.append(text[i:j])
            i = j
            continue
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "\n" and depth == 0:
            chunks.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    chunks.append("".join(cur))

    out: list = []
    for chunk in chunks:
        if not chunk.strip():
            # A blank line carries no statement -- `Stmt("nop")` renders to the
            # empty string, so reading one would be an image collision rather
            # than evidence of one -- but it is still *text*, and it rides on the
            # statement before it: a carried region then comes back with the
            # document's own line breaks instead of as a normalised run of
            # lines.  (A blank line at a region's head or tail goes with the
            # region's ends, which the block's own reader trims anyway.)
            if out:
                out[-1] = out[-1] + "\n"
            continue
        _lex(chunk)
        if out and chunk.lstrip().startswith("|"):
            # A continuation line belongs to the statement it continues, and
            # that is not always the *previous* entry: a comment between two
            # stages is carried in a region of its own, and the pipeline must
            # still come out whole (`| lines` / `# note` / `| length`).  Joining
            # it to the comment instead would truncate the pipeline.  The join
            # keeps the line break the author wrote, so a carried pipeline is
            # byte-for-byte the pipeline that went in.
            k = len(out) - 1
            while k >= 0 and _is_comment(out[k]):
                k -= 1
            if k < 0:
                raise ConversionError("a `|` continuation with no statement to "
                                      "continue")
            out[k] = out[k] + "\n" + chunk
            continue
        out.append(chunk)
    return out


# =====================================================================
# accessors and shared pattern fragments
# =====================================================================

def _unq(s: str) -> str:
    """Undo the renderer's quoting of a double-quoted literal (``_q`` escapes
    only ``"``)."""
    return s.replace('\\"', '"')


# One shell argument, as Nu spells it: a quoted literal (either style — the
# canonical form `_ser_tok` matches on is always double-quoted) or a bare word.
#
# The two spellings are *one* argument: `mkdir out` and `mkdir "out"` are the
# same Nushell program, and a human writing the first is not writing anything
# unusual.  So every position where ``ir_to_nu`` writes a quoted path reads both
# and stores the content, never the spelling (`_argval`) — the recovered op is
# the same op either way, and the spelling is a normal form like any other.
# The renderer keeps quoting what it quotes; this is the reverse leg being
# *wider* than the image, which is what lets a hand-written document be read at
# all.  A bare word cannot carry a space or a quote, so `mkdir "my dir"` has
# exactly one spelling and no ambiguity is introduced.
#
# A bare argument may not *open* with `-`: in Nu's argument position that begins
# a flag, not a path (`save -f -a` is `save` with two flags, and reading `-a` as
# a destination would be a mistranslation of a program that does not even run).
# The flags the layer does read are the ones `ir_to_nu` writes, matched
# explicitly ahead of the argument (`rm -r PATH`, `save [-f|-a] PATH`).
_ARG = r'(?:"(?:[^"\\]|\\.)*"|[^\s"|-][^\s"|]*)'


def _argval(text: str) -> str:
    """The content of one argument, whichever way it was spelled."""
    if text.startswith('"'):
        return _unq(text[1:-1])
    return text


# The bare file-operation commands `ir_to_nu` writes as a line of their own.
# A step led by one of these is a file operation the layer failed to *read*
# rather than a pipeline missing its data source, and is diagnosed as such.
_FILE_OPS = ("mkdir", "cp", "mv", "rm", "touch")


def _word(seg: list, i: int, text: str) -> bool:
    return i < len(seg) and seg[i].kind == "word" and seg[i].text == text


def _join_nl(seg: list) -> bool:
    r"""``str join "\n"`` — the stdout re-join, in either quote style (a
    single-quoted Nu literal holds the same two characters, so the style is not
    semantic here, unlike the `split row` case below)."""
    return (len(seg) == 3
            and seg[0].kind == "word" and seg[0].text == "str"
            and seg[1].kind == "word" and seg[1].text == "join"
            and seg[2].kind == "str" and seg[2].text == "\\n")


def _join_empty(seg: list) -> bool:
    r"""``str join ""`` — the redirect/`tee` re-join, where every line already
    carries the newline that a file gets and stdout does not need."""
    return (len(seg) == 3
            and seg[0].kind == "word" and seg[0].text == "str"
            and seg[1].kind == "word" and seg[1].text == "join"
            and seg[2].kind == "str" and seg[2].text == "")


def _nu_to_text(body: str) -> str:
    """A ``$"…"`` body back to the Bash text it was rendered from.

    The inverse of ``ir_to_nu._text_to_nu``: ``($var)`` holes come back as
    ``$var`` and the escapes the renderer wrote for a tab, a newline, a
    backslash and a quote are decoded.  A ``(`` that is not a hole, or a bare
    ``$``, is not something the renderer emits, so it refuses rather than
    guessing what it meant.
    """
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "(":
            end = body.find(")", i)
            hole = body[i + 1:end] if end != -1 else ""
            if end == -1 or not re.fullmatch(r"\$\w+", hole):
                raise ConversionError(
                    f"an interpolation that is not a `$variable` has no Bash "
                    f"text: {body[i:i + 24]!r}")
            out.append(hole)
            i = end + 1
            continue
        if c == "\\":
            if i + 1 >= n:
                raise ConversionError(f"dangling escape in {body!r}")
            e = body[i + 1]
            if e not in ("\\", '"', "t", "n"):
                raise ConversionError(f"unsupported escape \\{e} in {body!r}")
            out.append({"\\": "\\", '"': '"', "t": "\t", "n": "\n"}[e])
            i += 2
            continue
        if c == "$":
            raise ConversionError(
                f"a bare `$` in a rendered string is not a `$variable` hole: "
                f"{body[i:i + 24]!r}")
        out.append(c)
        i += 1
    return "".join(out)


def _head_word(toks: list) -> str | None:
    """The statement's leading word, when it has one (``let``/``print``/``if``).
    """
    return toks[0].text if toks and toks[0].kind == "word" else None


def _literal(v: _Tok) -> str | None:
    """The Bash text behind a rendered literal param.

    ``ir_to_nu._val`` strips quotes and re-quotes anything non-numeric, so a bare
    number is a number and every other literal's *content* is the param —
    except an interpolated one, which ``_val`` never emits.
    """
    if v.kind == "str":
        if v.prefix:
            raise ConversionError("an interpolated string is not a plain "
                                  "literal")
        return _unq(v.text)
    if v.kind == "word" and re.fullmatch(r"-?\d+(\.\d+)?", v.text):
        return v.text
    return None


def _closure(group: _Tok) -> tuple:
    """``{|k, v| body}`` → (params, body tokens)."""
    items = group.items
    if not items or not (items[0].kind == "op" and items[0].text == "|"):
        raise ConversionError(f"not a closure: {_ser_tok(group)!r}")
    end = None
    for k in range(1, len(items)):
        if items[k].kind == "op" and items[k].text == "|":
            end = k
            break
    if end is None:
        raise ConversionError(
            f"closure without a body separator: {_ser_tok(group)!r}")
    return [_ser_toks([t]) for t in items[1:end]], items[end + 1:]


# -- the emitted shapes, one pattern per `ir_to_nu` emission site ------------
#
# The path-carrying sites below match an argument in *either* spelling (`_ARG`):
# `open servers.json` and `open "servers.json"` are one op, and so are the file
# operations.  The rest of the patterns are the shapes `ir_to_nu` writes and
# nothing else, and match on content rather than on quoting.

_R_OPEN_RAW = re.compile(rf"^open --raw ({_ARG})$")
_R_OPEN = re.compile(rf"^open ({_ARG})$")
_R_LS = re.compile(r"^ls (.+)/\*\*/\*$")

# _side_effect: one bare file-operation line, no render step at all
_R_MKDIR = re.compile(rf"^mkdir ({_ARG})$")
_R_CPMV = re.compile(rf"^(cp|mv) ({_ARG}) ({_ARG})$")
_R_TOUCH = re.compile(rf"^touch ({_ARG})$")
_R_RM = re.compile(rf"^rm (-r )?({_ARG})$")
_R_SKIP = re.compile(r"^skip (\d+)$")
_R_GET = re.compile(r"^get ([\w.]+)$")
_R_SELECT = re.compile(r"^select ([\w.]+(?: [\w.]+)*)$")
_R_FIRST = re.compile(r"^first (\d+)$")
_R_LAST = re.compile(r"^last (\d+)$")

# _op_read for json is `open PATH`; for tsv/csv the line stream is re-joined
_R_FROM_TSV = re.compile(r"^from tsv -n$")
_R_FROM_CSV = re.compile(r"^from csv --noheaders$")

# _find_rows: the one listing step every find op renders
_R_FIND_ROWS = re.compile(
    r"^each \{\|(\w+)\| \{name: \$\1\.name, sz: \(\$\1\.size \| into int\)\}\}$")
_R_FIND_SIZE = re.compile(r"^sz > (\d+)$")

# _materialize_log: a `lines` stream turned into named log fields
_R_MAT = re.compile(
    r"^each \{\|(\w+)\| let (\w+) = \(\$\1 \| split row (?:' '|\" \")\); "
    r"\{(.*)\}\}$")
_R_MAT_FIELD = re.compile(r"^(\w+): \(\$(\w+) \| get (\d+)\)$")

# _op_awk_get / _op_cut on a `lines` stream
_R_EACH_SPLIT = re.compile(
    r"^each \{\|(\w+)\| \$\1 \| split row (.+?) \| get (\d+)\}$")
# _op_agg mode=sum over a `lines` stream
_R_AGG_SUM_LINES = re.compile(
    r"^each \{\|(\w+)\| \(\$\1 \| split row (?:' '|\" \") \| get (\d+) \| "
    r"into float\)\}$")

_R_TR = re.compile(r"^each \{\|(\w+)\| \$\1 \| str (uppercase|lowercase)\}$")
_R_SED_EACH = re.compile(
    r'^each \{\|(\w+)\| \$\1 \| str replace (--regex )?"((?:[^"\\]|\\.)*)" '
    r'"((?:[^"\\]|\\.)*)"\}$')
_R_SED_WHERE = re.compile(
    r'^where \{\|(\w+)\| \$\1 =~ "((?:[^"\\]|\\.)*)"\}$')

# _op_grep over lines/vals, and over records (the values-join form)
_R_GREP_LINE = re.compile(
    r'^where \{\|(\w+)\| (not )?\(\$\1 =~ "((?:[^"\\]|\\.)*)"\) \}$')
_R_GREP_ROW = re.compile(
    r'^where \{\|(\w+)\| (not )?\(\(\$\1 \| values \| str join '
    r"""(?:' '|" ")\) =~ "((?:[^"\\]|\\.)*)"\) \}$""")

# _op_group_count / _op_group_sum / _op_uniq_c / _op_jq_group item builders
_R_ITEMS_COUNT = re.compile(
    r"^items \{\|k,v\| \{key: \$k, count: \(\$v \| length\)\}\}$")
_R_ITEMS_SUM = re.compile(
    r"^items \{\|k,v\| let s = \(\$v \| get (\w+) \| into float \| math sum\); "
    r"\{key: \$k, sum: \$s\}\}$")
_R_ITEMS_UNIQ_C = re.compile(
    r"^items \{\|k,v\| \{column0: \(\$v \| length\), column1: \$k\}\}$")
_R_ITEMS_JQ_GROUP = re.compile(
    r"^items \{\|k,v\| \{key: \$k, s: \(\$v \| get (\w+) \| into float \| "
    r"math sum\)\}\}$")
_R_BUCKET_HEAD = re.compile(
    r'^each \{\|(\w+)\| if \((.+?) \| str starts-with "((?:[^"\\]|\\.)*)"\) '
    r'\{"((?:[^"\\]|\\.)*)"\} else \{"((?:[^"\\]|\\.)*)"\}\}$')

# _op_sort
_R_SORT_FLAT = re.compile(r"^sort( -r)?$")
# A numeric sort over a flat value/line stream: no column exists, so the
# renderer keys the closure on the scalar itself (`ir_to_nu._op_sort`).  The
# back-references pin the keyed variable to the closure's own parameter, so a
# closure reading some *other* name is not this shape.
_R_SORT_FLAT_NUM = re.compile(
    r"^sort-by \{\|(\w+)\| \(\$\1 \| into float\)\}( -r)?$")
_R_SORT_NUM = re.compile(
    r"^sort-by \{\|(\w+)\| \(\$\1\.(\w+) \| into float\)\}( -r)?$")
_R_SORT_JOIN = re.compile(
    r'^sort-by \{\|(\w+)\| \$"((?:\(\$\1\.\w+\) ?)+)"\}( -r)?$')
_R_SORT_NAME = re.compile(r"^sort-by ([\w.]+)( -r)?$")

# _render templates.  A row template's fields and its separator are parsed by
# `_row_template` rather than matched whole, because the separator is part of
# what the line means (see there).
_R_EACH_TPL = re.compile(r'^each \{\|(\w+)\| \$"(.*)"\}$')
_R_TPL_VALS_NL = re.compile(r'^each \{\|(\w+)\| \$"\(\$\1\)\(char nl\)"\}$')
_R_TPL_VALS = re.compile(r'^each \{\|(\w+)\| \$"\(\$\1\)"\}$')
_R_TPL_FIELD = re.compile(r"\(\$(\w+)\.(\w+)\)")
# `… | save [-f|-a] "F"` — the redirect / `tee` write terminator
_R_SAVE = re.compile(rf"^save (-[fa]) ({_ARG})$")

# statements
_R_LET = re.compile(r"^let\s+(\w+)\s*=\s*\((.*)\)$", re.S)
_R_COND = re.compile(r"^(.+?) (>=|<=|==|!=|>|<) (.+)$")
_R_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"$')
_R_NUMERIC = re.compile(r"-?\d+(\.\d+)?$")

_COMPARE_OPS = {"==", "!=", ">", ">=", "<", "<="}
# `ir_to_nu._if_to_nu` maps the numeric test operators to their Nu spelling; the
# string forms `==`/`!=` share a Nu spelling with `-eq`/`-ne` (see `_if_stmt`).
_TEST_OPS = {"==": "==", "!=": "!=", ">": "-gt", ">=": "-ge",
             "<": "-lt", "<=": "-le"}


# =====================================================================
# the recogniser
# =====================================================================

class _Rec:
    """Segment cursor + the renderer's own stream state.

    ``ir_to_nu._NuGen`` walks ops with a ``_Builder``; this walks segments with
    the *same* ``_Builder``, so ``col``/``rcol``/``render_columns``/
    ``natural_sort_col`` answer exactly as they did when the text was written.
    """

    def __init__(self, segs: list, hint: list | None, sep: str = " ",
                 chunk: str = ""):
        self.segs = segs
        self.hint = hint          # columns the pipeline's render template prints
        self.sep = sep            # the character between them in that template
        self.chunk = chunk        # the text the segments' offsets are into
        self.i = 0
        self.ops: list = []
        self.b = _Builder()
        self.side_effect = False  # a bare file-op line carries no render step
        self.pending_find = None      # find listing op awaiting its -size
        self.pending_reorder = None   # uniq_reorder awaiting its column order

    # -- cursor helpers ---------------------------------------------------
    def seg(self, k: int = 0) -> str:
        return _ser(self.at(k))

    def at(self, k: int = 0) -> list:
        if self.left(k) <= 0:
            # a truncated script is out of the subset like any other: refuse it
            # here rather than letting an index error escape
            self.refuse("the script ends in the middle of a step")
        return self.segs[self.i + k]

    def left(self, k: int = 0) -> int:
        return len(self.segs) - (self.i + k)

    def add(self, *ops):
        self.ops.extend(ops)

    def refuse(self, why: str):
        where = f" (at Nushell step {_ser(self.segs[self.i])!r})" \
            if 0 <= self.i < len(self.segs) else " (at the end of the script)"
        raise ConversionError(f"{why}{where}")

    # -- record-field addressing ------------------------------------------
    def _pos_index(self, name: str) -> int:
        """The index such that ``_Builder.col(i)``/``rcol(i)`` name ``name`` —
        what the ops that address a *rendered* field store (group keys, filter
        columns, cut columns)."""
        if name in self.b.colnames:
            return self.b.colnames.index(name)
        m = re.fullmatch(r"column(\d+)", name)
        if m and self.b.positional:
            i = int(m.group(1))
            if self.b.col(i) != name:
                self.refuse(f"field {name!r} is not a column of this shape")
            self.b.rcol(i)      # widen the shape exactly as the renderer would
            return i
        self.refuse(f"field {name!r} is not part of the current row shape")

    def _field_index(self, name: str) -> int:
        """The same index where the op stores a *Bash field* number: the awk
        steps address materialized log fields through a
        ``{field number: name}`` map, so ``f8`` means field 8 even when the
        materialize skipped the fields before it."""
        m = re.fullmatch(r"f(\d+)", name)
        if m and name in self.b.colnames:
            return int(m.group(1))
        return self._pos_index(name)

    def _value_text(self, text: str) -> str:
        """The Bash text behind a rendered comparison value (``_val``)."""
        if _R_NUMERIC.fullmatch(text):
            return text
        m = _R_QUOTED.match(text)
        if not m:
            self.refuse(f"comparison value {text!r} is not a rendered literal")
        return _unq(m.group(1))

    # -- materialize_log: a helper step, not an op -------------------------
    def _peek_materialize(self, k: int = 0):
        """``each {|l| let p = ($l | split row ' '); {fN: ($p | get N), ...}}``
        → [(field number, name)], without consuming the step."""
        if self.left(k) <= 0:
            return None
        m = _R_MAT.match(self.seg(k))
        if not m:
            return None
        var, out = m.group(2), []
        for field in m.group(3).split(", "):
            fm = _R_MAT_FIELD.match(field)
            if not fm or fm.group(2) != var \
                    or fm.group(1) != f"f{fm.group(3)}":
                return None
            out.append((int(fm.group(3)), fm.group(1)))
        if not out:
            return None
        return out

    def _take_materialize(self):
        mat = self._peek_materialize()
        if mat is None:
            self.refuse("not a materialize step")
        self.b.rows_name([n for _, n in mat])
        self.i += 1

    # =====================================================================
    # sources
    # =====================================================================
    def source(self):
        m = _R_OPEN_RAW.match(self.seg())
        if m:
            return self._src_open_raw(_argval(m.group(1)))
        m = _R_OPEN.match(self.seg())
        if m:
            path = _argval(m.group(1))
            if _file_ext_kind(path) != "json":
                self.refuse(f"{path!r} does not read as json")
            self.i += 1
            self.b.kind = "json"
            self.b.rows_name([])
            self.add(Op("read", kind="json", path=path, skip_header=0))
            return
        m = _R_LS.match(self.seg())
        if m:
            return self._src_find(m.group(1))
        if self._side_effect():
            return
        self.refuse("pipeline does not lead with a data source")

    def _src_open_raw(self, path: str):
        self.i += 1
        if (self.left() >= 2 and self.seg() == 'str replace --all --regex "[^\\n]" ""'
                and self.seg(1) == 'str length'):
            kind = _file_ext_kind(path)
            if kind not in ('log', 'text'):
                self.refuse('newline counting requires a text file')
            self.b.kind, self.b.stream = kind, 'lines'
            self.add(Op('read', kind=kind, path=path, skip_header=0))
            self.add(Op('count'))
            self.i += 2
            return
        if not self.left():
            # The generator's bare cat keeps the file bytes and does not split
            # them into lines, preserving a missing terminal newline.
            kind = _file_ext_kind(path)
            self.b.kind, self.b.stream = kind, 'lines'
            self.add(Op('read', kind=kind, path=path, skip_header=0))
            return
        if self.seg() not in (
            'lines',
            'if $in == "" {[]} else {$in | str replace --regex "\\n$" "" | split row (char nl)}',
        ):
            self.refuse("`open --raw` without the `| lines` line split")
        self.i += 1
        skip = 0
        m = _R_SKIP.match(self.seg())
        if m:
            skip = int(m.group(1))
            self.i += 1
        kind = _file_ext_kind(path)
        if self.seg() == _JOIN_TSV and self.left() >= 2:
            self.i += 1
            tail = self.seg()
            if _R_FROM_TSV.match(tail):
                kind = "tsv"
            elif _R_FROM_CSV.match(tail):
                kind = "csv"
            else:
                self.refuse(f"unrecognized table parse {tail!r}")
            self.i += 1
            self.b.rows_positional(0)
        else:
            if kind not in ("log", "text"):
                self.refuse(f"{path!r} reads as {kind!r}, not a line stream")
            self.b.stream = "lines"
        # The source kind is re-derived from the extension, never trusted from
        # the text — the same rule ``ir_to_bash._read_cmd`` enforces.
        if _file_ext_kind(path) != kind:
            self.refuse(f"read kind {kind!r} is not what path {path!r} "
                        f"re-derives")
        self.b.kind = kind
        self.add(Op("read", kind=kind, path=path, skip_header=skip))

    def _src_find(self, path: str):
        self.i += 1
        ops = [Op("source_find", path=path)]
        self.b.kind, self.b.stream = "files", "rows"
        if self.seg() == "where type == file":
            ops.append(Op("f_filter_file"))
            self.i += 1
        if not _R_FIND_ROWS.match(self.seg()):
            self.refuse("`ls` without the find listing step")
        self.i += 1
        # A find listing's print order is *not* in the listing step — only what
        # the pipeline finally printed says which fields it printed, so the
        # order is read from the render template (and checked there).
        order = self._hint_order([["name"], ["sz", "name"]])
        self.b.rows_name(["name", "sz"])
        self.b.print_order = list(order)
        # `rows_name` reset the separator to the default; a `-printf` that
        # separates with a tab is read off the render template (see `_hint_of`).
        self.b.sep = self.sep
        op = Op("f_list" if order == ["name"] else "f_size",
                print_order=list(order),
                **({} if self.sep == " " else {"sep": self.sep}))
        ops.append(op)
        self.pending_find = op
        self.add(*ops)

    def _side_effect(self) -> bool:
        """mkdir / cp / mv / rm / touch / truncate: one bare command line, with
        no render step at all (``ir_to_nu.pipeline`` returns before it).

        Each argument is read in either spelling (``mkdir out`` / ``mkdir
        "out"``); a file operation whose spelling the layer does not read is
        refused *as* a file operation, rather than being left to the generic
        "does not lead with a data source" message -- which names the wrong
        cause entirely, since the step is a perfectly good file operation and
        the pipeline is not the problem.
        """
        s0 = self.seg()
        toks = self.at(0)
        head = toks[0].text if toks and toks[0].kind == "word" else None
        op = None
        if m := _R_MKDIR.match(s0):
            op = Op("mkdir", path=_argval(m.group(1)))
        elif m := _R_CPMV.match(s0):
            op = Op(m.group(1), src=_argval(m.group(2)),
                    dst=_argval(m.group(3)))
        elif m := _R_TOUCH.match(s0):
            op = Op("touch", path=_argval(m.group(1)))
        elif m := _R_RM.match(s0):
            op = Op("rm", path=_argval(m.group(2)), rec=bool(m.group(1)))
        if op is not None:
            if self.left() != 1:
                self.refuse("file ops cannot share a pipeline")
            self.i += 1
            self.add(op)
            self.side_effect = True
            return True
        # bash `: > file` — `'' | save -f PATH`.  The literal is matched in its
        # *canonical* spelling: `_ser_tok` writes any string token double-quoted,
        # so the empty one arrives here as `""` however it was written.
        if s0 == '""' and self.left() == 2:
            m = _R_SAVE.match(self.seg(1))
            if not m or m.group(1) != "-f":
                self.refuse("unrecognized `save` form")
            self.i += 2
            self.add(Op("truncate", path=_argval(m.group(2))))
            self.side_effect = True
            return True
        if head in _FILE_OPS:
            self.refuse(f"unrecognized `{head}` file operation (the layer reads "
                        f"`mkdir PATH`, `cp SRC DST`, `mv SRC DST`, "
                        f"`rm [-r] PATH`, `touch PATH`)")
        return False

    def _hint_order(self, allowed, unordered: bool = False) -> list:
        """A derived print order: it must be one the render template confirms.

        ``unordered`` is for the group ops, whose END-block `print` may list the
        key and the aggregate in either order (the token list *is* the render
        order);
        a find listing's variants are distinct orders, so they are matched
        exactly.
        """
        if self.hint is None:
            self.refuse("print order lives only in the render template, and "
                        "this pipeline has none (a capture's `str join` steps "
                        "carry no column names)")
        ok = [set(a) == set(self.hint) for a in allowed] if unordered \
            else [list(a) == list(self.hint) for a in allowed]
        if not any(ok):
            self.refuse(f"print order {self.hint!r} has no rendering for this "
                        f"step")
        return list(self.hint)

    # =====================================================================
    # render / terminator
    # =====================================================================
    def render_tail(self) -> bool:
        rest = self.segs[self.i:]
        if not rest:
            return False
        # A redirect (`> file`) or a `tee` write ends the pipeline with
        # `… | str join "" | save [-a] "F"`; the empty join is the tell, because
        # every line already carries the newline the file gets.
        dest = None
        if len(rest) >= 3 and _join_empty(rest[-2]):
            m = _R_SAVE.match(_ser(rest[-1]))
            if not m:
                return False
            dest = (_argval(m.group(2)), m.group(1) == "-a")
            rest = rest[:-1]         # keep the join; only the save comes off
        if len(rest) == 1 and _join_nl(rest[0]):
            # A capture's re-join (`ir_to_nu._expr_join`).  The op list is the
            # same shape as a top-level render, so the render op is still part
            # of the IR — only its text differs.
            if dest is not None:
                self.refuse("a re-join and a redirect cannot share one render")
            if self.b.stream not in ("lines", "vals", "rows"):
                self.refuse(f"cannot re-join a {self.b.stream} stream")
            if self.b.stream == "rows":
                self._finish_rows(None)
            self.i = len(self.segs)
            self.add(Op("render"))
            return True
        if len(rest) != 2:
            return False
        explicit_newline = dest is None and _join_empty(rest[1])
        if not (_join_empty(rest[1]) if dest is not None or explicit_newline
                else _join_nl(rest[1])):
            return False
        head = _ser(rest[0])
        if '| fill -a right -w 7)' in head and not self._last_shape_is_uniq_c():
            self.refuse('count padding is only recognized after uniq -c')
        tpl = _row_template(head)
        if tpl is not None:
            _, cols, sep, tpl_nl = tpl
            if tpl_nl != (dest is not None or explicit_newline):
                self.refuse(f"row template {head!r} and its terminator "
                            f"disagree about the line ending")
            if sep != self.b.sep:
                self.refuse(f"row template {head!r} joins its fields with "
                            f"{sep!r}, which the step that made these rows "
                            f"does not print")
            self._finish_rows(list(cols))
        elif _R_TPL_VALS.match(head) or _R_TPL_VALS_NL.match(head):
            if bool(_R_TPL_VALS_NL.match(head)) != (dest is not None or explicit_newline):
                self.refuse(f"scalar template {head!r} and its terminator "
                            f"disagree about the line ending")
            if self.b.stream not in ("lines", "vals"):
                self.refuse(f"a scalar render template over a "
                            f"{self.b.stream} stream")
        elif _R_EACH_TPL.match(head):
            self.refuse(f"unrecognized row template: {head!r}")
        else:
            return False
        self.i = len(self.segs)
        self.add(Op("render", path=dest[0], append=dest[1]) if dest
                 else Op("render"))
        return True

    def while_read(self) -> bool:
        """`… | each {|l| …bindings… body } | ignore` — the line loop.

        The `each` consumes *this point's* lines, which is what the loop
        iterates whatever feeds it.  A loop fed by a bare `open --raw F | lines`
        is the `done < F` spelling and is recognised before the walk starts (see
        ``_file_loop``), so the source here is always the stages before it.
        """
        # The lines the loop iterates may be *rendered* by one more step: a
        # values or rows stream has to become text before `read` sees it, and
        # the renderer writes that step as part of the loop.  A stream that is
        # already lines needs no such step.
        at = self.i
        if self.left() >= 3 and _loop_tail(self.segs[self.i + 1:self.i + 3]):
            head = _ser(self.segs[self.i])
            if _R_TPL_VALS.match(head) or _row_template(head) is not None:
                at = self.i + 1
        if self.left() < at - self.i + 2:
            return False
        body = _loop_body(self.segs[at:at + 2], self.refuse, self.chunk)
        if body is None:
            return False
        variables, ifs, statements = body
        if not self.ops:
            self.refuse("a loop with nothing feeding it")
        self.i = at + 2
        self.add(Op("while_read", var=variables, ifs=ifs, path=None,
                    body=statements))
        return True

    def tee(self) -> bool:
        """`tee { $in | …(char nl)… | str join "" | save [-a] "F" }`.

        `ir_to_nu` renders a `tee` as a closure that re-renders *this point's*
        lines off `$in` — never a re-run of the pipeline — so the shape the
        closure names is checked against the stream the walk has built so far.
        """
        toks = self.at(0)
        if len(toks) != 2 or not _word(toks, 0, "tee") \
                or toks[1].kind != "group" or toks[1].text != "{":
            return False
        inner = _segments(toks[1].items)
        head = None
        if len(inner) == 3 and _ser(inner[0]) == "[$in]":
            # a scalar stream (`… | wc -l | tee f`): one line, so no template
            if self.b.stream != "value":
                self.refuse(f"a `tee` of `[$in]` over a {self.b.stream} stream")
        elif len(inner) == 4 and _ser(inner[0]) == "$in":
            head = _ser(inner[1])
        else:
            self.refuse(f"unrecognized `tee` closure: {_ser_tok(toks[1])!r}")
        if not _join_empty(inner[-2]):
            self.refuse(f"a `tee` that does not re-join its lines: "
                        f"{_ser(inner[-2])!r}")
        m = _R_SAVE.match(_ser(inner[-1]))
        if not m:
            self.refuse(f"unrecognized `tee` save: {_ser(inner[-1])!r}")
        if head is not None:
            tpl = _row_template(head)
            if tpl is not None:
                if not tpl[3] or self.b.stream != "rows":
                    self.refuse(f"`tee` template {head!r} does not match a "
                                f"{self.b.stream} stream")
                if tpl[2] != self.b.sep:
                    self.refuse(f"`tee` template {head!r} joins its fields "
                                f"with {tpl[2]!r}, which the stream does not")
            elif _R_TPL_VALS_NL.match(head):
                if self.b.stream not in ("lines", "vals"):
                    self.refuse(f"`tee` of scalar lines over a "
                                f"{self.b.stream} stream")
            else:
                self.refuse(f"unrecognized `tee` line template: {head!r}")
        self.i += 1
        self.add(Op("tee", path=_argval(m.group(2)), append=m.group(1) == "-a"))
        return True

    def _positional_cols(self, cols: list) -> list:
        out = []
        for name in cols:
            m = re.fullmatch(r"column(\d+)", name)
            if not m or not self.b.positional:
                self.refuse(f"a re-ordered record prints {name!r}, which is not "
                            f"a positional column")
            out.append(int(m.group(1)))
        return out

    def _finish_rows(self, cols: list | None):
        """Resolve and check everything the row shape itself does not carry.

        A find listing's print order, a group's print tokens and an
        ``uniq_reorder`` column permutation are visible only in what the
        pipeline finally printed, so the render template is both where they are
        read and where they are checked.
        """
        # 1. a re-order is visible only as a print order that is not the record's
        if cols is not None and self.pending_reorder is not None:
            out = self._positional_cols(cols)
            self.pending_reorder.params["cols"] = out
            self.pending_reorder = None
            self.b.print_order = [f"column{c}" for c in out]
        elif cols is not None and self._last_shape_is_uniq_c() \
                and list(cols) != list(self.b.render_columns()):
            # a *pure* re-order awk (`{print $2, $1}`) has no step of its own
            out = self._positional_cols(cols)
            self.ops.append(Op("uniq_reorder", cols=out, filter=None))
            self.b.print_order = [f"column{c}" for c in out]
        elif self.pending_reorder is not None:
            if cols is None:
                self.refuse("uniq_reorder columns live only in the render "
                            "template, and this pipeline has none")
            self.pending_reorder = None

        # 2. the observed print order must be one the recovered shape renders
        if cols is not None:
            expected = self.b.print_order or \
                [self.b.col(k) for k in range(self.b.ncols)]
            if list(cols) != list(expected):
                self.refuse(f"render template prints {cols!r} but the recovered "
                            f"row shape renders {list(expected)!r}")

        # 3. a find listing's size filter decides which listing op it was
        if self.pending_find is not None:
            op = self.pending_find
            self.pending_find = None
            if "size" in op.params:
                # `-size` alone prints paths; `-printf '%s %p'` prints size
                # first, so the print order says whether a -printf was there.
                want = (["sz", "name"]
                        if len(op.params["print_order"]) == 2 else ["name"])
                if list(op.params["print_order"]) != want:
                    self.refuse(f"find print order "
                                f"{op.params['print_order']!r} has no -printf "
                                f"rendering")

    # =====================================================================
    # transform steps
    # =====================================================================
    def _m_bucket(self) -> bool:
        """``each {|r| if (<col> | str starts-with "P") {"yes"} else {"other"}}
        | group-by {|x| $x} | items {key,count}`` — one `bucket` op."""
        m = _R_BUCKET_HEAD.match(self.seg())
        if not m or self.left() < 3:
            return False
        if _ser(self.at(1)) != "group-by {|x| $x}" \
                or not _R_ITEMS_COUNT.match(_ser(self.at(2))):
            return False
        if self.b.stream not in ("lines", "rows"):
            self.refuse(f"bucket over a {self.b.stream} stream")
        if (m.group(4), m.group(5)) != ("yes", "other"):
            self.refuse(f"bucket labels {(m.group(4), m.group(5))!r} are not "
                        f"the names ir_to_nu renders (the parser never carries "
                        f"them)")
        c = re.fullmatch(r"\(\$\w+ \| split row (?:' '|\" \") \| get (\d+)\)",
                         m.group(2))
        if c and self.b.stream == "lines":
            col = int(c.group(1))
        else:
            col = self._pos_index(m.group(2))
        self.i += 3
        self.add(Op("bucket", col=col, prefix=_unq(m.group(3))))
        self.b.rows_name(["key", "count"])
        return True

    def _m_group(self) -> bool:
        """``group-by <key> | items {...}`` — group_count / group_sum /
        uniq_c, and the (refused) jq group_by."""
        mat = self._peek_materialize()
        j = self.i + (1 if mat else 0)
        if j >= len(self.segs) or not _word(self.segs[j], 0, "group-by"):
            return False
        key = _ser(self.segs[j][1:])
        items = _ser(self.segs[j + 1]) if j + 1 < len(self.segs) else ""
        if key == "{|x| $x}" and _R_ITEMS_UNIQ_C.match(items):
            if self.b.stream not in ("lines", "vals", "rows"):
                self.refuse(f"uniq -c over a {self.b.stream} stream")
            if mat:
                self._take_materialize()
            self.i = j + 2
            self.add(Op("uniq_c"))
            self.b.rows_positional(2)
            self.b.print_order = ["column0", "column1"]
            return True
        if _R_ITEMS_JQ_GROUP.match(items):
            # The jq template is stored verbatim by the parser and is not a
            # function of the rendered Nu, so the summed field alone cannot
            # rebuild the op.
            self.refuse("a jq group_by template is not recoverable from its "
                        "Nushell image")
        if _R_ITEMS_COUNT.match(items):
            if key == "{|x| $x}":
                self.refuse("group-by {|x| $x} without a bucket step")
            print_tok = self._hint_order([["key", "count"]], unordered=True)
        elif _R_ITEMS_SUM.match(items):
            print_tok = self._hint_order([["key", "sum"]], unordered=True)
        else:
            return False
        if self.b.stream == "lines":
            self._take_materialize()
            j += 1
        elif self.b.stream != "rows":
            self.refuse(f"group-by over a {self.b.stream} stream")
        kcol = self._pos_index(key)
        self.i = j + 2
        if _R_ITEMS_COUNT.match(items):
            self.add(Op("group_count", key=kcol, print=list(print_tok)))
        else:
            vcol = self._pos_index(_R_ITEMS_SUM.match(items).group(1))
            self.add(Op("group_sum", key=kcol, val=vcol, print=list(print_tok)))
        self.b.rows_name(print_tok)
        self.b.print_order = list(print_tok)
        return True

    def _m_agg(self) -> bool:
        """The scalar aggregates that carry no `where`: ``get N | math sum`` on a
        record stream, ``each {...} | math sum`` over a line stream."""
        if self.left() < 2 or self.seg(1) != "math sum":
            return False
        m = _R_AGG_SUM_LINES.match(self.seg())
        if m:
            if self.b.stream not in ("lines", "vals"):
                self.refuse(f"awk sum over a {self.b.stream} stream")
            self.i += 2
            self.add(Op("agg", mode="sum", sumcol=int(m.group(2))))
        else:
            m = _R_GET.match(self.seg())
            if not m:
                return False
            if self.b.stream == "lines":
                self._take_materialize()
            if self.b.stream != "rows":
                self.refuse(f"awk sum over a {self.b.stream} stream")
            col = self._field_index(m.group(1))
            self.i += 2
            self.add(Op("agg", mode="sum", sumcol=col))
        self.b.stream = "value"
        return True

    def _m_materialize_select(self) -> bool:
        """A materialize step and nothing else: the multi-column `awk_get` of an
        awk over a line stream (its projection is the record's field names)."""
        mat = self._peek_materialize()
        if mat is None:
            return False
        if self.left() < 2:
            return False
        if _word(self.at(1), 0, "where") or self._is_projection(1):
            # a `where` belongs to _m_where_mat; a bare projection is not an
            # image any emission site produces
            return False
        self._take_materialize()
        self.add(Op("awk_get", cols=[idx for idx, _ in mat]))
        return True

    def _is_projection(self, k: int) -> bool:
        if self.left() <= k:
            return False
        s = self.seg(k)
        return bool(_R_SELECT.match(s) or _R_GET.match(s))

    def _projection(self) -> list:
        """Consume the projection half of a filter awk; returns its columns."""
        sel = self.seg()
        m = _R_SELECT.match(sel)
        if m:
            names = m.group(1).split(" ")
            cols = [self._field_index(n) for n in names]
            self.i += 1
            self.b.rows_name(names)
            return cols
        m = _R_GET.match(sel)
        if not m:
            self.refuse(f"unrecognized projection {sel!r}")
        col = self._field_index(m.group(1))
        self.i += 1
        self.b.stream = "vals"
        return [col]

    def _finish_awk_fget(self, col: int, o: str, val: str) -> bool:
        if self.b.stream == "lines":
            self.refuse("an awk filter over a line stream needs its "
                        "materialize step")
        if self.b.stream != "rows":
            self.refuse(f"awk filter over a {self.b.stream} stream")
        op = Op("awk_fget", fcol=col, op=o, val=val, cols=[])
        self.add(op)
        if self._is_projection(0):
            op.params["cols"] = self._projection()
            return True
        # no projection: the record keeps every materialized field, and the
        # printed subset is what the render template says it was
        if self.hint is None:
            self.refuse("an awk filter's columns live only in the render "
                        "template, and this pipeline has none")
        for n in self.hint:
            if n not in self.b.colnames:
                self.refuse(f"printed field {n!r} was never materialized")
        op.params["cols"] = [self._field_index(n) for n in self.hint]
        self.b.rows_name(list(self.hint))
        return True

    def _m_each_split(self) -> bool:
        """``each {|l| $l | split row D | get N}`` — `cut` for a delimiter,
        `awk_get` for awk's fixed single-quoted *space*.

        Both are one line-level field read; the only delimiter that names two
        ops is the space (awk's field split versus `cut -d' '`), and there the
        renderer's own spelling decides: ``' '`` is the awk split, ``" "`` is
        the cut.  Every other delimiter names exactly one op, so it is read
        quote-style-blind.  (A *record*-stream `get` is genuinely ambiguous
        between the two; see `_m_get`.)
        """
        m = _R_EACH_SPLIT.match(self.seg())
        if not m:
            return False
        if self.b.stream not in ("lines", "vals"):
            self.refuse(f"line split over a {self.b.stream} stream")
        delim = m.group(2)
        self.i += 1
        if delim == "' '":
            # the awk field split: the one literal whose spelling is semantic
            self.add(Op("awk_get", cols=[int(m.group(3))]))
        else:
            if not re.fullmatch(r'"(?:[^"\\])"', delim):
                # every other delimiter names exactly one op (the awk split is
                # always a *space*), so its content decides, not its quote style
                self.refuse(f"delimiter {delim!r} is not a one-character "
                            f"`cut -d` delimiter")
            self.add(Op("cut", delim=delim[1:-1], col=int(m.group(3))))
        self.b.stream = "vals"
        return True

    def _m_materialize_get(self) -> bool:
        """`get NAME` on a record stream — `awk_get` with one column.

        AMBIGUITY: on a *record* stream a `cut -fN` renders to this very step,
        so the two ops are indistinguishable here.  `awk_get` is taken because
        it needs nothing the text does not say: a `cut` op also carries a
        delimiter, and the step names none (only the source's extension could
        suggest one, which would be a guess).  Both render to this same Nu, so
        the delivered script is unaffected either way.
        """
        if self.b.stream != "rows":
            return False
        m = _R_GET.match(self.seg())
        if not m:
            return False
        col = self._field_index(m.group(1))
        self.i += 1
        self.add(Op("awk_get", cols=[col]))
        self.b.stream = "vals"
        return True

    def _m_select(self) -> bool:
        """`select A B` — the multi-column projection of `awk_get`."""
        if self.b.stream != "rows":
            return False
        m = _R_SELECT.match(self.seg())
        if not m:
            return False
        names = m.group(1).split(" ")
        cols = [self._field_index(n) for n in names]
        self.i += 1
        self.add(Op("awk_get", cols=cols))
        self.b.rows_name(names)
        return True

    def _m_tr(self) -> bool:
        m = _R_TR.match(self.seg())
        if not m:
            return False
        if self.b.stream not in ("lines", "vals"):
            self.refuse(f"tr over a {self.b.stream} stream")
        self.i += 1
        mode = "upper" if m.group(2) == "uppercase" else "lower"
        self.add(Op("tr", mode=mode))
        self.b.stream = "vals"
        return True

    def _m_sed(self) -> bool:
        """`sed` — optionally preceded by its own `keep_matched` filter, which
        carries the same pattern (``ir_to_nu._op_sed``)."""
        m = _R_SED_WHERE.match(self.seg())
        if m:
            keep, n = _unq(m.group(2)), 2
            if self.left() < 2:
                self.refuse("sed filter without its substitution step")
            m = _R_SED_EACH.match(self.seg(1))
            if not m or _unq(m.group(3)) != keep:
                self.refuse("sed filter without its matching substitution")
        else:
            m, n = _R_SED_EACH.match(self.seg()), 1
            if not m:
                return False
            if not m.group(2):
                # the front-end always normalizes BRE to ERE, so an unmarked
                # `str replace` is not an image any emission site produces
                self.refuse("sed without the regex normalization")
        if self.b.stream not in ("lines", "vals"):
            self.refuse(f"sed over a {self.b.stream} stream")
        self.i += n
        self.add(Op("sed", pat=_unq(m.group(3)), rep=_unq(m.group(4)),
                    regex=bool(m.group(2)), keep_matched=n == 2))
        self.b.stream = "vals"
        return True

    def _m_sort(self) -> bool:
        seg = self.seg()
        m = _R_SORT_FLAT.match(seg)
        if m:
            if self.b.stream not in ("vals", "lines"):
                self.refuse(f"sort over a {self.b.stream} stream")
            # Nu's `sort` *is* the lexical sort, and it is the spelling the
            # renderer writes for exactly one IR (`numeric=False`), so the flag
            # is read literally rather than chosen.  (Over a value/line stream
            # the numeric sort has its own spelling — the next branch — so the
            # two flags no longer share an image and the old collision is gone.)
            self.i += 1
            self.add(Op("sort", numeric=False, reverse=bool(m.group(1)),
                        col=None, unique=self._take_uniq()))
            return True
        m = _R_SORT_FLAT_NUM.match(seg)
        if m:
            if self.b.stream not in ("vals", "lines"):
                self.refuse(f"sort-by over a {self.b.stream} stream")
            # The identity closure with `into float` is the renderer's numeric
            # sort over a scalar stream; Bash's `sort -n` is the only source
            # shape it comes from.
            self.i += 1
            self.add(Op("sort", numeric=True, reverse=bool(m.group(2)),
                        col=None, unique=self._take_uniq()))
            return True
        if self.b.stream != "rows":
            return False
        keys = None
        if m := _R_SORT_NUM.match(seg):
            key, rev = m.group(2), bool(m.group(3))
            cols = self.b.render_columns()
            if key not in cols:
                self.refuse(f"sort key {key!r} is not a rendered column "
                            f"{cols!r}")
            idx = cols.index(key)
            # AMBIGUITY: on a numeric sort keyed on the *first* rendered column,
            # the bare `sort -n/-r` form and the explicit `-k1,1n` form have the
            # same Nu image.  The bare reading is taken for a positional table
            # (synthetic `column{i}` fields), the explicit one for named fields —
            # the split the corpus exhibits, and either reading renders *and
            # means* the same thing (bash's bare `-n` reads the leading field).
            if idx == 0 and self.b.positional:
                col = None
            else:
                col = idx
                keys = [{"col": idx, "numeric": True, "reverse": rev}]
            numeric = True
        elif m := _R_SORT_JOIN.match(seg):
            cols = [n for _, n in _R_TPL_FIELD.findall(m.group(2))]
            want = list(self.b.render_columns())
            if cols != want:
                self.refuse(f"sort key {cols!r} is not the rendered record "
                            f"{want!r}")
            rev, numeric, col = bool(m.group(3)), False, None
        elif m := _R_SORT_NAME.match(seg):
            cols = self.b.render_columns()
            if m.group(1) not in cols:
                self.refuse(f"sort key {m.group(1)!r} is not a rendered column "
                            f"{cols!r}")
            col, rev = cols.index(m.group(1)), bool(m.group(2))
            numeric = False
            keys = [{"col": col, "numeric": False, "reverse": rev}]
        else:
            return False
        self.i += 1
        self.add(Op("sort", numeric=numeric, reverse=rev, col=col,
                    unique=self._take_uniq(),
                    **({"keys": keys} if keys else {})))
        return True

    def _take_uniq(self) -> bool:
        """Fold a `uniq` step that immediately follows a sort into the sort op
        (the cursor must already be past the sort step)."""
        if self.left() >= 1 and self.seg() == "uniq":
            # AMBIGUITY: `sort -u` and `sort | uniq` share one Nu image (the
            # renderer folds the dedup into the sort).  The two are equivalent
            # on every corpus input, and the fold is the only reading the
            # corpus supports.
            self.i += 1
            return True
        return False

    def _m_uniq(self) -> bool:
        if self.seg() != "uniq":
            return False
        if self.b.stream not in ("vals", "lines", "rows"):
            self.refuse(f"uniq over a {self.b.stream} stream")
        self.i += 1
        # Nushell uniq is global, whereas bare Bash uniq is adjacent-only.
        # The sorted case is consumed by _m_sort; an unsorted uniq must return
        # to the awk form that preserves global deduplication.
        self.add(Op("uniq", global_dedup=True))
        return True

    def _m_slices(self) -> bool:
        m = _R_FIRST.match(self.seg())
        if m:
            self.i += 1
            self.add(Op("head", n=int(m.group(1))))
            return True
        m = _R_LAST.match(self.seg())
        if m:
            self.i += 1
            self.add(Op("tail", n=int(m.group(1))))
            return True
        m = _R_SKIP.match(self.seg())
        if m:
            self.i += 1
            self.add(Op("skip", n=int(m.group(1))))
            return True
        if self.seg() == "length":
            self.i += 1
            self.add(Op("count"))
            self.b.stream = "value"
            return True
        return False

    # =====================================================================
    # `where` — the widest family: filter, awk_fget, agg, grep, sed, jq,
    # find guard and uniq_reorder all spell their condition this way
    # =====================================================================
    def _where_form(self, seg: list):
        if not _word(seg, 0, "where"):
            return None
        rest = seg[1:]
        if len(rest) == 1 and rest[0].kind == "group" and rest[0].text == "{":
            params, body = _closure(rest[0])
            return ("closure", params[0] if params else "", _ser_toks(body))
        if not rest:
            return None
        return ("bare", _ser(rest), None)

    def _m_where(self) -> bool:
        w = self._where_form(self.at())
        if w is None:
            return False
        if w[0] == "bare":
            return self._where_bare(w[1])
        return self._where_closure(w[1], w[2])

    def _where_bare(self, text: str) -> bool:
        prev = self.ops[-1].op_type if self.ops else None
        if text == "type == file" and prev == "source_find":
            self.i += 1
            self.add(Op("f_filter_file"))
            return True
        m = _R_FIND_SIZE.match(text)
        if m and self.pending_find is not None:
            op = self.pending_find
            self.pending_find = None
            self.i += 1
            op.op_type = "f_gt_size"
            # param order matters for the IR's repr: `size` comes first
            op.params = {"size": int(m.group(1)),
                         "print_order": op.params["print_order"]}
            return True
        m = _R_COND.match(text)
        if not m:
            self.refuse(f"unrecognized `where` condition {text!r}")
        o = m.group(2)
        if o not in _COMPARE_OPS:
            self.refuse(f"comparison {o!r} is not one awk/Nu emits")
        col = self._field_index(m.group(1))
        val = self._value_text(m.group(3))

        # --- aggregates that fuse their filter into one awk program ---
        agg = self._agg_after_where(col, o, val)
        if agg is not None:
            n, ops = agg
            self.i += n
            self.add(*ops)
            self.b.stream = "value"
            return True

        # --- a re-order of the `uniq -c` table (its filter, if any) ---
        if self._last_shape_is_uniq_c() and not self._is_projection(1):
            self.i += 1
            op = Op("uniq_reorder", cols=None, filter=(col, o, val))
            self.pending_reorder = op
            self.add(op)
            return True

        # --- otherwise a filter, which is always half of one awk program ---
        if not self._is_projection(1):
            self.refuse("a `where` without a projection is not an op the "
                        "parser emits (a filter is always half of one awk)")
        self.i += 1
        if self.b.kind in ("log", "text"):
            return self._finish_awk_fget(col, o, val)
        self.add(Op("filter", col=col, op=o, val=val))
        self._projection()
        return True

    def _last_shape_is_uniq_c(self) -> bool:
        """Mirror of ``ir_to_bash._SHAPE_KEEPING``: walk back over the ops that
        leave the row shape alone."""
        k = len(self.ops) - 1
        while k >= 0 and self.ops[k].op_type in _SHAPE_KEEPING:
            k -= 1
        return k >= 0 and self.ops[k].op_type == "uniq_c"

    def _agg_after_where(self, col: int, o: str, val: str):
        """``where <col> == V`` fused with the END-block aggregate it belongs
        to, or None when the condition stands alone."""
        if o != "==":
            return None
        rest = self.segs[self.i + 1:]
        if len(rest) == 1 and _ser(rest[0]) == "length" \
                and not self._last_shape_is_uniq_c():
            # AMBIGUITY: the count-fused awk and the unfused `filter | count`
            # share this image, and the unfused form has no Bash rendering (a
            # `filter` op must be followed by its projection), so the fused
            # reading is the one the layer can round-trip.
            return (2, [Op("agg", mode="count", fcol=col, val=val)])
        if len(rest) < 4 or not _R_GET.match(_ser(rest[0])):
            return None
        if _ser(rest[1]) != "into float" or _ser(rest[2]) != "math avg":
            return None
        if _ser(rest[3]) == "math round":
            rint = None
        elif _ser(rest[3]) == "into int":
            rint = "int"
        else:
            return None
        sumcol = self._field_index(_R_GET.match(_ser(rest[0])).group(1))
        return (5, [Op("agg", mode="avg", fcol=col, val=val, sumcol=sumcol,
                       rint=rint)])

    def _grep(self, m) -> Op:
        """AMBIGUITY: `grep -c PAT F` and `grep PAT F | wc -l` share this image
        (the renderer spells the count filter the same way for both).  The
        two-op reading is taken — the count arrives as the following `length`
        step — which is the spelling the corpus uses; both count matching lines,
        so the delivered Nu is the same."""
        return Op("grep", pat=_unq(m.group(3)), invert=bool(m.group(2)),
                  count=False)

    def _where_closure(self, var: str, body: str) -> bool:
        wrapped = f"where {{|{var}| {body} }}"
        if _R_SED_WHERE.match(wrapped):
            return False          # _m_sed owns the filter/substitution pair
        m = _R_GREP_LINE.match(wrapped)
        if m:
            if self.b.stream not in ("lines", "vals"):
                self.refuse(f"grep over a {self.b.stream} stream")
            self.i += 1
            self.add(self._grep(m))
            return True
        m = _R_GREP_ROW.match(wrapped)
        if m:
            if self.b.stream != "rows":
                self.refuse(f"grep over a {self.b.stream} stream")
            self.i += 1
            self.add(self._grep(m))
            return True
        if self.b.kind == "json":
            return self._jq_where(var, body)
        m = re.fullmatch(r'\(\$(\w+)\.(\w+)\) =~ "((?:[^"\\]|\\.)*)"', body)
        if m:
            col = self._field_index(m.group(2))
            self.i += 1
            if self.b.kind in ("log", "text"):
                return self._finish_awk_fget(col, "~", m.group(3))
            self.add(Op("filter", col=col, op="~", val=m.group(3)))
            self._projection()
            return True
        m = re.fullmatch(
            r"\(\$(\w+)\.(\w+) \| into float\) (>=|<=|==|!=|>|<) (.+)", body)
        if m:
            col = self._field_index(m.group(2))
            val = self._value_text(m.group(4))
            if self.b.kind == "json":
                return self._jq_where(var, body)
            self.i += 1
            return self._finish_awk_fget(col, m.group(3), val)
        self.refuse(f"unrecognized `where` body {body!r}")

    # =====================================================================
    # jq — emitted only over a json source
    # =====================================================================
    def _jq_where(self, var: str, body: str) -> bool:
        conds = [self._jq_cond(part, var)
                 for part in re.split(r"\s+and\s+", body) if part.strip()]
        if not conds:
            self.refuse(f"unrecognized jq select {body!r}")
        self.i += 1
        first = None
        if self.left():
            m = _R_FIRST.match(self.seg())
            if m:
                # `ir_to_nu._op_jq_where` emits the slice *before* the
                # projection, so a `first` here is the jq slice, not a `head`.
                first = int(m.group(1))
                self.i += 1
        fields: list = []
        if self.left():
            if m := _R_SELECT.match(self.seg()):
                fields = m.group(1).split(" ")
                self.i += 1
            elif m := _R_GET.match(self.seg()):
                fields = [m.group(1)]
                self.i += 1
        if not fields and first is None:
            self.refuse("a jq select with neither a projection nor a slice")
        self.add(Op("jq_where", conds=conds, fields=list(fields), first=first))
        if len(fields) == 1:
            self.b.stream = "vals"
        else:
            self.b.rows_name(list(fields))
        return True

    def _jq_operand(self, text: str, var: str) -> tuple:
        """``($r.f | default N)`` / ``($r.f | into float)`` → (field, default,
        cast); the field name is returned so the caller can rebuild the cond."""
        default, cast = None, False
        s = text
        if s.startswith("(") and s.endswith(")"):
            inner = s[1:-1]
            for _ in range(2):
                if inner.endswith("| into float"):
                    cast, inner = True, inner[:-len("| into float")].strip()
                if "| default " in inner:
                    head, dflt = inner.split("| default ", 1)
                    if not _R_NUMERIC.fullmatch(dflt.strip()):
                        self.refuse(f"jq default {dflt!r} is not a digit string")
                    default, inner = dflt.strip(), head.strip()
            s = inner.strip().strip("()")
        return s, default, cast

    def _jq_cond(self, part: str, var: str) -> dict:
        m = re.fullmatch(
            rf"(.*?) (>=|<=|==|!=|>|<) (.+)", part.strip())
        if not m:
            self.refuse(f"unrecognized jq condition {part!r}")
        operand, default, cast = self._jq_operand(m.group(1), var)
        fm = re.fullmatch(rf"\${var}\.(\w+)", operand)
        if not fm:
            self.refuse(f"unrecognized jq operand {m.group(1)!r}")
        val = m.group(3).strip()
        if val != "null":
            val = self._jq_val(val)
        return {"field": fm.group(1), "op": m.group(2), "val": val,
                "cast": cast, "default": default}

    def _jq_val(self, text: str) -> str:
        """A jq value is jq syntax (quotes and all), re-emitted by ``_val``."""
        if _R_NUMERIC.fullmatch(text):
            return text
        m = _R_QUOTED.match(text)
        if not m:
            self.refuse(f"jq value {text!r} is not a rendered literal")
        return '"' + _unq(m.group(1)) + '"'


def _m_where_mat(r: _Rec) -> bool:
    """A `where` that follows a materialize step: the materialize is not an op,
    it is how the filter's fields come to exist."""
    if r._peek_materialize() is None or not _word(r.at(1), 0, "where"):
        return False
    form = r._where_form(r.at(1))
    if form is None or form[0] != "closure":
        return False
    # sed's own filter owns *both* steps, so it must not be taken here (a
    # matcher that returns False may not have consumed anything).
    if _R_SED_WHERE.match(f"where {{|{form[1]}| {form[2]} }}"):
        return False
    r._take_materialize()
    return r._m_where()


# The dispatch order matters where two emission sites produce the same step
# text; each matcher must be reachable before the more general one that would
# otherwise swallow it (`_m_where_mat` before `_m_where`, `_m_where` before
# `_m_materialize_get`, `_m_sort` before `_m_uniq`, ...).
_MATCHERS = (lambda r: r._m_bucket(), lambda r: r._m_group(),
             lambda r: r._m_agg(), _m_where_mat, lambda r: r._m_sed(),
             lambda r: r._m_materialize_select(),
             lambda r: r._m_materialize_get(), lambda r: r._m_select(),
             lambda r: r._m_each_split(), lambda r: r._m_tr(),
             lambda r: r._m_where(), lambda r: r._m_sort(),
             lambda r: r._m_uniq(), lambda r: r._m_slices(),
             lambda r: r.tee(), lambda r: r.while_read())

# `let V = ($l | split row (char tab) | get 0)` / `| skip 0 | str join (char tab)`
# — the two bindings `ir_to_nu._read_bindings` writes for a tab-splitting read.
_R_READ_GET = re.compile(
    r"^let (\w+) = \(\$l \| split row \(char tab\) \| get (\d+)\)$")
_R_READ_REST = re.compile(
    r"^let (\w+) = \(\$l \| split row \(char tab\) \| skip (\d+) "
    r"\| str join \(char tab\)\)$")
_R_READ_VERBATIM = re.compile(r"^let (\w+) = \$l$")
_R_READ_TRIMMED = re.compile(r"^let (\w+) = \(\$l \| str trim\)$")


# The closure variable a loop iterates as.  The read variables are bound inside
# the body against this name, so it is fixed (and the parser refuses a `read`
# whose own variable would collide with it).
_LOOP_LINE = "l"


def _loop_tail(segs: list) -> bool:
    """True when these two segments are a loop tail (`each {|l| …} | ignore`)."""
    return (len(segs) == 2 and _word(segs[0], 0, "each") and len(segs[0]) == 2
            and segs[0][1].kind == "group" and segs[0][1].text == "{"
            and _ser(segs[1]) == "ignore")


def _loop_body(segs: list, refuse, chunk: str) -> tuple | None:
    """``each {|l| …bindings… body } | ignore`` → ``(variables, ifs, body)``.

    ``None`` when these two segments are not a loop tail at all.  The bindings
    are exactly the `let` lines ``ir_to_nu._read_bindings`` writes, so the
    variable list and the separator are read off them rather than guessed.

    The body's statements are separated by *newlines*, which the tokeniser drops
    like any other whitespace, so the body is sliced out of ``chunk`` by the
    closure's own offsets (the same way ``_for_stmt`` recovers its body).
    """
    if not _loop_tail(segs):
        return None
    group = segs[0][1]
    params, body_toks = _closure(group)
    if params != [_LOOP_LINE]:
        refuse(f"a loop `each` iterates as {_LOOP_LINE!r} alone: {params!r}")
    if not body_toks:
        refuse("a loop with an empty body")
    body_text = chunk[body_toks[0].pos:group.end - 1]
    statements = _statements(body_text)
    if not statements:
        refuse("a loop with an empty body")
    variables, ifs, rest = _read_bindings_of(statements, refuse)
    if not rest:
        refuse("a loop whose body only binds its variables")
    return variables, ifs, [_statement(st) for st in rest]


def _read_bindings_of(statements: list, refuse) -> tuple:
    """``(variables, ifs, body statements)`` for a loop's leading `let` lines."""
    variables: list = []
    ifs = None
    k = 0
    for st in statements:
        text = st.strip()
        m = _R_READ_VERBATIM.match(text)
        if m:
            variables, ifs = [m.group(1)], ""
        else:
            m = _R_READ_TRIMMED.match(text)
            if m:
                variables, ifs = [m.group(1)], None
            else:
                m = _R_READ_GET.match(text)
                if not m:
                    m = _R_READ_REST.match(text)
                if not m or int(m.group(2)) != len(variables):
                    break
                if len(variables) and ifs != "\t":
                    refuse(f"a mixed `read` binding: {text!r}")
                variables.append(m.group(1))
                ifs = "\t"
        k += 1
    if not variables:
        refuse(f"a loop whose first line is not a `read` binding: "
               f"{statements[0].strip()!r}")
    return variables, ifs, statements[k:]


def _file_loop(segs: list, chunk: str):
    """``open --raw "F" | lines | each {|l| … } | ignore`` → the loop op.

    Recognised before the walk starts, because a `.tsv`-named file handed to a
    `while` read is read as raw *lines* — that is what `read` does — and the
    ordinary source recogniser would insist on parsing such a path as a table.
    """
    if len(segs) != 4:
        return None
    m = _R_OPEN_RAW.match(_ser(segs[0]))
    if not m or _ser(segs[1]) != "lines":
        return None
    body = _loop_body(segs[2:], _raise, chunk)
    if body is None:
        return None
    variables, ifs, statements = body
    return Op("while_read", var=variables, ifs=ifs, path=_unq(m.group(1)),
              body=statements)


def _raise(why: str):
    raise ConversionError(why)


def _render_template(segs: list) -> str | None:
    """The segment that *renders* this pipeline's lines, or ``None``.

    Three terminators say where the lines go, and each one is followed back to
    the step that shapes them:

      * ``… | str join "\\n"``             — the stdout render;
      * ``… | str join "" | save [-a] "F"`` — a redirect or a `tee` write (the
        empty join, because each line already carries its own newline);
      * ``… | each {|l| … } | ignore``     — a `while` loop, whose lines are the
        step *before* the loop.
    """
    rest = list(segs)
    if len(rest) >= 3 and _loop_tail(rest[-2:]):
        rest = rest[:-2]
        return _ser(rest[-1]) if rest else None
    saved = False
    if len(rest) >= 3 and _join_empty(rest[-2]) \
            and _R_SAVE.match(_ser(rest[-1])):
        rest = rest[:-1]             # keep the join; only the save comes off
        saved = True
    if len(rest) < 2:
        return None
    if not (_join_empty(rest[-1]) if saved else (_join_nl(rest[-1]) or _join_empty(rest[-1]))):
        return None
    return _ser(rest[-2])


def _row_template(head: str) -> tuple | None:
    """Parse a row render template into ``(var, fields, sep, nl)``.

    ``None`` when the segment is not a row template at all.  ``sep`` is the real
    character the fields are joined with — a `find -printf` that prints a tab
    keeps printing tabs all the way down the pipeline, so the template carries
    the separator and not only the field names.  ``nl`` says whether the
    template ends with the ``(char nl)`` a redirect or a `tee` write appends.
    """
    # GNU uniq -c uses a seven-column count. The renderer preserves that
    # spacing, while the IR keeps the count itself numeric for later sorting.
    head = re.sub(r'\(\$(\w+)\.column0 \| fill -a right -w 7\)',
                  r'($\1.column0)', head)
    m = _R_EACH_TPL.match(head)
    if not m:
        return None
    var, inner = m.group(1), m.group(2)
    nl = inner.endswith("(char nl)")
    if nl:
        inner = inner[: -len("(char nl)")]
    if not inner:
        return None
    parts = re.split(r"(\\t| )", inner)
    if len(parts) % 2 == 0:
        return None                      # a separator with no field after it
    names, seps, vars_ = [], [], set()
    for k, part in enumerate(parts):
        if k % 2:
            seps.append(part)
            continue
        fm = _R_TPL_FIELD.fullmatch(part)
        if not fm:
            return None
        vars_.add(fm.group(1))
        names.append(fm.group(2))
    if vars_ != {var} or len(set(seps)) > 1:
        return None                      # mixed closure vars or separators
    return var, names, ("\t" if seps[:1] == ["\\t"] else " "), nl


def _hint_of(segs: list) -> tuple:
    """``(columns, separator)`` a pipeline's render template prints.

    ``(None, " ")`` when the template is not a row template (or there is none).
    Read *before* the walk: a find listing's print order and separator, a group's
    print tokens and a re-order's column permutation are visible only here, and
    the walk needs them to rebuild the op that produced the rows.
    """
    head = _render_template(segs)
    if head is None:
        return None, " "
    tpl = _row_template(head)
    if tpl is None:
        return None, " "
    return tpl[1], tpl[2]


def _pipeline_ops(segs: list, chunk: str) -> list:
    if not segs:
        raise ConversionError("empty pipeline")
    loop = _file_loop(segs, chunk)
    if loop is not None:
        return [loop]
    hint, sep = _hint_of(segs)
    r = _Rec(segs, hint, sep, chunk)
    r.source()
    while r.i < len(r.segs):
        if r.render_tail():
            break
        for m in _MATCHERS:
            if m(r):
                break
        else:
            r.refuse("no recognized Nushell step")
    if not r.ops:
        raise ConversionError("empty pipeline")
    if r.side_effect:
        return r.ops
    if r.ops[-1].op_type in ("render", "while_read"):
        # a line loop is a terminal stage: its body prints for itself, so the
        # pipeline has no render step of its own
        return r.ops
    if r.b.stream in ("value", "lines"):
        # A scalar or line render is written *outside* the segment list
        # (`print (…)`, `(…) | str join "\n"`); the op is still in the IR.
        r.ops.append(Op("render"))
        return r.ops
    raise ConversionError("pipeline without a render step")


# =====================================================================
# statements
# =====================================================================

def _operand_back(text: str) -> str:
    """A rendered test operand → the Bash word the parser would have read."""
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return "$" + text
    if _R_NUMERIC.fullmatch(text):
        return text
    m = _R_QUOTED.match(text)
    if m:
        return _unq(m.group(1))
    raise ConversionError(f"test operand {text!r} is not a rendered operand")


def _inner_segments(chunk: str, group: _Tok) -> list:
    """The segment list of a bracketed expression (offsets are into ``chunk``,
    the text the group itself was tokenised from)."""
    toks, _ = _tokenize(chunk, group.pos, _OPEN[group.text])
    return _segments(toks)


def _assign(chunk: str) -> Stmt:
    m = _R_LET.match(chunk.strip())
    if not m:
        raise ConversionError(f"unrecognized `let` statement: {chunk.strip()!r}")
    name, rhs = m.group(1), m.group(2).strip()
    rtoks, _ = _tokenize(rhs)
    if len(rtoks) == 1 and rtoks[0].kind in ("str", "word"):
        lit = _literal(rtoks[0])
        if lit is not None and not _is_capture_head(rtoks):
            return Stmt("assign", {"name": name, "value": lit})
    ops = _pipeline_ops(_segments(rtoks), rhs)
    if ops[-1].op_type != "render":
        raise ConversionError("a capture expression is not a pipeline")
    return Stmt("assign", {"name": name,
                           "value": "$(" + ir_to_bash(Script([Pipeline(ops)]))
                           + ")"})


def _is_capture_head(toks: list) -> bool:
    """A one-token expression can still be a pipeline head (`open …` is two
    tokens, but a bare command word is not a value literal)."""
    return toks[0].kind == "word" and _literal(toks[0]) is None


def _print(chunk: str, toks: list) -> Stmt | Pipeline:
    rest = toks[1:]
    if not rest:
        return Stmt("echo", {"text": ""})
    if len(rest) == 2 and rest[0].kind == "word" and rest[0].text == "-n" \
            and rest[1].kind == "group" and rest[1].text == "(":
        segs = _inner_segments(chunk, rest[1])
        if len(segs) == 1 and _R_OPEN_RAW.match(_ser(segs[0])):
            return Pipeline(_pipeline_ops(segs, chunk))
        if len(segs) < 2 or not _join_empty(segs[-1]):
            raise ConversionError('print -n is supported only for newline-preserving rendered pipelines')
        head = _ser(segs[-2])
        row = _row_template(head)
        if not (_R_TPL_VALS_NL.match(head) or (row is not None and row[3])):
            raise ConversionError('print -n requires the generated per-line newline template')
        ops = _pipeline_ops(segs, chunk)
        if ops[-1].op_type != "render":
            raise ConversionError('print -n pipeline has no render step')
        return Pipeline(ops)
    if len(rest) == 1 and rest[0].kind == "group" and rest[0].text == "(":
        ops = _pipeline_ops(_inner_segments(chunk, rest[0]), chunk)
        if ops[-1].op_type != "render":
            raise ConversionError("`print (…)` without a render step")
        return Pipeline(ops)
    s = _ser(rest)
    if re.fullmatch(r"\$\w+", s):
        return Stmt("echo", {"text": s})
    if len(rest) == 1 and rest[0].kind == "str":
        if rest[0].prefix != "$":
            if "$" in rest[0].text:
                raise ConversionError("a literal echo carrying `$` would have "
                                      "been rendered as an interpolated string")
            return Stmt("echo", {"text": _unq(rest[0].text)})
        # `$"a ($x) b"` — the renderer's interpolation of an echo text
        text = re.sub(r"\(\$(\w+)\)", r"$\1", rest[0].text)
        return Stmt("echo", {"text": text})
    if len(rest) == 1 and rest[0].kind == "word" \
            and not rest[0].text.startswith("-") and "$" not in rest[0].text:
        # The same argument-position rule as the file operations: `print done`
        # and `print "done"` are one Nushell (verified), so the bare spelling is
        # read too.  A leading `-` is a flag (`print -n` prints no newline, it
        # does not print "-n") and a `$` raises the interpolation question the
        # quoted branch above already refuses to answer, so both stay out.
        return Stmt("echo", {"text": rest[0].text})
    raise ConversionError(f"unrecognized echo form: {s!r}")


def _if_stmt(chunk: str, toks: list) -> Stmt:
    k = 1
    while k < len(toks) and not (toks[k].kind == "group"
                                 and toks[k].text == "{"):
        k += 1
    if k >= len(toks):
        raise ConversionError("if without a body block")
    cond_text = _ser_toks(toks[1:k])
    if cond_text.startswith("not "):
        # `ir_to_nu` writes `not (…)` only for `negate=True`, which the Bash
        # parser never produces — so this is not an IR it can have emitted.
        raise ConversionError("negated if-condition is not in the subset")
    m = _R_COND.match(cond_text)
    if not m:
        raise ConversionError(f"unrecognized if-condition {cond_text!r}")
    op = _TEST_OPS.get(m.group(2))
    if op is None:
        raise ConversionError(f"comparison {m.group(2)!r} has no test spelling")
    then_toks = toks[k]
    then_body = _block(chunk, then_toks)
    else_body: list = []
    if k + 1 < len(toks):
        if not _word(toks, k + 1, "else") or k + 2 >= len(toks):
            raise ConversionError("unrecognized text after an if body")
        else_body = _block(chunk, toks[k + 2])
    return Stmt("if", {"cond": (_operand_back(m.group(1)), op,
                                _operand_back(m.group(3))), "negate": False},
                body=[then_body, else_body])


def _block(chunk: str, group: _Tok) -> list:
    """The statements inside a `{ … }` block token (the reverse of
    ``ir_to_nu._body_to_nu``)."""
    if group.kind != "group" or group.text != "{":
        raise ConversionError("expected a `{ … }` block")
    return [_statement(c) for c in _statements(chunk[group.pos:group.end])]


def _for_stmt(chunk: str, toks: list) -> Stmt:
    if len(toks) != 6 or toks[1].kind != "op" or not _word(toks, 2, "each") \
            or toks[4].kind != "op" or not _word(toks, 5, "ignore"):
        raise ConversionError("unrecognized for-loop shape")
    items = []
    for item in toks[0].items:
        lit = _literal(item)
        if lit is None:
            raise ConversionError("for-loop items are not literals")
        items.append(lit)
    if not items:
        raise ConversionError("for loop without an explicit word list")
    group = toks[3]
    if group.kind != "group" or group.text != "{":
        raise ConversionError("for loop without a body block")
    params, body = _closure(group)
    if len(params) != 1 or not body:
        raise ConversionError("for loop without a loop variable")
    body_text = chunk[body[0].pos:group.end]
    return Stmt("for", {"var": params[0], "items": items},
                body=[_statement(c) for c in _statements(body_text)])


def _write_stmt(toks: list) -> Stmt:
    """``$"…" | save [-a] "F"`` — `ir_to_nu`'s rendering of a ``write`` (the
    formatted line a `printf … > file` puts on disk)."""
    rest = toks[1:]
    if len(rest) < 2 or rest[0].kind != "op" or rest[0].text != "|":
        raise ConversionError("a bare interpolated string is not a statement")
    m = _R_SAVE.match(_ser(rest[1:]))
    if not m:
        raise ConversionError(f"unrecognized `save` form: {_ser(rest[1:])!r}")
    text = _nu_to_text(toks[0].text)
    if not text.endswith("\n"):
        raise ConversionError(
            "a saved line that does not end in a newline is not a `write` the "
            "renderer emits (the statement is one whole line)")
    return Stmt("write", {"text": text, "path": _argval(m.group(2)),
                          "append": m.group(1) == "-a"})


def _statement(chunk: str) -> Stmt | Pipeline:
    counted = re.fullmatch(
        r'do \{ let rosetta_grep_count = \((.*)\); print \$rosetta_grep_count; '
        r'if \$rosetta_grep_count == 0 \{ exit 1 \} \}', chunk.strip(), re.DOTALL)
    if counted:
        body = counted.group(1)
        toks, _ = _tokenize(body)
        ops = _pipeline_ops(_segments(toks), body)
        if len(ops) < 3 or ops[-3].op_type != 'grep' or ops[-2].op_type != 'count':
            raise ConversionError('invalid grep count status wrapper')
        ops[-3].params['count'] = True
        del ops[-2]
        return Pipeline(ops)
    wrapped = re.fullmatch(
        r'do \{ let rosetta_grep_matches = \((.*)\); if \(\$rosetta_grep_matches \| is-empty\) '
        r'\{ exit 1 \}; \$rosetta_grep_matches \| each \{\|x\| \$"\(\$x\)\(char nl\)"\} '
        r'\| str join "" \}', chunk.strip(), re.DOTALL)
    if wrapped:
        body = wrapped.group(1)
        toks, _ = _tokenize(body)
        ops = _pipeline_ops(_segments(toks), body)
        if len(ops) < 2 or ops[-2].op_type != 'grep' or ops[-2].params.get('count'):
            raise ConversionError('invalid grep status wrapper')
        return Pipeline(ops)
    toks, _ = _tokenize(chunk)
    if not toks:
        raise ConversionError("empty statement")
    head = _head_word(toks)
    if head == "let":
        return _assign(chunk)
    if head == "print":
        return _print(chunk, toks)
    if toks[0].kind == "str" and toks[0].prefix == "$":
        # a `$"…"` statement is only ever the `write` render
        return _write_stmt(toks)
    if head == "if":
        return _if_stmt(chunk, toks)
    if toks[0].kind == "group" and toks[0].text == "[":
        return _for_stmt(chunk, toks)
    segs = _segments(toks)
    if len(segs) > 1 and len(segs[0]) == 1 and segs[0][0].kind == "group" \
            and segs[0][0].text == "(" and _join_nl(segs[-1]):
        # A line-stream render is written `(<pipeline>) | str join "\n"`.
        segs = _inner_segments(chunk, segs[0][0]) + segs[1:]
    return Pipeline(_pipeline_ops(segs, chunk))


# =====================================================================
# top-level API
# =====================================================================

def _passthrough(chunks: list) -> Stmt:
    """One opaque region: the run of adjacent unclassifiable statements.

    Adjacent fragments are one region, not several, because that is what they
    are in the delivered artifact -- a contiguous block of Nu-only work -- and
    because ``converter._extract_passthrough`` reads a block back as one text.
    ``strip`` matches that reader's own ``.strip()`` on the joined block, so the
    Nu that comes back is the Nu that went in (Nu is not indentation-sensitive
    at statement level, so nothing is lost by trimming the ends).
    """
    text = "\n".join(chunks).strip()
    if not text:
        raise ConversionError("an empty statement is not a pass-through region")
    return Stmt("passthrough", {"text": text})


def nu_to_ir(nu_text: str, *, carry_whole: bool = False) -> Script:
    """Recover the shell IR of a Nu 0.114.1 script in the controlled subset.

    A statement outside the subset is carried verbatim as a ``passthrough``
    statement rather than refusing the document -- but only alongside at least
    one statement that *did* classify, since the carried region is the
    supplement to a translation and never a substitute for one (see the module
    docstring).  A document in which nothing classifies raises its first
    failure, unchanged.

    ``carry_whole`` is the one caller-selectable exception, and it belongs to
    the *projection* rather than to the reading: ``converter.nu_to_bash``, whose
    contract is that the delivered Bash preserves the document, not that the
    document is in the subset -- asks for it, so hand-written Nu that uses
    nothing the layer knows (`parse --regex`, `to text`, a comment-only file)
    projects as one opaque region instead of being refused.  The strict default
    is what keeps the layer loud: a caller that wants a *reading* still gets a
    refusal when there is no reading to be had, and a lexically broken document
    is refused either way (``_statements`` runs first and its refusals stay
    fatal).  Carrying is still not guessing: the region holds the Nu verbatim
    and nothing is derived from it.

    Raises ``ConversionError`` for anything the subset cannot read: a Nu step
    whose IR the Bash parser could not have produced, a param only the original
    Bash text could supply, a construct ``ir_to_nu`` cannot emit at all, or a
    lexically broken document.  The LLM is never consulted, nothing outside a
    carried region is guessed, and a partial reading of a statement is never
    returned as a whole one.
    """
    if not isinstance(nu_text, str):
        raise ConversionError("nu_to_ir expects Nu source text")
    chunks = _statements(nu_text)          # lexical refusals stay fatal
    out: list = []
    carried: list = []
    first: ConversionError | None = None
    opaque = 0
    for chunk in chunks:
        try:
            st = _statement(chunk)
        except ConversionError as e:
            first = first or e
            opaque += 1
            # the chunk goes in as it stands: ``_passthrough`` trims the ends of
            # the joined region (as the block's own reader does), and trimming
            # every fragment on the way in would eat the blank lines *between*
            # fragments -- text the region is supposed to preserve verbatim
            carried.append(chunk)
            continue
        if carried:
            out.append(_passthrough(carried))
            carried = []
        out.append(st)
    if carried:
        out.append(_passthrough(carried))
    if first is not None and opaque == len(chunks) and not carry_whole:
        raise first
    return Script(out)
