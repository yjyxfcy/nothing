"""Shell IR → Bash generator.

The reverse leg of the compat layer: ``bash_to_ir`` recognizes the controlled
Bash subset into the shell IR, ``ir_to_nu`` renders it to Nu 0.114.1, and this
module renders the same IR back to Bash.  Together they make the layer
bidirectional (Bash ⇄ IR ⇄ Nu), so a delivered Nu script can be shown as the
Bash it corresponds to without a second, hand-written source of truth.

Fidelity contract: the text emitted here must *re-parse to the identical IR* —
``bash_to_ir(ir_to_bash(ir))`` is structurally equal to ``ir``.  That is a
stronger and narrower promise than "matches the original script": the IR is a
normal form, so several Bash spellings collapse into one IR (``mkdir x`` and
``mkdir -p x``; a header skip written as ``tail -n +2`` or as an awk ``NR > 1``
prologue) and any of them may be emitted.  What is never allowed is a *silent
mistranslation*, so every field the parser re-derives rather than reads — the
source kind from the file extension, a sort key's column/flags, the shape of an
awk program — is checked against the parser's own derivation rules before it is
emitted, and any IR the subset cannot express exactly raises
``ConversionError`` instead of being approximated.

The one exception is the ``passthrough`` statement, and it is an exception on
purpose.  Every other statement here is a *translation*, so re-parsing it is how
the promise is kept; a pass-through is the opposite, a region this leg is
forbidden to translate, and it is emitted as the marked comment block that
``converter`` reads back (see ``_passthrough_cmd``).  ``bash_to_ir`` alone
therefore drops it — it has no marker vocabulary and never sees one, since
``bash_to_nu`` strips the blocks before parsing — so ``bash_to_ir ∘ ir_to_bash``
is the identity on the translated part and empty on the carried part.  The
round trip *is* exact, one level up: ``converter.bash_to_nu`` puts the fragment
back, byte for byte, which is the only reader that claims it.
"""
from __future__ import annotations

import re

from .bash_to_ir import _file_ext_kind, _looks_file
from .ir import ConversionError, Op, Pipeline, Script, Stmt


# ---------------------------------------------------------------------------
# the pass-through marker vocabulary
#
# A `passthrough` statement (see `ir.Stmt`) is Nu that Bash cannot spell, so it
# travels as comments between these two markers.  The words are defined here,
# where Bash is written, and `converter` builds the patterns that read them back
# out of them -- one spelling, two readers, so the writer and the reader cannot
# drift apart.
# ---------------------------------------------------------------------------

_PT_MARK_START = "@NU-PASSTHROUGH"
_PT_MARK_END = "@END-NU-PASSTHROUGH"
_PT_START = "# " + _PT_MARK_START
_PT_END = "# " + _PT_MARK_END


# ---------------------------------------------------------------------------
# Bash word rendering
# ---------------------------------------------------------------------------

_SIMPLE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def _text(v, what: str) -> str:
    if not isinstance(v, str):
        raise ConversionError(f"{what} {v!r} is not a string")
    return v


def _literal(s: str) -> str:
    """One Bash word carrying ``s`` verbatim (no expansion on re-parse)."""
    if not isinstance(s, str):
        raise ConversionError(f"cannot render non-string {s!r} as a shell word")
    if not s:
        raise ConversionError("cannot render an empty shell word")
    if _SIMPLE.fullmatch(s):
        return s
    if "'" in s or "\n" in s:
        raise ConversionError(f"cannot quote {s!r} as one literal Bash word")
    return "'" + s + "'"


def _expandable(s: str) -> str:
    """One Bash word carrying ``s``, keeping ``$var`` a live reference.

    Used for the places the subset *means* to expand — echo text, assignment
    values, test operands — where single-quoting would round-trip through
    bashlex but freeze the variable at run time.
    """
    if not isinstance(s, str):
        raise ConversionError(f"cannot render non-string {s!r} as a shell word")
    if _SIMPLE.fullmatch(s):
        return s
    if '"' not in s and "\\" not in s and "`" not in s:
        return '"' + s + '"'
    if "'" in s or "\n" in s:
        raise ConversionError(f"cannot quote {s!r} as one Bash word")
    return "'" + s + "'"


def _strip_unsafe(s, what: str) -> None:
    """Guard a value the parser normalizes with ``.strip("'\\"")``: rendering
    one that begins or ends with a quote would come back shorter."""
    if not isinstance(s, str):
        raise ConversionError(f"{what} {s!r} is not a string")
    if s != s.strip("'\"") or not s:
        raise ConversionError(
            f"{what} {s!r} cannot round-trip through the parser's quote strip")


