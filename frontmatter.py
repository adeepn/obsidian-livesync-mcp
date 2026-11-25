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
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass

from ruamel.yaml.events import (AliasEvent, ScalarEvent, MappingStartEvent,
    SequenceStartEvent, MappingEndEvent, SequenceEndEvent)

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import CommentToken
from ruamel.yaml.composer import Composer
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.representer import RoundTripRepresenter

_DELIMITER = "---"
# Sentinel key used to make ruamel attach comments of a comments-only
# block to a real mapping (a bare load of such a block returns None and
# drops the comments).
_SENTINEL_KEY = "__frontmatter_placeholder__"


@dataclass(frozen=True)
class FrontmatterLimits:
    """Property projection budgets; the stored note is never limited here."""

    max_nodes: int = 1_000_000
    max_depth: int = 200


frontmatter_limits: ContextVar[FrontmatterLimits] = ContextVar(
    "frontmatter_limits", default=FrontmatterLimits())


class FrontmatterError(ValueError):
    """Closed, value-free property projection failure."""

    def __init__(self, reason="invalid YAML", *, runtime_depth=False):
        self.reason = reason if reason in {
            "nodes", "depth", "alias expansion", "invalid YAML", "mapping required"} else "invalid YAML"
        variable = {"nodes": "LIVESYNC_FRONTMATTER_MAX_NODES",
                    "depth": "LIVESYNC_FRONTMATTER_MAX_DEPTH",
                    "alias expansion": "LIVESYNC_FRONTMATTER_MAX_NODES"}.get(self.reason)
        if runtime_depth:
            remedy = "The YAML parser reached its interpreter recursion ceiling; "
        elif self.reason == "alias expansion":
            remedy = ("Cyclic aliases have no finite JSON representation; for finite "
                      f"expansions raise {variable} and restart, or ")
        else:
            remedy = (f"Raise {variable} and restart, or " if variable else "")
        self.public_message = (
            f"Error: frontmatter {self.reason}; the note itself is intact and synced. "
            "Only properties/tags tools declined. " + remedy +
            "fetch or edit the raw note text.")
        super().__init__(self.public_message)

    def for_creation(self):
        """Render the same closed fields before any note has been published."""
        return self.public_message.replace(
            "the note itself is intact and synced.",
            "note creation declined before publication; no note was created.")


def _preflight(fm_raw: str) -> None:
    """Count expanded event nodes before constructing round-trip containers.

    LiveSync syncs Blob bytes without interpreting YAML (commonlib
    managers/EntryManager/EntryManagerImpls.ts:253-280; size predicate
    services/base/VaultService.ts:73-81). These are peer JSON tool budgets,
    not a vault setting or a claim about Obsidian's host YAML parser.
    A completed anchor carries its expansion count/height, so exponentially
    repeated aliases are refused in O(source events), without expanding them.
    """
    limits = frontmatter_limits.get()
    stack, anchors = [], {}

    def append(count, height, aliased):
        if count > limits.max_nodes:
            raise FrontmatterError("alias expansion" if aliased else "nodes")
        if len(stack) + height > limits.max_depth:
            raise FrontmatterError("depth")
        if stack:
            frame = stack[-1]
            frame[0] += count
            frame[1] = max(frame[1], height + 1)
            frame[2] |= aliased
            if frame[0] > limits.max_nodes:
                raise FrontmatterError("alias expansion" if frame[2] else "nodes")

    try:
        for event in _rt_yaml().parse(fm_raw):
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                if len(stack) > limits.max_depth:
                    raise FrontmatterError("depth")
                binding = None
                if event.anchor:
                    # Each declaration has its own completion record. A nested
                    # rebind shadows the outer name immediately and stays the
                    # latest binding when that outer container finishes.
                    binding = [None, None]
                    anchors[event.anchor] = binding
                stack.append([1, 0, False, binding])
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                count, height, aliased, anchor = stack.pop()
                if anchor is not None:
                    anchor[:] = count, height
                append(count, height, aliased)
            elif isinstance(event, ScalarEvent):
                if event.anchor:
                    anchors[event.anchor] = [1, 0]
                append(1, 0, False)
            elif isinstance(event, AliasEvent):
                if event.anchor not in anchors:
                    raise FrontmatterError("invalid YAML")
                if anchors[event.anchor][0] is None:
                    # Recursive YAML has no finite JSON representation. ruamel's
                    # round-trip constructor otherwise substitutes None here.
                    raise FrontmatterError("alias expansion")
                append(*anchors[event.anchor], True)
    except YAMLError:
        raise FrontmatterError("invalid YAML") from None


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


