"""Tests for the journal object-storage layer (Plan B, Task 4).

Covers the ``JournalObjectStorage`` protocol shape (no delete exists
anywhere — spec section 7 invariant), the in-memory fake used by Tasks
5-7 (put/get/exists/list with StartAfter, S3 lexicographic order
evidence on ``9-...`` vs ``10-...`` journal names, scriptable failures,
latency), boto3 adapter construction (endpoint/path-style, timeouts,
retries, prefix handling), and an env-gated MinIO smoke roundtrip
shared with the Task 9 integration suite.
"""

import os
import time
import traceback
import uuid

import pytest

from config import S3Config
from object_storage import Boto3ObjectStorage, JournalObjectStorage
from storage_base import StorageUnavailable
from tests.fake_object_storage import FakeObjectStorage

# Env-gated MinIO smoke: MINIO_TEST_ENDPOINT / MINIO_TEST_KEY /
# MINIO_TEST_SECRET / MINIO_TEST_BUCKET. Legacy credential names are aliases.
MINIO_ENV = {
    name: os.environ.get(name, "")
    for name in (
        "MINIO_TEST_ENDPOINT",
        "MINIO_TEST_KEY",
        "MINIO_TEST_SECRET",
        "MINIO_TEST_BUCKET",
    )
}
MINIO_ENV["MINIO_TEST_KEY"] = MINIO_ENV["MINIO_TEST_KEY"] or os.environ.get(
    "MINIO_TEST_ACCESS_KEY", ""
)
MINIO_ENV["MINIO_TEST_SECRET"] = MINIO_ENV["MINIO_TEST_SECRET"] or os.environ.get(
    "MINIO_TEST_SECRET_KEY", ""
)
MINIO_CONFIGURED = all(MINIO_ENV.values())

JOURNAL_2 = "2-docs.jsonl.gz"
JOURNAL_9 = "9-docs.jsonl.gz"
JOURNAL_10 = "10-docs.jsonl.gz"


def make_cfg(**overrides) -> S3Config:
    fields = {
        "endpoint": "http://minio.local:9000",
        "access_key": "test-access-key",
        "secret_key": "test-secret-key",
        "bucket": "test-bucket",
        "region": "us-east-1",
        "prefix": "testprefix/",
        "uri": "sls://test-access-key:test-secret-key@minio.local:9000"
        "?bucket=test-bucket&region=us-east-1&prefix=testprefix%2F",
    }
    fields.update(overrides)
    return S3Config(**fields)


