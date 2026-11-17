"""Tests for round-trip YAML frontmatter handling (audit D5/D6/D12/D17)."""

import pytest

from frontmatter import (
    FrontmatterError,
    add_tags,
    delete_properties,
    get_properties,
    insert_after_frontmatter,
    remove_tags,
    set_properties,
    split_frontmatter,
)

# Audit D5/D17: valid Obsidian YAML that the old hand-written parser corrupted.
ROUNDTRIP_NOTES = [
    # double-quoted wikilink and a single-quoted value containing a colon
    '---\nrelated: "[[Other]]"\ntitle: \'Re: meeting\'\n---\n\nBody.\n',
    # block scalar
    '---\nnote: |\n  line one\n  line two\n---\nbody\n',
    # nested mapping
    '---\nmeta:\n  a: 1\n  b:\n    c: x\n---\nbody\n',
    # comments
    '---\n# top-level comment\nkey: value # inline\n---\nbody\n',
    # quoted number and leading-zero scalar (old parser wrote 42 / 1234)
    "---\nnum: '42'\npin: 01234\n---\nbody\n",
    # key with an empty (null) value
    '---\naliases:\n---\nbody\n',
    # flow-style list stays flow-style
    '---\ntags: [a, b]\n---\nbody\n',
]


@pytest.mark.parametrize("text", ROUNDTRIP_NOTES)
def test_roundtrip_unchanged(text):
    assert set_properties(text, {}) == text
    assert delete_properties(text, []) == text
    assert add_tags(text, []) == text
    assert remove_tags(text, []) == text


def test_delimiters_whole_lines():
    # Audit D6: '---' inside a value must not close the block.
    text = "---\nsummary: before---after\nkey: value\n---\nbody"
    fm_raw, body = split_frontmatter(text)
    assert fm_raw == "summary: before---after\nkey: value\n"
    assert body == "body"

    # a '---' line further down belongs to the body, not the frontmatter
    text2 = "---\nkey: value\n---\nfirst\n---\nsecond"
    fm_raw2, body2 = split_frontmatter(text2)
    assert fm_raw2 == "key: value\n"
    assert body2 == "first\n---\nsecond"


def test_list_without_indent():
    # Audit D12: list items at the same column as the key are still a list.
    text = "---\ntags:\n- a\n- b\n---\nbody"
    assert get_properties(text)["tags"] == ["a", "b"]

    out = add_tags(text, ["c"])
    assert get_properties(out)["tags"] == ["a", "b", "c"]
    assert "\n- c\n" in out


def test_set_preserves_neighbors():
    text = (
        "---\n"
        'related: "[[Other]]"\n'
        "title: 'Re: meeting'\n"
        "num: '42'\n"
        "# comment\n"
        "aliases:\n"
        "---\n"
        "body"
    )
    out = set_properties(text, {"priority": "high"})
    assert 'related: "[[Other]]"' in out
    assert "title: 'Re: meeting'" in out
    assert "num: '42'" in out
    assert "# comment" in out
    assert "aliases:" in out
    assert get_properties(out) == {
        "related": "[[Other]]",
        "title": "Re: meeting",
        "num": "42",
        "aliases": None,
        "priority": "high",
    }


def test_remove_missing_is_noop():
    text = "---\ntags:\n- a\n---\nbody"
    assert delete_properties(text, ["missing"]) == text
    assert remove_tags(text, ["nothere"]) == text


def test_tags_scalar_to_list():
    # Audit D12: a scalar tags value becomes a list when tags are added,
    # with values coerced to strings (tags property is list[str]).
    text = "---\ntags: 2024\n---\nbody"
    out = add_tags(text, ["journal"])
    assert get_properties(out)["tags"] == ["2024", "journal"]


def test_tags_coerced_to_strings_on_edit():
    text = "---\ntags:\n- 2024\n- b\n---\nbody"
    out = add_tags(text, ["c"])
    assert get_properties(out)["tags"] == ["2024", "b", "c"]
    out = remove_tags(text, ["b"])
    assert get_properties(out)["tags"] == ["2024"]


def test_equal_value_edit_is_noop():
    # Non-empty props that already match must return the original object.
    text = "---\nx: true\nnum: 42\ntitle: keep\n---\nbody"
    assert set_properties(text, {"x": True}) is text
    assert set_properties(text, {"num": 42}) is text
    assert set_properties(text, {"title": "keep"}) is text


def test_type_change_is_real_edit():
    # True == 1 in Python, but a bool->int change must rewrite the value.
    text = "---\nx: true\nnum: 42\n---\nbody"
    out = set_properties(text, {"x": 1})
    assert out != text
    assert get_properties(out) == {"x": 1, "num": 42}
    # int -> float is a real change as well
    out2 = set_properties(text, {"num": 42.0})
    assert out2 != text
    assert get_properties(out2) == {"x": True, "num": 42.0}


