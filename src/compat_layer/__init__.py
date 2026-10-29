"""Compatibility Layer for LLM Agents.

A generic framework that allows LLM agents to use old (familiar) tools
while humans work with new (modern) tools. The agent never needs to learn
the new tool — it operates on a "file projection" where files appear in
the old format.

Architecture:
    interface.py  — ToolAdapter abstract base class (the "system call" API)
    projector.py  — FileProjector (shadow directory engine)
    adapters/     — concrete implementations for specific tool pairs
"""

from .interface import ToolAdapter, TranslationResult
from .projector import FileProjector

__all__ = ["ToolAdapter", "TranslationResult", "FileProjector"]