def _need(op: Op, *names: str, optional: tuple = ()) -> None:
    """Require exactly the params the parser writes for this op type.

    ``ir_to_bash`` promises IR *equality*: a param the parser would re-derive
    (or a default it would fill in) means the op cannot be shown to round-trip,
    so a missing or surplus one is refused rather than papered over.
    """
    missing = [n for n in names if n not in op.params]
    surplus = sorted(set(op.params) - set(names) - set(optional))
    if missing or surplus:
        raise ConversionError(
            f"{op.op_type} op has the wrong params: missing {missing}, "
            f"unexpected {surplus}")


def _keys(mapping, what: str, *names: str) -> dict:
    """Require exactly ``names`` as the keys of a recognized sub-mapping."""
    if not isinstance(mapping, dict):
        raise ConversionError(f"{what} {mapping!r} is not a mapping")
    missing = [n for n in names if n not in mapping]
    surplus = sorted(set(mapping) - set(names))
    if missing or surplus:
        raise ConversionError(
            f"{what} has the wrong keys: missing {missing}, unexpected {surplus}")
    return mapping


def _count(v, what: str) -> int:
    """A non-negative integer that renders as a plain number."""
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ConversionError(f"{what} {v!r} is not a non-negative integer")
    return v


def _flag(v, what: str) -> bool:
    """A real bool — a truthy non-bool would render as a flag that the parser
    then records as ``True``, quietly changing the op."""
    if not isinstance(v, bool):
        raise ConversionError(f"{what} {v!r} is not a boolean")
    return v


_SIDE_EFFECT = {"mkdir", "cp", "mv", "rm", "touch", "truncate"}
_AWK_OPS = {"awk_get", "awk_fget", "agg", "group_count", "group_sum",
            "uniq_reorder", "awk_program", "filter", "bucket"}
_COMPARE = (">", ">=", "<", "<=", "==", "!=")
# ops that leave the row shape alone, so a `uniq -c` table can still be the
# thing a trailing `uniq_reorder` awk is re-ordering
_SHAPE_KEEPING = {"sort", "head", "tail", "grep", "count"}
_TEST_OPS = ("=", "==", "!=", "-gt", "-lt", "-ge", "-le", "-eq", "-ne")


# ---------------------------------------------------------------------------
# pipelines: data sources
# ---------------------------------------------------------------------------

def _read_cmd(op: Op) -> str:
    _need(op, "kind", "path", "skip_header")
    p = op.params
    kind, path = p["kind"], p["path"]
    skip = p.get("skip_header", 0)
    # The source kind is not carried by the command — the parser re-derives it
    # from the path extension, so an op whose kind disagrees has no rendering.
    if _file_ext_kind(path) != kind:
        raise ConversionError(
            f"read kind {kind!r} is not what path {path!r} re-derives")
    if skip <= 0:
        return "cat " + _literal(path)
    if not _looks_file(path):
        raise ConversionError(
            f"header skip on {path!r} has no `tail -n +N` rendering")
    return f"tail -n +{skip + 1} " + _literal(path)


def _find_cmd(ops: list, i: int) -> tuple[str, int]:
    """One `find` command from the source_find/f_* op run."""
    op = ops[i]
    args = ["find", _literal(op.params["path"])]
    i += 1
    if i < len(ops) and ops[i].op_type == "f_filter_file":
        args += ["-type", "f"]
        i += 1
    if i >= len(ops) or ops[i].op_type not in ("f_list", "f_size", "f_gt_size"):
        raise ConversionError("find source without a listing op")
    listing = ops[i]
    i += 1
    # `-printf '%s %p\n'` is what makes find print size-then-path; without it
    # the same -size filter prints paths only.
    order = listing.params.get("print_order")
    if listing.op_type == "f_gt_size":
        _need(listing, "size", "print_order", optional=("sep",))
        size = listing.params["size"]
        if size % 1024 == 0:
            args += ["-size", f"+{size // 1024}k"]
        elif size % (1024 * 1024) == 0:
            args += ["-size", f"+{size // (1024 * 1024)}M"]
        else:
            raise ConversionError(
                f"-size {size} is not a whole -size +Nk / +NM")
        want = ["sz", "name"] if order == ["sz", "name"] else ["name"]
    elif listing.op_type == "f_size":
        _need(listing, "print_order", optional=("sep",))
        want = ["sz", "name"]
    else:
        _need(listing, "print_order", optional=("sep",))
        want = ["name"]
    if order != want:
        raise ConversionError(
            f"find print order {order!r} has no -printf rendering")
    sep = listing.params.get("sep", " ")
    if sep not in (" ", "\t"):
        raise ConversionError(f"find separator {sep!r} has no -printf rendering")
    if want == ["sz", "name"]:
        # `\t` is find's own escape for a tab in a -printf spec; it is what the
        # parser reads back as the tab separator, so it is what gets emitted.
        spec = "%s\\t%p\\n" if sep == "\t" else "%s %p\\n"
        args += ["-printf", _literal(spec)]
    elif sep != " ":
        raise ConversionError(
            "a separator only applies to the size-then-path -printf form")
    return " ".join(args), i