class _CompactComposer(Composer):
    """Share immutable implicit tags instead of one Tag/cache per scalar."""

    def __init__(self, loader=None):
        super().__init__(loader)
        self.warn_double_anchors = False
        self._implicit_tags = {}

    def compose_node(self, parent, index):
        # ruamel.yaml 0.18.17 composer.py:105-138; resolver implicit tags
        # have no handle/handles and are only read by the constructor.
        node = super().compose_node(parent, index)
        tag = node.ctag
        if tag.handle is None and tag.handles is None:
            node.ctag = self._implicit_tags.setdefault(tag.suffix, tag)
        return node


class _CompactSequence(CommentedSeq):
    def _yaml_set_idx_line_col(self, key, data):
        # ruamel.yaml 0.18.17 constructor.py:1190-1224 stores one index
        # dictionary entry/list for every sequence item. No peer consumer
        # reads those coordinates: source edits use YAML event token spans.
        # Comments/styles/anchors remain the upstream constructor's work.
        pass


class _CompactConstructor(RoundTripConstructor):
    def construct_yaml_seq(self, node):
        # Same generator contract as ruamel.yaml 0.18.17
        # constructor.py:1452-1459, using the coordinate-free sequence.
        data = _CompactSequence()
        data._yaml_set_line_col(node.start_mark.line, node.start_mark.column)
        yield data
        data.extend(self.construct_rt_sequence(node, data))
        self.set_collection_style(data, node)


class _CompactRepresenter(RoundTripRepresenter):
    pass


_CompactConstructor.add_constructor('tag:yaml.org,2002:seq', _CompactConstructor.construct_yaml_seq)
_CompactRepresenter.add_representer(_CompactSequence, RoundTripRepresenter.represent_list)


def _rt_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.Composer = _CompactComposer
    yaml.Constructor = _CompactConstructor
    yaml.Representer = _CompactRepresenter
    return yaml


def _parse(fm_raw: str) -> CommentedMap:
    """Parse frontmatter YAML into a round-trip mapping.

    An empty or comments-only block yields an empty mapping; comments are
    kept attached to it so that adding the first property preserves them.
    """
    _preflight(fm_raw)
    try:
        data = _rt_yaml().load(fm_raw)
    except RecursionError:
        raise FrontmatterError("depth", runtime_depth=True) from None
    except YAMLError:
        raise FrontmatterError("invalid YAML") from None
    if data is None:
        return _empty_with_comments(fm_raw)
    if not isinstance(data, CommentedMap):
        raise FrontmatterError("mapping required")
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


