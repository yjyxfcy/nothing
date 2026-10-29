"""bashlex AST → shell IR walker.

This is the *parser front-end* of the Bash→Nushell compat layer (the SQL layer
analog is ``sql_to_ir.py``: sqlglot AST → Relation).  Bash is parsed with a
real grammar (bashlex) into an AST; the walker recognizes the controlled subset
and emits shell-IR ops.  Anything outside the subset raises ``ConversionError``.

Two DSLs embedded in command *arguments* (awk programs, jq filters) have no Nu
semantic equivalent to compile to, so their argument strings are parsed by small
deterministic recognizers below — the same way the SQL layer parses the
expressions inside a SELECT.  They are never used to recognize the Bash script
itself; the script's structure comes from the AST.

Column convention (fidelity contract): bash awk ``$N`` is 1-indexed; Nu's
``from tsv -n`` / ``split row`` outputs are 0-indexed ``columnN``.  All column
references are stored 0-based here and rendered as ``column{i}``.  Because the
data files use positional tables, the compat layer stays schema-agnostic: the
same translator works for any tsv/csv/log layout.
"""
from __future__ import annotations

import os
import re

import bashlex

from .ir import ConversionError, Op, Pipeline, Script, Stmt


# ---------------------------------------------------------------------------
# AST word reconstruction
# ---------------------------------------------------------------------------

def _word_text(word_node) -> str:
    """Reconstruct an argument's literal text from a bashlex word node.

    ``$(...)`` / ``<( ... )`` / ``$((...))`` embedded in a word are refused
    (they require run-time capture); plain ``$var`` references are kept as text.
    """
    if word_node.kind != "word":
        raise ConversionError(f"unexpected word node kind {word_node.kind}")
    base = getattr(word_node, "word", "") or ""
    for part in getattr(word_node, "parts", []) or []:
        if getattr(part, "kind", None) == "commandsubstitution":
            raise ConversionError(
                "command substitution embedded in a word is not supported; "
                "assign `x=$(cmd)` at statement level instead")
        if getattr(part, "kind", None) == "processsubstitution":
            raise ConversionError("process substitution <( ... ) is not supported")
        if getattr(part, "kind", None) == "arithsubst":
            raise ConversionError("arithmetic expansion $((...)) is not supported")
    return base


def _cmd_parts(cmd_node) -> tuple:
    """Split a command node into (name, args, assignments, redirects).

    ``assignments`` : list[(name, value)] of leading ``VAR=...`` words.
    ``redirects``   : list of (input_fd, type, target_text).
    """
    name = None
    args = []
    assignments = []
    redirects = []
    for part in getattr(cmd_node, "parts", []) or []:
        k = getattr(part, "kind", None)
        if k == "word":
            txt = _word_text(part)
            if name is None:
                name = txt
            else:
                args.append(txt)
            continue
        if k == "assignment":
            word = getattr(part, "word", "")
            if "=" not in word:
                raise ConversionError(f"malformed assignment {word!r}")
            aname, aval = word.split("=", 1)
            assignments.append((aname, aval))
            continue
        if k == "redirect":
            redirects.append(_redirect(part))
            continue
        if k in ("parameter", "commandsubstitution"):
            continue
        raise ConversionError(f"unsupported command part kind {k}")
    if name is None:
        if assignments and not args:
            return None, [], assignments, redirects
        raise ConversionError("command node without a command word")
    return name, args, assignments, redirects


def _redirect(red_node):
    rtype = getattr(red_node, "type", ">")
    inp = getattr(red_node, "input", None)
    out = getattr(red_node, "output", None)
    target = _word_text(out) if isinstance(out, bashlex.ast.node) else str(out)
    return (inp, rtype, target)


def _read_words(word_nodes) -> list[str]:
    return [_word_text(w) for w in word_nodes]


# ---------------------------------------------------------------------------
# Statement segmentation
# ---------------------------------------------------------------------------

def _contains_exit(node) -> bool:
    """True when a node tree contains an `exit` command (for robustness-guard
    detection: ``[ -f x ] || exit 0``)."""
    k = getattr(node, "kind", None)
    if k == "command":
        try:
            return _cmd_parts(node)[0] == "exit"
        except ConversionError:
            return False
    if k == "list":
        return any(_contains_exit(p) for p in getattr(node, "parts", []) or [])
    if k == "compound":
        lst = getattr(node, "list", None)
        # bashlex compound.list is a *plain Python list* of parts (e.g. the
        # `{ echo ...; exit 1; }` guard body), not a list node.
        if isinstance(lst, list):
            return any(_contains_exit(p) for p in lst)
        return lst is not None and _contains_exit(lst)
    return False


def _is_exit_guard(parts, i) -> bool:
    """True when ``parts[i]`` is ``[ -f x ]``, ``parts[i+1]`` is ``||`` and
    ``parts[i+2]`` is an ``exit`` command/compound — a top-of-script robustness
    guard whose absence is a no-op on the (correct) test workspace, so it is
    dropped rather than refused."""
    if i + 2 >= len(parts):
        return False
    cmd = parts[i]
    if getattr(cmd, "kind", None) != "command":
        return False
    op = parts[i + 1]
    if getattr(op, "kind", None) != "operator" or getattr(op, "op", None) != "||":
        return False
    return _contains_exit(parts[i + 2])


def _segmented_top_level(nodes) -> list:
    """Split parsed top-level nodes into statements, rejecting && / || / &."""
    stmts = []
    for n in nodes:
        if getattr(n, "kind", None) == "list":
            parts = getattr(n, "parts", []) or []
            i = 0
            while i < len(parts):
                part = parts[i]
                k = getattr(part, "kind", None)
                if _is_exit_guard(parts, i):
                    i += 3  # drop `[ -f x ] || exit N` entirely
                    continue
                if k == "operator":
                    op = getattr(part, "op", None)
                    if op not in (";", "\n"):
                        raise ConversionError(
                            f"operator {op!r} has no Nushell equivalent "
                            "(&& / || / background & are not supported)")
                    i += 1
                    continue
                if k in ("command", "pipeline", "compound"):  # noqa: SIM114
                    stmts.append(part)
                    i += 1
                elif k in ("reservedword",):
                    i += 1
                else:
                    raise ConversionError(f"unsupported top-level node kind {k}")
        elif getattr(n, "kind", None) in ("command", "pipeline", "compound"):
            stmts.append(n)
        else:
            raise ConversionError(
                f"unsupported top-level node kind {getattr(n, 'kind', None)}")
    return stmts


# ---------------------------------------------------------------------------
# awk minimal recognizer
# ---------------------------------------------------------------------------