class TestFakeSemantics:
    def test_put_get_roundtrip(self):
        storage = FakeObjectStorage()
        storage.put(JOURNAL_9, b"payload")
        assert storage.get(JOURNAL_9) == b"payload"

    def test_get_missing_returns_none(self):
        assert FakeObjectStorage().get(JOURNAL_9) is None

    def test_exists(self):
        storage = FakeObjectStorage()
        assert storage.exists(JOURNAL_9) is False
        storage.put(JOURNAL_9, b"x")
        assert storage.exists(JOURNAL_9) is True

    def test_initial_mapping_seeds_store(self):
        storage = FakeObjectStorage({JOURNAL_2: b"a"})
        assert storage.get(JOURNAL_2) == b"a"
        assert storage.list() == [JOURNAL_2]

    def test_list_sorted_lexicographically(self):
        storage = FakeObjectStorage()
        for key in ("c", "a", "b"):
            storage.put(key, b"x")
        assert storage.list() == ["a", "b", "c"]

    def test_list_empty_store_returns_empty_list(self):
        assert FakeObjectStorage().list() == []

    def test_list_start_after_is_exclusive(self):
        storage = FakeObjectStorage({"1": b"", "2": b"", "3": b""})
        assert storage.list(start_after="2") == ["3"]
        assert storage.list(start_after="2zzz") == ["3"]
        assert storage.list(start_after="") == ["1", "2", "3"]

    def test_list_is_lexicographic_not_numeric_order(self):
        """S3 orders keys lexicographically: ``"10-..."`` sorts BEFORE
        ``"9-..."`` (and ``"2-..."`` between them), because ``"1" < "2" <
        "9"`` byte-wise. The fake must reproduce that faithfully instead
        of "helpfully" sorting numerically — numeric ordering of journal
        file names is the CALLER's duty (Plan B Review Focus 3: replay
        processes files by the integer millisecond prefix).
        """
        storage = FakeObjectStorage(
            {JOURNAL_9: b"", JOURNAL_10: b"", JOURNAL_2: b""}
        )
        assert storage.list() == [JOURNAL_10, JOURNAL_2, JOURNAL_9]

        # The caller-side duty this pins: sorting by the integer prefix
        # yields a different, numeric order the backend must apply itself.
        numeric = sorted(
            (JOURNAL_9, JOURNAL_10, JOURNAL_2), key=lambda k: int(k.split("-")[0])
        )
        assert numeric == [JOURNAL_2, JOURNAL_9, JOURNAL_10]

    def test_list_limit(self):
        storage = FakeObjectStorage({k: b"" for k in ("a", "b", "c", "d", "e")})
        assert storage.list(limit=2) == ["a", "b"]
        assert storage.list(limit=99) == ["a", "b", "c", "d", "e"]
        assert storage.list(limit=None) == ["a", "b", "c", "d", "e"]

    def test_fail_next_get_n_then_recovers(self):
        storage = FakeObjectStorage({JOURNAL_2: b"data"})
        storage.fail_next_get(2)
        with pytest.raises(StorageUnavailable):
            storage.get(JOURNAL_2)
        with pytest.raises(StorageUnavailable):
            storage.get(JOURNAL_2)
        assert storage.get(JOURNAL_2) == b"data"

    def test_fail_next_get_custom_error(self):
        storage = FakeObjectStorage()
        storage.fail_next_get(1, error=OSError("boom"))
        with pytest.raises(OSError, match="boom"):
            storage.get(JOURNAL_2)

    def test_fail_next_get_only_affects_get(self):
        storage = FakeObjectStorage({JOURNAL_2: b"data"})
        storage.fail_next_get(3)
        storage.put(JOURNAL_9, b"ok")
        assert storage.exists(JOURNAL_2) is True
        assert storage.list() == [JOURNAL_2, JOURNAL_9]

    def test_fail_next_put_once_then_recovers(self):
        storage = FakeObjectStorage()
        storage.fail_next_put()
        with pytest.raises(StorageUnavailable):
            storage.put(JOURNAL_2, b"one")
        storage.put(JOURNAL_2, b"two")
        assert storage.get(JOURNAL_2) == b"two"

    def test_scripted_failures_still_record_calls(self):
        storage = FakeObjectStorage()
        storage.fail_next_get(1)
        storage.fail_next_put()
        with pytest.raises(StorageUnavailable):
            storage.get("k")
        with pytest.raises(StorageUnavailable):
            storage.put("k", b"v")
        assert storage.get_calls == ["k"]
        assert storage.put_calls == [("k", b"v")]

    def test_call_recording(self):
        storage = FakeObjectStorage({JOURNAL_2: b""})
        storage.get(JOURNAL_2)
        storage.get(JOURNAL_9)
        storage.put(JOURNAL_9, b"x")
        storage.list(start_after=JOURNAL_2)
        storage.exists(JOURNAL_2)
        assert storage.get_calls == [JOURNAL_2, JOURNAL_9]
        assert storage.put_calls == [(JOURNAL_9, b"x")]
        assert storage.list_calls == [JOURNAL_2]
        assert storage.exists_calls == [JOURNAL_2]

    def test_latency_applies_per_operation(self):
        storage = FakeObjectStorage(latency=0.03)
        start = time.monotonic()
        storage.get("missing")  # latency applies to misses too
        storage.exists("missing")
        elapsed = time.monotonic() - start
        assert elapsed >= 2 * 0.03 * 0.9  # two ops, small clock tolerance

    def test_key_validation_rejects_empty_and_non_string(self):
        storage = FakeObjectStorage()
        for bad in ("", 123, None):
            with pytest.raises(ValueError):
                storage.put(bad, b"x")
            with pytest.raises(ValueError):
                storage.get(bad)
            with pytest.raises(ValueError):
                storage.exists(bad)


