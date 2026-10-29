"""Conservative Pandoc-backed conversion for block-style Typst/LaTeX lists.

The surrounding document stays on the existing Tylax pipeline. Only list
fragments are sent through Pandoc, which understands nested and ordered list
structure that Tylax currently flattens. Ambiguous boundaries and damaged
transport markers fail closed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .typst_pipeline import ConversionError, _directive_end


_FRAGMENT_WORD = "ROSETTALISTFRAGMENT"
_BATCH_WORD = "ROSETTABATCHSEPARATOR"
_TYPST_ITEM = re.compile(r"^[ \t]*(?:[+-]|\d+\.)[ \t]+\S")
_TOP_LEVEL_TYPST_ITEM = re.compile(r"^(?:[+-]|\d+\.)[ \t]+\S")
_LATEX_LIST_ENV = {"itemize", "enumerate"}
_LATEX_LIST_TOKEN = re.compile(r"\\(begin|end)\s*\{(itemize|enumerate)\}")
_LATEX_MARKER = re.compile(
    r"^[ \t]*%[ \t]*ROSETTA-LIST-(START|END)[ \t]+([A-Za-z0-9_-]+)(?:[ \t]+(tight|loose))?[ \t]*$"
)
_FRAGMENT_MARKER = re.compile(re.escape(_FRAGMENT_WORD) + r"([A-Za-z0-9_-]+)MARK")
_BATCH_MARKER = re.compile(re.escape(_BATCH_WORD) + r"\d+MARK")


def _splice_line_spans(
    lines: list[str], replacements: list[tuple[int, int, str]],
) -> str:
    """Replace non-overlapping line spans in one pass over the document."""
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        if start < cursor or end < start or end > len(lines):
            raise ConversionError("overlapping or invalid list fragment spans")
        output.extend(lines[cursor:start])
        output.append(replacement)
        cursor = end
    output.extend(lines[cursor:])
    return "".join(output)


def _splice_text_spans(
    text: str, replacements: list[tuple[int, int, str]],
) -> str:
    """Replace non-overlapping character spans without repeated whole-text copies."""
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        if start < cursor or end < start or end > len(text):
            raise ConversionError("overlapping or invalid LaTeX list fragment spans")
        output.extend((text[cursor:start], replacement))
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


def _generated_enum_block(lines: list[str], start: int, end: int) -> bool:
    """Recognize Pandoc's narrow #block[#set enum(...)+items] wrapper."""
    if start + 2 >= end or not re.fullmatch(r"[ \t]*#block\[[ \t]*", lines[start].rstrip("\r\n")):
        return False
    closing = end - 1
    if not re.fullmatch(r"[ \t]*\][ \t]*", lines[closing].rstrip("\r\n")):
        return False
    inner = lines[start + 1:closing]
    first = next((i for i, line in enumerate(inner) if line.strip()), None)
    if first is None or not re.match(r"[ \t]*#set\s+enum\s*\(", inner[first]):
        return False
    try:
        after_set = _directive_end(inner, first)
    except ConversionError:
        return False
    items = 0
    for line in inner[after_set:]:
        if not line.strip():
            continue
        if _TYPST_ITEM.match(line):
            items += 1
        elif line[:1] in (" ", "\t"):
            continue
        else:
            return False
    return items > 0


@dataclass(frozen=True)
class ListFragment:
    marker: str
    content: str
    tight: bool = False


def _marker(index: int | str) -> str:
    return f"{_FRAGMENT_WORD}{index}MARK"


def _fragment_marker_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _FRAGMENT_MARKER.finditer(text):
        marker = match.group(0)
        counts[marker] = counts.get(marker, 0) + 1
    return counts


def _has_reserved_marker(text: str) -> bool:
    return _FRAGMENT_WORD in text or "ROSETTA-LIST-" in text