def _to_plain(value, *, limits=None):
    """Memoised plain projection with expanded-node accounting and cycle guard."""
    limits = limits or frontmatter_limits.get()
    memo, active = {}, set()

    def convert(node, depth):
        if depth > limits.max_depth:
            raise FrontmatterError("depth")
        container = isinstance(node, (dict, list, tuple))
        identity = id(node)
        if container and identity in active:
            raise FrontmatterError("alias expansion")
        if container and identity in memo:
            result, count, height = memo[identity]
            if depth + height > limits.max_depth:
                raise FrontmatterError("depth")
            return result, count, height, True
        if not container:
            if isinstance(node, bool) or node is None:
                result = node
            elif isinstance(node, int):
                result = int(node)
            elif isinstance(node, float):
                result = float(node)
            else:
                result = str(node)
            return result, 1, 0, False
        active.add(identity)
        count, height, aliased = 1, 0, False
        result = {} if isinstance(node, dict) else []

        def child(item):
            nonlocal count, height, aliased
            plain, size, levels, reused = convert(item, depth + 1)
            count += size
            height = max(height, levels + 1)
            aliased |= reused
            if count > limits.max_nodes:
                raise FrontmatterError("alias expansion" if aliased else "nodes")
            return plain

        if isinstance(node, dict):
            for key, item in node.items():
                result[str(child(key))] = child(item)
        else:
            for item in node:
                result.append(child(item))
        active.remove(identity)
        memo[identity] = result, count, height
        return result, count, height, aliased

    try:
        return convert(value, 0)[0]
    except RecursionError:
        raise FrontmatterError("depth", runtime_depth=True) from None


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


_MERGE_KEY = object()


def _comment_line_starts(raw, comments):
    """Find actual standalone comment lines from parser comment tokens.

    A CommentToken may start inline and include subsequent comment lines.
    Scalar contents resembling comments never become CommentTokens, which
    distinguishes indented YAML comments from ``#`` inside a block scalar.
    """
    pending, starts = [comments], set()
    while pending:
        token = pending.pop()
        if isinstance(token, (list, tuple)):
            pending.extend(token)
        elif isinstance(token, CommentToken):
            position = raw.rfind('\n', 0, token.start_mark.index) + 1
            first = True
            while position < len(raw):
                end = raw.find('\n', position)
                end = len(raw) if end == -1 else end
                line = raw[position:end]
                if line.lstrip().startswith('#'):
                    starts.add(position)
                elif line.strip() and not first:
                    break
                first = False
                position = end + 1
    return starts


def _key_spans(raw, data):
    """Read explicit key/value token spans, without inherited merge keys.

    Event positions retain the explicit ``?`` prefix of complex keys and
    exclude a flow root's closing brace. Node .lc coordinates do neither.
    """
    explicit = iter(key for key, _ in data.non_merged_items())
    depth, key_turn, current, start = 0, True, None, 0
    spans, flow, closing, comment_lines = {}, False, len(raw), set()
    for event in _rt_yaml().parse(raw):
        comment_lines.update(_comment_line_starts(raw, getattr(event, "comment", None)))
        begins = isinstance(event, (ScalarEvent, AliasEvent, MappingStartEvent, SequenceStartEvent))
        if depth == 0 and isinstance(event, MappingStartEvent):
            flow = event.flow_style
        elif depth == 1 and begins:
            if key_turn:
                current = (_MERGE_KEY if isinstance(event, ScalarEvent)
                           and event.value == "<<" and event.style is None
                           and event.tag in (None, "tag:yaml.org,2002:merge")
                           else next(explicit))
                start = event.start_mark.index
                line_start = raw.rfind("\n", 0, start) + 1
                if raw[line_start:start].strip() == "?":
                    start = line_start
            key_turn = not key_turn
            if key_turn and isinstance(event, (ScalarEvent, AliasEvent)):
                spans[current] = (start, event.end_mark.index)
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1
            if depth == 1 and key_turn:
                spans[current] = (start, event.end_mark.index)
            if depth == 0:
                closing = event.start_mark.index
    return spans, bool(flow), closing, comment_lines


