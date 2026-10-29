"""Restricted-awk parser for the Bash → Nushell compat layer.

The old ``_parse_awk`` recognized a handful of whole-program regex templates
(select / group / agg / bucket / uniq / reorder).  Those shapes are exact and
still fast-path below in ``bash_to_ir``.  This module is the *general* fallback:
a real lexer + recursive-descent parser for a restricted awk sub-language,
followed by a classifier that recognizes the aggregation/print shapes the agents
actually write and renders each as a *validated* Nu 0.114.1 pipeline segment.

Fidelity contract (unchanged from the IR layer): any construct outside the
controlled subset raises ``ConversionError`` — an honest refusal, never a
silent mistranslation.  ``sub`` / ``gsub`` / arbitrary statement sequences /
user-defined functions are refused.  The segments interpolate only validated
values (field indices parsed as integers, string/number literals from the AST,
print formats restricted to ``%d`` / ``%.2f``), so nothing user-shaped ever
reaches the Nu text unvalidated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ir import ConversionError


# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------

@dataclass
class _Tok:
    kind: str
    value: str
    pos: int


_TOKEN_RE = re.compile(r"""
      (?P<WS>\s+)
    | (?P<COMMENT>\#[^\n]*)
    | (?P<STRING>"(?:\\.|[^"\\])*")
    | (?P<REGEX>/(?:\\.|[^/\\])*/)
    | (?P<NUMBER>\d+(?:\.\d*)?|\.\d+)
    | (?P<FIELD>\$\([^)]*\)|\$[0-9]+|\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<ID>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<OP>\+\+|--|\+=|-=|\*=|\/=|==|!=|<=|>=|&&|\|\||!~|[+\-*/%=<>!~])
    | (?P<PUNC>[{}()[\],;])
    | (?P<KW>BEGIN|END|next|in|if|else|for|print|printf|split|int|sub|gsub)
    """, re.X)

_KW_SET = {"BEGIN", "END", "next", "in", "if", "else", "for",
           "print", "printf", "split", "int", "sub", "gsub"}


def _lex(src: str) -> list[_Tok]:
    toks = []
    pos = 0
    depth = 0  # () / [] nesting — a newline inside parens is a line-continuation
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise ConversionError(
                f"unsupported awk token at offset {pos}: {src[pos:pos+40]!r}")
        kind = m.lastgroup
        val = m.group()
        if kind == "WS":
            if "\n" in val and depth == 0:
                toks.append(_Tok("PUNC", ";", pos))
        elif kind != "COMMENT":
            if kind == "PUNC" and val in "([":
                depth += 1
            elif kind == "PUNC" and val in ")]":
                depth -= 1
            if kind == "ID" and val in _KW_SET:
                kind = "KW"
            toks.append(_Tok(kind, val, pos))
        pos = m.end()
    toks.append(_Tok("EOF", "", pos))
    return toks


# ---------------------------------------------------------------------------
# restricted-awk AST
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """$N (index>=1) or $(expr) where expr is e.g. NF-1 (relative>=0)."""
    index: int | None = None     # 1-based absolute field
    relative: int | None = None  # 0-based from end: 0 = $NF
    whole: bool = False          # $0

@dataclass
class Num:
    value: str

@dataclass
class Str:
    value: str

@dataclass
class Regex:
    value: str

@dataclass
class Var:
    name: str

@dataclass
class Bin:
    op: str
    left: object
    right: object

@dataclass
class Arr:
    name: str
    index: object

@dataclass
class Call:
    name: str
    args: list

@dataclass
class Assign:
    lhs: object       # Var | Arr
    op: str           # = | += | ++
    rhs: object

@dataclass
class Print:
    exprs: list

@dataclass
class Printf:
    fmt: str
    exprs: list

@dataclass
class If:
    cond: object
    body: list
    els: list | None = None

@dataclass
class ForIn:
    var: str
    arr: str
    body: list

@dataclass
class Next:
    pass


# ---------------------------------------------------------------------------
# recursive-descent parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, toks: list[_Tok]):
        self.toks = toks
        self.i = 0

    def peek(self, k=None):
        t = self.toks[self.i]
        if k is None:
            return t
        return t.kind == k

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def at_end(self):
        return self.toks[self.i].kind == "EOF"

    def expect(self, k):
        t = self.next()
        if t.kind != k:
            raise ConversionError(f"awk: expected {k} at offset {t.pos}, "
                                  f"got {t.kind} {t.value!r}")
        return t

    # -- statements ------------------------------------------------------
    def parse_block(self) -> list:
        t = self.next()
        if t.value != "{":
            raise ConversionError(f"awk: expected '{{' at offset {t.pos}")
        body = []
        while not (self.toks[self.i].kind == "PUNC"
                   and self.toks[self.i].value == "}"):
            if self.at_end():
                raise ConversionError("awk: unterminated block")
            if self.toks[self.i].kind == "PUNC" and \
                    self.toks[self.i].value == ";":
                self.next()
                continue
            body.append(self.parse_stmt())
            if self.toks[self.i].kind == "PUNC" and \
                    self.toks[self.i].value in (";",):
                self.next()
        self.next()  # }
        return body

    def parse_stmt(self):
        t = self.toks[self.i]
        if t.kind == "KW" and t.value == "print":
            self.next()
            return Print(self.parse_print_exprs())
        if t.kind == "KW" and t.value == "printf":
            self.next()
            fmt = self.expect("STRING").value.strip('"')
            self.expect("PUNC")  # ,
            return Printf(fmt, self.parse_print_exprs())
        if t.kind == "KW" and t.value == "if":
            self.next()
            self.next()  # (
            cond = self.parse_expr()
            self.expect("PUNC")  # )
            body = self._parse_brace_or_stmt()
            # a `;` may separate the body from a following `else`
            while self.toks[self.i].kind == "PUNC" and \
                    self.toks[self.i].value == ";":
                self.next()
            els = None
            if self.toks[self.i].kind == "KW" and \
                    self.toks[self.i].value == "else":
                self.next()
                els = self._parse_brace_or_stmt()
            return If(cond, body, els)
        if t.kind == "KW" and t.value == "for":
            self.next()
            self.next()  # (
            var = self.expect("ID").value
            self.next()  # in
            arr = self.expect("ID").value
            self.expect("PUNC")  # )
            return ForIn(var, arr, self._parse_brace_or_stmt())
        if t.kind == "KW" and t.value == "next":
            self.next()
            return Next()
        # expression statement (assign / inc / call)
        return self.parse_expr_stmt()

    def _parse_brace_or_stmt(self) -> list:
        """An awk statement body: either a ``{...}`` block or a single
        statement without braces (``if (c) print $1``).  Leading ``;``
        (a newline separator) between the header and the body is skipped."""
        while self.toks[self.i].kind == "PUNC" and \
                self.toks[self.i].value == ";":
            self.next()
        if self.toks[self.i].kind == "PUNC" and \
                self.toks[self.i].value == "{":
            return self.parse_block()
        return [self.parse_stmt()]

    def parse_print_exprs(self) -> list:
        out = [self.parse_expr()]
        while self.toks[self.i].kind == "PUNC" and \
                self.toks[self.i].value == ",":
            self.next()
            out.append(self.parse_expr())
        return out

    def parse_expr_stmt(self):
        e = self.parse_expr()
        # `arr[k]++` / `x++` and `lhs op rhs` are statements here.
        t = self.toks[self.i]
        if t.kind == "OP" and t.value in ("++", "--"):
            self.next()
            return Assign(e, "++", Num("1"))
        if t.kind == "OP" and t.value in ("+=", "-=", "*=", "/=", "="):
            op = self.next().value
            rhs = self.parse_expr()
            return Assign(e, op, rhs)
        return e

    # -- expressions (precedence climbing) --------------------------------
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.toks[self.i].kind == "OP" and \
                self.toks[self.i].value == "||":
            self.next()
            left = Bin("||", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_cmp()
        while self.toks[self.i].kind == "OP" and \
                self.toks[self.i].value == "&&":
            self.next()
            left = Bin("&&", left, self.parse_cmp())
        return left

    def parse_cmp(self):
        left = self.parse_add()
        t = self.toks[self.i]
        if t.kind == "OP" and t.value in ("==", "!=", ">", ">=", "<", "<=",
                                          "~", "!~"):
            self.next()
            return Bin(t.value, left, self.parse_add())
        return left

    def parse_add(self):
        left = self.parse_concat()
        while self.toks[self.i].kind == "OP" and \
                self.toks[self.i].value in ("+", "-"):
            op = self.next().value
            left = Bin(op, left, self.parse_concat())
        return left

    def parse_concat(self):
        """awk juxtaposition concatenation (``a[1] " " a[2]``).  Binds
        tighter than ``+``, looser than ``*``."""
        left = self.parse_mul()
        while self._concat_next():
            left = Bin("cat", left, self.parse_mul())
        return left

    def _concat_next(self) -> bool:
        t = self.toks[self.i]
        if t.kind in ("EOF", "KW"):
            return False
        if t.kind == "PUNC" and t.value in ("}", "]", ")", ",", "{", ";"):
            return False
        if t.kind == "OP":
            return False
        return True

    def parse_mul(self):
        left = self.parse_unary()
        while self.toks[self.i].kind == "OP" and \
                self.toks[self.i].value in ("*", "/", "%"):
            op = self.next().value
            left = Bin(op, left, self.parse_unary())
        return left

    def parse_unary(self):
        t = self.toks[self.i]
        if t.kind == "OP" and t.value in ("!", "-"):
            self.next()
            return Bin(t.value, None, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_atom()
        t = self.toks[self.i]
        if t.kind == "PUNC" and t.value == "[":
            self.next()
            idx = self.parse_expr()
            self.expect("PUNC")  # ]
            if not isinstance(e, Var):
                raise ConversionError("awk: array index on non-variable")
            return Arr(e.name, idx)
        return e

    def parse_atom(self):
        t = self.toks[self.i]
        if t.kind == "NUMBER":
            self.next()
            return Num(t.value)
        if t.kind == "STRING":
            self.next()
            return Str(t.value.strip('"'))
        if t.kind == "REGEX":
            self.next()
            return Regex(t.value.strip("/"))
        if t.kind == "FIELD":
            self.next()
            return self._parse_field(t.value)
        if t.kind == "PUNC" and t.value == "(":
            self.next()
            e = self.parse_expr()
            self.expect("PUNC")  # )
            return e
        if t.kind == "ID":
            self.next()
            if self.toks[self.i].kind == "PUNC" and \
                    self.toks[self.i].value == "(":
                return Call(t.value, self._parse_call_args())
            return Var(t.value)
        if t.kind == "KW" and t.value in ("split", "int", "sub", "gsub"):
            self.next()
            return Call(t.value, self._parse_call_args())
        raise ConversionError(f"awk: unexpected token {t.kind} {t.value!r} "
                              f"at offset {t.pos}")

    def _parse_call_args(self) -> list:
        self.next()  # (
        args = []
        if not (self.toks[self.i].kind == "PUNC" and
                self.toks[self.i].value == ")"):
            args.append(self.parse_expr())
            while self.toks[self.i].kind == "PUNC" and \
                    self.toks[self.i].value == ",":
                self.next()
                args.append(self.parse_expr())
        self.expect("PUNC")  # )
        return args

    def _parse_field(self, text: str) -> Field:
        if text == "$0":
            return Field(whole=True)
        m = re.fullmatch(r"\$(\d+)", text)
        if m:
            return Field(index=int(m.group(1)))
        m = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", text)
        if m:
            name = m.group(1)
            if name == "NF":
                # $NF ≡ $(NF) — the k=0 relative field (last column).
                return Field(relative=0)
            # Any other $NAME is a dynamic field whose index is the runtime
            # value of the variable; the column shape is not static, so it is
            # outside the controlled subset (COMPAT_SPEC_v1.0 §3 family 9).
            raise ConversionError(
                f"awk: dynamic field by variable ${name} is not supported "
                f"(only $0, $N, $NF, $(NF-k))")
        if text.startswith("$(") and text.endswith(")"):
            inner = text[2:-1]  # $( ... )
            e = _Parser(_lex(inner)).parse_expr()
            rel = _nf_relative(e)
            if rel is None:
                raise ConversionError(f"awk: unsupported field expr {inner!r}")
            return Field(relative=rel)
        raise ConversionError(f"awk: unsupported field token {text!r}")


def _nf_relative(e) -> int | None:
    """NF - k  → k (0-based-from-end).  Anything else → None (unsupported)."""
    if isinstance(e, Var) and e.name == "NF":
        return 0
    if isinstance(e, Bin) and e.op == "-" and \
            isinstance(e.left, Var) and e.left.name == "NF" and \
            isinstance(e.right, Num):
        return int(e.right.value)
    return None


# ---------------------------------------------------------------------------
# program structure
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    pattern: object  # None = all rows
    body: list | None  # None = default action (print $0)


@dataclass
class Program:
    rules: list
    end: list


def parse_program(prog: str) -> Program:
    toks = _lex(prog)
    p = _Parser(toks)
    rules = []
    end = []
    saw_end = False
    while not p.at_end():
        t = p.toks[p.i]
        # stray ';' separators (newlines) between rules
        if t.kind == "PUNC" and t.value == ";":
            p.next()
            continue
        if t.kind == "KW" and t.value == "END":
            p.next()
            end = p.parse_block()
            saw_end = True
            # only trailing whitespace/comments may follow END; a trailing
            # newline lexes as a stray ';'
            while p.toks[p.i].kind == "PUNC" and p.toks[p.i].value == ";":
                p.next()
            break
        if t.kind == "KW" and t.value == "BEGIN":
            raise ConversionError("awk BEGIN block is not supported")
        if t.kind == "PUNC" and t.value == "{":
            rules.append(Rule(None, p.parse_block()))
            continue
        pat = p.parse_expr()
        # a newline between the pattern and its '{}' block lexes as ';'
        while p.toks[p.i].kind == "PUNC" and p.toks[p.i].value == ";":
            p.next()
        if p.toks[p.i].kind == "PUNC" and p.toks[p.i].value == "{":
            rules.append(Rule(pat, p.parse_block()))
        else:
            rules.append(Rule(pat, None))
    if saw_end and not p.at_end():
        raise ConversionError("awk: statements after END are not supported")
    return Program(rules, end)


# ---------------------------------------------------------------------------
# restricted-awk classifier: AST → validated Nu pipeline segment
# ---------------------------------------------------------------------------

@dataclass
class AwkSpec:
    family: str                        # select | group_count | group_sum |
                                       # scalar_sum | scalar_avg
    fs: str | None = None
    skip: int = 0
    cr_strip: bool = False
    nf_min: int | None = None          # NF >= k guard
    cond: object = None                # outer pattern condition (AST)
    cond2: object = None               # inner if-guard inside the body (AST)
    lets: list = field(default_factory=list)     # [(var, rhs-AST)] prologue
    split: tuple = None                # (src-AST, arr_name, sep) for split()
    split_nvar: str | None = None
    arrs: dict = field(default_factory=dict)     # {count-var: arr-name}
    print_args: list = field(default_factory=list)  # select: [expr, ...]
    print_mode: str = "print"          # print | printf
    printf_fmt: str | None = None
    key: object = None                 # group key expr (AST)
    val: object = None                 # group/scalar value expr (AST)
    gprint: list = field(default_factory=list)   # group: [(tok, fmt)] in order
    sround: str = None                 # scalar output: int | dec2 | round | None
    sempty: object = None              # scalar empty-result value (AST Num)
    cleanse_var: str | None = None     # var gsub-cleaned of non-[0-9.+-]
    fld_cleanse: dict = field(default_factory=dict)  # {field-N: (var, regex)}
    cr_lets: dict = field(default_factory=dict)  # {var: True} CR-stripped
    skip_conds: list = field(default_factory=list)  # `if (c) next` conditions


# ---------------------------------------------------------------------------
# Nu literal / expression helpers
# ---------------------------------------------------------------------------

def _nu_dq(s: str) -> str:
    """Double-quoted Nu string literal (backslash + quote escaped)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _nu_sq(s: str) -> str:
    """Single-quoted Nu string literal (no quote allowed)."""
    if "'" in s:
        raise ConversionError(
            f"awk: cannot render {s!r} in a single-quoted Nu literal")
    return f"'{s}'"