def extract_typst_lists(text: str) -> tuple[str, list[ListFragment]]:
    """Replace top-level ``+``/``-``/``1.`` list blocks with sentinels.

    Nested markers and indented continuation/code lines stay inside their
    parent fragment. Fenced code and block comments are skipped when looking
    for a top-level list start. This deliberately does not guess function-form
    ``#enum(...)``/``#list(...)`` boundaries.
    """
    if _has_reserved_marker(text):
        raise ConversionError("reserved Rosetta list marker text appears in Typst source")
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int, bool | None]] = []
    i = 0
    fence_char: str | None = None
    fence_size = 0
    block_comment_depth = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        fence = re.fullmatch(r"[ \t]*(`{3,}|~{3,})[^`~]*", line)
        if fence_char:
            closing = re.fullmatch(r"[ \t]*([`~]+)[ \t]*", line)
            if closing and closing[1][0] == fence_char and len(closing[1]) >= fence_size:
                fence_char = None
                fence_size = 0
            i += 1
            continue
        if block_comment_depth:
            block_comment_depth += line.count("/*") - line.count("*/")
            i += 1
            continue
        if fence:
            fence_char = fence[1][0]
            fence_size = len(fence[1])
            i += 1
            continue
        if "/*" in line:
            block_comment_depth += line.count("/*") - line.count("*/")
            i += 1
            continue
        if line.lstrip().startswith("//"):
            i += 1
            continue
        if re.fullmatch(r"[ \t]*#block\[[ \t]*", line):
            end = _directive_end(lines, i)
            if _generated_enum_block(lines, i, end):
                body = "".join(lines[i:end]).rstrip("\r\n")
                tight = not re.search(r"\r?\n[ \t]*\r?\n", body)
                spans.append((i, end, tight))
                i = end
                continue
        if re.match(r"^#(?:enum|list)\s*\(", line):
            end = _directive_end(lines, i)
            spans.append((i, end, True))
            i = end
            continue
        if not _TOP_LEVEL_TYPST_ITEM.match(line):
            i += 1
            continue

        start = i
        last_content = i
        i += 1
        after_blank_line = False
        while i < len(lines):
            current = lines[i].rstrip("\r\n")
            if not current.strip():
                after_blank_line = True
                i += 1
                continue
            # Tylax may indent a following block function by one space. After
            # a blank separator that is a new document block, not list-item
            # continuation; sending it to Pandoc as part of the list produces
            # malformed fragments on the next projection round. Keep genuinely
            # nested continuation blocks (two or more spaces) in the list.
            indentation = len(current) - len(current.lstrip(" \t"))
            if after_blank_line and indentation <= 1 and current.lstrip().startswith("#"):
                break
            if _TYPST_ITEM.match(current) or current[:1] in (" ", "\t"):
                last_content = i
                after_blank_line = False
                i += 1
                continue
            break
        end = last_content + 1
        spans.append((start, end, None))
        i = end

    if not spans:
        return text, []

    fragments: list[ListFragment] = []
    replacements: list[tuple[int, int, str]] = []
    for index, (start, end, tight_override) in reversed(list(enumerate(spans))):
        body = "".join(lines[start:end]).rstrip("\r\n")
        # A blank line inside the source list denotes a loose list. Blank lines
        # following the final item are outside the captured span.
        tight = tight_override if tight_override is not None else not re.search(r"\r?\n[ \t]*\r?\n", body)
        marker = _marker(index)
        fragments.append(ListFragment(marker, body, tight))
        replacements.append((start, end, marker + ("\n" if lines[end - 1].endswith(("\n", "\r")) else "")))
    fragments.reverse()
    return _splice_line_spans(lines, replacements), fragments


def _strip_latex_comment(line: str) -> str:
    for index, char in enumerate(line):
        if char != "%":
            continue
        slashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            slashes += 1
            cursor -= 1
        if slashes % 2 == 0:
            return line[:index]
    return line


def _find_latex_list_spans(text: str) -> list[tuple[int, int]]:
    """Find balanced itemize/enumerate blocks, ignoring comments and verbatim."""
    spans: list[tuple[int, int]] = []
    offset = 0
    stack: list[str] = []
    outer_start: int | None = None
    opaque_env: str | None = None
    for raw_line in text.splitlines(keepends=True):
        line = _strip_latex_comment(raw_line)
        if opaque_env:
            if re.search(r"\\end\s*\{" + re.escape(opaque_env) + r"\}", line):
                opaque_env = None
            offset += len(raw_line)
            continue
        opaque = re.search(r"\\begin\s*\{(verbatim|lstlisting|minted)\}", line)
        if opaque:
            opaque_env = opaque[1]
            offset += len(raw_line)
            continue
        for match in _LATEX_LIST_TOKEN.finditer(line):
            env_start = offset + match.start()
            env_end = offset + match.end()
            if match[1] == "begin":
                if not stack:
                    outer_start = env_start
                stack.append(match[2])
            else:
                if not stack or stack[-1] != match[2]:
                    raise ConversionError("unbalanced LaTeX itemize/enumerate environment")
                stack.pop()
                if not stack:
                    if outer_start is None:
                        raise ConversionError("LaTeX list boundary was lost")
                    spans.append((outer_start, env_end))
                    outer_start = None
        offset += len(raw_line)
    if stack:
        raise ConversionError("unclosed LaTeX itemize/enumerate environment")
    if opaque_env:
        raise ConversionError(f"unclosed LaTeX {opaque_env} environment")
    return spans