# ---------------------------------------------------------------------------
# pipelines: awk stages
# ---------------------------------------------------------------------------

def _awk(prog: str) -> str:
    """Wrap an awk program as one single-quoted Bash word.

    Every program below ends up here, so a quote anywhere in a column name, a
    pattern or a field value is refused once rather than breaking out of the
    shell word.
    """
    if "'" in prog or "\n" in prog:
        raise ConversionError(f"awk program {prog!r} cannot be quoted")
    return "awk '" + prog + "'"


def _cols_expr(cols) -> str:
    if not isinstance(cols, list) or not cols:
        raise ConversionError(f"awk print columns {cols!r} are not a list")
    return ", ".join(f"${_count(c, 'awk column') + 1}" for c in cols)


def _awk_select(cols: list) -> str:
    return _awk(f"{{print {_cols_expr(cols)}}}")


def _awk_filter(fcol: int, o: str, val: str, cols: list) -> str:
    """`$c <op> val {print ...}` — the filter_select shape."""
    _count(fcol, "awk field column")
    _text(val, "awk filter value")
    expr = _cols_expr(cols)
    if o == "~":
        if "/" in val:
            raise ConversionError(f"awk `~` pattern {val!r} contains a slash")
        return _awk(f"${fcol + 1} ~ /{val}/ {{print {expr}}}")
    if o not in _COMPARE:
        raise ConversionError(f"unsupported awk filter operator {o!r}")
    if re.search(r"\s", val) or "{" in val or "}" in val:
        raise ConversionError(f"awk filter value {val!r} is not one token")
    return _awk(f"${fcol + 1} {o} {val} {{print {expr}}}")


def _filter_cmd(ops: list, i: int) -> tuple[str, int]:
    """A `filter` op is only ever half of one awk: the parser emits the row
    filter and the column projection as two ops from a single program."""
    op = ops[i]
    if i + 1 >= len(ops) or ops[i + 1].op_type != "awk_get":
        raise ConversionError("filter op without its following awk_get")
    _need(op, "col", "op", "val")
    _need(ops[i + 1], "cols")
    p = op.params
    return (_awk_filter(p["col"], p["op"], p["val"],
                        ops[i + 1].params["cols"]), i + 2)


def _group_cmd(op: Op, mode: str) -> str:
    p = op.params
    if mode == "count":
        _need(op, "key", "print")
    else:
        _need(op, "key", "val", "print")
    key = _count(p["key"], "group key column")
    val = _count(p["val"], "group value column") if mode == "sum" else None
    arr, var = "a", "k"
    if mode == "count":
        head = f"{{{arr}[${key + 1}]++}}"
        default = ["key", "count"]
    else:
        head = f"{{{arr}[${key + 1}]+=${val + 1}}}"
        default = ["sum", "key"]
    toks = p["print"] or default
    if not isinstance(p["print"], list) or any(t not in ("key", mode) for t in toks):
        raise ConversionError(f"group print order {p['print']!r} is not a token list")
    exprs = []
    for tok in toks:
        if tok == "key":
            exprs.append(var)
        elif tok == mode:
            exprs.append(f"{arr}[{var}]")
        else:
            raise ConversionError(f"group print token {tok!r} not in mode {mode}")
    return _awk(f"{head} END {{for ({var} in {arr}) "
                f"print {', '.join(exprs)}}}")


