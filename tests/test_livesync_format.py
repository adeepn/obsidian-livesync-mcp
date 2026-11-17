"""Tests for LiveSync path/namespace rules (livesync_format)."""

import pytest

from livesync_format import (
    ForbiddenPathError,
    TEXT_EXTENSIONS,
    is_text_doc,
    path2id,
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
