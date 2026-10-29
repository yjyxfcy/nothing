"""Shell IR → Nu 0.114.1 generator.

Renders the ops produced by ``bash_to_ir`` to Nushell, with a small
type-tracker over the data stream (the Nu analogue of a relation binder):

  * ``lines`` — raw text lines (from ``open --raw f | lines``)
  * ``rows``  — a table of records (named columns / json fields / materialized
                log records); ``positional`` rows are named ``column{i}``
  * ``vals``  — a flat list of scalar values
  * ``value`` — a single scalar (e.g. after ``length``)

Fidelity contract: the final stdout text of the rendered Nu equals Bash's
stdout on the same data (modulo the normalization used by the isolation gate:
whitespace collapse, numeric ``.0`` stripping, row sort).  Every rendered pattern
below was verified against nu 0.114.1 in the ``_nu_probe`` scratch dir.
"""
from __future__ import annotations

import re

from .ir import ConversionError, Op, Pipeline, Script, Stmt
from .awk_parse import render_spec


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def _qsq(s: str) -> str:
    """Single-quoted Nu string — literal, no backslash interpolation.  Regex
    patterns/REPLs carrying a quote can't be rendered safely this way."""
    if "'" in s:
        raise ConversionError(
            f"cannot render string containing a quote in single-quoted Nu "
            f"literal: {s!r}")
    return "'" + s + "'"


def _val(v: str) -> str:
    """Render a bash literal as a Nu literal (unquoted if it is a number)."""
    v = v.strip().strip("'\"")
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return v
    return _q(v)


def _nu_text(s: str) -> str:
    """Plain text encoded for the *inside* of a Nu double-quoted literal.

    No interpolation is introduced: a `$` in the text stays a literal `$`, since
    only `(...)` holes are interpolated.  Tab and newline become their Nu
    escapes, so a `find -printf '%s\\t%p\\n'` separator survives as the tab it
    means rather than as a two-character string.
    """
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\t", "\\t").replace("\n", "\\n"))


