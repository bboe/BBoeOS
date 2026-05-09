#!/usr/bin/env python3
"""Reflow markdown prose to 80-column lines for mobile-friendly source.

Preserves fenced code blocks, indented code blocks, headings, tables, HTML
lines, YAML frontmatter, horizontal rules, and blank lines.  Reflows
paragraphs and list items, with continuation lines indented to match the
marker.  Single unbreakable tokens (long URLs) overflow the width rather
than being split.

Usage: tools/wrap_md.py <file.md> [<file.md> ...]
"""

import pathlib
import re
import sys

HORIZONTAL_RULE_MIN_LENGTH = 3
LEADING_WHITESPACE_RE = re.compile(r"^(\s*)")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)(\s+)(.*)$")
WIDTH = 80
WORD_OR_WHITESPACE_RE = re.compile(r"\s+|\S+")


def consume_indented_code(*, lines: list[str], out: list[str], start: int) -> int:
    """Append the indented-code block starting at *start* to *out*.  Return next index."""
    index = start
    while index < len(lines) and (is_indented_code(line=lines[index]) or is_blank(line=lines[index])):
        out.append(lines[index])
        index += 1
    return index


def fence_marker(*, line: str) -> str:
    """Return the fence delimiter (``` or ~~~) that opens *line*."""
    return "```" if "```" in line else "~~~"


