"""Tests for LiveSync path/namespace rules and document/chunk codecs."""

import base64
import hashlib

import pytest

from livesync_format import (
    WRITABLE_BINARY_EXTENSIONS,
    ForbiddenPathError,
    TEXT_EXTENSIONS,
    assemble_binary,
    assemble_text,
    chunk_id_for,
    doc_type_for,
    encode_newnote,
    is_text_doc,
    is_writable_text_path,
    new_metadata_doc,
    path2id,
    soft_delete_revision,
    validate_vault_path,
)


def test_path2id_basic():
    assert path2id("Inbox/Note.md") == "inbox/note.md"


def test_path2id_leading_underscore():
    assert path2id("_inbox/x.md") == "/_inbox/x.md"


def test_path2id_normalizes():
    expected = path2id("Inbox/x.md")

    assert path2id("/Inbox//x.md") == expected
    assert path2id("./Inbox/x.md") == expected
    assert path2id("Inbox\\x.md") == expected


def test_path2id_case_variants_share_id():
    # Upstream vector: livesync-commonlib path.unit.spec.ts,
    # "maps case variants to one document ID in case-insensitive mode"
    # (handleFilenameCaseSensitive=false is the upstream default).
    assert path2id("Calculus.md") == path2id("calculus.md") == "calculus.md"


def test_path2id_strips_leading_slash_before_underscore_escape():
    # id2path upstream returns "_private/x.md" for "/_private/x.md";
    # the round-trip must be stable when clients hand back the raw path.
    assert path2id("/_private/x.md") == "/_private/x.md"


def test_validate_forbidden():
    for path in [
        "i:.obsidian/x",
        "h:abc",
        ".obsidian/config",
        "_design/x",
        "f:abc",
        "obsydian_livesync_version",
        "syncinfo",
    ]:
        with pytest.raises(ForbiddenPathError):
            validate_vault_path(path)


def test_validate_forbidden_service_namespaces():
    # Audit B7/B10: case-insensitive, after stripping a leading "/":
    # metadata prefixes, CouchDB-reserved first segments, dot segments.
    for path in [
        "ix:abc",
        "ps:abc",
        "_local/x",
        "_DESIGN/x",
        "/i:foo",
        "/.obsidian/config",
        ".hidden",
        "folder/.obsidian/config",
    ]:
        with pytest.raises(ForbiddenPathError):
            validate_vault_path(path)


def test_validate_ok():
    assert validate_vault_path("Inbox/Note.md") is None
    validate_vault_path("_inbox/x.md")


def test_validate_ok_similar_to_reserved_names():
    # Only the exact CouchDB-reserved first segments are rejected;
    # ordinary underscore names stay valid (escaped to "/_" by path2id).
    validate_vault_path("_design.md")
    validate_vault_path("Inbox/obsydian_livesync_version.md")


def test_validate_empty_path():
    with pytest.raises(ForbiddenPathError):
        validate_vault_path("/")
    with pytest.raises(ForbiddenPathError):
        validate_vault_path("")


def test_is_text_doc():
    assert is_text_doc("plain", "x.bin") is True
    assert is_text_doc("newnote", "a.md") is True
    assert is_text_doc("newnote", "a.png") is False
    assert is_text_doc(None, "a.md") is True


@pytest.mark.parametrize("extension", sorted(TEXT_EXTENSIONS))
def test_is_text_doc_text_extensions(extension):
    assert is_text_doc("newnote", "note" + extension) is True


@pytest.mark.parametrize("extension", [".png", ".jpg", ".pdf", ".bin"])
def test_is_text_doc_binary_extensions(extension):
    assert is_text_doc("newnote", "note" + extension) is False


def test_is_writable_text_path_write_side_policy():
    # The write allowlist is the inverse of the read classification:
    # everything textual the tools advertise is writable, including
    # extensions upstream never classifies as text (.base, .json,
    # .yaml) and text-ish formats (.svg, .canvas).
    for path in [
        "Tasks.base", "DB.base", "note.md", "Board.canvas", "logo.svg",
        "data.json", "conf.yaml", "no-extension", "notes.txt",
    ]:
        assert is_writable_text_path(path) is True, path


