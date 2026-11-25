"""Tests for LiveSync path/namespace rules and document/chunk codecs."""

import base64
import unicodedata

import pytest

from livesync_format import (
    PATH_IDENTITY_LEGACY,
    PATH_IDENTITY_NFC,
    ForbiddenPathError,
    TEXT_EXTENSIONS,
    UnsupportedVaultSettings,
    assemble_binary,
    assemble_text,
    chunk_id_for,
    is_text_doc,
    new_metadata_doc,
    path2id,
    resolve_path_identity,
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
    assert is_text_doc("newnote", "a.md") is False
    assert is_text_doc("newnote", "a.png") is False
    assert is_text_doc(None, "a.md") is False


@pytest.mark.parametrize("extension", sorted(TEXT_EXTENSIONS))
def test_is_text_doc_text_extensions(extension):
    assert is_text_doc("newnote", "note" + extension) is False


@pytest.mark.parametrize("extension", [".png", ".jpg", ".pdf", ".bin"])
def test_is_text_doc_binary_extensions(extension):
    assert is_text_doc("newnote", "note" + extension) is False


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


def test_chunk_id_deterministic():
    chunk_id = chunk_id_for("same content")
    assert chunk_id == chunk_id_for("same content")
    assert chunk_id.startswith("h:")
    assert chunk_id != chunk_id_for("different content")


def test_chunk_id_upstream_xxhash64_vector():
    assert chunk_id_for("helloWorld") == "h:37me21jj44pcc"


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
    original.update(_rev="4-old", arbitrary={"nested": [1, 2]})
    revision = soft_delete_revision(original, mtime_ms=1730000009000)

    assert revision is not original
    for field in ("size", "ctime", "_rev", "arbitrary"):
        assert revision[field] == original[field]
    assert revision["deleted"] is True
    assert revision["children"] == ["h:abc123"]
    assert revision["type"] == "plain"
    assert revision["mtime"] == 1730000009000
    # The original document is not modified (audit B2).
    assert "deleted" not in original
    assert original["mtime"] == 1730000005000
    assert original["children"] == ["h:abc123"]
    assert original["type"] == "plain"


@pytest.mark.parametrize('path', [
    'note.md/', 'note.md/.', 'note.md\\.',
    'image.png/.', 'note.MD/.', 'note.json/',
])
def test_text_doc_type_overrides_path(path):
    assert is_text_doc('newnote', path) is False
    assert is_text_doc('plain', path)


@pytest.mark.parametrize('path,expected', [
    ('a/../B.md', 'b.md'), ('../Note.md', '../note.md'),
    ('Ä/İΣ.md', 'ä/i̇σ.md'), ('//Inbox///Note.md///', 'inbox/note.md'),
])
def test_explicit_path_vectors(path, expected):
    assert path2id(path) == expected
    if path.startswith('../'):
        with pytest.raises(ForbiddenPathError):
            validate_vault_path(path)


def test_text_extension_contract():
    assert TEXT_EXTENSIONS == {'.md', '.txt', '.svg', '.html', '.csv',
                               '.css', '.js', '.xml', '.canvas'}
    assert not is_text_doc('newnote', 'Note.MD')


def test_invalid_padding_identifies_chunk():
    with pytest.raises(ValueError, match='chunk 1'):
        assemble_binary(['YQ==', 'YQ'])


def test_non_ascii_binary_chunk_rejected():
    with pytest.raises(ValueError):
        assemble_binary(['é'])


def test_empty_chunks():
    assert assemble_binary([]) == b''
    assert assemble_text([]) == ''


def test_shared_note_classification():
    from livesync_format import is_note_document, SERVICE_NAMESPACE_PREFIXES, SERVICE_DOC_IDS
    for kind in ('plain', 'newnote', 'notes'):
        assert is_note_document({'_id': 'a.md', 'type': kind})
        assert not is_note_document({'_id': 'a.md', 'type': kind, 'deleted': True})
        for name in (*SERVICE_NAMESPACE_PREFIXES, *SERVICE_DOC_IDS, '_design/a', '_local/a'):
            assert not is_note_document({'_id': name, 'type': kind})
    assert not is_note_document({'_id': 'a.md', 'type': 'leaf'})


def test_shared_inline_and_legacy_helpers():
    from livesync_format import extract_inline_chunks, legacy_body
    doc = {'children': ['h:a', 'h:b'], 'eden': {'h:a': 'a', 'h:b': {'data': 'b'}, 'h:unused': 'x'}}
    assert extract_inline_chunks(doc) == {'h:a': 'a', 'h:b': 'b'}
    assert legacy_body({'type': 'notes', 'data': 'old'}) == 'old'
    assert legacy_body({'type': 'plain', 'data': 'unused'}) is None
    with pytest.raises(ValueError):
        extract_inline_chunks({'children': ['h:a'], 'eden': []})
    with pytest.raises(ValueError):
        legacy_body({'type': 'notes', 'data': None})


def test_clone_metadata_preserves_all_content_fields():
    from livesync_format import clone_metadata
    original = {'_id': 'a', '_rev': '1-a', 'children': ['h:a'],
                'eden': {'h:a': {'data': 'body'}}, 'data': 'legacy',
                'custom': {'future': [1]}, 'type': 'notes'}
    cloned = clone_metadata(original, path='b', mtime=99)
    assert cloned == {**original, 'path': 'b', 'mtime': 99}
    cloned['custom']['future'].append(2)
    cloned['eden']['h:a']['data'] = 'different'
    assert original['custom'] == {'future': [1]}
    assert original['eden']['h:a']['data'] == 'body'


# --- NFC path identity (Plan B; upstream pathNormalizationVersion v1) ---

def test_path2id_nfc_composes():
    # Upstream v1 (pathNormalization.ts normalisePathForIdentity): the raw
    # path is normalized to Unicode NFC before the existing normalization
    # and lowercase, so decomposed and precomposed spellings share one id.
    assert path2id("cafe\u0301.md", nfc=True) == path2id("café.md", nfc=True)
    assert path2id("cafe\u0301.md", nfc=True) == "café.md"
    # NFC applies per segment through the whole path, like upstream.
    assert path2id("No\u0308te/Bo\u0308ok.md", nfc=True) == "nöte/böok.md"


def test_path2id_nfc_ascii_unchanged():
    # ASCII paths have no decomposed forms; NFC mode must be a no-op.
    assert path2id("Inbox/Note.md", nfc=True) == "inbox/note.md"
    assert path2id("Inbox/Note.md", nfc=True) == path2id("Inbox/Note.md")
    assert path2id("_private/x.md", nfc=True) == "/_private/x.md"


def test_path2id_legacy_keeps_nfd():
    # Legacy mode (v0) must not collapse decomposed forms: the bytes of
    # the path reach the id unchanged apart from the existing rules.
    assert path2id("cafe\u0301.md") == "cafe\u0301.md"
    assert path2id("cafe\u0301.md") != path2id("café.md")
    assert path2id("cafe\u0301.md", nfc=False) == path2id("cafe\u0301.md")


def test_path2id_nfc_normalizes_before_lowercase():
    # W + U+030A (combining ring above): NFC leaves the sequence alone
    # (no precomposed capital exists), and the lowercase afterwards
    # yields "w" + U+030A — exactly upstream's normalize-then-lowercase
    # composition. Lowercasing FIRST would let NFC compose the result to
    # the precomposed U+1E98, a different id string entirely.
    assert path2id("W\u030a.md", nfc=True) == "w\u030a.md"
    wrong_order = unicodedata.normalize("NFC", "W\u030a.md".lower())
    assert wrong_order == "\u1e98.md"
    assert path2id("W\u030a.md", nfc=True) != wrong_order


def test_resolve_identity_preferred_nfc():
    milestone = {"tweak_values": {"PREFERRED": {"pathNormalizationVersion": 1}}}
    assert resolve_path_identity(milestone) == PATH_IDENTITY_NFC == "nfc"

    legacy = {"tweak_values": {"PREFERRED": {"pathNormalizationVersion": 0}}}
    assert resolve_path_identity(legacy) == PATH_IDENTITY_LEGACY == "legacy"

    # Older milestones store settings at the top level; same resolution.
    assert resolve_path_identity({"pathNormalizationVersion": 1}) == "nfc"
    assert resolve_path_identity({"pathNormalizationVersion": 0}) == "legacy"
    # An absent version is legacy (upstream treats undefined as version 0);
    # the map still needs a recognized tweak key to count as settings.
    assert resolve_path_identity(
        {"tweak_values": {"PREFERRED": {"encrypt": False}}}
    ) == "legacy"


def test_resolve_identity_device_fallback():
    # Without PREFERRED, every device map is read; any device on v1 makes
    # the vault NFC (mirrors the CouchDB startup check semantics).
    devices = {"tweak_values": {
        "device-a": {"pathNormalizationVersion": 0},
        "device-b": {"pathNormalizationVersion": 1},
    }}
    assert resolve_path_identity(devices) == "nfc"
    legacy_devices = {"tweak_values": {
        "device-a": {"pathNormalizationVersion": 0},
    }}
    assert resolve_path_identity(legacy_devices) == "legacy"
    # PREFERRED is authoritative when present and overrides device maps.
    preferred = {"tweak_values": {
        "PREFERRED": {"pathNormalizationVersion": 0},
        "device-a": {"pathNormalizationVersion": 1},
    }}
    assert resolve_path_identity(preferred) == "legacy"


def test_resolve_identity_refuses_encrypt():
    milestone = {"tweak_values": {"PREFERRED": {"encrypt": True}}}
    with pytest.raises(UnsupportedVaultSettings, match="encryption"):
        resolve_path_identity(milestone)
    # Any device map encrypting also refuses when PREFERRED is absent
    # (the device-fallback path; PREFERRED stays authoritative upstream).
    with pytest.raises(UnsupportedVaultSettings, match="encryption"):
        resolve_path_identity({"tweak_values": {
            "device-a": {"encrypt": True},
        }})


def test_resolve_identity_refuses_obfuscation():
    milestone = {"tweak_values": {"PREFERRED": {"usePathObfuscation": True}}}
    with pytest.raises(UnsupportedVaultSettings, match="obfuscation"):
        resolve_path_identity(milestone)


@pytest.mark.parametrize("nfc", [False, True])
def test_case_sensitive_identity(nfc):
    from livesync_format import PATH_IDENTITY_CASE_SENSITIVE
    identity = resolve_path_identity({"handleFilenameCaseSensitive": True,
                                      "pathNormalizationVersion": int(nfc)})
    assert PATH_IDENTITY_CASE_SENSITIVE in identity
    assert ("nfc" in identity) == nfc
    assert path2id("A.md", nfc=nfc, case_sensitive=True) == "A.md"
    assert path2id("a.md", nfc=nfc, case_sensitive=True) == "a.md"
    assert path2id("_A.md", case_sensitive=True) == "/_A.md"
    assert (path2id("Cafe\u0301.md", nfc=nfc, case_sensitive=True) ==
            path2id("Café.md", nfc=nfc, case_sensitive=True)) == nfc


def test_obfuscated_identifier_is_unchanged():
    assert path2id("f:AbCd") == "f:AbCd"


def test_resolve_identity_no_settings_fails_closed():
    for milestone in (
        {"tweak_values": {}},                      # no readable settings
        {"tweak_values": {"device-a": None}},      # values are not maps
        {"tweak_values": []},                      # tweak_values unreadable
    ):
        with pytest.raises(UnsupportedVaultSettings, match="missing or unreadable"):
            resolve_path_identity(milestone)


def test_resolve_identity_settings_must_be_recognized():
    # A map only counts as readable settings when it holds at least one
    # recognized tweak key; anything else fails closed instead of
    # silently resolving to legacy.
    with pytest.raises(UnsupportedVaultSettings, match="missing or unreadable"):
        resolve_path_identity({})              # empty flat milestone
    metadata_only = {                          # milestone fields, no tweaks
        "_rev": "1-abc",
        "tweakModified": 5,
        "node_chunk_info": {},
    }
    with pytest.raises(UnsupportedVaultSettings, match="missing or unreadable"):
        resolve_path_identity(metadata_only)
    with pytest.raises(UnsupportedVaultSettings, match="missing or unreadable"):
        resolve_path_identity({"tweak_values": {"PREFERRED": {}}})

    # A flat map with a recognized key resolves normally...
    assert resolve_path_identity({"pathNormalizationVersion": 0}) == "legacy"
    # ...and the device fallback skips unrecognized maps.
    mixed = {"tweak_values": {
        "device-a": {"tweakModified": 5},       # not settings
        "device-b": {"pathNormalizationVersion": 1},
    }}
    assert resolve_path_identity(mixed) == "nfc"


def test_resolve_identity_refuses_unknown_version():
    # Upstream pathNormalization.ts accepts only absent, 0, or 1; every
    # other value must be refused with the offending value named.
    for version in (2, 7, -1, "1", "x", None, True, 1.5):
        milestone = {"tweak_values": {"PREFERRED": {
            "pathNormalizationVersion": version,
        }}}
        with pytest.raises(
            UnsupportedVaultSettings, match="pathNormalizationVersion"
        ):
            resolve_path_identity(milestone)
    # The refusal names the offending value (not a secret: a version).
    with pytest.raises(UnsupportedVaultSettings, match="pathNormalizationVersion"):
        resolve_path_identity(
            {"tweak_values": {"PREFERRED": {"pathNormalizationVersion": 2}}}
        )
    # Explicit 0 and 1 stay valid, as does a device map disagreeing on 1.
    assert resolve_path_identity(
        {"tweak_values": {"PREFERRED": {"pathNormalizationVersion": 1}}}
    ) == "nfc"
    assert resolve_path_identity(
        {"tweak_values": {"PREFERRED": {"pathNormalizationVersion": 0}}}
    ) == "legacy"


def test_unsupported_vault_settings_contract():
    # Purity contract, pinned: livesync_format never imports
    # storage_base, so the refusal crosses the storage boundary as this
    # ValueError subclass and backends re-raise it as their
    # UnsupportedVault with str(exc) as the message.
    import inspect

    import livesync_format as module

    assert "storage_base" not in inspect.getsource(module)
    # Cold imports must preserve the boundary transitively through the splitter.
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, '-c', "import sys; import livesync_format; assert 'storage_base' not in sys.modules"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert issubclass(UnsupportedVaultSettings, ValueError)
    with pytest.raises(UnsupportedVaultSettings) as excinfo:
        resolve_path_identity({})
    assert str(excinfo.value) == "vault settings missing or unreadable"


@pytest.mark.parametrize("preferred", ["corrupt", None, 42, []])
def test_malformed_preferred_refuses_device_fallback(preferred):
    with pytest.raises(UnsupportedVaultSettings) as exc:
        resolve_path_identity({"tweak_values": {
            "PREFERRED": preferred,
            "device": {"pathNormalizationVersion": 0},
        }})
    assert "PREFERRED" in str(exc.value)
    assert type(preferred).__name__ in str(exc.value)
    assert "corrupt" not in str(exc.value)


def test_preferred_absent_skips_heterogeneous_device_entries():
    assert resolve_path_identity({"tweak_values": {
        "old-device": None,
        "other-device": ["legacy"],
        "device": {"pathNormalizationVersion": 0},
    }}) == "legacy"