def extract_latex_lists(text: str) -> tuple[str, list[ListFragment]]:
    """Extract marked lists and newly-authored itemize/enumerate blocks."""
    if _has_reserved_marker(text) and "ROSETTA-LIST-" not in text:
        raise ConversionError("reserved Rosetta list fragment text appears in LaTeX source")

    lines = text.splitlines(keepends=True)
    line_offsets = _line_offsets(lines)
    offset_to_line = {offset: index for index, offset in enumerate(line_offsets)}
    marked_spans: list[tuple[int, int, ListFragment]] = []
    active: tuple[str, int, bool] | None = None
    seen: set[str] = set()
    offset = 0
    for raw_line in lines:
        marker_match = _LATEX_MARKER.fullmatch(raw_line.rstrip("\r\n"))
        if "ROSETTA-LIST-" in raw_line and not marker_match:
            raise ConversionError("malformed Rosetta list marker in LaTeX view")
        if marker_match:
            side, ident, tightness = marker_match.groups()
            if side == "START":
                if active is not None or ident in seen:
                    raise ConversionError("nested or duplicate Rosetta list marker")
                active = (ident, offset, tightness == "tight")
                seen.add(ident)
            else:
                if active is None or active[0] != ident:
                    raise ConversionError("unmatched Rosetta list end marker")
                start_line = offset_to_line[active[1]]
                end_line = offset_to_line[offset]
                body = "".join(lines[start_line + 1:end_line]).strip("\r\n")
                marker = _marker(ident)
                marked_spans.append((start_line, end_line + 1, ListFragment(marker, body, active[2])))
                active = None
        offset += len(raw_line)
    if active is not None:
        raise ConversionError("unclosed Rosetta list marker in LaTeX view")

    fragments: list[ListFragment] = []
    marked_replacements: list[tuple[int, int, str]] = []
    for start, end, fragment in reversed(marked_spans):
        fragments.append(fragment)
        newline = "\n" if lines[end - 1].endswith(("\n", "\r")) else ""
        marked_replacements.append((start, end, fragment.marker + newline))

    masked_text = _splice_line_spans(lines, marked_replacements)
    automatic_replacements: list[tuple[int, int, str]] = []
    for start, end in reversed(_find_latex_list_spans(masked_text)):
        marker = _marker(f"AUTO{len(fragments)}")
        body = masked_text[start:end]
        fragments.append(ListFragment(marker, body, False))
        newline = "\n" if body.endswith(("\n", "\r")) else ""
        automatic_replacements.append((start, end, marker + newline))
    masked_text = _splice_text_spans(masked_text, automatic_replacements)

    if _FRAGMENT_WORD in masked_text:
        marker_counts = _fragment_marker_counts(masked_text)
        if any(marker_counts.get(fragment.marker, 0) != 1 for fragment in fragments):
            raise ConversionError("LaTeX list fragment was duplicated or lost")
    return masked_text, fragments


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    return offsets


