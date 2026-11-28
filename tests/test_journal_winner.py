"""The journal backend holds the revision a device holds.

``tests/fixtures/journal_winner_upstream.json.gz`` comes from
``scripts/extract_winner_vectors.mjs``. It receives every file through the
plugin's own decoder and selection functions (``journalRecords.ts``,
``journalReceive.ts``) and the published PouchDB 9.0.0 database: the IDB
adapter, the one devices default to, runs unmodified on a pinned
implementation of IndexedDB, so the order its transaction stores the records
of a batch in is its own; the unmodified memory adapter is run beside it as
a cross-check; the cases where it parts ways with IDB are recorded on the
case.

After every file, applied or refused, the store's winning revision, its
leaves in device order, its conflicts, the chain a device reports, the stored
body and whether the file applies at all must equal what the device holds. A
refused file leaves in the store exactly what a device committed before it
refused. The fixture also records the plugin's own write path over each
starting state and the platform's forgiving base64 decode.

Numbers are converted as the engine converts them: the fixture records
``Number`` and ``parseInt`` of a corpus of strings and every character
``Number`` trims, and whole files whose positions, hashes and number literals
are chosen around those conversions.
"""

import gzip
import json
import math
from pathlib import Path

import pytest

from diagnostic_codes import DiagnosticCode
from journal_codec import deflate_journal, inflate_journal, parse_journal, serialize_journal
from revision_tree import _JS_WHITESPACE, _decode_base64, _parse_int, _string_number
from journal_store import JournalStore, _db_key
from livesync_format import PATH_IDENTITY_LEGACY, chunk_id_for, path2id
from revision_tree import REVS_LIMIT, RecordRefused, collect_conflicts, collect_leaves, is_deleted, winning_rev
from storage_base import NoteNotFound, StorageError
from tests.test_storage_journal import write_backend

FIXTURE = json.loads(
    gzip.decompress((Path(__file__).parent / "fixtures/journal_winner_upstream.json.gz").read_bytes())
)
CASES = {case["name"]: case for case in FIXTURE["cases"]}
# A served body the store refused to hand out.
REFUSED = object()


def views(case):
    return [observed for file_views in case["views"] for observed in file_views]


# Devices accept these into a tree whose behaviour then depends on the
# JavaScript engine and the adapter; the peer refuses them (docs/peer-limitations.md).
# Both adapters store a tree as JSON, where a position that is not finite becomes
# null; the observation marks such a view. The stored position cannot be told from
# Infinity or NaN through JSON; unit tests cover both refusals.
ENGINE_DEFINED = {"empty-ids": "missing-revision-id"}
ENGINE_DEFINED.update(dict.fromkeys(
    (case["name"] for case in FIXTURE["cases"] if any(view.get("unbounded") for view in views(case))),
    "non-finite-number"))


def as_doubles(value):
    """A body's numbers as the engine holds them, over a stack of its own.

    A double JSON cannot carry is spelled as the fixture spells it. An integer
    beyond double precision is rounded, as a device holds it; the fixture keeps
    the integer spelling of that double.
    """
    if not isinstance(value, (dict, list)):
        return _as_double(value)
    root = {} if isinstance(value, dict) else [None] * len(value)
    stack = [(value, root)]
    while stack:
        source, target = stack.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in items:
            if isinstance(item, (dict, list)):
                clone = {} if isinstance(item, dict) else [None] * len(item)
                target[key] = clone
                stack.append((item, clone))
            else:
                target[key] = _as_double(item)
    return root


def _as_double(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "-Infinity" if value < 0 else "Infinity"
    if isinstance(value, int) and not -2**53 <= value <= 2**53:
        return float(value)
    return value


def as_converted(spelled):
    """A number of the fixture, or the double its spelling stands for."""
    if isinstance(spelled, str):
        return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "-0": -0.0}[spelled]
    return float(spelled)


def same_double(actual, expected):
    if math.isnan(expected):
        return math.isnan(actual)
    return actual == expected and math.copysign(1, actual) == math.copysign(1, expected)


def spellings(value):
    """The doubles of a recorded body that JSON could not carry, in order."""
    if value in ("Infinity", "-Infinity"):
        return [value]
    if isinstance(value, (list, dict)):
        entries = value.values() if isinstance(value, dict) else value
        return [spelling for entry in entries for spelling in spellings(entry)]
    return []


def same_double(actual, expected):
    if math.isnan(expected):
        return math.isnan(actual)
    return actual == expected and math.copysign(1, actual) == math.copysign(1, expected)


def file_name(index):
    return f"170000{index:09d}-docs.jsonl.gz"


def replay(store, index, records):
    """Apply one file as the backend does: through the peer's own decoder.

    ``records`` is a list of records, written as JSON lines, or the frames of
    a file as text when a case exercises the compact chunk records.
    """
    frames = records if isinstance(records, str) else "".join(json.dumps(record) + "\n" for record in records)
    journal = parse_journal(frames.encode("utf-8"), name=file_name(index))
    store.apply_journal(file_name(index), journal.chunks, journal.docs, records=journal.records)