def _agg_cmd(op: Op, read_op: Op) -> str:
    p = op.params
    mode = p.get("mode")
    if mode == "sum":
        _need(op, "mode", "sumcol")
        return _awk(f"{{s+=${_count(p['sumcol'], 'sum column') + 1}}} "
                    f"END {{print s}}")
    if mode == "avg":
        _need(op, "mode", "fcol", "val", "sumcol", "rint")
        fcol = _count(p["fcol"], "avg filter column")
        val = _text(p["val"], "avg compare value")
        sumcol = _count(p["sumcol"], "avg sum column")
        if '"' in val:
            raise ConversionError(f"awk compare value {val!r} contains a quote")
        if p["rint"] not in (None, "int"):
            raise ConversionError(f"agg rint {p['rint']!r} is not None or 'int'")
        if p["rint"] == "int":
            # `%d` truncation is only recognized with a leading `NR > 1`, which
            # also forces the read op's header skip on re-parse.
            if read_op.params.get("skip_header", 0) < 1:
                raise ConversionError(
                    "avg with %d truncation requires a header-skipping source")
            return _awk(f"NR > 1 && ${fcol + 1}==\"{val}\" "
                        f"{{s+=${sumcol + 1}; n++}} "
                        f'END {{if (n>0) printf "%d\\n", s/n}}')
        return _awk(f"${fcol + 1}==\"{val}\" {{s+=${sumcol + 1}; n++}} "
                    f'END {{printf "%.0f\\n", s/n}}')
    if mode == "count":
        _need(op, "mode", "fcol", "val")
        fcol = _count(p["fcol"], "count filter column")
        val = _text(p["val"], "count compare value")
        if '"' in val:
            raise ConversionError(f"awk compare value {val!r} contains a quote")
        return _awk(f"${fcol + 1}==\"{val}\" {{n++}} END {{print n + 0}}")
    raise ConversionError(f"unknown agg mode {mode!r}")


def _uniq_reorder_cmd(op: Op) -> str:
    """`uniq -c` rows are [count, line]; the trailing awk re-orders/filters them.

    Two programs produce this op (a `$1 >= 3 {print $2, $1}` filter-select and
    the equivalent variable-assignment form); only the first is emitted.
    """
    _need(op, "cols", "filter")
    cols = op.params["cols"]
    expr = _cols_expr(cols)
    filt = op.params["filter"]
    if filt is None:
        return _awk(f"{{print {expr}}}")
    if not isinstance(filt, tuple) or len(filt) != 3:
        raise ConversionError(f"awk filter {filt!r} is not a 3-tuple")
    col, o, val = _count(filt[0], "awk filter column"), filt[1], \
        _text(filt[2], "awk filter value")
    if o == "~":
        if "/" in val:
            raise ConversionError(f"awk `~` pattern {val!r} contains a slash")
        return _awk(f"${col + 1} ~ /{val}/ {{print {expr}}}")
    if o not in _COMPARE:
        raise ConversionError(f"unsupported awk filter operator {o!r}")
    if re.search(r"\s", val) or "{" in val or "}" in val:
        raise ConversionError(f"awk filter value {val!r} is not one token")
    return _awk(f"${col + 1} {o} {val} {{print {expr}}}")


# ---------------------------------------------------------------------------
# pipelines: jq
# ---------------------------------------------------------------------------

def _jq_cond(c) -> str:
    _keys(c, "jq condition", "field", "op", "val", "cast", "default")
    if not _IDENT.fullmatch(_text(c["field"], "jq field")):
        raise ConversionError(f"jq field {c['field']!r} is not an identifier")
    if c["op"] not in _COMPARE:
        raise ConversionError(f"jq operator {c['op']!r} is not a comparison")
    # the value is jq syntax (quotes and all), not a shell word — only the
    # one-token rule the parser's `\S+` imposes applies here
    if re.search(r"\s", _text(c["val"], "jq value")):
        raise ConversionError(f"jq value {c['val']!r} is not one token")
    expr = "." + c["field"]
    if c["default"] is not None:
        d = _text(c["default"], "jq default")
        if not re.fullmatch(r"\d+", d):
            raise ConversionError(f"jq default {d!r} is not a digit string")
        expr = f"({expr} // {d})"
    if _flag(c["cast"], "jq cast"):
        expr = f"({expr} | tonumber)"
    return f"{expr} {c['op']} {c['val']}"


def _jq_cmd(op: Op, path: str) -> str:
    _need(op, "conds", "fields", "first")
    p = op.params
    conds, fields, first = p["conds"], p["fields"] or [], p["first"]
    if not conds:
        raise ConversionError("jq_where without a condition")
    cond = " and ".join(_jq_cond(c) for c in conds)
    if not isinstance(fields, list) \
            or any(not _IDENT.fullmatch(str(f)) for f in fields):
        raise ConversionError(f"jq projection {fields!r} is not a field list")
    # `\(.field)` — jq's interpolation; bashlex sees it as plain text inside the
    # single-quoted program, so the backslash survives both legs.
    template = '"' + " ".join(f"\\(.{f})" for f in fields) + '"'
    if first is not None:
        filt = f"[.[] | select({cond})][0:{_count(first, 'jq slice')}][]"
        if fields:
            filt += " | " + template
    elif fields:
        filt = f".[] | select({cond}) | {template}"
    else:
        raise ConversionError("jq_where with neither a projection nor a slice")
    return f"jq -r {_literal(filt)} {_literal(path)}"


