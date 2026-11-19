"""Tests for the journal storage backend (Plan B, Tasks 5-6).

Covers ``JournalBackend.startup_check`` (milestone locked/missing, settings
refusals, path-identity state mismatch, sync-parameter epoch change, remote
wipe probe), ``refresh`` (ordered replay with numeric ms ordering, corrupt
file stops without advancing the checkpoint, control objects never parsed,
throttling with ``force`` bypass) and ``backend_info``/``close``. Task 6
adds the read paths — ``list_notes``/``get_meta``/``read_note``/
``iterate_texts`` — against an NFC-identity vault fixture with Cyrillic
names, a binary ``newnote``, a tombstone, a service doc and a hidden
dotfile, mirroring the CouchDB backend's contracts. All object storage goes
through ``FakeObjectStorage`` — no network.
"""

import base64
import json
import logging
import unicodedata
from datetime import datetime

import pytest

import storage_journal
from journal_codec import deflate_journal, serialize_journal
from livesync_format import chunk_id_for, path2id
from storage_base import (
    NoteNotFound,
    StorageError,
    StorageUnavailable,
    UnsupportedVault,
)
from storage_journal import (
    MILESTONE_KEY,
    SYNC_PARAMETERS_KEY,
    JournalBackend,
)
from tests.fake_object_storage import FakeObjectStorage

JOURNAL_1 = "1-docs.jsonl.gz"
JOURNAL_2 = "2-docs.jsonl.gz"
JOURNAL_3 = "3-docs.jsonl.gz"
JOURNAL_9 = "9-docs.jsonl.gz"
JOURNAL_10 = "10-docs.jsonl.gz"


# -- Test data builders -----------------------------------------------------


def milestone_blob(*, locked: bool = False, path_version: int = 0) -> bytes:
    """A milestone JSON with PREFERRED tweak values, as devices write it."""
    doc = {
        "_id": MILESTONE_KEY,
        "type": "milestoneinfo",
        "created": 1,
        "accepted_nodes": [],
        "node_info": {},
        "locked": locked,
        "node_chunk_info": {},
        "tweak_values": {
            "PREFERRED": {"pathNormalizationVersion": path_version}
        },
    }
    return json.dumps(doc).encode("utf-8")


def params_blob(*, protocol: int = 2, salt: str = "salt-a") -> bytes:
    """Journal sync parameters object (epoch inputs protocolVersion+salt)."""
    doc = {"protocolVersion": protocol, "pbkdf2salt": salt}
    return json.dumps(doc).encode("utf-8")


def meta_doc(path: str, rev: str) -> dict:
    return {
        "_id": path,
        "_rev": rev,
        "path": path,
        "type": "newnote",
        "ctime": 1,
        "mtime": 1,
        "size": 1,
    }


def journal_blob(docs: list[dict]) -> bytes:
    return deflate_journal(serialize_journal([], docs))


def three_journals() -> dict[str, bytes]:
    return {
        JOURNAL_1: journal_blob([meta_doc("a.md", "1-a")]),
        JOURNAL_2: journal_blob([meta_doc("b.md", "1-b")]),
        JOURNAL_3: journal_blob([meta_doc("c.md", "1-c")]),
    }


def healthy_bucket(**overrides) -> dict[str, bytes]:
    objects = {
        MILESTONE_KEY: milestone_blob(),
        SYNC_PARAMETERS_KEY: params_blob(),
        **three_journals(),
    }
    objects.update(overrides)
    objects = {k: v for k, v in objects.items() if v is not None}
    return objects


def make_backend(tmp_path, objects=None, **kwargs) -> JournalBackend:
    storage = FakeObjectStorage(objects or {})
    backend = JournalBackend(storage, tmp_path / "state", **kwargs)
    return backend, storage


async def files_processed(backend: JournalBackend) -> int:
    return (await backend.backend_info())["files_processed"]


# -- Upstream control-object names ------------------------------------------


class TestControlObjectNames:
    def test_names_match_upstream_commonlib(self):
        # commonlib LiveSyncJournalReplicator.ts:64 and sync.definition.ts:11.
        assert MILESTONE_KEY == "_00000000-milestone.json"
        assert (
            SYNC_PARAMETERS_KEY
            == "_obsidian_livesync_journal_sync_parameters.json"
        )


# -- startup_check -----------------------------------------------------------


