"""In-memory double for the ``JournalObjectStorage`` protocol.

Implements the exact protocol semantics of
:class:`object_storage.JournalObjectStorage` for tests:

- keys are already prefix-relative (the fake performs NO prefix logic —
  the boto3 adapter owns the configured bucket prefix);
- ``list`` returns keys in S3's lexicographic (UTF-8 binary) order
  strictly after ``start_after`` — it deliberately does NOT "helpfully"
  sort numerically: ``"10-..."`` sorts before ``"9-..."``, and numeric
  journal ordering is the caller's duty (Plan B Review Focus 3);
- ``get`` of a missing key returns ``None``.

Extras beyond the protocol (test tooling): strict key validation,
scriptable failures (``fail_next_get``/``fail_next_put``), an optional
per-operation latency for concurrency tests, and per-op call recording.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping

from storage_base import StorageUnavailable


def _check_key(key: object) -> str:
    """Reject anything but a non-empty string (caller bug in a test)."""
    if not isinstance(key, str) or not key:
        raise ValueError(f"object key must be a non-empty string (got {key!r})")
    return key


class FakeObjectStorage:
    """Dictionary-backed :class:`JournalObjectStorage` double.

    ``initial`` seeds the store (``{key: bytes}``). ``latency`` seconds are
    slept before every operation — set it in concurrency tests to widen
    race windows deterministically.
    """

    def __init__(
        self,
        initial: Mapping[str, bytes] | None = None,
        *,
        latency: float = 0.0,
    ):
        self._objects: dict[str, bytes] = dict(initial or {})
        self._lock = threading.Lock()
        self._latency = latency
        self._fail_get_remaining = 0
        self._fail_get_error: Exception | None = None
        self._fail_put = False
        self._fail_put_error: Exception | None = None
        # Call recording for assertions (e.g. throttle tests).
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, bytes]] = []
        self.list_calls: list[str] = []
        self.exists_calls: list[str] = []

    # -- Scriptable failures ------------------------------------------------

    def fail_next_get(self, n: int = 1, error: Exception | None = None) -> None:
        """Make the next ``n`` ``get`` calls raise ``error``.

        The default error is ``StorageUnavailable`` — what a real backend
        surfaces for a failed read. Puts, lists and exists stay healthy.
        """
        self._fail_get_remaining = n
        self._fail_get_error = error

    def fail_next_put(self, error: Exception | None = None) -> None:
        """Make the next single ``put`` call raise ``error``."""
        self._fail_put = True
        self._fail_put_error = error

    def _scripted_error(self, error: Exception | None, what: str) -> Exception:
        return error if error is not None else StorageUnavailable(
            f"scripted {what} failure (fake object storage)"
        )

    def _sleep(self) -> None:
        if self._latency > 0:
            time.sleep(self._latency)

    # -- JournalObjectStorage protocol --------------------------------------

    def get(self, key: str) -> bytes | None:
        key = _check_key(key)
        self.get_calls.append(key)
        self._sleep()
        if self._fail_get_remaining > 0:
            self._fail_get_remaining -= 1
            raise self._scripted_error(self._fail_get_error, "get")
        with self._lock:
            return self._objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        key = _check_key(key)
        self.put_calls.append((key, data))
        self._sleep()
        if self._fail_put:
            self._fail_put = False
            raise self._scripted_error(self._fail_put_error, "put")
        with self._lock:
            self._objects[key] = data

    def list(self, start_after: str = "", limit: int | None = None) -> list[str]:
        self.list_calls.append(start_after)
        self._sleep()
        with self._lock:
            # Plain str comparison is S3's UTF-8 binary order (UTF-8
            # preserves code-point ordering), strictly after start_after.
            keys = sorted(k for k in self._objects if k > start_after)
        if limit is not None:
            keys = keys[:limit]
        return keys

    def exists(self, key: str) -> bool:
        key = _check_key(key)
        self.exists_calls.append(key)
        self._sleep()
        with self._lock:
            return key in self._objects