def assert_holds(store, observed):
    """The store's tree and bodies equal one observation of the device."""
    doc_id = observed["id"]
    tree = store._tree(doc_id)
    if observed.get("tree", "present") is None:
        assert tree is None
        assert store.winner(doc_id) is None and store.leaves(doc_id) == []
        return
    assert tree is not None, doc_id
    winner = winning_rev(tree)
    assert winner == observed["winner"]
    # Only a device holds a position that is not finite (ENGINE_DEFINED above).
    assert observed["unbounded"] is False, doc_id
    # isDeleted answers undefined for a hash the revision does not name.
    assert is_deleted(tree, winner) == observed.get("deleted", False)
    assert [list(leaf) for leaf in collect_leaves(tree)] == observed["leaves"]
    assert collect_conflicts(tree) == observed["conflicts"]
    # A leaf's body is read as the IndexedDB adapter reads any body: its _rev
    # is decoded off the slot's key, and a leaf without a stored body keeps the
    # tree's name.
    def reported(name):
        stored = store._body(doc_id, name)
        return name if stored is None else stored["_rev"]

    def flag(name, deleted):
        # A leaf's body keeps the flag it was stored with, as a device stores it:
        # over a shared slot that is another document's.
        stored = store._body(doc_id, name)
        return bool(deleted or (stored is not None and stored.get("_deleted")))

    assert [(leaf["_rev"], bool(leaf.get("_deleted"))) for leaf in store.leaves(doc_id)] == [
        (reported(name), flag(name, deleted)) for name, deleted in observed["leaves"]]
    assert [leaf["_rev"] for leaf in store.conflicts(doc_id)] == [reported(name) for name in observed["conflicts"]]
    # The plugin's writer and deleter read the base with a raw get, which never reads the
    # tree: the slot's body, with the identity and the flags it was stored with, or missing.
    try:
        raw = store.raw_get(doc_id)
    except ValueError:
        raw = REFUSED
    plain = observed["plain"]
    if "error" in plain:
        assert raw is None, (doc_id, plain)
    else:
        assert raw is not None and (raw["_id"], raw["_rev"], raw.get("_deleted") is True) == (
            plain["id"], plain["rev"], plain["deleted"]), doc_id
    # A case may record which revision of the tree still has a body: a merge stems a
    # revision away from one path while a shorter path keeps it, and the adapter
    # compacts only what no path keeps.
    for rev, present in observed.get("slots", {}).items():
        assert (store._body(doc_id, rev) is not None) == present, (doc_id, rev)
    try:
        body = store.winner(doc_id)
    except ValueError:
        body = REFUSED
    read = observed["read"]
    if "rev" in read:
        # A device stamps _deleted on a read from the tree; the store does the same.
        def without_flag(doc):
            return {key: value for key, value in doc.items() if key != "_deleted"}

        assert body is not None and without_flag(as_doubles(body)) == without_flag(as_doubles(read["body"]))
        assert (body["_rev"], body["_id"]) == (read["rev"], read["body"]["_id"])
        assert store.revision_chain(doc_id, body["_rev"]) == read["revisions"]
    elif read["error"] in ("deleted", "missing"):
        # What the device cannot read is not served here under another name.
        assert body is None or body is REFUSED or body.get("_deleted") is True
    else:
        # The device cannot name the winning revision in its tree; the store
        # refuses to serve such a note as corrupt, for the same reason.
        assert body is REFUSED


@pytest.mark.parametrize("name", [case["name"] for case in FIXTURE["cases"] if case["name"] not in ENGINE_DEFINED],
                         ids=str)
def test_store_holds_what_the_device_holds_after_each_file(name, tmp_path):
    case = CASES[name]
    store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    try:
        assert len(case["views"]) == len(case["files"])
        for index, file in enumerate(case["files"]):
            if file["outcome"] == "parse":
                # The plugin's decoder refuses the file; so does the peer's codec.
                with pytest.raises(ValueError) as refused:
                    replay(store, index, file["records"])
                assert not isinstance(refused.value, RecordRefused)
            elif file["outcome"] is not None:
                with pytest.raises(RecordRefused) as refused:
                    replay(store, index, file["records"])
                assert refused.value.stage == file["outcome"]
                assert not store.processed(file_name(index))
            else:
                replay(store, index, file["records"])
                assert store.processed(file_name(index))
            for observed in case["views"][index]:
                assert_holds(store, observed)
    finally:
        store.close()


@pytest.mark.parametrize("name", sorted(ENGINE_DEFINED))
def test_refuses_what_a_device_holds_as_engine_defined(name, tmp_path):
    case = CASES[name]
    assert [file["outcome"] for file in case["files"]] == [None]
    store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    try:
        with pytest.raises(RecordRefused) as refused:
            replay(store, 0, case["files"][0]["records"])
        assert refused.value.reason == ENGINE_DEFINED[name]
        # The refusal precedes the metadata batch: what a device committed before it
        # stays, the document it holds at such a position is not written at all.
        assert refused.value.stage == "metadata-write"
        for observed in case["views"][0]:
            if observed.get("unbounded") or ENGINE_DEFINED[name] == "missing-revision-id":
                assert store._tree(observed["id"]) is None and store.winner(observed["id"]) is None
            else:
                assert_holds(store, observed)
        assert not store.processed(file_name(0))
    finally:
        store.close()


def test_engine_defined_positions_come_in_every_numeric_spelling():
    assert ENGINE_DEFINED["missing-start"] == "non-finite-number"
    # The third-round review's cases are held by a device at their converted positions.
    assert "start-negative" in CASES and "start-thin-spaces" in CASES
    assert not (set(ENGINE_DEFINED) & {"start-negative", "start-thin-spaces"})
    assert [view["winner"] for view in CASES["start-negative"]["views"][0]] == ["-2-x"]
    assert [view["winner"] for view in CASES["start-thin-spaces"]["views"][0]] == ["2-x"]
    assert {kind for kind in ENGINE_DEFINED if kind.startswith("start-")} >= {
        "start-overflow", "start-negative-overflow", "start-radix-overflow", "start-binary-overflow",
        "start-infinity", "start-digits-overflow", "start-nan"}


def test_the_trimmed_characters_are_those_of_the_engine():
    # Number(ch + '1' + ch) is 1 for exactly these code points of the engine.
    assert [ord(char) for char in _JS_WHITESPACE] == FIXTURE["whitespace"] == [
        0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF]


def test_number_strings_convert_as_the_engine_converts_them():
    assert len(FIXTURE["numbers"]) >= 2000
    wrong = []
    for vector in FIXTURE["numbers"]:
        for function, key in ((_string_number, "number"), (_parse_int, "parseInt")):
            if not same_double(function(vector["text"]), as_converted(vector[key])):
                wrong.append((key, vector))
    assert wrong == []