@pytest.mark.parametrize(
    "extension",
    sorted(WRITABLE_BINARY_EXTENSIONS),
)
def test_is_writable_text_path_binary_denylist(extension):
    assert is_writable_text_path("file" + extension) is False


def test_is_writable_text_path_denylist_is_case_insensitive():
    assert is_writable_text_path("Photo.PNG") is False
    assert is_writable_text_path("Archive.ZIP") is False


def test_is_writable_text_path_svg_is_not_a_binary_extension():
    # Regression guard: svg is XML text and must stay writable even if
    # someone "completes" the image denylist carelessly.
    assert ".svg" not in WRITABLE_BINARY_EXTENSIONS


# --- Document and chunk codecs (task 3) ---


def test_assemble_text_concat():
    assert assemble_text(["Hel", "lo ", "World"]) == "Hello World"


def test_assemble_binary_concat():
    # Audit B4: chunks are base64-encoded separately; the body is the
    # concatenation of the per-chunk decoded bytes.
    assert assemble_binary(["SGVsbG8=", "IFdvcmxk"]) == b"Hello World"


def test_assemble_binary_invalid():
    with pytest.raises(ValueError):
        assemble_binary(["SGVsbG8=", "!!!not-base64!!!"])


def test_encode_newnote_roundtrip():
    data = "héllo wörld \N{SNOWMAN}".encode("utf-8") + bytes(range(256))
    assert assemble_binary(encode_newnote(data)) == data


def test_encode_newnote_single_chunk():
    # v1 encoding: the whole body is one base64 chunk.
    assert encode_newnote(b"12345") == [base64.b64encode(b"12345").decode("ascii")]


def test_chunk_id_deterministic():
    chunk_id = chunk_id_for("same content")
    assert chunk_id == chunk_id_for("same content")
    assert chunk_id.startswith("h:")
    assert chunk_id != chunk_id_for("different content")


def test_chunk_id_sha256_prefix():
    expected = "h:" + hashlib.sha256("body".encode("utf-8")).hexdigest()[:16]
    assert chunk_id_for("body") == expected


def test_metadata_doc_shape():
    doc = new_metadata_doc(
        path="Inbox/Note.md",
        children=["h:abc123", "h:def456"],
        size=11,
        doc_type="plain",
        ctime_ms=1730000000000,
        mtime_ms=1730000005000,
    )
    assert set(doc) == {
        "_id",
        "path",
        "children",
        "ctime",
        "mtime",
        "size",
        "type",
        "eden",
    }
    assert doc["_id"] == path2id("Inbox/Note.md")
    assert doc["path"] == "Inbox/Note.md"
    assert doc["children"] == ["h:abc123", "h:def456"]
    assert doc["size"] == 11
    assert doc["type"] == "plain"
    assert doc["eden"] == {}
    assert isinstance(doc["ctime"], int)
    assert doc["ctime"] == 1730000000000
    assert isinstance(doc["mtime"], int)
    assert doc["mtime"] == 1730000005000


def test_soft_delete_keeps_body():
    original = new_metadata_doc(
        path="Inbox/Note.md",
        children=["h:abc123"],
        size=5,
        doc_type="plain",
        ctime_ms=1730000000000,
        mtime_ms=1730000005000,
    )
    revision = soft_delete_revision(original, mtime_ms=1730000009000)

    assert revision is not original
    assert revision["deleted"] is True
    assert revision["children"] == ["h:abc123"]
    assert revision["type"] == "plain"
    assert revision["mtime"] == 1730000009000
    # The original document is not modified (audit B2).
    assert "deleted" not in original
    assert original["mtime"] == 1730000005000
    assert original["children"] == ["h:abc123"]
    assert original["type"] == "plain"


def test_doc_type_for():
    # Audit B3: any text is written as type "plain" regardless of extension.
    assert doc_type_for("x.md") == "plain"
    assert doc_type_for("x.base") == "plain"