def _is_num(v: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", v.strip()))


class _Builder:
    """Stream-type tracking while building a pipeline's Nu text."""

    def __init__(self):
        self.kind = None        # tsv | csv | json | log | text | files
        self.stream = None      # lines | rows | vals | value
        self.positional = False  # rows use column{i} names
        self.colnames = []      # explicit record field names
        self.ncols = 0          # column count for positional rows
        self.print_order = []   # render order override (e.g. group print order)
        self.uniq_count_format = False  # GNU uniq -c pads the count to seven columns
        self.sep = " "          # field separator the *text* uses between fields
        self.chunks: list[str] = []

    def add(self, frag: str):
        self.chunks.append(frag)

    def piped(self) -> str:
        return " | ".join(self.chunks)

    def rows_name(self, names: list[str]):
        self.stream = "rows"
        self.positional = False
        self.colnames = list(names)
        self.ncols = len(names)
        self.print_order = []
        self.uniq_count_format = False
        # A new row shape is a new rendering of the line, so the separator the
        # old shape's text used does not carry over (an awk re-print joins its
        # fields with a space, whatever the source file used).
        self.sep = " "

    def rows_positional(self, n: int):
        self.stream = "rows"
        self.positional = True
        self.colnames = []
        self.ncols = n
        self.print_order = []
        self.uniq_count_format = False
        self.sep = " "

    def col(self, index: int) -> str:
        """Emission name of the positional record field ``index`` (0-based)."""
        if self.positional:
            return f"column{index}"
        return self.colnames[index]

    def render_columns(self) -> list:
        """Field names in the order a bash reader sees them on the rendered
        line: the print-order override when one is set (group/bucket/uniq -c),
        else the natural record order."""
        if self.print_order:
            return list(self.print_order)
        if self.positional:
            return [f"column{i}" for i in range(self.ncols)]
        return list(self.colnames)

    def rcol(self, index: int) -> str:
        """Name of the ``index``-th field as read off the *rendered* line."""
        if self.positional:
            # positional rows expose column{i} for *every* bash field; ncols is
            # only a lower bound for the render fallback.
            if index >= self.ncols:
                self.ncols = index + 1
            return f"column{index}"
        ro = list(self.print_order or self.colnames)
        return ro[index]

    def natural_sort_col(self) -> str:
        """The render column a bare `sort -n/-r` keys on (bash sorts the first
        printed field)."""
        if self.print_order:
            return self.print_order[0]
        if self.positional:
            if self.ncols == 0:
                self.ncols = 1
            return "column0"
        return self.colnames[0]


_SIDE_EFFECT = {"mkdir", "cp", "mv", "rm", "touch", "truncate"}


class _NuGen:
    # =====================================================================
    # pipeline entry
    # =====================================================================
    def pipeline(self, pl: Pipeline, for_capture: bool = False,
                 display: bool = True) -> str:
        """``display`` = this pipeline may rely on Nushell showing its value.

        Nushell displays the value of the script's last expression and discards
        every earlier one, so a text pipeline anywhere but the end has to say
        `print` itself.  Only `ir_to_nu`'s statement loop knows the position, so
        it passes the answer in; nested calls (a `for` body, an assignment
        capture) keep the default.
        """
        b = _Builder()
        ops = pl.ops if isinstance(pl.ops, list) else [pl.ops]
        if (len(ops) == 3 and ops[0].op_type == 'read'
                and ops[1].op_type == 'count' and ops[2].op_type == 'render'
                and not ops[0].params.get('skip_header') and not ops[1].params
                and not ops[2].params):
            # wc -l counts newline bytes, not records: an unterminated last
            # line must not be counted. Count newline characters directly;
            # split row drops trailing empty fields for streamed file input.
            raw = f'open --raw {_q(ops[0].params["path"])} | str replace --all --regex "[^\\n]" "" | str length'
            return raw if for_capture else f'print ({raw})'
        if (not for_capture and len(ops) == 2 and ops[0].op_type == 'read'
                and ops[1].op_type == 'render' and not ops[1].params
                and not ops[0].params.get('skip_header')):
            # A bare cat copies bytes as-is, including a missing final newline.
            raw = f'open --raw {_q(ops[0].params["path"])}'
            return raw if display else f'print -n ({raw})'
        for index, op in enumerate(ops):
            if op.op_type in ("uniq", "uniq_c") and not op.params.get("global_dedup"):
                if index == 0 or ops[index - 1].op_type != "sort":
                    raise ConversionError("Adjacent uniq requires a preceding sort in this adapter")
            if op.op_type == "render":
                if not for_capture:
                    if (display and index > 0 and ops[index - 1].op_type == 'grep'
                            and ops[index - 1].params.get('count')
                            and op.params.get('path') is None and b.stream == 'value'):
                        # grep -c prints 0 and still exits 1 on no matching lines.
                        return ('do { let rosetta_grep_count = (' + b.piped() +
                                '); print $rosetta_grep_count; '
                                'if $rosetta_grep_count == 0 { exit 1 } }')
                    if (display and index > 0 and ops[index - 1].op_type == 'grep'
                            and not ops[index - 1].params.get('count')
                            and op.params.get('path') is None and b.stream == 'lines'):
                        # A final grep determines the script's exit status. Nu's
                        # `where` otherwise succeeds on an empty result. Materialize
                        # once so no-match returns 1 without rereading the input.
                        return ('do { let rosetta_grep_matches = (' + b.piped() +
                                '); if ($rosetta_grep_matches | is-empty) { exit 1 }; '
                                '$rosetta_grep_matches | each {|x| $"($x)(char nl)"} | str join "" }')
                    return self._render(b, op, display)
                return self._expr_join(b)
            m = getattr(self, "_op_" + op.op_type, None)
            if m is None:
                raise ConversionError(f"unknown op {op.op_type}")
            m(b, op)
        # side-effect-only pipelines (mkdir/cp/mv/rm/touch) print nothing on
        # stdout — the same as Bash — so they render as bare command lines.
        if ops and all(op.op_type in _SIDE_EFFECT for op in ops):
            return "\n".join(b.chunks)
        # a line loop is a terminal stage too: its body prints for itself
        if ops and ops[-1].op_type == "while_read":
            return b.chunks[0]
        raise ConversionError("pipeline without a render step")

    # -- side-effect file ops -------------------------------------------
    def _op_mkdir(self, b: _Builder, op: Op):
        b.add(f"mkdir {_q(op.params['path'])}")

    def _op_cp(self, b: _Builder, op: Op):
        b.add(f"cp {_q(op.params['src'])} {_q(op.params['dst'])}")

    def _op_mv(self, b: _Builder, op: Op):
        b.add(f"mv {_q(op.params['src'])} {_q(op.params['dst'])}")

    def _op_rm(self, b: _Builder, op: Op):
        r = "-r " if op.params.get("rec") else ""
        b.add(f"rm {r}{_q(op.params['path'])}")

    def _op_touch(self, b: _Builder, op: Op):
        b.add(f"touch {_q(op.params['path'])}")

    def _op_truncate(self, b: _Builder, op: Op):
        # bash `: > file` — empty the file in place.
        b.add(f"'' | save -f {_q(op.params['path'])}")

    # -- message: pipeline has internal prints only when for_capture False ----
    def _expr_join(self, b: _Builder) -> str:
        """An assignment-capture expression: value (scalar) or joined text."""
        if b.stream == "value":
            return b.piped()
        if b.stream in ("rows", "vals", "lines"):
            return f"{b.piped()} | str join \"\\n\""
        raise ConversionError(f"cannot capture stream {b.stream}")

    # =====================================================================
    # source ops
    # =====================================================================
    def _op_read(self, b: _Builder, op: Op):
        kind, path = op.params["kind"], op.params["path"]
        skip = op.params.get("skip_header", 0)
        b.kind = kind
        if kind in ("tsv", "csv"):
            # Skip the header on the LINE stream *before* parse so `from tsv -n`
            # infers column types from pure data rows (a header string would
            # degrade a numeric column to oneof<string, float>).
            parse = "from tsv -n" if kind == "tsv" else "from csv --noheaders"
            head = f"open --raw {_q(path)} | lines"
            if skip:
                head += f" | skip {skip}"
            b.add(f"{head} | str join (char nl) | {parse}")
            b.rows_positional(0)
        elif kind == "json":
            b.add(f"open {_q(path)}")
            b.rows_name([])
        elif kind in ("log", "text"):
            # `lines` removes CR from CRLF input, whereas Bash line tools keep
            # it as data. Split on LF while retaining CR and avoid inventing an
            # extra empty line after the file's final delimiter.
            head = (f'open --raw {_q(path)} | if $in == "" {{ [] }} else {{ '
                    '$in | str replace --regex "\\n$" "" | split row (char nl) }')
            if skip:
                head += f" | skip {skip}"
            b.add(head)
            b.stream = "lines"
        else:
            raise ConversionError(f"unknown source kind {kind}")

    # =====================================================================
    # awk stages
    # =====================================================================
    def _materialize_log(self, b: _Builder, indices: list[int]) -> dict:
        """Convert a ``lines`` stream to a named record of log fields."""
        if b.stream != "lines":
            raise ConversionError("materialize_log on non-lines stream")
        fnames = [f"f{i}" for i in indices]
        subs = ", ".join(f"{n}: ($p | get {i})" for n, i in zip(fnames, indices))
        b.add(f"each {{|l| let p = ($l | split row ' '); {{{subs}}}}}")
        b.rows_name(fnames)
        return dict(zip(indices, fnames))

    def _op_awk_get(self, b: _Builder, op: Op):
        cols = op.params["cols"]
        if b.stream == "lines":
            if len(cols) == 1:
                b.add(f"each {{|l| $l | split row ' ' | get {cols[0]} }}")
                b.stream = "vals"
            else:
                self._materialize_log(b, cols)
            return
        if b.stream == "rows":
            if len(cols) == 1:
                b.add(f"get {b.rcol(cols[0])}")
                b.stream = "vals"
            else:
                names = [b.rcol(i) for i in cols]
                b.add("select " + " ".join(names))
                b.rows_name(names)
            return
        raise ConversionError(f"awk_get on {b.stream} not supported")

    def _op_filter(self, b: _Builder, op: Op):
        col, o, v = op.params["col"], op.params["op"], op.params["val"]
        if b.stream == "rows":
            if o == "~":
                # awk `$col ~ /re/` → Nu regex match on the record field.
                b.add(f"where {{|r| ($r.{b.rcol(col)}) =~ {_qsq(v)}}}")
            else:
                b.add(f"where {b.rcol(col)} {o} {_val(v)}")
            return
        raise ConversionError(f"filter on {b.stream} not supported")

    def _op_awk_fget(self, b: _Builder, op: Op):
        fcol, o, v = op.params["fcol"], op.params["op"], op.params["val"]
        cols = op.params["cols"]
        if b.stream == "lines":
            needed = sorted(set(cols + [fcol]))
            names = self._materialize_log(b, needed)
            lhs = names[fcol]
            if o == "~":
                b.add(f"where {{|r| ($r.{lhs}) =~ {_qsq(v)}}}")
            elif _is_num(v):
                # log fields from `split row ' '` are strings; numeric filters
                # must cast, or nu rejects string-vs-int comparison.
                b.add(f"where {{|r| ($r.{lhs} | into float) {o} {_val(v)}}}")
            else:
                b.add(f"where {lhs} {o} {_val(v)}")
            sel = [names[caddr] for caddr in cols]
            if len(sel) == 1:
                b.add(f"get {sel[0]}")
                b.stream = "vals"
            else:
                b.rows_name(sel)
            return
        if b.stream == "rows":
            fname = b.rcol(fcol)
            if o == "~":
                b.add(f"where {{|r| ($r.{fname}) =~ {_qsq(v)}}}")
            else:
                b.add(f"where {fname} {o} {_val(v)}")
            sel = [b.rcol(c) for c in cols]
            if len(sel) == 1:
                b.add(f"get {sel[0]}")
                b.stream = "vals"
            else:
                b.add("select " + " ".join(sel))
                b.rows_name(sel)
            return
        raise ConversionError("awk_fget on unsupported stream")

    def _op_agg(self, b: _Builder, op: Op):
        """Scalar awk aggregates: `sum`, conditional `avg`, conditional `count`."""
        mode = op.params["mode"]
        if mode == "sum":
            i = op.params["sumcol"]
            if b.stream == "rows":
                b.add(f"get column{i} | math sum")
            elif b.stream == "lines":
                b.add(f"each {{|l| ($l | split row ' ' | get {i} | "
                      f"into float)}} | math sum")
            else:
                raise ConversionError(f"agg sum over {b.stream} not supported")
            b.stream = "value"
            return
        if mode == "avg":
            fcol, val, sumcol = op.params["fcol"], op.params["val"], \
                op.params["sumcol"]
            rounder = "into int" if op.params.get("rint") == "int" \
                else "math round"
            if b.stream == "rows":
                b.add(f"where column{fcol} == {_val(val)}")
                b.add(f"get column{sumcol} | into float | math avg | {rounder}")
            elif b.stream == "lines":
                names = self._materialize_log(b, sorted({fcol, sumcol}))
                b.add(f"where {names[fcol]} == {_val(val)}")
                b.add(
                    f"get {names[sumcol]} | into float | math avg | {rounder}")
            else:
                raise ConversionError(f"agg avg over {b.stream} not supported")
            b.stream = "value"
            return
        if mode == "count":
            fcol, val = op.params["fcol"], op.params["val"]
            if b.stream == "rows":
                b.add(f"where column{fcol} == {_val(val)}")
                b.add("length")
            elif b.stream == "lines":
                names = self._materialize_log(b, [fcol])
                b.add(f"where {names[fcol]} == {_val(val)}")
                b.add("length")
            else:
                raise ConversionError(f"agg count over {b.stream} not supported")
            b.stream = "value"
            return
        raise ConversionError(f"unknown agg mode {mode}")

    def _group(self, b: _Builder, op: Op, which: str):
        key0 = op.params["key"]
        val0 = op.params["val"] if which == "sum" else None
        if b.stream == "lines":
            need = [key0] + ([val0] if val0 is not None else [])
            names = self._materialize_log(b, need)
            keyname = names[key0]
            valname = names[val0] if val0 is not None else None
        elif b.stream == "rows":
            keyname = b.col(key0)
            valname = b.col(val0) if val0 is not None else None
        else:
            raise ConversionError("group-by must follow a rows source")
        frag = [f"group-by {keyname}"]
        if which == "count":
            frag.append("items {|k,v| {key: $k, count: ($v | length)}}")
            tok = _order_tokens(op.params.get("print"), "count")
            b.rows_name(list(dict.fromkeys(tok)))
            b.print_order = tok
        else:
            frag.append(
                f"items {{|k,v| let s = ($v | get {valname} | into float | "
                f"math sum); {{key: $k, sum: $s}}}}")
            tok = _order_tokens(op.params.get("print"), "sum")
            b.rows_name(["key", "sum"])
            b.print_order = tok
        b.add(" | ".join(frag))

    def _op_group_count(self, b: _Builder, op: Op):
        self._group(b, op, "count")

    def _op_group_sum(self, b: _Builder, op: Op):
        self._group(b, op, "sum")

    def _op_awk_program(self, b: _Builder, op: Op):
        """General restricted-awk classifier output (relative fields $NF-1,
        split()/lets, guarded group/scalar aggregates with printf formats)."""
        spec = op.params["spec"]
        # awk sees the current record as a line regardless of the source stream:
        # rows materialize back to lines via `values | str join`, vals already
        # are lines.  Only a scalar stream has no record structure.
        if b.stream == "rows":
            stream = "rows"
            rcol = b.rcol
        elif b.stream in ("lines", "vals"):
            stream = "lines"
            rcol = None
        else:
            raise ConversionError(f"awk_program on {b.stream} not supported")
        frag = render_spec(spec, stream, spec.fs, rcol)
        b.add(frag)
        if spec.family == "select":
            b.stream = "vals"
        elif spec.family in ("scalar_sum", "scalar_avg"):
            b.stream = "value"
        elif spec.family in ("group_count", "group_sum"):
            if spec.family == "group_count":
                b.rows_name(["key", "count"])
            else:
                b.rows_name(["key", "sum"])
            b.print_order = [tok for tok, _ in spec.gprint]
        else:
            raise ConversionError(f"unknown spec family {spec.family}")

    def _op_bucket(self, b: _Builder, op: Op):
        col = op.params["col"]
        prefix = op.params["prefix"]
        yes = op.params.get("yes", "yes")
        no = op.params.get("no", "other")
        if b.stream == "lines":
            colpath = f"($l | split row ' ' | get {col})"
        elif b.stream == "rows":
            colpath = b.col(col)
        else:
            raise ConversionError(f"bucket on {b.stream} not supported")
        b.add(f"each {{|r| if ({colpath} | str starts-with {_q(prefix)}) "
              f"{{{_q(yes)}}} else {{{_q(no)}}}}}")
        b.stream = "vals"
        b.add("group-by {|x| $x}")
        b.add("items {|k,v| {key: $k, count: ($v | length)}}")
        b.rows_name(["key", "count"])

    def _op_uniq(self, b: _Builder, op: Op):
        """Whole-line/whole-record dedup: bare `uniq` or `!seen[$0]++`.

        Adjacent and global deduplication coincide after whole-line sorting.
        An awk `!seen[$0]++` stage is global by definition."""
        if b.stream not in ("vals", "lines", "rows"):
            raise ConversionError(f"uniq on {b.stream} not supported")
        b.add("uniq")

    def _op_uniq_c(self, b: _Builder, op: Op):
        """`uniq -c`: [count, line] table. Nu has no uniq -c, so build it from a
        group-by; positional column0=count, column1=line (bash field order)."""
        if b.stream not in ("vals", "lines", "rows"):
            raise ConversionError(f"uniq -c on {b.stream} not supported")
        b.add("group-by {|x| $x}")
        b.add("items {|k,v| {column0: ($v | length), column1: $k}}")
        b.rows_positional(2)
        b.print_order = ["column0", "column1"]
        b.uniq_count_format = True

    def _op_uniq_reorder(self, b: _Builder, op: Op):
        """Trailing ``awk '$1>N {print $2,$1}'`` after uniq -c: operates on the
        positional [count,line] table in place."""
        cols = op.params.get("cols", [])
        filt = op.params.get("filter")
        if filt:
            fi, o, v = filt
            b.add(f"where column{fi} {o} {_val(v)}")
        if not b.positional:
            raise ConversionError("uniq_reorder requires positional rows")
        # cols is already 0-based into [count, line]: column0=count, column1=line
        b.print_order = [f"column{c}" for c in cols]
        b.uniq_count_format = False  # awk prints its selected fields afresh

    # =====================================================================
    # sort / slice / count
    # =====================================================================
    def _op_sort(self, b: _Builder, op: Op):
        rev = " -r" if op.params.get("reverse") else ""
        if b.stream in ("vals", "lines"):
            if op.params.get("numeric"):
                # A value/line stream has no field to key on, so the numeric
                # sort keys its closure on the scalar itself -- the flat
                # analogue of the `rows` spellings below, and the only form
                # that keeps the flag *in the image*: a lexical `sort` and a
                # numeric one are different IRs, and a shared spelling would
                # make the flag unrecoverable from the delivered Nu (the corpus
                # holds one task of each).
                b.add(f"sort-by {{|x| ($x | into float)}}{rev}")
            else:
                # Nu's `sort` is the lexical sort, which is what a bare Bash
                # `sort` is; it is also its own recognisable spelling.
                b.add("sort" + rev)
        elif b.stream == "rows":
            col = op.params.get("col")
            if col is None:
                if op.params.get("numeric"):
                    # bare `sort -n/-r` keys on the *first rendered field*
                    # (awk print order / group print order).
                    cname = b.natural_sort_col()
                    b.add(f"sort-by {{|r| ($r.{cname} | into float)}}{rev}")
                else:
                    # bare `sort` sorts the whole line, so sort by the joined
                    # rendered record; `uniq` then dedups adjacent records.
                    names = b.render_columns()
                    cell = " ".join(f"($r.{n})" for n in names)
                    b.add(f"sort-by {{|r| $\"{cell}\"}}{rev}")
            else:
                # `sort -k<N>` keys on the N-th *rendered* field; after a
                # reorder (e.g. uniq_reorder print $2,$1) the record's natural
                # column index no longer matches the printed position, so map
                # through render_columns() (print-order aware).
                rcols = b.render_columns()
                cname = rcols[col] if col < len(rcols) else b.rcol(col)
                if op.params.get("numeric"):
                    b.add(f"sort-by {{|r| ($r.{cname} | into float)}}{rev}")
                else:
                    b.add(f"sort-by {cname}{rev}")
        else:
            raise ConversionError(f"sort on {b.stream} not supported")
        if op.params.get("unique"):
            b.add("uniq")

    def _op_head(self, b: _Builder, op: Op):
        b.add(f"first {op.params['n']}")

    def _op_skip(self, b: _Builder, op: Op):
        b.add(f"skip {op.params['n']}")

    def _op_tail(self, b: _Builder, op: Op):
        b.add(f"last {op.params['n']}")

    def _op_count(self, b: _Builder, op: Op):
        b.add("length")
        b.stream = "value"

    # =====================================================================
    # text filters
    # =====================================================================
    def _op_grep(self, b: _Builder, op: Op):
        pat = op.params["pat"]
        inv = "not " if op.params.get("invert") else ""
        if op.params.get("count"):
            # `grep -c` prints the number of matching lines (one per line, not
            # per match) — where|length over the same filter.
            if b.stream in ("lines", "vals"):
                b.add(f"where {{|l| {inv}($l =~ {_q(pat)}) }}")
                b.add("length")
            elif b.stream == "rows":
                b.add(f"where {{|r| {inv}(($r | values | str join \" \") =~ {_q(pat)}) }}")
                b.add("length")
            else:
                raise ConversionError(f"grep -c on {b.stream} not supported")
            b.stream = "value"
            return
        if b.stream in ("lines", "vals"):
            b.add(f"where {{|l| {inv}($l =~ {_q(pat)}) }}")
        elif b.stream == "rows":
            # bash `grep` searches the whole line; values-join recovers it for
            # both positional and named rows without needing column names.
            b.add(f"where {{|r| {inv}(($r | values | str join \" \") =~ {_q(pat)}) }}")
        else:
            raise ConversionError(f"grep on {b.stream} not supported")

    def _op_sed(self, b: _Builder, op: Op):
        pat, rep = op.params["pat"], op.params["rep"]
        if b.stream in ("lines", "vals"):
            flag = "--regex " if op.params.get("regex") else ""
            frags = []
            # sed -n 's/pat/rep/p' prints only lines where the pattern matched
            # (with the substitution applied); without -n every line is printed.
            if op.params.get("keep_matched"):
                frags.append(f"where {{|r| $r =~ {_qsq(pat)} }}")
            frags.append(
                f"each {{|r| $r | str replace {flag}{_qsq(pat)} {_qsq(rep)}}}")
            b.add(" | ".join(frags))
            b.stream = "vals"
        else:
            raise ConversionError(f"sed on {b.stream} not supported")

    def _op_tr(self, b: _Builder, op: Op):
        if b.stream in ("lines", "vals"):
            w = "uppercase" if op.params["mode"] == "upper" else "lowercase"
            b.add(f"each {{|r| $r | str {w}}}")
            b.stream = "vals"
        else:
            raise ConversionError(f"tr on {b.stream} not supported")

    def _op_cut(self, b: _Builder, op: Op):
        col = op.params['col']
        if b.stream == "lines":
            b.add(f"each {{|l| $l | split row {_q(op.params['delim'])} | "
                  f"get {col}}}")
            b.stream = "vals"
        elif b.stream == "rows":
            # tsv/csv rows already expose the field as a named column; cut just
            # projects it (fields is later narrowed, so downstream sort/uniq see
            # a single value stream).
            b.add(f"get {b.rcol(col)}")
            b.stream = "vals"
        else:
            raise ConversionError(f"cut on {b.stream} not supported")

    # =====================================================================
    # jq
    # =====================================================================
    def _jq_operand(self, c: dict) -> str:
        """Record-field operand for a jq cond: ``$r.f`` with an optional
        `default N` (jq ``// N`` for null fields) and `into float` cast (jq
        ``| tonumber`` for JSON string numbers)."""
        e = f"$r.{c['field']}"
        if c.get("default"):
            e = f"({e} | default {c['default']})"
        if c.get("cast"):
            e = f"({e} | into float)"
        return e

    def _op_jq_where(self, b: _Builder, op: Op):
        conds = op.params["conds"]
        fields = op.params.get("fields") or [conds[0]["field"]]
        # nu's paren cell-path shorthand `where (mem > 85)` treats `mem` as a
        # command; the closure form resolves record fields as variables.
        def _jq_val(v):
            return "null" if v == "null" else _val(v)
        cond_parts = " and ".join(
            f"{self._jq_operand(c)} {c['op']} {_jq_val(c['val'])}"
            for c in conds)
        b.add(f"where {{|r| {cond_parts} }}")
        if op.params.get("first"):
            b.add(f"first {op.params['first']}")
        if len(fields) == 1:
            b.add(f"get {fields[0]}")
            b.stream = "vals"
        else:
            b.add("select " + " ".join(fields))
            b.rows_name(list(fields))

    def _op_jq_group(self, b: _Builder, op: Op):
        field = op.params["field"]
        template = op.params["template"]
        m = re.search(r"\\\(\s*\[\s*\[\.\]\.(\w+)\s*\]\s*\|\s*add\s*\)", template)
        if not m:
            raise ConversionError(
                f"unsupported jq group template {template[:40]}")
        val = m.group(1)
        b.add(f"group-by {field}")
        b.add(f"items {{|k,v| {{key: $k, s: ($v | get {val} | into float | math sum)}}}}")
        b.rows_name(["key", "s"])
        # jq template prints group key first then the summed value
        b.print_order = ["key", "s"]

    # =====================================================================
    # find
    # =====================================================================
    def _op_source_find(self, b: _Builder, op: Op):
        # glob must stay unquoted: a quoted `ls "dir/**/*"` is treated as a
        # literal filename by nu and fails with "No matches found".
        path = op.params["path"].rstrip("/") + "/**/*"
        b.add(f"ls {path}")
        b.stream = "rows"
        b.kind = "files"

    def _op_f_filter_file(self, b: _Builder, op: Op):
        b.add("where type == file")

    def _find_rows(self, b: _Builder, op: Op):
        """thin {name, sz} rows from `ls` file rows; print order comes from
        the bash find's -printf/%s %p variants."""
        b.add("each {|f| {name: $f.name, sz: ($f.size | into int)}}")
        b.rows_name(["name", "sz"])
        b.print_order = op.params.get("print_order") or ["name"]
        b.sep = op.params.get("sep", " ")

    def _op_f_gt_size(self, b: _Builder, op: Op):
        self._find_rows(b, op)
        b.add(f"where sz > {op.params['size']}")

    def _op_f_size(self, b: _Builder, op: Op):
        self._find_rows(b, op)

    def _op_f_list(self, b: _Builder, op: Op):
        self._find_rows(b, op)

    # =====================================================================
    # tee: the stream continues, and a copy of the text goes to a file
    # =====================================================================
    def _op_tee(self, b: _Builder, op: Op):
        """`cmd | tee FILE` — Nu's `tee` takes a closure and hands it the same
        input, so the closure renders this point's lines through `$in` and saves
        them, exactly the bytes bash would have written."""
        inner = self._save(self._lines_expr(b, head="$in", nl=True), op)
        b.add(f"tee {{ {inner} }}")

    # =====================================================================
    # `while IFS= read -r VARS; do BODY; done`
    # =====================================================================
    def _op_while_read(self, b: _Builder, op: Op):
        """The line loop: `each` over the source's lines, body included.

        A `print` in the body writes to stdout on its own, and `each` collects
        the body's (null) return values, so a trailing `| ignore` drops that
        list — the same shape `_for_to_nu` emits for a `for` loop.
        """
        path = op.params.get("path")
        if path is not None:
            lines = f"open --raw {_q(path)} | lines"
        elif b.chunks:
            lines = self._lines_expr(b)
        else:
            raise ConversionError("a while loop with neither a file nor a "
                                  "pipeline feeding it")
        var = op.params["var"]
        body = self._read_bindings(op) + _body_to_nu(op.params["body"])
        out = [f"{lines} | each {{|l|"]
        out += ["  " + ln for ln in body]
        out.append("}")
        out.append("| ignore")
        b.chunks = ["\n".join(out)]

    def _read_bindings(self, op: Op) -> list:
        """The `let` lines that bind the loop's variables to the line's fields."""
        variables = list(op.params["var"])
        ifs = op.params.get("ifs")
        if len(variables) == 1 and ifs == "":
            return [f"let {variables[0]} = $l"]
        if len(variables) == 1 and ifs is None:
            # bash strips the IFS whitespace off both ends of the line it reads
            return [f"let {variables[0]} = ($l | str trim)"]
        if ifs != "\t":
            raise ConversionError(
                f"a `read` splitting on {ifs!r} has no Nushell rendering; the "
                f"subset splits on a tab")
        sep = "(char tab)"
        out = []
        for k, name in enumerate(variables):
            if k < len(variables) - 1:
                out.append(f"let {name} = ($l | split row {sep} | get {k})")
            else:
                # bash gives the last variable the rest of the line, separators
                # included
                out.append(f"let {name} = ($l | split row {sep} | skip {k} "
                           f"| str join {sep})")
        return out

    # =====================================================================
    # render
    # =====================================================================
    def _lines_expr(self, b: _Builder, head: str | None = None,
                    nl: bool = False) -> str:
        """A Nu expression for the pipeline's output *lines*, as a list of
        strings — one element per line bash would have printed.

        ``head`` replaces the reconstructed pipeline prefix inside a `tee`
        closure, where the lines arrive as `$in` rather than as a re-run of the
        whole chain.  ``nl`` appends the newline bash ends every line with,
        which is what a redirect or a `tee` writes (see ``_save``).
        """
        src = b.piped() if head is None else head
        tail = "(char nl)" if nl else ""
        if b.stream == "lines":
            if not nl:
                return src
            return f'{src} | each {{|x| $"($x){tail}"}}'
        if b.stream == "vals":
            return f'{src} | each {{|x| $"($x){tail}"}}'
        if b.stream == "rows":
            names = b.print_order or [b.col(i) for i in range(b.ncols)]
            return f"{src} | each {{|r| {self._row_tpl(names, b.sep, nl, b.uniq_count_format)}}}"
        if b.stream == "value":
            # one scalar, so one line — but still a *list*, so the save below
            # writes it with the same trailing newline bash does
            return f"[{src}]"
        raise ConversionError(f"cannot render stream {b.stream}")

    def _save(self, lines: str, op: Op) -> str:
        """`<lines> | save ... FILE` — the bytes bash writes for `> FILE` / `tee`.

        bash writes each line *followed by* a newline, and writes a genuinely
        empty file for an empty stream.  `save` on a list of strings agrees for
        a non-empty list but writes a bare newline for an empty one, so the
        newline is built into each line (``nl=True``) and the pieces are joined
        here — which is right in both cases.
        """
        append = "-a" if op.params.get("append") else "-f"
        return (f"{lines} | str join \"\" | save {append} "
                f"{_q(op.params['path'])}")

    def _render(self, b: _Builder, op: Op | None = None,
                display: bool = True) -> str:
        path = op.params.get("path") if op is not None else None
        if path is not None:
            return self._save(self._lines_expr(b, nl=True), op)
        if b.stream == "value":
            return f"print ({b.piped()})"
        # Each Bash output line ends in a newline. Build it per item so an
        # empty stream produces no bytes, including for mid-script pipelines.
        rendered = f'{self._lines_expr(b, nl=True)} | str join ""'
        return rendered if display else f'print -n ({rendered})'

    def _row_tpl(self, names: list[str], sep: str = " ",
                 nl: bool = False, uniq_count_format: bool = False) -> str:
        """The `$"(...)"` template for one row.

        ``sep`` is the *real* character between the fields (a `find -printf`
        with a tab keeps printing tabs all the way down the pipeline), encoded
        here as the Nushell source escape for it.  ``nl`` appends the line's
        newline, for the redirect/`tee` write.
        """
        parts = [f"($r.{n})" for n in names]
        if uniq_count_format and names == ["column0", "column1"]:
            parts[0] = '($r.column0 | fill -a right -w 7)'
        return '$"' + _nu_text(sep).join(parts) + ("(char nl)" if nl else "") + '"'


def _order_tokens(tokens: list | None, which: str) -> list[str]:
    if not tokens:
        return ["key", "sum"] if which == "sum" else ["key", "count"]
    return [{"key": "key", "sum": "sum", "count": "count"}[t] for t in tokens]


# =====================================================================
# statements (assign / echo / if / for)
# =====================================================================

def _var_interp(value: str) -> str:
    """x=$(cmd) → capture expression; x="lit" → literal (re-quoted)."""
    value = value.strip()
    if value.startswith("$(") and value.endswith(")"):
        inner = value[2:-1]
        return nu_expr(inner)
    return _val(value)


def nu_expr(bash_cmd: str) -> str:
    """Translate a bash pipeline into a Nu *capture expression* for $()."""
    from .bash_to_ir import bash_to_ir
    script = bash_to_ir(bash_cmd)
    if len(script.stmts) != 1 or not isinstance(script.stmts[0], Pipeline):
        raise ConversionError("capture RHS must be a single pipeline")
    return _NuGen().pipeline(script.stmts[0], for_capture=True)


def stmt_to_nu(st: Stmt) -> str:
    t = st.st_type
    if t == "assign":
        return f"let {st.params['name']} = ({_var_interp(st.params['value'])})"
    if t == "echo":
        text = st.params["text"]
        if not text:
            return 'print ""'
        if re.fullmatch(r"\$\w+", text.strip()):
            return f"print {text}"
        if "$" in text:
            # Nu interpolated strings require the variable inside the parens to
            # keep its `$`: `$"Top customer: ($name)"`, never `(name)` (a bare
            # identifier there is parsed as a command). Handles both `$name`
            # and `${name}` forms.
            interp = re.sub(r"\$\{(\w+)\}", r"($\1)", text)
            interp = re.sub(r"\$(\w+)", r"($\1)", interp)
            return f"print $\"{interp}\""
        return f"print {_q(text.strip())}"
    if t == "if":
        return _if_to_nu(st)
    if t == "for":
        return _for_to_nu(st)
    if t == "block":
        # `command -v X` tool-availability guard unwrapped: only the then-branch
        # (the fallback is dead in the compat environment where X is installed).
        return "\n".join(_body_to_nu(st.body[0]))
    if t == "nop":
        # dropped robustness guard (`if [ ! -f x ]; then ... exit; fi`) — dead
        # on the correct workspace; renders to nothing.
        return ""
    if t == "write":
        return _write_to_nu(st)
    if t == "passthrough":
        # Nu the recogniser could not classify, kept as text (see
        # ``nu_to_ir``).  Its Nu image is itself: this leg renders Nu, so there
        # is nothing to spell -- only ``ir_to_bash`` needs markers, because
        # Bash has no room for raw Nu.  Reading it back is a no-op, which is
        # what makes ``ir_to_nu(nu_to_ir(x)) == x`` hold through an opaque
        # region as well as a translated one.
        text = st.params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ConversionError("malformed passthrough statement")
        return text
    raise ConversionError(f"unknown statement {t}")


def _text_to_nu(text: str) -> str:
    """Bash text with ``$var`` references → the body of a Nu ``$"…"`` literal."""
    return re.sub(r"\$(\w+)", r"($\1)", _nu_text(text))


def _write_to_nu(st: Stmt) -> str:
    """`write` — bash text (a formatted printf line) saved to a file.

    `save` writes a string verbatim, so the line's newline stays in the string
    and the file gets exactly the bytes bash would have written.
    """
    append = "-a" if st.params.get("append") else "-f"
    return (f'$"{_text_to_nu(st.params["text"])}" | save {append} '
            f'{_q(st.params["path"])}')


def _if_to_nu(st: Stmt) -> str:
    a, op, b = st.params["cond"]
    negate = "not (" if st.params.get("negate") else ""
    close = ")" if st.params.get("negate") else ""
    if op in ("-gt", "-lt", "-ge", "-le", "-eq", "-ne"):
        nuop = {"-gt": ">", "-lt": "<", "-ge": ">=",
                "-le": "<=", "-eq": "==", "-ne": "!="}[op]
        cond = f"{_operand(a)} {nuop} {_operand(b)}"
    elif op in ("=", "==", "!="):
        nuop = "!=" if op == "!=" else "=="
        cond = f"{_operand(a)} {nuop} {_operand(b)}"
    elif op == "-f":
        cond = f"({_operand(a)} | path exists)"
        negate, close = "not (", ")"
    else:
        raise ConversionError(f"unsupported if operator {op}")
    out = [f"if {negate}{cond}{close} {{"]
    for ln in _body_to_nu(st.body[0]):
        out.append("  " + ln)
    if len(st.body) > 1 and st.body[1]:
        out.append("} else {")
        for ln in _body_to_nu(st.body[1]):
            out.append("  " + ln)
    out.append("}")
    return "\n".join(out)


def _operand(s: str) -> str:
    """A test operand: `$x` → Nu var; numeric → bare; else quoted literal."""
    if re.fullmatch(r"\$\w+", s):
        return s[1:]
    return _val(s)


def _body_to_nu(body: list) -> list[str]:
    out = []
    for st in body:
        if isinstance(st, Pipeline):
            out.append(_NuGen().pipeline(st))
        elif isinstance(st, Stmt):
            out.append(stmt_to_nu(st))
    return out


def _for_to_nu(st: Stmt) -> str:
    var = st.params["var"]
    items = " ".join(_val(x) for x in st.params["items"])
    out = [f"[{items}] | each {{|{var}|"]
    for ln in _body_to_nu(st.body):
        out.append("  " + ln)
    out.append("}")
    # `each` collects the body's return values (null for every `print`); a
    # trailing `| ignore` drops that list so nu doesn't render an empty table.
    out.append("| ignore")
    return "\n".join(out)


# =====================================================================
# top-level API
# =====================================================================

def ir_to_nu(script: Script) -> str:
    """Render a full Bash script IR to Nu."""
    lines = []
    stmts = script.stmts
    for i, st in enumerate(stmts):
        last = i == len(stmts) - 1
        if isinstance(st, Pipeline):
            lines.append(_NuGen().pipeline(st, display=last))
        elif isinstance(st, Stmt):
            lines.append(stmt_to_nu(st))
        else:
            raise ConversionError(f"unknown statement type {type(st).__name__}")
    return "\n".join(lines)
