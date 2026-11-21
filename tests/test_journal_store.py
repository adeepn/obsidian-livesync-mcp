"""Tests for the journal local store: SQLite revision trees + chunk files.

Covers the Task 3 interface consumed by Tasks 5-7: ``pick_winner`` (the
deterministic winner rule for CouchDB-style ``<gen>-<hash>`` revisions),
``JournalStore.apply_journal`` (idempotent journal replay with per-file
transactions), epoch resets that wipe both tables and chunk files, path
identity enforcement on reopen, and note reads (children order, eden inline
chunks, legacy ``notes`` bodies, binary ``newnote`` bodies, E2EE refusal,
per-note corruption isolation in ``iterate_notes``).
"""

import base64
import logging
import os
import sqlite3
import threading
import unicodedata

import pytest

import journal_store
from journal_store import JournalStore, pick_winner
from livesync_format import PATH_IDENTITY_LEGACY, PATH_IDENTITY_NFC
from storage_base import StorageError

JOURNAL_1 = "1700000000000-docs.jsonl.gz"
JOURNAL_2 = "1700000001000-docs.jsonl.gz"
JOURNAL_3 = "1700000002000-docs.jsonl.gz"

DB_PATH = "journal.sqlite3"


def meta_doc(doc_id, rev, *, path=None, children=(), doc_type="plain", **extra):
    """Minimal LiveSync metadata document as journal records carry it."""
    doc = {
        "_id": doc_id,
        "_rev": rev,
        "type": doc_type,
        "path": doc_id if path is None else path,
        "children": list(children),
        "eden": {},
    }
    doc.update(extra)
    return doc


@pytest.fixture()
def store(tmp_path):
    instance = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    yield instance
    instance.close()


class TestPickWinner:
    def test_higher_generation_beats_lower(self):
        leaves = [{"_rev": "2-zzz"}, {"_rev": "3-aaa"}]
        assert pick_winner(leaves)["_rev"] == "3-aaa"

    def test_generation_tie_breaks_on_largest_hash(self):
        leaves = [{"_rev": "3-bbb"}, {"_rev": "3-aaa"}, {"_rev": "2-zzz"}]
        assert pick_winner(leaves)["_rev"] == "3-bbb"

    def test_single_leaf_is_returned_verbatim(self):
        only = {"_rev": "1-x", "path": "a.md"}
        assert pick_winner([only]) is only

    def test_empty_leaf_list_returns_none(self):
        # Brief contract: no leaves -> None (no exception); winner()
        # surfaces that as None for unknown ids.
        assert pick_winner([]) is None

    def test_malformed_revision_raises(self):
        with pytest.raises(ValueError, match="revision"):
            pick_winner([{"_rev": "bogus"}])