def translate_fragments(
    text: str,
    fragments: list[ListFragment],
    pandoc: Path,
    source_format: str,
    target_format: str,
) -> str:
    """Translate all isolated list fragments in one Pandoc process."""
    if not fragments:
        return text

    inputs: list[str] = []
    marker_counts = _fragment_marker_counts(text)
    for fragment in fragments:
        if marker_counts.get(fragment.marker, 0) != 1:
            raise ConversionError("list fragment was lost, duplicated or moved into another construct")
        fragment_input = fragment.content
        if source_format == "latex" and target_format == "typst":
            # Tylax emits this exact default label definition alongside
            # \setcounter for non-1 ordered lists. Pandoc otherwise treats the
            # label macro as custom numbering and drops the start value. The
            # definition is semantically identical to LaTeX's default label;
            # remove it only when the matching counter is explicitly present.
            for counter in ("enumi", "enumii", "enumiii", "enumiv"):
                if re.search(r"\\setcounter\s*\{" + counter + r"\}", fragment_input):
                    default_label = re.compile(
                        r"\\def\s*\\label" + counter + r"\s*\{\s*\\arabic\s*\{"
                        + counter + r"\}\s*\.\s*\}"
                    )
                    fragment_input = default_label.sub("", fragment_input)
        if _BATCH_WORD in fragment_input:
            raise ConversionError("reserved Rosetta batch marker text appears in a list fragment")
        inputs.append(fragment_input)

    separators = [f"{_BATCH_WORD}{index}MARK" for index in range(len(fragments) - 1)]
    combined_parts: list[str] = []
    for index, fragment_input in enumerate(inputs):
        if index:
            combined_parts.extend(("\n\n", separators[index - 1], "\n\n"))
        combined_parts.append(fragment_input)
    combined_input = "".join(combined_parts)
    try:
        result = subprocess.run(
            [str(pandoc), "-f", source_format, "-t", target_format, "--no-highlight"],
            input=combined_input,
            capture_output=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        raise ConversionError(f"Pandoc list conversion failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip()[:500]
        raise ConversionError(f"Pandoc could not convert list fragments: {detail or 'no output'}")

    converted_fragments: list[str] = []
    cursor = 0
    separator_index = 0
    for match in _BATCH_MARKER.finditer(result.stdout):
        if separator_index >= len(separators) or match.group(0) != separators[separator_index]:
            raise ConversionError("Pandoc lost, duplicated or altered a list batch separator")
        converted_fragments.append(result.stdout[cursor:match.start()].strip())
        cursor = match.end()
        separator_index += 1
    if separator_index != len(separators):
        raise ConversionError("Pandoc lost, duplicated or altered a list batch separator")
    converted_fragments.append(result.stdout[cursor:].strip())
    if len(converted_fragments) != len(fragments):
        raise ConversionError("Pandoc returned a different number of list fragments")

    replacements: dict[str, str] = {}
    for fragment, converted in zip(fragments, converted_fragments):
        if target_format == "latex":
            replacement = (
                f"% ROSETTA-LIST-START {fragment.marker[len(_FRAGMENT_WORD):-4]} "
                f"{'tight' if fragment.tight else 'loose'}\n{converted}\n"
                f"% ROSETTA-LIST-END {fragment.marker[len(_FRAGMENT_WORD):-4]}"
            )
        elif target_format == "typst" and fragment.tight:
            # Pandoc's writer inserts presentation blank lines even for tight
            # lists. Remove only separators directly adjacent to list markers;
            # paragraph spacing elsewhere remains intact.
            lines = converted.splitlines()
            previous: list[str] = [""] * len(lines)
            following: list[str] = [""] * len(lines)
            latest = ""
            for index, line in enumerate(lines):
                previous[index] = latest
                if line.strip():
                    latest = line
            latest = ""
            for index in range(len(lines) - 1, -1, -1):
                following[index] = latest
                if lines[index].strip():
                    latest = lines[index]
            compact: list[str] = []
            for index, line in enumerate(lines):
                if line.strip():
                    compact.append(line)
                    continue
                before = previous[index]
                after = following[index]
                if _TYPST_ITEM.match(before) and _TYPST_ITEM.match(after):
                    continue
                compact.append(line)
            replacement = "\n".join(compact)
        else:
            replacement = converted
        if fragment.marker in replacements:
            raise ConversionError("duplicate list fragment marker in conversion request")
        replacements[fragment.marker] = replacement
    def replace_marker(match: re.Match[str]) -> str:
        marker = match.group(0)
        if marker not in replacements:
            raise ConversionError("unknown list fragment marker in document")
        return replacements[marker]

    text = _FRAGMENT_MARKER.sub(replace_marker, text)
    if _BATCH_WORD in text:
        raise ConversionError("unrecognized Pandoc batch marker remains after list translation")
    if _FRAGMENT_WORD in text:
        raise ConversionError("unrecognized list fragment marker remains after translation")
    return text
