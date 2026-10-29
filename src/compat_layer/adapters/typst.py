"""Typst ↔ LaTeX compatibility adapter — v2 systematic rewrite.

Based on official Typst documentation (typst.app/docs/reference) and standard LaTeX.
Organized by functional category with comprehensive command coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..interface import ToolAdapter, TranslationResult


# ======================================================================
# Multi-pass engine
# ======================================================================

class _RuleEngine:
    """Multi-pass regex translator with math mode protection."""

    def __init__(self, rules: list[tuple[str, str]], pass_name: str = ""):
        self.rules = rules
        self.pass_name = pass_name

    def apply(self, text: str) -> str:
        for pattern, replacement in self.rules:
            try:
                text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
            except re.error:
                continue
        return text


def _protect_math(text: str) -> tuple[str, dict[str, str]]:
    """Extract math blocks into safe placeholders."""
    placeholders = {}
    counter = [0]

    def _replace(m):
        key = f"«MATH{counter[0]}»"
        placeholders[key] = m.group(0)
        counter[0] += 1
        return key

    # Display math: \[...\], $$...$$
    text = re.sub(r'\\\[(.+?)\\\]', _replace, text, flags=re.DOTALL)
    text = re.sub(r'\$\$(.+?)\$\$', _replace, text, flags=re.DOTALL)
    # Inline math: $...$ (single $, not $$)
    text = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', _replace, text)
    # \(...\)
    text = re.sub(r'\\\((.+?)\\\)', _replace, text)
    # \ensuremath{...}
    text = re.sub(r'\\ensuremath\{([^}]*)\}', lambda m: _replace(m), text)
    return text, placeholders


def _protect_verbatim(text: str) -> tuple[str, dict[str, str]]:
    """Extract verbatim/code blocks into safe placeholders."""
    placeholders = {}
    counter = [1000]  # separate counter from math

    def _replace(m):
        key = f"«VB{counter[0]}»"
        placeholders[key] = m.group(0)
        counter[0] += 1
        return key

    text = re.sub(r'\\begin\{verbatim\}.*?\\end\{verbatim\}', _replace, text, flags=re.DOTALL)
    text = re.sub(r'\\begin\{lstlisting\}.*?\\end\{lstlisting\}', _replace, text, flags=re.DOTALL)
    text = re.sub(r'\\verb\|([^|]*)\|', lambda m: _replace(m), text)
    return text, placeholders


def _restore_all(text: str, *placeholders_dicts: dict[str, str]) -> str:
    """Restore all placeholders."""
    for ph_dict in placeholders_dicts:
        for key, value in ph_dict.items():
            text = text.replace(key, value)
    return text


# ======================================================================
# CATEGORY 1: Document Structure (LaTeX → Typst)
# ======================================================================

LATEX_STRUCTURE = [
    # --- Document wrapper (strip) ---
    (r'\\documentclass(?:\[[^\]]*\])?\{[^}]*\}\s*', ''),
    (r'\\usepackage(?:\[[^\]]*\])?\{[^}]*\}\s*', ''),
    (r'\\begin\{document\}\s*', ''),
    (r'\\end\{document\}\s*', ''),
    (r'\\maketitle\s*', ''),
    (r'\\title\{([^}]*)\}\s*', ''),  # Handled by heading
    (r'\\author\{([^}]*)\}\s*', ''),
    (r'\\date\{([^}]*)\}\s*', ''),

    # --- Sectioning ---
    (r'\\section\*?\{([^}]*)\}\s*', r'\n= \1\n'),
    (r'\\subsection\*?\{([^}]*)\}\s*', r'\n== \1\n'),
    (r'\\subsubsection\*?\{([^}]*)\}\s*', r'\n=== \1\n'),
    (r'\\paragraph\{([^}]*)\}\s*', r'\n==== \1\n'),
    (r'\\subparagraph\{([^}]*)\}\s*', r'\n===== \1\n'),

    # --- Abstract ---
    (r'\\begin\{abstract\}\s*', r'\n#quote[\n'),
    (r'\\end\{abstract\}\s*', r'\n]\n'),

    # --- Theorem-like (strip, keep content) ---
    (r'\\begin\{(theorem|lemma|corollary|definition|proposition|example|remark|proof)\}\s*', r'\n'),
    (r'\\end\{(theorem|lemma|corollary|definition|proposition|example|remark|proof)\}\s*', r'\n'),

    # --- Lists ---
    (r'\\begin\{itemize\}\s*', '\n'),
    (r'\\end\{itemize\}\s*', '\n'),
    (r'\\begin\{enumerate\}\s*', '\n'),
    (r'\\end\{enumerate\}\s*', '\n'),
    (r'\\begin\{description\}\s*', '\n'),
    (r'\\end\{description\}\s*', '\n'),
    (r'\\item\s*\[([^\]]*)\]\s*', r'- *\1* '),   # \item[label] → bold bullet
    (r'\\item\s+', '- '),

    # --- Quote ---
    (r'\\begin\{quote\}\s*', r'#quote[\n'),
    (r'\\end\{quote\}\s*', r'\n]\n'),
    (r'\\begin\{quotation\}\s*', r'#quote[\n'),
    (r'\\end\{quotation\}\s*', r'\n]\n'),

    # --- Center ---
    (r'\\begin\{center\}\s*', r'#align(center)[\n'),
    (r'\\end\{center\}\s*', r'\n]\n'),

    # --- Flush left/right ---
    (r'\\begin\{flushleft\}\s*', r'#align(left)[\n'),
    (r'\\end\{flushleft\}\s*', r'\n]\n'),
    (r'\\begin\{flushright\}\s*', r'#align(right)[\n'),
    (r'\\end\{flushright\}\s*', r'\n]\n'),

    # --- Figure (simplified) ---
    (r'\\begin\{figure\*?\}(?:\[[^\]]*\])?\s*', r'#figure(\n  '),
    (r'\\end\{figure\*?\}\s*', r'\n)\n'),
    (r'\\includegraphics\*?(?:\[[^\]]*\])?\{([^}]*)\}', r'image("\1")'),
    (r'\\caption\{([^}]*)\}', r'  caption: [\1]'),
    (r'\\captionof\{([^}]*)\}\{([^}]*)\}', r'#figure(kind: "\1", caption: [\2])['),
    (r'\\label\{([^}]*)\}', r'  kind: "\1"'),  # will be overwritten by inline label rule

    # --- Table ---
    (r'\\begin\{table\}(?:\[[^\]]*\])?\s*', r'\n#figure(\n'),
    (r'\\end\{table\}\s*', r'\n)\n'),

    # --- Tabular (complex — handled separately) ---
    # We strip tabular and leave content; & → comma handled in cleanup

    # --- Equation environments ---
    (r'\\begin\{equation\*?\}\s*', r'$ '),
    (r'\\end\{equation\*?\}\s*', r' $\n'),
    (r'\\begin\{align\*?\}\s*', r'$ '),
    (r'\\end\{align\*?\}\s*', r' $\n'),
    (r'\\begin\{gather\*?\}\s*', r'$ '),
    (r'\\end\{gather\*?\}\s*', r' $\n'),

    # --- Bibliography ---
    (r'\\bibliography\{([^}]*)\}', ''),
    (r'\\bibliographystyle\{([^}]*)\}', ''),
    (r'\\thebibliography\{[^}]*\}', ''),
    (r'\\endthebibliography', ''),
    (r'\\bibitem(?:\[[^\]]*\])?\{([^}]*)\}', r'- @\1'),

    # --- Spacing commands ---
    (r'\\\\\s*', '\n'),                # line break
    (r'\\newline\s*', '\n'),
    (r'\\newpage\s*', '#pagebreak()\n'),
    (r'\\pagebreak\s*', '#pagebreak()\n'),
    (r'\\clearpage\s*', '#pagebreak()\n'),
    (r'\\cleardoublepage\s*', '#pagebreak()\n'),
    (r'\\hspace\*?\{([^}]*)\}', r'#h(\1)'),
    (r'\\vspace\*?\{([^}]*)\}', r'#v(\1)'),
    (r'\\hfill\s*', '#h(1fr)'),
    (r'\\vfill\s*', '#v(1fr)'),
    (r'\\noindent', ''),
    (r'\\indent', '#h(1em)'),
    (r'\\medskip\s*', '#v(0.5em)\n'),
    (r'\\bigskip\s*', '#v(1em)\n'),
    (r'\\smallskip\s*', '#v(0.25em)\n'),
    (r'\\par\s*', '\n\n'),
    (r'\\centering\s*', ''),
    (r'\\raggedright\s*', ''),
    (r'\\raggedleft\s*', ''),

    # --- Horizontal rules ---
    (r'\\hline\s*', '\n#line(length: 100%)\n'),
    (r'\\toprule\s*', ''),
    (r'\\midrule\s*', ''),
    (r'\\bottomrule\s*', ''),

    # --- Text sizing (document-level) ---
    (r'\\tiny\s*', '#set text(7pt)\n'),
    (r'\\scriptsize\s*', '#set text(8pt)\n'),
    (r'\\footnotesize\s*', '#set text(9pt)\n'),
    (r'\\small\s*', '#set text(10pt)\n'),
    (r'\\normalsize\s*', '#set text(11pt)\n'),
    (r'\\large\s*', '#set text(12pt)\n'),
    (r'\\Large\s*', '#set text(14pt)\n'),
    (r'\\LARGE\s*', '#set text(17pt)\n'),
    (r'\\huge\s*', '#set text(20pt)\n'),
    (r'\\Huge\s*', '#set text(25pt)\n'),

    # --- Common packages ---
    (r'\\usepackage\{[^}]*\}', ''),
    (r'\\usetikzlibrary\{[^}]*\}', ''),
    (r'\\definecolor\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}', r'#let \1 = rgb("\3")'),
]


# ======================================================================
# CATEGORY 2: Inline Formatting (LaTeX → Typst)
# ======================================================================

LATEX_INLINE = [
    # --- INNER FIRST: italic, mono, smallcaps (may be nested inside bold) ---
    (r'\\textit\{([^}]*)\}', r'_\1_'),
    (r'\\emph\{([^}]*)\}', r'_\1_'),
    (r'\\texttt\{([^}]*)\}', r'`\1`'),
    (r'\\textsc\{([^}]*)\}', r'#smallcaps[\1]'),
    (r'\\underline\{([^}]*)\}', r'#underline[\1]'),
    (r'\\sout\{([^}]*)\}', r'#strike[\1]'),

    # --- OUTER: bold (wraps inner formatting) ---
    (r'\\textbf\{([^}]*)\}', r'*\1*'),

    # --- Small caps ---
    (r'\\textsc\{([^}]*)\}', r'#smallcaps[\1]'),

    # --- Underline / Strikethrough ---
    (r'\\underline\{([^}]*)\}', r'#underline[\1]'),
    (r'\\sout\{([^}]*)\}', r'#strike[\1]'),

    # --- Superscript / Subscript ---
    (r'\\textsuperscript\{([^}]*)\}', r'#super[\1]'),
    (r'\\textsubscript\{([^}]*)\}', r'#sub[\1]'),

    # --- Text sizing (inline) ---
    (r'\\tiny\{([^}]*)\}', r'#text(size: 7pt)[\1]'),
    (r'\\scriptsize\{([^}]*)\}', r'#text(size: 8pt)[\1]'),
    (r'\\footnotesize\{([^}]*)\}', r'#text(size: 9pt)[\1]'),
    (r'\\small\{([^}]*)\}', r'#text(size: 10pt)[\1]'),
    (r'\\large\{([^}]*)\}', r'#text(size: 12pt)[\1]'),
    (r'\\Large\{([^}]*)\}', r'#text(size: 14pt)[\1]'),
    (r'\\LARGE\{([^}]*)\}', r'#text(size: 17pt)[\1]'),
    (r'\\huge\{([^}]*)\}', r'#text(size: 20pt)[\1]'),
    (r'\\Huge\{([^}]*)\}', r'#text(size: 25pt)[\1]'),

    # --- Color ---
    (r'\\textcolor\{([^}]*)\}\{([^}]*)\}', r'#text(fill: \1)[\2]'),
    (r'\\color\{([^}]*)\}', r'#set text(fill: \1); '),

    # --- References & Citations ---
    (r'\\cite\{([^}]*)\}', r'@\1'),
    (r'\\citep\{([^}]*)\}', r'@\1'),
    (r'\\citet\{([^}]*)\}', r'@\1'),
    (r'\\citeauthor\{([^}]*)\}', r'@\1'),
    (r'\\citeyear\{([^}]*)\}', r'@\1'),
    (r'\\citealt\{([^}]*)\}', r'@\1'),
    (r'\\citealp\{([^}]*)\}', r'@\1'),
    (r'\\ref\{([a-zA-Z][a-zA-Z0-9:_-]*)\}', r'@\1'),
    (r'\\eqref\{([^}]*)\}', r'@\1'),
    (r'\\pageref\{([^}]*)\}', r'@\1-p'),

    # --- Labels (skip URLs / domains) ---
    (r'\\label\{(https?://[^}]*)\}', r''),      # URL label → skip
    (r'\\label\{(www\.[^}]*)\}', r''),          # www domain → skip
    (r'\\label\{(ftp://[^}]*)\}', r''),          # FTP → skip
    (r'\\label\{(mailto:[^}]*)\}', r''),         # email → skip
    (r'\\label\{([a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?)\}', r'<\1>'),  # valid identifier label

    # --- Links ---
    (r'\\url\{([^}]*)\}', r'#link("\1")[\1]'),
    (r'\\href\{([^}]*)\}\{([^}]*)\}', r'#link("\1")[\2]'),

    # --- Footnote ---
    (r'\\footnote\{([^}]*)\}', r'#footnote[\1]'),

    # --- Math font commands (outside $...$ — wrap in $...$) ---
    (r'\\mathbb\{([^}]*)\}', r'$math.bb("\1")$'),
    (r'\\mathcal\{([^}]*)\}', r'$math.cal("\1")$'),
    (r'\\mathscr\{([^}]*)\}', r'$math.scr("\1")$'),
    (r'\\mathfrak\{([^}]*)\}', r'$math.frak("\1")$'),
    (r'\\mathbf\{([^}]*)\}', r'$math.bold(\1)$'),
    (r'\\mathit\{([^}]*)\}', r'$math.italic(\1)$'),
    (r'\\mathrm\{([^}]*)\}', r'$math.upright(\1)$'),
    (r'\\mathsf\{([^}]*)\}', r'$math.sans(\1)$'),
    (r'\\mathtt\{([^}]*)\}', r'$math.mono(\1)$'),

    # --- Boxes ---
    (r'\\mbox\{([^}]*)\}', r'#box[\1]'),
    (r'\\fbox\{([^}]*)\}', r'#box(stroke: 0.5pt)[\1]'),
    (r'\\makebox(?:\[[^\]]*\])?\{([^}]*)\}', r'#box(width: auto)[\1]'),

    # --- Phantom / vphantom / hphantom ---
    (r'\\phantom\{([^}]*)\}', r'#hide[#box[\1]]'),
    (r'\\vphantom\{([^}]*)\}', r'#box(height: 0pt)[\1]'),
    (r'\\hphantom\{([^}]*)\}', r'#box(width: 0pt)[\1]'),

    # --- Non-breaking space ---
    (r'~', ' '),        # LaTeX ~ → Unicode non-breaking space
    (r'\\,', ' '),       # thin space
    (r'\\enspace', ' '), # en space
    (r'\\quad', ' '),    # em space (quad)
    (r'\\qquad', '  '),  # double quad

    # --- Accents (text mode, outside math) ---
    (r"\\'\{([^}]*)\}", r'\1́'),    # acute
    (r'\\`\{([^}]*)\}', r'\1̀'),     # grave
    (r'\\^\{([^}]*)\}', r'\1̂'),     # circumflex
    (r'\\"\{([^}]*)\}', r'\1̈'),     # umlaut/diaeresis
    (r'\\~\{([^}]*)\}', r'\1̃'),     # tilde
    (r'\\=\{([^}]*)\}', r'\1̄'),     # macron
    (r'\\.\{([^}]*)\}', r'\1̇'),     # dot
    (r'\\u\{([^}]*)\}', r'\1̆'),     # breve
    (r'\\v\{([^}]*)\}', r'\1̌'),     # caron/hacek
    (r'\\H\{([^}]*)\}', r'\1̋'),     # double acute
    (r'\\c\{([^}]*)\}', r'\1̧'),     # cedilla
    (r'\\k\{([^}]*)\}', r'\1̨'),     # ogonek
    (r'\\r\{([^}]*)\}', r'\1̊'),     # ring

    # --- Special characters ---
    (r'\\textbackslash\b', r'\\'),
    (r'\\textasciitilde\b', '~'),
    (r'\\textasciicircum\b', '^'),
    (r'\\textdollar\b', '$'),
    (r'\\textunderscore\b', '_'),
    (r'\\textbar\b', '|'),
    (r'\\textless\b', '<'),
    (r'\\textgreater\b', '>'),
    (r'\\textbraceleft\b', '{'),
    (r'\\textbraceright\b', '}'),
    (r'\\#', '#'),
    (r'\\%', '%'),
    (r'\\&', '&'),
    (r'\\_', '_'),
    (r'\\{', '{'),
    (r'\\}', '}'),
    (r'\\pounds\b', '£'),
    (r'\\euro\b', '€'),
    (r'\\S\b', '§'),
    (r'\\P\b', '¶'),
    (r'\\dag\b', '†'),
    (r'\\ddag\b', '‡'),
    (r'\\dots\b', '...'),
    (r'\\ldots\b', '...'),

    # --- Dashes ---
    (r'---', '—'),    # em-dash
    (r'--', '–'),      # en-dash
    (r"\\-", ''),           # soft hyphen → remove

    # --- Quotes (use Typst's built-in smart quotes) ---
    (r'``', '\"'),      # `` → "
    (r"''", '\"'),       # '' → "
    # Don't convert single ` and ' — they appear naturally in text

    # --- Line/page breaks ---
    (r'\\linebreak\s*', '\n'),
    (r'\\nolinebreak\s*', ''),
]


# ======================================================================
# CATEGORY 3: Math Commands (inside $...$ blocks)
# ======================================================================

LATEX_MATH = [
    # --- Math font commands → Typst math functions ---
    (r'\\mathbb\{([^}]*)\}', r'math.bb("\1")'),
    (r'\\mathcal\{([^}]*)\}', r'math.cal("\1")'),
    (r'\\mathscr\{([^}]*)\}', r'math.scr("\1")'),
    (r'\\mathfrak\{([^}]*)\}', r'math.frak("\1")'),
    (r'\\mathbf\{([^}]*)\}', r'math.bold(\1)'),
    (r'\\mathit\{([^}]*)\}', r'math.italic(\1)'),
    (r'\\mathrm\{([^}]*)\}', r'math.upright(\1)'),
    (r'\\mathsf\{([^}]*)\}', r'math.sans(\1)'),
    (r'\\mathtt\{([^}]*)\}', r'math.mono(\1)'),

    # --- Accents ---
    (r'\\hat\{([^}]*)\}', r'hat(\1)'),
    (r'\\bar\{([^}]*)\}', r'overline(\1)'),
    (r'\\overline\{([^}]*)\}', r'overline(\1)'),
    (r'\\tilde\{([^}]*)\}', r'tilde(\1)'),
    (r'\\vec\{([^}]*)\}', r'arrow(\1)'),
    (r'\\dot\{([^}]*)\}', r'dot(\1)'),
    (r'\\ddot\{([^}]*)\}', r'ddot(\1)'),
    (r'\\check\{([^}]*)\}', r'check(\1)'),
    (r'\\breve\{([^}]*)\}', r'breve(\1)'),
    (r'\\acute\{([^}]*)\}', r'acute(\1)'),
    (r'\\grave\{([^}]*)\}', r'grave(\1)'),
    (r'\\widehat\{([^}]*)\}', r'wide(hat, \1)'),
    (r'\\widetilde\{([^}]*)\}', r'wide(tilde, \1)'),

    # --- Fractions & Roots ---
    (r'\\frac\{([^}]*)\}\{([^}]*)\}', r'frac(\1, \2)'),
    (r'\\dfrac\{([^}]*)\}\{([^}]*)\}', r'frac(\1, \2)'),
    (r'\\tfrac\{([^}]*)\}\{([^}]*)\}', r'frac(\1, \2)'),
    (r'\\binom\{([^}]*)\}\{([^}]*)\}', r'binom(\1, \2)'),
    (r'\\sqrt\{([^}]*)\}', r'sqrt(\1)'),
    (r'\\sqrt\[([^\]]*)\]\{([^}]*)\}', r'root(\2, \1)'),

    # --- Greek lowercase ---
    (r'\\alpha\b', 'alpha'), (r'\\beta\b', 'beta'), (r'\\gamma\b', 'gamma'),
    (r'\\delta\b', 'delta'), (r'\\epsilon\b', 'epsilon'), (r'\\varepsilon\b', 'epsilon'),
    (r'\\zeta\b', 'zeta'), (r'\\eta\b', 'eta'), (r'\\theta\b', 'theta'),
    (r'\\vartheta\b', 'theta'), (r'\\iota\b', 'iota'), (r'\\kappa\b', 'kappa'),
    (r'\\lambda\b', 'lambda'), (r'\\mu\b', 'mu'), (r'\\nu\b', 'nu'),
    (r'\\xi\b', 'xi'), (r'\\pi\b', 'pi'), (r'\\varpi\b', 'pi'),
    (r'\\rho\b', 'rho'), (r'\\varrho\b', 'rho'), (r'\\sigma\b', 'sigma'),
    (r'\\varsigma\b', 'sigma'), (r'\\tau\b', 'tau'), (r'\\upsilon\b', 'upsilon'),
    (r'\\phi\b', 'phi'), (r'\\varphi\b', 'phi'),
    (r'\\chi\b', 'chi'), (r'\\psi\b', 'psi'), (r'\\omega\b', 'omega'),
    # Greek uppercase
    (r'\\Gamma\b', 'Gamma'), (r'\\Delta\b', 'Delta'), (r'\\Theta\b', 'Theta'),
    (r'\\Lambda\b', 'Lambda'), (r'\\Xi\b', 'Xi'), (r'\\Pi\b', 'Pi'),
    (r'\\Sigma\b', 'Sigma'), (r'\\Upsilon\b', 'Upsilon'),
    (r'\\Phi\b', 'Phi'), (r'\\Psi\b', 'Psi'), (r'\\Omega\b', 'Omega'),

    # --- Delimiters ---
    (r'\\left\s*([\[\(\{])', r'lr(\1'),
    (r'\\right\s*([\]\)\}])', r'\1)'),
    (r'\\left\.', r'lr(.)'),
    (r'\\right\.', r'.'),
    (r'\\langle\b', 'langle'), (r'\\rangle\b', 'rangle'),
    (r'\\lvert\b', '|'), (r'\\rvert\b', '|'),
    (r'\\lVert\b', '||'), (r'\\rVert\b', '||'),
    (r'\\lfloor\b', 'floor.l'), (r'\\rfloor\b', 'floor.r'),
    (r'\\lceil\b', 'ceil.l'), (r'\\rceil\b', 'ceil.r'),
    (r'\\{', '{'), (r'\\}', '}'),

    # --- Big operators ---
    (r'\\sum\b', 'sum'), (r'\\prod\b', 'product'),
    (r'\\int\b', 'integral'), (r'\\iint\b', 'integral.double'),
    (r'\\iiint\b', 'integral.triple'), (r'\\oint\b', 'integral.cont'),
    (r'\\bigcup\b', 'union.big'), (r'\\bigcap\b', 'sect.big'),
    (r'\\bigoplus\b', 'oplus.big'), (r'\\bigotimes\b', 'otimes.big'),
    (r'\\bigvee\b', 'or.big'), (r'\\bigwedge\b', 'and.big'),

    # --- Symbols ---
    (r'\\infty\b', 'infinity'), (r'\\partial\b', 'partial'), (r'\\nabla\b', 'nabla'),
    (r'\\forall\b', 'forall'), (r'\\exists\b', 'exists'),
    (r'\\emptyset\b', 'emptyset'), (r'\\varnothing\b', 'nothing'),
    (r'\\propto\b', 'proportional'), (r'\\approx\b', 'approx'),
    (r'\\equiv\b', 'equiv'), (r'\\cong\b', 'cong'), (r'\\sim\b', 'tilde.op'),
    (r'\\simeq\b', 'simeq'),
    (r'\\leq\b', 'lt.eq'), (r'\\geq\b', 'gt.eq'),
    (r'\\ll\b', 'lt.lt'), (r'\\gg\b', 'gt.gt'),
    (r'\\neq\b', '!='), (r'\\ne\b', '!='),
    (r'\\times\b', 'times'), (r'\\cdot\b', 'dot.op'),
    (r'\\cdots\b', 'dots.c'), (r'\\ldots\b', 'dots'), (r'\\vdots\b', 'dots.v'),
    (r'\\ddots\b', 'dots.d'),
    (r'\\rightarrow\b', '->'), (r'\\to\b', '->'), (r'\\Rightarrow\b', '=>'),
    (r'\\Leftarrow\b', '<='), (r'\\leftarrow\b', '<-'),
    (r'\\leftrightarrow\b', '<->'), (r'\\Leftrightarrow\b', '<=>'),
    (r'\\longrightarrow\b', '-->'), (r'\\Longrightarrow\b', '==>'),
    (r'\\mapsto\b', '|->'),
    (r'\\subset\b', 'subset'), (r'\\subseteq\b', 'subseteq'),
    (r'\\supset\b', 'supset'), (r'\\supseteq\b', 'supseteq'),
    (r'\\in\b', 'in'), (r'\\notin\b', 'notin'), (r'\\ni\b', 'ni'),
    (r'\\pm\b', 'plus.minus'), (r'\\mp\b', 'minus.plus'),
    (r'\\setminus\b', 'without'), (r'\\cap\b', 'sect'), (r'\\cup\b', 'union'),
    (r'\\oplus\b', 'oplus'), (r'\\otimes\b', 'otimes'),
    (r'\\land\b', 'and'), (r'\\lor\b', 'or'), (r'\\lnot\b', 'not'),
    (r'\\triangle\b', 'triangle'), (r'\\angle\b', 'angle'),
    (r'\\mid\b', '|'), (r'\\nmid\b', '| not'),
    (r'\\parallel\b', 'parallel'), (r'\\nparallel\b', 'parallel not'),

    # --- Functions ---
    (r'\\sin\b', 'sin'), (r'\\cos\b', 'cos'), (r'\\tan\b', 'tan'),
    (r'\\arcsin\b', 'arcsin'), (r'\\arccos\b', 'arccos'), (r'\\arctan\b', 'arctan'),
    (r'\\sinh\b', 'sinh'), (r'\\cosh\b', 'cosh'), (r'\\tanh\b', 'tanh'),
    (r'\\log\b', 'log'), (r'\\ln\b', 'ln'), (r'\\exp\b', 'exp'),
    (r'\\min\b', 'min'), (r'\\max\b', 'max'),
    (r'\\inf\b', 'inf'), (r'\\sup\b', 'sup'),
    (r'\\lim\b', 'lim'), (r'\\liminf\b', 'lim inf'), (r'\\limsup\b', 'lim sup'),
    (r'\\gcd\b', 'gcd'), (r'\\det\b', 'det'), (r'\\dim\b', 'dim'),
    (r'\\Pr\b', 'Pr'),

    # --- Limits (subscript/superscript after operators) ---
    (r'\\limits\b', 'limits: #true'),
    (r'\\nolimits\b', 'limits: #false'),

    # --- Subscript / Superscript in math ---
    (r'\\sb\{([^}]*)\}', r'_\1'),   # \sb{X} → _X
    (r'\\sp\{([^}]*)\}', r'^\1'),   # \sp{X} → ^X

    # --- Matrix ---
    (r'\\begin\{pmatrix\}', r'mat('),
    (r'\\end\{pmatrix\}', r')'),
    (r'\\begin\{bmatrix\}', r'mat(delim: "[")'),
    (r'\\end\{bmatrix\}', r')'),
    (r'\\begin\{Bmatrix\}', r'mat(delim: "{")'),
    (r'\\end\{Bmatrix\}', r')'),
    (r'\\begin\{vmatrix\}', r'mat(delim: "|")'),
    (r'\\end\{vmatrix\}', r')'),
    (r'\\begin\{Vmatrix\}', r'mat(delim: "||")'),
    (r'\\end\{Vmatrix\}', r')'),
    (r'\\begin\{matrix\}', r'mat(delim: #none)'),
    (r'\\end\{matrix\}', r')'),

    # --- Cases ---
    (r'\\begin\{cases\}', r'cases('),
    (r'\\end\{cases\}', r')'),

    # --- Display math markers ---
    (r'\\displaystyle\b', ''),
    (r'\\textstyle\b', ''),
    (r'\\scriptstyle\b', ''),
    (r'\\scriptscriptstyle\b', ''),

    # --- Math spacing ---
    (r'\\quad\b', ' '), (r'\\qquad\b', '  '),
    (r'\\,', ' '), (r'\\:', ' '), (r'\\;', ' '),
]


# ======================================================================
# TypstAdapter
# ======================================================================

class TypstAdapter(ToolAdapter):
    """Typst ↔ LaTeX adapter with comprehensive rule coverage.

    Based on official Typst documentation (typst.app/docs/reference) and
    standard LaTeX commands.
    """

    tool_name = "Typst"
    old_tool_name = "LaTeX"
    old_extension = ".tex"
    new_extension = ".typ"

    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate LaTeX → Typst."""
        text = content

        # Pass 0: Protect math and verbatim
        text, math_ph = _protect_math(text)
        text, verb_ph = _protect_verbatim(text)

        # Pass 1: Structure rules
        text = _RuleEngine(LATEX_STRUCTURE, "structure").apply(text)

        # Pass 2: Inline formatting
        text = _RuleEngine(LATEX_INLINE, "inline").apply(text)

        # Pass 3: Math rules (inside protected math blocks)
        math_engine = _RuleEngine(LATEX_MATH, "math")
        for key, math_text in math_ph.items():
            math_ph[key] = math_engine.apply(math_text)

        # Pass 4: Restore
        text = _restore_all(text, math_ph, verb_ph)

        # Cleanup
        text = self._cleanup(text)

        # Auto-add heading numbering if refs used
        if re.search(r'@[a-zA-Z]', text):
            text = '#set heading(numbering: "1.")\n' + text

        return TranslationResult.ok(
            text.strip(),
            metadata={"method": "rules"},
        )

    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate Typst → LaTeX."""
        # (unchanged from original)
        text = content
        text, math_ph = _protect_math(text)
        text = _RuleEngine(TYPST_TO_LATEX_STRUCTURE, "typst_struct").apply(text)
        text = _RuleEngine(TYPST_TO_LATEX_INLINE, "typst_inline").apply(text)
        for key, math_text in math_ph.items():
            math_ph[key] = _RuleEngine(TYPST_TO_LATEX_MATH, "typst_math").apply(math_text)
        text = _restore_all(text, math_ph)
        text = self._cleanup(text)
        # Always wrap in document structure for standalone compilation
        if '\\documentclass' not in text:
            text = '\\documentclass{article}\n\\begin{document}\n\n' + text + '\n\n\\end{document}'
        return TranslationResult.ok(text.strip(), metadata={"method": "rules"})

    def validate_new(self, content: str) -> bool:
        return content.count('{') == content.count('}') and content.count('[') == content.count(']')

    def validate_old(self, content: str) -> bool:
        return bool(re.search(r'\\[a-zA-Z]+', content))

    def _cleanup(self, text: str) -> str:
        """Post-process translated text."""
        # Remove TeX comments
        text = re.sub(r'(?<!\\)%.*$', '', text, flags=re.MULTILINE)
        # Remove control characters
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        # Collapse blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)
        # Remove trailing whitespace
        text = '\n'.join(line.rstrip() for line in text.splitlines())
        # Remove stray backslashes on empty lines
        text = re.sub(r'^\\\\s*$', '', text, flags=re.MULTILINE)
        # Clean orphaned placeholder markers
        text = re.sub(r'«MATH\d+»', '', text)
        text = re.sub(r'«VB\d+»', '', text)
        return text.strip()

# ======================================================================
# Typst → LaTeX Rules (unchanged from original for now)
# ======================================================================

TYPST_TO_LATEX_STRUCTURE: list[tuple[str, str]] = [
    (r'^=====\s+(.+)$', r'\\subparagraph{\1}'),
    (r'^====\s+(.+)$', r'\\paragraph{\1}'),
    (r'^===\s+(.+)$', r'\\subsubsection{\1}'),
    (r'^==\s+(.+)$', r'\\subsection{\1}'),
    (r'^=\s+(.+)$', r'\\section{\1}'),
    (r'#strong\[([^\]]*)\]', r'\\textbf{\1}'),
    (r'#emph\[([^\]]*)\]', r'\\textit{\1}'),
    (r'#mono\[([^\]]*)\]', r'\\texttt{\1}'),
    (r'#underline\[([^\]]*)\]', r'\\underline{\1}'),
    (r'#smallcaps\[([^\]]*)\]', r'\\textsc{\1}'),
    (r'#footnote\[([^\]]*)\]', r'\\footnote{\1}'),
    (r'#link\("([^"]*)"\)\[([^\]]*)\]', r'\\href{\1}{\2}'),
    (r'#pagebreak\(\)', r'\\newpage'),
    (r'#h\(1em\)', r'\\indent'),
    (r'#image\("([^"]*)"\)', r'\\includegraphics{\1}'),
    (r'^\s*-\s+(.+)$', r'\\item \1'),
    (r'^\s*\+\s+(.+)$', r'\\item \1'),
    (r'<([a-zA-Z0-9_:-]+)>', r'\\label{\1}'),
]

TYPST_TO_LATEX_INLINE: list[tuple[str, str]] = [
    (r'\*\*([^*]+)\*\*', r'\\textbf{\1}'),
    (r'\*([^*\n]+)\*', r'\\textbf{\1}'),
    (r'_([^_\n]+)_', r'\\textit{\1}'),
    (r'`([^`\n]+)`', r'\\texttt{\1}'),
    (r'(?<!\w)@([a-zA-Z][a-zA-Z0-9_:-]*[a-zA-Z0-9])', r'\\cite{\1}'),
]

TYPST_TO_LATEX_MATH: list[tuple[str, str]] = [
    (r'\$\$\s*(.+?)\s*\$\$', r'\\[\1\\]'),
    (r'\$(.+?)\$', r'\\(\1\\)'),
    (r'frac\(([^,]+),\s*([^)]+)\)', r'\\frac{\1}{\2}'),
    (r'sqrt\(([^)]+)\)', r'\\sqrt{\1}'),
    (r'root\(([^,]+),\s*([^)]+)\)', r'\\sqrt[\2]{\1}'),
    (r'hat\(([^)]+)\)', r'\\hat{\1}'),
    (r'overline\(([^)]+)\)', r'\\bar{\1}'),
    (r'lr\(([^)]+)\)', r'\\left\1'),
]
