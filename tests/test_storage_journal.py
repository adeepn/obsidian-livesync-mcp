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

import asyncio
import base64
import json
import logging
import unicodedata
from datetime import datetime

import pytest

import storage_journal
import journal_store
from journal_codec import (
    deflate_journal, inflate_journal, parse_journal, serialize_journal,
)
from journal_store import pick_winner
from livesync_format import ForbiddenPathError, chunk_id_for, path2id
from storage_base import (
    ConcurrentModification,
    NoteExists,
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
    async def test_locked_milestone_accepted_with_warning(self, tmp_path, caplog):
        backend, _ = make_backend(
            tmp_path,
            {
                MILESTONE_KEY: milestone_blob(locked=True),
                SYNC_PARAMETERS_KEY: params_blob(),
            },
        )
        with caplog.at_level(logging.WARNING, logger="storage_journal"):
            await backend.startup_check()
        assert any(
            record.name == "storage_journal"
            and record.levelno == logging.WARNING
            and "locked" in record.message
            and "remote marked as rebuilt" in record.message
            and "peer performs full replay" in record.message
            for record in caplog.records
        )
        await backend.close()

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
        assert sorted(storage.get_calls) == sorted([SYNC_PARAMETERS_KEY, JOURNAL_1, JOURNAL_2, JOURNAL_3])
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
        assert storage.get_calls == [SYNC_PARAMETERS_KEY, JOURNAL_1, JOURNAL_2, JOURNAL_3]
        assert await files_processed(backend) == 3

    async def test_second_refresh_after_full_replay_is_noop(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        storage.get_calls.clear()
        storage.list_calls.clear()

        await backend.refresh(force=True)
        # One complete listing (Ruling B-8: no StartAfter cursor); every
        # listed journal is already processed; only sync params are fetched.
        assert storage.list_calls == [""]
        assert storage.get_calls == [SYNC_PARAMETERS_KEY]
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
        assert storage.get_calls == [SYNC_PARAMETERS_KEY, JOURNAL_10]
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

        # Repair the broken file: force bypasses failure backoff.
        storage._objects[JOURNAL_9] = journal_blob([meta_doc("i.md", "1-i")])
        storage.get_calls.clear()
        await backend.refresh(force=True)
        assert storage.get_calls == [SYNC_PARAMETERS_KEY, JOURNAL_9, JOURNAL_10, journal_11]
        assert await files_processed(backend) == 3

    async def test_replay_error_message_sanitized(self, tmp_path, monkeypatch):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        storage.get_calls.clear()
        original_get = storage.get

        def fail_journal(key, **kwargs):
            if key == JOURNAL_1:
                raise ValueError("/abs/state/path note.md 3-x secret")
            return original_get(key, **kwargs)

        monkeypatch.setattr(storage, 'get', fail_journal)
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
        assert storage.get_calls == [SYNC_PARAMETERS_KEY, JOURNAL_2]

    async def test_control_objects_are_never_parsed_as_journals(self, tmp_path):
        backend, storage = make_backend(
            tmp_path,
            healthy_bucket(**{"_maintenance.json": b"{}"}),
        )
        await backend.startup_check()
        storage.get_calls.clear()

        await backend.refresh()
        assert storage.get_calls == [SYNC_PARAMETERS_KEY, JOURNAL_1, JOURNAL_2, JOURNAL_3]
        assert await files_processed(backend) == 3

    async def test_unavailable_get_propagates_and_keeps_checkpoint(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        storage.get_calls.clear()
        storage.fail_next_get(1)
        with pytest.raises(StorageUnavailable):
            await backend.refresh()
        assert await files_processed(backend) == 0
        assert storage.get_calls == [SYNC_PARAMETERS_KEY]

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

    async def test_failed_refresh_is_throttled(self, tmp_path):
        backend, storage = make_backend(
            tmp_path, healthy_bucket(**{JOURNAL_1: b"corrupt"}), refresh_min_interval=60.0
        )
        await backend.startup_check()
        with pytest.raises(StorageError):
            await backend.refresh()
        # A hostile file cannot cause unlimited immediate retry work.
        with pytest.raises(StorageError, match="refresh failed previously"):
            await backend.refresh()
        assert len(storage.list_calls) == 1
        with pytest.raises(StorageError):
            await backend.refresh(force=True)
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

    async def test_write_methods_refuse_before_startup(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        with pytest.raises(StorageError, match="startup_check"):
            await backend.write_note("a.md", "body")
        with pytest.raises(StorageError, match="startup_check"):
            await backend.delete_note("a.md")
        with pytest.raises(StorageError, match="startup_check"):
            await backend.rename_note("a.md", "b.md")
        with pytest.raises(StorageError, match="startup_check"):
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


# Task 7: exercise publication at the object boundary and replay with real stores.


async def write_backend(tmp_path, *, objects=None):
    backend, storage = make_backend(tmp_path, objects or {
        MILESTONE_KEY: milestone_blob(path_version=1),
        SYNC_PARAMETERS_KEY: params_blob(),
    })
    await backend.startup_check()
    await backend.refresh()
    storage.get_calls.clear()
    storage.list_calls.clear()
    return backend, storage


class TestJournalWrites:
    async def test_create_revision_chain_replace_and_delete(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        first = await backend.write_note('A.md', 'привет\n', create=True, mtime=123)
        assert first['ctime'] == first['mtime'] == 123
        assert first['size'] == 13
        doc = backend._store.winner('a.md')
        assert doc['_revisions'] == {'start': 1, 'ids': [first['rev'].split('-', 1)[1]]}
        assert storage.get_calls == [storage.put_calls[0][0]]
        second = await backend.write_note('A.md', 'new', expected_rev=first['rev'])
        doc = backend._store.winner('a.md')
        assert doc['_revisions']['start'] == 2
        assert doc['_revisions']['ids'] == [second['rev'].split('-', 1)[1], first['rev'].split('-', 1)[1]]
        assert (await backend.read_note('A.md'))['text'] == 'new'
        before = doc.copy()
        assert await backend.delete_note('A.md', expected_rev=second['rev'])
        deleted = backend._store.winner('a.md')
        assert deleted['deleted'] is True
        for field in ('children', 'eden', 'type', 'path', 'ctime'):
            assert deleted[field] == before[field]
        assert deleted['_revisions']['start'] == 3
        assert await backend.read_note('A.md') is None
        assert not await backend.delete_note('A.md')
        with pytest.raises(NoteNotFound):
            await backend.write_note('A.md', 'no', overwrite=True)
        restored = await backend.write_note('A.md', 'restored', create=True)
        assert restored['rev'].startswith('4-')
        assert (await backend.read_note('a.md'))['text'] == 'restored'
        assert not hasattr(storage, 'delete')

    async def test_create_and_missing_rules_no_publication(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('a.md', 'original', create=True)
        count = len(storage.put_calls)
        with pytest.raises(NoteExists):
            await backend.write_note('a.md', 'bad', create=True)
        with pytest.raises(NoteNotFound):
            await backend.write_note('absent.md', 'bad', overwrite=True)
        assert len(storage.put_calls) == count
        await backend.write_note('a.md', 'replace', create=True, overwrite=True)
        assert (await backend.read_note('a.md'))['text'] == 'replace'

    @pytest.mark.parametrize('operation', ['write', 'delete'])
    @pytest.mark.parametrize('path', ['a.md', 'missing.md'])
    async def test_stale_revision_never_puts(self, tmp_path, operation, path):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('a.md', 'original', create=True)
        storage.put_calls.clear()
        with pytest.raises(ConcurrentModification, match='changed concurrently; re-read and retry'):
            if operation == 'write':
                await backend.write_note(path, 'bad', expected_rev='1-stale')
            else:
                await backend.delete_note(path, expected_rev='1-stale')
        assert storage.put_calls == []
        assert (await backend.read_note('a.md'))['text'] == 'original'

    @pytest.mark.parametrize('kind', ['missing', 'corrupt', 'other-records', 'extra-record'])
    async def test_readback_failure_does_not_change_store(self, tmp_path, monkeypatch, kind):
        backend, storage = await write_backend(tmp_path)
        old = await backend.write_note('a.md', 'original', create=True)
        checkpoint = backend._store.last_sent_key
        count = backend._store.files_processed_count()
        original_get = storage.get

        def wrong_get(key):
            blob = original_get(key)
            if kind == 'missing':
                return None
            if kind == 'corrupt':
                return b'not deflate'
            journal = parse_journal(inflate_journal(blob))
            if kind == 'other-records':
                journal.docs[0]['path'] = 'wrong.md'
            else:
                journal.docs.append(meta_doc('extra.md', '1-extra'))
            return deflate_journal(serialize_journal(list(journal.chunks.items()), journal.docs))

        monkeypatch.setattr(storage, 'get', wrong_get)
        with pytest.raises(StorageError):
            await backend.write_note('a.md', 'replacement')
        assert backend._store.last_sent_key == checkpoint
        assert backend._store.files_processed_count() == count
        assert (await backend.get_meta('a.md')) == old
        assert (await backend.read_note('a.md'))['text'] == 'original'
        assert not backend._store._chunk_path(chunk_id_for('replacement')).exists()

    async def test_fresh_peer_roundtrip_three_notes(self, tmp_path):
        writer, storage = await write_backend(tmp_path / 'writer')
        texts = {'a.md': 'one\n', 'Заметки/Приём.md': 'два', 'c.canvas': '{"nodes":[]}'}
        for path, text in texts.items():
            await writer.write_note(path, text, create=True)
        reader = JournalBackend(storage, tmp_path / 'reader')
        await reader.startup_check()
        await reader.refresh()
        for path, text in texts.items():
            assert (await reader.read_note(path))['text'] == text
            assert await reader.get_meta(path) == await writer.get_meta(path)
        assert len(await reader.list_notes()) == 3
        await reader.close()
        await writer.close()

    async def test_monotonic_names_include_processed_keys_and_clock_rollback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_journal.time, 'time', lambda: 10)
        objects = {MILESTONE_KEY: milestone_blob(), SYNC_PARAMETERS_KEY: params_blob(),
                   '10000-docs.jsonl.gz': journal_blob([meta_doc('old.md', '1-old')])}
        backend, storage = await write_backend(tmp_path, objects=objects)
        await backend.refresh(force=True)  # processed keys must still contribute
        await backend.write_note('a.md', 'one', create=True)
        await backend.write_note('a.md', 'two')
        monkeypatch.setattr(storage_journal.time, 'time', lambda: 1)
        await backend.write_note('a.md', 'three')
        names = [key for key, _ in storage.put_calls]
        numbers = [int(key.split('-')[0]) for key in names]
        assert numbers[0] > 10000
        assert numbers[0] < numbers[1] < numbers[2]
        assert len(set(names)) == 3

    async def test_device_sibling_kept_and_listing_unique(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('a.md', 'ours', create=True)
        ours = backend._store.winner('a.md')
        device = {**ours, '_rev': '1-ffffffffffffffffffffffffffffffff',
                  '_revisions': {'start': 1, 'ids': ['ffffffffffffffffffffffffffffffff']}}
        storage.put('9999999999999-docs.jsonl.gz', journal_blob([device]))
        await backend.refresh(force=True)
        leaves = backend._store.leaves('a.md')
        assert {doc['_rev'] for doc in leaves} == {ours['_rev'], device['_rev']}
        assert backend._store.winner('a.md') == pick_winner(leaves)
        assert backend._store.note_ids() == ['a.md']
        assert len(await backend.list_notes()) == 1

    @pytest.mark.parametrize('operation', ['write', 'delete', 'rename-source', 'rename-target', 'copy-source', 'copy-target'])
    async def test_forbidden_before_any_storage_call(self, tmp_path, operation):
        backend, storage = await write_backend(tmp_path)
        with pytest.raises(ForbiddenPathError):
            if operation == 'write':
                await backend.write_note('.obsidian/x.md', 'bad', create=True)
            elif operation == 'delete':
                await backend.delete_note('.obsidian/x.md')
            else:
                method = backend.rename_note if operation.startswith('rename') else backend.copy_note
                args = ('.obsidian/x.md', 'ok.md') if operation.endswith('source') else ('ok.md', '.obsidian/x.md')
                await method(*args)
        assert storage.put_calls == storage.get_calls == storage.list_calls == []

    @pytest.mark.parametrize('full_json', [False, True])
    async def test_shared_chunk_collision_refused(self, tmp_path, full_json):
        backend, storage = await write_backend(tmp_path)
        chunk = chunk_id_for('wanted')
        backend._store.apply_journal('collision', {} if full_json else {chunk: 'different'},
                                    [{'_id': chunk, '_rev': '1-bad', 'type': 'leaf', 'data': 'different'}] if full_json else [])
        with pytest.raises(StorageError):
            await backend.write_note('a.md', 'wanted', create=True)
        assert storage.put_calls == []
        assert await backend.read_note('a.md') is None


class TestJournalMoves:
    async def test_same_path_and_case_only_rename(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        first = await backend.write_note('A.md', 'body', create=True)
        storage.put_calls.clear()
        assert await backend.rename_note('A.md', 'A.md') == first
        assert storage.put_calls == []
        renamed = await backend.rename_note('A.md', 'a.MD')
        assert renamed['path'] == 'a.MD'
        assert renamed['rev'].startswith('2-')
        journal = parse_journal(inflate_journal(storage.put_calls[0][1]))
        assert len(journal.docs) == 1
        assert not journal.docs[0].get('deleted')
        assert journal.chunks == {}
        assert (await backend.read_note('A.md'))['text'] == 'body'

    @pytest.mark.parametrize('operation', ['rename_note', 'copy_note'])
    async def test_target_rules_and_missing_source(self, tmp_path, operation):
        backend, storage = await write_backend(tmp_path)
        method = getattr(backend, operation)
        await backend.write_note('a.md', 'source', create=True)
        target = await backend.write_note('b.md', 'target', create=True)
        count = len(storage.put_calls)
        with pytest.raises(NoteExists):
            await method('a.md', 'b.md')
        with pytest.raises(NoteNotFound):
            await method('missing.md', 'c.md')
        assert len(storage.put_calls) == count
        result = await method('a.md', 'b.md', overwrite=True)
        doc = backend._store.winner('b.md')
        assert result['rev'].startswith('2-')
        assert doc['_revisions']['ids'][1] == target['rev'].split('-', 1)[1]
        assert (await backend.read_note('b.md'))['text'] == 'source'
        assert (await backend.read_note('a.md') is None) == (operation == 'rename_note')

    @pytest.mark.parametrize('operation', ['rename_note', 'copy_note'])
    async def test_eden_only_body_and_ctime_preserved(self, tmp_path, operation):
        backend, storage = await write_backend(tmp_path)
        chunk = chunk_id_for('inline')
        source = {'_id': 'a.md', '_rev': '3-source', '_revisions': {'start': 3, 'ids': ['source', 'parent', 'root']},
                  'path': 'a.md', 'type': 'plain', 'children': [chunk],
                  'eden': {chunk: {'data': 'inline'}}, 'ctime': 12, 'mtime': 34, 'size': 6}
        storage.put('1-docs.jsonl.gz', journal_blob([source]))
        await backend.refresh(force=True)
        storage.put_calls.clear()
        await getattr(backend, operation)('a.md', 'b.md')
        doc = backend._store.winner('b.md')
        assert doc['children'] == source['children']
        assert doc['eden'] == source['eden']
        assert doc['ctime'] == 12
        assert doc['_revisions']['start'] == 1
        assert (await backend.read_note('b.md'))['text'] == 'inline'
        assert len(storage.put_calls) == 1
        journal = parse_journal(inflate_journal(storage.put_calls[0][1]))
        assert journal.chunks == {}
        assert len(journal.docs) == (2 if operation == 'rename_note' else 1)
        if operation == 'rename_note':
            assert journal.docs[1]['deleted'] is True
            assert journal.docs[1]['_revisions']['ids'][1:] == ['source', 'parent', 'root']
        reader = JournalBackend(storage, tmp_path / 'fresh')
        await reader.startup_check()
        await reader.refresh()
        assert (await reader.read_note('b.md'))['text'] == 'inline'
        await reader.close()

    @pytest.mark.parametrize('operation', ['rename_note', 'copy_note'])
    async def test_failed_publication_preserves_source_and_target(self, tmp_path, operation, monkeypatch):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('a.md', 'source', create=True)
        await backend.write_note('b.md', 'target', create=True)
        monkeypatch.setattr(storage, 'get', lambda key: None)
        with pytest.raises(StorageError):
            await getattr(backend, operation)('a.md', 'b.md', overwrite=True)
        assert (await backend.read_note('a.md'))['text'] == 'source'
        assert (await backend.read_note('b.md'))['text'] == 'target'


class TestJournalWriteEdges:
    async def test_same_ms_independent_writers_replay_both_notes(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(storage_journal.time, 'time', lambda: 10)
        first, storage = await write_backend(tmp_path / 'first')
        second = JournalBackend(storage, tmp_path / 'second')
        await second.startup_check()
        await second.refresh()
        try:
            results = await asyncio.gather(
                first.write_note('a.md', 'first', create=True),
                second.write_note('b.md', 'second', create=True),
            )
            keys = [key for key, _ in storage.put_calls]
            assert len(keys) == len(set(keys)) == 2
            assert all(storage.get(key) == blob
                       for key, blob in storage.put_calls)
            reader = JournalBackend(storage, tmp_path / 'reader')
            await reader.startup_check()
            await reader.refresh()
            try:
                assert len(await reader.list_notes()) == 2
                for path, text, meta in zip(
                    ['a.md', 'b.md'], ['first', 'second'], results,
                ):
                    assert (await reader.read_note(path))['text'] == text
                    assert await reader.get_meta(path) == meta
            finally:
                await reader.close()
        finally:
            await first.close()
            await second.close()

    @pytest.mark.parametrize('exhausted', [False, True])
    async def test_random_key_collision_retries_without_overwrite(
        self, tmp_path, monkeypatch, exhausted,
    ):
        backend, storage = await write_backend(tmp_path)
        monkeypatch.setattr(storage_journal.time, 'time', lambda: 10)
        occupied = '10000-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-docs.jsonl.gz'
        original = journal_blob([meta_doc('existing.md', '1-existing')])
        storage.put(occupied, original)
        storage.put_calls.clear()
        real_token_hex = storage_journal.secrets.token_hex
        attempts = []

        revision_generated = False

        def token_hex(length):
            nonlocal revision_generated
            assert length == 16
            if not revision_generated:
                revision_generated = True
                return real_token_hex(length)
            attempts.append(length)
            return ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' if exhausted or len(attempts) == 1
                    else 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')

        monkeypatch.setattr(storage_journal.secrets, 'token_hex', token_hex)
        try:
            if exhausted:
                with pytest.raises(StorageError, match='journal key'):
                    await backend.write_note('a.md', 'body', create=True)
                assert storage.put_calls == []
                assert backend._store.note_ids() == []
                assert len(attempts) == 10
            else:
                await backend.write_note('a.md', 'body', create=True)
                assert [key for key, _ in storage.put_calls] == [
                    '10000-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-docs.jsonl.gz',
                ]
                assert (await backend.read_note('a.md'))['text'] == 'body'
            assert storage.get(occupied) == original
        finally:
            await backend.close()

    @pytest.mark.parametrize('failure', [
        'before-apply', 'before-chunk-replace', 'after-chunk-replace',
    ])
    async def test_verified_publication_recovers_after_apply_failure(
        self, tmp_path, monkeypatch, failure,
    ):
        backend, storage = await write_backend(tmp_path)
        store = backend._store
        checkpoint = store.last_sent_key
        real_replace = journal_store.os.replace

        def fail_apply(name, chunks, docs):
            assert storage.get_calls[-1] == name
            assert storage.get(name) is not None
            raise OSError('scripted local apply failure')

        def fail_replace(source, target):
            assert len(storage.put_calls) == 1
            if failure == 'after-chunk-replace':
                real_replace(source, target)
            raise OSError('scripted chunk publication failure')

        try:
            with monkeypatch.context() as injection:
                if failure == 'before-apply':
                    injection.setattr(store, 'apply_journal', fail_apply)
                else:
                    injection.setattr(journal_store.os, 'replace', fail_replace)
                with pytest.raises(StorageError):
                    await backend.write_note('a.md', 'recover me', create=True)
            assert len(storage.put_calls) == 1
            key, blob = storage.put_calls[0]
            published = parse_journal(inflate_journal(blob)).docs[0]
            assert storage.get(key) == blob
            assert not store.processed(key)
            assert store.files_processed_count() == 0
            assert store.last_sent_key == checkpoint
            assert store._chunk_path(chunk_id_for('recover me')).exists() == (
                failure == 'after-chunk-replace'
            )
            await backend.refresh(force=True)
            assert (await backend.read_note('a.md'))['text'] == 'recover me'
            assert (await backend.get_meta('a.md'))['rev'] == published['_rev']
            assert store.winner('a.md') == published
            assert store.leaves('a.md') == [published]
            assert store.processed(key)
            assert store.files_processed_count() == 1
            await backend.refresh(force=True)
            assert store.leaves('a.md') == [published]
            assert store.files_processed_count() == 1
        finally:
            await backend.close()

    @pytest.mark.parametrize('failure', ['put', 'get'])
    async def test_transport_failure_no_local_success(self, tmp_path, failure):
        backend, storage = await write_backend(tmp_path)
        if failure == 'put':
            storage.fail_next_put()
        else:
            storage.fail_next_get()
        with pytest.raises(StorageUnavailable):
            await backend.write_note('a.md', 'body', create=True)
        assert backend._store.note_ids() == []
        assert backend._store.last_sent_key == ''
        assert backend._store.files_processed_count() == 0

    async def test_reuse_identical_chunk_reset_eden_and_preserve_ctime(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        chunk = chunk_id_for('body')
        source = nfc_note_doc('a.md', '1-source', children=[chunk],
                              eden={chunk: {'data': 'inline'}}, ctime=12)
        backend._store.apply_journal('seed', {chunk: 'body'}, [source])
        await backend.write_note('a.md', 'body')
        doc = backend._store.winner('a.md')
        assert doc['eden'] == {}
        assert doc['ctime'] == 12
        await backend.write_note('b.md', 'body', create=True)
        assert (await backend.read_note('a.md'))['text'] == 'body'
        assert (await backend.read_note('b.md'))['text'] == 'body'
        assert backend._store._chunk_path(chunk).read_bytes() == b'body'

    @pytest.mark.parametrize('operation', ['rename_note', 'copy_note'])
    async def test_binary_reference_copy_and_tombstoned_target(self, tmp_path, operation):
        backend, storage = await write_backend(tmp_path, objects=read_vault_objects())
        await backend.write_note('target.md', 'old', create=True)
        await backend.delete_note('target.md')
        tombstone = backend._store.winner('target.md')
        await getattr(backend, operation)(LOGO_PATH, 'target.md')
        copied = backend._store.winner('target.md')
        original = backend._store.winner(path2id(LOGO_PATH, nfc=True))
        assert copied['children'] == original['children']
        assert copied['type'] == 'newnote'
        assert copied['ctime'] == original['ctime']
        assert not copied.get('deleted')
        assert copied['_revisions']['ids'][1] == tombstone['_rev'].split('-', 1)[1]
        assert (await backend.read_note('target.md'))['data'] == LOGO_BYTES

    @pytest.mark.parametrize('operation', ['rename_note', 'copy_note'])
    async def test_deleted_source_cannot_move_or_copy(self, tmp_path, operation):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('a.md', 'body', create=True)
        await backend.delete_note('a.md')
        storage.put_calls.clear()
        with pytest.raises(NoteNotFound):
            await getattr(backend, operation)('a.md', 'b.md')
        assert storage.put_calls == []

    async def test_copy_same_identity_requires_overwrite(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note('A.md', 'body', create=True)
        with pytest.raises(NoteExists):
            await backend.copy_note('A.md', 'a.md')
        copied = await backend.copy_note('A.md', 'a.md', overwrite=True)
        assert copied['rev'].startswith('2-')
        assert (await backend.read_note('a.md'))['text'] == 'body'

    async def test_nfc_and_nfd_write_share_identity(self, tmp_path):
        backend, storage = await write_backend(tmp_path)
        await backend.write_note(PRIEM_PATH_NFD, 'first', create=True)
        await backend.write_note(PRIEM_PATH, 'second')
        assert backend._store.note_ids() == [path2id(PRIEM_PATH, nfc=True)]
        assert (await backend.read_note(PRIEM_PATH_NFD))['text'] == 'second'

    @pytest.mark.parametrize('chain', [ {'start': 4, 'ids': ['parent']}, {'start': 1, 'ids': ['wrong']}, {'start': 1, 'ids': []} ])
    async def test_bad_parent_ancestry_refused_before_put(self, tmp_path, chain):
        backend, storage = await write_backend(tmp_path)
        backend._store.apply_journal('seed', {}, [nfc_note_doc('a.md', '1-parent', _revisions=chain)])
        with pytest.raises(StorageError, match='ancestry'):
            await backend.write_note('a.md', 'body')
        assert storage.put_calls == []


class TestRuntimeHistoryGuards:
    @pytest.mark.parametrize('changed_epoch', [False, True])
    async def test_refresh_replays_replacement_history(self, tmp_path, changed_epoch):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        sent = await backend.write_note('sent.md', 'old', create=True)
        last_sent = backend._store.last_sent_key
        # Keep a processed key but replace its contents: reset must replay it.
        replacement = meta_doc('replacement.md', '1-new')
        storage._objects[JOURNAL_1] = journal_blob([replacement])
        if changed_epoch:
            storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt='salt-b')
        else:
            del storage._objects[last_sent]
        storage.get_calls.clear()
        await backend.refresh(force=True)
        paths = {meta['path'] for meta in await backend.list_notes()}
        assert 'replacement.md' in paths
        assert 'a.md' not in paths
        assert (sent['path'] in paths) == changed_epoch
        assert storage.get_calls.count(SYNC_PARAMETERS_KEY) == 1
        assert backend._store.epoch == ('2:salt-b' if changed_epoch else '2:salt-a')
        await backend.close()

    async def test_write_after_wipe_resets_and_refuses_publication(self, tmp_path):
        backend, storage = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        await backend.refresh()
        original = await backend.write_note('sent.md', 'old', create=True)
        del storage._objects[backend._store.last_sent_key]
        before = dict(storage._objects)
        puts = len(storage.put_calls)
        with pytest.raises(ConcurrentModification, match='remote was rebuilt; retry the operation after refresh'):
            await backend.write_note('sent.md', 'stale', expected_rev=original['rev'])
        assert storage._objects == before
        assert len(storage.put_calls) == puts
        assert await backend.list_notes() == []
        await backend.refresh()  # reset also clears the throttle
        assert await files_processed(backend) == 3
        await backend.close()


@pytest.mark.parametrize('field,value', [
    ('mtime', float('1e999')), ('ctime', 'x'),
    ('mtime', -1), ('ctime', True), ('mtime', float('-inf')),
])
async def test_invalid_timestamps_are_isolated(tmp_path, field, value):
    good = meta_doc('good.md', '1-good')
    good.update(type='notes', data='valid')
    bad = meta_doc('bad.md', '1-bad')
    bad.update(type='notes', data='invalid')
    bad[field] = value
    # Valid JSON exponent overflows Python's float parser. Bypass the
    # publication serializer, which correctly refuses non-finite numbers.
    payload = (json.dumps(good) + '\n' +
               json.dumps(bad).replace('Infinity', '1e999') + '\n').encode()
    backend, _ = make_backend(tmp_path, healthy_bucket(**{
        JOURNAL_1: deflate_journal(payload), JOURNAL_2: None, JOURNAL_3: None,
    }))
    await backend.startup_check()
    await backend.refresh()
    assert [meta['path'] for meta in await backend.list_notes()] == ['good.md']
    assert [(meta['path'], text) async for meta, text in backend.iterate_texts('.md')] == [('good.md', 'valid')]
    await backend.close()

@pytest.mark.parametrize("limit,value,match", [
    ("max_line_bytes", 30, "line length"),
    ("max_inflated_bytes", 30, "inflated"),
    ("max_records", 1, "record count"),
    ("max_refresh_bytes", 1, "download"),
    ("max_object_bytes", 1, "object"),
])
async def test_publication_preflight_limits(tmp_path, limit, value, match):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    setattr(storage if limit == "max_object_bytes" else backend, limit, value)
    with pytest.raises(StorageError, match=match):
        await backend.write_note("a.md", "body", create=True)
    assert storage.put_calls == []
    assert backend._store.winner("a.md") is None
    assert backend._store.last_sent_key == ""


async def test_publication_rejects_oversized_chunk_before_put(tmp_path):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    with pytest.raises(StorageError, match="line length"):
        backend._publish([("h:oversized", "x" * (64 * 1024**2 + 1))], [])
    assert storage.put_calls == []
    assert backend._store.winner("big.md") is None


async def test_publication_preflights_store_revision(tmp_path):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    with pytest.raises(StorageError):
        backend._publish([], [meta_doc("bad.md", "invalid")])
    assert storage.put_calls == []


@pytest.mark.parametrize("observation", ["startup", "refresh", "unknown"])
async def test_publication_reserves_control_read_bytes(tmp_path, observation):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    params = storage._objects[SYNC_PARAMETERS_KEY]
    if observation == "refresh":
        # The latest control read must replace the startup observation.
        params += b" " * 2048
        storage._objects[SYNC_PARAMETERS_KEY] = params
        await backend.refresh(force=True)
    elif observation == "unknown":
        backend._sync_parameters_bytes = None
    docs = [meta_doc("new.md", "1-new")]
    blob = deflate_journal(serialize_journal([], docs))
    reserve = 64 * 1024 if observation == "unknown" else len(params) + 1024
    backend.max_refresh_bytes = len(blob) + reserve - 1
    before = dict(storage._objects)
    try:
        with pytest.raises(StorageError, match="download"):
            backend._publish([], docs)
        assert storage.put_calls == []
        assert storage._objects == before
        # Original reviewer case: payload alone fits the entire budget.
        backend.max_refresh_bytes = len(blob)
        with pytest.raises(StorageError, match="download"):
            backend._publish([], docs)
        assert storage.put_calls == []
    finally:
        await backend.close()


async def test_accepted_publication_fits_fresh_refresh(tmp_path):
    backend, storage = make_backend(tmp_path, {
        MILESTONE_KEY: milestone_blob(), SYNC_PARAMETERS_KEY: params_blob(),
    })
    await backend.startup_check()
    docs = [meta_doc("new.md", "1-new")]
    blob = deflate_journal(serialize_journal([], docs))
    budget = len(blob) + len(params_blob()) + 1024
    backend.max_refresh_bytes = budget
    reader = JournalBackend(storage, tmp_path / "reader", max_refresh_bytes=budget)
    try:
        backend._publish([], docs)
        assert len(storage.put_calls) == 1
        await reader.startup_check()
        await reader.refresh()
        assert reader._store.processed(backend._store.last_sent_key)
        assert reader._store.winner("new.md") == docs[0]
    finally:
        await reader.close()
        await backend.close()


@pytest.mark.parametrize("force", [False, True])
async def test_refresh_failure_recovers_after_retry(tmp_path, force):
    backend, storage = make_backend(
        tmp_path, healthy_bucket(**{JOURNAL_1: b"corrupt"}), refresh_min_interval=60
    )
    await backend.startup_check()
    with pytest.raises(StorageError):
        await backend.refresh()
    with pytest.raises(StorageError, match="refresh failed previously"):
        await backend.refresh()
    storage.put(JOURNAL_1, journal_blob([meta_doc("a.md", "1-a")]))
    if not force:
        backend._last_refresh -= 61
    await backend.refresh(force=force)
    await backend.refresh()
    assert backend._store.winner("a.md")["_rev"] == "1-a"


async def test_entire_latex_note_survives_publish_readback_and_parse(tmp_path):
    backend, storage = await write_backend(tmp_path)
    text = r"\frac{1}{2}"
    try:
        await backend.write_note("Math.md", text, create=True)
        key, blob = storage.put_calls[-1]
        assert key in storage.get_calls  # publication read-back was performed
        parsed = parse_journal(inflate_journal(storage.get(key)), name=key)
        assert list(parsed.chunks.values()) == [text]
        assert (await backend.read_note("Math.md"))["text"] == text
    finally:
        await backend.close()


async def test_backend_info_state_size(tmp_path):
    backend, _ = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        info = await backend.backend_info()
        assert info["state_dir_path"] == str(tmp_path / "state")
        assert info["state_dir_bytes"] > 0
        assert info["state_dir_bytes"] == backend._store.state_size_bytes()
    finally:
        await backend.close()


class TestMetadataCanary:
    async def test_json_chunk_has_no_protocol_notes(self, tmp_path):
        chunk = {"_id": "h:abc", "_rev": "1-a", "type": "leaf", "data": "body"}
        backend, _ = make_backend(tmp_path, healthy_bucket(**{
            JOURNAL_1: journal_blob([chunk]), JOURNAL_2: None, JOURNAL_3: None,
        }))
        try:
            await backend.startup_check()
            await backend.refresh(force=True)
            assert (await backend.backend_info())["protocol_notes"] == []
        finally:
            await backend.close()

    async def test_unknown_fields_are_sorted_cached_and_cleared(self, tmp_path):
        doc = dict(meta_doc("a.md", "1-a"), foo=1, zeta=True)
        backend, storage = make_backend(tmp_path, healthy_bucket(**{
            JOURNAL_1: journal_blob([doc]), JOURNAL_2: None, JOURNAL_3: None,
        }))
        await backend.startup_check()
        await backend.refresh(force=True)
        expected = ["unknown metadata fields in recent journals: foo, zeta"]
        assert (await backend.backend_info()).get("protocol_notes") == expected
        await backend.refresh(force=True)
        assert (await backend.backend_info()).get("protocol_notes") == expected
        storage.put(JOURNAL_2, b"corrupt")
        with pytest.raises(StorageError, match="could not be replayed"):
            await backend.refresh(force=True)
        assert (await backend.backend_info()).get("protocol_notes") == expected
        with pytest.raises(StorageError, match="refresh failed previously"):
            await backend.refresh()
        storage.put(JOURNAL_2, journal_blob([meta_doc("b.md", "1-b")]))
        await backend.refresh(force=True)
        assert not (await backend.backend_info()).get("protocol_notes")
        await backend.close()

    async def test_golden_fixture_has_no_protocol_notes(self, tmp_path):
        from pathlib import Path
        sample = Path(__file__).parent / "fixtures/live_client_journal_sample.bin"
        backend, _ = make_backend(tmp_path, healthy_bucket(**{
            JOURNAL_1: sample.read_bytes(), JOURNAL_2: None, JOURNAL_3: None,
        }))
        await backend.startup_check()
        await backend.refresh(force=True)
        assert not (await backend.backend_info()).get("protocol_notes")
        await backend.close()


@pytest.mark.parametrize("nfc", [False, True])
async def test_sensitive_paths_and_folder_identity(tmp_path, nfc):
    milestone = json.loads(milestone_blob(path_version=int(nfc)))
    milestone["tweak_values"]["PREFERRED"]["handleFilenameCaseSensitive"] = True
    backend, _ = make_backend(tmp_path, healthy_bucket(**{
        MILESTONE_KEY: json.dumps(milestone).encode(),
    }))
    try:
        await backend.startup_check()
        upper = await backend.write_note("Folder/A.md", "upper", create=True)
        assert upper["doc_id"] == "Folder/A.md"
        await backend.write_note("Folder/a.md", "lower", create=True)
        assert (await backend.read_note("Folder/A.md"))["text"] == "upper"
        assert (await backend.read_note("Folder/a.md"))["text"] == "lower"
        assert [m["path"] for m in await backend.list_notes(folder="Folder")] == [
            "Folder/A.md", "Folder/a.md"]
        assert await backend.list_notes(folder="folder") == []
        if nfc:
            await backend.write_note("ééééé/sub/x.md", "nested", create=True)
            folder_nfd = "e\u0301" * 5
            assert await backend.list_notes(folder=folder_nfd, recursive=False) == []
            nested = await backend.list_notes(folder=folder_nfd, recursive=True)
            assert [m["path"] for m in nested] == ["ééééé/sub/x.md"]
        await backend.write_note("Café.md", "one", create=True)
        if nfc:
            with pytest.raises(NoteExists):
                await backend.write_note("Cafe\u0301.md", "two", create=True)
        else:
            await backend.write_note("Cafe\u0301.md", "two", create=True)
    finally:
        await backend.close()


def test_identity_paths_have_no_unconditional_lowercase():
    import ast
    import pathlib
    for name in ("storage_journal.py", "storage_couchdb.py", "journal_store.py", "links.py"):
        tree = ast.parse(pathlib.Path(name).read_text())

        def inspect(node, function=None):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"lower", "casefold"}):
                # Extension matching stays insensitive. The link comparison
                # helper is identity-aware and covered by four-mode tests.
                allowed = ({"_without_md", "_comparison_key"} if name == "links.py"
                           else {"_matches_extension"})
                assert function in allowed, (name, node.lineno, function)
            for child in ast.iter_child_nodes(node):
                inspect(child, function)

        inspect(tree)


@pytest.mark.parametrize("deleted", [False, True])
async def test_replay_rejects_metadata_identity_collision(tmp_path, deleted):
    backend, _ = make_backend(tmp_path, healthy_bucket(**{
        JOURNAL_1: journal_blob([
            dict(meta_doc("A.md", "1-a"), deleted=deleted),
            dict(meta_doc("a.md", "1-b"), deleted=deleted),
        ]),
    }))
    try:
        await backend.startup_check()
        with pytest.raises(StorageError, match="could not be replayed"):
            await backend.refresh(force=True)
        assert not backend._store.processed(JOURNAL_1)
    finally:
        await backend.close()


async def test_deleted_metadata_identity_refused_before_resurrection(tmp_path):
    milestone = json.loads(milestone_blob())
    milestone["tweak_values"]["PREFERRED"]["handleFilenameCaseSensitive"] = True
    backend, objects = make_backend(tmp_path, healthy_bucket(**{
        MILESTONE_KEY: json.dumps(milestone).encode(),
    }))
    try:
        await backend.startup_check()
        # Seed an already-cached tombstone to exercise publication, not replay.
        backend._store.apply_journal("cached", {}, [{
            "_id": "a.md", "_rev": "1-a", "path": "A.md", "type": "plain",
            "deleted": True, "children": [], "size": 0,
        }])
        before = list(objects.put_calls)
        with pytest.raises(StorageError, match="identity mismatch"):
            await backend.write_note("a.md", "replacement", create=True)
        assert objects.put_calls == before
        assert backend._store.winner("a.md")["_rev"] == "1-a"
    finally:
        await backend.close()
