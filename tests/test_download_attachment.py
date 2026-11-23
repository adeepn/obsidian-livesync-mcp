"""Tool contracts for complete, size-bounded binary downloads."""
import base64
import hashlib
import json

import pytest

import server
from storage_base import StorageError
from storage_journal import JournalBackend
from tests.fake_backend import FakeBackend
from tests.test_storage_journal import healthy_bucket, make_backend

CAP = 32 * 1024 * 1024
DATA = b"%PDF-1.7\n\x00\xff\x80scan\r\n"


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, "backend", backend)
    return backend


@pytest.mark.parametrize("data", [DATA, b""])
async def test_download_response_shape_and_raw_metadata(fake, data):
    fake.add("scans/scan.pdf", data=data, size=999)
    result = json.loads(await server.obsidian_download_attachment("scans/scan.pdf"))
    assert result == {
        "path": "scans/scan.pdf", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }


@pytest.mark.parametrize("path", ["note.md", "board.canvas", "db.base", "plain.bin"])
async def test_download_refuses_text(fake, path):
    fake.add(path, text="private textual content")
    result = await server.obsidian_download_attachment(path)
    assert "textual note" in result and "use obsidian_fetch" in result
    assert "private textual content" not in result


async def test_download_missing(fake):
    result = await server.obsidian_download_attachment("missing.pdf")
    assert "not found" in result and "missing.pdf" in result


@pytest.mark.parametrize("limit", [0, -1, len(DATA), len(DATA) + 1])
async def test_download_within_caller_limit(fake, limit):
    fake.add("scan.pdf", data=DATA)
    result = json.loads(await server.obsidian_download_attachment("scan.pdf", limit))
    assert base64.b64decode(result["content_b64"], validate=True) == DATA


async def test_download_caller_limit_rejects_whole_file(fake):
    fake.add("scan.pdf", data=DATA)
    result = await server.obsidian_download_attachment("scan.pdf", len(DATA) - 1)
    assert "error" in result.lower()
    assert str(len(DATA)) in result and f"max_bytes ({len(DATA) - 1}" in result
    assert "content_b64" not in result
    assert base64.b64encode(DATA).decode() not in result


@pytest.mark.parametrize("limit", [0, CAP * 2])
async def test_download_hard_cap_cannot_be_disabled(fake, limit):
    fake.add("scan.pdf", data=b"x" * (CAP + 1))
    result = await server.obsidian_download_attachment("scan.pdf", limit)
    assert "error" in result.lower() and "32 MiB" in result
    assert str(CAP) in result and str(CAP + 1) in result
    assert "content_b64" not in result


async def test_download_exact_hard_cap(fake):
    data = b"x" * CAP
    fake.add("scan.pdf", data=data)
    result = json.loads(await server.obsidian_download_attachment("scan.pdf"))
    assert base64.b64decode(result["content_b64"], validate=True) == data


async def test_download_storage_error(fake):
    fake.errors["read_note"] = StorageError()
    result = await server.obsidian_download_attachment("scan.pdf")
    assert "error" in result.lower() and "content_b64" not in result


async def test_tool_upload_download_two_journal_peers(tmp_path, monkeypatch):
    writer, storage = make_backend(tmp_path / "writer", healthy_bucket())
    reader = JournalBackend(storage, tmp_path / "reader")
    try:
        await writer.startup_check()
        monkeypatch.setattr(server, "backend", writer)
        uploaded = json.loads(await server.obsidian_upload_attachment(
            "scans/scan.pdf", base64.b64encode(DATA).decode("ascii"),
        ))
        await reader.startup_check()
        monkeypatch.setattr(server, "backend", reader)
        downloaded = json.loads(await server.obsidian_download_attachment("scans/scan.pdf"))
        assert base64.b64decode(downloaded.pop("content_b64"), validate=True) == DATA
        assert downloaded == uploaded
    finally:
        await writer.close()
        await reader.close()