class TestProtocolContract:
    @pytest.mark.parametrize("limit", [-1, 0])
    def test_fake_list_rejects_nonpositive_limit(self, limit):
        storage = FakeObjectStorage({"a": b"", "b": b""})
        with pytest.raises(ValueError, match="limit"):
            storage.list(limit=limit)

    def test_implementations_satisfy_protocol(self):
        assert isinstance(FakeObjectStorage(), JournalObjectStorage)
        # Boto3ObjectStorage construction is offline (no I/O until a call).
        assert isinstance(Boto3ObjectStorage(make_cfg()), JournalObjectStorage)

    def test_no_delete_anywhere(self):
        """Spec section 7: journal objects are NEVER deleted; the storage
        protocol cannot even express deletion. Pinned on the protocol and
        both implementations, instance-level too, sweeping every public
        attribute for a delete-ish name.
        """
        instances = (
            FakeObjectStorage(),
            Boto3ObjectStorage(make_cfg()),
        )
        for cls in (JournalObjectStorage, FakeObjectStorage, Boto3ObjectStorage):
            assert not hasattr(cls, "delete")
        for instance in instances:
            assert not hasattr(instance, "delete")
            leaked = [
                name for name in dir(instance) if "delete" in name.lower()
            ]
            assert leaked == []


class TestBoto3AdapterConstruction:
    """Offline white-box checks: the client is configured per the task
    brief (endpoint + path-style when an endpoint is set, connect/read
    timeouts 5/30, retries off, prefix handled by the adapter).
    """

    def test_endpoint_and_path_style_addressing(self):
        adapter = Boto3ObjectStorage(make_cfg())
        assert adapter._client.meta.endpoint_url == "http://minio.local:9000"
        assert adapter._client.meta.config.s3.get("addressing_style") == "path"

    def test_aws_without_endpoint_intentionally_keeps_default_addressing(self):
        adapter = Boto3ObjectStorage(make_cfg(endpoint=""))
        # Intended contract: custom endpoints use path-style; AWS without
        # an endpoint retains boto3's default addressing configuration.
        assert not adapter._client.meta.config.s3

    def test_timeouts_and_retries_off(self):
        adapter = Boto3ObjectStorage(make_cfg())
        config = adapter._client.meta.config
        assert config.connect_timeout == 5
        assert config.read_timeout == 30
        # Retries disabled: exactly one total attempt (the journal write
        # path performs its own read-back verification); botocore
        # normalizes the mode alongside the attempt count.
        assert config.retries.get("total_max_attempts") == 1

    def test_wire_key_applies_prefix(self):
        assert Boto3ObjectStorage(make_cfg())._wire_key("k") == "testprefix/k"
        assert Boto3ObjectStorage(make_cfg(prefix=""))._wire_key("k") == "k"

    def test_relative_key_strips_prefix(self):
        adapter = Boto3ObjectStorage(make_cfg())
        assert adapter._relative_key("testprefix/k") == "k"

    def test_region_passed_to_client(self):
        adapter = Boto3ObjectStorage(make_cfg())
        assert adapter._client.meta.region_name == "us-east-1"


