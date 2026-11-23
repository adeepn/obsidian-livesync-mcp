"""In-memory storage double shared by tool tests."""

from copy import deepcopy

from livesync_format import is_text_doc
from storage_base import (
    ConcurrentModification, NoteExists, NoteNotFound, StorageError,
)


class FakeBackend:
    """Emulates the ``StorageBackend`` contract, including revisions.

    Every mutation bumps a monotonic per-note revision (``"1-x"``,
    ``"2-x"``, ...). ``write_note``/``delete_note`` honor ``expected_rev``
    like CouchDB: a stale or vanished revision raises
    ``ConcurrentModification`` instead of clobbering the newer write.
    """

    def __init__(self, notes=None):
        self.notes = notes or {}
        self.calls = []
        self.errors = {}

    def _call(self, method, *args):
        self.calls.append((method, args))
        error = self.errors.get((method, *args), self.errors.get(method))
        if error:
            raise error

    def _next_rev(self, path):
        previous = self.notes.get(path, {}).get("rev")
        if previous is None:
            return "1-x"
        try:
            generation = int(previous.split("-", 1)[0])
        except ValueError:
            generation = 1
        return f"{generation + 1}-x"

    def add(self, path, text=None, data=None, **meta):
        rev = self._next_rev(path)
        self.notes[path] = {
            "meta": {"path": path, "doc_id": path.lower(), "ctime": 1,
                     "mtime": 2, "size": len(data or (text or "").encode()),
                     "type": "plain" if text is not None else "newnote",
                     "rev": rev, **meta},
            "text": text, "data": data, "rev": rev,
        }

    def _current_rev(self, path):
        note = self.notes.get(path)
        return note["rev"] if note is not None else None

    async def startup_check(self):
        self._call("startup_check")

    async def refresh(self, force=False):
        if force:
            self._call("refresh", force)
        else:
            self._call("refresh")

    async def close(self):
        self._call("close")

    def check_writer_support(self) -> None:
        pass

    async def backend_info(self):
        self._call("backend_info")
        return {"backend": "fake"}

    async def list_notes(self, folder="", extension=None, recursive=True):
        self._call("list_notes", folder, extension, recursive)
        prefix = folder.rstrip("/") + "/" if folder else ""
        return [deepcopy(n["meta"]) for p, n in self.notes.items()
                if p.startswith(prefix)
                and (recursive or "/" not in p[len(prefix):])
                and (not extension or p.endswith(extension))]

    async def get_meta(self, path):
        self._call("get_meta", path)
        if path not in self.notes:
            raise NoteNotFound(path)
        return deepcopy(self.notes[path]["meta"])

    async def read_note(self, path):
        self._call("read_note", path)
        note = deepcopy(self.notes.get(path))
        if note is not None:
            note["is_text"] = is_text_doc(note["meta"]["type"], path)
        return note

    async def iterate_texts(self, extension=".md"):
        self._call("iterate_texts", extension)
        for path, note in self.notes.items():
            if not path.endswith(extension):
                continue
            try:
                content = await self.read_note(path)
                text = content["text"] if content["is_text"] else None
            except StorageError:
                text = None
            yield deepcopy(note["meta"]), text

    async def write_note(self, path, content, *, create=False,
                         overwrite=False, mtime=None, expected_rev=None):
        self._call("write_note", path, content, create, overwrite, mtime,
                   expected_rev)
        if expected_rev is not None and self._current_rev(path) != expected_rev:
            raise ConcurrentModification(
                "note changed concurrently; re-read and retry"
            )
        if path in self.notes and create and not overwrite:
            raise NoteExists(path)
        if path not in self.notes and not create:
            raise NoteNotFound(path)
        self.add(path, content, mtime=mtime if mtime is not None else 2)
        return deepcopy(self.notes[path]["meta"])

    async def write_attachment(self, path, data, *, create=False,
                               overwrite=False, expected_rev=None):
        self._call("write_attachment", path, data, create, overwrite, expected_rev)
        if expected_rev is not None and self._current_rev(path) != expected_rev:
            raise ConcurrentModification("note changed concurrently; re-read and retry")
        if path in self.notes and create and not overwrite:
            raise NoteExists(path)
        if path not in self.notes and not create:
            raise NoteNotFound(path)
        self.add(path, data=data)
        return deepcopy(self.notes[path]["meta"])

    async def delete_note(self, path, expected_rev=None):
        self._call("delete_note", path, expected_rev)
        if expected_rev is not None and self._current_rev(path) != expected_rev:
            raise ConcurrentModification(
                "note changed concurrently; re-read and retry"
            )
        return self.notes.pop(path, None) is not None

    async def copy_note(self, path, new_path, *, overwrite=False):
        self._call("copy_note", path, new_path, overwrite)
        if path not in self.notes:
            raise NoteNotFound(path)
        if new_path in self.notes and not overwrite:
            raise NoteExists(new_path)
        self.notes[new_path] = deepcopy(self.notes[path])
        self.notes[new_path]["rev"] = self._next_rev(new_path)
        self.notes[new_path]["meta"].update(
            path=new_path, doc_id=new_path.lower(),
            rev=self.notes[new_path]["rev"],
        )
        return deepcopy(self.notes[new_path]["meta"])

    async def rename_note(self, path, new_path, *, overwrite=False):
        self._call("rename_note", path, new_path, overwrite)
        if path == new_path:
            return await self.get_meta(path)
        result = await self.copy_note(path, new_path, overwrite=overwrite)
        del self.notes[path]
        return result
