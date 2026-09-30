"""
simple_yaml.py -- minimal, dependency-free YAML-subset loader
=============================================================

Deployment reality: the mpiio_evolve launcher/evaluator runs on a cluster
login node with a **frozen system Python (3.9)** and no guaranteed PyYAML.
This module implements just enough YAML (core schema) to load ``config.yaml``
using only the standard library:

  * nested block mappings (consistent indentation per level)
  * flow sequences  [a, b, "c", null, true]   and empty flow map  {}
  * block scalars   |  >  |-  >-   (clip/strip chomping)
  * full-line and trailing comments (quote-aware)
  * scalars: null / ~ / empty, true / false, ints, floats, quoted + plain
    strings

Explicitly NOT supported (and absent from this project's config): anchors
&/aliases, tags, multi-document streams, sequences of mappings, complex
keys, backslash escapes in single quotes.

Escape hatch if the config ever grows past this subset: run
``python3 tools/compile_config.py`` on any machine with PyYAML and ship
``config.generated.json`` -- JSON is guaranteed to parse with the stdlib.
"""

from __future__ import annotations

import re

__all__ = ["load", "YamlError"]


class YamlError(ValueError):
    """Raised when the input exceeds the supported YAML subset."""


# plain identifier keys only (this project's configs qualify)
_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-/]+):(?:[ \t]+(.*))?$")


def load(text: str):
    """Parse a YAML-subset document into dict/list/scalar Python values."""
    reader = _Reader(text)
    first = reader.peek()
    if first is None:
        return {}
    value = _parse_node(reader, first[0])
    leftover = reader.peek()
    if leftover is not None:
        raise YamlError(f"unexpected content after document: {leftover[1]!r}")
    return value


# ---------------------------------------------------------------------------
# Line handling
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment, respecting quoted strings."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


class _Reader:
    """Cursor over raw physical lines with significant-line lookahead."""

    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.pos = 0

    def peek(self):
        """(indent, stripped_content) of the next significant line, or None."""
        i = self.pos
        while i < len(self.raw):
            content = _strip_comment(self.raw[i]).strip()
            if content:
                return _indent_of(_strip_comment(self.raw[i])), content
            i += 1
        return None

    def advance(self) -> None:
        """Skip insignificant lines, then consume one significant line."""
        while self.pos < len(self.raw):
            if _strip_comment(self.raw[self.pos]).strip():
                break
            self.pos += 1
        self.pos += 1


# ---------------------------------------------------------------------------
# Structure parsing
# ---------------------------------------------------------------------------


def _parse_node(reader: _Reader, indent: int):
    nxt = reader.peek()
    if nxt is not None and (nxt[1] == "-" or nxt[1].startswith("- ")):
        return _parse_seq(reader, indent)
    return _parse_map(reader, indent)


def _parse_map(reader: _Reader, indent: int) -> dict:
    result = {}
    while True:
        nxt = reader.peek()
        if nxt is None or nxt[0] < indent:
            break
        if nxt[0] > indent:
            raise YamlError(f"inconsistent indentation: {nxt[1]!r}")
        content = nxt[1]
        if content == "-" or content.startswith("- "):
            break  # block sequence belongs to an enclosing key

        m = _KEY_RE.match(content)
        if not m:
            raise YamlError(f"expected 'key: value', got: {content!r}")
        key, rest = m.group(1), (m.group(2) or "").strip()
        reader.advance()

        if rest == "":
            child = reader.peek()
            if child is not None and child[0] > indent:
                result[key] = _parse_node(reader, child[0])
            elif child is not None and child[0] == indent and (
                child[1] == "-" or child[1].startswith("- ")
            ):
                result[key] = _parse_seq(reader, indent)  # seq at key indent
            else:
                result[key] = None
        elif rest[0] in ">|" and rest[1:] in ("", "-", "+"):
            result[key] = _read_block(reader, indent, rest[0], rest[1:])
        elif rest.startswith("[") and rest.endswith("]"):
            result[key] = _parse_flow_seq(rest)
        elif rest.startswith("{") and rest.endswith("}"):
            if rest.strip("{} \t"):
                raise YamlError(f"inline mappings not supported: {rest!r}")
            result[key] = {}
        else:
            result[key] = _scalar(rest)
    return result


def _parse_seq(reader: _Reader, indent: int) -> list:
    items = []
    while True:
        nxt = reader.peek()
        if nxt is None or nxt[0] < indent:
            break
        content = nxt[1]
        if not (content == "-" or content.startswith("- ")):
            break
        if nxt[0] > indent:
            raise YamlError(f"inconsistent indentation in sequence: {content!r}")
        item = content[1:].strip()
        reader.advance()
        if not item:
            child = reader.peek()
            items.append(_parse_node(reader, child[0])
                         if child and child[0] > indent else None)
        elif item.startswith("[") and item.endswith("]"):
            items.append(_parse_flow_seq(item))
        else:
            items.append(_scalar(item))
    return items


def _parse_flow_seq(text: str) -> list:
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [_scalar(part) for part in _split_commas(inner)]


def _split_commas(inner: str) -> list:
    parts, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p != ""]


def _read_block(reader: _Reader, key_indent: int, style: str, chomp: str) -> str:
    """Consume a literal (|) or folded (>) block scalar."""
    lines = []
    while reader.pos < len(reader.raw):
        line = reader.raw[reader.pos]
        if line.strip() == "":
            lines.append("")
            reader.pos += 1
            continue
        if _indent_of(line) <= key_indent:
            break
        lines.append(line)
        reader.pos += 1

    trailing = 0
    while lines and lines[-1] == "":
        lines.pop()
        trailing += 1
    if not lines:
        return "\n" if chomp == "+" else ""

    base = min(_indent_of(l) for l in lines if l.strip())
    ded = [l[base:] if l.strip() else "" for l in lines]

    if style == "|":
        text = "\n".join(ded)
    else:  # folded: consecutive text lines join with a space, blanks -> \n
        out, cur = [], []
        for l in ded:
            if l == "":
                if cur:
                    out.append(" ".join(cur))
                    cur = []
                out.append("\n")
            else:
                cur.append(l)
        if cur:
            out.append(" ".join(cur))
        text = "".join(out)

    if chomp == "-":
        return text
    if chomp == "+":
        return text + "\n" * (1 + trailing)
    return text + "\n"          # clip: exactly one trailing newline


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _scalar(text: str):
    s = text.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return _unescape_double(s[1:-1])
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    low = s.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "/": "/"}


def _unescape_double(inner: str) -> str:
    out, esc = [], False
    for ch in inner:
        if esc:
            out.append(_ESCAPES.get(ch, "\\" + ch))
            esc = False
        elif ch == "\\":
            esc = True
        else:
            out.append(ch)
    return "".join(out)