def _jq_group_cmd(op: Op, path: str) -> str:
    """The parser keeps only the group field and the render template of a
    group_by filter; the template is re-emitted verbatim as the quoted string."""
    _need(op, "field", "template")
    field = _text(op.params["field"], "jq group field")
    template = _text(op.params["template"], "jq group template")
    if not _IDENT.fullmatch(field) or '"' in template or "\n" in template:
        raise ConversionError(f"cannot render jq group_by {field!r}")
    filt = "group_by(." + field + ')[] | "' + template + '"'
    return f"jq -r {_literal(filt)} {_literal(path)}"


# ---------------------------------------------------------------------------
# pipelines: sort / slices / text filters
# ---------------------------------------------------------------------------

def _sort_cmd(op: Op) -> str:
    _need(op, "numeric", "reverse", "col", "unique", optional=("keys",))
    p = op.params
    col = p.get("col")
    keys = p.get("keys")
    numeric = _flag(p["numeric"], "sort numeric")
    reverse = _flag(p["reverse"], "sort reverse")
    args = ["sort", "-u"] if _flag(p["unique"], "sort unique") else ["sort"]
    if col is None:
        if keys:
            raise ConversionError("sort keys without a primary column")
        args += ["-n"] if numeric else []
        args += ["-r"] if reverse else []
        return " ".join(args)
    if not keys:
        raise ConversionError("sort column without a key list")
    # The parser reads the op's own numeric/reverse off the *first* key, and
    # spreads a global -n/-r onto every key that carries no flag of its own.
    kcols = []
    for k in keys:
        k = _keys(k, "sort key", "col", "numeric", "reverse")
        kcols.append({"col": _count(k["col"], "sort key column"),
                      "numeric": _flag(k["numeric"], "sort key numeric"),
                      "reverse": _flag(k["reverse"], "sort key reverse")})
    if kcols[0]["col"] != col or kcols[0]["numeric"] != numeric \
            or kcols[0]["reverse"] != reverse:
        raise ConversionError("sort column/flags disagree with the key list")
    if numeric and not any(k["numeric"] for k in kcols):
        raise ConversionError("global -n would not survive as a per-key flag")
    if reverse and not any(k["reverse"] for k in kcols):
        raise ConversionError("global -r would not survive as a per-key flag")
    for k in kcols:
        flags = ("n" if k["numeric"] else "") + ("r" if k["reverse"] else "")
        c = k["col"] + 1
        args.append(f"-k{c},{c}{flags}")
    return " ".join(args)


def _cut_cmd(op: Op) -> str:
    _need(op, "delim", "col")
    delim = _text(op.params["delim"], "cut delimiter")
    col = _count(op.params["col"], "cut column")
    if delim == "\t":
        return f"cut -f{col + 1}"           # POSIX cut's default delimiter
    if len(delim) == 1 and not delim.isspace() and delim not in "'\"\\":
        return f"cut -d{delim} -f{col + 1}"
    raise ConversionError(f"cut delimiter {delim!r} has no `-d` rendering")


def _sed_cmd(op: Op) -> str:
    _need(op, "pat", "rep", "regex", "keep_matched")
    p = op.params
    if not _flag(p["regex"], "sed regex"):
        raise ConversionError("sed without the ERE normalization")
    keep = _flag(p["keep_matched"], "sed keep_matched")
    pat = _text(p["pat"], "sed pattern")
    rep = _text(p["rep"], "sed replacement")
    for s in (pat, rep):
        if "'" in s or "\n" in s or s.endswith("\\"):
            raise ConversionError(f"sed text {s!r} cannot be quoted")
        if re.search(r"\\[(){}]", s):
            # the parser normalizes BRE group/quantifier escapes away
            raise ConversionError(f"sed text {s!r} is not in normalized ERE form")
    d = "/"
    if "/" in pat or "/" in rep:
        if "|" in pat or "|" in rep:
            raise ConversionError("sed text carries both delimiters")
        d = "|"
    flags = "p" if keep else ""
    return f"sed 's{d}{pat}{d}{rep}{d}{flags}'"


