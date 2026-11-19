"""Tests for the journal storage backend startup and replay (Plan B, Task 5).

Covers ``JournalBackend.startup_check`` (milestone locked/missing, settings
refusals, path-identity state mismatch, sync-parameter epoch change, remote
wipe probe), ``refresh`` (ordered replay with numeric ms ordering, corrupt
file stops without advancing the checkpoint, control objects never parsed,
throttling with ``force`` bypass) and ``backend_info``/``close``. All object
storage goes through ``FakeObjectStorage`` — no network.
"""

import json
import logging
from datetime import datetime

import pytest

from journal_codec import deflate_journal, serialize_journal
from storage_base import StorageError, StorageUnavailable, UnsupportedVault
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

    async def test_read_and_write_methods_raise_until_tasks_6_7(self, tmp_path):
        backend, _ = make_backend(tmp_path, healthy_bucket())
        with pytest.raises(NotImplementedError):
            await backend.list_notes()
        with pytest.raises(NotImplementedError):
            await backend.get_meta("a.md")
        with pytest.raises(NotImplementedError):
            await backend.read_note("a.md")
        with pytest.raises(NotImplementedError):
            await backend.write_note("a.md", "body")
        with pytest.raises(NotImplementedError):
            await backend.delete_note("a.md")
        with pytest.raises(NotImplementedError):
            await backend.rename_note("a.md", "b.md")
        with pytest.raises(NotImplementedError):
            await backend.copy_note("a.md", "b.md")
        with pytest.raises(NotImplementedError):
            backend.iterate_texts()
