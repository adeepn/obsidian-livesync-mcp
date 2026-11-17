"""Pure-text link extraction and resolution over vault-relative paths."""

from collections import namedtuple
from collections.abc import Iterable
import posixpath
import re
from urllib.parse import unquote

Link = namedtuple("Link", ["target", "kind"])

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _without_md(path: str) -> str:
    return path[:-3] if path.casefold().endswith(".md") else path


def _prose(text: str) -> str:
    """Remove fenced blocks, including an unfinished final block."""
    lines = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if not fence_char:
            if match:
                fence_char = match[1][0]
                fence_length = len(match[1])
            else:
                lines.append(line)
        elif (match and match[1][0] == fence_char
              and len(match[1]) >= fence_length and not match[2].strip()):
            fence_char = ""
            lines.append("\n")
    return "".join(lines)


def _without_code_spans(text: str) -> str:
    """Mask spans using the next backtick run of exactly the same length."""
    runs = list(re.finditer(r"`+", text))
    following = {}
    closing = {}
    for run in reversed(runs):
        length = run.end() - run.start()
        if length in following:
            closing[run.start()] = following[length]
        following[length] = run
    pieces = []
    cursor = 0
    for run in runs:
        if run.start() < cursor or run.start() not in closing:
            continue
        end = closing[run.start()].end()
        pieces.append(text[cursor:run.start()])
        pieces.append("".join("\n" if c == "\n" else " "
                              for c in text[run.start():end]))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _delimiter_positions(text: str) -> tuple[list[int], ...]:
    """Index destination delimiters once instead of rescanning failed tails."""
    size = len(text)
    word, angle, title, nonspace = ([size] * (size + 1) for _ in range(4))
    escaped = set()
    pairs = {}
    stack = []
    i = 0
    while i < size:
        if text[i] == "\\" and i + 1 < size:
            escaped.add(i + 1)
            i += 2
            continue
        if text[i] == "(":
            stack.append(i)
        elif text[i] == ")" and stack:
            pairs[stack.pop()] = i
        i += 1
    for i in range(size - 1, -1, -1):
        char = text[i]
        if i in escaped:
            word[i] = word[i + 1]
        elif i in pairs:
            word[i] = word[pairs[i] + 1]
        else:
            word[i] = i if char.isspace() or char == ")" else word[i + 1]
        angle[i] = i if char in ">\n" else angle[i + 1]
        title[i] = i if char in '\n"' else title[i + 1]
        nonspace[i] = nonspace[i + 1] if char.isspace() else i
    return word, angle, title, nonspace


def _markdown_destination(
    text: str, start: int, positions: tuple[list[int], ...],
) -> tuple[str, int] | None:
    word, angle, title, nonspace = positions
    size = len(text)
    if start >= size:
        return None
    if text[start] == "<":
        end = angle[start + 1]
        if end == size or text[end] != ">":
            return None
        target_start, target_end = start + 1, end
        end += 1
    else:
        end = word[start]
        if end == start:
            return None
        target_start, target_end = start, end
    if end < size and text[end].isspace():
        end = nonspace[end]
        if end >= size or text[end] != '"':
            return None
        end = title[end + 1]
        if end >= size or text[end] != '"':
            return None
        end += 1
    if end >= size or text[end] != ")":
        return None
    return text[target_start:target_end], end + 1


def _extract_links(text: str, distinguish_embeds: bool = False) -> list[Link]:
    """Extract links in source order, excluding fenced and inline code.

    Pair label brackets and index destination delimiters in bounded passes.
    Strip aliases, headings, block references and optional note extensions;
    preserve attachment extensions and omit external URLs and local anchors.
    """
    text = _without_code_spans(_prose(text))
    positions = _delimiter_positions(text)
    pairs = {}
    leaves = set()
    stack = []
    previous_bracket = -1
    for i, char in enumerate(text):
        if char == "\n":
            stack.clear()
        elif char == "[":
            stack.append(i)
            previous_bracket = i
        elif char == "]" and stack:
            start = stack.pop()
            pairs[start] = i
            if previous_bracket == start:
                leaves.add(start)
            previous_bracket = i

    links = []
    i = 0
    while i < len(text):
        end = pairs.get(i)
        if end is None:
            i += 1
            continue
        embed = i > 0 and text[i - 1] == "!"
        if (i + 1 in leaves and pairs.get(i + 1) == end - 1):
            target = text[i + 2:end - 1].split("|", 1)[0].strip()
            kind = "wikilink"
            i = end + 1
        elif text[end + 1:end + 2] == "(":
            destination = _markdown_destination(text, end + 2, positions)
            if destination is None:
                i += 1
                continue
            target, i = destination
            target = unquote(re.sub(r"\\([()\\])", r"\1", target))
            kind = "markdown"
        else:
            i += 1
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue
        if target.startswith("//"):
            continue
        target = _without_md(re.split(r"[#^]", target, maxsplit=1)[0].strip())
        if target:
            links.append(Link(target, "embed" if embed and not distinguish_embeds else kind))
    return links


def extract_links(text: str) -> list[Link]:
    """Extract targets and public link kinds in source order."""
    return _extract_links(text)


class _PathIndex:
    """Index unique original paths; None records an ambiguous lookup."""

    def __init__(self, paths: Iterable[str]):
        self.exact = {}
        self.basename = {}
        self.suffix = {}
        for path in dict.fromkeys(paths):
            name = _without_md(path).casefold()
            parts = name.split("/")
            for table, key in [(self.exact, name),
                               (self.basename, parts[-1])]:
                table[key] = None if key in table else path
            for i in range(1, len(parts)):
                key = "/".join(parts[i:])
                self.suffix[key] = None if key in self.suffix else path

    def resolve(self, target: str, source: str | None = None) -> str | None:
        key = _without_md(target.strip()).casefold()
        if not key:
            return None
        if source is not None and key.startswith(("./", "../")):
            key = posixpath.normpath(posixpath.join(
                posixpath.dirname(source), target.strip()))
            return self.exact.get(_without_md(key).casefold())
        for table in (self.exact, self.basename, self.suffix):
            if key in table:
                return table[key]
        return None


def resolve_link(target: str, paths: list[str]) -> str | None:
    """Resolve exact paths, basenames, then suffixes; reject ambiguities."""
    return _PathIndex(paths).resolve(target)


def build_graph(
    texts: Iterable[tuple[str, str]], all_paths: Iterable[str] | None = None
) -> dict:
    """Build resolved, distinct edges from readable bodies.

    Optional all_paths supplies the complete node and resolution namespace,
    including notes whose bodies could not be read.
    """
    notes = list(texts)
    paths = list(dict.fromkeys(
        all_paths if all_paths is not None else (path for path, _ in notes)
    ))
    index = _PathIndex(paths)
    edges = []
    seen = set()
    for source, text in notes:
        for link in _extract_links(text, distinguish_embeds=True):
            target = index.resolve(
                link.target, source if link.kind != "wikilink" else None)
            if target is not None and (source, target) not in seen:
                seen.add((source, target))
                edges.append({"from": source, "to": target})
    return {"nodes": paths, "edges": edges}