class TestBoto3WireOperations:
    @pytest.fixture()
    def wire(self):
        from botocore.stub import Stubber

        adapter = Boto3ObjectStorage(make_cfg())
        with Stubber(adapter._client) as stub:
            yield adapter, stub
            stub.assert_no_pending_responses()

    def test_put_get_exists_use_wire_prefix(self, wire):
        import io

        adapter, stub = wire
        params = {"Bucket": "test-bucket", "Key": "testprefix/k"}
        stub.add_response("put_object", {}, {**params, "Body": b"payload"})
        body = io.BytesIO(b"payload")
        stub.add_response("get_object", {"Body": body}, params)
        stub.add_response("head_object", {}, params)
        assert adapter.put("k", b"payload") is None
        assert adapter.get("k") == b"payload"
        assert body.closed
        assert adapter.exists("k") is True

    def test_missing_get_and_head(self, wire):
        adapter, stub = wire
        params = {"Bucket": "test-bucket", "Key": "testprefix/missing"}
        stub.add_client_error("get_object", service_error_code="NoSuchKey",
                              http_status_code=404, expected_params=params)
        stub.add_client_error("head_object", service_error_code="404",
                              http_status_code=404, expected_params=params)
        assert adapter.get("missing") is None
        assert adapter.exists("missing") is False

    def test_list_paginates_with_relative_start_after_and_results(self, wire):
        adapter, stub = wire
        params = {"Bucket": "test-bucket", "Prefix": "testprefix/"}
        stub.add_response("list_objects_v2", {
            "Contents": [{"Key": "testprefix/b"}],
            "IsTruncated": True, "NextContinuationToken": "page2",
        }, {**params, "StartAfter": "testprefix/a", "MaxKeys": 2})
        stub.add_response("list_objects_v2", {
            "Contents": [{"Key": "testprefix/c"}], "IsTruncated": False,
        }, {**params, "ContinuationToken": "page2", "MaxKeys": 1})
        assert adapter.list(start_after="a", limit=2) == ["b", "c"]

    @pytest.mark.parametrize("limit", [-1, 0])
    def test_list_nonpositive_limit_performs_no_request(self, wire, limit):
        adapter, _ = wire
        with pytest.raises(ValueError, match="limit"):
            adapter.list(limit=limit)

    def test_body_read_timeout_is_typed_and_closes_stream(self, wire):
        import io

        from botocore.exceptions import ReadTimeoutError

        class FailingBody(io.BytesIO):
            def read(self, *args, **kwargs):
                raise ReadTimeoutError(endpoint_url="http://example.invalid")

        adapter, stub = wire
        body = FailingBody(b"payload")
        stub.add_response("get_object", {"Body": body}, {
            "Bucket": "test-bucket", "Key": "testprefix/k",
        })
        try:
            with pytest.raises(StorageUnavailable):
                adapter.get("k")
        finally:
            assert body.closed

    @pytest.mark.parametrize("operation, method", [
        ("GET", "get_object"), ("PUT", "put_object"),
        ("HEAD", "head_object"), ("LIST", "list_objects_v2"),
    ])
    def test_errors_do_not_expose_credentials_in_logs_or_tracebacks(
        self, wire, caplog, operation, method,
    ):
        adapter, stub = wire
        stub.add_client_error(
            method, service_error_code="AccessDenied", http_status_code=403,
            service_message="request failed at http://ak:sk@host",
        )
        with pytest.raises(StorageUnavailable) as caught:
            if operation == "GET":
                adapter.get("k")
            elif operation == "PUT":
                adapter.put("k", b"payload")
            elif operation == "HEAD":
                adapter.exists("k")
            else:
                adapter.list(start_after="k")
        # botocore creates an AccessDenied exception subclass. Our adapter
        # logs its class name, never the service message or exception text.
        adapter_logs = [r for r in caplog.records if r.name == "object_storage"]
        assert [r.getMessage() for r in adapter_logs] == [
            f"object storage {operation} failed for key 'k': AccessDenied"
        ]
        assert adapter_logs[0].exc_info is None
        assert "ak:sk" not in caplog.text
        assert "ak:sk" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.skipif(
    not MINIO_CONFIGURED,
    reason="MinIO smoke is env-gated: set MINIO_TEST_ENDPOINT, "
    "MINIO_TEST_KEY, MINIO_TEST_SECRET and MINIO_TEST_BUCKET "
    "(legacy MINIO_TEST_ACCESS_KEY / MINIO_TEST_SECRET_KEY also accepted)",
)
class TestMinioSmokeRoundtrip:
    """Live roundtrip against a dev MinIO (same env as Task 9's suite).

    Uses a unique prefix per run: spec section 7 forbids deleting journal
    objects, so leftovers from previous runs must not disturb assertions.
    """

    @pytest.fixture()
    def storage(self):
        prefix = f"task4-smoke-{uuid.uuid4().hex[:10]}/"
        return Boto3ObjectStorage(
            make_cfg(
                endpoint=MINIO_ENV["MINIO_TEST_ENDPOINT"],
                access_key=MINIO_ENV["MINIO_TEST_KEY"],
                secret_key=MINIO_ENV["MINIO_TEST_SECRET"],
                bucket=MINIO_ENV["MINIO_TEST_BUCKET"],
                prefix=prefix,
            )
        )

    def test_put_list_get_roundtrip_with_prefix_stripping(self, storage):
        storage.put(JOURNAL_9, b"nine")
        storage.put(JOURNAL_10, b"ten")
        storage.put("_00000000-milestone.json", b"{}")
        # Prefix stripped from results; lexicographic S3 order (underscore
        # sorts after digits), service objects included — filtering is the
        # caller's duty.
        assert storage.list() == [
            JOURNAL_10,
            JOURNAL_9,
            "_00000000-milestone.json",
        ]
        assert storage.get(JOURNAL_9) == b"nine"
        assert storage.get(JOURNAL_10) == b"ten"
        assert storage.exists(JOURNAL_9) is True
        # StartAfter resumes strictly after the given (prefix-relative) key.
        assert storage.list(start_after=JOURNAL_10) == [
            JOURNAL_9,
            "_00000000-milestone.json",
        ]

    def test_get_missing_returns_none_and_exists_false(self, storage):
        assert storage.get(f"absent-{uuid.uuid4().hex}") is None
        assert storage.exists(f"absent-{uuid.uuid4().hex}") is False