class TestApplyJournal:
    def test_note_and_chunk_file_stored(self, tmp_path):
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "hi"},
            [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])],
        )
        assert store.note_ids() == ["note.md"]
        assert store.winner("note.md")["_rev"] == "1-x"
        assert store.processed(JOURNAL_1) is True
        store.close()

    def test_chunk_files_are_hex_sharded_under_state_dir(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.apply_journal(JOURNAL_1, {"h:0123456789abcdef": "привет"}, [])
        chunk_file = state / "chunks" / "01" / "23456789abcdef"
        assert chunk_file.read_bytes() == "привет".encode("utf-8")
        store.close()

    def test_reapplying_same_file_changes_nothing(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        chunks = {"h:0123456789abcdef": "body"}
        docs = [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])]
        store.apply_journal(JOURNAL_1, chunks, docs)
        files_before = sorted(
            p for p in (state / "chunks").rglob("*") if p.is_file()
        )
        store.apply_journal(JOURNAL_1, chunks, docs)
        assert store.files_processed_count() == 1
        assert store.note_ids() == ["note.md"]
        assert len(store.leaves("note.md")) == 1
        files_after = sorted(
            p for p in (state / "chunks").rglob("*") if p.is_file()
        )
        assert files_after == files_before
        store.close()

    def test_merge_sibling_leaf_keeps_both_leaves_single_winner(self, tmp_path):
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1,
            {"h:a": "first "},
            [meta_doc("note.md", "2-aaa", children=["h:a"])],
        )
        store.apply_journal(
            JOURNAL_2,
            {"h:b": "second"},
            [meta_doc("note.md", "2-bbb", children=["h:b"])],
        )
        assert sorted(d["_rev"] for d in store.leaves("note.md")) == [
            "2-aaa",
            "2-bbb",
        ]
        assert store.winner("note.md")["_rev"] == "2-bbb"
        assert store.note_ids() == ["note.md"]
        doc, text, data, is_text = store.read_note("note.md")
        assert doc["_rev"] == "2-bbb"
        assert (text, data, is_text) == ("second", None, True)
        store.close()

    def test_identical_chunk_in_two_journals_is_written_once(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.apply_journal(JOURNAL_1, {"h:same": "data"}, [])
        store.apply_journal(JOURNAL_2, {"h:same": "data"}, [])
        files = [p for p in (state / "chunks").rglob("*") if p.is_file()]
        assert len(files) == 1
        store.close()

    def test_tombstone_winner_hides_note_but_keeps_leaf(self, tmp_path):
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        store.apply_journal(
            JOURNAL_2,
            {},
            [meta_doc("note.md", "2-x", children=["h:a"], deleted=True)],
        )
        assert store.winner("note.md")["_rev"] == "2-x"
        assert store.note_ids() == []
        assert store.read_note("note.md") is None
        assert sorted(d["_rev"] for d in store.leaves("note.md")) == ["1-a", "2-x"]
        store.close()

    def test_full_json_chunk_record_serves_children(self, store):
        chunk_doc = {"_id": "h:full", "_rev": "1-q", "data": "from json record"}
        note = meta_doc("viajson.md", "1-x", children=["h:full"])
        store.apply_journal(JOURNAL_1, {}, [chunk_doc, note])
        doc, text, data, is_text = store.read_note("viajson.md")
        assert (text, data, is_text) == ("from json record", None, True)

    def test_malformed_rev_rejects_whole_file_atomically(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        # Prior, successfully applied state must survive untouched.
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "keep"},
            [meta_doc("seed.md", "1-a", children=["h:0123456789abcdef"])],
        )
        with pytest.raises(ValueError, match=JOURNAL_2):
            store.apply_journal(
                JOURNAL_2,
                {"h:fedcba9876543210": "staged"},
                [meta_doc("a.md", "1-x"), {"_id": "b.md", "_rev": "bogus"}],
            )
        assert store.processed(JOURNAL_2) is False
        assert store.note_ids() == ["seed.md"]
        # SQLite prior state kept AND zero new published chunk files (nor
        # staged leftovers) from the failed application.
        files = sorted(
            p.name for p in (state / "chunks").rglob("*") if p.is_file()
        )
        assert files == ["23456789abcdef"]
        store.close()

    def test_publication_failure_leaves_journal_replayable(self, store, monkeypatch):
        chunks = {"h:first": "first body", "h:second": "second body"}
        docs = [meta_doc("retry.md", "1-x", children=list(chunks))]
        real_replace = os.replace
        calls = 0

        def fail_second_publish(src, dst):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected publication failure")
            real_replace(src, dst)

        with monkeypatch.context() as patch:
            patch.setattr(journal_store.os, "replace", fail_second_publish)
            with pytest.raises(OSError, match="injected publication failure"):
                store.apply_journal(JOURNAL_1, chunks, docs)

        assert store.processed(JOURNAL_1) is False
        assert store.leaves("retry.md") == docs
        assert store._chunk_path("h:first").read_bytes() == b"first body"
        assert not store._chunk_path("h:second").exists()
        assert not list(store._chunks_dir.rglob(".staged-*"))

        store.apply_journal(JOURNAL_1, chunks, docs)

        assert store.processed(JOURNAL_1) is True
        assert store.files_processed_count() == 1
        assert store.leaves("retry.md") == docs
        assert store.read_note("retry.md")[1] == "first bodysecond body"
        for chunk_id, data in chunks.items():
            assert store._chunk_path(chunk_id).read_bytes() == data.encode("utf-8")
        assert not list(store._chunks_dir.rglob(".staged-*"))

    @pytest.mark.parametrize("same_journal", [False, True])
    def test_concurrent_apply_same_chunk_both_succeed(
        self, tmp_path, monkeypatch, same_journal
    ):
        # Two live store instances replaying journals that carry the same
        # content-addressed chunk: publication must not collide on a shared
        # temporary file name. The barrier forces both os.replace calls to
        # run back-to-back, which the shared "<name>.tmp" scheme fails.
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()  # establish WAL once
        barrier = threading.Barrier(2)
        real_replace = os.replace
        checked = threading.Barrier(2)
        real_processed = JournalStore.processed

        def rendezvous_processed(instance, name):
            result = real_processed(instance, name)
            checked.wait(timeout=10)
            return result

        monkeypatch.setattr(JournalStore, "processed", rendezvous_processed)

        def rendezvous_replace(src, dst):
            barrier.wait(timeout=10)
            real_replace(src, dst)

        monkeypatch.setattr(journal_store.os, "replace", rendezvous_replace)

        chunk = {"h:0123456789abcdef": "shared body"}
        errors: list[BaseException] = []

        def replay(name: str, doc_id: str) -> None:
            store = JournalStore(state, PATH_IDENTITY_LEGACY)
            try:
                store.apply_journal(
                    name, chunk, [meta_doc(doc_id, "1-x", children=["h:0123456789abcdef"])]
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=replay, args=(JOURNAL_1, "a.md")),
            threading.Thread(target=replay, args=(JOURNAL_1 if same_journal else JOURNAL_2,
                                                         "a.md" if same_journal else "b.md")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []

        files = [p for p in (state / "chunks").rglob("*") if p.is_file()]
        assert len(files) == 1
        assert files[0].read_bytes() == b"shared body"
        check = JournalStore(state, PATH_IDENTITY_LEGACY)
        assert check.files_processed_count() == (1 if same_journal else 2)
        assert check.read_note("a.md")[1] == "shared body"
        assert check.read_note("a.md" if same_journal else "b.md")[1] == "shared body"
        check.close()

    def test_invalid_chunk_ids_skipped_with_warning_not_crash(self, store, caplog):
        # Bad chunk ids (wrong prefix, path traversal) must not abort the
        # replay: the journal is still processed, the ids are skipped.
        with caplog.at_level(logging.WARNING, logger="journal_store"):
            store.apply_journal(
                JOURNAL_1,
                {"f:notachunk": "x", "h:aa../../../escaped": "evil"},
                [meta_doc("a.md", "1-x")],
            )
        assert store.processed(JOURNAL_1) is True
        files = [p for p in (store.state_dir / "chunks").rglob("*") if p.is_file()]
        assert files == []
        skipped = {rec.message for rec in caplog.records}
        assert any("f:notachunk" in message for message in skipped)
        assert any("h:aa../../../escaped" in message for message in skipped)

    def test_traversal_chunk_write_never_escapes_state_dir(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        # chunks/aa/../../../escaped resolves outside the state dir.
        store.apply_journal(JOURNAL_1, {"h:aa../../../escaped": "evil"}, [])
        assert not (tmp_path / "escaped").exists()
        assert [
            p for p in tmp_path.rglob("*") if p.is_file() and "escaped" in p.name
        ] == []
        store.close()

    def test_traversal_child_is_isolated_not_an_escape(self, store, tmp_path):
        # Reading must not exfiltrate files outside the chunk store through
        # crafted child references. The valid "aa"-shard chunk makes the
        # shard directory exist, which is exactly the state in which an
        # unguarded path join would stat and read the outside file.
        secret = tmp_path / "outside"
        secret.write_text("SECRET")
        doomed = meta_doc("doomed.md", "1-x", children=["h:aa../../../outside"])
        good = meta_doc("good.md", "1-y", children=["h:g"])
        store.apply_journal(
            JOURNAL_1, {"h:g": "fine", "h:aabait": "shard exists"}, [doomed, good]
        )
        assert (store.state_dir / "chunks" / "aa").is_dir()
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("doomed.md")
        pairs = dict((d["_id"], text) for d, text in store.iterate_notes())
        assert pairs == {"doomed.md": None, "good.md": "fine"}

    def test_full_valid_charset_chunk_ids_round_trip(self, store):
        chunk_id = "h:Ab0-_9+zZ"
        doc = meta_doc("charset.md", "1-x", children=[chunk_id])
        store.apply_journal(JOURNAL_1, {chunk_id: "payload"}, [doc])
        assert store.read_note("charset.md")[1] == "payload"

    @pytest.mark.parametrize("chunk_id", ["h:", "h:" + "a" * 65, "h:abc\n", "h:a/b"])
    def test_invalid_id_boundaries_rejected_for_write_and_inline_read(self, store, chunk_id):
        doc = meta_doc(
            "invalid.md", "1-x", children=[chunk_id],
            eden={chunk_id: {"data": "must not read", "epoch": 1}},
        )
        store.apply_journal(JOURNAL_1, {chunk_id: "must not write"}, [doc])
        assert not any(p.is_file() for p in store._chunks_dir.rglob("*"))
        assert list(store.iterate_notes())[0][1] is None

    def test_symlink_shard_cannot_escape_chunks_root(self, store, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(b"private")
        (store._chunks_dir / "aa").symlink_to(outside, target_is_directory=True)
        store.apply_journal(
            JOURNAL_1, {"h:aanew": "bad"},
            [meta_doc("bad.md", "1-x", children=["h:aapayload"])],
        )
        assert not (outside / "new").exists()
        assert list(store.iterate_notes())[0][1] is None


class TestProcessedAndMeta:
    @pytest.mark.parametrize("symlink_root", [False, True], ids=["shard", "root"])
    def test_reset_unlinks_directory_symlinks_without_touching_targets(
        self, store, tmp_path, symlink_root
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_shard = outside / "bb"
        outside_shard.mkdir()
        (outside / "payload").write_bytes(b"private")
        (outside_shard / "nested").write_bytes(b"also private")
        if symlink_root:
            store._chunks_dir.rmdir()
            link = store._chunks_dir
        else:
            link = store._chunks_dir / "aa"
        link.symlink_to(outside, target_is_directory=True)
        store.mark_processed(JOURNAL_1)

        store.reset()

        assert (outside / "payload").read_bytes() == b"private"
        assert (outside_shard / "nested").read_bytes() == b"also private"
        assert not link.is_symlink()
        assert not link.exists()
        assert store.files_processed_count() == 0

    def test_defaults_on_fresh_store(self, store):
        assert store.last_sent_key == ""
        assert store.epoch == ""
        assert store.files_processed_count() == 0
        assert store.note_ids() == []

    def test_mark_processed_lifecycle(self, store):
        assert store.processed("some-file") is False
        store.mark_processed("some-file")
        assert store.processed("some-file") is True
        assert store.files_processed_count() == 1

    def test_last_sent_key_roundtrip(self, store):
        store.last_sent_key = JOURNAL_2
        assert store.last_sent_key == JOURNAL_2

    def test_state_persists_across_reopen(self, tmp_path):
        state = tmp_path / "state"
        first = JournalStore(state, PATH_IDENTITY_LEGACY)
        # Realistic order (Task 5): epoch is known from sync parameters
        # before any journal is replayed; the first epoch set on a fresh
        # store differs from the "" default and resets nothing.
        first.epoch = "1:salt"
        first.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        first.last_sent_key = JOURNAL_1
        first.close()

        second = JournalStore(state, PATH_IDENTITY_LEGACY)
        assert second.processed(JOURNAL_1) is True
        assert second.note_ids() == ["note.md"]
        assert second.last_sent_key == JOURNAL_1
        assert second.epoch == "1:salt"
        second.close()

    def test_epoch_change_wipes_tables_and_chunk_files(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.epoch = "1:salt"
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "body"},
            [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])],
        )
        store.last_sent_key = JOURNAL_1
        assert store.note_ids() == ["note.md"]
        chunks_root = state / "chunks"
        assert any(p.is_file() for p in chunks_root.rglob("*"))

        store.epoch = "2:othersalt"

        assert store.epoch == "2:othersalt"
        assert store.note_ids() == []
        assert store.files_processed_count() == 0
        assert store.last_sent_key == ""
        assert [p for p in chunks_root.rglob("*") if p.is_file()] == []
        store.close()

    def test_setting_same_epoch_does_not_reset(self, store):
        store.epoch = "1:salt"
        store.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        store.epoch = "1:salt"
        assert store.note_ids() == ["note.md"]
        assert store.files_processed_count() == 1


class TestPathIdentity:
    def test_unknown_identity_value_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="path identity"):
            JournalStore(tmp_path / "state", "weird")

    def test_identity_mismatch_on_reopen_raises(self, tmp_path):
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
        with pytest.raises(ValueError, match="path identity"):
            JournalStore(state, PATH_IDENTITY_NFC)
        # The stored identity survives the refused reopen.
        reopened = JournalStore(state, PATH_IDENTITY_LEGACY)
        reopened.close()

    def test_unicode_doc_id_untouched_in_nfc_mode(self, tmp_path):
        nfd_id = unicodedata.normalize("NFD", "café.md")
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_NFC)
        store.apply_journal(JOURNAL_1, {}, [meta_doc(nfd_id, "1-x")])
        # The store never derives or rewrites ids: whatever the journal
        # delivered is preserved byte-for-byte.
        assert store.note_ids() == [nfd_id]
        assert store.winner(nfd_id)["_id"] == nfd_id
        store.close()