def test_the_level_adapter_parts_ways_only_where_recorded():
    # The level core stores a body as JSON, where a number beyond every double
    # is null; IDB keeps it. In a chunk body the revision is the same on both,
    # since the hash is taken over JSON.stringify's spelling before storage. The
    # level core also stores a body by sequence and never decodes a revision off
    # a key, so it reads a revision that holds a colon where IDB cannot, and
    # two documents never share a slot there.
    assert [case["name"] for case in FIXTURE["cases"] if "level" in case] == [
        "level-ignored-kept-revision",
        "rev-colon", "rev-colon-then-child", "rev-colon-own-only", "rev-colon-hash-only",
        "rev-double-colon", "rev-colon-only", "rev-two-colons", "rev-colon-logical-delete",
        "slot-collision", "slot-collision-reverse", "slot-collision-two-files",
        "slot-collision-two-files-reverse", "slot-collision-replayed", "slot-collision-stemmed",
        "slot-collision-stemmed-reverse", "slot-collision-deep", "slot-collision-deep-reverse",
        "slot-collision-deep-files", "slot-collision-tombstone-batch",
        "slot-collision-tombstone-batch-attachment", "slot-collision-tombstone-files",
        "slot-collision-tombstone-files-attachment", "slot-collision-tombstone-replay",
        "slot-collision-tombstone-replay-attachment", "slot-collision-tombstone-reverse",
        "slot-collision-tombstone-reverse-attachment", "slot-collision-named-tombstone",
        "slot-collision-groups", "slot-collision-groups-first-attachment",
        "slot-collision-groups-both-attachment", "slot-collision-groups-compaction",
        "slot-collision-groups-compaction-attachment", "slot-collision-groups-live",
        "slot-collision-groups-taken", "attachment-mapping-accumulates",
        "attachment-compaction-order",
        "chunk-nonfinite-hash", "chunk-nonfinite-hash-negative", "chunk-nonfinite-hash-array",
        "chunk-nonfinite-hash-object", "chunk-nonfinite-hash-many-digits",
        "number-integer-overflow", "number-integer-overflow-short",
        "number-negative-integer-overflow-short", "number-negative-integer-overflow",
        "number-exponent-overflow", "number-negative-exponent-overflow", "number-nested-overflow",
        "messy-45", "messy-50"]
    # Every other case, refused files included: the unmodified memory adapter holds the same.
    assert len(FIXTURE["cases"]) == 611


@pytest.mark.parametrize("name", ["number-integer-overflow", "number-integer-overflow-short",
                                  "number-negative-integer-overflow-short", "number-negative-integer-overflow",
                                  "number-exponent-overflow", "number-negative-exponent-overflow",
                                  "number-nested-overflow", "chunk-nonfinite-hash",
                                  "chunk-nonfinite-hash-negative", "chunk-nonfinite-hash-array",
                                  "chunk-nonfinite-hash-object", "chunk-nonfinite-hash-many-digits"])
def test_a_number_beyond_every_double_is_held_as_indexeddb_holds_it(name, tmp_path):
    # The level adapter stores a document body as JSON, where such a number is
    # null; the IndexedDB adapter keeps it, and so does the store.
    case = CASES[name]
    store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    try:
        replay(store, 0, case["files"][0]["records"])
        assert store.processed(file_name(0))
        assert_holds(store, case["views"][0][0])
        device = case["views"][0][0]["read"]["body"]
        level = case["level"]["view"][0]["read"]["body"]
        assert spellings(device) and not spellings(level)
    finally:
        store.close()


def test_recorded_divergence_is_a_repeated_malformed_revision(tmp_path):
    # The level and IndexedDB adapters ignore a record whose revision already
    # has a stored body although the tree does not name it: "null-b" cannot be
    # found by revExists. IDB, the adapter devices default to, merges it again
    # and the tombstone turns live. The peer follows IDB.
    case = CASES["level-ignored-kept-revision"]
    assert case["level"]["file"] == 1 and case["level"]["view"][0]["deleted"] is True
    store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    try:
        replay(store, 0, case["files"][0]["records"])
        assert_holds(store, case["views"][0][0])
        replay(store, 1, case["files"][1]["records"])
        assert_holds(store, case["views"][1][0])
        assert is_deleted(store._tree("n"), winning_rev(store._tree("n"))) is False
    finally:
        store.close()