def _transform_cmd(op: Op, read_op: Op) -> str:
    t, p = op.op_type, op.params
    if t == "count":
        _need(op)
        return "wc -l"
    if t == "head":
        _need(op, "n")
        return f"head -n {_count(p['n'], 'head')}"
    if t == "tail":
        _need(op, "n")
        return f"tail -n {_count(p['n'], 'tail')}"
    if t == "tr":
        _need(op, "mode")
        if p["mode"] == "upper":
            return "tr 'a-z' 'A-Z'"
        if p["mode"] == "lower":
            return "tr 'A-Z' 'a-z'"
        raise ConversionError(f"unsupported tr mode {p['mode']!r}")
    if t == "cut":
        return _cut_cmd(op)
    if t == "grep":
        _need(op, "pat", "invert", "count")
        _strip_unsafe(p["pat"], "grep pattern")
        args = ["grep"]
        if _flag(p["invert"], "grep invert"):
            args.append("-v")
        if _flag(p["count"], "grep count"):
            args.append("-c")
        word = _literal(p["pat"])
        if word.startswith("-"):
            # the parser takes the first non-flag word as the pattern
            raise ConversionError(f"grep pattern {p['pat']!r} reads as a flag")
        return " ".join(args + [word])
    if t == "sed":
        return _sed_cmd(op)
    if t == "sort":
        return _sort_cmd(op)
    if t == "uniq":
        _need(op, optional=("global_dedup",))
        return "awk '!seen[$0]++'" if op.params.get("global_dedup") else "uniq"
    if t == "uniq_c":
        _need(op)
        return "uniq -c"
    if t == "uniq_reorder":
        return _uniq_reorder_cmd(op)
    if t == "awk_get":
        _need(op, "cols")
        return _awk_select(p["cols"])
    if t == "awk_fget":
        _need(op, "fcol", "op", "val", "cols")
        return _awk_filter(p["fcol"], p["op"], p["val"], p["cols"])
    if t == "agg":
        return _agg_cmd(op, read_op)
    if t == "group_count":
        return _group_cmd(op, "count")
    if t == "group_sum":
        return _group_cmd(op, "sum")
    if t in ("jq_where", "jq_group"):
        # only reachable at the head of a pipeline, where it fuses with `read`
        raise ConversionError(f"{t} is not a pipeline filter")
    raise ConversionError(f"no Bash rendering for op {t!r}")


def _side_effect(op: Op) -> str:
    t, p = op.op_type, op.params
    if t == "mkdir":
        _need(op, "path")
        if p["path"] == "-p":
            raise ConversionError("mkdir path '-p' reads as a flag")
        return "mkdir " + _literal(p["path"])
    if t == "cp":
        _need(op, "src", "dst")
        return "cp " + _literal(p["src"]) + " " + _literal(p["dst"])
    if t == "mv":
        _need(op, "src", "dst")
        return "mv " + _literal(p["src"]) + " " + _literal(p["dst"])
    if t == "rm":
        _need(op, "path", "rec")
        word = _literal(p["path"])
        if word.startswith("-"):
            raise ConversionError(f"rm path {p['path']!r} reads as a flag")
        return ("rm -r " if _flag(p["rec"], "rm rec") else "rm ") + word
    if t == "touch":
        _need(op, "path")
        return "touch " + _literal(p["path"])
    if t == "truncate":
        _need(op, "path")
        return ": > " + _literal(p["path"])
    raise ConversionError(f"no Bash rendering for file op {t!r}")


def _while_read_cmd(ops: list) -> str:
    """`while IFS= read -r VARS; do BODY; done [< FILE]` — the line loop.

    The loop is the pipeline's last stage either way: a file after the `done`,
    or whatever the stages before it print.
    """
    loop = ops[-1]
    _need(loop, "var", "ifs", "path", "body")
    variables = list(loop.params["var"])
    if not variables or not all(_IDENT.fullmatch(v) for v in variables):
        raise ConversionError(f"read variables {variables!r} are not identifiers")
    ifs = loop.params["ifs"]
    prefix = ""
    if ifs == "\t":
        # `$'\t'` is how a tab reaches `read` through a shell word
        prefix = "IFS=$'\\t' "
    elif ifs == "":
        prefix = "IFS= "
    elif ifs is not None:
        raise ConversionError(f"IFS value {ifs!r} has no Bash rendering")
    body = loop.params["body"]
    if not body:
        raise ConversionError("while loop with an empty body")
    out = "while " + prefix + "read -r " + " ".join(variables) \
        + "; do " + _body_inline(body) + "; done"
    path = loop.params["path"]
    if path is not None:
        if len(ops) != 1:
            raise ConversionError("a while loop reading a file cannot also "
                                  "have a pipeline feeding it")
        return out + " < " + _literal(path)
    if len(ops) == 1:
        raise ConversionError("a while loop with nothing feeding it")
    # the stages feeding the loop are rendered as an ordinary pipeline; Bash has
    # no explicit render step, so the prefix's own render op is implicit
    return _render_pipeline(Pipeline(ops[:-1] + [Op("render")])) + " | " + out