class TestReadNote:
    def test_unknown_doc_returns_none(self, store):
        assert store.read_note("nope.md") is None
        assert store.winner("nope.md") is None

    def test_plain_children_concatenate_in_order(self, store):
        doc = meta_doc("a.md", "1-x", children=["h:one", "h:two"])
        store.apply_journal(
            JOURNAL_1, {"h:one": "Hello, ", "h:two": "мир"}, [doc]
        )
        winner, text, data, is_text = store.read_note("a.md")
        assert winner["_rev"] == "1-x"
        assert (text, data, is_text) == ("Hello, мир", None, True)

    def test_eden_inline_chunk_resolves_without_store_file(self, store):
        doc = meta_doc(
            "inline.md",
            "1-x",
            children=["h:inline"],
            eden={"h:inline": "inline body"},
        )
        store.apply_journal(JOURNAL_1, {}, [doc])
        _, text, data, is_text = store.read_note("inline.md")
        assert (text, data, is_text) == ("inline body", None, True)

    def test_newnote_binary_decodes_base64_per_chunk(self, store):
        payload = b"\x89PNG\r\n\x1a\nbinary"
        doc = meta_doc("image.png", "1-x", children=["h:bin"], doc_type="newnote")
        store.apply_journal(
            JOURNAL_1,
            {"h:bin": base64.b64encode(payload).decode("ascii")},
            [doc],
        )
        _, text, data, is_text = store.read_note("image.png")
        assert data == payload
        assert text is None
        assert is_text is False

    def test_newnote_with_text_extension_decodes_utf8(self, store):
        payload = "кириллица".encode("utf-8")
        doc = meta_doc("x.md", "1-x", children=["h:bin"], doc_type="newnote")
        store.apply_journal(
            JOURNAL_1,
            {"h:bin": base64.b64encode(payload).decode("ascii")},
            [doc],
        )
        _, text, data, is_text = store.read_note("x.md")
        assert (text, data, is_text) == ("кириллица", payload, True)

    def test_legacy_notes_body(self, store):
        doc = meta_doc("old.md", "1-x", doc_type="notes", data="legacy body")
        store.apply_journal(JOURNAL_1, {}, [doc])
        winner, text, data, is_text = store.read_note("old.md")
        assert (text, data, is_text) == ("legacy body", None, True)

    def test_encrypted_winner_marker_refused(self, store):
        doc = meta_doc("enc.md", "1-x", children=["h:a"], e_=True)
        store.apply_journal(JOURNAL_1, {"h:a": "x"}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_legacy_percent_prefix_refused(self, store):
        doc = meta_doc("enc.md", "1-x", doc_type="notes", data="%gibberish")
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_encrypted_eden_inline_chunk_refused(self, store):
        doc = meta_doc(
            "enc.md",
            "1-x",
            children=["h:e1"],
            eden={"h:e1": {"e_": True, "data": "secret"}},
        )
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_missing_chunk_raises_corrupt(self, store):
        doc = meta_doc("broken.md", "1-x", children=["h:gone"])
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("broken.md")

    def test_invalid_children_field_raises_corrupt(self, store):
        doc = meta_doc("bad.md", "1-x")
        doc["children"] = "not-a-list"
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("bad.md")


class TestIterateNotes:
    def test_yields_texts_sorted_and_filters_extension(self, store):
        docs = [
            meta_doc("b.md", "1-x", children=["h:b"]),
            meta_doc("a.md", "1-y", children=["h:a"]),
            meta_doc("c.png", "1-z", children=["h:c"], doc_type="newnote"),
        ]
        store.apply_journal(
            JOURNAL_1, {"h:a": "alpha", "h:b": "beta", "h:c": "aGVsbG8="}, docs
        )
        pairs = [(d["_id"], text) for d, text in store.iterate_notes()]
        assert pairs == [("a.md", "alpha"), ("b.md", "beta")]

    def test_custom_extension_parameter(self, store):
        doc = meta_doc("draw.canvas", "1-x", children=["h:x"])
        store.apply_journal(JOURNAL_1, {"h:x": "shapes"}, [doc])
        pairs = [(d["_id"], text) for d, text in store.iterate_notes(".canvas")]
        assert pairs == [("draw.canvas", "shapes")]

    def test_corrupt_note_isolated_others_read_fine(self, store):
        good = meta_doc("good.md", "1-x", children=["h:good"])
        bad = meta_doc("bad.md", "1-y", children=["h:missing"])
        store.apply_journal(JOURNAL_1, {"h:good": "fine"}, [good, bad])
        pairs = dict((d["_id"], text) for d, text in store.iterate_notes())
        assert pairs == {"good.md": "fine", "bad.md": None}


class TestSqliteLayout:
    def test_wal_journal_mode_is_persistent(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.apply_journal(JOURNAL_1, {}, [meta_doc("a.md", "1-x")])
        store.close()
        conn = sqlite3.connect(state / DB_PATH)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()

    def test_schema_v1_tables_exist(self, tmp_path):
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
        conn = sqlite3.connect(state / DB_PATH)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.close()
        assert {"docs", "processed", "meta"} <= names

    def test_second_journal_name_increments_count(self, store):
        store.apply_journal(JOURNAL_1, {}, [meta_doc("a.md", "1-x")])
        store.apply_journal(JOURNAL_2, {}, [meta_doc("b.md", "1-x")])
        assert store.files_processed_count() == 2


@pytest.mark.parametrize("target", ["state", "chunks", "sqlite"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_open_rejects_symlinked_persistence(tmp_path, target, reopen, dangling):
    state = tmp_path / "nested" / "state"
    if reopen:
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
    if target == "state":
        if reopen:
            state.rename(tmp_path / "original")
        else:
            state.parent.mkdir()
        link = state
        destination = tmp_path / "original" if reopen and not dangling else outside
    else:
        state.mkdir(parents=True, exist_ok=True)
        link = state / ("chunks" if target == "chunks" else DB_PATH)
        if link.exists():
            if target == "chunks":
                link.rmdir()
            else:
                link.unlink()
        destination = outside if target == "chunks" else outside / "database"
        if target == "sqlite" and not dangling:
            destination.write_bytes(b"untouched")
    link.symlink_to(destination, target_is_directory=target != "sqlite")
    before = {str(p.relative_to(outside)): p.read_bytes()
              for p in outside.rglob("*") if p.is_file()}
    with pytest.raises(StorageError, match="symlink.*refusing"):
        JournalStore(state, PATH_IDENTITY_LEGACY)
    assert link.is_symlink()
    assert {str(p.relative_to(outside)): p.read_bytes()
            for p in outside.rglob("*") if p.is_file()} == before
    if dangling:
        assert not outside.exists()


def test_nested_state_normal_operation_and_reopen(tmp_path):
    state = tmp_path / "nested" / "state"
    store = JournalStore(state, PATH_IDENTITY_LEGACY)
    store.apply_journal(JOURNAL_1, {"h:0123456789abcdef": "body"}, [])
    store.close()
    reopened = JournalStore(state, PATH_IDENTITY_LEGACY)
    assert reopened.processed(JOURNAL_1)
    assert (state / "chunks" / "01" / "23456789abcdef").read_text() == "body"
    reopened.close()


def test_state_size_bytes_includes_sqlite_sidecars_and_nested_chunks(store):
    root = store.state_dir
    shard = root / "chunks" / "nested"
    shard.mkdir()
    (shard / "fixture").write_bytes(b"abcde")
    expected = sum(p.stat().st_size for p in root.iterdir() if p.is_file()) + 5
    (root / "unrelated.txt").write_bytes(b"ignored")
    assert store.state_size_bytes() == expected
