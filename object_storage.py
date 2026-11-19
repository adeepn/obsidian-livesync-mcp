"""Object-storage access layer for the S3 journal peer (Plan B, Task 4).

This is the only module that talks S3. The journal backend (Tasks 5-7)
depends on the thin :class:`JournalObjectStorage` protocol; tests use
:class:`tests.fake_object_storage.FakeObjectStorage` instead of the
network. Upstream reference: ``MinioStorageAdapter.ts`` (ListObjectsV2
with ``StartAfter``, keys stored under a bucket prefix, GET →
``NoSuchKey`` semantics).

Key semantics (both implementations):

- **Keys are prefix-relative.** Callers use journal names such as
  ``1700000000000-docs.jsonl.gz``; the boto3 adapter prepends the
  configured bucket prefix (``S3Config.prefix``) on the wire and strips
  it from listing results. The fake performs no prefix logic.
- **Ordering is S3's lexicographic (UTF-8 binary) order**, strictly
  after ``start_after``. Numeric ordering of journal file names
  (``"10-..."`` sorts *before* ``"9-..."`` lexicographically) is the
  caller's duty: replay must process files by their integer millisecond
  prefix (Plan B Review Focus 3).
- **No delete exists anywhere** (spec section 7: journal objects are
  append-only and are never deleted or overwritten). The protocol
  deliberately cannot express deletion; a test pins this.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config import S3Config
from storage_base import StorageUnavailable

logger = logging.getLogger(__name__)

# S3 ListObjectsV2 never returns more than 1000 keys per page.
_MAX_PAGE_KEYS = 1000

# Error codes reported by S3-compatible stores for a missing object.
# HEAD returns 404 without a body, which botocore surfaces as code "404".
_MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


@runtime_checkable
class JournalObjectStorage(Protocol):
    """Synchronous object-storage access for journal files.

    ``get`` returns the object bytes, or ``None`` when the key is
    missing. ``put`` returns ``None`` on success and raises
    :class:`~storage_base.StorageUnavailable` when the store rejects the
    write or is unreachable; note that the *journal* success contract
    (spec section 7.6) additionally requires the caller's read-back
    verification, which lives in the backend, not here. ``list`` returns
    prefix-relative keys in lexicographic order strictly after
    ``start_after`` (at most ``limit`` when given); ``exists`` is a HEAD
    probe. There is intentionally no delete (spec section 7).
    """

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, data: bytes) -> None: ...

    def list(self, start_after: str = "", limit: int | None = None) -> list[str]: ...

    def exists(self, key: str) -> bool: ...


def _is_missing(exc: ClientError) -> bool:
    """Whether a ClientError means 'no such object' (GET/HEAD miss)."""
    response = exc.response
    code = str(response.get("Error", {}).get("Code", ""))
    if code in _MISSING_CODES:
        return True
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404


class Boto3ObjectStorage:
    """boto3 implementation of :class:`JournalObjectStorage`.

    The client uses path-style addressing when ``cfg.endpoint`` is set
    (MinIO and other S3-compatible stores; AWS proper with an empty
    endpoint keeps boto3's default virtual-hosted addressing and needs
    ``region`` in the URI). Connect/read timeouts are 5/30 seconds and
    retries are disabled (one attempt) — the journal write path performs
    its own read-back verification and prefers loud, immediate failure.

    Keys are prefix-relative: the adapter prepends ``cfg.prefix`` on the
    wire and strips it from listing results. Failures other than a
    missing key raise :class:`~storage_base.StorageUnavailable` whose
    message names only the bucket and key (never credentials or
    endpoints); details go to the log.
    """

    def __init__(self, cfg: S3Config):
        if not cfg.bucket:
            raise ValueError("S3 bucket must be a non-empty string")
        self._bucket = cfg.bucket
        self._prefix = cfg.prefix or ""

        client_config = Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 1},
        )
        client_kwargs: dict = {
            "aws_access_key_id": cfg.access_key,
            "aws_secret_access_key": cfg.secret_key,
            # An empty region falls back to boto3's own resolution
            # (environment/profile); fine for MinIO, required for AWS.
            "region_name": cfg.region or None,
            "config": client_config,
        }
        if cfg.endpoint:
            client_kwargs["endpoint_url"] = cfg.endpoint
            client_config.s3 = {"addressing_style": "path"}

        self._client = boto3.client("s3", **client_kwargs)

    # -- Key translation -----------------------------------------------------

    def _wire_key(self, key: str) -> str:
        """Protocol key → on-the-wire key (bucket prefix prepended)."""
        return self._prefix + key

    def _relative_key(self, wire_key: str) -> str:
        """On-the-wire key → protocol key (prefix stripped when present)."""
        if wire_key.startswith(self._prefix):
            return wire_key[len(self._prefix) :]
        return wire_key

    # -- JournalObjectStorage protocol --------------------------------------

    def get(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=self._wire_key(key)
            )
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise self._unavailable("GET", key, exc) from exc
        except BotoCoreError as exc:
            raise self._unavailable("GET", key, exc) from exc
        return response["Body"].read()

    def put(self, key: str, data: bytes) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._wire_key(key), Body=data
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._unavailable("PUT", key, exc) from exc

    def list(self, start_after: str = "", limit: int | None = None) -> list[str]:
        if limit == 0:
            return []
        keys: list[str] = []
        kwargs: dict = {"Bucket": self._bucket, "Prefix": self._prefix}
        if start_after:
            kwargs["StartAfter"] = self._wire_key(start_after)
        while True:
            if limit is not None:
                kwargs["MaxKeys"] = min(limit - len(keys), _MAX_PAGE_KEYS)
            try:
                response = self._client.list_objects_v2(**kwargs)
            except (ClientError, BotoCoreError) as exc:
                raise self._unavailable("LIST", start_after, exc) from exc
            for obj in response.get("Contents") or []:
                wire_key = obj.get("Key", "")
                if not wire_key.startswith(self._prefix):
                    continue  # defensive, mirrors the upstream adapter
                keys.append(self._relative_key(wire_key))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
            # StartAfter only applies to the first page; continuation
            # pages are driven by the token alone.
            kwargs.pop("StartAfter", None)
            kwargs["ContinuationToken"] = token
            if limit is not None and len(keys) >= limit:
                break
        if limit is not None:
            keys = keys[:limit]
        return keys

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(
                Bucket=self._bucket, Key=self._wire_key(key)
            )
            return True
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise self._unavailable("HEAD", key, exc) from exc
        except BotoCoreError as exc:
            raise self._unavailable("HEAD", key, exc) from exc

    # -- Error shaping -------------------------------------------------------

    def _unavailable(self, operation: str, key: str, exc: Exception) -> StorageUnavailable:
        """StorageUnavailable naming only bucket/key; details go to the log."""
        logger.warning(
            "object storage %s failed for bucket %r key %r: %s: %s",
            operation,
            self._bucket,
            key,
            type(exc).__name__,
            exc,
        )
        return StorageUnavailable(
            f"object storage {operation} failed for bucket "
            f"{self._bucket!r} key {key!r}"
        )