def _dump_preserving_keys(raw, original_spans, original_flow, closing, comment_lines, before, data, after):
    """Replace changed key tokens in the original source, preserving trivia."""
    rendered = _dump(data)
    if not original_spans:
        return rendered
    rendered_spans, _, _, _ = _key_spans(rendered, data)
    replacements = []
    added = []
    for key, (start, end) in original_spans.items():
        if key is _MERGE_KEY:
            continue
        if key in rendered_spans:
            if not _plain_equal(before[str(_to_plain(key))], after[str(_to_plain(key))]):
                new_start, new_end = rendered_spans[key]
                replacement = rendered[new_start:new_end]
                if raw[start:end].endswith('\n') and not replacement.endswith('\n'):
                    replacement += '\n'
                replacements.append((start, end, replacement))
    # Delete maximal adjacent source-key runs. Flow punctuation belongs to
    # the whole run; per-key comma intervals overlap for a removed suffix.
    ordered = list(original_spans)
    index = 0
    while index < len(ordered):
        key = ordered[index]
        if key is _MERGE_KEY or key in rendered_spans:
            index += 1
            continue
        first = index
        while index < len(ordered) and ordered[index] is not _MERGE_KEY and ordered[index] not in rendered_spans:
            index += 1
        start = original_spans[ordered[first]][0]
        end = original_spans[ordered[index - 1]][1]
        if original_flow:
            if index < len(ordered):
                end = original_spans[ordered[index]][0]
            elif first > 0:
                previous_end = original_spans[ordered[first - 1]][1]
                comma = raw.find(',', previous_end, start)
                if comma != -1:
                    start = comma
            else:
                end = closing
        else:
            # Block collection end marks can already point at the next key.
            # Use its start directly, never consume its following newline.
            end = original_spans[ordered[index]][0] if index < len(ordered) else len(raw)
            # Collection end marks can include following standalone comments.
            # Those source bytes belong outside the deleted value: retain the
            # trailing actual comment/blank-line run before the next key.
            # Token classification excludes indented block-scalar content.
            while end > start:
                search_end = end - 1 if raw[end - 1] == '\n' else end
                line_start = max(start, raw.rfind('\n', start, search_end) + 1)
                line = raw[line_start:end]
                if line.strip() and line_start not in comment_lines:
                    break
                end = line_start
        replacements.append((start, end, ''))
    for key, (start, end) in rendered_spans.items():
        if key not in original_spans:
            added.append(rendered[start:end])
    if added:
        if original_flow:
            surviving = [span for key, span in original_spans.items() if key in rendered_spans]
            if surviving:
                tail = raw[surviving[-1][1]:closing].lstrip()
                prefix = ' ' if tail.startswith(',') else ', '
            else:
                prefix = ''
            replacements.append((closing, closing, prefix + ', '.join(added)))
        else:
            prefix = '' if raw.endswith('\n') else '\n'
            replacements.append((len(raw), len(raw), prefix + '\n'.join(added) + '\n'))
    for start, end, replacement in sorted(replacements, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    try:
        reparsed = _to_plain(_parse(raw))
    except FrontmatterError:
        raise FrontmatterError("alias expansion") from None
    if not _plain_equal(reparsed, after):
        raise FrontmatterError("alias expansion")
    return raw


def _apply(text: str, editor: Callable[[CommentedMap], None]) -> str:
    """Edit the frontmatter mapping in place and re-serialize.

    Returns the original ``text`` object when the parsed properties did
    not change, so no-op calls never reformat anything.
    """
    fm_raw, body, has_block = _split_block(text)
    if not has_block:
        return text
    data = _parse(fm_raw)
    original_spans, original_flow, closing, comment_lines = _key_spans(fm_raw, data)
    before = _to_plain(data)
    editor(data)
    after = _to_plain(data)
    if _plain_equal(after, before):
        return text
    result = _new_block(_dump_preserving_keys(fm_raw, original_spans, original_flow, closing, comment_lines, before, data, after), body)
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
        _to_plain(data)
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
        # An alias used as tags must not edit its earlier anchor's property.
        # If tags itself defines the anchor, later aliases keep following it.
        for key, item in data.non_merged_items():
            if key == 'tags':
                break
            pending, seen = [item], set()
            found = False
            while pending:
                candidate = pending.pop()
                if candidate is value:
                    found = True
                    break
                if id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                if isinstance(candidate, dict):
                    pending.extend(candidate.values())
                elif isinstance(candidate, (list, tuple)):
                    pending.extend(candidate)
            if found:
                value = deepcopy(value)
                value.yaml_set_anchor(None)
                break
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
        _to_plain(data)
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