class TestStartup:
    async def test_locked_milestone_refused(self, tmp_path):
        backend, _ = make_backend(
            tmp_path,
            {
                MILESTONE_KEY: milestone_blob(locked=True),
                SYNC_PARAMETERS_KEY: params_blob(),
            },
        )
        with pytest.raises(UnsupportedVault, match="locked"):
            await backend.startup_check()

    async def test_journals_without_milestone_refused(self, tmp_path):
        one_journal = {JOURNAL_1: journal_blob([meta_doc("a.md", "1-a")])}
        backend, _ = make_backend(
            tmp_path, {SYNC_PARAMETERS_KEY: params_blob(), **one_journal}
        )
        with pytest.raises(
            UnsupportedVault, match="rebuild from the authoritative vault"
        ):
            await backend.startup_check()

    async def test_journals_without_sync_parameters_refused(self, tmp_path):
        one_journal = {JOURNAL_1: journal_blob([meta_doc("a.md", "1-a")])}
        backend, _ = make_backend(
            tmp_path, {MILESTONE_KEY: milestone_blob(), **one_journal}
        )
        with pytest.raises(UnsupportedVault, match="sync parameters"):
            await backend.startup_check()

    async def test_encrypted_settings_refused_with_upstream_text(self, tmp_path):
        encrypted = json.loads(milestone_blob().decode("utf-8"))
        encrypted["tweak_values"]["PREFERRED"]["encrypt"] = True
        backend, _ = make_backend(
            tmp_path,
            {
                MILESTONE_KEY: json.dumps(encrypted).encode("utf-8"),
                SYNC_PARAMETERS_KEY: params_blob(),
            },
        )
        with pytest.raises(UnsupportedVault, match="end-to-end encryption"):
            await backend.startup_check()

    async def test_identity_mismatch_against_existing_state(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        assert (await backend.backend_info())["path_identity"] == "legacy"

        backend, storage = make_backend(
            tmp_path, healthy_bucket(**{MILESTONE_KEY: milestone_blob(path_version=1)})
        )
        # Same state dir, now NFC: a state-configuration error (ValueError).
        with pytest.raises(ValueError, match="path identity"):
            await backend.startup_check()
        await backend.close()

    async def test_fresh_empty_bucket_defaults_to_legacy(self, tmp_path, caplog):
        backend, _ = make_backend(tmp_path)
        with caplog.at_level(logging.WARNING):
            await backend.startup_check()
        info = await backend.backend_info()
        assert info["path_identity"] == "legacy"
        assert info["files_processed"] == 0
        assert "fresh" in caplog.text
        await backend.close()

    async def test_startup_is_idempotent_with_stable_bucket(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.startup_check()
        assert await files_processed(backend) == 0
        await backend.close()


# -- Epoch handling and remote wipe probe ------------------------------------


class TestEpochAndWipe:
    async def test_epoch_change_resets_state_and_replays_everything(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        assert await files_processed(backend) == 3
        storage.get_calls.clear()
        storage.list_calls.clear()

        storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt="salt-b")
        await backend.startup_check()
        info = await backend.backend_info()
        assert info["files_processed"] == 0
        assert info["epoch"] == "2:salt-b"

        storage.get_calls.clear()
        storage.list_calls.clear()
        await backend.refresh(force=True)
        assert await files_processed(backend) == 3
        # The reset cleared the processed ledger: the replay lists from the
        # beginning again and re-reads every journal.
        assert storage.list_calls == [""]
        assert sorted(storage.get_calls) == [JOURNAL_1, JOURNAL_2, JOURNAL_3]
        await backend.close()

    async def test_same_epoch_keeps_state(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        await backend.startup_check()
        assert await files_processed(backend) == 3
        await backend.close()

    async def test_missing_last_sent_file_means_remote_wipe(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        assert await files_processed(backend) == 3
        # Simulate a journal this peer wrote earlier (Task 7's duty).
        backend._store.last_sent_key = JOURNAL_3
        del storage._objects[JOURNAL_3]

        await backend.startup_check()
        assert JOURNAL_3 in storage.exists_calls
        info = await backend.backend_info()
        assert info["files_processed"] == 0
        assert info["epoch"] == "2:salt-a"
        await backend.close()

    async def test_surviving_last_sent_file_no_wipe(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        backend._store.last_sent_key = JOURNAL_3
        await backend.startup_check()
        assert await files_processed(backend) == 3
        await backend.close()


# -- refresh: ordered replay --------------------------------------------------


class TestReplay:
    async def test_three_files_replayed_in_numeric_order(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        storage.get_calls.clear()

        await backend.refresh()
        assert storage.get_calls == [JOURNAL_1, JOURNAL_2, JOURNAL_3]
        assert await files_processed(backend) == 3

    async def test_second_refresh_after_full_replay_is_noop(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        storage.get_calls.clear()
        storage.list_calls.clear()

        await backend.refresh(force=True)
        # One complete listing (Ruling B-8: no StartAfter cursor); every
        # listed journal is already processed, so nothing is fetched.
        assert storage.list_calls == [""]
        assert storage.get_calls == []
        assert await files_processed(backend) == 3

    async def test_numeric_ordering_beats_lexicographic(self, tmp_path):
        extra = {
            JOURNAL_9: journal_blob([meta_doc("i.md", "1-i")]),
            JOURNAL_10: journal_blob([meta_doc("j.md", "1-j")]),
        }
        backend, storage = make_backend(
            tmp_path, healthy_bucket(**three_journals(), **extra)
        )
        await backend.startup_check()
        storage.get_calls.clear()

        await backend.refresh()
        # "9-..." sorts lexicographically after "10-..."; replay must apply
        # the numerically smaller file first.
        nine = storage.get_calls.index(JOURNAL_9)
        ten = storage.get_calls.index(JOURNAL_10)
        assert nine < ten
        assert await files_processed(backend) == 5

    async def test_later_mixed_width_journal_is_not_hidden(self, tmp_path):
        # Ruling B-8: after "9-..." is processed, a lexicographic StartAfter
        # cursor at "9-..." would hide the later-arriving "10-..." (which
        # sorts before it) forever; the complete listing must see it.
        objects = healthy_bucket(
            **{
                JOURNAL_1: None,
                JOURNAL_2: None,
                JOURNAL_3: None,
                JOURNAL_9: journal_blob([meta_doc("i.md", "1-i")]),
            }
        )
        backend, storage = make_backend(tmp_path, objects)
        await backend.startup_check()
        await backend.refresh()
        assert await files_processed(backend) == 1

        storage._objects[JOURNAL_10] = journal_blob([meta_doc("j.md", "1-j")])
        storage.get_calls.clear()
        await backend.refresh(force=True)
        assert storage.get_calls == [JOURNAL_10]
        assert await files_processed(backend) == 2

    async def test_mixed_width_failure_and_repair_converges(self, tmp_path):
        # Ruling B-8: a lexicographic StartAfter cursor at "9-..." would
        # permanently hide the numerically later "10-..."/"11-..."; the
        # complete listing must not.
        journal_11 = "11-docs.jsonl.gz"
        objects = healthy_bucket(
            **{
                JOURNAL_1: None,
                JOURNAL_2: None,
                JOURNAL_3: None,
                JOURNAL_9: b"corrupt",
                JOURNAL_10: journal_blob([meta_doc("j.md", "1-j")]),
                journal_11: journal_blob([meta_doc("k.md", "1-k")]),
            }
        )
        backend, storage = make_backend(tmp_path, objects)
        await backend.startup_check()
        with pytest.raises(StorageError, match=JOURNAL_9):
            await backend.refresh()
        assert await files_processed(backend) == 0

        # Repair the broken file: the retry sees the whole bucket again.
        storage._objects[JOURNAL_9] = journal_blob([meta_doc("i.md", "1-i")])
        storage.get_calls.clear()
        await backend.refresh()
        assert storage.get_calls == [JOURNAL_9, JOURNAL_10, journal_11]
        assert await files_processed(backend) == 3

    async def test_replay_error_message_sanitized(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        storage.get_calls.clear()
        storage.fail_next_get(
            1, error=ValueError("/abs/state/path note.md 3-x secret")
        )
        with pytest.raises(StorageError, match=JOURNAL_1) as exc_info:
            await backend.refresh()
        message = str(exc_info.value)
        # Public text: the file name and the error class only — no paths,
        # document ids or other internals (audit C7).
        assert "ValueError" in message
        assert "/abs/state/path" not in message
        assert "note.md" not in message

    async def test_corrupt_second_file_stops_replay_at_checkpoint(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(**{JOURNAL_2: b"not-a-deflate-stream"})
        )
        await backend.startup_check()
        with pytest.raises(StorageError, match=JOURNAL_2):
            await backend.refresh()

        assert await files_processed(backend) == 1
        assert JOURNAL_3 not in storage.get_calls

        # The retry starts at the same broken file.
        storage.get_calls.clear()
        with pytest.raises(StorageError, match=JOURNAL_2):
            await backend.refresh(force=True)
        assert storage.get_calls == [JOURNAL_2]

    async def test_control_objects_are_never_parsed_as_journals(self, tmp_path):
        backend, storage = make_backend(
            tmp_path,
            healthy_bucket(**{"_maintenance.json": b"{}"}),
        )
        await backend.startup_check()
        storage.get_calls.clear()

        await backend.refresh()
        assert storage.get_calls == [JOURNAL_1, JOURNAL_2, JOURNAL_3]
        assert await files_processed(backend) == 3

    async def test_unavailable_get_propagates_and_keeps_checkpoint(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        storage.get_calls.clear()
        storage.fail_next_get(1)
        with pytest.raises(StorageUnavailable):
            await backend.refresh()
        assert await files_processed(backend) == 0
        assert storage.get_calls == [JOURNAL_1]

        # Once healthy again, replay completes from the checkpoint.
        await backend.refresh(force=True)
        assert await files_processed(backend) == 3


# -- refresh throttling --------------------------------------------------------


class TestThrottle:
    async def test_refresh_within_interval_is_a_noop(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(), refresh_min_interval=5.0
        )
        await backend.startup_check()
        await backend.refresh()
        assert len(storage.list_calls) == 1

        await backend.refresh()
        assert len(storage.list_calls) == 1  # throttled: list not called
        assert await files_processed(backend) == 3

    async def test_force_bypasses_the_throttle(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(), refresh_min_interval=5.0
        )
        await backend.startup_check()
        await backend.refresh()
        await backend.refresh(force=True)
        assert len(storage.list_calls) == 2
        assert storage.list_calls == ["", ""]

    async def test_state_reset_clears_the_throttle(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(), refresh_min_interval=60.0
        )
        await backend.startup_check()
        await backend.refresh()  # arms the throttle
        assert (await backend.backend_info())["last_refresh"] is not None

        storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt="salt-b")
        await backend.startup_check()  # epoch change: state reset
        assert (await backend.backend_info())["last_refresh"] is None

        storage.list_calls.clear()
        await backend.refresh()  # unforced: must run, not throttle
        assert storage.list_calls == [""]
        assert await files_processed(backend) == 3

    async def test_failed_refresh_is_not_throttled(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(**{JOURNAL_1: b"corrupt"}), refresh_min_interval=60.0
        )
        await backend.startup_check()
        with pytest.raises(StorageError):
            await backend.refresh()
        # The failed attempt does not arm the throttle: an immediate retry
        # hits the storage again instead of silently skipping.
        with pytest.raises(StorageError):
            await backend.refresh()
        assert len(storage.list_calls) == 2


# -- backend_info, close, unimplemented reads/writes ---------------------------


class TestLifecycle:
    def test_backend_satisfies_storage_backend_protocol(self, tmp_path):
        from storage_base import StorageBackend

        backend, _ = make_backend(tmp_path, healthy_bucket())
        assert isinstance(backend, StorageBackend)

    async def test_refresh_before_startup_refused(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        with pytest.raises(StorageError, match="startup_check"):
            await backend.refresh()

    async def test_backend_info_shape(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        info = await backend.backend_info()
        assert info["backend"] == "s3-journal"
        assert info["path_identity"] is None
        assert info["last_refresh"] is None

        await backend.startup_check()
        await backend.refresh()
        info = await backend.backend_info()
        assert info["path_identity"] == "legacy"
        assert info["epoch"] == "2:salt-a"
        assert info["files_processed"] == 3
        datetime.fromisoformat(info["last_refresh"])  # ISO-8601 timestamp
        await backend.close()

    async def test_close_is_idempotent(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.close()
        await backend.close()

    async def test_write_methods_raise_until_task_7(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        with pytest.raises(NotImplementedError):
            await backend.write_note("a.md", "body")
        with pytest.raises(NotImplementedError):
            await backend.delete_note("a.md")
        with pytest.raises(NotImplementedError):
            await backend.rename_note("a.md", "b.md")
        with pytest.raises(NotImplementedError):
            await backend.copy_note("a.md", "b.md")


# -- Read paths (Task 6) -------------------------------------------------------
#
# An NFC-identity vault (milestone pathNormalizationVersion=1) with notes in
# two folders, one NFC-relevant Cyrillic path (precomposed ё vs decomposed
# е+U+0308), a binary newnote, a legacy ``notes`` body, a note with a missing
# chunk (corrupt), a tombstone, a service document and a hidden dotfile note.
# Journal doc ids are derived the way an NFC-identity client derives them
# (path2id(path, nfc=True)); the backend must resolve path->id with the same
# identity.

OBOZOR_PATH = "Заметки/Обзор.md"
OBOZOR_TEXT = "Текст обзора"
OBOZOR_REV = "3-aaaa1111bbbb2222cccc3333dddd4444e5"

# NFC-relevant: ё precomposed (U+0451) vs NFD е + combining diaeresis.
PRIEM_PATH = "Заметки/Приём.md"
PRIEM_PATH_NFD = unicodedata.normalize("NFD", PRIEM_PATH)
assert PRIEM_PATH != PRIEM_PATH_NFD  # the two spellings must differ
PRIEM_TEXT = "Записи приёма"
PRIEM_REV = "2-eeee1111ffff2222aaaa3333bbbb4444c5"

README_PATH = "docs/readme.md"
README_PART_A = "first "
README_PART_B = "second"
README_REV = "4-1234567890abcdef1234567890abcdef12"

LOGO_PATH = "docs/logo.png"
LOGO_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
LOGO_CHUNK_DATA = base64.b64encode(LOGO_BYTES).decode("ascii")
LOGO_REV = "1-fedcba0987654321fedcba0987654321f0"

BROKEN_PATH = "docs/broken.md"  # references a chunk no journal ever carried
BROKEN_REV = "1-0000000000000000000000000000000090"

NESTED_PATH = "docs/sub/Заметка.MD"  # nested + uppercase extension
NESTED_TEXT = "вложенная"
NESTED_REV = "2-9999888877776666555544443333222211"

LEGACY_PATH = "docs/legacy.md"  # type "notes": body in the doc itself
LEGACY_TEXT = "старая заметка"
LEGACY_REV = "5-abcabcabcabcabcabcabcabcabcabcabca"

TOMBSTONE_PATH = "docs/old.md"
HIDDEN_PATH = ".obsidian/app.json"

OBOZOR_CTIME, OBOZOR_MTIME, OBOZOR_SIZE = 1731000001000, 1731000002000, 24

LISTED_PATHS = sorted(
    [
        OBOZOR_PATH,
        PRIEM_PATH,
        README_PATH,
        LOGO_PATH,
        BROKEN_PATH,
        NESTED_PATH,
        LEGACY_PATH,
    ]
)
MD_PATHS = sorted([p for p in LISTED_PATHS if p.lower().endswith(".md")])


def nfc_note_doc(path: str, rev: str, *, doc_type="plain", children=(), **extra):
    """A metadata doc the way an NFC-identity client would publish it."""
    doc = {
        "_id": path2id(path, nfc=True),
        "_rev": rev,
        "path": path,
        "type": doc_type,
        "children": list(children),
        "eden": {},
        "ctime": 1731000000000,
        "mtime": 1731000000000,
        "size": 10,
    }
    doc.update(extra)
    return doc


def read_vault_objects() -> dict[str, bytes]:
    """The Task 6 fixture bucket: milestone(NFC) + params + two journals."""
    chunks = [
        (chunk_id_for(OBOZOR_TEXT), OBOZOR_TEXT),
        (chunk_id_for(PRIEM_TEXT), PRIEM_TEXT),
        (chunk_id_for(README_PART_A), README_PART_A),
        (chunk_id_for(README_PART_B), README_PART_B),
        (chunk_id_for(LOGO_CHUNK_DATA), LOGO_CHUNK_DATA),
        (chunk_id_for(NESTED_TEXT), NESTED_TEXT),
    ]
    docs = [
        nfc_note_doc(
            OBOZOR_PATH,
            OBOZOR_REV,
            children=[chunk_id_for(OBOZOR_TEXT)],
            ctime=OBOZOR_CTIME,
            mtime=OBOZOR_MTIME,
            size=OBOZOR_SIZE,
        ),
        nfc_note_doc(
            PRIEM_PATH, PRIEM_REV, children=[chunk_id_for(PRIEM_TEXT)]
        ),
        nfc_note_doc(
            README_PATH,
            README_REV,
            children=[
                chunk_id_for(README_PART_A),
                chunk_id_for(README_PART_B),
            ],
        ),
        nfc_note_doc(
            LOGO_PATH,
            LOGO_REV,
            doc_type="newnote",
            children=[chunk_id_for(LOGO_CHUNK_DATA)],
        ),
        nfc_note_doc(
            BROKEN_PATH, BROKEN_REV, children=["h:missing0000000000000000"]
        ),
        nfc_note_doc(
            NESTED_PATH, NESTED_REV, children=[chunk_id_for(NESTED_TEXT)]
        ),
        # Legacy "notes" doc: body inline in `data`, no children/size/ctime.
        {
            "_id": path2id(LEGACY_PATH, nfc=True),
            "_rev": LEGACY_REV,
            "path": LEGACY_PATH,
            "type": "notes",
            "data": LEGACY_TEXT,
            "children": [],
        },
    ]
    later_docs = [
        # Tombstone: a live revision superseded by a deleted winner.
        nfc_note_doc(TOMBSTONE_PATH, "1-oldlive000000000000000000000000ff"),
        nfc_note_doc(
            TOMBSTONE_PATH,
            "2-deaddead0000000000000000000000000e",
            deleted=True,
        ),
        # Service document: never a note (is_service_document_id).
        nfc_note_doc("syncinfo", "1-svc000000000000000000000000000005c"),
        # Hidden dotfile note: a note doc that must never surface.
        nfc_note_doc(HIDDEN_PATH, "1-hid000000000000000000000000000000dd"),
    ]
    return {
        MILESTONE_KEY: milestone_blob(path_version=1),
        SYNC_PARAMETERS_KEY: params_blob(),
        JOURNAL_1: deflate_journal(serialize_journal(chunks, docs)),
        JOURNAL_2: deflate_journal(serialize_journal([], later_docs)),
    }


async def read_backend(tmp_path, objects=None, **kwargs):
    """A backend over the Task 6 fixture, started and refreshed."""
    backend, storage = make_backend(
        tmp_path, objects or read_vault_objects(), **kwargs
    )
    await backend.startup_check()
    await backend.refresh()
    storage.get_calls.clear()
    storage.list_calls.clear()
    return backend, storage


class TestListNotes:
    async def test_lists_live_notes_with_display_paths_sorted(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        metas = await backend.list_notes()
        # Display path is the doc's canonical `path` field (original
        # spelling), not the lowercased doc id; service doc, tombstone and
        # hidden dotfile never surface.
        assert [meta["path"] for meta in metas] == LISTED_PATHS

    async def test_meta_fields_come_from_the_winner_doc(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        metas = {meta["path"]: meta for meta in await backend.list_notes()}
        obzor = metas[OBOZOR_PATH]
        assert obzor["doc_id"] == path2id(OBOZOR_PATH, nfc=True)
        assert obzor["rev"] == OBOZOR_REV
        assert obzor["type"] == "plain"
        assert obzor["ctime"] == OBOZOR_CTIME
        assert obzor["mtime"] == OBOZOR_MTIME
        assert obzor["size"] == OBOZOR_SIZE
        # Legacy "notes" doc: no size field -> computed from the data body.
        legacy = metas[LEGACY_PATH]
        assert legacy["size"] == len(LEGACY_TEXT.encode("utf-8"))
        assert legacy["type"] == "notes"
        assert legacy["ctime"] == 0  # absent -> 0, like the CouchDB backend

    async def test_every_rev_is_a_nonempty_string(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        for meta in await backend.list_notes():
            assert isinstance(meta["rev"], str) and meta["rev"]

    @pytest.mark.parametrize(
        "folder", ["docs", "DOCS", "Docs"], ids=["lower", "upper", "mixed"]
    )
    async def test_folder_filter_is_case_insensitive(self, tmp_path, folder):
        backend, _ = await read_backend(tmp_path)
        metas = await backend.list_notes(folder=folder)
        assert [meta["path"] for meta in metas] == [
            p for p in LISTED_PATHS if p.startswith("docs/")
        ]

    @pytest.mark.parametrize(
        "folder", ["Заметки", "заметки", "ЗАМЕТКИ"],
        ids=["nfc-mixed", "lower", "upper"],
    )
    async def test_cyrillic_folder_filter_both_directions(self, tmp_path, folder):
        backend, _ = await read_backend(tmp_path)
        metas = await backend.list_notes(folder=folder)
        assert [meta["path"] for meta in metas] == sorted(
            [OBOZOR_PATH, PRIEM_PATH]
        )

    async def test_folder_dot_and_backslash_spellings_normalize(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        for folder in ("./docs", "docs/", "docs\\"):
            metas = await backend.list_notes(folder=folder)
            assert all(meta["path"].startswith("docs/") for meta in metas)
            assert len(metas) == 5

    async def test_recursive_false_lists_direct_children_only(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        metas = await backend.list_notes(folder="docs", recursive=False)
        assert [meta["path"] for meta in metas] == sorted(
            [BROKEN_PATH, LEGACY_PATH, LOGO_PATH, README_PATH]
        )
        # The nested note still appears under its own folder filter.
        nested = await backend.list_notes(folder="docs/sub")
        assert [meta["path"] for meta in nested] == [NESTED_PATH]

    async def test_recursive_false_at_vault_root(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        # Every fixture note lives in a folder, so the root has no children.
        assert await backend.list_notes(recursive=False) == []

    async def test_extension_filter_case_insensitive_with_or_without_dot(
        self, tmp_path
    ):
        backend, _ = await read_backend(tmp_path)
        for extension in (".md", "md", "MD", ".MD"):
            metas = await backend.list_notes(extension=extension)
            assert [meta["path"] for meta in metas] == MD_PATHS
        png = await backend.list_notes(extension="png")
        assert [meta["path"] for meta in png] == [LOGO_PATH]

    async def test_folder_and_extension_combine(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        metas = await backend.list_notes(folder="Docs", extension="MD")
        # recursive=True by default, so the nested .MD note is included.
        assert [meta["path"] for meta in metas] == sorted(
            [BROKEN_PATH, LEGACY_PATH, README_PATH, NESTED_PATH]
        )

    async def test_tombstone_service_and_hidden_never_listed(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        listed = {meta["path"] for meta in await backend.list_notes()}
        assert TOMBSTONE_PATH not in listed
        assert HIDDEN_PATH not in listed
        assert "syncinfo" not in listed

    async def test_list_before_startup_refused(self, tmp_path):
        backend, _ = make_backend(tmp_path, read_vault_objects())
        with pytest.raises(StorageError, match="startup_check"):
            await backend.list_notes()

    async def test_backend_info_gains_note_count(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        info = await backend.backend_info()
        # Live note documents in the store: the 7 listed notes plus the
        # hidden dotfile doc (a note doc that listing filters out).
        assert info["notes"] == 8


class TestGetMeta:
    async def test_returns_winner_meta(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        meta = await backend.get_meta(OBOZOR_PATH)
        assert meta["path"] == OBOZOR_PATH
        assert meta["doc_id"] == path2id(OBOZOR_PATH, nfc=True)
        assert meta["rev"] == OBOZOR_REV

    async def test_resolves_nfc_and_nfd_spellings_via_identity(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        nfc_meta = await backend.get_meta(PRIEM_PATH)
        nfd_meta = await backend.get_meta(PRIEM_PATH_NFD)
        # The client derived ids with NFC identity; both spellings of the
        # same path must hit the same document.
        assert nfc_meta["doc_id"] == path2id(PRIEM_PATH, nfc=True)
        assert nfd_meta["doc_id"] == nfc_meta["doc_id"]
        assert nfd_meta["rev"] == nfc_meta["rev"] == PRIEM_REV

    async def test_case_only_respelling_resolves_to_same_doc(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        meta = await backend.get_meta("DOCS/README.MD")
        assert meta["doc_id"] == path2id(README_PATH, nfc=True)
        assert meta["rev"] == README_REV

    async def test_missing_raises_note_not_found(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        with pytest.raises(NoteNotFound):
            await backend.get_meta("nope/missing.md")

    async def test_tombstone_raises_note_not_found(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        with pytest.raises(NoteNotFound):
            await backend.get_meta(TOMBSTONE_PATH)

    async def test_hidden_path_refused_as_not_found(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        with pytest.raises(NoteNotFound):
            await backend.get_meta(HIDDEN_PATH)


class TestReadNote:
    async def test_text_note_round_trips_with_rev(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        content = await backend.read_note(OBOZOR_PATH)
        assert content["text"] == OBOZOR_TEXT
        assert content["data"] is None
        assert content["is_text"] is True
        assert content["rev"] == OBOZOR_REV
        assert content["meta"]["path"] == OBOZOR_PATH

    async def test_multi_chunk_note_assembles_in_children_order(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        content = await backend.read_note(README_PATH)
        assert content["text"] == README_PART_A + README_PART_B

    async def test_nfd_spelling_reads_the_same_note(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        content = await backend.read_note(PRIEM_PATH_NFD)
        assert content["text"] == PRIEM_TEXT
        assert content["meta"]["path"] == PRIEM_PATH  # display path as stored

    async def test_binary_newnote_returns_exact_bytes(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        content = await backend.read_note(LOGO_PATH)
        assert content["data"] == LOGO_BYTES
        assert content["text"] is None
        assert content["is_text"] is False
        assert content["rev"] == LOGO_REV

    async def test_legacy_notes_doc_body(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        content = await backend.read_note(LEGACY_PATH)
        assert content["text"] == LEGACY_TEXT
        assert content["is_text"] is True

    async def test_missing_returns_none(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        assert await backend.read_note("nope/missing.md") is None

    async def test_tombstone_returns_none(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        assert await backend.read_note(TOMBSTONE_PATH) is None

    async def test_hidden_path_returns_none(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        assert await backend.read_note(HIDDEN_PATH) is None

    async def test_corrupt_note_raises_storage_error(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        with pytest.raises(StorageError, match="corrupt"):
            await backend.read_note(BROKEN_PATH)

    async def test_read_before_startup_refused(self, tmp_path):
        backend, _ = make_backend(tmp_path, read_vault_objects())
        with pytest.raises(StorageError, match="startup_check"):
            await backend.read_note(OBOZOR_PATH)

    async def test_note_named_like_the_e2ee_message_is_still_corrupt(
        self, tmp_path
    ):
        # Classification must be structural, not substring-based: a note
        # *named* "end-to-end encryption.md" with a missing chunk is a
        # corrupt note, not an UnsupportedVault refusal.
        trap_path = "end-to-end encryption.md"
        objects = {
            MILESTONE_KEY: milestone_blob(path_version=1),
            SYNC_PARAMETERS_KEY: params_blob(),
            JOURNAL_1: deflate_journal(
                serialize_journal(
                    [],
                    [
                        nfc_note_doc(
                            trap_path,
                            "1-trap000000000000000000000000000005",
                            children=["h:missing0000000000000000"],
                        )
                    ],
                )
            ),
        }
        backend, _ = await read_backend(tmp_path, objects)
        with pytest.raises(StorageError, match="corrupt") as exc_info:
            await backend.read_note(trap_path)
        assert not isinstance(exc_info.value, UnsupportedVault)
        results = [item async for item in backend.iterate_texts()]
        assert len(results) == 1
        assert results[0][0]["path"] == trap_path
        assert results[0][1] is None

    async def test_encrypted_note_refused_as_unsupported(self, tmp_path):
        doc = nfc_note_doc(
            "secret.md",
            "1-enc0000000000000000000000000000005f",
            children=["h:whatever0000000000000000"],
            e_=True,
        )
        objects = {
            MILESTONE_KEY: milestone_blob(path_version=1),
            SYNC_PARAMETERS_KEY: params_blob(),
            JOURNAL_1: deflate_journal(serialize_journal([], [doc])),
        }
        backend, _ = await read_backend(tmp_path, objects)
        with pytest.raises(UnsupportedVault, match="end-to-end encryption"):
            await backend.read_note("secret.md")


class TestIterateTexts:
    async def collect(self, backend, extension=".md"):
        return {
            meta["path"]: text
            async for meta, text in backend.iterate_texts(extension)
        }

    async def test_iterates_md_notes_in_path_order(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        results = await self.collect(backend)
        assert list(results) == MD_PATHS
        assert results[README_PATH] == README_PART_A + README_PART_B
        assert results[LEGACY_PATH] == LEGACY_TEXT
        assert results[NESTED_PATH] == NESTED_TEXT  # .MD matches case-lessly

    async def test_corrupt_note_is_isolated_not_fatal(self, tmp_path, caplog):
        backend, _ = await read_backend(tmp_path)
        with caplog.at_level(logging.WARNING, logger="storage_journal"):
            results = await self.collect(backend)
        assert results[BROKEN_PATH] is None
        # The rest of the vault still scanned to completion.
        assert len(results) == len(MD_PATHS)
        assert any(BROKEN_PATH in record.message for record in caplog.records)

    async def test_binary_png_never_surfaces_in_md_scan(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        results = await self.collect(backend)
        assert LOGO_PATH not in results
        png_only = await self.collect(backend, extension=".png")
        assert list(png_only) == [LOGO_PATH]
        assert png_only[LOGO_PATH] is None  # binary body: no text

    async def test_hidden_and_tombstone_never_surface(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        results = await self.collect(backend)
        assert HIDDEN_PATH not in results
        assert TOMBSTONE_PATH not in results

    async def test_metas_carry_winner_rev(self, tmp_path):
        backend, _ = await read_backend(tmp_path)
        revs = {
            meta["path"]: meta["rev"]
            async for meta, _ in backend.iterate_texts()
        }
        assert revs[BROKEN_PATH] == BROKEN_REV
        assert revs[README_PATH] == README_REV

    async def test_no_refresh_inside_iteration(self, tmp_path):
        backend, storage = await read_backend(tmp_path)
        # Tools own refresh: iterating must not trigger a journal listing.
        assert await self.collect(backend) is not None
        assert storage.list_calls == []
        assert storage.get_calls == []

    async def test_windowed_iteration_covers_every_note(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_journal, "_TEXT_WINDOW_SIZE", 2)
        backend, _ = await read_backend(tmp_path)
        results = await self.collect(backend)
        assert list(results) == MD_PATHS  # several windows, all yielded

    async def test_iterate_before_startup_refused(self, tmp_path):
        backend, _ = make_backend(tmp_path, read_vault_objects())
        with pytest.raises(StorageError, match="startup_check"):
            async for _ in backend.iterate_texts():
                pass

    async def test_unexpected_store_error_is_isolated(
        self, tmp_path, monkeypatch, caplog
    ):
        # Ruling 1: an unexpected per-note failure (here OSError from the
        # store) must yield (meta, None) and never end the scan — the same
        # per-note boundary the CouchDB peer has.
        backend, _ = await read_backend(tmp_path)
        store = backend._store
        original = store.read_note

        def raising_read(doc_id):
            if doc_id == path2id(README_PATH, nfc=True):
                raise OSError("simulated per-note I/O failure")
            return original(doc_id)

        monkeypatch.setattr(store, "read_note", raising_read)
        with caplog.at_level(logging.ERROR, logger="storage_journal"):
            results = await self.collect(backend)
        assert list(results) == MD_PATHS  # iteration ran to completion
        assert results[README_PATH] is None
        assert any(
            README_PATH in record.message for record in caplog.records
        )

    async def test_storage_unavailability_still_aborts_iteration(
        self, tmp_path, monkeypatch
    ):
        # Backend-wide storage unavailability is not a per-note failure:
        # the scan must abort instead of reporting the whole vault empty.
        backend, _ = await read_backend(tmp_path)
        store = backend._store

        def unavailable(doc_id):
            raise StorageUnavailable("scripted backend-wide failure")

        monkeypatch.setattr(store, "read_note", unavailable)
        with pytest.raises(StorageUnavailable):
            async for _ in backend.iterate_texts():
                pass