def test_comments_only_block_keeps_comments():
    text = "---\n# retain me\n---\nbody"
    out = set_properties(text, {"x": 1})
    assert "# retain me" in out
    assert get_properties(out) == {"x": 1}
    # multiple comments, and untouched block stays as-is
    text2 = "---\n# one\n# two\n---\nbody"
    out2 = set_properties(text2, {"x": 1})
    assert "# one\n# two" in out2
    assert set_properties(text, {}) is text
    assert get_properties(text) == {}


def test_insert_after_frontmatter_at_eof_delimiter():
    # Note ending in '---' without a newline must not glue the insertion
    # onto the delimiter line.
    text = "---\nx: 1\n---"
    out = insert_after_frontmatter(text, "Inserted.\n")
    assert out == "---\nx: 1\n---\nInserted.\n"
    # the normal case is unaffected
    text2 = "---\nx: 1\n---\nBody."
    assert insert_after_frontmatter(text2, "I.\n") == "---\nx: 1\n---\nI.\nBody."


def test_insert_after_frontmatter():
    # Audit D7: insertion goes after the closing '---', not before it.
    text = "---\nkey: value\n---\nBody text."
    out = insert_after_frontmatter(text, "Inserted.\n")
    assert out == "---\nkey: value\n---\nInserted.\nBody text."

    # without frontmatter the insertion goes to the top of the note
    assert insert_after_frontmatter("Body.", "Top.\n") == "Top.\nBody."


def test_split_without_frontmatter():
    assert split_frontmatter("plain note") == ("", "plain note")
    # '----' on the first line is not a delimiter
    assert split_frontmatter("----\nnot fm\n") == ("", "----\nnot fm\n")
    # unterminated block is not frontmatter
    assert split_frontmatter("---\nkey: value\n") == ("", "---\nkey: value\n")


def test_split_empty_block():
    fm_raw, body = split_frontmatter("---\n---\nbody")
    assert fm_raw == ""
    assert body == "body"


def test_get_properties_plain_values():
    text = "---\ntitle: plain\nnum: '42'\nplain: 42\ndone: true\naliases:\n---\nbody"
    assert get_properties(text) == {
        "title": "plain",
        "num": "42",
        "plain": 42,
        "done": True,
        "aliases": None,
    }


def test_get_properties_without_frontmatter():
    assert get_properties("no frontmatter here") == {}


def test_set_properties_creates_block():
    out = set_properties("Body.", {"title": "New"})
    assert out == "---\ntitle: New\n---\nBody."
    assert get_properties(out) == {"title": "New"}


def test_set_properties_empty_creates_nothing():
    assert set_properties("Body.", {}) == "Body."


def test_add_tags_deduplicates():
    text = "---\ntags:\n- a\n---\nbody"
    assert add_tags(text, ["a"]) == text
    out = add_tags(text, ["a", "b"])
    assert get_properties(out)["tags"] == ["a", "b"]


def test_add_tags_without_frontmatter():
    out = add_tags("Body.", ["x"])
    assert out == "---\ntags:\n- x\n---\nBody."
    assert get_properties(out)["tags"] == ["x"]


def test_remove_tags():
    text = "---\ntags:\n- a\n- b\n---\nbody"
    out = remove_tags(text, ["a"])
    assert get_properties(out)["tags"] == ["b"]
    out_all = remove_tags(text, ["a", "b"])
    assert get_properties(out_all)["tags"] == []


def test_remove_tags_scalar():
    text = "---\ntags: 2024\n---\nbody"
    out = remove_tags(text, ["2024"])
    assert get_properties(out)["tags"] == []
    assert remove_tags(text, ["other"]) == text


def test_delete_properties():
    text = "---\na: 1\nb: 2\n---\nbody"
    out = delete_properties(text, ["a"])
    assert get_properties(out) == {"b": 2}
    assert delete_properties("no fm", ["a"]) == "no fm"


def test_invalid_yaml_raises():
    text = "---\nbroken: [unclosed\n---\nbody"
    with pytest.raises(FrontmatterError):
        get_properties(text)
    with pytest.raises(FrontmatterError):
        set_properties(text, {"a": 1})


def test_reject_aliases_depth_and_node_budget():
    from frontmatter import _to_plain
    shared = [1, 2]
    with pytest.raises(FrontmatterError, match='aliases'):
        _to_plain({'a': shared, 'b': [shared] * 8})
    value = 'leaf'
    for _ in range(34):
        value = [value]
    with pytest.raises(FrontmatterError, match='depth'):
        _to_plain(value)
    with pytest.raises(FrontmatterError, match='nodes'):
        _to_plain(list(range(10001)))
    assert _to_plain({'a': [{'b': 2}]}) == {'a': [{'b': 2}]}


def test_yaml_alias_bomb_rejected():
    text = '---\na: &a [one, two]\nb: &b [*a, *a, *a]\nc: [*b, *b, *b]\n---\nbody'
    with pytest.raises(FrontmatterError, match='aliases'):
        get_properties(text)
