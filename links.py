"""Pure-text link extraction and resolution over vault-relative paths."""

from collections import namedtuple
from collections.abc import Iterable
import re
from urllib.parse import unquote

Link = namedtuple("Link", ["target", "kind"])

_LINK = re.compile(
    r'(?P<wiki>!?\[\[(?P<target>[^\]\n]+)\]\])'
    r'|(?P<markdown>!?\[[^\]\n]*\]\('
    r'(?P<destination><[^>\n]*>|[^\s)]+)'
    r'(?:\s+"[^"\n]*")?\))'
)
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


def extract_links(text: str) -> list[Link]:
    """Extract inline wiki/Markdown links in text order, excluding fences.

    Strip aliases, headings, block references and the optional note extension.
    Attachment extensions are preserved. External URLs and local anchors do
    not identify vault files and are omitted.
    """
    links = []
    for match in _LINK.finditer(_prose(text)):
        if match["wiki"]:
            target = match["target"].split("|", 1)[0].strip()
            kind = "wikilink"
        else:
            target = unquote(match["destination"].strip("<>"))
            kind = "markdown"
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue
        if target.startswith("//"):
            continue
        target = _without_md(re.split(r"[#^]", target, maxsplit=1)[0].strip())
        if target:
            if match[0].startswith("!"):
                kind = "embed"
            links.append(Link(target, kind))
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
