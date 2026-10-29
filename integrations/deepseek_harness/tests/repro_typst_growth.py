"""Regression check for repeated projection/delivery with Tylax.

This case previously grew by 165 bytes per round when a source document used a
heading-numbering directive identical to Tylax's injected default. It now
asserts byte-stable output from the second projection onward. The comparison
document covers the ordinary path where the source does not contain that
directive.

Run through ``run_acceptance.ps1`` so the configured Tylax path is supplied.
Direct use: set ``ROSETTA_TYLAX`` or put ``t2l`` on ``PATH`` first.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap
import importlib.util

BRIDGE = pathlib.Path(__file__).resolve().parents[1] / "python" / "rosetta_harness_bridge.py"

SOURCE = textwrap.dedent('''\
    #set page(paper: "a4", margin: 2.4cm)
    #set heading(numbering: "1.")

    #show heading.where(level: 1): it => block(
      above: 1.2em,
      below: 0.8em,
      text(weight: "bold", fill: rgb("#123a63"), it.body),
    )

    = Release 4.2

    == Highlights

    The scheduler now reserves capacity per tenant, so a noisy tenant can no
    longer consume the whole queue.

    == Upgrade notes

    Restart the workers after deploying; the on-disk format is unchanged.
    ''')

CONTROL = textwrap.dedent('''\
    #set page(paper: "a4", margin: 2.4cm)

    = Release 4.2

    == Highlights

    The scheduler now reserves capacity per tenant.
    ''')


def load_bridge():
    spec = importlib.util.spec_from_file_location("rosetta_growth_regression", BRIDGE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load bridge from {BRIDGE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_case(rb, workspace: pathlib.Path, label: str, source: str, rounds: int = 6) -> bool:
    workspace.mkdir(parents=True, exist_ok=False)
    roots = rb.Roots([str(workspace)])
    rb.ensure_usable_temp(roots)
    store = rb.Store(workspace / ".rosetta" / "sessions.sqlite3")
    try:
        app = rb.Rosetta(roots, store, dict(os.environ))
        pair = app.pairs["LaTeX / Typst"]
        target = workspace / "document.typ"
        target.write_text(source, encoding="utf-8")

        print(f"\n=== {label} ===\n  source: {len(source.encode('utf-8'))} bytes")
        previous: str | None = None
        stable = True
        for round_index in range(1, rounds + 1):
            projected, _ = rb.project_with_preservation(pair, target.read_text(encoding="utf-8"))
            if not projected.success:
                print(f"  round {round_index}: projection failed {projected.errors}")
                return False
            delivered = pair.deliver(projected.content)
            if not delivered.success:
                print(f"  round {round_index}: delivery failed {delivered.errors}")
                return False
            engine = (delivered.metadata or {}).get("engine")
            if engine != "tylax":
                print(f"  round {round_index}: expected Tylax, got {engine!r}")
                return False

            content = delivered.content
            changed = previous is not None and content != previous
            stable = stable and not changed
            comparison = str(changed) if previous is not None else "first"
            print(
                f"  round {round_index}: {len(content.encode('utf-8')):5d} bytes"
                f" | engine={engine}"
                f" | blocks={content.count('// @TYPST-PASSTHROUGH')}"
                f" | changed-from-prior={comparison}"
            )
            target.write_text(content, encoding="utf-8")
            previous = content

        print("  -> " + ("PASS: byte-stable after the first delivery" if stable else "FAIL: output changed across rounds"))
        return stable
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=pathlib.Path,
        help="existing writable workspace for temporary regression artifacts",
    )
    args = parser.parse_args()

    tylax = os.environ.get("ROSETTA_TYLAX") or shutil.which("t2l")
    if not tylax or not pathlib.Path(tylax).is_file():
        print("SKIP: Tylax is not configured; this regression must not pass on the regex fallback.")
        return 77

    base = args.workspace or (pathlib.Path(__file__).resolve().parents[1] / "acceptance-workspaces")
    base.mkdir(parents=True, exist_ok=True)
    rb = load_bridge()
    with tempfile.TemporaryDirectory(prefix="typst-idempotence-", dir=str(base)) as root:
        root_path = pathlib.Path(root)
        cases = (
            ("source repeats Tylax's heading-numbering default", SOURCE),
            ("control without a duplicate default", CONTROL),
        )
        outcomes = []
        for index, (label, source) in enumerate(cases, start=1):
            outcomes.append(check_case(rb, root_path / f"case-{index}", label, source))

    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