def _nu_fs(fs) -> str:
    """Nu field-separator literal for `split row`.  ``fs`` is the raw text of
    the ``-F`` value: a literal backslash-escape (``\\t``) or a bare char."""
    if not fs:
        return "' '"
    esc = {"\\t": "\t", "\\n": "\n", "\\r": "\r", "\\v": "\v", "\\f": "\f"}
    if fs in esc:
        # Nu double-quoted strings decode `\t` etc. at parse time, but a raw
        # control byte inside a string literal breaks the parser
        # (`unexpected_eof`).  Emit the escape-sequence *text* so Nu decodes
        # it itself.
        return '"' + fs + '"'
    if "'" in fs or "\\" in fs:
        return _nu_dq(fs)
    return _nu_sq(fs)


def _nf(stream: str) -> str:
    return "($p | length)" if stream == "lines" else "($r | columns | length)"


def _whole_line(stream: str, fs) -> str:
    if stream == "lines":
        return "$l"
    return f"($r | values | str join {_nu_fs(fs)})"


def _fref(ref, stream, fs, rcol=None):
    """Field reference → Nu string expression.

    ``rcol`` is an optional ``(index)->name`` callback for named/positional
    rows (builder rcol): awk field ``$N`` on a row reads the ``(N-1)``-th
    *current* field, whatever it is named downstream."""
    if ref.whole:
        return _whole_line(stream, fs)
    if ref.index is not None:
        if stream == "lines":
            return f"($p | get {ref.index - 1})"
        if rcol is not None:
            return f"($r.{rcol(ref.index - 1)})"
        return f"($r.column{ref.index - 1})"
    # relative-from-end: $NF → index NF-1, $(NF-k) → NF-1-k (0-based)
    if stream == "lines":
        if ref.relative == 0:
            return "($p | last)"
        return f"($p | get ((($p | length) - 1 - {ref.relative})))"
    cols = "($r | columns)"
    if ref.relative == 0:
        return f"($r | get ({cols} | last))"
    return (f"($r | get ({cols} | get (({cols} | length) - 1 - "
            f"{ref.relative})))")


