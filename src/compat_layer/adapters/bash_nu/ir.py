"""Bash → IR → Nushell compat layer IR types.

This mirrors the SQL layer's ``sql_to_ir.py -> Relation`` symmetry: bashlex AST
is walked into a list of recognized shell operations (the IR shell analogue of
the Relational IR), and ``ir_to_nu`` renders them to Nu 0.114.1.

Anything the walker cannot recognize exactly raises ``ConversionError``.
"""
from __future__ import annotations


class ConversionError(Exception):
    """The Bash construct is outside the compat layer's controlled subset."""


class Op:
    """One recognized operation; the shell-IR node (shell analogue of a
    relational operator in the SQL layer)."""

    __slots__ = ("op_type", "params")

    def __init__(self, op_type: str, **params):
        self.op_type = op_type
        self.params = params

    def __repr__(self):
        return f"Op({self.op_type}, {self.params})"


class Pipeline:
    """A recognized pipeline: an ordered list of Ops ending in how it prints."""

    __slots__ = ("ops",)

    def __init__(self, ops: list):
        self.ops = ops

    def __repr__(self):
        return f"Pipeline({self.ops!r})"


class Stmt:
    """A non-pipeline statement: assignment / echo / if / for / file op.

    One st_type is not a *translation*: ``passthrough`` holds a fragment of
    Nushell that no IR op denotes (a Nu-only value, a step outside the subset),
    carried as its own text in ``params["text"]``.  It is the reverse leg's
    counterpart of ``converter``'s ``# @NU-PASSTHROUGH`` block: the renderers
    hand it back in each language's own spelling (raw Nu for ``ir_to_nu``,
    marker comments for ``ir_to_bash``) and neither reads it, so it costs the
    fidelity contract nothing.  Where the ops *are* the meaning, this is the
    one node whose meaning is "this text, exactly".
    """

    __slots__ = ("st_type", "params", "body")

    def __init__(self, st_type: str, params: dict | None = None,
                 body: list | None = None):
        self.st_type = st_type
        self.params = params or {}
        self.body = body  # nested statement list for if/for bodies

    def __repr__(self):
        return f"Stmt({self.st_type}, {self.params})"


class Script:
    __slots__ = ("stmts",)

    def __init__(self, stmts: list):
        self.stmts = stmts

    def __repr__(self):
        return f"Script({self.stmts!r})"