def test_fixture_receives_through_the_published_database():
    assert FIXTURE["commonlib"] == "484c164450d61deb2e301582b60cb9b5f9fa977f"
    assert [source["path"] for source in FIXTURE["plugin_source"]] == [
        "src/replication/journal/journalReceive.ts", "src/replication/journal/journalRecords.ts"]
    assert len(FIXTURE["dependencies"]) == 64
    assert all(entry["integrity"].startswith("sha512-") and len(entry["archive_sha256"]) == 64
               for entry in FIXTURE["dependencies"])
    assert set(FIXTURE["adapters"]) == {
        "pouchdb-adapter-idb", "pouchdb-adapter-indexeddb", "pouchdb-adapter-memory"}
    # The IDB adapter runs unmodified on one implementation of IndexedDB, pinned beside the
    # plugin's lockfile and verified the same way.
    assert FIXTURE["indexeddb"] == {
        "package": "fake-indexeddb", "version": "6.2.8",
        "url": "https://registry.npmjs.org/fake-indexeddb/-/fake-indexeddb-6.2.8.tgz",
        "integrity": "sha512-mpVW/HLUqwKXJ6F/s0hMgdK1eQfM4ncTHDioNGHWL0WKV3VcZCoZq1NKgboBbAge07aRCO7L48yziKssL59XEQ==",
        "archive_sha256": "fa32bf22123fa975860ed79fdc794ce26be236d3aec4fda59173ed7dbf934dce"}
    assert REVS_LIMIT == 100
    files = [file for case in FIXTURE["cases"] for file in case["files"]]
    observed = [view for case in FIXTURE["cases"] for view in case["views"] for view in view]
    assert len(files) >= 1110
    assert {file["outcome"] for file in files if file["outcome"]} == {
        "parse", "chunk-write", "metadata-selection", "metadata-write"}
    assert sum(file["outcome"] is not None for file in files) >= 95
    # Compact chunk records are documents of the chunk batch, trees included.
    compact = [case for case in FIXTURE["cases"] if case["name"].startswith("compact-")
               or case["name"] in ("full-then-compact", "full-compact-same")]
    assert len(compact) == 10
    assert {file["outcome"] for case in compact for file in case["files"]} == {None, "chunk-write"}
    assert all(any(view["id"].startswith("h:") and view.get("winner", "").startswith("1-")
                   for view in case["views"][-1]) for case in compact)
    assert len(ENGINE_DEFINED) >= 25
    # Every refused file is followed by the device's state, chunk batch included.
    assert sum(case["views"][-1] is not None and case["files"][len(case["views"]) - 1]["outcome"] is not None
               for case in FIXTURE["cases"]) >= 50
    assert sum(view.get("deleted") is True for view in observed) >= 100
    assert sum(bool(view["conflicts"]) for view in observed if "conflicts" in view) >= 500
    records = [record for file in files if isinstance(file["records"], list) for record in file["records"]]
    assert sum(record.get("_deleted") is True for record in records) >= 200
    assert len(FIXTURE["base64"]) == 34 and sum(vector["ok"] for vector in FIXTURE["base64"]) == 18
    assert [entry["name"] for entry in FIXTURE["writes"]] == [
        "absent", "live", "logical-deletion", "revision-tombstone", "untyped", "conflicted",
        "tombstone-above-deletion", "colon-revision", "slot-collision", "slot-collision-groups",
        "slot-collision-deep",
        "slot-collision-named-tombstone", "slot-collision-tombstone", "numeric-hash",
        "missing-body", "padded-tombstone", "colon-tombstone", "missing-tombstone", "high-root",
        "high-root-tombstone"]
    for entry in FIXTURE["writes"]:
        for result in (*entry["idb"].values(), *entry["level"].values()):
            assert set(result) == {"base", "ok", "status", "error", "id", "generation", "ancestors", "conflicts",
                                  "after"}
            assert result["ok"] == (result["generation"] is not None)
            assert result["ok"] or result["after"] is None
    # Both adapters agree on every starting state but two, where the IDB adapter
    # decodes the revision off the slot's key and the level core does not.
    assert [entry["name"] for entry in FIXTURE["writes"] if entry["idb"] != entry["level"]] == [
        "colon-revision", "slot-collision", "slot-collision-deep", "slot-collision-named-tombstone",
        "slot-collision-tombstone"]
    # Attachment mappings accumulate on a slot, so a stub is still admitted after a merge
    # stemmed the slot that first mapped its digest; the level adapter drops the attachment.
    # Two digests of one body part ways: the second stub is refused by both adapters. A merge's
    # compaction and another document's attachments in one batch leave both attachments held:
    # each request's event is dispatched before the next request is served.
    assert [f["outcome"] for c in FIXTURE["cases"] if c["name"] == "attachment-compaction-order"
            for f in c["files"]] == [None, None, None, "metadata-write"]
    assert [case["files"][-1]["outcome"] for case in FIXTURE["cases"]
            if case["name"] == "attachment-two-digests"] == ["metadata-write"]
    assert [case["files"][-1]["outcome"] for case in FIXTURE["cases"]
            if case["name"] in ("attachment-stemmed", "attachment-stem-kept")] == ["metadata-write", None]


# -- What the rule means for a note ---------------------------------------------


def child(rev, ids, base, **changes):
    """A journal record for ``rev`` whose chain is ``ids``, over the base's body."""
    record = {key: value for key, value in base.items()
              if key not in ("_rev", "_deleted", "deleted", "mtime")}
    record.update(changes, _rev=rev)
    record["_revisions"] = {"start": int(rev.split("-", 1)[0]), "ids": ids}
    return record


async def conflict(tmp_path):
    """A note deleted on one device and edited on another, both offline."""
    backend, storage = await write_backend(tmp_path)
    await backend.write_note("n.md", "original\n", create=True)
    base = backend._store.winner("n.md")
    root = base["_rev"].split("-", 1)[1]
    deletion = child("2-" + "d" * 32, ["d" * 32, root], base, deleted=True)
    edit = child("2-" + "e" * 32, ["e" * 32, root], base, children=["h:edit"], mtime=2)
    storage._objects[file_name(0)] = deflate_journal(serialize_journal({}, [deletion]))
    storage._objects[file_name(1)] = deflate_journal(serialize_journal([("h:edit", "edited\n")], [edit]))
    await backend.refresh(force=True)
    return backend, root, deletion, edit


async def arrive(backend, name, record):
    backend._object_storage._objects[name] = deflate_journal(serialize_journal({}, [record]))
    await backend.refresh(force=True)


async def test_note_survives_when_the_device_keeps_the_edit(tmp_path):
    # "Use Conflicted": the losing deletion receives a deleted child.
    backend, root, deletion, _ = await conflict(tmp_path)
    try:
        await arrive(backend, file_name(2), child(
            "3-" + "f" * 32, ["f" * 32, "d" * 32, root], deletion, _deleted=True))
        note = await backend.read_note("n.md")
        assert (note["rev"], note["text"]) == ("2-" + "e" * 32, "edited\n")
        assert await backend.get_meta("n.md") == note["meta"]
        assert [meta["path"] for meta in await backend.list_notes()] == ["n.md"]
    finally:
        await backend.close()


