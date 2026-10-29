"""Bash ⇄ Nushell compat layer: bashlex AST → ShellOp IR → Nu 0.114.1 generator.

Anything outside the recognized controlled subset raises ``ConversionError``
instead of silently guessing a translation.

The layer is bidirectional over that subset: ``nu_to_bash`` runs the same IR
backwards (Nu → IR → Bash), so a Nushell program can be projected into Bash
without a second, hand-written translator.

Both directions also carry what the subset cannot express, in the same way and
for the same reason: a ``# @NU-PASSTHROUGH`` block is raw Nu the forward leg
preserves instead of translating, and a fragment the reverse leg cannot classify
is carried as an opaque region and rendered back into that same block.  Carrying
is never guessing — nothing inside a region is read, so the text survives the
round trip exactly; a statement is translated whole or carried whole, and a
document with no Bash view at all (nothing in it classifies) is still refused.
"""
from .bash_to_ir import bash_to_ir
from .converter import bash_to_nu, nu_to_bash
from .ir import ConversionError, Op, Pipeline, Stmt

__all__ = ["bash_to_ir", "bash_to_nu", "nu_to_bash", "ConversionError", "Op",
           "Pipeline", "Stmt"]