def _render_pipeline(pl: Pipeline) -> str:
    ops = pl.ops if isinstance(pl.ops, list) else [pl.ops]
    if not ops:
        raise ConversionError("empty pipeline")
    if all(o.op_type in _SIDE_EFFECT for o in ops):
        if len(ops) != 1:
            raise ConversionError("file ops cannot share a pipeline")
        return _side_effect(ops[0])
    if ops[-1].op_type == "while_read":
        # a line loop is the pipeline's last stage and prints for itself
        return _while_read_cmd(ops)
    if ops[-1].op_type != "render":
        raise ConversionError("pipeline without a trailing render op")
    _need(ops[-1], optional=("path", "append"))
    body = ops[:-1]
    if not body or body[0].op_type not in ("read", "source_find"):
        raise ConversionError("pipeline does not lead with a data source")
    if body[0].op_type == "source_find" \
            and any(o.op_type in _AWK_OPS for o in body[1:]):
        raise ConversionError("awk over find output is not supported")
    cmds: list[str] = []
    i = 0
    if len(body) > 1 and body[0].op_type == "read" \
            and body[1].op_type in ("jq_where", "jq_group"):
        # `jq FILTER FILE` is a *source-introducing* command, not a filter on an
        # already-open stream: the read op and the jq op are one Bash command.
        _need(body[0], "kind", "path", "skip_header")
        path = body[0].params["path"]
        if body[0].params["skip_header"]:
            raise ConversionError("jq source cannot skip a header")
        if _file_ext_kind(path) != body[0].params["kind"]:
            raise ConversionError(
                f"read kind {body[0].params['kind']!r} is not what path "
                f"{path!r} re-derives")
        if body[1].op_type == "jq_where":
            cmds.append(_jq_cmd(body[1], path))
        else:
            cmds.append(_jq_group_cmd(body[1], path))
        i = 2
    while i < len(body):
        op = body[i]
        if op.op_type in ("read", "source_find") and i != 0:
            # a second source op is not a Bash command the parser recognizes;
            # emitting one would drop the op on re-parse
            raise ConversionError(f"mis-placed {op.op_type} op")
        if op.op_type == "read":
            cmds.append(_read_cmd(op))
            i += 1
        elif op.op_type == "source_find":
            cmd, i = _find_cmd(body, i)
            cmds.append(cmd)
        elif op.op_type == "filter":
            cmd, i = _filter_cmd(body, i)
            cmds.append(cmd)
        elif op.op_type == "tee":
            _need(op, "path", "append")
            flag = "-a " if _flag(op.params["append"], "tee append") else ""
            cmds.append("tee " + flag + _literal(op.params["path"]))
            i += 1
        elif op.op_type in _SIDE_EFFECT:
            raise ConversionError("file op inside a data pipeline")
        else:
            if op.op_type == "uniq_reorder":
                # its meaning is "re-order the [count, line] rows uniq -c made",
                # so a pipeline whose last shape-setting op is anything else
                # would re-parse that awk as a plain column select
                j = i - 1
                while j > 0 and body[j].op_type in _SHAPE_KEEPING:
                    j -= 1
                if body[j].op_type != "uniq_c":
                    raise ConversionError(
                        "uniq_reorder op does not follow a uniq -c op")
            cmds.append(_transform_cmd(op, body[0]))
            i += 1
    line = " | ".join(cmds)
    # The pipeline's stdout redirect, when the render op carries one.  A
    # pipeline without one is byte-identical to what it rendered before.
    if "path" in ops[-1].params:
        _need(ops[-1], "path", "append")     # a redirect is both keys or neither
        path = _text(ops[-1].params["path"], "redirect path")
        if _flag(ops[-1].params["append"], "redirect append"):
            return line + " >> " + _literal(path)
        return line + " > " + _literal(path)
    return line


# ---------------------------------------------------------------------------
# statements
# ---------------------------------------------------------------------------

def _body_inline(body: list) -> str:
    return "; ".join(_stmt_or_pipeline(st) for st in body)


def _stmt(st: Stmt) -> str:
    t = st.st_type
    if t == "assign":
        name, value = st.params["name"], st.params["value"]
        if not _IDENT.fullmatch(name) or "\n" in value:
            raise ConversionError(f"cannot render assignment {name!r}")
        if value.startswith("$(") and value.endswith(")"):
            return f"{name}={value}"     # already shell text, verbatim
        return f"{name}={_expandable(value)}"
    if t == "echo":
        text = st.params["text"]
        return "echo" if not text else "echo " + _expandable(text)
    if t == "write":
        return _write_cmd(st)
    if t == "for":
        var, items = st.params["var"], st.params["items"]
        if not _IDENT.fullmatch(var):
            raise ConversionError(f"for loop variable {var!r} is not an identifier")
        if not items:
            raise ConversionError("for loop without an explicit word list")
        words = " ".join(_literal(x) for x in items)
        return f"for {var} in {words}; do {_body_inline(st.body)}; done"
    if t == "if":
        return _if_stmt(st)
    if t == "passthrough":
        return _passthrough_cmd(st)
    raise ConversionError(f"no Bash rendering for statement {t!r}")