async def test_discarded_edit_is_not_shown_as_live(tmp_path):
    # "Use Base": the losing edit receives a deleted child; the deletion remains.
    backend, root, _, edit = await conflict(tmp_path)
    try:
        await arrive(backend, file_name(2), child(
            "3-" + "f" * 32, ["f" * 32, "e" * 32, root], edit, _deleted=True))
        assert await backend.read_note("n.md") is None
        with pytest.raises(NoteNotFound):
            await backend.get_meta("n.md")
        assert await backend.list_notes() == []
        # The discarded text is still in the store, as history.
        doc_id = path2id("n.md", nfc=True)
        assert [leaf["_rev"][:2] for leaf in backend._store.leaves(doc_id)] == ["3-", "2-"]
        assert [leaf["_rev"][:2] for leaf in backend._store.revisions(doc_id)] == ["1-", "2-", "2-", "3-"]
    finally:
        await backend.close()


# -- A revision tombstone is an absent note for every writer ---------------------


async def tombstoned(tmp_path):
    """A note whose winning revision is a tombstone without ``deleted``."""
    backend, storage = await write_backend(tmp_path)
    await backend.write_note("n.md", "old", create=True)
    old = backend._store.winner("n.md")
    backend._store.apply_journal("tomb", {}, [child(
        "2-t", ["t", old["_rev"].split("-", 1)[1]], old, _deleted=True)])
    storage.put_calls.clear()
    return backend, storage, old


# Starting states the plugin's writer and deleter read the same base over (fixture: writes).
STATES = [entry["name"] for entry in FIXTURE["writes"]]
# PouchDB answers a put over these with an error and writes nothing.
UNWRITABLE = {"colon-revision": DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE,
              "missing-body": DiagnosticCode.IS_CORRUPT_THE_WINNING_REVISION_HAS_NO_STORED_BODY,
              "padded-tombstone": DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE}
# A device marks an object that is not a live note too, another document's tombstone
# in a shared slot included; the peer deletes live notes only (docs/peer-limitations.md,
# deletion policy).
NOT_LIVE = {"untyped", "logical-deletion", "slot-collision-tombstone"}
# A deletion over a body that carries another path than its own document's: the plugin puts
# that object as it is, a record its own rebuild check rejects; the peer refuses it.
FOREIGN_DELETION = {"slot-collision-named-tombstone"}


async def seeded(entry, tmp_path):
    backend, storage = await write_backend(tmp_path)
    for index, records in enumerate(entry["files"]):
        replay(backend._store, index, records)
    return backend, storage


def conflicts_of(backend, path="n.md"):
    return collect_conflicts(backend._store._tree(path))


WRITTEN = "<the revision written>"


def read_after(backend, after, doc_id="n.md"):
    """What a raw get of ``doc_id`` returns after an operation is what the device's returned."""
    raw = backend._store.raw_get(doc_id)
    if "error" in after:
        return raw is None
    return raw is not None and (raw["_id"], raw.get("_deleted") is True) == (after["id"], after["deleted"])


@pytest.mark.parametrize("name", STATES)
async def test_a_file_is_put_over_the_base_a_device_reads(tmp_path, name):
    # The plugin reads the base with a raw get, which decodes the revision off
    # the slot's key and never reads the tree, and its put with force names that
    # revision's hash as the new revision's only ancestor: where the tree does
    # not name it the new revision is a root and the leaves there stay as
    # conflicts (fixture: writes).
    entry = next(entry for entry in FIXTURE["writes"] if entry["name"] == name)
    backend, storage = await seeded(entry, tmp_path)
    try:
        device = entry["idb"]["write"]
        tree = backend._store._tree("n.md")
        if device["base"] is None:
            assert tree is None or is_deleted(tree, winning_rev(tree)) or backend._store._body(
                "n.md", winning_rev(tree)) is None
        else:
            body = backend._store._body("n.md", winning_rev(tree))
            assert (body["_id"], body["_rev"]) == (device["base"]["id"], device["base"]["rev"])
        if name in UNWRITABLE:
            with pytest.raises(StorageError) as refused:
                await backend.write_note("n.md", "body", create=True, overwrite=True)
            assert refused.value.diagnostic.code is UNWRITABLE[name]
            assert storage.put_calls == []
            return
        result = await backend.write_note("n.md", "body", create=True, overwrite=True)
        record = published_records(storage)[0]
        assert device["ok"]
        assert record["_id"] == device["id"] == "n.md"
        assert record["_revisions"]["start"] == device["generation"] == int(result["rev"].split("-", 1)[0])
        assert record["_revisions"]["ids"][1:] == device["ancestors"]
        assert [WRITTEN if rev == result["rev"] else rev for rev in conflicts_of(backend)] == device["conflicts"]
        assert read_after(backend, device["after"])
        if device["after"]["deleted"] or device["conflicts"] == [WRITTEN]:
            # The write lost: to a leaf whose slot holds another document's tombstone, which
            # the plugin reads as deleted, or to a leaf of the note's own, which it still
            # shows. Either way the note does not read as written, on a device as here.
            if device["after"]["deleted"]:
                assert await backend.read_note("n.md") is None
            else:
                note = await backend.read_note("n.md")
                assert note is not None and note["rev"] != result["rev"] and note["text"] != "body"
        else:
            assert (await backend.read_note("n.md"))["text"] == "body"
            assert await backend.get_meta("n.md") == result
        if name == "slot-collision":
            # A device builds the new document from the file alone: nothing of
            # the body the shared slot handed it is kept but the revision.
            assert "mark" not in record and record["ctime"] == result["ctime"]
    finally:
        await backend.close()


