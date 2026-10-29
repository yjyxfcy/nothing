"""EDA compat adapter: SystemVerilog (human-facing) <-> Verilog (agent-facing).

Projection is driven by the external sv2v converter plus a thin post-pass;
the upgrade direction (Verilog→SV) is mechanical idiom rules, because no
mature Verilog→SV tool exists (SV ⊇ Verilog; the "upgrade" of a correct
flat Verilog module into modern SV idioms is the adapter's own light logic).

  old_to_new (Verilog -> SV): upgrade the agent's `.v` back into the human's
      SV project -- mechanical idioms (`always @(posedge)` -> `always_ff`,
      `always @*` -> `always_comb`, `reg` -> `logic`). Interface re-wrapping
      (flattened bus ports -> `interface` port) is task-schema specific and
      not reconstructed by the generic adapter.

  new_to_old (SV -> Verilog): project the SV project into the agent's shadow
      by shelling out to **sv2v** -- a real plugin, exactly as the Typst
      adapter shells out to Tylax. sv2v faithfully lowers always_ff/always_comb,
      logic, typedef enum/struct, generate, parameters, and -- given a flat
      top that instantiates them -- interfaces. An interface-port *module* with
      no flat top cannot be flattened (sv2v emits nothing), so this adapter
      refuses that projection.

"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from bisect import bisect_left
from pathlib import Path
from typing import Optional

from ..interface import ToolAdapter, TranslationResult

SV2V_BIN = Path(os.environ.get("ROSETTA_SV2V", "") or shutil.which("sv2v") or "sv2v")

# ----------------------------------------------------------------------
# Upgrade rules (Verilog -> SystemVerilog). No mature plugin does this
# direction; these are the mechanical, semantics-preserving idiom lifts.
# ----------------------------------------------------------------------

_UPGRADE_RULES = [
    # always @(posedge/negedge clk [, ...]) -> always_ff
    (re.compile(r"\balways\s+@\s*\(\s*(posedge|negedge)\s+"), r"always_ff @(\1 "),
    # always @( * ) / always @* / always @( * )  -> always_comb (paren form first)
    (re.compile(r"\balways\s+@\s*\(\s*\*\s*\)"), "always_comb"),
    (re.compile(r"\balways\s+@\*\s*"), "always_comb "),
    # type keywords: reg -> logic; wire -> logic in port directions
    (re.compile(r"\boutput\s+wire\b"), "output logic"),
    (re.compile(r"\binput\s+wire\b"), "input logic"),
    (re.compile(r"\breg\b"), "logic"),
]

_VERILOG_OPAQUE = re.compile(
    r'//[^\r\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\\[^\s]+', re.S
)
_SV_DIRECTIVE_LINE = re.compile(r"^[ \t]*`[A-Za-z_][A-Za-z0-9_$]*[^\r\n]*$")
_SV_MACRO_NAME = re.compile(r"^[ \t]*`define[ \t]+([A-Za-z_][A-Za-z0-9_$]*)(.*)$")
_SV_NUMERIC_MACRO_VALUE = re.compile(
    r"[+-]?(?:\d[\d_]*|'[01xXzZ]|\d[\d_]*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+)\Z"
)
_SV_MACRO_PACKAGE_PREFIX = "__ROSETTA_MACRO_VALUES_"
_SV_MACRO_ALIAS_PREFIX = "__ROSETTA_MACRO_"
_SV_COMMENT_PREFIX = "__ROSETTA_COMMENT_"
_SV_COMMENT_ATTRIBUTE = re.compile(
    r"\(\*\s*(__ROSETTA_COMMENT_[0-9a-f]{10}_\d+)\s*=\s*"
    r'"[A-Za-z0-9+/=]*"\s*\*\)'
)


def _mask_sv_comments_and_strings(source: str) -> str:
    """Blank comments/strings without changing offsets or line boundaries."""
    chars = list(source)
    for match in _VERILOG_OPAQUE.finditer(source):
        token = match.group(0)
        if not token.startswith(("//", "/*", '"')):
            continue
        for index in range(match.start(), match.end()):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)


def _prepare_sv2v_projection_source(
    source: str,
) -> tuple[
    str, dict[str, str], list[tuple[int, str, str]],
    list[tuple[int, str, str]], dict[str, int],
]:
    """Keep simple numeric defines symbolic while letting sv2v lower the design.

    sv2v expands preprocessor macros and drops some compilation-unit
    directives. For simple numeric macros, route the temporary definition
    through a synthetic package constant. sv2v lowers package references into
    localparams; those declarations can then be removed and their use sites
    restored as Verilog macro references. Leading directives that sv2v drops
    are likewise restored. Constructs whose scope cannot safely be preserved
    are refused instead of being flattened silently.
    """
    masked = _mask_sv_comments_and_strings(source)
    masked_lines = masked.splitlines(keepends=True)
    source_lines = source.splitlines(keepends=True)

    first_design_unit_line = next(
        (i for i, line in enumerate(masked_lines)
         if not re.match(r"^[ \t]*`", line)
         and re.search(r"\b(module|interface|package|program|primitive|checker)\b", line)),
        len(masked_lines),
    )
    package_match = re.search(
        r"(?m)^[ \t]*package[ \t]+(?:(?:automatic|static)[ \t]+)?"
        r"([A-Za-z_][A-Za-z0-9_$]*)[ \t]*;",
        masked,
    )
    first_module_line = next(
        (i for i, line in enumerate(masked_lines)
         if not re.match(r"^[ \t]*`", line) and re.search(r"\bmodule\b", line)),
        len(masked_lines),
    )
    first_module_offset = sum(len(line) for line in masked_lines[:first_module_line])
    use_existing_package = bool(
        package_match and (first_module_line == len(masked_lines)
                           or package_match.start() < first_module_offset)
    )
    macro_package_name = (package_match.group(1) if use_existing_package
                          else f"{_SV_MACRO_PACKAGE_PREFIX}"
                          f"{hashlib.sha256(source.encode('utf-8')).hexdigest()[:10]}")
    directives: list[tuple[int, str, str]] = []
    for index, (code_line, original_line) in enumerate(zip(masked_lines, source_lines)):
        stripped = code_line.rstrip("\r\n")
        match = re.match(r"^[ \t]*`([A-Za-z_][A-Za-z0-9_$]*)\b", stripped)
        if match:
            directives.append((index, match.group(1), original_line.rstrip("\r\n")))

    unsupported = {"ifdef", "ifndef", "elsif", "else", "endif", "undef"}
    conditional = [(i + 1, name) for i, name, _ in directives if name in unsupported]
    if conditional:
        line, name = conditional[0]
        raise ConversionError(
            f"conditional preprocessor directive `{name} on line {line} can change "
            "the active design; this single-file projection cannot preserve all branches"
        )

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    prepared_lines = list(source_lines)
    macro_restore: dict[str, str] = {}
    macro_expected_uses: dict[str, int] = {}
    macro_alias_declarations: list[str] = []
    injected_directives: list[tuple[int, str, str]] = []
    seen_macros: set[str] = set()
    for index, name, original_line in directives:
        if name == "include":
            # The caller emits the more specific project-aware diagnostic.
            continue
        if name == "define":
            if index >= first_design_unit_line:
                raise ConversionError(
                    f"`define on line {index + 1} appears after a module starts; "
                    "moving it into the projected preamble could change earlier modules"
                )
            code_line = masked_lines[index].rstrip("\r\n")
            name_match = _SV_MACRO_NAME.match(code_line)
            original_match = _SV_MACRO_NAME.match(original_line)
            if not name_match or not original_match:
                raise ConversionError(f"cannot safely parse `define on line {index + 1}")
            macro_name = name_match.group(1)
            masked_tail = name_match.group(2)
            if masked_tail.startswith("("):
                raise ConversionError(
                    f"function-like macro `{macro_name} on line {index + 1} is not "
                    "safe to flatten; use a simple numeric object-like `define"
                )
            if macro_name in seen_macros:
                raise ConversionError(
                    f"macro `{macro_name} is defined more than once; conditional or "
                    "redefinition behavior is not safe to reconstruct"
                )
            seen_macros.add(macro_name)
            leading = len(masked_tail) - len(masked_tail.lstrip())
            value = masked_tail[leading:].rstrip()
            if not value or not _SV_NUMERIC_MACRO_VALUE.fullmatch(value):
                raise ConversionError(
                    f"`define {macro_name} on line {index + 1} is not a supported "
                    "single numeric literal"
                )
            identifiers = [token for token in _SV_TOKEN.findall(source)
                           if not token.startswith(("//", "/*", '"'))]
            if any(token.startswith(_SV_MACRO_PACKAGE_PREFIX)
                   or token.startswith(_SV_MACRO_ALIAS_PREFIX) for token in identifiers):
                raise ConversionError("source uses a reserved Rosetta SV macro identifier prefix")
            alias = f"{_SV_MACRO_ALIAS_PREFIX}{digest}_{len(macro_restore)}"
            package = macro_package_name
            alias_type = _sv_macro_literal_type(value)
            if alias_type is None:
                raise ConversionError(
                    f"`define {macro_name} on line {index + 1} has a context-sized "
                    "literal; only unsized decimal or sized numeric literals are supported"
                )
            macro_alias_declarations.append(
                f"  localparam {alias_type} {alias} = {value};"
            )
            # Use the same character offsets as the masked line.  Keep any
            # trailing comment and whitespace from the authored directive.
            original_noeol = original_line.rstrip("\r\n")
            value_start = name_match.start(2) + leading
            value_end = value_start + len(value)
            prepared_lines[index] = (original_noeol[:value_start] + f"{package}::{alias}"
                                     + original_noeol[value_end:]
                                     + source_lines[index][len(original_noeol):])
            flattened_alias = f"{package}_{alias}"
            macro_restore[flattened_alias] = f"`{macro_name}"
            macro_expected_uses[flattened_alias] = _count_sv_macro_uses(source, macro_name)
            injected_directives.append((index, name, original_line))
            continue

        # These compilation-unit directives can be moved only from the
        # preamble. If the converter erases one later in a design, the caller
        # must refuse rather than changing its scope.
        if index < first_design_unit_line and name in {
            "timescale", "default_nettype", "celldefine", "endcelldefine",
            "unconnected_drive", "nounconnected_drive", "resetall",
            "begin_keywords", "end_keywords", "pragma", "line",
        }:
            injected_directives.append((index, name, original_line))

    if macro_alias_declarations:
        if use_existing_package:
            package = macro_package_name
            prepared = "".join(prepared_lines)
            prepared_package = re.search(
                rf"(?m)^[ \t]*package[ \t]+(?:(?:automatic|static)[ \t]+)?"
                rf"{re.escape(package)}[ \t]*;",
                prepared,
            )
            if not prepared_package:
                raise ConversionError("could not locate the source package for symbolic macros")
            declarations = "\n" + "\n".join(macro_alias_declarations) + "\n"
            prepared = prepared[:prepared_package.end()] + declarations + prepared[prepared_package.end():]
            prepared_lines = [prepared]
        else:
            package = macro_package_name
            alias_package = (f"package {package};\n"
                            + "\n".join(macro_alias_declarations)
                            + "\nendpackage\n")
            prepared_lines.insert(first_design_unit_line, alias_package)
    else:
        package = ""
    return ("".join(prepared_lines), macro_restore, injected_directives,
            directives, macro_expected_uses)


def _directive_key(line: str) -> str:
    parts: list[str] = []
    last = 0
    for match in _VERILOG_OPAQUE.finditer(line):
        token = match.group(0)
        if token.startswith(("//", "/*")):
            parts.extend((line[last:match.start()], " "))
            last = match.end()
    parts.append(line[last:])
    cleaned = "".join(parts)
    return " ".join(cleaned.strip().split())


def _sv_macro_literal_type(value: str) -> str | None:
    sized = re.fullmatch(r"(\d+)'([sS]?)([bBoOdDhH])[0-9a-fA-F_xXzZ?]+", value)
    if sized:
        width = int(sized.group(1))
        if width < 1:
            return None
        signed = "signed " if sized.group(2) else ""
        return f"logic {signed}[{width - 1}:0]"
    if re.fullmatch(r"[+-]?\d[\d_]*", value):
        number = int(value.replace("_", ""))
        if -(2**31) <= number < 2**31:
            return "integer"
    return None


def _count_sv_macro_uses(source: str, macro_name: str) -> int:
    masked_lines = _mask_sv_comments_and_strings(source).splitlines()
    count = 0
    use = re.compile(rf"`{re.escape(macro_name)}\b")
    for line in masked_lines:
        if re.match(r"^[ \t]*`define\b", line):
            continue
        count += len(use.findall(line))
    return count


def _restore_sv_macro_aliases(
    source: str, aliases: dict[str, str], expected_uses: dict[str, int],
) -> str:
    output = source
    for alias, macro_ref in aliases.items():
        escaped = re.escape(alias)
        declaration = re.compile(
            rf"(?m)^[ \t]*localparam\b[^\r\n]*\b{escaped}\s*="
            rf"[^\r\n]*;[ \t]*(?:\r?\n|$)"
        )
        declarations = declaration.findall(output)
        expected = expected_uses.get(alias, 0)
        if expected and not declarations:
            raise ConversionError(
                f"sv2v folded or dropped the symbolic macro {macro_ref}; "
                "its use sites cannot be restored safely"
            )
        output = declaration.sub("", output)
        actual = len(re.findall(rf"\b{escaped}\b", _mask_sv_comments_and_strings(output)))
        if actual != expected:
            raise ConversionError(
                f"sv2v changed the number of {macro_ref} use sites "
                f"({expected} source, {actual} projected); refusing a non-equivalent view"
            )
        output = _replace_sv_identifiers(output, {alias: macro_ref})
    return output


def _directive_occurrences(source: str) -> list[tuple[int, str, str]]:
    masked_lines = _mask_sv_comments_and_strings(source).splitlines(keepends=True)
    source_lines = source.splitlines(keepends=True)
    result: list[tuple[int, str, str]] = []
    for index, (masked, original) in enumerate(zip(masked_lines, source_lines)):
        if re.match(r"^[ \t]*`[A-Za-z_][A-Za-z0-9_$]*\b", masked.rstrip("\r\n")):
            raw = original.rstrip("\r\n")
            result.append((index, raw, _directive_key(raw)))
    return result


def _encode_sv_comments(source: str) -> tuple[str, dict[str, str], int]:
    """Carry comments through sv2v using inert, removable attributes.

    Attributes stay adjacent to the source line in the common case. Comments
    that would otherwise attach to an ``end*`` token or EOF are banked before
    the first design unit, where they can still be transported safely.
    """
    tokens = [m.group(0) for m in _SV_TOKEN.finditer(source)
              if not m.group(0).startswith(("//", "/*", '"'))]
    if any(token.startswith(_SV_COMMENT_PREFIX) for token in tokens):
        raise ConversionError("source uses the reserved Rosetta comment marker prefix")
    masked = _mask_sv_comments_and_strings(source)
    code_positions = [match.start() for match in re.finditer(r"\S+", masked)]
    closing = re.compile(
        r"(?:endmodule|endinterface|endpackage|endprogram|endprimitive|"
        r"endchecker|endfunction|endtask|endclass|endcase|endgenerate|"
        r"endclocking|endproperty|endsequence|end|else|end\b)"
    )
    package = re.compile(r"package\b")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    replacements: list[tuple[int, int, str]] = []
    insertions: dict[int, list[str]] = {}
    restore: dict[str, str] = {}
    relocated = 0
    directive_line_starts: set[int] = set()
    first_design_unit_offset = len(source)
    first_module_offset = len(source)
    first_package_offset = len(source)
    offset = 0
    for line in masked.splitlines(keepends=True):
        if re.match(r"^[ \t]*`[A-Za-z_][A-Za-z0-9_$]*\b", line.rstrip("\r\n")):
            directive_line_starts.add(offset)
        else:
            if first_design_unit_offset == len(source) and re.search(
                r"\b(module|interface|package|program|primitive|checker)\b", line
            ):
                first_design_unit_offset = offset
            if first_module_offset == len(source) and re.search(r"\bmodule\b", line):
                first_module_offset = offset
            if first_package_offset == len(source) and re.search(r"\bpackage\b", line):
                first_package_offset = offset
        offset += len(line)

    for match in _VERILOG_OPAQUE.finditer(source):
        comment = match.group(0)
        if not comment.startswith(("//", "/*")):
            continue
        line_start = max(source.rfind("\n", 0, match.start()),
                         source.rfind("\r", 0, match.start())) + 1
        if line_start in directive_line_starts:
            continue
        name = f"{_SV_COMMENT_PREFIX}{digest}_{len(restore)}"
        encoded = base64.b64encode(comment.encode("utf-8")).decode("ascii")
        attribute = f'(* {name} = "{encoded}" *)'
        restore[name] = comment
        masked_prefix = masked[line_start:match.start()]
        newline_positions = [p for p in (masked.find("\n", line_start),
                                          masked.find("\r", line_start)) if p >= 0]
        line_end = min(newline_positions) if newline_positions else len(masked)
        line_prefix = masked[line_start:line_end]
        first_token = re.sub(r"^\s*", "", line_prefix)
        next_index = bisect_left(code_positions, match.end())
        next_offset = code_positions[next_index] if next_index < len(code_positions) else len(masked)
        unsafe_anchor = (next_offset == len(masked) or bool(closing.match(masked, next_offset))
                         or bool(package.match(masked, next_offset))
                         or bool(closing.match(first_token))
                         or bool(package.match(first_token))
                         or (first_package_offset < first_module_offset
                             and match.start() < first_package_offset))
        indent_len = len(line_prefix) - len(line_prefix.lstrip(" \t"))
        if unsafe_anchor:
            anchor = (first_module_offset if first_module_offset < len(source)
                      else first_design_unit_offset)
        elif masked_prefix.strip():
            # A trailing/inline comment is moved to the beginning of its line
            # so the attribute attaches to that line's first construct.
            anchor = line_start + indent_len
        else:
            anchor = match.start()
        if anchor != match.start():
            relocated += 1
        insertions.setdefault(anchor, []).append(attribute)
        blanked = "".join("\n" if c == "\n" else "\r" if c == "\r" else " "
                           for c in comment)
        replacements.append((match.start(), match.end(), blanked))
    for position, attributes in insertions.items():
        # Add a line boundary around banked attributes and around attributes
        # inserted at the start of a source line. The latter remain valid
        # prefix attributes after sv2v reformats the design.
        joined = "\n".join(attributes) + "\n"
        replacements.append((position, position, joined))
    # Insertions precede a replacement at the same offset; consume the original
    # text once instead of copying the whole source for every comment.
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements, key=lambda item: (item[0], item[1])):
        if start < cursor:
            raise ConversionError("overlapping SystemVerilog comment transport markers")
        output.extend((source[cursor:start], replacement))
        cursor = end
    output.append(source[cursor:])
    return "".join(output), restore, relocated


def _restore_sv_comments(source: str, comments: dict[str, str]) -> str:
    """Replace each intact transport attribute with the authored comment."""
    found: dict[str, int] = {name: 0 for name in comments}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in comments:
            raise ConversionError("projection contains an unknown Rosetta comment marker")
        found[name] += 1
        comment = comments[name]
        # A line comment must end before any following syntax that sv2v placed
        # after the attribute on the same line.
        return comment + ("\n" if comment.startswith("//") else " ")

    output = _SV_COMMENT_ATTRIBUTE.sub(replace, source)
    missing = [name for name, count in found.items() if count != 1]
    if missing:
        raise ConversionError(
            f"sv2v did not preserve source comment marker {missing[0]}; "
            "refusing to return a comment-stripped view"
        )
    if _SV_COMMENT_PREFIX in _mask_sv_comments_and_strings(output):
        raise ConversionError("malformed or edited Rosetta comment marker in sv2v output")
    return output


def _upgrade_code_only(source: str) -> str:
    """Keep comments, strings and escaped identifiers outside idiom rewrites."""
    pieces = []
    start = 0
    for match in _VERILOG_OPAQUE.finditer(source):
        code = source[start:match.start()]
        code = _upgrade_verilog_code_region(code)
        pieces.extend((code, match.group(0)))
        start = match.end()
    code = source[start:]
    pieces.append(_upgrade_verilog_code_region(code))
    return "".join(pieces)


def _upgrade_verilog_code_region(code: str) -> str:
    """Apply idiom lifts while leaving preprocessor directives byte-stable."""
    out: list[str] = []
    code_run: list[str] = []

    def flush_code_run() -> None:
        if not code_run:
            return
        text = "".join(code_run)
        for pattern, replacement in _UPGRADE_RULES:
            text = pattern.sub(replacement, text)
        out.append(text)
        code_run.clear()

    for line in code.splitlines(keepends=True):
        if _SV_DIRECTIVE_LINE.match(line.rstrip("\r\n")):
            flush_code_run()
            out.append(line)
        else:
            code_run.append(line)
    flush_code_run()
    return "".join(out)

# ----------------------------------------------------------------------
# pass-through ("原样保留"): a SystemVerilog-only construct the agent cannot
# express in Verilog (e.g. `typedef enum`) is written verbatim as Verilog
# comments between two markers; the layer preserves it as raw SV rather than
# translating (or refusing) it.
#
#   module TopModule (...);
#     // @SV-PASSTHROUGH
#     // typedef enum logic [1:0] { A = 0, B = 1 } state_t;
#     // state_t state;
#     // @END-SV-PASSTHROUGH
#     always @* begin ... end
#   endmodule
#
# Each block line is `// <sv>`; the `// ` prefix is stripped to recover the SV.
# ----------------------------------------------------------------------
_PT_START = re.compile(r"^\s*//\s*@SV-PASSTHROUGH\s*$")
_PT_END = re.compile(r"^\s*//\s*@END-SV-PASSTHROUGH\s*$")
_PT_LINE = re.compile(r"^\s*//\s?")
_SENTINEL = re.compile(r"^\s*//\s*__SV_PASSTHROUGH_(\d+)__\s*$")
_SENTINEL_TEXT = "__SV_PASSTHROUGH_"
_SV2V_TEMP = re.compile(r"\b_sv2v_(\d+)\b")
_ROSETTA_SV2V_TEMP = re.compile(r"\b__ROSETTA_SV2V_GEN_[0-9a-f]{10}_\d+\b")
_ROSETTA_SV2V_PREFIX = "__ROSETTA_SV2V_GEN_"
_ROSETTA_USER_PREFIX = "__ROSETTA_USER_SV2V_"
_ROSETTA_USER_TEMP = re.compile(r"\b__ROSETTA_USER_SV2V_[0-9a-f]{10}_\d+\b")
_SV_TOKEN = re.compile(r'//[^\r\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_$]*', re.S)


def _replace_sv_identifiers(text: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith(("//", "/*", '"')):
            return token
        return mapping.get(token, token)

    return _SV_TOKEN.sub(replace, text)


def _prepare_sv2v_source(source: str) -> str:
    identifiers = {match.group(0) for match in _SV_TOKEN.finditer(source)
                   if not match.group(0).startswith(("//", "/*", '"'))}
    if any(name.startswith((_ROSETTA_SV2V_PREFIX, _ROSETTA_USER_PREFIX))
           for name in identifiers):
        raise ConversionError("source uses a reserved Rosetta sv2v temporary-name prefix")
    originals = sorted({name for name in identifiers if _SV2V_TEMP.fullmatch(name)},
                       key=lambda name: int(_SV2V_TEMP.fullmatch(name).group(1)))
    if not originals:
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    mapping = {name: f"{_ROSETTA_USER_PREFIX}{digest}_{_SV2V_TEMP.fullmatch(name).group(1)}"
               for name in originals}
    if set(mapping.values()) & identifiers:
        raise ConversionError("source collides with generated Rosetta sv2v temporary names")
    return _replace_sv_identifiers(source, mapping)


def _restore_user_sv2v_temps(text: str) -> str:
    names = {match.group(0) for match in _ROSETTA_USER_TEMP.finditer(text)}
    mapping = {name: f"_sv2v_{name.rsplit('_', 1)[1]}" for name in names}
    return _replace_sv_identifiers(text, mapping)


def _tag_generated_sv2v_temps(source: str, projected: str) -> str:
    """Give sv2v-generated temporaries stable, reserved names for one projection."""
    source_tokens = {match.group(0) for match in _SV_TOKEN.finditer(source)
                     if not match.group(0).startswith(("//", "/*", '"'))}
    if any(name.startswith(_ROSETTA_SV2V_PREFIX) for name in source_tokens):
        raise ConversionError(
            f"source uses reserved generated-name prefix {_ROSETTA_SV2V_PREFIX!r}")
    source_names = {match.group(0) for match in _SV_TOKEN.finditer(source)
                    if _SV2V_TEMP.fullmatch(match.group(0))}
    projected_names = {match.group(0) for match in _SV_TOKEN.finditer(projected)
                       if _SV2V_TEMP.fullmatch(match.group(0))}
    generated = sorted((int(name.rsplit("_", 1)[1]) for name in projected_names
                        if name not in source_names))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    for number in generated:
        old = f"_sv2v_{number}"
        new = f"{_ROSETTA_SV2V_PREFIX}{digest}_{number}"
        projected = _replace_sv_identifiers(projected, {old: new})
    return projected


def _without_comments_and_strings(text: str) -> str:
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\r\n]*', re.S)
    return pattern.sub(lambda match: " " * len(match.group(0)), text)


def _strip_generated_sv2v_temps(verilog: str) -> str:
    """Remove only intact Rosetta-tagged sv2v startup guards from an agent view."""
    identifiers = {match.group(0) for match in _SV_TOKEN.finditer(verilog)
                   if not match.group(0).startswith(("//", "/*", '"'))}
    reserved = {name for name in identifiers if name.startswith(_ROSETTA_SV2V_PREFIX)}
    invalid = reserved - {name for name in reserved if _ROSETTA_SV2V_TEMP.fullmatch(name)}
    if invalid:
        raise ConversionError(f"malformed reserved sv2v helper name: {sorted(invalid)[0]}")
    user_reserved = {name for name in identifiers if name.startswith(_ROSETTA_USER_PREFIX)}
    if user_reserved - {name for name in user_reserved if _ROSETTA_USER_TEMP.fullmatch(name)}:
        raise ConversionError("malformed reserved user sv2v identifier")
    names = sorted(reserved)
    out = verilog
    for name in names:
        escaped = re.escape(name)
        declaration = re.compile(
            rf"(?m)^[ \t]*(?:reg|logic|wire)\s+{escaped}\s*;[ \t]*\r?\n?"
        )
        initialization = re.compile(
            rf"(?m)^[ \t]*initial\s+{escaped}\s*=\s*0\s*;[ \t]*\r?\n?"
        )
        guard = re.compile(
            rf"(?m)^[ \t]*if\s*\(\s*{escaped}\s*\)[ \t]*(?:\r?\n[ \t]*)?;[ \t]*\r?\n?"
        )
        declarations = declaration.findall(out)
        initializations = initialization.findall(out)
        guards = guard.findall(out)
        if len(declarations) != 1 or len(initializations) != 1 or not guards:
            raise ConversionError(
                f"generated sv2v temporary {name} is incomplete or was edited; "
                "refusing an ambiguous round-trip")
        candidate = declaration.sub("", out)
        candidate = initialization.sub("", candidate)
        candidate = guard.sub("", candidate)
        if re.search(rf"\b{escaped}\b", _without_comments_and_strings(candidate)):
            raise ConversionError(
                f"generated sv2v temporary {name} has non-generated uses; "
                "refusing an ambiguous round-trip")
        out = candidate
    return out


class ConversionError(ValueError):
    """A Verilog source cannot be upgraded without silently losing code.

    Raised (rather than returned as a failed TranslationResult) so it cannot be
    swallowed by a caller that only reads ``result.content``: an empty or
    half-upgraded deliverable is exactly the silent outcome this guards.
    """


def _extract_passthrough(verilog: str) -> tuple[str, list[str]]:
    """Split `// @SV-PASSTHROUGH` blocks out of a Verilog source.

    Returns ``(verilog_with_sentinels, sv_blocks)``.  Each block is replaced by
    a ``// __SV_PASSTHROUGH_i__`` comment that the mechanical upgrade leaves
    untouched, and which we splice back into after the surrounding Verilog is
    upgraded.  The recovered SV is stripped of trailing whitespace but otherwise
    verbatim.

    Two ways this could lose code silently, both refused rather than delivered:

    * **unterminated block** -- a missing ``@END`` marker made the reader
      swallow the rest of the file into the block.  The swallowed lines are
      spliced back verbatim, so they bypass the upgrade entirely: the delivered
      ``.sv`` keeps Verilog-2001 spelling (``reg``, ``always @*``) in the middle
      of a file that is otherwise SystemVerilog.  Verilog is a subset of
      SystemVerilog, so the result very often still lints -- which is precisely
      what makes it dangerous, and why the deliverable looks fine.
    * **sentinel collision** -- the sentinel is ordinary text, so an agent that
      wrote ``// __SV_PASSTHROUGH_0__`` itself would have that line replaced by a
      duplicate of block 0.  The marker namespace is reserved: any occurrence
      outside a block is an error rather than a duplicated block.
    """
    lines = verilog.splitlines()
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _PT_START.match(lines[i]):
            marker = lines[i]
            indent = marker[:len(marker) - len(marker.lstrip())]
            sv_lines: list[str] = []
            i += 1
            while i < n and not _PT_END.match(lines[i]):
                sv_lines.append(_PT_LINE.sub("", lines[i]))
                i += 1
            if i >= n:
                raise ConversionError(
                    "unterminated `// @SV-PASSTHROUGH` block: the `// "
                    "@END-SV-PASSTHROUGH` marker is missing, so the rest of "
                    "the file would have been delivered as raw SystemVerilog "
                    "without passing through the upgrade")
            out.append(f"{indent}// __SV_PASSTHROUGH_{len(blocks)}__")
            blocks.append("\n".join(sv_lines).strip())
            i += 1  # consume the END marker
            continue
        if _SENTINEL_TEXT in lines[i]:
            raise ConversionError(
                f"reserved marker text {_SENTINEL_TEXT!r} appears outside a "
                f"pass-through block (line {i + 1}); it was not written by the "
                f"layer and would collide with a preserved block")
        out.append(lines[i])
        i += 1
    return "\n".join(out), blocks


def _splice_passthrough(sv: str, blocks: list[str]) -> str:
    """Replace each ``// __SV_PASSTHROUGH_i__`` comment with the raw SV block.

    The sentinel's own indentation is re-applied to every line of the block, so
    a block written inside a module (or inside an ``always`` body) is delivered
    at its nesting depth rather than flush-left.  Verilog delimits blocks with
    ``begin``/``end`` rather than indentation, so an unindented body still
    compiled -- it just made the delivered artifact look as if the preserved
    code had escaped its block.
    """
    out: list[str] = []
    found: set[int] = set()
    for line in sv.splitlines():
        m = _SENTINEL.match(line)
        if m:
            index = int(m.group(1))
            if index >= len(blocks) or index in found:
                raise ConversionError("SystemVerilog pass-through marker is invalid or duplicated")
            found.add(index)
            indent = line[:len(line) - len(line.lstrip())]
            block = blocks[index]
            out.append("\n".join(indent + bl if bl.strip() else bl
                                 for bl in block.split("\n")))
            continue
        if _SENTINEL_TEXT in line:
            raise ConversionError("SystemVerilog pass-through marker was altered or moved")
        out.append(line)
    if len(found) != len(blocks):
        raise ConversionError("SystemVerilog pass-through marker was lost during translation")
    return "\n".join(out)


class EdaVerilogAdapter(ToolAdapter):
    """SystemVerilog <-> Verilog compatibility adapter.

    Projection (new_to_old) = the sv2v plugin + a light post-pass.
    Upgrade   (old_to_new) = mechanical idiom rules (+ per-task interface
                             re-wrapping supplied by the caller).
    """

    tool_name = "SystemVerilog"
    old_tool_name = "Verilog"
    old_extension = ".v"
    new_extension = ".sv"

    # ------------------------------------------------------------------
    # old_to_new : Verilog (agent) -> SystemVerilog (human)
    # ------------------------------------------------------------------

    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        """Upgrade an agent's Verilog view into the SystemVerilog artifact.

        Projection-only startup guards are removed only when their complete
        generated shape is intact. The ``_sv2v_N`` namespace is escaped while
        projecting user identifiers; both Rosetta temporary-name prefixes
        are reserved and edited generated guards fail closed.
        """
        # Refused projection artifacts and malformed pass-through blocks must
        # reach the caller as run failures, not as empty successful deliveries.
        content = _strip_generated_sv2v_temps(content)
        content, blocks = _extract_passthrough(content)
        out = _upgrade_code_only(content)
        out = _restore_user_sv2v_temps(out)
        if blocks:
            blocks = [_restore_user_sv2v_temps(block) for block in blocks]
            out = _splice_passthrough(out, blocks)
        handled = [
            ("always_ff", len(re.findall(r"\balways_ff\b", out))),
            ("always_comb", len(re.findall(r"\balways_comb\b", out))),
            ("logic", len(re.findall(r"\blogic\b", out))),
        ]
        return TranslationResult.ok(
            out,
            engine="rules/verilog2sv",
            passthrough=len(blocks),
            upgraded=handled,
        )

    # ------------------------------------------------------------------
    # new_to_old : SystemVerilog (human) -> Verilog (agent shadow) via sv2v
    # ------------------------------------------------------------------

    _FALLBACK_PROJECT_RULES = [
        # always_ff @(posedge clk) -> always @(posedge clk)
        (re.compile(r"\balways_ff\s+@\s*\(\s*(posedge|negedge)\s+"), r"always @(\1 "),
        # always_comb -> always @*
        (re.compile(r"\balways_comb\b"), "always @*"),
        # logic -> reg (lenient; shadow is not standalone-compiled)
        (re.compile(r"\blogic\b"), "reg"),
    ]

    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        """Project SV -> Verilog with the sv2v plugin (reused binary).

        sv2v faithfully lowers the SV idioms. When the source is an
        interface-port module with no flat top, sv2v intentionally emits
        nothing (it cannot flatten an interface without an instantiation
        context); this adapter refuses an empty projection.

        sv2v-generated ``_sv2v_N`` startup guards are tagged so they can be
        removed from the agent's returned view before the next projection.
        User-authored identifiers in that tool namespace are temporarily
        renamed and restored on delivery. Identifiers beginning with
        ``__ROSETTA_SV2V_GEN_`` or ``__ROSETTA_USER_SV2V_`` are reserved.
        """
        # sv2v can expand includes when given project paths, but this adapter
        # currently receives only one file and cannot reconstruct macro uses or
        # include boundaries on delivery. Refuse explicitly instead of exposing
        # a flattened result as a maintainable round trip.
        code = _VERILOG_OPAQUE.sub(
            lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()), content
        )
        if re.search(r'(?m)^\s*`include\b', code):
            return TranslationResult.fail([
                'SystemVerilog `include requires project-aware projection and source reconstruction; '
                'the current adapter cannot preserve those dependencies. Use a self-contained copy.'
            ])
        try:
            (projection_source, macro_restore, injectable_directives,
             source_directives, macro_expected_uses) = _prepare_sv2v_projection_source(content)
            projection_source, comment_restore, relocated_comments = _encode_sv_comments(
                projection_source
            )
            prepared = _prepare_sv2v_source(projection_source)
        except ConversionError as exc:
            return TranslationResult.fail([str(exc)])
        if not SV2V_BIN.exists():
            return TranslationResult.fail([
                f"sv2v executable not found at {SV2V_BIN}. Install sv2v and rerun "
                "setup.ps1, or set ROSETTA_SV2V to its executable path; restart "
                "Harness after changing the converter configuration."
            ])

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "projection.sv"
            src.write_text(prepared, encoding="utf-8")
            try:
                r = subprocess.run(
                    [str(SV2V_BIN), str(src)],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace",
                )
            except subprocess.TimeoutExpired:
                return TranslationResult.fail(["sv2v projection timed out"])
            except Exception as e:  # noqa: BLE001
                return TranslationResult.fail([f"sv2v exception: {e}"])

        out = r.stdout.strip()
        if r.returncode != 0:
            return TranslationResult.fail(
                [f"sv2v error: {(r.stderr or r.stdout)[:300]}"])
        if not out:
            return TranslationResult.fail([
                "sv2v produced an empty projection; interface-only modules need an explicit flattening schema"
            ])
        try:
            out = _tag_generated_sv2v_temps(prepared, out)
        except ConversionError as exc:
            return TranslationResult.fail([str(exc)])

        # Restore symbolic macro references after sv2v has lowered the rest of
        # the design.  Then preserve any source directives the converter
        # dropped, but only when relocating them from the compilation-unit
        # preamble is semantics-safe.
        try:
            out = _restore_sv_macro_aliases(out, macro_restore, macro_expected_uses)
        except ConversionError as exc:
            return TranslationResult.fail([str(exc)])
        try:
            out = _restore_sv_comments(out, comment_restore)
        except ConversionError as exc:
            return TranslationResult.fail([str(exc)])
        output_lines = out.splitlines(keepends=True)
        output_positions: dict[str, list[int]] = {}
        for output_index, _output_line, key in _directive_occurrences(out):
            output_positions.setdefault(key, []).append(output_index)
        injectable = {(index, name, line) for index, name, line in injectable_directives}
        restore: list[tuple[int, str]] = []
        for index, name, line in source_directives:
            if name == "include":
                continue
            key = _directive_key(line)
            positions = output_positions.get(key, [])
            if name != "define" and positions:
                output_index = positions.pop(0)
                output_line = output_lines[output_index]
                ending = ("\r\n" if output_line.endswith("\r\n") else
                          output_line[-1] if output_line.endswith(("\n", "\r")) else "")
                # Restore directive comments and exact spelling in place; this
                # preserves scope and avoids duplicating directives.
                output_lines[output_index] = line + ending
                continue
            if name == "define" or (index, name, line) in injectable:
                restore.append((index, line))
                continue
            return TranslationResult.fail([
                f"sv2v dropped `{name} on line {index + 1}; moving it could change "
                "its scope, so projection was refused instead of losing the directive"
            ])
        out = "".join(output_lines)
        if restore:
            prefix = "\n".join(line for _, line in sorted(restore))
            out = prefix + "\n" + out
        warnings = ([
            f"sv2v preserved all comments; {relocated_comments} comment(s) were moved "
            "to a nearby construct or the module preamble for transport"
        ] if relocated_comments else [])
        return TranslationResult.ok(out, warnings=warnings, engine="sv2v", projected=True)


if __name__ == "__main__":
    # quick self-test: sv2v lowers the mechanical idioms; upgrade lifts them.
    SV_MOD = """\
module counter (
    input logic clk,
    input logic rst,
    output logic [7:0] count
);
    always_ff @(posedge clk) begin
        if (rst) count <= 8'd0;
        else count <= count + 1;
    end
    always_comb begin
        ;
    end
endmodule
"""
    a = EdaVerilogAdapter()
    proj = a.new_to_old(SV_MOD, filepath="counter.sv")
    assert proj.success and proj.content.strip(), proj.errors or proj.warnings
    assert "always @(posedge clk)" in proj.content, proj.content
    assert "always @(*)" in proj.content, proj.content
    assert "output reg [7:0] count" in proj.content, proj.content
    assert proj.metadata.get("engine") == "sv2v", proj.metadata

    up = a.old_to_new(proj.content, filepath="counter.v")
    assert up.success, "old_to_new failed"
    assert "always_ff @(posedge clk)" in up.content, up.content
    assert "always_comb" in up.content, up.content
    assert "output logic [7:0] count" in up.content, up.content

    print("== project (.sv -> .v) via sv2v ==")
    print(proj.content)
    print("== upgrade (.v -> .sv) via rules ==")
    print(up.content)
    print("OK: sv2v projection + rules upgrade pass")
