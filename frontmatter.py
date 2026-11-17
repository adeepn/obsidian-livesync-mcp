"""Round-trip YAML frontmatter handling for Obsidian notes.

All parsing and serialization goes through ruamel.yaml round-trip mode, so
an edit touches only the requested keys: quoting style, comments, key
order, block scalars and scalar spellings (``'42'``, ``01234``) of
neighbouring properties survive byte-identically (audit D5/D17).

Frontmatter delimiters are recognized only as whole ``---`` lines at
column 0 (audit D6); ``---`` inside a value is data. Tags stored as a
scalar normalize to a list on tag edits (audit D12). Every mutator returns
the original string object when the request changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

_DELIMITER = "---"
# Sentinel key used to make ruamel attach comments of a comments-only
# block to a real mapping (a bare load of such a block returns None and
# drops the comments).
_SENTINEL_KEY = "__frontmatter_placeholder__"


class FrontmatterError(ValueError):
    """Raised when the frontmatter block is not parseable YAML mapping."""


def _is_delimiter_line(line: str) -> bool:
    # A delimiter is a whole line at column 0; trailing whitespace is
    # tolerated, leading whitespace is not (that would be block-scalar
    # content). 'summary: before---after' is data (audit D6).
    return line.rstrip() == _DELIMITER


def _split_block(text: str) -> tuple[str, str, bool]:
    """Split into ``(frontmatter_raw, body, has_block)``.

    ``frontmatter_raw`` is the YAML between the delimiters, without the
    ``---`` lines themselves. A block only counts when the opening ``---``
    is the first line and a closing whole-line ``---`` follows.
    """
    if not text.startswith(_DELIMITER):
        return "", text, False
    first_nl = text.find("\n")
    if first_nl == -1 or not _is_delimiter_line(text[:first_nl]):
        return "", text, False
    pos = first_nl + 1
    while pos <= len(text):
        nl = text.find("\n", pos)
        line = text[pos:] if nl == -1 else text[pos:nl]
        if _is_delimiter_line(line):
            fm_raw = text[first_nl + 1 : pos]
            body = "" if nl == -1 else text[nl + 1 :]
            return fm_raw, body, True
        if nl == -1:
            return "", text, False  # unterminated block is not frontmatter
        pos = nl + 1
    return "", text, False


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_raw, body)``; ``("", text)`` without a block."""
    fm_raw, body, _ = _split_block(text)
    return fm_raw, body


def _rt_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def _parse(fm_raw: str) -> CommentedMap:
    """Parse frontmatter YAML into a round-trip mapping.

    An empty or comments-only block yields an empty mapping; comments are
    kept attached to it so that adding the first property preserves them.
    """
    try:
        data = _rt_yaml().load(fm_raw)
    except YAMLError as exc:
        raise FrontmatterError(f"invalid frontmatter YAML: {exc}") from exc
    if data is None:
        return _empty_with_comments(fm_raw)
    if not isinstance(data, CommentedMap):
        raise FrontmatterError("frontmatter is not a property mapping")
    return data


def _empty_with_comments(fm_raw: str) -> CommentedMap:
    """Empty mapping carrying the comments of a comments-only block.

    ruamel drops the comments when such a block is loaded on its own, so
    the block is re-loaded with a sentinel key and the key is deleted
    again; the comments stay attached to the mapping.
    """
    if "#" not in fm_raw:
        return CommentedMap()
    source = fm_raw if fm_raw.endswith("\n") else fm_raw + "\n"
    try:
        data = _rt_yaml().load(f"{source}{_SENTINEL_KEY}: null\n")
        del data[_SENTINEL_KEY]
    except (YAMLError, KeyError, TypeError):
        return CommentedMap()  # pathological block: comments not retrievable
    return data