@pytest.mark.parametrize("name", STATES)
async def test_a_deletion_is_put_over_the_object_a_device_reads(tmp_path, name):
    # deleteDBEntryByPath puts the object the raw get returned, so over a shared
    # slot it is the other document that receives the deletion (fixture: writes).
    entry = next(entry for entry in FIXTURE["writes"] if entry["name"] == name)
    backend, storage = await seeded(entry, tmp_path)
    try:
        device = entry["idb"]["delete"]
        if device["status"] == 400:
            with pytest.raises(StorageError) as refused:
                await backend.delete_note("n.md")
            assert refused.value.diagnostic.code is DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE
            assert storage.put_calls == []
            return
        before = backend._store._tree("n.md")
        if name in FOREIGN_DELETION:
            with pytest.raises(StorageError) as refused:
                await backend.delete_note("n.md")
            assert refused.value.diagnostic.code is DiagnosticCode.METADATA_PATH_IDENTITY_MISMATCH_OR_COLLISION_REBUILD_FETCH_REQUIRED
            assert storage.put_calls == []
            return
        deleted = await backend.delete_note("n.md")
        if not device["ok"] or name in NOT_LIVE:
            assert deleted is False and storage.put_calls == []
            return
        assert deleted is True
        record = published_records(storage)[0]
        assert record["_id"] == device["id"]
        assert record["_revisions"]["start"] == device["generation"]
        assert record["_revisions"]["ids"][1:] == device["ancestors"]
        assert record["deleted"] is True and "_deleted" not in record
        assert conflicts_of(backend, device["id"]) == device["conflicts"]
        assert read_after(backend, device["after"], doc_id=device["id"])
        if device["id"] != "n.md":
            # The document asked for is untouched, and stays one no reader
            # serves; the deletion sits in the document whose body its
            # winner's slot holds.
            assert backend._store._tree("n.md") == before
        else:
            assert await backend.read_note("n.md") is None
    finally:
        await backend.close()


async def test_tombstone_is_absent_for_every_reader(tmp_path):
    backend, _, _ = await tombstoned(tmp_path)
    try:
        assert await backend.read_note("n.md") is None
        with pytest.raises(NoteNotFound):
            await backend.get_meta("n.md")
        assert await backend.list_notes() == []
    finally:
        await backend.close()


@pytest.mark.parametrize("create, overwrite", [(True, False), (True, True)])
async def test_create_over_a_tombstone_descends_from_it(tmp_path, create, overwrite):
    # PouchDB's updateDoc attaches a first write over a revision-deleted
    # winner below that winner; the note lives again as its child.
    backend, storage, old = await tombstoned(tmp_path)
    try:
        result = await backend.write_note("n.md", "new", create=create, overwrite=overwrite)
        record = published_records(storage)[0]
        assert record["_revisions"] == {
            "start": 3, "ids": [record["_rev"].split("-", 1)[1], "t", old["_rev"].split("-", 1)[1]]}
        assert record["_rev"] == result["rev"]
        assert (await backend.read_note("n.md"))["text"] == "new"
        assert await backend.get_meta("n.md") == result
        assert "_deleted" not in backend._store.winner("n.md")
        # The tombstone is no longer a leaf: the new revision hangs below it.
        assert [(leaf["_rev"][:2], leaf.get("_deleted")) for leaf in backend._store.leaves(path2id("n.md", nfc=True))] == [
            ("3-", None)]
    finally:
        await backend.close()


async def test_update_of_a_tombstone_is_not_found_and_publishes_nothing(tmp_path):
    backend, storage, old = await tombstoned(tmp_path)
    try:
        for overwrite in (False, True):
            with pytest.raises(NoteNotFound):
                await backend.write_note("n.md", "new", overwrite=overwrite)
        assert storage.put_calls == []
        assert await backend.read_note("n.md") is None
    finally:
        await backend.close()


async def test_delete_of_a_tombstone_changes_nothing(tmp_path):
    backend, storage, _ = await tombstoned(tmp_path)
    try:
        assert not await backend.delete_note("n.md")
        assert storage.put_calls == []
    finally:
        await backend.close()


@pytest.mark.parametrize("operation", ["copy_note", "rename_note"])
async def test_copy_onto_a_tombstone_descends_from_it(tmp_path, operation):
    backend, storage, old = await tombstoned(tmp_path)
    try:
        await backend.write_note("source.md", "source", create=True)
        storage.put_calls.clear()
        result = await getattr(backend, operation)("source.md", "n.md")
        record = published_records(storage)[0]
        assert record["_rev"] == result["rev"]
        assert record["_revisions"] == {
            "start": 3, "ids": [record["_rev"].split("-", 1)[1], "t", old["_rev"].split("-", 1)[1]]}
        assert (await backend.read_note("n.md"))["text"] == "source"
    finally:
        await backend.close()


async def test_create_over_a_document_that_is_not_a_note_descends_from_it(tmp_path):
    backend, storage = await write_backend(tmp_path)
    try:
        # A record without a note type at the note's path: a device's raw get
        # still returns it and the plugin writes below its revision.
        backend._store.apply_journal("seed", {}, [
            {"_id": "n.md", "_rev": "2-e", "path": "n.md", "_revisions": {"start": 2, "ids": ["e", "a"]}}])
        assert await backend.read_note("n.md") is None
        result = await backend.write_note("n.md", "body", create=True)
        record = published_records(storage)[0]
        assert record["_revisions"]["start"] == 3 and record["_revisions"]["ids"][1:] == ["e", "a"]
        assert result["rev"] == record["_rev"]
        assert (await backend.read_note("n.md"))["text"] == "body"
    finally:
        await backend.close()


def published_records(storage):
    """The metadata records of the one journal the backend published."""
    assert len(storage.put_calls) == 1
    return parse_journal(inflate_journal(storage.put_calls[0][1])).docs