_AWK_COMPARE = {"==": "==", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


class _NeedsParser(Exception):
    """Raised by the fast-path awk/jq recognizers when a construct needs the
    restricted-awk parser (e.g. ``$(NF-1)`` relative field references).  The
    caller falls through to the general classifier instead of refusing."""


class _AwkProg:
    """Parsed shape of one awk program argument."""

    def __init__(self):
        self.fs = None            # -F field separator
        self.kind = None          # select | filter_select | group | bucket |
                                  # agg_sum | agg_avg | agg_count | uniq |
                                  # reorder_if | program
        self.cols = []            # select: 0-based columns to print
        self.filter = None        # (col0, op, value)
        self.grp_key = None       # 0-based group key column
        self.grp_val = None       # 0-based value column (sum) or None (count)
        self.grp_print = []       # order of (key|sum|count) tokens in END print
        self.bucket = None        # (col0, prefix, name_when_match, name_else)
        self.skip_rows = 0        # leading input records dropped by `NR > N`
        self.rint = None          # "int" for printf %d (truncate), None = round
        self.reorder = None       # reorder_if vars/cond/print
        self.spec = None          # AwkSpec when kind == "program"


_RAW_BASH: str | None = None

_F_RAW_RE = re.compile(r"""-F\s*(?:'([^']*)'|"([^"]*)"|(\S+))""")


def _parse_awk_fs(args: list[str]) -> str | None:
    """Return the -F separator from awk argv (default None = whitespace)."""
    fs = None
    if "-F" in args:
        i = args.index("-F")
        nxt = args[i + 1] if i + 1 < len(args) else None
        # `-F'"'` collapses to a bare `-F` whose next arg is the program, not
        # the separator — fall back to the default FS rather than mis-read it.
        if nxt is None or re.search(r"[\{\}\$;]", nxt):
            fs = None
        else:
            fs = nxt
    else:
        for a in args:
            if re.match(r"^-F", a) or a.startswith("-F'") or a.startswith("-F\""):
                fs = a[2:].strip("'\"")
                break
    # bashlex unescapes backslash-escapes inside quoted words: `-F'\t'`
    # comes back as the bare letter `-Ft`, and `-F'"'` collapses to a bare
    # `-F` with the separator dropped entirely (fs == None above).  The raw
    # source still holds the true separator, so prefer it whenever a quoted
    # single-char -F literal appears.  Only touch commands that actually
    # declared a -F (a plain awk with no -F must keep the default FS).
    has_f = "-F" in args or any(re.match(r"^-F", a) for a in args)
    if _RAW_BASH and has_f:
        for m in _F_RAW_RE.finditer(_RAW_BASH):
            val = m.group(1) if m.group(1) is not None else (
                m.group(2) if m.group(2) is not None else m.group(3))
            if len(val) == 1:
                # `-F'"'` → `-F` (fs dropped): recover a single-char separator.
                if fs is None or val == fs:
                    return val
            elif len(val) == 2 and val.startswith("\\") and fs is not None \
                    and val[1:] == fs:
                # `-F'\t'` → `-Ft`: the escaped form's unescaped char is fs.
                return val
    return fs


def _parse_awk(prog: str, fs: str | None) -> _AwkProg:
    p = _AwkProg()
    p.fs = fs
    # The restricted parser gets the program with only `\`-line-continuations
    # removed (it handles newlines + `#` comments natively).  The fast path
    # below needs a fully single-line-collapsed text.
    prog_native = re.sub(r"\\\s+", " ", prog)
    # normalize: drop whole-line `#` comments, remove `\` line continuations,
    # and collapse adjacent whitespace/newlines so multi-line programs match
    # the single-line recognizers below.
    prog = re.sub(r"(?m)^\s*#.*$", "", prog)
    prog = re.sub(r"\\\s+", " ", prog)
    prog = re.sub(r"\s+", " ", prog.strip())
    try:
        return _parse_awk_fast(prog, p)
    except _NeedsParser:
        pass
    # General fallback: the restricted-awk parser + classifier (validated Nu
    # segments).  This is where relative fields `$(NF-1)`, split()/lets/guards,
    # group/scalar aggregates with printf formatting, and the skip/CR-strip
    # prologues land.  The parser gets the *non-collapsed* program: it handles
    # newlines (statement separators) and mid-line `#` comments natively, which
    # the whitespace collapse above would otherwise fuse into one giant line.
    try:
        from . import awk_parse
        program = awk_parse.parse_program(prog_native)
        spec = awk_parse.classify(program, fs)
    except awk_parse.ConversionError:
        raise
    ap = _AwkProg()
    ap.fs = fs
    ap.kind = "program"
    ap.spec = spec
    ap.skip_rows = spec.skip
    return ap


def _parse_awk_fast(prog: str, p: _AwkProg) -> _AwkProg:
    """Whole-program regex fast path for the exact shapes agents write.  Any
    construct outside these shapes raises ``_NeedsParser`` so the caller can
    try the general restricted-awk classifier."""

    # bucket: {if ($c ~ /^P/) a["X"]++; else a["Y"]++} END {print "X", a["X"]+0; ...}
    m = re.match(
        r"\{\s*if\s*\(\s*\$\d+\s*~\s*/\^(.+?)/\s*\)\s*(\w+)\[\"(\w+)\"\]\s*\+\+"
        r";?\s*else\s*(\w+)\[\"(\w+)\"\]\s*\+\+\s*\}\s*END"
        r"\s*\{\s*print\s+\"(\w+)\"\s*,\s*\4\[\"\1\"\]\+0\s*;\s*print\s+\"(\w+)\"\s*,\s*"
        r"\5\[\"\2\"\]\+0\s*\}", prog)
    if m:
        col = int(re.search(r"\$\d+", prog.split("if")[1]).group(0)[1:]) - 1
        p.kind = "bucket"
        # the awk pattern /^\/api\// means "starts with /api/" — strip the
        # backslash escapes bashlex kept from the single-quoted program.
        prefix = m.group(1).replace("\\/", "/")
        p.bucket = {"col": col, "prefix": prefix,
                    "yes": m.group(3), "no": m.group(5)}
        return p

    # scalar sum: {t+=$6} END {print t}
    m = re.match(r"^\{\s*\w+\s*\+=\s*\$(\d+)\s*\}\s*END\s*\{\s*print\s+\w+\s*\}$",
                 prog)
    if m:
        p.kind = "agg_sum"
        p.agg_sum = int(m.group(1)) - 1
        return p

    # conditional average with printf rounding:
    #   '$7=="Electronics" {sum+=$6; n++} END {printf "%.0f\n", sum/n}'
    m = re.match(
        r"^(\$\d+)\s*==\s*\"([^\"]*)\"\s*\{\s*\w+\s*\+=\s*\$(\d+)\s*;\s*\w+\+\+\s*\}"
        r"\s*END\s*\{\s*printf\s*\"%\.0f\\n\"\s*,\s*\w+/\w+\s*\}\s*$", prog)
    if m:
        fcol = int(m.group(1)[1:]) - 1
        p.kind = "agg_avg"
        p.agg_avg = (fcol, m.group(2), int(m.group(3)) - 1)
        return p

    # scalar sum with header skip: 'NR > 1 {sum+=$6} END {print|printf sum}'
    # (the agent's NR>1 idiom in place of `tail -n +2 | awk ...`).
    m = re.match(
        r"^NR\s*>\s*1\s*\{\s*\w+\s*\+=\s*\$(\d+)\s*\}\s*END\s*\{\s*"
        r"(?:print\s+\w+|printf\s*\"[^\"]*\"\s*,\s*\w+)\s*\}$", prog)
    if m:
        p.kind = "agg_sum"
        p.agg_sum = int(m.group(1)) - 1
        p.skip_rows = 1
        return p

    # conditional average with header skip + truncating printf:
    #   'NR > 1 && $7=="Electronics" {sum+=$6; n++} END {if (n>0)
    #    printf "%d\n", sum/n}' — %d truncates (rint="int"), not rounds.
    m = re.match(
        r"^NR\s*>\s*1\s*&&\s*(\$\d+)\s*==\s*\"([^\"]*)\"\s*\{\s*\w+\s*\+=\s*\$(\d+)\s*;\s*\w+\+\+\s*\}"
        r"\s*END\s*\{\s*(?:if\s*\(\s*\w+\s*>\s*0\s*\)\s*)?printf\s*\"([^\"]*)\"\s*,\s*"
        r"\w+\s*/\s*\w+\s*\}\s*$",
        prog)
    if m:
        fcol = int(m.group(1)[1:]) - 1
        p.kind = "agg_avg"
        p.agg_avg = (fcol, m.group(2), int(m.group(3)) - 1)
        p.skip_rows = 1
        p.rint = "int" if "%d" in m.group(4) else None
        return p

    # conditional count with optional header skip:
    #   '$5=="pending" {count++} END {print count + 0}'  (and NR > 1 && ...)
    m = re.match(
        r"^(?:NR\s*>\s*1\s*&&\s*)?(\$\d+)\s*==\s*\"([^\"]*)\"\s*\{\s*\w+\+\+\s*\}"
        r"\s*END\s*\{\s*print\s+\w+(?:\s*\+\s*0)?\s*\}$", prog)
    if m:
        p.kind = "agg_count"
        p.agg_count = (int(m.group(1)[1:]) - 1, m.group(2))
        if prog.startswith("NR"):
            p.skip_rows = 1
        return p

    # order-preserving whole-line dedup: '!seen[$0]++'  → Op("uniq")
    m = re.match(r"^![\w]+\s*\[\$0\]\s*\+\+$", prog)
    if m:
        p.kind = "uniq"
        return p

    # reorder + filter on [count,line] rows via variables (uniq -c context):
    #   '{count = $1; url = $2; if (count >= 3) print url, count}'
    # $1 is count (col0), $2 is the group value (col1); print re-orders them.
    m = re.match(
        r"^\{\s*(\w+)\s*=\s*\$1\s*;\s*(\w+)\s*=\s*\$2\s*;\s*if\s*\(\s*(\w+)\s*"
        r"(>=|<=|==|!=|>|<)\s*(\S+)\s*\)\s*print\s+([^}]*)\}\s*$", prog)
    if m:
        var1, var2 = m.group(1), m.group(2)
        p.kind = "reorder_if"
        p.reorder = {"assign": {var1: 0, var2: 1},
                     "cond": (m.group(3), m.group(4), m.group(5)),
                     "print": [t.strip() for t in m.group(6).split(",")]}
        return p

    # group with a leading header skip: `NR > 1 {arr[$K]+=$V} END {for(k in arr)
    # printf "%.2f %s\n", arr[k], k}` (or `print`) — the first input record
    # (a column header) is dropped before grouping.  skip_rows is forwarded to
    # the read op so the rendered Nu skips the same header line.
    m = re.match(
        r"^NR\s*>\s*1\s*\{\s*(\w+)\[\$(\d+)\]\s*\+=\s*\$(\d+)\s*\}\s*END\s*"
        r"\{\s*for\s*\((\w+)\s+in\s+\1\)\s*printf\s*\"[^\"]*\"\s*,\s*(.+)\s*\}$",
        prog)
    if m:
        p.kind = "group"
        p.skip_rows = 1
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = int(m.group(3)) - 1
        p.grp_print = _awk_group_print(m.group(5), sum_token=m.group(1))
        return p
    m = re.match(
        r"^NR\s*>\s*1\s*\{\s*(\w+)\[\$(\d+)\]\s*\+=\s*\$(\d+)\s*\}\s*END\s*"
        r"\{\s*for\s*\((\w+)\s+in\s+\1\)\s*print\s+(.+)\s*\}$", prog)
    if m:
        p.kind = "group"
        p.skip_rows = 1
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = int(m.group(3)) - 1
        p.grp_print = _awk_group_print(m.group(5), sum_token=m.group(1))
        return p
    m = re.match(
        r"^NR\s*>\s*1\s*\{\s*(\w+)\[\$(\d+)\]\s*\+\+\s*\}\s*END\s*"
        r"\{\s*for\s*\((\w+)\s+in\s+\1\)\s*print\s+(.+)\s*\}$", prog)
    if m:
        p.kind = "group"
        p.skip_rows = 1
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = None
        p.grp_print = _awk_group_print(m.group(4), sum_token="")
        return p

    # group: {arr[$K] += $V} END {for(k in arr) print ...}  (sum)
    # anchored: a leading condition (e.g. `$9 >= 400 {...`) must NOT be
    # silently dropped — that is a different shape and gets an honest refusal.
    m = re.match(r"^\{\s*(\w+)\[\$(\d+)\]\s*\+=\s*\$(\d+)\s*\}\s*END\s*\{\s*"
                 r"for\s*\((\w+)\s+in\s+\1\)\s*print\s+(.+)\s*\}$", prog)
    if m:
        p.kind = "group"
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = int(m.group(3)) - 1
        p.grp_print = _awk_group_print(m.group(5), sum_token=m.group(1))
        return p

    # group sum via explicit assign: {arr[$K] = arr[$K] + $V} END {for...}
    m = re.match(r"^\{\s*(\w+)\[\$(\d+)\]\s*=\s*\1\[\$(\d+)\]\s*\+\s*\$(\d+)\s*\}\s*"
                 r"END\s*\{\s*for\s*\((\w+)\s+in\s+\1\)\s*print\s+(.+)\s*\}$", prog)
    if m:
        p.kind = "group"
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = int(m.group(4)) - 1
        p.grp_print = _awk_group_print(m.group(6), sum_token=m.group(1))
        return p

    # group: {arr[$K]++} END {for(k in arr) print ...}  (count)
    m = re.match(r"^\{\s*(\w+)\[\$(\d+)\]\s*\+\+\s*\}\s*END\s*\{\s*"
                 r"for\s*\((\w+)\s+in\s+\1\)\s*print\s+(.+)\s*\}$", prog)
    if m:
        p.kind = "group"
        p.grp_key = int(m.group(2)) - 1
        p.grp_val = None
        p.grp_print = _awk_group_print(m.group(4), sum_token="")
        return p

    # filter + select: $c op V {print ...}
    m = re.match(r"^\$\d+\s*(>=?|<=?|==|!=)\s*\S+\s*\{print\s+([^}]*)\}\s*$", prog)
    if m:
        op = m.group(1)
        if op == "!=":
            op = "!="
        col = int(re.search(r"\$(\d+)", prog.split("{")[0]).group(1)) - 1
        val = prog.split(op, 1)[1].split("{")[0].strip()
        p.kind = "filter_select"
        p.filter = (col, op, val)
        p.cols = _awk_cols(m.group(2))
        return p

    # regex filter + select: '$8 ~ /^5[0-9][0-9]$/ {print $1, $8}' — awk `~`
    # is a regex match, not a string compare; the value is the pattern (no
    # slashes after bashlex strips them from the single-quoted program).
    m = re.match(r"^(\$\d+)\s*~\s*/([^/]+)/\s*\{print\s+([^}]*)\}\s*$", prog)
    if m:
        col = int(m.group(1)[1:]) - 1
        p.kind = "filter_select"
        p.filter = (col, "~", m.group(2))
        p.cols = _awk_cols(m.group(3))
        return p

    # select: {print $a, $b}  (possibly preceded by BEGIN/patterns ignored)
    m = re.match(r"^\s*\{\s*print\s+([^}]*)\s*\}\s*$", prog)
    if m:
        p.kind = "select"
        p.cols = _awk_cols(m.group(1))
        return p

    # filter only: $c op V {next/...} — not needed for the subset
    raise _NeedsParser(prog)


def _awk_cols(expr: str) -> list[int]:
    if "$(" in expr:
        # `$(NF-1)` is a relative-from-end field, not the literal `$1` inside —
        # the fast path cannot express it; defer to the restricted parser.
        raise _NeedsParser(expr)
    cols = [int(x) - 1 for x in re.findall(r"\$(\d+)", expr)]
    if not cols:
        raise ConversionError(f"awk print with no $-columns: {expr[:40]}")
    return cols


def _awk_group_print(end_print: str, sum_token: str) -> list[str]:
    """Order of tokens in the END print list.

    ``end_print`` is the substring after ``print`` (e.g. ``r[c], c``).  A
    bracketed array reference is the aggregate (sum or count); a bare
    identifier (the loop variable) is the group key.
    """
    exprs = [e.strip() for e in end_print.split(",")]
    if not exprs or exprs == [""]:
        return ["sum", "key"] if sum_token else ["key", "count"]
    order = []
    for expr in exprs:
        if not expr:
            continue
        if re.search(r"\[", expr):
            order.append("sum" if sum_token else "count")
        elif re.fullmatch(r"[a-zA-Z_]\w*", expr):
            order.append("key")
        else:
            order.append("sum" if sum_token else "count")
    return order


# ---------------------------------------------------------------------------
# jq minimal recognizer
# ---------------------------------------------------------------------------

def _jq_conds(cond_str: str) -> list:
    """Parse a jq ``select`` condition into ``[{field, op, val, cast, default}]``.

    Supports ``.f op v``, ``(.f | tonumber) op v`` (cast), ``(.f // 0) op v``
    (default), and ``and``-chains."""
    conds = []
    for part in re.split(r"\s+and\s+", cond_str):
        part = part.strip()
        if not part:
            continue
        cast = bool(re.search(r"\|\s*tonumber\b", part))
        default = None
        dm = re.search(r"//\s*(\d+)", part)
        if dm:
            default = dm.group(1)
        s = re.sub(r"\|\s*(?:tonumber|tostring)\b", "", part)
        s = re.sub(r"//\s*\d+", "", s)
        s = s.replace("(", "").replace(")", "").strip()
        m = re.fullmatch(r"\.(\w+)\s*(>=?|<=?|==|!=)\s*(\S+)", s)
        if not m:
            raise ConversionError(f"unsupported jq select cond: {part}")
        conds.append({"field": m.group(1), "op": m.group(2), "val": m.group(3),
                      "cast": cast, "default": default})
    if not conds:
        raise ConversionError(f"unsupported jq select: {cond_str}")
    return conds


def _jq_fields(tail: str) -> list:
    """Field projection after a ``[.[] | select(...)][0:N][]`` slice: a render
    template ``"\\\\(.a) \\\\(.b)"`` or a bare ``.field``."""
    if not tail:
        return []
    tm = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', tail)
    if tm:
        fields = re.findall(r"\\\(\s*\.(\w+)\s*\)", tm.group(1))
        if not fields:
            raise ConversionError(f"unsupported jq template {tail[:40]}")
        return fields
    fm = re.fullmatch(r"\.(\w+)", tail.strip())
    if fm:
        return [fm.group(1)]
    raise ConversionError(f"unsupported jq slice projection {tail[:40]}")


def _parse_jq(filt: str) -> dict:
    r"""Return a dict for supported jq filter shapes.

    Supported:
      .[] | select(.f <op> v [and ...]) | "(str(a) str(b))"   -> where/render
      .[] | select(...) | .field                          -> where/project
      [.[] | select(...)][0:N][] | <project>              -> where/first N
      group_by(.g)[] | "\(.[0].g) \(([.[].x] | add / length * 10 | floor / 10))"
                                                          -> group-by/render (round-1)
    """
    # strip whole-line `#` comments and collapse the multi-line filters agents
    # write inside `command -v jq` guards
    filt = re.sub(r"(?m)#.*$", "", filt)
    filt = re.sub(r"\s+", " ", filt.strip())

    if filt.count("group_by") == 1 and re.search(r"group_by\(\.\w+\)\s*\[?\]?", filt):
        g = re.search(r"group_by\(\.(\w+)\)", filt)
        field = g.group(1)
        template = re.search(r'"([^"]*)"', filt)
        return {"group_by": field, "template": template.group(1) if template else ""}

    # slice form: [.[] | select(COND)][0:N][] | <project>
    m = re.fullmatch(
        r"\[\s*\.\[\]\s*\|\s*select\((.+?)\)\s*\]\s*\[\s*0:(\d+)\s*\]\s*\[\]\s*(?:\|\s*(.+))?",
        filt)
    if m:
        return {"select": _jq_conds(m.group(1)), "fields": _jq_fields(m.group(3)),
                "first": int(m.group(2))}

    # .[] | select(...) | "\(.a) ..."
    m = re.fullmatch(r"\.\[\]\s*\|\s*select\((.+)\)\s*\|\s*\"(.*)\"", filt)
    if m:
        fields = re.findall(r"\\\(\s*\.(\w+)\s*\)", m.group(2))
        if not fields:
            raise ConversionError(f"unsupported jq select: {filt}")
        return {"select": _jq_conds(m.group(1)), "fields": fields}

    # .[] | select(...) | .field     (bare field projection, e.g. `.host`)
    m = re.fullmatch(r"\.\[\]\s*\|\s*select\((.+)\)\s*\|\s*\.(\w+)\s*", filt)
    if m:
        return {"select": _jq_conds(m.group(1)), "fields": [m.group(2)]}

    raise ConversionError(f"unsupported jq filter: {filt[:60]}")


# ---------------------------------------------------------------------------
# pipeline recognition
# ---------------------------------------------------------------------------

def _file_ext_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".tsv":
        return "tsv"
    if ext == ".csv":
        return "csv"
    if ext == ".json":
        return "json"
    if ext in (".log", ".txt", ".md") or "access" in path.lower():
        return "log"
    return "text"


def _find_file_arg(args: list[str]) -> str | None:
    """The data file argument (paths ending in a known data extension), or None."""
    for a in args:
        if re.search(r"\.(tsv|csv|json|log|txt)$", a) and not a.startswith("-"):
            if os.path.sep in a or "/" in a or re.search(r"[A-Za-z]:[\\/]", a):
                return a
            return a
    # a `$VAR` / `${VAR}` argument that resolves to a data file
    for a in args:
        m = re.match(r"^\$?\{?([A-Za-z_]\w*)\}?$", a)
        if m and _VAR_MAP.get(m.group(1)):
            return _VAR_MAP[m.group(1)]
    return None


# Script-level `VAR="path"` assignments (e.g. `log="access.log"`), resolved for
# `$VAR` / `${VAR}` file arguments.  The compat converter processes one script
# at a time, so a module-level map is safe.
_VAR_MAP: dict = {}


def _script_assignments(bash: str) -> dict:
    """Leading ``VAR="data.tsv"`` lines → {name: path} (file-like values only)."""
    out = {}
    for ln in bash.splitlines():
        m = re.match(r"\s*([A-Za-z_]\w*)=(.+?)\s*$", ln)
        if not m or "[" in ln or "]" in ln:
            continue
        val = m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or \
                (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if re.search(r"\.(tsv|csv|json|log|txt)$", val):
            out[m.group(1)] = val
    return out


def _resolved_path(a: str) -> str:
    """Resolve a ``$VAR`` / ``${VAR}`` arg to its script-assigned path."""
    m = re.match(r"^\$?\{?([A-Za-z_]\w*)\}?$", a)
    if m and _VAR_MAP.get(m.group(1)):
        return _VAR_MAP[m.group(1)]
    return a


def _env_assign(assigns) -> bool:
    """A command-scoped environment assignment cannot be silently discarded."""
    return False


def _stdin_source(redirs) -> str | None:
    """The ``< file`` stdin-redirect target, acting as the data source when a
    filter command reads its input from a redirect instead of a file argument
    (e.g. ``wc -l < out/orders.txt``)."""
    for inp, rtype, tgt in redirs:
        if rtype == "<" and inp in (None, 0):
            return tgt
    return None


def _tail_redirect(redirs) -> tuple:
    """The ``> file`` / ``>> file`` a pipeline's *last* command writes stdout to.

    Returns ``(path, append)``, or ``(None, False)`` when there is none.  Only a
    stdout redirect counts: ``2>`` and ``1>&2`` never touch the stream the
    fidelity contract observes (normalized stdout + workspace final state), so
    they stay ignored exactly as before.

    This used to be dropped on the floor: ``... | head -3 > out.txt`` translated
    to a pipeline that printed the three lines and left no file behind, which is
    a silent mistranslation of a construct agents write constantly.  It is now
    carried on the terminal ``render`` op and written by the renderer.
    """
    outs = [(rt, tgt) for inp, rt, tgt in redirs
            if rt in (">", ">>") and inp in (None, 1)]
    if not outs:
        return None, False
    if len(outs) > 1:
        raise ConversionError(
            f"more than one stdout redirect on a pipeline: {outs}")
    rtype, target = outs[0]
    return target, rtype == ">>"


def _render_op(path: str | None, append: bool) -> Op:
    """The pipeline's terminal render step, carrying the redirect it feeds.

    The redirect keys are only present when there *is* a redirect, so a pipeline
    without one keeps byte-for-byte the IR it had before this was supported.
    """
    if path is None:
        return Op("render")
    return Op("render", path=path, append=append)


def _tee_op(args: list[str]) -> Op:
    """``tee FILE`` — pass the stream through and write a copy of it to FILE."""
    append = False
    files = []
    for a in args:
        if a == "-a":
            append = True
        elif a.startswith("-"):
            raise ConversionError(f"unsupported tee flag {a!r}")
        else:
            files.append(a)
    if len(files) != 1:
        raise ConversionError("tee supports exactly one file argument")
    return Op("tee", path=files[0], append=append)


def _recognize_pipeline(pn) -> Pipeline:
    """Turn a bashlex pipeline node — or a bare single command node — into a
    recognized IR Pipeline."""
    while_loop = None
    if getattr(pn, "kind", None) == "command":
        cmds = [pn]
    else:
        cmds = []
        for part in getattr(pn, "parts", []) or []:
            if getattr(part, "kind", None) == "command":
                cmds.append(part)
            elif getattr(part, "kind", None) in ("pipe", "reservedword"):
                continue
            elif getattr(part, "kind", None) == "compound" \
                    and _is_while_node(part):
                # `A | B | while read …; do …; done` — the loop is the last
                # stage and *is* the statement's output, so it closes the
                # pipeline rather than being fed by a render.
                while_loop = part
                break
            else:
                raise ConversionError(f"unsupported pipeline part "
                                      f"{getattr(part, 'kind', None)}")

    if len(cmds) == 1 and _is_fileop_cmd(cmds[0]):
        return _fileop_pipeline(cmds[0])
    if len(cmds) == 1:
        n0, a0, asg0, red0 = _cmd_parts(cmds[0])
        if n0 == ":":
            # `: > file` — the bash null command with a truncating redirect.
            if len(red0) == 1 and red0[0][1] == ">":
                return Pipeline([Op("truncate", path=red0[0][2])])
            raise ConversionError("unsupported `:` command form")

    # --- build source + transform ops with field-shape tracking ---
    source = None
    ops: list[Op] = []
    fields: list = []          # bash field names of the CURRENT row shape
    sname = None
    hdr_skip = 0               # leading data/header rows dropped by tail -n +N
    fs_override = None         # -F for awk-read sources
    tail_redirs: list = []     # redirects on the LAST command of the pipeline

    for c in cmds:
        name, args, assigns, redirs = _cmd_parts(c)
        tail_redirs = redirs      # the last command's redirects win
        if name is None:
            if assigns:
                raise ConversionError("standalone assignment must be a statement; "
                                      "use VAR=value on its own line")
            continue
        if assigns and not _env_assign(assigns):
            raise ConversionError(
                f"temporary assignment on command line '{name}' not supported")

        # -------- source-introducing commands (only before source is set) -----
        if source is None:
            if name == "cat":
                if len(args) == 1:
                    sname, source = _mk_source(_resolved_path(args[0]), fields)
                    continue
                src = _stdin_source(redirs)
                if src is not None:
                    sname, source = _mk_source(src, fields)
                    continue
                raise ConversionError("multi-file cat not supported")
            if name == "tail":
                m = re.match(r"-n\s*\+\s*(\d+)", " ".join(args[:2]) + " ")
                if m and len(args) >= 3 and _looks_file(_resolved_path(args[-1])):
                    k = int(m.group(1))
                    sname, source = _mk_source(_resolved_path(args[-1]), fields)
                    hdr_skip = k - 1
                    continue
                # tail -n N F (last N records): source + last
                m2 = re.match(r"-n\s*(\d+)", " ".join(args[:2]) + " ")
                if m2 and len(args) >= 3 and _looks_file(_resolved_path(args[-1])):
                    sname, source = _mk_source(_resolved_path(args[-1]), fields)
                    ops.append(Op("tail", n=int(m2.group(1))))
                    continue
                raise ConversionError(f"unsupported tail form: {' '.join(args)}")
            if name == "awk":
                fs_override = _parse_awk_fs(args)
                prog = _awk_arg(args)
                ap = _parse_awk(prog, fs_override)
                fname = _find_file_arg(args) or _stdin_source(redirs)
                if fname is None:
                    # awk reading stdin — but we have no source yet → refuses to
                    # guess stdin data; only file-backed awk is supported here.
                    raise ConversionError(
                        "awk without a file argument and no upstream source "
                        "is not supported")
                sname, source = _mk_source(fname, fields)
                _emit_awk(source[0], ap, ops, fields)
                hdr_skip = max(hdr_skip, getattr(ap, "skip_rows", 0))
                continue
            if name == "jq":
                fname = _find_file_arg(args) or _stdin_source(redirs)
                if fname is None:
                    raise ConversionError("jq without a file argument not supported")
                sname, source = _mk_source(fname, fields)
                # bashlex word text is already unquoted; stripping here would
                # eat the template's trailing '"' of `| "\(.a) \(.b)"`.
                filt = next((a for a in args if a.startswith(("'", '"'))), "")
                if not filt:
                    filt = args[1] if len(args) > 1 else ""
                q = _parse_jq(filt)
                _emit_jq(q, ops, fields)
                continue
            if name in ("head", "sort", "grep", "sed", "uniq", "wc", "cut"):
                fname = _find_file_arg(args) or _stdin_source(redirs)
                if fname is not None:
                    sname, source = _mk_source(fname, fields)
                    # the introducing command is its own first transform
                    if name == "grep":
                        ops.append(_grep_op(args, fields))
                    elif name == "sort":
                        ops.append(_sort_op(args, fields))
                    elif name == "sed":
                        ops.append(_sed_op(args))
                        fields = fields[:1]
                    elif name == "uniq":
                        ops.append(Op("uniq_c" if "-c" in args else "uniq"))
                        fields = ["count", "line"] if "-c" in args \
                            else fields[:1]
                    elif name == "wc":
                        if "-l" in args or args == []:
                            ops.append(Op("count"))
                        else:
                            raise ConversionError(f"unsupported wc flags: {args}")
                    elif name == "head":
                        m = re.search(r"-n?\s*(\d+)", " ".join(args))
                        if m:
                            ops.append(Op("head", n=int(m.group(1))))
                    elif name == "cut":
                        ops.append(_cut_op(args))
                        fields = fields[:1]
                    continue
        # ----------------------------------------------------------------

        # -------- transform commands (source must now exist) -----
        if name == "awk":
            fs_override = _parse_awk_fs(args)
            prog = _awk_arg(args)
            ap = _parse_awk(prog, fs_override)
            _emit_awk(source[0], ap, ops, fields)
            hdr_skip = max(hdr_skip, getattr(ap, "skip_rows", 0))
            continue
        if name == "sort":
            ops.append(_sort_op(args, fields))
            continue
        if name == "uniq":
            ops.append(Op("uniq_c" if "-c" in args else "uniq"))
            if "-c" in args:
                fields = ["count", "line"]
            else:
                fields = fields[:1]
            continue
        if name == "head":
            m = re.search(r"-n?\s*(\d+)", " ".join(args))
            if m:
                ops.append(Op("head", n=int(m.group(1))))
            continue
        if name == "tail":
            # Both spellings of a count: `-n 5` and the short `-5`.  The match
            # is anchored on the first argument, so a `-2` inside a *filename*
            # (`tail -f log-2.txt`) can never be read as a count — and an
            # unrecognized tail now refuses instead of being dropped, which is
            # what it used to do: `head -2 f | tail -1` kept the head and lost
            # the tail, silently answering a different question.
            m = re.match(r"^-n?\s*(\d+)\b", " ".join(args))
            if m is None:
                raise ConversionError(f"unsupported tail form: {' '.join(args)}")
            if source is None:
                source = _mk_source(args[-1], fields)[1]
            ops.append(Op("tail", n=int(m.group(1))))
            continue
        if name == "wc":
            if "-l" in args or args == []:
                ops.append(Op("count"))
            else:
                raise ConversionError(f"unsupported wc flags: {args}")
            continue
        if name == "grep":
            ops.append(_grep_op(args, fields))
            continue
        if name == "sed":
            ops.append(_sed_op(args))
            fields = fields[:1]
            continue
        if name == "tr":
            if _is_wc_padding_strip(args, ops):
                continue
            ops.append(_tr_op(args))
            fields = fields[:1]
            continue
        if name == "cut":
            ops.append(_cut_op(args))
            fields = fields[:1]
            continue
        if name == "find":
            if source is None:
                sname = "files"
                source = ("files",)
                ops.extend(_find_ops(args, fields))
            continue
        if name in ("cat",):
            continue  # stdin passthrough mid-pipeline
        if name == "tee":
            ops.append(_tee_op(args))
            continue
        if name == "printf":
            raise ConversionError(
                "printf is only supported as a standalone statement, not as a "
                "pipeline stage")
        raise ConversionError(f"unsupported pipeline command '{name}'")

    if while_loop is not None:
        if tail_redirs:
            raise ConversionError("a redirect on a `while` loop stage")
        loop = _while_read_op(while_loop)
        if loop.params["path"] is not None:
            raise ConversionError(
                "a `while` loop stage cannot also redirect its input")
        if sname is None:
            raise ConversionError(
                "pipeline with no file-backed source is not supported")
        if not ops and hdr_skip == 0:
            # `cat F | while read x` reads F's lines, which is exactly what
            # `while read x; done < F` reads (and no transform has run in
            # between).  The two Bash spellings collapse onto the one IR form —
            # the file — so the delivered Nushell, which is the same for both,
            # recovers to the same op.
            loop.params["path"] = source[1]
            return Pipeline([loop])
        ops.append(loop)
        return Pipeline(ops)

    if sname is None:
        raise ConversionError(
            "pipeline with no file-backed source is not supported")

    tail_path, tail_append = _tail_redirect(tail_redirs)

    if sname == "files":
        # find emits its own opener (source_find); there is no read op.
        ops.append(_render_op(tail_path, tail_append))
        return Pipeline(ops)

    # Emit the opener first (the generator keys column naming off its kind).
    ops.insert(0, Op("read", kind=sname, path=source[1], skip_header=hdr_skip))
    ops.append(_render_op(tail_path, tail_append))
    return Pipeline(ops)


def _mk_source(path: str, fields) -> tuple:
    kind = _file_ext_kind(path)
    if kind in ("tsv", "csv"):
        fields[:] = []
    elif kind == "json":
        fields[:] = []
    elif kind == "log":
        fields[:] = []
    else:
        fields[:] = []
    return kind, (kind, path)


def _looks_file(a: str) -> bool:
    return bool(re.search(r"(?:\.(?:tsv|csv|json|log|txt))$", a)) or "/" in a


def _awk_arg(args: list[str]) -> str:
    """The awk program string (the quoted single argument)."""
    def unq(s: str) -> str:
        # bashlex already strips surrounding quotes from word values; only drop
        # a quote pair when it is genuinely wrapped. `.strip('"\'' )` would eat
        # the closing quote of a trailing string literal (`$5=="pending"`).
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
            return s[1:-1]
        return s

    for a in args:
        if a.startswith("'") or ("{print" in a or "END" in a or "++" in a or "ar" in a):
            return unq(a)
        if re.search(r"\{\$|print|END|for\(|~ /", a):
            return unq(a)
        # bare-pattern program: bashlex strips single quotes, so a pattern
        # like `NR>1 && $5=="pending"` arrives with no quote marker. It is the
        # arg that carries awk-pattern syntax yet is neither the -F value nor
        # a file.
        if (re.search(r"(?:NR|NF)\b|\$\w+\s*(?:==|!=|>=|<=|>|<)|&&|\|\||~ /", a)
                and not _looks_file(a)
                and not a.startswith("-F")
                and not re.fullmatch(r"-[a-zA-Z0-9]+", a)):
            return unq(a)
    raise ConversionError("awk program not found")


def _emit_awk(kind: str, ap: _AwkProg, ops: list, fields) -> None:
    if kind == "files":
        raise ConversionError(
            "awk over find output is not supported; keep find self-contained")
    # After `uniq -c` the row shape is [count, line]; a trailing awk that only
    # touches $1/$2 is a *reorder*, not a fresh awk select on source columns.
    if fields == ["count", "line"] and ap.kind in ("select", "filter_select",
                                                   "reorder_if"):
        if ap.kind == "reorder_if":
            assign, cond, print_toks = (ap.reorder["assign"],
                                        ap.reorder["cond"],
                                        ap.reorder["print"])
            cvar, cop, cval = cond
            filt = (assign[cvar], cop, cval)
            cols = [assign[t] for t in print_toks]
            ops.append(Op("uniq_reorder", cols=cols, filter=filt))
            return
        filt = ap.filter if ap.kind == "filter_select" else None
        ops.append(Op("uniq_reorder", cols=ap.cols, filter=filt))
        return
    if ap.kind == "uniq":
        # `!seen[$0]++` dedups whole lines keeping first occurrence — Nu `uniq`
        # over the current stream is the faithful equivalent.
        ops.append(Op("uniq", global_dedup=True))
        return
    if ap.kind == "agg_sum":
        ops.append(Op("agg", mode="sum", sumcol=ap.agg_sum))
        return
    if ap.kind == "agg_avg":
        fcol, val, sumcol = ap.agg_avg
        ops.append(Op("agg", mode="avg", fcol=fcol, val=val, sumcol=sumcol,
                      rint=ap.rint))
        return
    if ap.kind == "agg_count":
        fcol, val = ap.agg_count
        ops.append(Op("agg", mode="count", fcol=fcol, val=val))
        return
    if ap.kind == "select":
        ops.append(Op("awk_get", cols=ap.cols))
        if len(ap.cols) == 1:
            fields[:] = [f"c{ap.cols[0]}"]
        else:
            fields[:] = [f"c{i}" for i in ap.cols]
        return
    if ap.kind == "filter_select":
        col, op, val = ap.filter
        if not ap.cols:
            raise ConversionError("filter_select without print cols")
        if kind in ("log", "text"):
            # line materialization has no ready-made field filter op; awk_fget
            # materializes the needed log fields then filters in place.
            ops.append(Op("awk_fget", fcol=col, op=op, val=val,
                          cols=ap.cols))
        else:
            ops.append(Op("filter", col=col, op=op, val=val))
            ops.append(Op("awk_get", cols=ap.cols))
        if len(ap.cols) == 1:
            fields[:] = [f"c{ap.cols[0]}"]
        else:
            fields[:] = [f"c{i}" for i in ap.cols]
        return
    if ap.kind == "group":
        if ap.grp_val is None:
            ops.append(Op("group_count", key=ap.grp_key, print=ap.grp_print))
        else:
            ops.append(Op("group_sum", key=ap.grp_key, val=ap.grp_val,
                          print=ap.grp_print))
        return
    if ap.kind == "bucket":
        ops.append(Op("bucket", col=ap.bucket["col"],
                      prefix=ap.bucket["prefix"]))
        fields[:] = [f"c{ap.bucket['col']}"]
        return
    if ap.kind == "program":
        # General restricted-awk classifier output: select / scalar / group.
        # The select family projects to one or more fields (the vals stream);
        # group families emit key/count or key/sum rows with a print order.
        spec = ap.spec
        if spec.family == "select":
            fields[:] = [f"c{i}" for i in range(len(spec.print_args))]
        elif spec.family in ("group_count", "group_sum"):
            fields[:] = [tok for tok, _ in spec.gprint]
        ops.append(Op("awk_program", spec=spec))
        return
    raise ConversionError(f"unrecognized awk kind {ap.kind}")


def _emit_jq(q: dict, ops: list, fields) -> None:
    if "group_by" in q:
        ops.append(Op("jq_group", field=q["group_by"], template=q["template"]))
        fields[:] = [f".{q['group_by']}", "extra"]
        return
    ops.append(Op("jq_where", conds=q["select"], fields=q["fields"],
                  first=q.get("first")))
    if len(q["fields"]) == 1:
        fields[:] = [q["fields"][0]]
    else:
        fields[:] = q["fields"]


def _sort_op(args: list[str], fields) -> Op:
    joined = " ".join(args)
    opts = {"numeric": False, "reverse": False, "col": None, "unique": False}
    if "-u" in args:
        opts["unique"] = True
    if "-n" in args or "-rn" in args or "-nr" in args:
        opts["numeric"] = True
    if "-rn" in args or "-nr" in args or "-r" in args:
        opts["reverse"] = True
    # `-k N,M<flags>` (e.g. `-k2,2nr`) carries its sort flags in the key spec.
    # A script may chain several keys (`sort -k2,2nr -k1,1`); capture them all.
    kmatches = list(re.finditer(r"-k\s*(\d+)(?:,\s*\d+)?([a-z]*)", joined))
    if kmatches:
        keys = [{"col": int(km.group(1)) - 1,
                 "numeric": "n" in (km.group(2) or ""),
                 "reverse": "r" in (km.group(2) or "")}
                for km in kmatches]
        # Global -n/-r fill in for keys whose own spec carries no flags.
        if not any(k["numeric"] for k in keys) and opts["numeric"]:
            for k in keys:
                k["numeric"] = True
        if not any(k["reverse"] for k in keys) and opts["reverse"]:
            for k in keys:
                k["reverse"] = True
        first = keys[0]
        opts["col"] = first["col"]
        opts["numeric"] = first["numeric"]
        opts["reverse"] = first["reverse"]
        opts["keys"] = keys
    return Op("sort", **opts)


def _grep_op(args: list[str], fields) -> Op:
    invert = "-v" in args
    count = "-c" in args
    # -o/-Eo/-oE = print each *match* on its own line (match extraction).  The
    # IR has no exact rendering for per-match emission; silently treating it
    # as a line filter would mistranslate (COMPAT_SPEC_v1.0 §1 exactness
    # contract), so refuse honestly.
    if any(re.fullmatch(r"-\w*o\w*", a) for a in args if a.startswith("-")):
        raise ConversionError("grep -o (per-match extraction) is not "
                              "supported by the compat layer")
    pat = next((a for a in args if not a.startswith("-")), None)
    if pat is None:
        raise ConversionError("grep without a pattern")
    return Op("grep", pat=pat.strip("'\""), invert=invert, count=count)


_SED_RE = re.compile(
    r"^s(?P<d>[/|])(?P<pat>.*?)(?<!\\)(?P=d)(?P<rep>.*?)(?<!\\)(?P=d)"
    r"(?P<flags>[igp]*)$")


def _bre_to_ere(pat: str) -> str:
    """Normalize GNU BRE escapes to ERE for Nu's rust/fancy regex.

    BRE uses ``\\(`` for a capture group, ``\\{`` for a quantifier; in ERE
    (and rust-regex) those backslashes are *literal* punctuation — a group
    written ``\\(...)`` would silently match literal parens.  ``\\[``/``\\]``
    keep their backslash (literal-bracket escape is identical in both)."""
    return re.sub(r"\\([(){}])", r"\1", pat)


def _sed_op(args: list[str]) -> Op:
    if "-i" in args:
        raise ConversionError("in-place sed (-i) is not supported")
    prog = next((a for a in args if a.startswith("s/") or a.startswith("s|")),
                None)
    if prog is None:
        raise ConversionError(f"unsupported sed program: {args}")
    prog = prog.strip("'\"")
    m = _SED_RE.match(prog)
    if not m:
        raise ConversionError(f"unsupported sed program: {prog[:40]}")
    flags = m.group("flags")
    if "i" in flags or "g" in flags:
        raise ConversionError(
            f"unsupported sed flags: {flags!r} (only /p is supported)")
    # `-n` suppresses the default line print; combined with a `/p` modifier
    # the output is exactly the lines whose pattern matched (replacement
    # applied) — rendered as a where-filter in Nu.
    keep_matched = "p" in flags or "-n" in args
    if keep_matched and "p" not in flags:
        # `-n` with a plain s/// (no p) prints *nothing*; refuse rather than
        # guess the intent.
        raise ConversionError("sed -n without a /p modifier is not supported")
    return Op("sed", pat=_bre_to_ere(m.group("pat")), rep=m.group("rep"),
              regex=True, keep_matched=keep_matched)


def _tr_op(args: list[str]) -> Op:
    if len(args) < 2:
        raise ConversionError("tr needs two char sets")
    f, t = args[0].strip("'\"") or "a-z", args[1].strip("'\"") or ""
    if (f, t) in (("a-z", "A-Z"), ("[:lower:]", "[:upper:]")):
        return Op("tr", mode="upper")
    if (f, t) in (("A-Z", "a-z"), ("[:upper:]", "[:lower:]")):
        return Op("tr", mode="lower")
    raise ConversionError(f"unsupported tr set {f!r} {t!r}")


def _cut_op(args: list[str]) -> Op:
    joined = " ".join(args)
    m = re.search(r"-d\s*['\"]?(.)['\"]?", joined)
    mf = re.search(r"-f\s*(\d+)", joined)
    if not mf:
        raise ConversionError(f"unsupported cut form: {args}")
    if m:
        # `-d' '` / `-d':'` : the literal character right after -d.  A `-d`
        # A literal `\t` string is not a supported delimiter here.
        delim = m.group(1)
    else:
        # POSIX `cut` defaults to tab when no -d is given.
        delim = "\t"
    return Op("cut", delim=delim, col=int(mf.group(1)) - 1)


def _find_ops(args: list[str], fields) -> list:
    if not args:
        raise ConversionError("find without a directory")
    d = args[0].strip()
    size = None
    printf = None              # '%s %p' → size then path
    sep = " "                  # the separator -printf puts between the fields
    for i, a in enumerate(args):
        if a == "-size" and i + 1 < len(args):
            s = args[i + 1]
            mm = re.fullmatch(r"\+(\d+)([kM])", s)
            if mm:
                mult = 1024 if mm.group(2) == "k" else 1024 * 1024
                size = int(mm.group(1)) * mult
            else:
                raise ConversionError(f"unsupported -size spec {s}")
        if a == "-printf" and i + 1 < len(args):
            sep = _find_printf_spec(args[i + 1])
            printf = "sp"
    ops = [Op("source_find", path=d)]
    if "-type" in args:
        kind = args[args.index("-type") + 1]
        if kind != "f":
            raise ConversionError(f"find -type {kind} not supported")
        ops.append(Op("f_filter_file"))
    fields[:] = ["fname", "fsize"]
    # The separator only enters the IR when it is not the default space, so a
    # `-printf '%s %p\n'` find keeps exactly the IR it had before tabs were
    # supported.
    extra = {} if sep == " " else {"sep": sep}
    if size is not None:
        # `-size` alone prints paths only; with `-printf '%s %p'` it prints
        # size-then-path.
        ops.append(Op("f_gt_size", size=size,
                      print_order=(["sz", "name"] if printf else ["name"]),
                      **extra))
    elif printf:
        # -printf with no -size: still print size/path pairs
        ops.append(Op("f_size", print_order=["sz", "name"], **extra))
    else:
        ops.append(Op("f_list", print_order=["name"]))
    return ops


# `find DIR -printf '%s<SEP>%p\n'` — the only two find formats in the subset:
# size-then-path joined by a space, or by a tab.
#
# bashlex hands a *double*-quoted word over de-escaped, so the same source
# spelling arrives in more than one form and the pattern has to know both:
#   '%s %p\n'   -> '%s %p\n'      "%s %p\n"  -> '%s %pn'
#   '%s\t%p\n'  -> '%s\t%p\n'     "%s\t%p\n" -> '%st%pn'   (refused)
# The space forms are the ones the layer has always accepted.  The last row is
# refused deliberately: with the backslash gone, `%st%pn` is indistinguishable
# from a format that really prints a `t` between the fields, so reading it as a
# tab would be a guess.  The tab spelling in the subset is the single-quoted one.
_FIND_PRINTF = re.compile(r"^%s(?P<sep> |\t|\\t)%p(?:\\n|n)?$")


def _find_printf_spec(spec: str) -> str:
    """The field separator of a `find -printf` spec, as a real character."""
    m = _FIND_PRINTF.match(spec)
    if not m:
        raise ConversionError(f"unsupported -printf spec {spec}")
    return "\t" if m.group("sep") in ("\t", "\\t") else " "


# ---------------------------------------------------------------------------
# file-operator commands (cp/mv/mkdir/rm/touch) and plain echo/assign
# ---------------------------------------------------------------------------

_FILECOPS = {"cp": "cp", "mv": "mv", "mkdir": "mkdir", "rm": "rm",
             "touch": "touch"}


def _is_wc_padding_strip(args: list, ops: list) -> bool:
    """``… | wc -l | tr -d '[:space:]'`` — stripping the blanks `wc` pads with.

    `wc -l` prints its count right-aligned in a blank-padded field, and taking
    that padding off again is the whole point of the `tr`.  The Nu model's
    ``count`` op *is* the bare count — there is nothing padded to strip — so the
    `tr` is an exact no-op on this side and the delivered Nushell is the same
    with or without it.  Dropping it is the same kind of rescue as dropping a
    trailing ``|| true`` (see ``_strip_noop_cmdsubst``): on any other stream
    `tr -d` really does delete characters, and stays refused.
    """
    if not ops or ops[-1].op_type != "count":
        return False
    return args in (["-d", "[:space:]"], ["-d", " "])


def _is_fileop_cmd(cmd_node) -> bool:
    name, args, assigns, redirs = _cmd_parts(cmd_node)
    return name in _FILECOPS


def _fileop_pipeline(cmd_node) -> Pipeline:
    name, args, assigns, redirs = _cmd_parts(cmd_node)
    if name == "mkdir":
        if "-p" in args:
            args = [a for a in args if a != "-p"]
        if len(args) != 1:
            raise ConversionError("mkdir supports one directory")
        return Pipeline([Op("mkdir", path=args[0])])
    if name == "cp":
        if len(args) != 2:
            raise ConversionError("cp supports SRC DST")
        return Pipeline([Op("cp", src=args[0], dst=args[1])])
    if name == "mv":
        if len(args) != 2:
            raise ConversionError("mv supports SRC DST")
        return Pipeline([Op("mv", src=args[0], dst=args[1])])
    if name == "rm":
        force = "-rf" in args or "-f" in args
        danger = "rm -rf" and any(a in ("/", "/*", r"\*", "*") for a in args)
        if "-rf" in args and len(args) <= 2:
            nonflags = [a for a in args if not a.startswith("-")]
            if any(a.rstrip("/") in ("", "/") for a in nonflags):
                raise ConversionError("refusing rm -rf / (destructive on purpose)")
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) == 1:
            return Pipeline([Op("rm", path=paths[0], rec=("-r" in args))])
        raise ConversionError("rm supports one path")
    if name == "touch":
        if len(args) != 1:
            raise ConversionError("touch supports one file")
        return Pipeline([Op("touch", path=args[0])])
    raise ConversionError(f"unsupported file op {name}")


# ---------------------------------------------------------------------------
# top-level statements
# ---------------------------------------------------------------------------

# A control preamble (`#!/usr/bin/env bash`, `set -euo pipefail`) is shell
# environment noise an agent is prone to add; it has no Nu equivalent and is
# dropped before parsing rather than refused.  Only flag/option tokens are
# accepted, so a positional `set x y` (which changes $1..) is still refused.
_PROLOGUE_LINE = re.compile(
    r"^\s*(?:set\s+(?:(?:-\w+(?:\s+|$))"
    r"|(?:pipefail|errexit|nounset|xtrace|posix|verbose|noclobber)(?:\s+|$))+"
    r"|#!.*)$")

# Defensive existence guard some agents prefix scripts with:
#   if [[ ! -f "file.json" ]]; then echo "Error: ..." >&2; exit 1; fi
# Existence depends on the caller's workspace, so these guards are refused.
_GUARD_IF_RE = re.compile(
    r"^\s*if\s+\[\[\s*!\s*-[fe]\s+[\"']?[^\"'\]]+[\"']?\s*\]\]\s*(?:;|\n)\s*"
    r"then\s*\n(?:[^\n]*\n)*?\s*fi\s*\n?",
    re.MULTILINE)


def _strip_prologue(bash: str) -> str:
    keep = [ln for ln in bash.splitlines() if not _PROLOGUE_LINE.match(ln)]
    text = "\n".join(keep).strip("\n")
    if _GUARD_IF_RE.search(text):
        raise ConversionError("File-existence guards require runtime state and cannot be projected safely")
    text = text.strip("\n")
    if not text:
        return ""
    return text


# A trailing no-op `|| true` / `|| :` inside a `$( ... )` command substitution.
# See COMPAT_SPEC_v1.0 §2.6 #2.  `true`/`:` are builtins returning 0 with no
# output, so `cmd || true` preserves `cmd`'s stdout verbatim and differs only
# in exit status — which the subset never observes (errexit is always elided by
# _strip_prologue and top-level `&&`/`||` chaining is refused), so removing it
# is an exact no-op that rescues a parse bashlex cannot otherwise handle.
_NOOP_CMDSUBST_TAIL = re.compile(r"\|\|\s*(?:true|:)\s*$")


def _strip_noop_cmdsubst(bash: str) -> str:
    """Remove a trailing `|| true` / `|| :` from `$( ... )` substitutions.

    Conservative by construction: only a NON-NESTED span with no heredoc marker
    and no unbalanced parens is touched, and only when the `$(` is NOT inside a
    single-quoted literal (the one context where `$(...)` is inert text).  Any
    other shape is left verbatim — bashlex then refuses exactly as before; this
    pass never creates a translation, it only ever rescues an exact one.
    """
    n = len(bash)
    out = []
    i = 0
    q = None            # current shell quote: "'", '"', or None
    while i < n:
        if q is None and bash.startswith("$(", i):
            # scan to the matching close paren from i+2 (quote-aware)
            depth = 1
            k = i + 2
            lq = None
            nested = False
            while k < n and depth:
                c = bash[k]
                if lq:
                    if c == lq and bash[k - 1] != "\\":
                        lq = None
                elif c in ("'", '"'):
                    lq = c
                elif c == "(":
                    if bash[k - 1] == "$":
                        nested = True
                    depth += 1
                elif c == ")":
                    depth -= 1
                k += 1
            if depth != 0:
                out.append(bash[i:k])          # unbalanced — leave verbatim
                i = k
                continue
            inner = bash[i + 2:k - 1]
            if not nested and "<<" not in inner:
                inner = _NOOP_CMDSUBST_TAIL.sub("", inner)
            out.append("$(" + inner + ")")
            i = k
            continue
        ch = bash[i]
        if q is None:
            if ch == "'":
                q = "'"
            elif ch == '"':
                q = '"'
        elif ch == q and (q == "'" or i == 0 or bash[i - 1] != "\\"):
            q = None
        out.append(ch)
        i += 1
    return "".join(out)


def _single_stmt(node, ctx="body") -> Stmt:
    """One statement from a command/pipeline/compound/if node (used for bare
    bodies, where bashlex yields a single node instead of a list)."""
    k = getattr(node, "kind", None)
    if k == "command":
        name, args, assigns, redirs = _cmd_parts(node)
        if assigns:
            return _assignment_stmt(assigns[0])
        if name == "exit":
            # In the compat happy-path a trailing `exit N` is a no-op (it only
            # terminates the script); the error-exit guards are dropped whole.
            return None
        if name == "echo":
            return _echo_stmt(args, redirs)
        if name == "printf":
            return _printf_stmt(args, redirs)
        if name in _FILECOPS:
            return _fileop_pipeline(node)
        if name in ("cd", "export", "source", "."):
            raise ConversionError(
                f"'{name}' is not supported by the compat layer")
        return _recognize_pipeline(node)
    if k == "pipeline":
        return _recognize_pipeline(node)
    if k == "compound":
        return _compound_stmt(node)
    if k == "if":
        return _if_stmt(list(getattr(node, "parts", []) or []))
    raise ConversionError(f"unsupported {ctx} node kind {k}")

def bash_to_ir(bash: str) -> Script:
    """Parse a Bash script (bashlex AST) into shell IR."""
    bash = _strip_prologue(bash)
    bash = _strip_noop_cmdsubst(bash)
    global _VAR_MAP, _RAW_BASH
    _VAR_MAP = _script_assignments(bash)
    _RAW_BASH = bash
    try:
        nodes = bashlex.parse(bash)
    except NotImplementedError as e:
        raise ConversionError(f"bashlex cannot parse: {e}")
    except bashlex.errors.ParsingError as e:
        raise ConversionError(f"bash parse error: {e}")

    stmts = []
    for node in _segmented_top_level(nodes):
        k = getattr(node, "kind", None)
        if k == "pipeline":
            stmts.append(_recognize_pipeline(node))
        elif k == "command":
            name, args, assigns, redirs = _cmd_parts(node)
            if name is None and assigns:
                stmts.append(_assignment_stmt(assigns[0]))
                continue
            if assigns:
                raise ConversionError(
                    f"assignment prefix on command '{name}' not supported")
            if name == "exit":
                continue  # trailing script terminator — a no-op in the happy path
            if name in _FILECOPS:
                stmts.append(_fileop_pipeline(node))
            elif name == "echo":
                stmts.append(_echo_stmt(args, redirs))
            elif name == "printf":
                stmts.append(_printf_stmt(args, redirs))
            elif name in ("cd", "export", "source", "."):
                raise ConversionError(
                    f"'{name}' is not supported by the compat layer")
            else:
                stmts.append(_recognize_pipeline(node))
        elif k == "compound":
            stmts.append(_compound_stmt(node))
        elif k == "if":
            raise ConversionError("mis-placed if node")
        else:
            raise ConversionError(f"unsupported statement node kind {k}")
    return Script(stmts)


def _assignment_stmt(assign) -> Stmt:
    aname, aval = assign
    return Stmt("assign", {"name": aname, "value": aval})


def _echo_stmt(args: list, redirs) -> Stmt:
    if not args:
        return Stmt("echo", {"text": ""})
    if redirs:
        raise ConversionError("echo with redirect is unsupported; "
                              "use echo ... > file at statement level")
    return Stmt("echo", {"text": " ".join(args)})


# ---------------------------------------------------------------------------
# printf: a formatted line to stdout or to a file
# ---------------------------------------------------------------------------

# `printf 'FMT' args... > file` — the one form agents reach for when they want a
# TSV line on disk, e.g.
#   printf '%s\t%s\t%s\n' "$orders" "$customers" "$categories" > summary/counts.txt
# The subset is deliberately narrow: a format built from literals and `%s`
# holes, exactly one argument per hole, and a format that ends its line with a
# newline.  Everything else is refused rather than approximated — `%d`/`%.2f`
# need a value *conversion* (truncate, round, pad) with no faithful line-level
# rendering, a format shorter than the argument list needs bash's format-reuse
# loop, and `\0NNN`/`%b` need a byte-level escape the line model doesn't carry.
_PRINTF_ESCAPES = {"t": "\t", "n": "\n", "\\": "\\"}
_PRINTF_VAR = re.compile(r"^\$\{?(\w+)\}?$")


def _printf_parts(fmt: str) -> list:
    """Split a bash `printf` format into ``('lit', text)`` / ``('spec',)``.

    Escapes are *decoded* here (the returned literals hold real tabs and
    newlines), so both renderers work on the format's meaning rather than on its
    Bash spelling.
    """
    out: list = []
    buf: list = []
    i, n = 0, len(fmt)
    while i < n:
        c = fmt[i]
        if c == "\\":
            if i + 1 >= n:
                raise ConversionError(
                    f"dangling backslash in printf format {fmt!r}")
            e = fmt[i + 1]
            if e not in _PRINTF_ESCAPES:
                raise ConversionError(
                    f"unsupported printf escape \\{e} in format {fmt!r}")
            buf.append(_PRINTF_ESCAPES[e])
            i += 2
            continue
        if c == "%":
            if fmt[i:i + 2] != "%s":
                raise ConversionError(
                    f"unsupported printf specifier in format {fmt!r}; only %s "
                    f"is in the subset")
            out.append(("lit", "".join(buf)))
            buf = []
            out.append(("spec",))
            i += 2
            continue
        buf.append(c)
        i += 1
    out.append(("lit", "".join(buf)))
    return out


def _printf_stmt(args: list, redirs) -> Stmt:
    """`printf 'FMT' args... [> file]` — a formatted line, on stdout or on disk.

    On stdout the line *is* an echo (`printf '%s\\n' "$x"` prints exactly what
    `echo "$x"` prints), so it becomes the existing ``echo`` statement rather
    than a new node that would render to the same Nushell and leave the
    recogniser unable to tell the two apart.  With a redirect there is no echo
    spelling to reuse, so the text goes to a ``write`` statement.
    """
    if not args:
        raise ConversionError("printf without a format string")
    fmt, values = args[0], list(args[1:])
    parts = _printf_parts(fmt)
    holes = sum(1 for p in parts if p[0] == "spec")
    if holes != len(values):
        raise ConversionError(
            f"printf format {fmt!r} has {holes} %s hole(s) for {len(values)} "
            f"argument(s); bash's format-reuse rule has no line-by-line "
            f"rendering in the subset")
    if parts[-1][0] != "lit" or not parts[-1][1].endswith("\n"):
        raise ConversionError(
            f"printf format {fmt!r} does not end its line with a newline; the "
            f"subset is line-oriented and has no rendering for a partial line")
    if any(p[0] == "lit" and "\n" in p[1][:-1] for p in parts[:-1]) \
            or "\n" in parts[-1][1][:-1]:
        raise ConversionError(
            f"printf format {fmt!r} prints more than one line; the subset "
            f"renders one line per statement")
    for v in values:
        if _PRINTF_VAR.match(v):
            continue
        if any(ch in v for ch in '$"\\()'):
            raise ConversionError(
                f"printf argument {v!r} is neither a $variable nor a plain "
                f"literal; it has no faithful Nushell interpolation")
    # Text is the *decoded* line, with each `%s` hole back as the `$var` it
    # prints.  A literal `(`, `)` or `$` would be read back as an interpolation
    # by the Nushell renderer, and a `'` cannot be carried in the single-quoted
    # Bash word the reverse leg writes, so those formats stay refused.
    for p in parts:
        if p[0] == "lit" and any(ch in p[1] for ch in "()$'"):
            raise ConversionError(
                f"printf format {fmt!r} contains a character that neither "
                f"Nushell interpolation nor a single-quoted printf word can "
                f"carry back")
    text = []
    it = iter(values)
    for p in parts:
        text.append(p[1] if p[0] == "lit" else next(it))
    text = "".join(text)
    path, append = _tail_redirect(redirs)
    if path is None:
        # `echo`'s rendering ends the line for it, so the format's own trailing
        # newline is dropped here rather than doubled.
        return Stmt("echo", {"text": text[:-1]})
    return Stmt("write", {"text": text, "path": path, "append": append})


# ---------------------------------------------------------------------------
# `while IFS= read -r VAR...; do BODY; done [< FILE]`
# ---------------------------------------------------------------------------

# The one `while` shape in the subset: the line loop.  Anything else (`until`,
# `case`, a condition that is not `read`) is refused as before.
#
# The IFS the loop reads with is the one thing bashlex cannot carry: it hands
# `IFS=$'\t'` over as `IFS=$t` (the backslash-escape is dropped), which is a
# *different* value, so the verbatim source of the assignment word is read back
# out of `_RAW_BASH` by position instead.  Only the two separators the corpus
# uses are understood, plus the empty IFS (no splitting at all).
_IFS_TAB = "$'\\t'"


def _read_ifs_text(text: str) -> str | None:
    """The IFS value a `read` runs with, from the assignment's source text.

    Returns ``""`` for the empty IFS (no splitting), ``"\\t"`` for a tab, and
    ``None`` when nothing is assigned (bash's default: any whitespace run).
    """
    m = re.fullmatch(r"\s*IFS\s*=(.*)", text, re.S)
    if not m:
        raise ConversionError(f"unrecognized `read` assignment {text!r}")
    val = m.group(1).strip()
    if val in ("", "''", '""'):
        return ""
    if val == _IFS_TAB:
        return "\t"
    raise ConversionError(
        f"unsupported IFS value {val!r} on a `read`; the subset knows the empty "
        f"IFS and a tab")


def _while_read_op(node) -> Op:
    """``while IFS=<ifs> read -r VARS...; do BODY; done [< FILE]`` → one op.

    The loop's *source* is either the file after a `done < FILE` redirect or the
    pipeline feeding it; bashlex does not carry the redirect on the `while` node
    at all, so it is read back out of the source text between the `done` keyword
    and the end of the whole compound.
    """
    inner = node.list[0] if isinstance(getattr(node, "list", None), list) \
        else node
    parts = list(getattr(inner, "parts", []) or [])

    def _word_at(nodes, wanted):
        return next((k for k, p in enumerate(nodes)
                     if getattr(p, "kind", None) == "reservedword"
                     and getattr(p, "word", None) == wanted), None)

    # parts: [rw while, cond…, rw do, body…, rw done].  A one-line body is a
    # single `list` node; a multi-line one appears as bare command/pipeline
    # nodes between the reserved words (the same split `_if_stmt` handles).
    i_do, i_done = _word_at(parts, "do"), _word_at(parts, "done")
    if i_do is None or i_done is None or i_done < i_do:
        raise ConversionError("malformed while loop")
    cond_parts = [p for p in parts[1:i_do]
                  if getattr(p, "kind", None) != "reservedword"]
    body_parts = parts[i_do + 1:i_done]
    if not cond_parts or not body_parts:
        raise ConversionError("while loop without a condition or a body")

    cmd = next((q for p in cond_parts for q in
                (getattr(p, "parts", []) or [p])
                if getattr(q, "kind", None) == "command"), None)
    if cmd is None:
        raise ConversionError("while loop whose condition is not a command")
    name, args, assigns, redirs = _cmd_parts(cmd)
    if name != "read":
        raise ConversionError(
            f"unsupported while condition {name!r}; only `read` is in the subset")
    flags = [a for a in args if a.startswith("-")]
    if flags not in ([], ["-r"]):
        raise ConversionError(f"unsupported read flags: {flags}")
    variables = [a for a in args if not a.startswith("-")]
    if not variables:
        raise ConversionError("`read` without a variable")
    ifs = None
    if assigns:
        if len(assigns) > 1 or assigns[0][0] != "IFS":
            raise ConversionError(
                f"unsupported read environment: {assigns}")
        assign_node = next((q for q in (getattr(cmd, "parts", []) or [])
                            if getattr(q, "kind", None) == "assignment"), None)
        raw = _raw_text(assign_node)
        ifs = _read_ifs_text(raw)

    # the redirect that feeds the loop, which bashlex drops entirely
    tail = _raw_text(node)[inner.pos[1] - node.pos[0]:] \
        if node.pos and inner.pos else ""
    path = None
    m = re.fullmatch(r"\s*(?:<\s*(\S+))?\s*", tail)
    if not m or (tail.strip() and not m.group(1)):
        raise ConversionError(
            f"unsupported input redirect on a while loop: {tail.strip()!r} "
            f"(process substitution is not in the subset)")
    if m.group(1):
        path = m.group(1).strip("'\"")
        if path.startswith("("):
            raise ConversionError(
                "process substitution `< <(...)` is not supported")
    body = _body_section(body_parts)
    if not body:
        raise ConversionError("while loop with an empty body")
    return Op("while_read", var=variables, ifs=ifs, path=path, body=body)


def _is_while_node(node) -> bool:
    """True for a `compound` node wrapping a `while` (as a pipeline stage)."""
    sub = list(getattr(node, "list", None) or []) or \
        list(getattr(node, "parts", []) or [])
    return bool(sub) and getattr(sub[0], "kind", None) == "while"


def _raw_text(node) -> str:
    """The verbatim source of a bashlex node (``pos`` is a (start, end) pair)."""
    if _RAW_BASH is None or not getattr(node, "pos", None):
        raise ConversionError("cannot recover the source of a node")
    start, end = node.pos
    return _RAW_BASH[start:end]


def _compound_stmt(node) -> Stmt:
    """if/then/else and for loops (bashlex 'compound' nodes).

    bashlex represents both as a compound node whose ``.list`` — a plain
    Python list — is a single node of kind ``for`` / ``if`` whose own
    ``.parts`` holds the reserved-word + list structure.
    """
    sub = list(getattr(node, "list", None) or []) or \
        list(getattr(node, "parts", []) or [])
    if not sub:
        raise ConversionError("empty compound node")
    inner = sub[0]
    k = getattr(inner, "kind", None)
    parts = list(getattr(inner, "parts", None) or [])
    if k == "for":
        return _for_stmt(parts)
    if k == "if":
        return _if_stmt(parts)
    if k == "while":
        # A bare `while …; done < FILE` is the whole statement; its op stands
        # alone in the pipeline, exactly as `find`'s opener does.
        return Pipeline([_while_read_op(node)])
    if k in ("until", "case"):
        raise ConversionError(f"compound '{k}' is not supported")
    raise ConversionError(f"unsupported compound {k or sub[0].kind}")


def _list_body(list_node) -> list:
    """Body statements of a list node (skip operator/reservedword parts)."""
    out = []
    for part in getattr(list_node, "parts", []):
        k = getattr(part, "kind", None)
        if k == "operator":
            op = getattr(part, "op", None)
            if op not in (";", "\n"):
                raise ConversionError(f"operator {op!r} not supported in body")
            continue
        if k in ("command", "pipeline", "compound", "if"):
            s = _single_stmt(part)
            if s is not None:
                out.append(s)
        elif k == "reservedword":
            continue
        else:
            raise ConversionError(f"unsupported body node {k}")
    return out


def _body_section(parts: list) -> list:
    """Body statements of one if/else section.  A single-line section is a
    ``list`` node; a multi-line body appears as bare command/pipeline nodes."""
    if len(parts) == 1 and getattr(parts[0], "kind", None) == "list":
        return _list_body(parts[0])
    out = []
    for p in parts:
        s = _single_stmt(p)
        if s is not None:
            out.append(s)
    return out


def _condition_of(list_node) -> tuple:
    """Numeric/string test from ``[ ... ]``: returns ('num'|'str', var, op, val)."""
    cmd = None
    for part in getattr(list_node, "parts", []):
        if getattr(part, "kind", None) == "command":
            cmd = part
            break
    if cmd is None:
        raise ConversionError("test list without a command")
    name, args, assigns, redirs = _cmd_parts(cmd)
    if name == "test":
        args = args
    args = [a for a in args if a not in ("[", "]")]
    if len(args) != 3:
        raise ConversionError(f"unsupported test args: {args}")
    a, op, b = args
    if op in ("=", "==", "!=", "-gt", "-lt", "-ge", "-le", "-eq", "-ne"):
        pass
    else:
        raise ConversionError(f"unsupported test op {op}")
    return (a, op, b)


def _is_command_check(list_node) -> bool:
    """Recognize tool-availability guards so the caller can refuse them."""
    for part in getattr(list_node, "parts", []) or []:
        if getattr(part, "kind", None) != "command":
            continue
        try:
            name, args, assigns, redirs = _cmd_parts(part)
        except ConversionError:
            return False
        if name == "command" and args and args[0] == "-v":
            return True
        if name == "find" and _is_find_printf_probe(args):
            return True
    return False


def _is_negated_command_check(list_node) -> bool:
    """Recognize a negated tool-availability guard."""
    parts = list(getattr(list_node, "parts", []) or [])
    if not parts or getattr(parts[0], "kind", None) != "pipeline":
        return False
    inner = list(getattr(parts[0], "parts", []) or [])
    if not inner or getattr(inner[0], "kind", None) != "reservedword" \
            or getattr(inner[0], "word", None) != "!":
        return False
    for part in inner:
        if getattr(part, "kind", None) != "command":
            continue
        try:
            name, args, assigns, redirs = _cmd_parts(part)
        except ConversionError:
            return False
        if name == "command" and args and args[0] == "-v":
            return True
    return False


def _is_find_printf_probe(args: list) -> bool:
    """``find DIR -maxdepth 0 -printf ''`` — a GNU-find `-printf` probe.

    Narrow on purpose: the directory must be `-maxdepth 0` (so nothing is
    descended into) and the format must be empty (so the probe prints nothing
    even on success).  Anything richer would be a real `find`, not a probe.
    """
    if len(args) < 4 or args[1] != "-maxdepth" or args[2] != "0":
        return False
    if not args[0] or args[0].startswith("-"):
        return False
    i = args.index("-printf") if "-printf" in args else -1
    return i != -1 and i + 1 < len(args) and args[i + 1] == ""


def _is_negated_file_guard(list_node) -> bool:
    """``if [ ! -f x ]; then <error>; exit 1; fi`` — a file-existence guard whose
    then-branch is the *failure* path.  On the correct test workspace the file
    exists, so the whole ``if`` is a no-op and is dropped."""
    for part in getattr(list_node, "parts", []) or []:
        if getattr(part, "kind", None) != "command":
            continue
        try:
            name, args, assigns, redirs = _cmd_parts(part)
        except ConversionError:
            return False
        args = [a for a in args if a not in ("[", "]")]
        if name == "[" and len(args) == 3 and args[0] == "!" \
                and args[1] in ("-f", "-e"):
            return True
    return False


def _if_stmt(parts) -> Stmt:
    # parts: [rw if, cond list, rw then, body, rw else?, body, rw fi].
    # Single-line bodies are `list` nodes; multi-line bodies appear as bare
    # command/pipeline nodes between the reserved words.
    i, n = 0, len(parts)
    while i < n and getattr(parts[i], "kind", None) == "reservedword":
        i += 1                      # leading `if`
    if i >= n:
        raise ConversionError("if without condition")
    if (_is_command_check(parts[i]) or _is_negated_command_check(parts[i])
            or _is_negated_file_guard(parts[i])):
        raise ConversionError("Availability and file-existence guards need runtime checks")
    cond = _condition_of(parts[i])
    i += 1
    while i < n and getattr(parts[i], "kind", None) == "reservedword":
        i += 1                      # `then` (+ anything before the body)
    then_parts = []
    while i < n and getattr(parts[i], "kind", None) != "reservedword":
        then_parts.append(parts[i]); i += 1
    then_body = _body_section(then_parts)
    while (i < n and getattr(parts[i], "kind", None) == "reservedword"
           and getattr(parts[i], "word", None) != "fi"):
        i += 1                      # `else` when present
    else_parts = []
    while i < n and getattr(parts[i], "kind", None) != "reservedword":
        else_parts.append(parts[i]); i += 1
    else_body = _body_section(else_parts)
    return Stmt("if", {"cond": cond, "negate": False},
                body=[then_body, else_body])


def _for_stmt(parts) -> Stmt:
    # parts: [rw for, word var, rw in, words..., rw ';', rw do, body, rw done]
    var = None
    items = []
    body_parts = []
    seen_in = False
    in_body = False
    for p in parts:
        k = getattr(p, "kind", None)
        if k == "word":
            txt = _word_text(p)
            if var is None:
                var = txt
            elif seen_in and not in_body:
                items.append(txt)
        elif k == "reservedword":
            w = getattr(p, "word", None)
            if w == "in":
                seen_in = True
            elif w == "do":
                in_body = True
        elif k == "list":
            body_parts = [p]
        elif in_body:
            body_parts.append(p)
    if var is None:
        raise ConversionError("for loop without a variable")
    if not items:
        raise ConversionError("for loop without an explicit word list")
    body = _body_section(body_parts)
    return Stmt("for", {"var": var, "items": items}, body=body)
