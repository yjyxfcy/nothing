"""LaTeX preprocessor: clean up agent-generated LaTeX before translation.

Agent-generated LaTeX is often "dirty" — stray backslashes, unmatched braces,
non-standard command usage, deprecated packages. This preprocessor normalizes
it before feeding to any translator (Tylax, Pandoc, or regex).

Design principle: be conservative. Only fix what we're confident about.
When in doubt, leave it alone — the translator can handle it or fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PreprocessResult:
    content: str
    fixes_applied: list[str] = field(default_factory=list)

    @property
    def fix_count(self) -> int:
        return len(self.fixes_applied)


# ============================================================================
# Individual fix functions — each returns (new_text, fix_description or None)
# ============================================================================


def _fix_unmatched_braces(text: str) -> tuple[str, str | None]:
    """Fix unmatched {} pairs by adding missing closing braces.

    Agent-generated LaTeX often has \textbf{something without closing }.
    We count braces and add missing ones at reasonable positions.
    """
    # Count braces (excluding math mode $...$ and \[...\])
    # Simple heuristic: if { count > } count, add } at end of each line
    # that seems to have unclosed braces
    open_count = text.count("{") - text.count("\\{")
    close_count = text.count("}") - text.count("\\}")
    if open_count <= close_count:
        return text, None

    # Try to find the most likely positions for missing closing braces
    # Strategy: find lines where inline commands like \textbf{, \textit{, etc.
    # appear without matching closing braces
    fixes = 0
    lines = text.split("\n")
    for i, line in enumerate(lines):
        # Count braces on this line
        line_open = line.count("{") - line.count("\\{")
        line_close = line.count("}") - line.count("\\}")
        if line_open > line_close:
            missing = line_open - line_close
            # Add closing braces at end of line (before any trailing whitespace)
            lines[i] = line.rstrip() + "}" * missing
            fixes += missing

    if fixes > 0:
        return "\n".join(lines), f"Added {fixes} missing closing braces"
    return text, None


def _fix_double_escaping(text: str) -> tuple[str, str | None]:
    """Fix double-escaped LaTeX commands like \\\\textbf → \\textbf.

    Agent sometimes over-escapes backslashes: \\\\section → \\section.
    """
    # Pattern: \\ followed by a LaTeX command (letter-based)
    old = text
    # Fix \\\\[a-zA-Z] → \\[a-zA-Z] (double backslash before command)
    text = re.sub(r"(?<!\\)\\\\\\\([a-zA-Z]+)", r"\\\1", text)
    # Fix \\[a-zA-Z] where the first \ is escaped (in JSON strings, etc.)
    # But DON'T fix \\\\ which is intentional line break
    if text != old:
        return text, "Fixed double-escaping of LaTeX commands"
    return text, None


def _fix_stray_backslashes(text: str) -> tuple[str, str | None]:
    """Remove truly stray backslashes that aren't part of any LaTeX command.

    A backslash followed by a space or at end of line is almost always a mistake.
    """
    old = text
    # Backslash followed by whitespace and not part of \\[optional_args]{mandatory}.
    # `(?<!\\)` / `(?!\\)` keep every backslash that belongs to a `\\` line
    # break. Without them the two rules below gang up on a table row separator:
    # the second `\` is followed by a newline (rule 1 deletes it), which leaves
    # the first `\` alone at end of line (rule 2 deletes it). The engines then
    # receive a tabular with no row boundaries at all and silently merge the
    # rows into single cells -- still compiling, so nothing ever caught it.
    # \] and \) close standard display/inline math; deleting their backslash
    # can make the converter absorb the remainder of the document into math.
    text = re.sub(r"(?<!\\)\\(?![\\)\]])(?=\s*[\s,.;:!?)}\]])", "", text)
    # Lone backslash at end of line (not \\ line break)
    text = re.sub(r"(?<!\\)\\(?!\\)(?=\s*$)", "", text, flags=re.MULTILINE)
    if text != old:
        return text, "Removed stray backslashes"
    return text, None


def _fix_math_delimiters(text: str) -> tuple[str, str | None]:
    """Normalize math mode delimiters.

    Agent sometimes uses inconsistent math delimiters:
    - \\( ... \\)  → $...$ (inline)
    - \\[ ... \\]  → $$...$$ (display) — but keep if already standard
    - Missing $ around math commands used as text
    """
    old = text
    # Fix: \\(...\\) → $...$  (inline math)
    text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text)
    # Already standard — no fix needed for \\[...\\]

    # Fix: lone \mathbb, \mathcal etc. outside math mode → wrap in $$
    # (Only do this if there's no active math mode on the line)
    # Actually this is risky — skip for now

    if text != old:
        return text, "Normalized math delimiters"
    return text, None


def _fix_verbatim_commands(text: str) -> tuple[str, str | None]:
    """Replace \\verb|...| with \\texttt{...} since Tylax struggles with \\verb.

    Also handle \\verb+...+ and \\verb!...! variants.
    """
    fixes = 0

    def _replace_verb(m):
        nonlocal fixes
        fixes += 1
        content = m.group(1)
        return f"\\texttt{{{content}}}"

    # \verb|...|  (pipe delimiter)
    text = re.sub(r"\\verb\|([^|]*)\|", _replace_verb, text)
    # \verb+...+
    text = re.sub(r"\\verb\+([^+]*)\+", _replace_verb, text)
    # \verb!...!
    text = re.sub(r"\\verb!([^!]*)!", _replace_verb, text)

    if fixes > 0:
        return text, f"Replaced {fixes} \\verb commands with \\texttt"
    return text, None


def _fix_textbackslash_in_tt(text: str) -> tuple[str, str | None]:
    """Fix \\textbackslash inside \\texttt{} — Tylax can't parse this.

    \\texttt{\\textbackslash vspace} → \\texttt{\\vspace}
    """
    old = text
    # \textbackslash followed by a word (inside texttt typically)
    text = re.sub(r"\\textbackslash\s+([a-zA-Z])", r"\\\1", text)
    # \textbackslash{}word  → \word
    text = re.sub(r"\\textbackslash\{\}([a-zA-Z])", r"\\\1", text)
    if text != old:
        return text, "Simplified \\textbackslash usage"
    return text, None


def _fix_environments(text: str) -> tuple[str, str | None]:
    """Strip or simplify problematic environments.

    - Remove \\newenvironment definitions (Tylax can't use them)
    - Convert \\begin{comment}...\\end{comment} to comments
    - Convert \\begin{proof}...\\end{proof} (no Typst native equivalent)
    """
    fixes = []

    # Remove \newenvironment{name}{begin}{end} definitions
    # These are 3-argument patterns. We strip the whole definition.
    newenv_count = len(re.findall(r"\\newenvironment\{", text))
    if newenv_count > 0:
        # Remove each \newenvironment block (they're multi-line, so use DOTALL)
        # \newenvironment{name}{...}{...}  — 3 brace groups, possibly spanning lines
        def _strip_newenv(m):
            return f"% [preprocessor: removed \\newenvironment{{{m.group(1)}}}]"

        text = re.sub(
            r"\\newenvironment\{([^}]*)\}\s*\{(.*?)\}\s*\{(.*?)\}",
            _strip_newenv,
            text,
            flags=re.DOTALL,
        )
        fixes.append(f"Removed {newenv_count} \\newenvironment definitions")

    # Convert theorem-like \begin{proof}...\end{proof} — strip env borders, keep content
    for env in ["proof", "theorem", "lemma", "corollary", "definition",
                "proposition", "example", "remark", "conjecture", "observation"]:
        if f"\\begin{{{env}}}" in text:
            # Replace \begin{env} with a bold label + newline
            text = re.sub(
                rf"\\begin\{{{env}\}}(?:\[[^\]]*\])?\s*",
                f"\n*{env.capitalize()}.* ",
                text,
            )
            text = re.sub(
                rf"\\end\{{{env}\}}\s*",
                "\n",
                text,
            )
            fixes.append(f"Converted {{{env}}} environment borders")

    if fixes:
        return text, "; ".join(fixes)
    return text, None


def _fix_graphics_options(text: str) -> tuple[str, str | None]:
    """Simplify \\includegraphics options that Tylax can't handle.

    \\includegraphics[width=0.6\\textwidth]{file} → \\includegraphics{file}
    We keep the filename but strip formatting options.
    """
    old = text
    # Strip [options] from \includegraphics
    text = re.sub(
        r"\\includegraphics\*?\s*\[([^\]]*)\]\s*\{",
        r"\\includegraphics{",
        text,
    )

    # Handle \includegraphics without options (already fine)
    if text != old:
        return text, "Simplified \\includegraphics options (kept filename only)"
    return text, None


def _fix_vspace_hspace(text: str) -> tuple[str, str | None]:
    """Convert \\vspace and \\hspace to simpler forms.

    These are purely visual spacing — in a compat layer, we can simplify
    or remove them since Typst handles spacing differently.
    """
    fixes = 0

    # \vspace{...} → blank line (content is mostly spacing)
    # Only do this for simple length values, not \fill or \stretch
    def _vspace_repl(m):
        nonlocal fixes
        fixes += 1
        arg = m.group(1)
        if "fill" in arg or "stretch" in arg:
            return "\n#v(1fr)\n"
        return "\n#v(0.5em)\n"

    text = re.sub(r"\\vspace\*?\{([^}]*)\}", _vspace_repl, text)

    # \hspace{...} → just a space (or remove)
    def _hspace_repl(m):
        nonlocal fixes
        fixes += 1
        return " "

    text = re.sub(r"\\hspace\*?\{([^}]*)\}", _hspace_repl, text)

    # \hfill → #h(1fr)
    if "\\hfill" in text:
        text = text.replace("\\hfill", "#h(1fr)")
        fixes += text.count("#h(1fr)")

    if fixes > 0:
        return text, f"Simplified {fixes} vspace/hspace commands"
    return text, None


def _fix_setcounter_renew(text: str) -> tuple[str, str | None]:
    """Remove \\setcounter, \\renewcommand, \\newcommand (LaTeX internals)."""
    fixes = 0

    for cmd in ["setcounter", "addtocounter", "stepcounter", "refstepcounter",
                "renewcommand", "newcommand", "providecommand",
                "newtheorem", "newtheoremstyle", "theoremstyle"]:
        count = len(re.findall(rf"\\{cmd}\b", text))
        if count > 0:
            # Remove lines containing these commands
            text = re.sub(rf"^.*\\{cmd}\b.*$", "", text, flags=re.MULTILINE)
            fixes += count

    if fixes > 0:
        return text, f"Removed {fixes} LaTeX internal commands"
    return text, None


def _fix_href_commands(text: str) -> tuple[str, str | None]:
    """Convert \\href{url}{text} to Typst #link syntax BEFORE Tylax.

    Tylax translates \\href to something that creates <url> labels
    which fail in Typst. Converting to #link first avoids this.
    """
    fixes = 0

    def _replace_href(m):
        nonlocal fixes
        fixes += 1
        url = m.group(1)
        link_text = m.group(2)
        return f"#link(\"{url}\")[{link_text}]"

    # \href{url}{text}  (simple)
    text = re.sub(r'\\href\{([^}]+)\}\{([^}]+)\}', _replace_href, text)

    # \href{url}{text with \nested{commands}} (nested braces)
    # Use a simpler approach: just extract URL, keep text as-is
    def _replace_href_nested(m):
        nonlocal fixes
        fixes += 1
        url = m.group(1)
        rest = m.group(2)
        # Find matching closing brace for the text argument
        depth = 1
        i = 0
        result = []
        while i < len(rest) and depth > 0:
            if rest[i] == '{':
                depth += 1
            elif rest[i] == '}':
                depth -= 1
            if depth > 0:
                result.append(rest[i])
            i += 1
        link_text = ''.join(result)
        return f"#link(\"{url}\")[{link_text}]"

    if fixes == 0:
        # Try nested version
        text = re.sub(r'\\href\{([^}]+)\}\{(.+?)\}(?=\s|$|[.,;:!?)])', _replace_href_nested, text)

    if fixes > 0:
        return text, f"Converted {fixes} \\href to #link"
    return text, None


def _fix_cite_commands(text: str) -> tuple[str, str | None]:
    """Convert \\cite{key} → @key and strip bibliography commands.

    Tylax can't handle bibliography/citation system.
    """
    fixes = 0

    # \cite{key1,key2} → @key1 @key2
    def _replace_cite(m):
        nonlocal fixes
        fixes += 1
        keys = [k.strip() for k in m.group(1).split(',')]
        return ', '.join(f'@{k}' for k in keys)

    text = re.sub(r'\\cite\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citep\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citet\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citeauthor\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citeyear\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citealt\{([^}]+)\}', _replace_cite, text)
    text = re.sub(r'\\citealp\{([^}]+)\}', _replace_cite, text)

    # Strip bibliography infrastructure
    text = re.sub(r'\\bibliography\{[^}]*\}', '', text)
    text = re.sub(r'\\bibliographystyle\{[^}]*\}', '', text)
    text = re.sub(r'\\addbibresource\{[^}]*\}', '', text)
    text = re.sub(r'\\printbibliography\b', '', text)

    # Strip thebibliography environment
    text = re.sub(r'\\begin\{thebibliography\}\{[^}]*\}.*?\\end\{thebibliography\}',
                  '', text, flags=re.DOTALL)

    if fixes > 0:
        return text, f"Converted {fixes} \\cite to @ref and stripped bibliography"
    return text, None


def _fix_verb_pipe_escape(text: str) -> tuple[str, str | None]:
    """Ensure \\verb|...| content won't produce stray | in Tylax output.

    Tylax sometimes leaves the pipe delimiter in the output.
    Convert ALL \\verb variants to \\texttt before Tylax runs.
    """
    fixes = 0

    # \verb|text| → \texttt{text}
    for delim in ['|', '+', '!', '#', '$', '%', '&', '@', '^', '_', '~']:
        escaped_delim = re.escape(delim)
        pattern = rf'\\verb\{escaped_delim}([^{escaped_delim}]*)\{escaped_delim}'
        def _make_repl(d):
            return lambda m: _count_and_replace(m, d)
        text = re.sub(pattern, lambda m, d=delim: _verb_repl(m, fixes), text)

    def _verb_repl(m, counter_list):
        counter_list[0] += 1
        return f'\\texttt{{{m.group(1)}}}'

    # Actually do it properly
    fix_count = [0]
    for delim in ['|', '+', '!', '#', '$', '%', '&', '@', '^', '_', '~']:
        escaped = re.escape(delim)
        def repl(m, c=fix_count):
            c[0] += 1
            return f'\\texttt{{{m.group(1)}}}'
        text = re.sub(rf'\\verb\{escaped}([^{escaped}]*)\{escaped}', repl, text)

    if fix_count[0] > 0:
        return text, f"Converted {fix_count[0]} \\verb to \\texttt"
    return text, None


def _fix_enumitem_options(text: str) -> tuple[str, str | None]:
    """Strip enumitem package options from itemize/enumerate.

    \\begin{itemize}[label=--]  → \\begin{itemize}
    \\begin{enumerate}[label=(\\roman*)] → \\begin{enumerate}
    """
    old = text
    text = re.sub(
        r"(\\begin\{(?:itemize|enumerate|description)\})\s*\[[^\]]*\]",
        r"\1",
        text,
    )
    if text != old:
        return text, "Stripped enumitem options from list environments"
    return text, None


def _fix_unicode_escaping(text: str) -> tuple[str, str | None]:
    """Fix incorrectly escaped Unicode characters.

    Agent sometimes produces \\uXXXX sequences from JSON, or
    malformed UTF-8 that shows up as escape sequences.
    """
    # Replace literal \uXXXX with actual Unicode if it appears
    old = text

    def _unescape(m):
        try:
            cp = int(m.group(1), 16)
            if cp < 0xD800 or cp > 0xDFFF:  # Skip surrogates
                return chr(cp)
        except (ValueError, OverflowError):
            pass
        return m.group(0)

    text = re.sub(r"\\u([0-9a-fA-F]{4})", _unescape, text)

    if text != old:
        return text, "Fixed Unicode escape sequences"
    return text, None


def _fix_bigl_bigr(text: str) -> tuple[str, str | None]:
    """Simplify \\Bigl, \\Bigr, \\bigl, \\bigr etc.

    Typst auto-sizes delimiters, so these size hints are unnecessary.
    Just remove them — the content inside parentheses/brackets stays.
    """
    old = text
    for cmd in ["Biggl", "Biggr", "biggl", "biggr",
                "Bigl", "Bigr", "bigl", "bigr",
                "big", "Big", "bigg", "Bigg"]:
        text = re.sub(rf"\\{cmd}\b", "", text)
    if text != old:
        return text, "Removed manual delimiter sizing (Typst auto-sizes)"
    return text, None


def _fix_tabbing_spaces(text: str) -> tuple[str, str | None]:
    """Replace \\quad and \\qquad in text mode with regular spaces.

    These are spacing commands that don't have meaning in Typst text mode.
    In math mode they're handled by the math translator.
    """
    # Only fix outside math mode — but this is complex to detect.
    # For simplicity, just reduce to single space.
    old = text
    text = re.sub(r"\\qquad\b", " ", text)
    text = re.sub(r"\\quad\b", " ", text)
    if text != old:
        return text, "Simplified \\quad/\\qquad to spaces"
    return text, None


def _fix_rule_command(text: str) -> tuple[str, str | None]:
    """Convert \\rule{w}{h} → #line or just remove.

    \\rule{6cm}{0.4pt} is a horizontal line, used for signature blocks.
    """
    old = text
    # \rule{width}{thickness} → horizontal line
    text = re.sub(r"\\rule\{([^}]*)\}\{([^}]*)\}", r"#line(length: \1)", text)
    if text != old:
        return text, "Converted \\rule to #line"
    return text, None


def _fix_text_commands_in_math(text: str) -> tuple[str, str | None]:
    """Fix \\text{} inside math mode — Tylax should handle but sometimes fails.

    \\text{EVI} in math mode requires special handling.
    We leave this to the translator engines, but pre-normalize.
    """
    # This is tricky to fix in preprocessing — leave to translators
    return text, None


# ============================================================================
# Preprocessing pipeline
# ============================================================================

FIX_FUNCTIONS = [
    ("double_escaping", _fix_double_escaping),
    ("unicode_escaping", _fix_unicode_escaping),
    ("stray_backslashes", _fix_stray_backslashes),
    ("href_commands", _fix_href_commands),
    ("cite_commands", _fix_cite_commands),
    ("verb_pipe_escape", _fix_verb_pipe_escape),
    ("verbatim_commands", _fix_verbatim_commands),
    ("textbackslash_in_tt", _fix_textbackslash_in_tt),
    ("math_delimiters", _fix_math_delimiters),
    ("environments", _fix_environments),
    ("enumitem_options", _fix_enumitem_options),
    ("graphics_options", _fix_graphics_options),
    ("vspace_hspace", _fix_vspace_hspace),
    ("setcounter_renew", _fix_setcounter_renew),
    ("bigl_bigr", _fix_bigl_bigr),
    ("unmatched_braces", _fix_unmatched_braces),
    ("tabbing_spaces", _fix_tabbing_spaces),
    ("rule_command", _fix_rule_command),
]


def preprocess(latex: str, verbose: bool = False) -> PreprocessResult:
    """Apply all preprocessing fixes to agent-generated LaTeX.

    Args:
        latex: Raw LaTeX content from agent output
        verbose: If True, print each fix applied

    Returns:
        PreprocessResult with cleaned content and list of fixes
    """
    text = latex
    all_fixes = []

    for fix_name, fix_fn in FIX_FUNCTIONS:
        try:
            text, fix_desc = fix_fn(text)
            if fix_desc:
                all_fixes.append(fix_desc)
                if verbose:
                    print(f"  [preprocess] {fix_name}: {fix_desc}")
        except Exception as e:
            if verbose:
                print(f"  [preprocess] {fix_name}: ERROR — {e}")

    # Final cleanup
    text = re.sub(r"\n{3,}", "\n\n", text)  # Collapse blank lines
    text = text.strip()

    return PreprocessResult(content=text, fixes_applied=all_fixes)