def _arr_idx(e) -> int:
    if isinstance(e, Num):
        return int(e.value) - 1
    raise ConversionError(
        f"awk: only literal array indices are supported, got {e!r}")


def _operand(e, stream, fs, arrs=None, rcol=None):
    if isinstance(e, Var):
        if e.name == "NF":
            return _nf(stream)
        if arrs and e.name in arrs:
            return f"(${arrs[e.name]} | length)"
        return f"${e.name}"
    if isinstance(e, Num):
        return e.value
    if isinstance(e, Str):
        return _nu_dq(e.value)
    if isinstance(e, Field):
        return _fref(e, stream, fs, rcol)
    if isinstance(e, Arr):
        return f"(${e.name} | get {_arr_idx(e.index)}? | default '')"
    raise ConversionError(f"awk: unsupported operand {e!r}")


def _cond(e, stream, fs, arrs=None, rcol=None):
    if isinstance(e, Bin) and e.op in ("&&", "||"):
        op = "and" if e.op == "&&" else "or"
        return (f"({_cond(e.left, stream, fs, arrs, rcol)} {op} "
                f"{_cond(e.right, stream, fs, arrs, rcol)})")
    if isinstance(e, Bin) and e.op in ("==", "!=", ">", ">=", "<", "<="):
        l = _operand(e.left, stream, fs, arrs, rcol)
        r = _operand(e.right, stream, fs, arrs, rcol)
        # awk coerces a field against a numeric literal; Nu strings need an
        # explicit cast to compare numerically.
        if isinstance(e.right, Num) and not isinstance(e.left, Num):
            return f"(({l} | into float) {e.op} {r})"
        if isinstance(e.left, Num) and not isinstance(e.right, Num):
            return f"({l} {e.op} ({r} | into float))"
        return f"({l} {e.op} {r})"
    if isinstance(e, Bin) and e.op in ("~", "!~"):
        l = _operand(e.left, stream, fs, arrs, rcol)
        if not isinstance(e.right, Regex):
            raise ConversionError("awk: `~` right side must be a regex literal")
        pat = _nu_sq(e.right.value)
        if e.op == "!~":
            return f"(not ({l} =~ {pat}))"
        return f"({l} =~ {pat})"
    raise ConversionError(f"awk: unsupported condition {e!r}")