async def test_new_revision_keeps_the_device_chain_limit(tmp_path):
    # A device reports the stemmed path of the base, at most 100 revisions, and
    # publishes the new revision with itself in front of it.
    backend, storage = await write_backend(tmp_path)
    try:
        chain = [f"h{index}" for index in range(99, -1, -1)]
        backend._store.apply_journal("seed", {}, [
            {"_id": "a.md", "_rev": "100-h99", "type": "plain", "path": "a.md", "children": [],
             "_revisions": {"start": 100, "ids": chain}}])
        await backend.write_note("a.md", "body")
        record = published_records(storage)[0]
        assert record["_revisions"] == {"start": 101, "ids": [record["_rev"].split("-", 1)[1], *chain[:99]]}
    finally:
        await backend.close()


async def test_a_winner_the_tree_does_not_name_is_not_read_and_is_written_over_as_a_device_does(tmp_path):
    # The revision "2-7" does not name the numeric hash 7, so a device's get
    # with revs finds no path for it. No reader, list or scan may serve it, and
    # what is copied or moved is read first. The plugin's writer does not read
    # the tree: its raw get returns the body and its put makes the new revision
    # a root above "7", after which the note is readable again, the old winner
    # beside it as a conflict.
    backend, storage = await write_backend(tmp_path)
    try:
        await backend.write_note("keep.md", "body", create=True)
        storage.put_calls.clear()
        backend._store.apply_journal("seed", {}, [
            {"_id": "m.md", "_rev": "2-7", "type": "newnote", "path": "m.md", "children": [],
             "data": "text", "_revisions": {"start": 2, "ids": [7, "a"]}}])
        code = DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE
        calls = [
            backend.read_note("m.md"), backend.read_text_note("m.md"), backend.get_meta("m.md"),
            backend.copy_note("m.md", "copy.md"), backend.rename_note("m.md", "moved.md"),
        ]
        for call in calls:
            with pytest.raises(StorageError) as refused:
                await call
            assert refused.value.diagnostic.code is code
        assert [meta["path"] for meta in await backend.list_notes()] == ["keep.md"]
        scanned = [item async for item in backend.iterate_texts()]
        assert [(meta["path"], text) for meta, text in scanned] == [("keep.md", "body")]
        assert storage.put_calls == []
        result = await backend.write_note("m.md", "new", create=True, overwrite=True)
        record = published_records(storage)[0]
        assert record["_revisions"]["ids"][1:] == ["7"] and record["_revisions"]["start"] == 3
        assert conflicts_of(backend, "m.md") == ["2-7"]
        assert (await backend.read_note("m.md"))["text"] == "new"
        assert await backend.get_meta("m.md") == result
        assert [meta["path"] for meta in await backend.list_notes()] == ["keep.md", "m.md"]
    finally:
        await backend.close()


def legacy_chunk_file(backend, chunk_id, data):
    """A chunk file as a release before chunk documents wrote it."""
    chunk_file = backend._store._chunk_path(chunk_id)
    chunk_file.parent.mkdir(parents=True, exist_ok=True)
    chunk_file.write_bytes(data.encode("utf-8"))


async def test_the_chunk_document_decides_over_a_file_of_an_earlier_release(tmp_path):
    # Where a chunk document exists, the note is assembled from it, whatever
    # a file of an earlier release says. A chunk document whose tree names no
    # stored body is missing, as a device's get reports it, and is not read
    # through a file either.
    backend, _ = await write_backend(tmp_path)
    try:
        sound, unsound = chunk_id_for("sound"), chunk_id_for("unsound")
        backend._store.apply_journal("seed", {}, [
            {"_id": sound, "_rev": "1-s", "type": "leaf", "data": "held"},
            {"_id": unsound, "_rev": "1-u", "type": "leaf", "data": "held"},
            {"_id": "sound.md", "_rev": "1-m", "type": "plain", "path": "sound.md", "children": [sound]},
            {"_id": "unsound.md", "_rev": "1-m", "type": "plain", "path": "unsound.md", "children": [unsound]},
        ])
        # State whose tree names the winning revision by number, as a corrupt
        # or hand-written state may; no record can make it, since a chunk's
        # revision is always the hash of its body.
        tree = backend._store._tree(unsound)
        tree[0][1][0] = 7
        with backend._store._conn:
            backend._store._conn.execute("UPDATE rev_trees SET tree = ? WHERE doc_id = ?",
                                         (json.dumps(tree), _db_key(unsound)))
        legacy_chunk_file(backend, sound, "stale file")
        legacy_chunk_file(backend, unsound, "file-hidden")
        assert (await backend.read_note("sound.md"))["text"] == "held"
        with pytest.raises(StorageError) as refused:
            await backend.read_note("unsound.md")
        # The winner "1-7" names no stored body: the chunk is missing.
        assert refused.value.diagnostic.code is DiagnosticCode.IS_CORRUPT_A_CONTENT_CHUNK_IS_MISSING
        assert [meta["path"] for meta in await backend.list_notes()] == ["sound.md", "unsound.md"]
        # A record for a chunk the state holds is skipped, a deleted one too:
        # selectChunksToSave keeps only identifiers the device does not hold.
        backend._store.apply_journal("again", {}, [
            {"_id": sound, "_rev": "2-g", "type": "leaf", "data": "x", "_deleted": True,
             "_revisions": {"start": 2, "ids": ["g", "s"]}}])
        assert (await backend.read_note("sound.md"))["text"] == "held"
        # A chunk received as deleted is missing on every device.
        gone = chunk_id_for("gone")
        backend._store.apply_journal("gone", {}, [
            {"_id": gone, "_rev": "2-g", "type": "leaf", "data": "x", "_deleted": True},
            {"_id": "gone.md", "_rev": "1-m", "type": "plain", "path": "gone.md", "children": [gone]}])
        with pytest.raises(StorageError):
            await backend.read_note("gone.md")
    finally:
        await backend.close()