def _passthrough_cmd(st: Stmt) -> str:
    """Nu the layer does not translate, carried in a marked comment block.

    Bash has nowhere to put raw Nushell, so the fragment travels as comments
    between the two markers ``converter._extract_passthrough`` reads back --
    exactly the spelling a compat agent writes by hand for a Nu-only step, and
    the only shape in which a Bash file can hold code Bash cannot run.  Nothing
    is translated on either side of the round trip, which is what makes the
    preservation exact rather than approximate (contrast every other op here,
    which is re-derived from the IR and checked against the parser's rules).

    The recogniser refuses any Nu containing ``#`` (comments are outside the
    subset), so a fragment can never spell a marker itself and close the block
    early; ``#`` alone carries an interior blank line, which
    ``_PT_LINE`` strips back to the blank it was.
    """
    p = _keys(st.params, "passthrough statement", "text")
    text = _text(p["text"], "passthrough text")
    if not text.strip():
        raise ConversionError("a pass-through block with no Nu in it")
    body = [("# " + ln) if ln.strip() else "#" for ln in text.split("\n")]
    return "\n".join([_PT_START] + body + [_PT_END])


def _printf_escape(s: str) -> str:
    """Bash text → its `printf` format spelling."""
    if any(ch in s for ch in "'%"):
        raise ConversionError(
            f"printf text {s!r} cannot be spelled in a single-quoted printf "
            f"format word")
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _write_cmd(st: Stmt) -> str:
    """`write` — bash text saved to a file, spelled as the `printf` that makes it.

    Each ``$var`` in the text becomes a ``%s`` hole and its argument; everything
    else is the format, escaped back to how a shell word carries a tab and a
    newline.
    """
    p = _keys(st.params, "write statement", "text", "path", "append")
    text = _text(p["text"], "write text")
    if not text.endswith("\n"):
        raise ConversionError(
            "a write without a trailing newline has no printf rendering")
    fmt: list = []
    args: list = []
    i = 0
    for m in re.finditer(r"\$(\w+)", text):
        fmt.append(_printf_escape(text[i:m.start()]))
        fmt.append("%s")
        args.append("$" + m.group(1))
        i = m.end()
    fmt.append(_printf_escape(text[i:]))
    out = "printf " + _literal("".join(fmt))
    for a in args:
        out += " " + _expandable(a)
    out += (" >> " if _flag(p["append"], "write append") else " > ") \
        + _literal(_text(p["path"], "write path"))
    return out


def _if_stmt(st: Stmt) -> str:
    if st.params.get("negate"):
        raise ConversionError("negated if-condition is not in the subset")
    a, op, b = st.params["cond"]
    if op not in _TEST_OPS:
        raise ConversionError(f"unsupported test operator {op!r}")
    then_body = st.body[0] if st.body else []
    else_body = st.body[1] if len(st.body) > 1 else []
    if not then_body:
        # the subset has no no-op statement to put in an empty branch
        raise ConversionError("if with an empty then-branch")
    out = (f"if [ {_expandable(a)} {op} {_expandable(b)} ]; then "
           f"{_body_inline(then_body)}; ")
    if else_body:
        out += f"else {_body_inline(else_body)}; "
    return out + "fi"


def _stmt_or_pipeline(st) -> str:
    if isinstance(st, Pipeline):
        return _render_pipeline(st)
    if isinstance(st, Stmt):
        return _stmt(st)
    raise ConversionError(f"unknown statement type {type(st).__name__}")


# ---------------------------------------------------------------------------
# top-level API
# ---------------------------------------------------------------------------

def ir_to_bash(script: Script) -> str:
    """Render a shell IR back to Bash that re-parses to the same IR."""
    if not isinstance(script, Script):
        raise ConversionError("ir_to_bash expects a Script")
    out = []
    for st in script.stmts:
        try:
            out.append(_stmt_or_pipeline(st))
        except ConversionError:
            raise
        except (KeyError, TypeError, IndexError) as e:
            # a hand-built op missing the params its type needs is exactly the
            # "cannot render this faithfully" case — refuse, never emit
            raise ConversionError(f"malformed IR node: {e!r}") from e
    return "\n".join(out)