def _chunks(e, stream, fs, arrs=None, rcol=None) -> list:
    """A print argument (concat expression) → [(lit, text)|(val, expr)]."""
    if isinstance(e, Bin) and e.op == "cat":
        return (_chunks(e.left, stream, fs, arrs, rcol)
                + _chunks(e.right, stream, fs, arrs, rcol))
    if isinstance(e, Str):
        return [("lit", e.value)]
    if isinstance(e, Num):
        return [("val", f"{e.value} | into string")]
    if isinstance(e, Field):
        return [("val", _fref(e, stream, fs, rcol))]
    if isinstance(e, Arr):
        return [("val", f"(${e.name} | get {_arr_idx(e.index)}? | default '')")]
    if isinstance(e, Var):
        return [("val", f"${e.name}")]
    raise ConversionError(f"awk: unsupported print expression {e!r}")


def _printf_seps(fmt: str) -> list:
    """Literal text between the %-slots of a printf format."""
    parts = re.split(r"%[.\d]*[dfse%]", fmt)
    return parts[1:-1]


def _interp(chunks: list) -> str:
    """Nu interpolated string for a chunk list."""
    parts = []
    for kind, v in chunks:
        if kind == "lit":
            parts.append(v.replace("\\", "\\\\").replace('"', '\\"'))
        else:
            parts.append(f"({v})")
    return '$"' + "".join(parts) + '"'


def _interp_args(spec, stream, fs, rcol=None) -> str:
    """The printed value: print args joined by OFS; printf args joined by the
    fmt's literal separators."""
    if spec.print_mode == "print":
        pieces = []
        for i, expr in enumerate(spec.print_args):
            if i:
                pieces.append(("lit", " "))
            pieces.extend(_chunks(expr, stream, fs, spec.arrs, rcol))
        return _interp(pieces)
    seps = _printf_seps(spec.printf_fmt or "")
    pieces = []
    for i, expr in enumerate(spec.print_args):
        if i:
            lit = seps[i - 1] if i - 1 < len(seps) else " "
            pieces.append(("lit", lit))
        pieces.extend(_chunks(expr, stream, fs, spec.arrs, rcol))
    return _interp(pieces)


def _let_rhs(e, stream, fs, rcol=None) -> str:
    """RHS of a prologue `let`: field, numeric-cast, or array token."""
    if isinstance(e, Field):
        return _fref(e, stream, fs, rcol)
    if isinstance(e, Bin) and e.op == "+" and isinstance(e.right, Num):
        return f"({_fref(e.left, stream, fs, rcol)} | into float)"
    if isinstance(e, Num):
        return e.value
    if isinstance(e, Arr):
        return f"(${e.name} | get {_arr_idx(e.index)}? | default '')"
    raise ConversionError(f"awk: unsupported let rhs {e!r}")


