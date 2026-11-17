"""Pure-text link extraction and resolution over vault-relative paths."""

from collections import namedtuple
from collections.abc import Iterable
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
    for i in range(size - 1, -1, -1):
        char = text[i]
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


def extract_links(text: str) -> list[Link]:
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
            target = unquote(target)
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
            links.append(Link(target, "embed" if embed else kind))
    return links


def resolve_link(target: str, paths: list[str]) -> str | None:
    """Return the original vault path for an unambiguous target.

    Prefer exact paths, then basenames, then component-aligned path suffixes
    (the shortest unique path form). Ambiguous matches return None rather
    than depending on input order. The optional .md suffix is ignored.
    """
    key = _without_md(target.strip()).casefold()
    if not key:
        return None
    candidates = list(dict.fromkeys(paths))
    normalized = [(path, _without_md(path).casefold()) for path in candidates]
    for matches in (
        [path for path, name in normalized if name == key],
        [path for path, name in normalized if name.rsplit("/", 1)[-1] == key],
        [path for path, name in normalized if name.endswith("/" + key)],
    ):
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def build_graph(texts: Iterable[tuple[str, str]]) -> dict:
    """Build a stable graph containing only resolved, distinct directed edges."""
    notes = list(texts)
    paths = list(dict.fromkeys(path for path, _ in notes))
    edges = []
    seen = set()
    for source, text in notes:
        for link in extract_links(text):
            target = resolve_link(link.target, paths)
            if target is not None and (source, target) not in seen:
                seen.add((source, target))
                edges.append({"from": source, "to": target})
    return {"nodes": paths, "edges": edges}
