"""Concrete adapter implementations for specific tool pairs.

Each adapter implements the ToolAdapter interface defined in
compat_layer.interface, providing bidirectional translation between
an old (agent-familiar) format and a new (human-facing) format.

Adapters planned:
    prql.py    — PRQL ↔ SQL (easiest; PRQL has built-in compiler)
    typst.py   — Typst ↔ LaTeX (medium; rule-based + LLM fallback)
    nushell.py — Nushell ↔ Bash (hardest; best-effort with ^bash fallback)
"""