def _prologue(spec, stream, fs, rcol=None) -> list:
    """Nu `let` statements for the split + body prologue, in order."""
    out = []
    if spec.split:
        src, arr, sep = spec.split
        split_let = (f"let {arr} = ({_operand(src, stream, fs, spec.arrs, rcol)}"
                     f" | split row {_nu_fs(sep)})")
        # A split source that is a body variable (`line = $0; split(line, f, ...)`)
        # must come after that variable's own `let`. Field/NR sources are free.
        if isinstance(src, Var) and src.name != "NF":
            idx = next((i for i, (n, _) in enumerate(spec.lets)
                        if n == src.name), None)
            if idx is not None:
                for i, (name, rhs) in enumerate(spec.lets):
                    out.append(_let_stmt(spec, name, rhs, stream, fs, rcol))
                    if i == idx:
                        out.append(split_let)
                return out
        out.append(split_let)
    for name, rhs in spec.lets:
        out.append(_let_stmt(spec, name, rhs, stream, fs, rcol))
    for idx, (var, regex) in spec.fld_cleanse.items():
        out.append(f"let {var} = ({_fref(Field(index=idx), stream, fs, rcol)}"
                   f" | str replace -a -r {_nu_sq(regex)} '')")
    return out


def _let_stmt(spec, name, rhs, stream, fs, rcol=None) -> str:
    val = _let_rhs(rhs, stream, fs, rcol)
    if spec.cleanse_var == name:
        val += " | str replace -a -r '[^0-9.+-]' ''"
    if spec.cr_lets.get(name):
        val += " | str replace -r '\\r$' ''"
    return f"let {name} = ({val})"


def _guard(spec, stream, fs, rcol=None):
    parts = []
    if spec.nf_min is not None:
        parts.append(f"({_nf(stream)} >= {spec.nf_min})")
    if spec.cond is not None:
        parts.append(f"({_cond(spec.cond, stream, fs, spec.arrs, rcol)})")
    if not parts:
        return None
    return " and ".join(parts)


def _skip_guard(spec, stream, fs, rcol=None):
    """`if (c) next` conditions combined → emit the row only when false."""
    if not spec.skip_conds:
        return None
    conds = [_cond(c, stream, fs, spec.arrs, rcol) for c in spec.skip_conds]
    return "not (" + " or ".join(f"({c})" for c in conds) + ")"


def _render_select(spec, stream, fs, rcol=None) -> str:
    prologue = _prologue(spec, stream, fs, rcol)
    interp = _interp_args(spec, stream, fs, rcol)
    if spec.cond2 is not None:
        emit = f"if ({_cond(spec.cond2, stream, fs, spec.arrs, rcol)}) {{ {interp} }}"
    else:
        emit = interp
    inner = "; ".join(prologue + [emit]) if prologue else emit
    if stream == "lines":
        head = f"let p = ($l | split row {_nu_fs(fs)})"
        g = _guard(spec, stream, fs, rcol)
        if g:
            return f"each {{|l| {head}; if ({g}) {{ {inner} }} }} | compact"
        return f"each {{|l| {head}; {inner} }} | compact"
    g = _guard(spec, stream, fs, rcol)
    if g:
        return f"each {{|r| if ({g}) {{ {inner} }} }} | compact"
    return f"each {{|r| {inner} }} | compact"


def _render_scalar(spec, stream, fs, rcol=None) -> str:
    prologue = _prologue(spec, stream, fs, rcol)
    val = spec.val
    if isinstance(val, Call) and val.name == "int" and len(val.args) == 1:
        val = val.args[0]
    row = f"{{s: ({_operand(val, stream, fs, spec.arrs, rcol)} | into float), c: 1}}"
    sk = _skip_guard(spec, stream, fs, rcol)
    inner = "; ".join(prologue + [row]) if prologue else row
    if sk:
        inner = "; ".join(prologue + [f"if ({sk}) {{ {row} }}"]) if prologue \
            else f"if ({sk}) {{ {row} }}"
    if stream == "lines":
        head = f"let p = ($l | split row {_nu_fs(fs)})"
        g = _guard(spec, stream, fs, rcol)
        if g:
            closure = f"each {{|l| {head}; if ({g}) {{ {inner} }} }}"
        else:
            closure = f"each {{|l| {head}; {inner} }}"
    else:
        g = _guard(spec, stream, fs, rcol)
        if g:
            closure = f"each {{|r| if ({g}) {{ {inner} }} }}"
        else:
            closure = f"each {{|r| {inner} }}"
    red = (f"{closure} | compact | reduce --fold {{s: 0.0, c: 0}} "
           f"{{|x, acc| {{s: ($acc.s + $x.s), c: ($acc.c + $x.c)}}}}")
    if spec.family == "scalar_sum":
        if spec.sround == "dec2":
            return red + " | get s | into string --decimals 2"
        if spec.sround == "int":
            return red + " | get s | into int"
        return red + " | get s"
    # scalar_avg
    empty = "0"
    if spec.sempty is not None:
        empty = spec.sempty.value if isinstance(spec.sempty, Num) else "0"
    if spec.sround == "int":
        return (red + f" | if (($in.c | into int) > 0) "
                f"{{ ($in.s / $in.c | into int) }} else {{ {empty} }}")
    if spec.sround == "round":
        return (red + f" | if (($in.c | into int) > 0) "
                f"{{ ($in.s / $in.c | math round) }} else {{ {empty} }}")
    return (red + f" | if (($in.c | into int) > 0) "
            f"{{ ($in.s / $in.c | into float) }} else {{ {empty} }}")


