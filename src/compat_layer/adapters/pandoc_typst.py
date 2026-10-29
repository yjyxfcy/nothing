"""Pandoc AST → Typst translator.

Uses pandoc to parse LaTeX into its AST (JSON), then walks the AST
to generate Typst code. Pandoc handles ALL LaTeX nesting correctly,
fixing the "unclosed delimiter" problem at its root.

Requires: pandoc binary (installed at PROJECT/pandoc_local/pandoc-3.6.4/pandoc.exe)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PANDOC_BIN = Path(os.environ.get("ROSETTA_PANDOC", "") or shutil.which("pandoc") or "pandoc")


# ======================================================================
# Comprehensive LaTeX Math → Typst Math Translation Rules
# (shared with typst.py's LATEX_MATH, replicated here for self-containment)
# ======================================================================

MATH_TRANSLATION_RULES: list[tuple[str, str]] = [
    # --- Math font commands ---
    (r'\\mathbb\{([^}]*)\}', r'bb("\1")'),
    (r'\\mathcal\{([^}]*)\}', r'cal("\1")'),
    (r'\\mathscr\{([^}]*)\}', r'scr("\1")'),
    (r'\\mathfrak\{([^}]*)\}', r'frak("\1")'),
    (r'\\mathbf\{([^}]*)\}', r'bold(\1)'),
    (r'\\mathit\{([^}]*)\}', r'italic(\1)'),
    (r'\\mathrm\{([^}]*)\}', r'"\1"'),
    (r'\\mathsf\{([^}]*)\}', r'sans(\1)'),
    (r'\\mathtt\{([^}]*)\}', r'mono(\1)'),
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
    (r'\\sqrt\[([^\]]*)\]\{([^}]*)\}', r'root(\2, \1)'),
    (r'\\sqrt\{([^}]*)\}', r'sqrt(\1)'),
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
    (r'\\left\s*\[', 'lr('), (r'\\right\s*\]', ')'),
    (r'\\left\s*\(', 'lr('), (r'\\right\s*\)', ')'),
    (r'\\left\s*\{', 'lr('), (r'\\right\s*\}', ')'),
    (r'\\left\.', 'lr(.)'), (r'\\right\.', '.'),
    (r'\\left\s*\|', 'lr(|'), (r'\\right\s*\|', '|)'),
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
    (r'\\lim\b', 'lim'), (r'\\det\b', 'det'),
    (r'\\gcd\b', 'gcd'), (r'\\deg\b', 'deg'),
    (r'\\Pr\b', 'Pr'), (r'\\dim\b', 'dim'),
    (r'\\arg\b', 'arg'), (r'\\ker\b', 'ker'),
    # --- Missing symbols ---
    (r'\\ell\b', 'ell'), (r'\\circ\b', 'circle'), (r'\\bullet\b', 'bullet'),
    (r'\\dots\b', 'dots'), (r'\\cdots\b', 'dots.c'),
    (r'\\implies\b', '==>'), (r'\\iff\b', '<=>'),
    (r'\\cdot\b', 'dot.op'),
    # --- Spacing ---
    (r'\\,', ''), (r'\\;', ' '), (r'\\:', ' '), (r'\\!', ''),
    (r'\\\\', '\n'),  # line break in math
    (r'\\qquad\b', '  '), (r'\\quad\b', ' '),
    # --- Big delimiters (size modifiers — strip, Typst auto-sizes) ---
    (r'\\bigl\b', ''), (r'\\bigr\b', ''),
    (r'\\Bigl\b', ''), (r'\\Bigr\b', ''),
    (r'\\biggl\b', ''), (r'\\biggr\b', ''),
    (r'\\Biggl\b', ''), (r'\\Biggr\b', ''),
    # --- Subscript/superscript with braces (Typst uses _ and ^) ---
    # These remain as-is since both LaTeX and Typst use _{...} and ^{...}
    # --- \text and \mbox in math ---
    (r'\\text\{([^}]*)\}', r'"\1"'),
    (r'\\mbox\{([^}]*)\}', r'"\1"'),
    (r'\\text(rm|tt|sf|bf|it)\{([^}]*)\}', r'"\2"'),
    # --- phantom commands ---
    (r'\\hphantom\b', 'phantom'), (r'\\vphantom\b', 'phantom'),
    (r'\\phantom\b', 'phantom'),
    # --- \label inside math: strip it ---
    (r'\\operatorname\{([^}]*)\}', r'"\1"'),
    # --- \limits, \nolimits, \displaystyle, \textstyle ---
    (r'\\displaystyle\b', ''), (r'\\textstyle\b', ''),
    # --- Math environments: strip all begin/end markers ---
    # For aligned/gathered: just strip markers, leave content
    # For matrix/cases: convert to Typst equivalents is complex, just strip for now
    (r'\\begin\{[a-zA-Z]+\*?\}', ''),
    (r'\\end\{[a-zA-Z]+\*?\}', ''),
    # --- Ampersand (alignment tab in LaTeX math — not needed in Typst) ---
    (r'&', ' '),
    # --- \nonumber ---
    (r'\\nonumber\b', ''),
    # --- \limits / \nolimits ---
    (r'\\limits\b', ''), (r'\\nolimits\b', ''),
]

# Math font commands that require #import math
# (Use bare function names — in math mode, math.bb() doesn't work; need bb() after import)
_MATH_IMPORT_FUNCTIONS = ['bb("', 'cal("', 'scr("', 'frak("', 'bold(']


# ======================================================================
# Pandoc AST → Typst Generator
# ======================================================================

class PandocTypst:
    """Generate Typst from Pandoc JSON AST."""

    def __init__(self):
        self.in_paragraph = False
        self._list_depth = 0
        self._heading_numbering = False

    def generate(self, ast: dict) -> str:
        """Convert pandoc AST to Typst."""
        blocks = ast.get("blocks", [])
        parts = [self._block(b) for b in blocks]

        # Fallback: if no blocks, try to use title/abstract from metadata
        if not parts:
            meta = ast.get("meta", {})
            abstract = meta.get("abstract")
            if isinstance(abstract, dict) and "c" in abstract:
                parts = [self._block(b) for b in abstract["c"]]

        result = '\n\n'.join(p for p in parts if p)
        if self._heading_numbering:
            result = '#set heading(numbering: "1.")\n' + result
        # Auto-add math import if any math font commands are used
        if any(f in result for f in _MATH_IMPORT_FUNCTIONS):
            result = '#import math: bb, cal, scr, frak, bold\n' + result
        return result.strip()

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    def _block(self, b: dict) -> str:
        try:
            t = b.get("t", "")
            c = b.get("c", [])
            handlers = {
                "Header": self._header,
                "Para": self._para,
                "Plain": self._plain,
                "CodeBlock": self._code_block,
                "BlockQuote": self._block_quote,
                "BulletList": self._bullet_list,
                "OrderedList": self._ordered_list,
                "DefinitionList": self._def_list,
                "Table": self._table,
                "Figure": self._figure,
                "Div": self._div,
                "RawBlock": self._raw_block,
                "HorizontalRule": lambda *_: '\n#line(length: 100%)\n',
                "Null": lambda *_: '',
            }
            handler = handlers.get(t)
            if handler:
                return handler(c)
            # Unknown block type - skip
            return ''
        except Exception:
            return ''  # Skip any block that causes errors

    def _header(self, c: list) -> str:
        level = c[0]
        attrs = c[1]
        content = self._inlines(c[2])
        hashes = '=' * min(level, 5)
        if r'\ref' in str(c) or '@' in content:
            self._heading_numbering = True
        return f'\n{hashes} {content}'

    def _para(self, c: list) -> str:
        return self._inlines(c)

    def _plain(self, c: list) -> str:
        return self._inlines(c)

    def _code_block(self, c: list) -> str:
        attrs = c[0]
        code = c[1]
        lang = ""
        if attrs and len(attrs) >= 3 and attrs[2]:
            lang = attrs[2][0] if attrs[2] else ""
        if lang:
            return f'```{lang}\n{code}\n```'
        return f'```\n{code}\n```'

    def _block_quote(self, c: list) -> str:
        blocks = [self._block(b) for b in c]
        inner = '\n\n'.join(p for p in blocks if p)
        return f'#quote[\n{inner}\n]'

    def _bullet_list(self, c: list) -> str:
        self._list_depth += 1
        items = []
        for item in c:
            blocks = [self._block(b) for b in item]
            item_text = ' '.join(p for p in blocks if p)
            items.append(f'{"  " * (self._list_depth - 1)}- {item_text}')
        self._list_depth -= 1
        return '\n'.join(items)

    def _ordered_list(self, c: list) -> str:
        list_args = c[0]
        items_data = c[1]
        self._list_depth += 1
        items = []
        for item in items_data:
            blocks = [self._block(b) for b in item]
            item_text = ' '.join(p for p in blocks if p)
            items.append(f'{"  " * (self._list_depth - 1)}+ {item_text}')
        self._list_depth -= 1
        return '\n'.join(items)

    def _def_list(self, c: list) -> str:
        items = []
        for entry in c:
            term = self._inlines(entry[0])
            defs = []
            for d in entry[1]:
                blocks = [self._block(b) for b in d]
                defs.append(' '.join(p for p in blocks if p))
            items.append(f'- *{term}*: {" ".join(defs)}')
        return '\n'.join(items)

    def _table(self, c: list) -> str:
        """Pandoc Table: [Caption, ColSpecs, TableHead, [TableBody], TableFoot, [Attr]].

        Caption = [ShortCaption | null, [Inline]] (block-level caption)
        TableHead = [Attr, [Row]] where Row = [Attr, [Cell]]
        TableBody = [Attr, RowHeadColumns, [], [Row]]
        Cell = [Attr, Alignment, RowSpan, ColSpan, [Block]]
        """
        # Pandoc Table c = [ShortCaption, LongCaption, ColSpecs, TableHead, [TableBody], TableFoot]
        # ShortCaption = [str, [], []] (3-element attr-like)
        # LongCaption = [null | InlineList, [Block]] (or sometimes [null, [Block]])
        # TableHead = [HeadAttrs, [Row]]
        # TableBody = [BodyAttrs, RowHeadColumns, [], [Row]]
        caption_text = ""
        if len(c) >= 2 and isinstance(c[1], list) and len(c[1]) >= 2:
            long_cap = c[1]  # [null|inlines, [blocks]]
            if isinstance(long_cap[1], list) and long_cap[1]:
                cap_parts = [self._block(b) for b in long_cap[1] if isinstance(b, dict)]
                caption_text = ' '.join(p for p in cap_parts if p)

        colspecs = c[2] if len(c) > 2 else []
        head = c[3] if len(c) > 3 else None
        bodies = c[4] if len(c) > 4 else []

        ncols = len(colspecs) if colspecs else 2
        all_rows = []

        def extract_cells(row_list):
            """Extract text from table row cells.
            Row = [Attrs, [Cell]], Cell = [Attrs, Align, RowSpan, ColSpan, [Block]]
            """
            for row in row_list:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                cells = row[1]  # row[0] = attrs, row[1] = cells list
                if not isinstance(cells, list):
                    continue
                for cell_data in cells:
                    if isinstance(cell_data, list) and len(cell_data) >= 5:
                        blocks = cell_data[4]
                        cell_text = ' '.join(
                            self._block(b) for b in blocks if isinstance(b, dict)
                        )
                        all_rows.append(cell_text)

        # Extract header (head[0] = attrs, head[1] = row list)
        if head and isinstance(head, list) and len(head) >= 2:
            row_list = head[1]
            if isinstance(row_list, list):
                extract_cells(row_list)

        # Extract bodies (TableBody = [BodyAttrs, RowHeadColumns, [], [Row]])
        if isinstance(bodies, list):
            for body in bodies:
                if isinstance(body, list) and len(body) >= 4:
                    row_list = body[3]  # body[0]=attrs, body[1]=rowheadcols, body[2]=[], body[3]=rows
                    if isinstance(row_list, list):
                        extract_cells(row_list)

        cells_str = ',\n    '.join(f'[{c}]' for c in all_rows if c)
        cap = f'  caption: [{caption_text}],\n' if caption_text else ''
        return f'#figure(\n{cap}  table(\n    columns: {ncols},\n    {cells_str},\n  ),\n)'

    def _figure(self, c: list) -> str:
        # Pandoc figure: [attrs, caption, [blocks]]
        caption_text = ""
        caption_data = c[1] if len(c) > 1 else None
        if isinstance(caption_data, list) and len(caption_data) >= 2:
            long_cap = caption_data[1]
            if isinstance(long_cap, list) and long_cap:
                cap_parts = [self._block(b) for b in long_cap if isinstance(b, dict)]
                caption_text = ' '.join(p for p in cap_parts if p)

        blocks = [self._block(b) for b in c[2]] if len(c) > 2 else []
        inner = '\n'.join(p for p in blocks if p)

        if inner.strip():
            cap = f'  caption: [{caption_text}],\n' if caption_text else ''
            return f'#figure(\n{cap}  {inner},\n)'
        elif caption_text:
            # Figure with caption only — use empty body placeholder
            return f'#figure(caption: [{caption_text}])[]\n'
        else:
            return ''

    def _div(self, c: list) -> str:
        blocks = [self._block(b) for b in c[1]]
        return '\n'.join(p for p in blocks if p)

    def _raw_block(self, c: list) -> str:
        fmt = c[0]
        text = c[1]
        if fmt in ("latex", "context"):
            return ""  # Skip raw LaTeX
        # Escape # for Typst
        text = text.replace('#', '\\#')
        return text

    # ------------------------------------------------------------------
    # Inlines
    # ------------------------------------------------------------------

    def _inlines(self, inlines: list) -> str:
        result = []
        for il in inlines:
            if isinstance(il, str):
                result.append(il)
            elif isinstance(il, dict):
                result.append(self._inline(il))
            else:
                result.append(str(il))
        return ''.join(result)

    def _inline(self, il: dict) -> str:
        t = il.get("t", "")
        c = il.get("c", [])

        handlers = {
            "Str": lambda c: (str(c)
                .replace('#', '\\#')
                .replace('<', '\\<')
                .replace('>', '\\>')
                # Escape @ that appears in text (NOT at word start — citations
                # from pandoc use the Cite element, not Str). @ preceded by
                # a letter is an email/username@domain pattern.
            ),
            "Space": lambda *_: ' ',
            "SoftBreak": lambda *_: ' ',
            "LineBreak": lambda *_: '\n',
            "Emph": lambda c: f'_{self._inlines(c)}_',
            "Strong": lambda c: f'*{self._inlines(c)}*',
            "Underline": lambda c: f'#underline[{self._inlines(c)}]',
            "Strikeout": lambda c: f'#strike[{self._inlines(c)}]',
            "SmallCaps": lambda c: f'#smallcaps[{self._inlines(c)}]',
            "Superscript": lambda c: f'#super[{self._inlines(c)}]',
            "Subscript": lambda c: f'#sub[{self._inlines(c)}]',
            "Code": lambda c: f'`{c[1]}`',
            "Quoted": self._quoted,
            "Cite": self._cite,
            "Link": lambda c: self._link(c),
            "Image": lambda c: self._image(c),
            "Math": self._math,
            "RawInline": lambda c: '' if c[0] in ("latex", "context") else str(c[1]).replace('#', '\\#'),
            "Note": lambda c: f'#footnote[{self._blocks_to_text(c)}]',
            "Span": lambda c: self._inlines(c[1]),
        }

        handler = handlers.get(t, lambda *_: '')
        return handler(c)

    def _quoted(self, c: list) -> str:
        qtype = c[0].get("t", "") if c[0] else ""
        content = self._inlines(c[1])
        if qtype == "DoubleQuote":
            return f'"{content}"'
        return f"'{content}'"

    def _cite(self, c: list) -> str:
        citations = c[0]
        keys = [cit["citationId"] for cit in citations]
        return '@' + ', @'.join(keys)

    def _link(self, c: list) -> str:
        attrs = c[0]
        content = self._inlines(c[1])
        target = attrs[0] if attrs else ""
        if not target:
            return content  # Link without URL → just show text
        return f'#link("{target}")[{content}]'

    def _image(self, c: list) -> str:
        attrs = c[0]
        alt = self._inlines(c[1])
        src = attrs[0] if attrs else ""
        if not src:
            return f'[{alt}]'  # Image without src → show alt text
        return f'#image("{src}", alt: "{alt}")'

    @staticmethod
    def _match_braces(text: str, start: int) -> str:
        """Extract {balanced braces} starting at text[start]. Returns content inside braces."""
        if start >= len(text) or text[start] != '{':
            return ""
        depth = 1
        i = start + 1
        while i < len(text) and depth > 0:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        return text[start + 1:i - 1] if depth == 0 else ""

    def _math(self, c: list) -> str:
        mtype = c[0].get("t", "InlineMath")
        tex = c[1]

        # ---- Phase 0: Bracket-aware translation for nested braces ----

        # Pre-process common functions/symbols (before regex rules)
        for cmd in ['sin', 'cos', 'tan', 'log', 'ln', 'exp', 'Pr', 'det', 'dim',
                     'gcd', 'deg', 'ker', 'arg', 'inf', 'sup', 'lim', 'max', 'min',
                     'sigma', 'Sigma', 'alpha', 'beta', 'gamma', 'delta', 'epsilon',
                     'lambda', 'mu', 'nu', 'omega', 'Omega', 'phi', 'pi', 'Pi',
                     'theta', 'Theta', 'rho', 'tau', 'xi', 'Xi', 'zeta', 'eta']:
            tex = re.sub(r'\\' + cmd + r'(?![a-zA-Z])', cmd, tex)
        # \exp\left(X\right) → exp(X)  (lr is redundant inside exp)
        # Handle this with a loop since \right) needs to match nested content
        i = 0
        while i < len(tex):
            m = re.search(r'\\exp(?:\\!|\\,|\\;)?\s*\\left\s*\(', tex[i:])
            if not m:
                break
            pos = i + m.start()
            # Find matching \right)
            after_lr = tex[m.end():]  # text after \left(
            # Find matching \right) — search for \right) with balanced braces
            depth = 0
            right_pos = -1
            j = 0
            while j < len(after_lr):
                ch = after_lr[j]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                elif depth == 0 and after_lr[j:j+7] == '\\right)':
                    right_pos = j
                    break
                j += 1
            if right_pos < 0:
                i = pos + 1
                continue
            # Extract content between \left( and \right)
            inner = after_lr[:right_pos]
            full_span = (m.end() - pos) + right_pos + 7  # \exp\left( + inner + \right)
            replacement = f'exp({inner})'
            tex = tex[:pos] + replacement + tex[pos + full_span:]
            i = pos + len(replacement)
        # Handle remaining \exp (not followed by \left)
        tex = re.sub(r'\\exp\b', 'exp', tex)
        tex = re.sub(r'\\left\s*\(', 'lr(', tex)
        tex = re.sub(r'\\left\s*\[', 'lr([', tex)
        tex = re.sub(r'\\right\s*\]', '])', tex)
        tex = re.sub(r'\\left\s*\|', 'lr(|', tex)
        tex = re.sub(r'\\right\s*\|', '|)', tex)
        tex = re.sub(r'\\left\s*\{', 'lr({', tex)
        tex = re.sub(r'\\right\s*\}', '})', tex)
        i = 0
        while i < len(tex):
            m = re.search(r'\\(?:frac|dfrac|tfrac|binom)\{', tex[i:])
            if not m:
                break
            pos = i + m.start()       # position of \ in \frac{
            cmd_len = m.end() - m.start()  # length of \frac{ or \dfrac{ or \binom{
            after = tex[pos + cmd_len - 1:]  # text starting at { (skip \frac etc.)
            num = self._match_braces(after, 0)
            if not num:
                i = pos + 1
                continue
            num_span = len(num) + 2    # {num}
            denom = self._match_braces(after, num_span)
            if not denom:
                i = pos + 1
                continue
            denom_span = len(denom) + 2  # {denom}
            full_span = (cmd_len - 1) + num_span + denom_span
            # \binom → binom, \dfrac → frac, \tfrac → frac, \frac → frac
            fn_name = 'binom' if 'binom' in m.group() else 'frac'
            replacement = f'{fn_name}({num}, {denom})'
            tex = tex[:pos] + replacement + tex[pos + full_span:]
            i = pos + len(replacement)

        # \sqrt[n]{x} or \sqrt{x} — same approach
        i = 0
        while i < len(tex):
            m = re.search(r'\\sqrt(\[|\{)', tex[i:])
            if not m:
                break
            pos = i + m.start()
            after = tex[pos + 5:]       # text starting at [ or { (skip \sqrt = 5 chars)
            if after.startswith('['):
                bracket_end = after.find(']')
                n = after[1:bracket_end]
                body = self._match_braces(after, bracket_end + 1)
                if not body:
                    i = pos + 1
                    continue
                full_span = 5 + bracket_end + 1 + len(body) + 2
                replacement = f'root({body}, {n})'
            else:
                body = self._match_braces(after, 0)
                if not body:
                    i = pos + 1
                    continue
                full_span = 5 + len(body) + 2
                replacement = f'sqrt({body})'
            tex = tex[:pos] + replacement + tex[pos + full_span:]
            i = pos + len(replacement)

        # ---- Phase 1: Apply ALL regex translation rules ----
        for pattern, replacement in MATH_TRANSLATION_RULES:
            try:
                # Fix: \b doesn't match before _ or ^ (both are word chars in regex).
                # Replace ONLY trailing \b (preceded by a letter) with (?![a-zA-Z]).
                # Must NOT replace \b that is part of the LaTeX command prefix!
                fixed_pattern = re.sub(
                    r'([a-zA-Z])\\b', r'\1(?![a-zA-Z])', pattern
                )
                tex = re.sub(fixed_pattern, replacement, tex, flags=re.MULTILINE)
            except re.error:
                pass

        # Safety-net: fix concatenations formed during Phase 1
        tex = tex.replace('explr(', 'exp(lr(')
        tex = tex.replace('Prlr(', 'Pr lr(')

        if mtype == "DisplayMath":
            return f'$ {tex} $'
        return f'${tex}$'

    def _blocks_to_text(self, blocks: list) -> str:
        return ' '.join(self._block(b) for b in blocks if b)


# ======================================================================
# Main API
# ======================================================================

def pandoc_latex_to_typst(content: str) -> str:
    """Translate LaTeX → Typst using pandoc AST.

    Args:
        content: LaTeX source text

    Returns:
        Typst source text, or empty string on failure
    """
    if not PANDOC_BIN.exists():
        raise FileNotFoundError(f"Pandoc not found at {PANDOC_BIN}")

    try:
        r = subprocess.run(
            [str(PANDOC_BIN), "-f", "latex", "-t", "json"],
            input=content,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            stderr = r.stderr[:500]
            # If pandoc fails, fall back to v2 regex
            if "Error" in stderr or "error" in stderr:
                from compat_layer.adapters.typst import TypstAdapter
                adapter = TypstAdapter()
                result = adapter.old_to_new(content)
                return result.content if result.success else ""
            return ""

        ast = json.loads(r.stdout)
        generator = PandocTypst()
        result = generator.generate(ast)

        # =================================================================
        # Comprehensive post-generation cleanup
        # =================================================================

        # Merge consecutive blank lines
        result = re.sub(r'\n{3,}', '\n\n', result)

        # Strip LaTeX environment residues (begin/end outside math)
        result = re.sub(r'\\begin\{[a-zA-Z]+\*?\}\s*', '', result)
        result = re.sub(r'\\end\{[a-zA-Z]+\*?\}\s*', '', result)

        # Strip common LaTeX commands that pandoc leaves as text
        result = re.sub(r'\\label\{[^}]*\}', '', result)
        result = re.sub(r'\\ref\{[^}]*\}', '???', result)  # unresolved ref
        result = re.sub(r'\\cite\{[^}]*\}', '@???', result)

        # Strip LaTeX spacing/sizing commands
        result = re.sub(r'\\(small|med|big)skip\b', '', result)
        result = re.sub(r'\\centering\b', '', result)
        result = re.sub(r'\\ragged(right|left)\b', '', result)
        result = re.sub(r'\\noindent\b', '', result)
        result = re.sub(r'\\indent\b', '', result)
        result = re.sub(r'\\newpage\b', '', result)
        result = re.sub(r'\\pagebreak\b', '', result)
        result = re.sub(r'\\clearpage\b', '', result)
        result = re.sub(r'\\linebreak\b', '', result)

        # Apply light cleanup only — pandoc output is already clean
        lines = result.split('\n')
        cleaned_lines = []
        for line in lines:
            # Only strip orphaned LaTeX residues (2-3 letter commands that
            # appear at word boundaries). Be conservative — pandoc output
            # shouldn't have many residues.
            if line.count('$') <= 2:
                # Only strip very short LaTeX commands that are clearly orphaned
                # (\nt, \dt, \rm, \bf, \it, \sl, \sc, \tt, \aa, \ss, \oe, \OE, etc.)
                line = re.sub(r'(?<!\\)\\(nt|dt|rm|bf|it|sl|sc|tt|aa|ss|oe|OE)\b', '', line)
            cleaned_lines.append(line)
        result = '\n'.join(cleaned_lines)

        # Remove dangling fragments from orphaned commands
        result = re.sub(r'(?<!\\)\babel\b', '', result)
        result = re.sub(r'(?<!\\)\bnt\b', '', result)

        # Fix: escape any `@` that appears as plain text (not in math)
        # Typst treats @ as citation prefix
        # (already handled by pandoc → @key for actual citations)

        # Remove stray backslash-command fragments
        result = re.sub(r'\\[a-z]+\s*\{\s*\}', '', result)  # empty commands like \mbox{}
        result = re.sub(r'\\(small|tiny|footnotesize|large|Large|LARGE|huge|Huge)\b', '', result)

        return result.strip()

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return ""