def _to_plain(value, *, _seen=None, _budget=None, _depth=0):
    """Convert round-trip scalars/containers to plain Python values."""
    if _seen is None:
        _seen, _budget = set(), [10000]
    _budget[0] -= 1
    if _budget[0] < 0:
        raise FrontmatterError("frontmatter exceeds maximum nodes")
    if _depth > 32:
        raise FrontmatterError("frontmatter exceeds maximum depth")
    if isinstance(value, (dict, list, tuple)):
        if id(value) in _seen:
            raise FrontmatterError("YAML aliases are not supported in frontmatter")
        _seen.add(id(value))
    def convert(item):
        return _to_plain(item, _seen=_seen, _budget=_budget, _depth=_depth + 1)
    if isinstance(value, dict):
        return {str(convert(key)): convert(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [convert(item) for item in value]
    if isinstance(value, bool):  # before int: bool is an int subclass
        return value
    if isinstance(value, int):  # keeps '01234' as 1234, '0x1F' as 31
        return int(value)
    if isinstance(value, float):
        return float(value)
    if value is None:
        return None
    return str(value)  # quoted/plain strings, dates, timestamps


def _dump(data: CommentedMap) -> str:
    out = StringIO()
    _rt_yaml().dump(data, out)
    return out.getvalue()


def _new_block(fm_yaml: str, body: str) -> str:
    return f"{_DELIMITER}\n{fm_yaml}{_DELIMITER}\n{body}"


def _plain_equal(left, right) -> bool:
    """Type-sensitive equality for plain property trees.

    Python equality conflates ``True == 1`` and ``1 == 1.0``; a no-op
    detection built on it would swallow real bool/int/float type changes.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _plain_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _plain_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _apply(text: str, editor: Callable[[CommentedMap], None]) -> str:
    """Edit the frontmatter mapping in place and re-serialize.

    Returns the original ``text`` object when the parsed properties did
    not change, so no-op calls never reformat anything.
    """
    fm_raw, body, has_block = _split_block(text)
    if not has_block:
        return text
    data = _parse(fm_raw)
    before = _to_plain(data)
    editor(data)
    if _plain_equal(_to_plain(data), before):
        return text
    result = _new_block(_dump(data), body)
    return text if result == text else result


def get_properties(text: str) -> dict:
    """Return the frontmatter properties as a plain dict ({} if none)."""
    fm_raw, _, has_block = _split_block(text)
    if not has_block or not fm_raw.strip():
        return {}
    return _to_plain(_parse(fm_raw))


def set_properties(text: str, props: dict) -> str:
    """Set ``props`` in the frontmatter, creating the block if needed.

    Existing keys are overwritten, everything else is preserved verbatim.
    """
    if not props:
        return text
    _, body, has_block = _split_block(text)
    if not has_block:
        data = CommentedMap()
        for key, value in props.items():
            data[str(key)] = value
        return _new_block(_dump(data), body)

    def editor(data: CommentedMap) -> None:
        for key, value in props.items():
            data[str(key)] = value

    return _apply(text, editor)


def delete_properties(text: str, keys: list[str]) -> str:
    """Remove ``keys`` from the frontmatter; missing keys change nothing."""

    def editor(data: CommentedMap) -> None:
        for key in keys:
            if str(key) in data:
                del data[str(key)]

    return _apply(text, editor) if keys else text


def _coerce_tags(seq: CommentedSeq) -> None:
    """Coerce non-string tag entries to str in place (audit D12).

    String nodes (quoted or plain) keep their ruamel node so quoting
    style survives; only genuinely non-string scalars are replaced.
    """
    for i, item in enumerate(seq):
        if not isinstance(item, str):
            seq[i] = str(item)


def _current_tags(data: CommentedMap) -> CommentedSeq | None:
    """Existing tags as a list; a scalar value wraps into one (audit D12)."""
    value = data.get("tags")
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return value
    seq = CommentedSeq()
    seq.append(value if isinstance(value, str) else str(value))
    return seq


def add_tags(text: str, tags: list[str]) -> str:
    """Append ``tags`` not already present; creates the block if needed."""
    if not tags:
        return text
    _, body, has_block = _split_block(text)
    if not has_block:
        data = CommentedMap()
        seq = CommentedSeq()
        seq.extend(str(tag) for tag in tags)
        data["tags"] = seq
        return _new_block(_dump(data), body)

    def editor(data: CommentedMap) -> None:
        value = data.get("tags")
        current = _current_tags(data)
        if current is None:
            current = CommentedSeq()
        existing = {str(item) for item in current}
        added = [tag for tag in dict.fromkeys(tags) if str(tag) not in existing]
        if not added:
            return  # all present: keep the original spelling untouched
        _coerce_tags(current)  # audit D12: the tags property is list[str]
        current.extend(str(tag) for tag in added)
        if value is not current:  # scalar wrapped into a fresh list
            data["tags"] = current

    return _apply(text, editor)


def remove_tags(text: str, tags: list[str]) -> str:
    """Drop ``tags`` from the tags property; absent tags change nothing."""
    if not tags:
        return text

    def editor(data: CommentedMap) -> None:
        value = data.get("tags")
        current = _current_tags(data)
        if current is None:
            return
        wanted = {str(tag) for tag in tags}
        drop = [
            i
            for i, item in enumerate(current)
            if not isinstance(item, (dict, list)) and str(item) in wanted
        ]
        if not drop:
            return  # nothing matched; keep the original spelling
        for i in reversed(drop):  # in place: flow/block style survives
            del current[i]
        _coerce_tags(current)  # audit D12: the tags property is list[str]
        if value is not current:  # scalar wrapped into a fresh list
            data["tags"] = current

    return _apply(text, editor)


def insert_after_frontmatter(text: str, insertion: str) -> str:
    """Insert ``insertion`` right after the closing ``---`` (audit D7).

    Without a frontmatter block the insertion goes to the top of the note.
    """
    if not insertion:
        return text
    _, body, has_block = _split_block(text)
    if not has_block:
        return insertion + text
    head = text[: len(text) - len(body)]
    if not head.endswith("\n"):  # closing '---' at EOF: keep it a whole line
        head += "\n"
    return head + insertion + body