def _render_group(spec, stream, fs, rcol=None) -> str:
    prologue = _prologue(spec, stream, fs, rcol)
    key = _operand(spec.key, stream, fs, spec.arrs, rcol)
    if spec.family == "group_count":
        rec = f"{{k: ({key})}}"
        items = "items {|k,v| {key: $k, count: ($v | length)}}"
    else:
        val = _operand(spec.val, stream, fs, spec.arrs, rcol)
        rec = f"{{k: ({key}), v: ({val} | into float)}}"
        sumfmt = ""
        if spec.sround == "dec2":
            sumfmt = " | into string --decimals 2"
        elif spec.sround == "int":
            sumfmt = " | into int"
        items = (f"items {{|k,v| let s = ($v | get v | into float | math sum); "
                 f"{{key: $k, sum: ($s{sumfmt})}}}}")
    inner = "; ".join(prologue + [rec]) if prologue else rec
    if stream == "lines":
        head = f"let p = ($l | split row {_nu_fs(fs)})"
        g = _guard(spec, stream, fs, rcol)
        if g:
            closure = f"each {{|l| {head}; if ({g}) {{ {inner} }} }}"
        else:
            closure = f"each {{|l| {head}; {inner} }}"
    else:
        g = _guard(spec, stream, fs, rcol)
        if g:
            closure = f"each {{|r| if ({g}) {{ {inner} }} }}"
        else:
            closure = f"each {{|r| {inner} }}"
    return f"{closure} | compact | group-by k | {items}"


def render_spec(spec, stream, fs, rcol=None) -> str:
    if spec.family == "select":
        return _render_select(spec, stream, fs, rcol)
    if spec.family in ("scalar_sum", "scalar_avg"):
        return _render_scalar(spec, stream, fs, rcol)
    if spec.family in ("group_count", "group_sum"):
        return _render_group(spec, stream, fs, rcol)
    raise ConversionError(f"unknown spec family {spec.family}")


# ---------------------------------------------------------------------------
# classify: restricted-awk Program → AwkSpec
# ---------------------------------------------------------------------------

def _is_cr_strip_rule(rule) -> bool:
    """`{ sub(/\r$/, "") }` — a whole-line CR strip (no-op on LF data)."""
    if rule.pattern is not None or not rule.body:
        return False
    if len(rule.body) != 1:
        return False
    st = rule.body[0]
    if isinstance(st, Call) and st.name == "sub" and st.args:
        return isinstance(st.args[0], Regex) and st.args[0].value == r"\r$"
    return False


def _nr_next(rule) -> bool:
    return (rule.pattern is not None and isinstance(rule.pattern, Bin)
            and rule.pattern.op == "==" and isinstance(rule.pattern.left, Var)
            and rule.pattern.left.name == "NR"
            and isinstance(rule.pattern.right, Num)
            and rule.pattern.right.value == "1"
            and rule.body is not None and len(rule.body) == 1
            and isinstance(rule.body[0], Next))


def _nr_gt_split(e):
    """(skip, rest) for `NR > k` and `NR > k && cond` patterns."""
    if isinstance(e, Bin) and e.op == ">" and isinstance(e.left, Var) \
            and e.left.name == "NR" and isinstance(e.right, Num):
        return int(e.right.value), None
    if isinstance(e, Bin) and e.op == "&&" and isinstance(e.left, Bin) \
            and e.left.op == ">" and isinstance(e.left.left, Var) \
            and e.left.left.name == "NR" and isinstance(e.left.right, Num):
        return int(e.left.right.value), e.right
    return None, e


def _nf_guard_split(e):
    """(nf_min, rest) for `NF >= k` patterns (and bare `NF` → NF >= 1)."""
    if isinstance(e, Var) and e.name == "NF":
        return 1, None
    if isinstance(e, Bin) and e.op in (">=", ">") and isinstance(e.left, Var) \
            and e.left.name == "NF" and isinstance(e.right, Num):
        return int(e.right.value), None
    if isinstance(e, Bin) and e.op == "&&" and isinstance(e.left, Bin) \
            and e.left.op in (">=", ">") and isinstance(e.left.left, Var) \
            and e.left.left.name == "NF" and isinstance(e.left.right, Num):
        return int(e.left.right.value), e.right
    return None, e


def _body_shape(body):
    has_arr_inc = any(isinstance(st, Assign) and isinstance(st.lhs, Arr)
                      for st in body)
    has_var_acc = any(isinstance(st, Assign) and isinstance(st.lhs, Var)
                      and st.op in ("+=", "++") for st in body)
    return has_arr_inc, has_var_acc


def _find_split(spec, body):
    """Detect `n = split($2, arr, "sep")` (or bare `split(...)`) in the body."""
    for st in body:
        rhs = st.rhs if isinstance(st, Assign) else st
        if isinstance(rhs, Call) and rhs.name == "split":
            if not (len(rhs.args) == 3 and isinstance(rhs.args[1], Var)
                    and isinstance(rhs.args[2], Str)):
                raise ConversionError(f"awk: unsupported split call {rhs!r}")
            spec.split = (rhs.args[0], rhs.args[1].name, rhs.args[2].value)
            if isinstance(st, Assign) and isinstance(st.lhs, Var):
                spec.split_nvar = st.lhs.name
            if spec.split_nvar:
                spec.arrs = {spec.split_nvar: spec.split[1]}
            return