async def test_a_winner_that_names_no_stored_body_is_missing_as_a_device_reports_it(tmp_path):
    # A padded start is kept in the revision and converted in the tree: the
    # winner's name and the stored body's name differ. A device's get looks
    # the winner up by name and reports it missing before it reads the tree.
    backend, _ = await write_backend(tmp_path)
    try:
        backend._store.apply_journal("seed", {}, [
            {"_id": "p.md", "_rev": "2-a", "type": "plain", "path": "p.md", "children": [],
             "_revisions": {"start": " -1 ", "ids": ["a"]}}])
        assert backend._store.winner("p.md") is None
        assert [leaf["_rev"] for leaf in backend._store.leaves("p.md")] == ["-1-a"]
        assert await backend.read_note("p.md") is None
        assert await backend.list_notes() == []
    finally:
        await backend.close()


async def test_a_file_of_an_earlier_release_is_read_only_for_a_chunk_no_document_names(tmp_path):
    # A release before chunk documents held a compact chunk as a file alone.
    # It is still read from there, and a record for it does not replace it.
    backend, storage = await write_backend(tmp_path)
    try:
        chunk = chunk_id_for("legacy")
        legacy_chunk_file(backend, chunk, "legacy body")
        backend._store.apply_journal("old", {}, [{"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md",
                                                   "children": [chunk]}])
        assert (await backend.read_note("n.md"))["text"] == "legacy body"
        backend._object_storage._objects[file_name(0)] = deflate_journal(
            ("~" + chunk + "\x1freplacement\n" + json.dumps({"_id": "n.md", "_rev": "1-b", "type": "plain",
                                                             "path": "n.md", "children": [chunk]}) + "\n").encode("utf-8"))
        await backend.refresh(force=True)
        assert (await backend.read_note("n.md"))["text"] == "legacy body"
        assert backend._store._chunk_path(chunk).read_bytes() == b"legacy body"
        assert backend._store._tree(chunk) is None
    finally:
        await backend.close()


async def test_a_compact_record_is_received_as_a_chunk_document(tmp_path):
    # Through the backend: a held chunk is kept, two records for one chunk in
    # a file fail it after the first is written, and no file is written for
    # a chunk received since: it is held as a document.
    backend, storage = await write_backend(tmp_path)
    chunk = chunk_id_for("piece")
    try:
        def journal(*records):
            return deflate_journal("".join(records).encode("utf-8"))

        note = json.dumps({"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md", "children": [chunk]}) + "\n"
        backend._object_storage._objects[file_name(0)] = journal(
            "~" + chunk + "\x1ffirst\n", "~" + chunk + "\x1fsecond\n", note)
        with pytest.raises(StorageError):
            await backend.refresh(force=True)
        assert await backend.read_note("n.md") is None
        assert backend._store.winner(chunk)["data"] == "first"
        assert not backend._store._chunk_path(chunk).exists()
        backend._object_storage._objects[file_name(1)] = journal("~" + chunk + "\x1fthird\n", note)
        await backend.refresh(force=True)
        assert (await backend.read_note("n.md"))["text"] == "first"
        assert not backend._store._chunk_path(chunk).exists()
    finally:
        await backend.close()


async def test_a_chunk_whose_data_holds_a_lone_utf16_unit_does_not_stop_the_file(tmp_path):
    # The fourth-round reviews' case. A JSON record carries the unit as an
    # escape and a device holds the chunk and the metadata after it. The chunk
    # is held as a document; nothing is encoded as a file, so nothing fails
    # between the two commits and a retry has nothing left to do.
    backend, storage = await write_backend(tmp_path)
    chunk = chunk_id_for("lone")
    try:
        frames = (json.dumps({"_id": chunk, "_rev": "1-x", "type": "leaf", "data": "\ud800"}) + "\n"
                  + json.dumps({"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md",
                                "children": [chunk]}) + "\n").encode("utf-8")
        backend._object_storage._objects[file_name(0)] = deflate_journal(frames)
        await backend.refresh(force=True)
        assert backend._store.winner(chunk)["data"] == "\ud800"
        assert backend._store.winner("n.md")["_rev"] == "1-a"
        assert backend._store.processed(file_name(0))
        assert not backend._store._chunk_path(chunk).exists()
        await backend.refresh(force=True)
        assert [meta["path"] for meta in await backend.list_notes()] == ["n.md"]
    finally:
        await backend.close()


async def test_a_write_over_a_note_with_a_deep_field_publishes(tmp_path):
    # A writer copies the note's body before it publishes a new revision. The
    # copy keeps its own stack too, so a field nested as deep as the decoder
    # admits does not fail the write.
    backend, storage = await write_backend(tmp_path)
    try:
        deep = "x"
        for _ in range(505):
            deep = [deep]
        backend._store.apply_journal("seed", {}, [
            {"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md", "children": [],
             "data": "old", "extra": deep}])
        await backend.write_note("n.md", "new")
        record = published_records(storage)[0]
        assert record["_revisions"]["ids"][1:] == ["a"]
        assert record["extra"] == deep
        assert (await backend.read_note("n.md"))["text"] == "new"
    finally:
        await backend.close()


async def test_revision_deleted_chunk_is_missing_for_readers_and_writers(tmp_path):
    backend, _, _ = await tombstoned(tmp_path)
    try:
        discarded = chunk_id_for("discarded")
        backend._store.apply_journal("chunk", {}, [
            {"_id": discarded, "_rev": "1-x", "type": "leaf", "data": "discarded", "_deleted": True}])
        backend._store.apply_journal("note", {}, [
            {"_id": "m.md", "_rev": "1-m", "type": "plain", "path": "m.md", "children": [discarded]}])
        with pytest.raises(StorageError):
            await backend.read_note("m.md")
        with pytest.raises(StorageError):
            # Referencing that chunk again would publish a note no device can read.
            await backend.write_note("k.md", "discarded", create=True)
    finally:
        await backend.close()
