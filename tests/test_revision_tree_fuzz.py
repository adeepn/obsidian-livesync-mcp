"""No JSON value fails the receive path with anything but a typed refusal.

A device's database either holds a record or rejects it; there is no third
outcome, so the port has none either: whatever a journal file carries,
:class:`revision_tree.RecordRefused` is the only exception the receive path
raises. The values are drawn from the shapes that exercise ECMAScript
coercion (numeric strings, overlong digit strings, objects standing for
hashes, keys named like array indices and like members of ``Object.prototype``)
and from nesting as deep as the decoder's budget admits and far beyond it:
the port's walks keep their own stacks, so depth is never a Python error.
"""

import json
import math
import random

import pytest

from revision_tree import (
    REVS_LIMIT, RecordRefused, collect_conflicts, collect_leaves, merge, parse_record, plan_file,
    revision_chain, stem, winning_rev, _concat, _root_to_leaf, _traverse,
)

SEED = 0x4A0B1C2D
CASES = 4000

KEYS = [
    "_id", "_rev", "_revisions", "_deleted", "_attachments", "_rev_tree", "_conflicts", "_access",
    "deleted", "type", "path", "children", "mtime", "size", "start", "ids", "length", "toString",
    "valueOf", "data", "stub", "digest", "content_type", "pos", "status", "constructor", "__proto__",
    "0", "1", "7", "01", "10", "4294967294", "4294967295", "9" * 30, "9" * 5000, "", "-", "a-b", "_custom",
]
TEXTS = [
    "", "a", "n", "n.md", "plain", "leaf", "h:x", "1-a", "1-a-b", "2-x", "-2", "+2", " 2 ", "\u20092\u2009",
    "\u00a0-3\u2028", "\ufeff2", "0x10", "0x" + "f" * 300, "1e400", "-1e400", "-Infinity", "NaN", "9" * 5000,
    "0" * 5000 + "1", "9007199254740993", "\u0661-a", "\ud83d\ude00", "\ue000", "\ud800", "\x00", "md5-abc",
]
NUMBERS = [0, 1, 2, 100, 2**31, 2**32 - 1, 2**32, 2**53, 2**53 + 1, -(2**53 + 1), 10**400, -(10**400),
           0.0, -0.0, 0.5, 1.5, 1e21, 1e-7, math.inf, -math.inf]


# One array to a step, or an array in an object to a step: the decoder's
# budget is 512 container levels.
DEPTHS = [1, 2, 60, 200, 420, 505, 512, 520, 900, 1500]


def nested(rng, depth: int):
    """A text wrapped in ``depth`` steps of arrays."""
    inner = rng.choice(TEXTS)
    for _ in range(depth):
        inner = [inner] if rng.random() < 0.8 else {"k": [inner]}
    return inner


def value(rng, depth=0):
    """A JSON value, with the non-finite doubles the decoder yields for 1e999."""
    kinds = ["text", "number", "bool", "null", "list", "dict"] if depth < 3 else [
        "text", "number", "bool", "null", "deep"]
    kind = rng.choice(kinds)
    if kind == "deep":
        return nested(rng, rng.choice(DEPTHS))
    if kind == "text":
        return rng.choice(TEXTS)
    if kind == "number":
        return rng.choice(NUMBERS)
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "list":
        return [value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(KEYS): value(rng, depth + 1) for _ in range(rng.randrange(5))}


def record(rng):
    doc = value(rng)
    if not isinstance(doc, dict):
        doc = {}
    # Records reach the port from the decoder with these two fields of these types.
    doc["_id"] = rng.choice(["n", "h:k", "other", "a-b"])
    doc["_rev"] = rng.choice(["1-a", "2-c", "1-x", ""])
    return doc


def nodes_of(tree):
    """Every revision a tree names, as a device spells it."""
    found = set()
    _traverse(tree, lambda is_leaf, pos, rev_id, opts: found.add(_concat(pos, "-", rev_id)))
    return found


@pytest.mark.parametrize("seed", [SEED, SEED + 1])
def test_every_value_is_held_or_refused(seed):
    rng = random.Random(seed)
    for index in range(CASES):
        doc = record(rng)
        if index % 4 == 0:
            # A deep value in every fourth record: beside the hash, as the hash
            # itself, or as the whole record (a chunk's revision is hashed over
            # every field).
            where = index % 12
            if where == 0:
                doc = nested(rng, rng.choice(DEPTHS))
            elif where == 4:
                doc["_revisions"] = {"start": 1, "ids": [nested(rng, rng.choice(DEPTHS))]}
            else:
                doc[rng.choice(["extra", "x", "_attachments"])] = nested(rng, rng.choice(DEPTHS))
        try:
            parsed = parse_record(doc, new_edits=rng.random() < 0.3)
        except RecordRefused:
            pass
        else:
            assert set(parsed) == {"id", "rev", "tree", "data", "raw_deleted"}, (index, doc)
        try:
            plan = plan_file([doc], lambda doc_id: None)
        except RecordRefused:
            continue
        assert plan.failed is None or plan.refusal is not None, index
        for row in plan.chunk_rows + plan.rows:
            body = row.body
            # A stored body keeps its nesting, as a device stores it.
            json.dumps(body)


@pytest.mark.parametrize("seed", [SEED + 2])
def test_every_tree_answers_its_readers(seed):
    """Every tree a held record names a winner the readers can ask about."""
    rng = random.Random(seed)
    held = 0
    for index in range(CASES):
        doc = record(rng)
        try:
            parsed = parse_record(doc, new_edits=False)
        except RecordRefused:
            continue
        try:
            plan = plan_file([doc], lambda doc_id: None)
        except RecordRefused:
            continue
        if plan.failed is not None:
            continue
        for tree in plan.trees.values():
            held += 1
            winner = winning_rev(tree)
            assert isinstance(winner, str), (index, doc, tree)
            collect_leaves(tree)
            collect_conflicts(tree)
            cut, stemmed = stem(tree, REVS_LIMIT)
            merged, _, _ = merge([], tree[0], REVS_LIMIT)
            assert stem(merged, REVS_LIMIT)[0] is not None
            # What stemming cut away is named, is gone from the tree it left,
            # and every path of that tree is within the limit; it stems no further.
            assert len(stemmed) == len(set(stemmed)) and not nodes_of(cut) & set(stemmed)
            assert all(len(history) <= REVS_LIMIT for _, history in _root_to_leaf(cut))
            assert stem(cut, REVS_LIMIT)[1] == []
            # The chain is asked for the winner and for any text.
            for rev in (winner, *rng.sample(TEXTS, 4)):
                try:
                    chain = revision_chain(tree, rev)
                except ValueError:
                    pass
                else:
                    assert isinstance(chain["ids"], list), (index, doc, tree, rev)
    assert held > CASES // 4, held