def _classify_group(spec, body, end):
    inc = None
    for st in body:
        if isinstance(st, Assign) and isinstance(st.lhs, Arr):
            if st.op == "++":
                inc = ("count", st.lhs.index, None)
            elif st.op == "+=":
                inc = ("sum", st.lhs.index, st.rhs)
            elif st.op == "=" and isinstance(st.rhs, Bin) \
                    and st.rhs.op == "+" and isinstance(st.rhs.left, Arr) \
                    and st.rhs.left.name == st.lhs.name:
                inc = ("sum", st.lhs.index, st.rhs.right)
            else:
                raise ConversionError(f"awk: unsupported group stmt {st!r}")
        elif isinstance(st, Assign) and isinstance(st.lhs, Var):
            if spec.split and st.lhs.name == spec.split[1]:
                raise ConversionError("awk: cannot re-assign the split array")
            spec.lets.append((st.lhs.name, st.rhs))
        elif isinstance(st, If) and st.body and isinstance(st.body[0], Next):
            spec.skip_conds.append(st.cond)
        elif isinstance(st, Next):
            continue
        else:
            raise ConversionError(f"awk: unsupported group body stmt {st!r}")
    if inc is None:
        raise ConversionError("awk: no group increment found")
    mode, key, val = inc
    spec.family = "group_count" if mode == "count" else "group_sum"
    spec.key = key
    spec.val = val
    if not end or len(end) != 1 or not isinstance(end[0], ForIn):
        raise ConversionError(
            "awk: group program requires a single `for (k in arr)` END")
    spec.gprint = _group_print(end[0], spec)
    for tok, fmt in spec.gprint:
        if tok == "sum" and spec.sround is None:
            spec.sround = _fmt_round(fmt) or "float"
    return spec


def _group_print(fin: ForIn, spec) -> list:
    """Parse the END for-loop's print/printf into [(tok, fmt)]."""
    arr_name = None
    # find the incremented array name from the lets/skip machinery isn't
    # stored yet; the loop var / array ref in the END matches the inc target.
    out = []
    slots = None
    for st in fin.body:
        if isinstance(st, Printf):
            slots = _printf_seps_padded(st.fmt)
            exprs = st.exprs
        elif isinstance(st, Print):
            slots = None
            exprs = st.exprs
        else:
            raise ConversionError(f"awk: unsupported END body stmt {st!r}")
        for i, ex in enumerate(exprs):
            fmt = slots[i] if slots and i < len(slots) else None
            if isinstance(ex, Var) and ex.name == fin.var:
                out.append(("key", fmt))
            elif isinstance(ex, Arr):
                out.append((spec.family[6:], fmt))  # count | sum
            elif isinstance(ex, Num):
                out.append(("lit", ex.value))
            else:
                raise ConversionError(f"awk: unsupported END print expr {ex!r}")
    return out


def _printf_seps_padded(fmt: str) -> list:
    """%d / %s / %.2f ... slot markers (for matching exprs to slots)."""
    return re.findall(r"%[.\d]*[dfse%]", fmt) or None


def _classify_scalar(spec, body, end):
    acc = None       # (name, rhs-AST) for `x += expr`
    cnt = None       # count accumulator (`n++`)
    for st in body:
        if isinstance(st, Assign) and isinstance(st.lhs, Var):
            if st.op == "+=":
                acc = (st.lhs.name, st.rhs)
            elif st.op == "++":
                cnt = st.lhs.name
            elif st.op == "=":
                if isinstance(st.rhs, Call) and st.rhs.name == "gsub":
                    # `gsub(/[^0-9.+-]/, "", v)` cleanses a var in place
                    if len(st.rhs.args) == 3 and isinstance(st.rhs.args[0], Regex) \
                            and st.rhs.args[0].value == r"[^0-9.+-]" \
                            and isinstance(st.rhs.args[2], Var):
                        spec.cleanse_var = st.rhs.args[2].name
                        continue
                    raise ConversionError(f"awk: unsupported gsub {st.rhs!r}")
                if isinstance(st.rhs, Call) and st.rhs.name == "split":
                    continue  # split handled by spec.split / split_nvar
                spec.lets.append((st.lhs.name, st.rhs))
            else:
                raise ConversionError(f"awk: unsupported scalar assign {st!r}")
        elif isinstance(st, Call) and st.name == "gsub":
            # bare `gsub(/[^0-9.+-]/, "", v)` — in-place cleanse of v
            if len(st.args) == 3 and isinstance(st.args[0], Regex) \
                    and st.args[0].value == r"[^0-9.+-]" \
                    and isinstance(st.args[2], Var):
                spec.cleanse_var = st.args[2].name
                continue
            raise ConversionError(f"awk: unsupported gsub {st!r}")
        elif isinstance(st, Call) and st.name == "split":
            continue  # spec.split is set by _find_split
        elif isinstance(st, Call) and st.name == "sub":
            # `sub(/\r$/, "", var)` — no-op on LF data (CR-strip the let)
            if len(st.args) == 3 and isinstance(st.args[0], Regex) \
                    and st.args[0].value == r"\r$" and isinstance(st.args[2], Var):
                spec.cr_lets[st.args[2].name] = True
                continue
            raise ConversionError(f"awk: unsupported sub {st!r}")
        elif isinstance(st, If) and len(st.body) == 1 \
                and isinstance(st.body[0], Next):
            spec.skip_conds.append(st.cond)
        elif isinstance(st, Next):
            continue
        else:
            raise ConversionError(f"awk: unsupported scalar body stmt {st!r}")
    if acc is None:
        raise ConversionError("awk: no scalar accumulator found")
    spec.val = acc[1]
    mode, fmt, expr, empty = _scalar_end(end)
    spec.sempty = empty
    # `int(sum / n)` unwraps to the division; otherwise avg detection misses it.
    if isinstance(expr, Call) and expr.name == "int" and len(expr.args) == 1:
        expr = expr.args[0]
    if isinstance(expr, Bin) and expr.op == "/":
        spec.family = "scalar_avg"
        spec.sround = _fmt_round(fmt)
        if spec.sround is None:
            spec.sround = "int"  # integer division by default
    else:
        spec.family = "scalar_sum"
        spec.sround = _fmt_round(fmt)
    return spec


def _fmt_round(fmt: str | None) -> str | None:
    if not fmt:
        return None
    if "%d" in fmt:
        return "int"
    if "%." in fmt:
        m = re.search(r"%\.(\d+)f", fmt)
        if m:
            return "dec2" if m.group(1) == "2" else "round"
    if "%f" in fmt:
        return "dec2"
    return None


