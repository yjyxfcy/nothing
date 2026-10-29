"""Product-facing adapters for the Rosetta Studio prototype.

This module deliberately imports no benchmark, scorer, or agent-runner code.
"""
from __future__ import annotations

from typing import Callable

from compat_layer.interface import TranslationResult
from compat_layer.adapters.eda import EdaVerilogAdapter
from compat_layer.adapters.prql import PRQLAdapter
from compat_layer.adapters.typst_pipeline import TypstPipeline
from compat_layer.adapters.bash_nu import ConversionError, bash_to_nu, nu_to_bash


class Pair:
    def __init__(self, name: str, target: str, view: str,
                 project: Callable[[str], TranslationResult],
                 deliver: Callable[[str], TranslationResult], *,
                 experimental: bool = False,
                 mark_unrepresentable: Callable[[str], tuple[str, list[str]]] | None = None):
        self.name = name
        self.target_extension = target
        self.view_extension = view
        self._project = project
        self._deliver = deliver
        self._mark_unrepresentable = mark_unrepresentable
        self.experimental = experimental

    def project(self, content: str) -> TranslationResult:
        annotated, preserved = self.mark_unrepresentable(content)
        result = self._project(annotated)
        if preserved and result.success:
            result.metadata["preserved_regions"] = preserved
        return result

    def deliver(self, content: str) -> TranslationResult:
        return self._deliver(content)

    def mark_unrepresentable(self, content: str) -> tuple[str, list[str]]:
        """Annotate target-only regions so a projection cannot drop them.

        Returns ``(annotated_content, preserved_descriptions)``. Pairs whose
        target format is fully expressible in the view return the input
        unchanged, which makes the projection a plain translation.
        """
        if self._mark_unrepresentable is None:
            return content, []
        return self._mark_unrepresentable(content)


def _prql_pair() -> Pair:
    adapter = PRQLAdapter()

    def sql_to_prql(content: str) -> TranslationResult:
        result = adapter._sqlglot_to_prql(content.strip().rstrip(";"))
        if not result.success:
            return result
        checked = adapter._validate_roundtrip(result.content, content.strip().rstrip(";"))
        if not checked.success:
            return TranslationResult.fail(
                ["Deterministic SQL-to-PRQL round-trip validation failed.", *checked.errors],
                warnings=checked.warnings,
            )
        result.warnings.extend(checked.warnings)
        result.metadata.update(method="sqlglot", validated=True,
                               validation=checked.metadata.get("validation", "scoped_sql_ast"))
        return result

    return Pair(
        "PRQL / SQL", ".prql", ".sql",
        lambda text: adapter.new_to_old(text),
        sql_to_prql,
    )


def available_pairs() -> dict[str, Pair]:
    typst = TypstPipeline(verbose=False)
    eda = EdaVerilogAdapter()
    return {
        "LaTeX / Typst": Pair(
            "LaTeX / Typst", ".typ", ".tex",
            lambda text: typst.new_to_old(text),
            lambda text: typst.old_to_new(text),
            mark_unrepresentable=typst.mark_unrepresentable,
        ),
        "Verilog / SystemVerilog": Pair(
            "Verilog / SystemVerilog", ".sv", ".v",
            lambda text: eda.new_to_old(text),
            lambda text: eda.old_to_new(text),
        ),
        "PRQL / SQL": _prql_pair(),
    }


def experimental_pairs() -> dict[str, Pair]:
    """The Bash/Nushell adapter remains limited to its documented syntax subset."""

    def translate(fn: Callable[[str], str], content: str) -> TranslationResult:
        try:
            return TranslationResult.ok(fn(content), experimental=True)
        except (ConversionError, ValueError, NotImplementedError) as exc:
            return TranslationResult.fail([str(exc)])

    pair = Pair(
        "Bash / Nushell (experimental)", ".nu", ".sh",
        lambda text: translate(nu_to_bash, text),
        lambda text: translate(bash_to_nu, text),
        experimental=True,
    )
    return {pair.name: pair}


PAIR_LABELS = [
    "LaTeX / Typst",
    "Verilog / SystemVerilog",
    "PRQL / SQL",
    "Bash / Nushell (experimental)",
]


def result_text(result: TranslationResult) -> tuple[str, str]:
    if not result.success:
        return "", "\n".join(result.errors or ["Translation failed."])
    notes = list(result.warnings)
    metadata = result.metadata or {}
    if metadata.get("experimental"):
        notes.append("Experimental Bash/Nushell conversion; inspect semantics and target output.")
    if metadata.get("compiles") is False:
        notes.append("The produced Typst was not confirmed to compile.")
    method = metadata.get("method") or metadata.get("engine")
    if method:
        notes.append(f"Backend: {method}")
    return result.content, "\n".join(notes) or "Translation completed."