def gather_paragraph(*, lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect consecutive prose / list-continuation lines starting at *start*.

    Stops at the first line that begins a new block (blank, heading, table,
    fence, HTML, list item, horizontal rule) or — for non-list paragraphs —
    an indented-code line.  Returns the gathered lines and the index of the
    first line *not* consumed.
    """
    paragraph = [lines[start]]
    scout = start + 1
    while scout < len(lines):
        following = lines[scout]
        if is_paragraph_terminator(line=following):
            break
        if is_indented_code(line=following) and not is_list_item(line=paragraph[0]):
            break
        paragraph.append(following)
        scout += 1
    return paragraph, scout


def is_blank(*, line: str) -> bool:
    """Return True if *line* is empty or whitespace-only."""
    return not line.strip()


def is_fence(*, line: str) -> bool:
    """Return True if *line* opens or closes a fenced code block."""
    return line.lstrip().startswith(("```", "~~~"))


def is_heading(*, line: str) -> bool:
    """Return True if *line* is an ATX heading (`#`-prefixed)."""
    return line.lstrip().startswith("#")


def is_horizontal_rule(*, line: str) -> bool:
    """Return True if *line* is a markdown horizontal rule (---, ***, or ___)."""
    stripped = line.strip()
    if len(stripped) < HORIZONTAL_RULE_MIN_LENGTH:
        return False
    return any(stripped == character * len(stripped) for character in "-*_")


def is_html(*, line: str) -> bool:
    """Return True if *line* looks like a raw HTML block (starts with `<`)."""
    stripped = line.lstrip()
    return stripped.startswith("<") and not stripped.startswith("<<")


def is_indented_code(*, line: str) -> bool:
    """Return True if *line* is an indented code block line (4+ space indent)."""
    return line.startswith("    ") and not LIST_RE.match(line)


def is_list_item(*, line: str) -> bool:
    """Return True if *line* starts with a markdown list marker."""
    return bool(LIST_RE.match(line))


def is_paragraph_terminator(*, line: str) -> bool:
    """Return True if *line* ends the current paragraph."""
    return (
        is_blank(line=line)
        or is_fence(line=line)
        or is_heading(line=line)
        or is_horizontal_rule(line=line)
        or is_html(line=line)
        or is_list_item(line=line)
        or is_table(line=line)
    )


def is_table(*, line: str) -> bool:
    """Return True if *line* is a markdown table row (starts with `|`)."""
    return line.lstrip().startswith("|")


def main() -> None:
    """Reflow each markdown file passed on the command line."""
    for path in sys.argv[1:]:
        target = pathlib.Path(path)
        original = target.read_text(encoding="utf-8")
        rewrapped = reflow(content=original)
        if rewrapped != original:
            target.write_text(rewrapped, encoding="utf-8")
            print(f"rewrapped: {path}")
        else:
            print(f"unchanged: {path}")


def paragraph_indent_and_text(*, paragraph: list[str]) -> tuple[str, str, str]:
    """Compute (indent_first, indent_rest, text) for *paragraph*.

    For list items, indents account for the marker plus its trailing
    whitespace; continuation lines align under the first character past the
    marker.  For prose, the leading whitespace of the first line is used for
    every wrapped line.  Continuation lines are stripped before joining.
    """
    first = paragraph[0]
    list_match = LIST_RE.match(first)
    if list_match:
        indent_first = list_match.group(1) + list_match.group(2) + list_match.group(3)
        indent_rest = " " * len(indent_first)
        text_parts = [list_match.group(4), *(continuation.strip() for continuation in paragraph[1:])]
    else:
        indent_match = LEADING_WHITESPACE_RE.match(first)
        indent_first = indent_match.group(1) if indent_match else ""
        indent_rest = indent_first
        text_parts = [first.strip(), *(continuation.strip() for continuation in paragraph[1:])]
    text = " ".join(part for part in text_parts if part)
    return indent_first, indent_rest, text


def reflow(*, content: str) -> str:
    """Return *content* with prose reflowed to WIDTH-column lines."""
    in_fence = False
    lines = content.split("\n")
    marker = ""
    out: list[str] = []
    index = skip_frontmatter(lines=lines, out=out)
    while index < len(lines):
        line = lines[index]
        if in_fence:
            out.append(line)
            if line.lstrip().startswith(marker):
                in_fence = False
            index += 1
            continue
        if is_fence(line=line):
            in_fence = True
            marker = fence_marker(line=line)
            out.append(line)
            index += 1
            continue
        if is_horizontal_rule(line=line):
            out.append(line)
            index += 1
            continue
        if is_indented_code(line=line) and (not out or is_blank(line=out[-1])):
            index = consume_indented_code(lines=lines, out=out, start=index)
            continue
        if is_blank(line=line) or is_heading(line=line) or is_html(line=line) or is_table(line=line):
            out.append(line)
            index += 1
            continue
        paragraph, index = gather_paragraph(lines=lines, start=index)
        indent_first, indent_rest, text = paragraph_indent_and_text(paragraph=paragraph)
        out.append(wrap_text(indent_first=indent_first, indent_rest=indent_rest, text=text))
    return "\n".join(out)


def skip_frontmatter(*, lines: list[str], out: list[str]) -> int:
    """Copy YAML frontmatter from *lines* into *out* if present.

    Returns the index of the first line after the closing `---` (or 0 when
    no frontmatter is present).
    """
    if not lines or lines[0].strip() != "---":
        return 0
    out.append(lines[0])
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        out.append(lines[index])
        index += 1
    if index < len(lines):
        out.append(lines[index])
        index += 1
    return index


def tokenize(*, text: str) -> list[tuple[str, str]]:
    """Split *text* into ``(separator_before, word)`` tokens.

    Preserves inline whitespace runs so reflowing doesn't collapse, e.g.,
    the double-space convention after sentence-ending punctuation.
    """
    parts = WORD_OR_WHITESPACE_RE.findall(text)
    separator = ""
    tokens: list[tuple[str, str]] = []
    for part in parts:
        if part and part[0].isspace():
            separator = part
        else:
            tokens.append((separator, part))
            separator = ""
    return tokens


def wrap_text(*, indent_first: str, indent_rest: str, text: str, width: int = WIDTH) -> str:
    """Wrap *text* to *width* columns using the supplied indents.

    *indent_first* prefixes the first output line; *indent_rest* prefixes
    every continuation.  Tokens that exceed *width* on their own (long URLs)
    overflow rather than being split.
    """
    tokens = tokenize(text=text)
    if not tokens:
        return ""
    current = indent_first + tokens[0][1]
    lines: list[str] = []
    for separator, word in tokens[1:]:
        candidate = current + separator + word
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = indent_rest + word
    lines.append(current)
    return "\n".join(lines)


if __name__ == "__main__":
    main()