def _scalar_end(end):
    """Output statement(s) of a scalar END block → (mode, fmt, expr, empty)."""
    out = None
    empty = None
    for st in end:
        if isinstance(st, Printf):
            out = ("printf", st.fmt,
                   st.exprs[0] if st.exprs else None, empty)
        elif isinstance(st, Print):
            if out is None:
                out = ("print", None,
                       st.exprs[0] if st.exprs else None, empty)
            elif len(st.exprs) == 1 and isinstance(st.exprs[0], Num):
                empty = st.exprs[0]
        elif isinstance(st, If):
            for bst in st.body:
                if isinstance(bst, Printf):
                    out = ("printf", bst.fmt,
                           bst.exprs[0] if bst.exprs else None, empty)
            for est in st.els or []:
                if isinstance(est, Print) and len(est.exprs) == 1 \
                        and isinstance(est.exprs[0], Num):
                    empty = est.exprs[0]
    if out is None:
        raise ConversionError(f"awk: unsupported scalar END {end!r}")
    return out


def _classify_select(spec, body):
    spec.family = "select"
    for st in body:
        if isinstance(st, Assign) and isinstance(st.lhs, Var):
            if spec.split_nvar and st.lhs.name == spec.split_nvar:
                continue  # the split count maps to the array length
            spec.lets.append((st.lhs.name, st.rhs))
        elif isinstance(st, Assign) and isinstance(st.lhs, Arr):
            raise ConversionError("awk: array assignment in a select body")
        elif isinstance(st, Print):
            spec.print_args = list(st.exprs)
            spec.print_mode = "print"
        elif isinstance(st, Printf):
            spec.print_args = list(st.exprs)
            spec.print_mode = "printf"
            spec.printf_fmt = st.fmt
        elif isinstance(st, If):
            spec.cond2 = st.cond
            for bst in st.body:
                if isinstance(bst, Print):
                    spec.print_args = list(bst.exprs)
                    spec.print_mode = "print"
                elif isinstance(bst, Printf):
                    spec.print_args = list(bst.exprs)
                    spec.print_mode = "printf"
                    spec.printf_fmt = bst.fmt
                else:
                    raise ConversionError(
                        f"awk: unsupported select guard body {bst!r}")
        elif isinstance(st, Call) and st.name == "split":
            continue  # spec.split is set by _find_split
        elif isinstance(st, Call) and st.name == "gsub":
            # `gsub(/^"/, "", $5)` — cleanse a FIELD in place. Later reads of
            # that field are rewritten to the cleansed let.
            if len(st.args) == 3 and isinstance(st.args[0], Regex) \
                    and isinstance(st.args[2], Field) \
                    and st.args[2].index is not None:
                idx = st.args[2].index
                if idx not in spec.fld_cleanse:
                    var = f"g{idx}"
                    spec.fld_cleanse[idx] = (var, st.args[0].value)
                continue
            raise ConversionError(f"awk: unsupported gsub {st!r}")
        elif isinstance(st, Next):
            continue
        else:
            raise ConversionError(f"awk: unsupported select stmt {st!r}")
    if not spec.print_args:
        raise ConversionError("awk: select without a print")
    # Rewrite post-gsub field reads (`print $5` after `gsub(..., $5)`) to the
    # cleansed variable so the renderer never re-reads the raw field.
    for idx, (var, _) in spec.fld_cleanse.items():
        spec.print_args = [_fld_to_var(e, idx, var) for e in spec.print_args]
        if spec.cond2 is not None:
            spec.cond2 = _fld_to_var(spec.cond2, idx, var)
    return spec


def _fld_to_var(e, idx: int, var: str):
    """Replace reads of Field(idx) with Var(var) inside a select expr."""
    if isinstance(e, Field):
        return Var(var) if e.index == idx else e
    if isinstance(e, Bin):
        return Bin(e.op, _fld_to_var(e.left, idx, var),
                   _fld_to_var(e.right, idx, var))
    if isinstance(e, Call):
        return Call(e.name, [_fld_to_var(a, idx, var) for a in e.args])
    if isinstance(e, Arr):
        return e
    if isinstance(e, (Var, Num, Str, Regex)):
        return e
    raise ConversionError(f"awk: cannot rewrite select expr {e!r}")


def classify(program: Program, fs) -> AwkSpec:
    """Classify a parsed restricted-awk program into an AwkSpec."""
    spec = AwkSpec(family="", fs=fs)
    rules = []
    for rule in program.rules:
        if _is_cr_strip_rule(rule):
            spec.cr_strip = True
            continue
        if _nr_next(rule):
            spec.skip += 1
            continue
        pat = rule.pattern
        if pat is not None:
            skip_n, rest = _nr_gt_split(pat)
            if skip_n:
                spec.skip += skip_n
                pat = rest
        if pat is not None:
            nf, rest = _nf_guard_split(pat)
            if nf is not None:
                if spec.nf_min is not None and spec.nf_min != nf:
                    raise ConversionError(
                        f"awk: conflicting NF guards {spec.nf_min} vs {nf}")
                spec.nf_min = nf
                pat = rest
        rules.append(Rule(pat, rule.body))
    if not rules:
        raise ConversionError("awk: no main rule (END-only programs unsupported)")
    if len(rules) > 1:
        raise ConversionError("awk: multiple pattern blocks are not supported")
    main = rules[0]
    if main.pattern is not None:
        spec.cond = main.pattern
    if main.body is None:
        # bare pattern → awk's default action prints the whole matching line
        # (e.g. `awk '$5=="pending"' | wc -l` counts the matched records).
        spec.family = "select"
        spec.print_args = [Field(whole=True)]
        spec.print_mode = "print"
        return spec
    body = main.body
    _find_split(spec, body)
    has_arr_inc, has_var_acc = _body_shape(body)
    if has_arr_inc:
        return _classify_group(spec, body, program.end)
    if has_var_acc:
        return _classify_scalar(spec, body, program.end)
    return _classify_select(spec, body)
