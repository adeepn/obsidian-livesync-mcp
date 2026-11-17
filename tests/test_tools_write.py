"""Write-tool regressions for the audit's data-safety boundaries."""

import inspect
import json

import pytest

import server
from storage_base import ConcurrentModification, StorageError
from tests.fake_backend import FakeBackend


@pytest.fixture
def vault(monkeypatch):
    fake = FakeBackend()
    fake.add("N.md", "original")
    monkeypatch.setattr(server, "backend", fake)
    return fake


def assert_error(result):
    assert "error" in result.lower(), result


def assert_no_mutations(vault):
    mutations = {"write_note", "rename_note", "copy_note", "delete_note"}
    assert not any(method in mutations for method, _ in vault.calls)


async def test_update_requires_mode(vault):
    with pytest.raises(TypeError):
        await server.obsidian_update_note("N.md")
    assert (
        inspect.signature(server.obsidian_update_note).parameters["mode"].default
        is inspect.Parameter.empty
    )


async def test_update_empty_overwrite_refused(vault):
    assert_error(await server.obsidian_update_note("N.md", "overwrite"))
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


@pytest.mark.parametrize(
    "args", [{"search": "x"}, {"replace": "x"}, {"heading": "x"}, {"regex": True}]
)
async def test_update_search_args_with_overwrite_refused(vault, args):
    assert_error(
        await server.obsidian_update_note("N.md", "overwrite", content="new", **args)
    )
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


@pytest.mark.parametrize("regex", [False, True])
async def test_search_replace_literal(vault, regex):
    await server.obsidian_update_note(
        "N.md", "search-replace", search="original", replace=r"\frac\1", regex=regex
    )
    assert vault.notes["N.md"]["text"] == r"\frac\1"


async def test_search_replace_counts_matches(vault):
    result = await server.obsidian_update_note(
        "N.md", "search-replace", search="i", replace="i"
    )
    assert "2 matches replaced" in result
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


async def test_identical_overwrite_never_writes(vault):
    result = await server.obsidian_update_note("N.md", "overwrite", content="original")
    assert "Updated" in result
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


async def test_invalid_regex_handled(vault):
    result = await server.obsidian_update_note(
        "N.md", "search-replace", search="[", regex=True
    )
    assert "invalid" in result.lower() and "regex" in result.lower()
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


async def test_fetch_edit_overwrite_keeps_frontmatter(vault):
    raw = '---\ntitle: "42" # comment\n---\nBody\n'
    vault.add("N.md", raw)
    fetched = json.loads(await server.obsidian_fetch("N.md"))["content"]
    await server.obsidian_update_note("N.md", "overwrite", content=fetched)
    assert vault.notes["N.md"]["text"] == raw


async def test_prepend_after_frontmatter(vault):
    vault.add("N.md", "---\ntitle: X\n---\nBody")
    await server.obsidian_update_note("N.md", "prepend", content="First")
    assert vault.notes["N.md"]["text"] == "---\ntitle: X\n---\nFirst\nBody"


async def test_properties_set_on_canvas_refused(vault):
    vault.add("N.canvas", "{}")
    assert_error(await server.obsidian_manage_properties("N.canvas", "set", '{"x": 1}'))
    assert vault.notes["N.canvas"]["text"] == "{}"
    assert_no_mutations(vault)


@pytest.mark.parametrize("properties", ["[]", "1", "null", '"str"'])
async def test_properties_json_type_checked(vault, properties):
    result = await server.obsidian_manage_properties("N.md", "set", properties)
    assert_error(result)
    assert "object" in result.lower()
    assert vault.notes["N.md"]["text"] == "original"
    assert_no_mutations(vault)


async def test_tags_add_on_binary_refused(vault):
    vault.add("photo.png", data=b"photo")
    assert_error(await server.obsidian_manage_tags("photo.png", "add", "one"))
    assert vault.notes["photo.png"]["data"] == b"photo"
    assert_no_mutations(vault)


async def test_after_heading_real_headings(vault):
    raw = "---\n# Tasks\n---\n#project\n```md\n## Tasks\n```\n~~~\n## Tasks\n~~~\n## Tasks\nBody"
    vault.add("N.md", raw)
    assert_error(await server.obsidian_update_note(
        "N.md", "after-heading", heading="project", content="INSERT"
    ))
    assert vault.notes["N.md"]["text"] == raw
    assert_no_mutations(vault)
    await server.obsidian_update_note(
        "N.md", "after-heading", heading="## Tasks", content="INSERT"
    )
    assert vault.notes["N.md"]["text"] == raw[:-4] + "INSERT\nBody"


async def test_move_case_only(vault):
    await server.obsidian_move_note("N.md", "n.md")
    assert "N.md" not in vault.notes
    assert vault.notes["n.md"]["text"] == "original"


async def test_move_onto_existing_refused(vault):
    vault.add("target.md", "target")
    assert_error(await server.obsidian_move_note("N.md", "target.md"))
    assert vault.notes["target.md"]["text"] == "target"
    assert vault.notes["N.md"]["text"] == "original"


@pytest.mark.parametrize("target", ["target.md", ""])
async def test_duplicate_onto_existing_refused(vault, target):
    name = target or "N copy.md"
    vault.add(name, "target")
    assert_error(await server.obsidian_duplicate_note("N.md", target))
    assert vault.notes[name]["text"] == "target"
    assert vault.notes["N.md"]["text"] == "original"


async def test_binary_move_allowed(vault):
    vault.add("photo.png", data=b"photo")
    await server.obsidian_move_note("photo.png", "moved.png")
    assert "photo.png" not in vault.notes
    assert vault.notes["moved.png"]["data"] == b"photo"


async def test_binary_duplicate_delete(vault):
    vault.add("photo.png", data=b"photo")
    await server.obsidian_duplicate_note("photo.png")
    assert vault.notes["photo copy.png"]["data"] == b"photo"
    await server.obsidian_delete_note("photo.png")
    assert "photo.png" not in vault.notes


@pytest.mark.parametrize("error", [ConcurrentModification, StorageError])
async def test_success_only_after_write(vault, error):
    vault.errors["write_note"] = error("private")
    result = await server.obsidian_update_note("N.md", "overwrite", content="new")
    assert_error(result)
    assert "Updated" not in result and "private" not in result
    assert vault.notes["N.md"]["text"] == "original"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("obsidian_manage_properties", ("N.md", "set", '{"title": "42"}')),
        ("obsidian_manage_properties", ("N.md", "delete", "", "absent")),
        ("obsidian_manage_tags", ("N.md", "add", "one")),
        ("obsidian_manage_tags", ("N.md", "remove", "absent")),
    ],
)
async def test_frontmatter_noop_never_writes(vault, tool, args):
    raw = '---\ntitle: "42" # comment\ntags: [one]\n---\n#inline'
    vault.add("N.md", raw)
    await getattr(server, tool)(*args)
    assert vault.notes["N.md"]["text"] == raw
    assert not any(method == "write_note" for method, _ in vault.calls)


async def test_tags_coerce_and_inline_honesty(vault):
    vault.add("N.md", "---\ntags: [42, true]\n---\n#inline")
    result = json.loads(await server.obsidian_manage_tags("N.md"))
    assert result["frontmatter_tags"] == ["42", "True"]
    result = await server.obsidian_manage_tags("N.md", "remove", "inline")
    assert "inline tags untouched" in result.lower()
    assert vault.notes["N.md"]["text"].endswith("#inline")


async def test_template_variables_and_create_rules(vault):
    vault.add("T.md", "{{name}} {{ name }} {{n}} {{yes}}")
    result = await server.obsidian_create_from_template(
        "T.md", "new.md", '{"name":"X", "n":2, "yes":true}'
    )
    assert "Created" in result
    assert vault.notes["new.md"]["text"] == '"X" "X" 2 true'
    assert_error(await server.obsidian_create_from_template("T.md", "new.md"))
    assert_error(await server.obsidian_create_from_template("T.md", "other.md", "[]"))


async def test_create_properties_roundtrip_and_type(vault):
    raw = '---\ntitle: "42" # comment\n---\nBody'
    await server.obsidian_create_note("new.md", raw, '{"x": 1}')
    assert 'title: "42" # comment' in vault.notes["new.md"]["text"]
    assert_error(await server.obsidian_create_note("bad.md", "", "[]"))
    assert_error(await server.obsidian_create_note("bad.canvas", "{}", '{"x":1}'))


async def test_unknown_mode_lists_choices(vault):
    result = await server.obsidian_update_note("N.md", "invalid")
    for mode in ["overwrite", "append", "prepend", "after-heading", "search-replace"]:
        assert mode in result


async def test_mcp_schema_requires_mode(vault):
    tools = await server.mcp.list_tools()
    tool = next(tool for tool in tools if tool.name == "obsidian_update_note")
    assert "mode" in tool.inputSchema["required"]


@pytest.mark.parametrize(
    "tool,args,method",
    [
        ("obsidian_create_note", ("new.md", "new"), "write_note"),
        ("obsidian_create_from_template", ("N.md", "new.md"), "write_note"),
        ("obsidian_manage_properties", ("N.md", "set", '{"x":1}'), "write_note"),
        ("obsidian_manage_tags", ("N.md", "add", "one"), "write_note"),
        ("obsidian_move_note", ("N.md", "new.md"), "rename_note"),
        ("obsidian_duplicate_note", ("N.md", "new.md"), "copy_note"),
        ("obsidian_delete_note", ("N.md",), "delete_note"),
    ],
)
@pytest.mark.parametrize("error", [ConcurrentModification, StorageError])
async def test_mutation_errors_never_report_success(vault, tool, args, method, error):
    vault.errors[method] = error("private")
    result = await getattr(server, tool)(*args)
    assert_error(result)
    for word in ["Updated", "Created", "Moved", "Duplicated", "Deleted", "private"]:
        assert word not in result
    assert vault.notes["N.md"]["text"] == "original"
    assert "new.md" not in vault.notes


@pytest.mark.parametrize(
    "tool,args,method",
    [
        ("obsidian_create_note", ("new.md", "new"), "write_note"),
        ("obsidian_update_note", ("N.md", "append", "new"), "read_note"),
        ("obsidian_move_note", ("N.md", "new.md"), "rename_note"),
        ("obsidian_duplicate_note", ("N.md",), "copy_note"),
        ("obsidian_delete_note", ("N.md",), "delete_note"),
    ],
)
async def test_forbidden_path_is_sanitized(vault, tool, args, method):
    from livesync_format import ForbiddenPathError

    vault.errors[method] = ForbiddenPathError("private")
    result = await getattr(server, tool)(*args)
    assert_error(result)
    assert "forbidden" in result.lower() and "private" not in result


@pytest.mark.parametrize(
    "tool,args",
    [
        ("obsidian_update_note", ("photo.png", "append", "new")),
        ("obsidian_manage_properties", ("photo.png", "set", '{"x":1}')),
        ("obsidian_create_from_template", ("photo.png", "new.md")),
    ],
)
async def test_text_edit_binary_refused(vault, tool, args):
    vault.add("photo.png", data=b"photo")
    result = await getattr(server, tool)(*args)
    assert_error(result)
    assert "text note" in result.lower()
    assert vault.notes["photo.png"]["data"] == b"photo"
    assert "new.md" not in vault.notes
    assert_no_mutations(vault)


async def test_append_and_no_matches(vault):
    await server.obsidian_update_note("N.md", "append", "end")
    assert vault.notes["N.md"]["text"] == "original\nend"
    vault.calls.clear()
    result = await server.obsidian_update_note(
        "N.md", "search-replace", search="absent"
    )
    assert "No matches" in result
    assert not any(method == "write_note" for method, _ in vault.calls)


async def test_same_path_move(vault):
    await server.obsidian_move_note("N.md", "N.md")
    assert vault.notes["N.md"]["text"] == "original"
    assert not any(method in ("copy_note", "delete_note") for method, _ in vault.calls)


@pytest.mark.parametrize("properties", ["{", "[]", "1", "null", '"str"'])
@pytest.mark.parametrize("path", ["N.md", "N.canvas"])
async def test_invalid_properties_never_mutate(vault, properties, path):
    vault.add(path, "original")
    assert_error(await server.obsidian_manage_properties(path, "set", properties))
    assert vault.notes[path]["text"] == "original"
    assert_no_mutations(vault)


@pytest.mark.parametrize("path,properties", [("N.md", "{"), ("N.md", "[]"),
                                            ("N.canvas", "{")])
async def test_create_invalid_properties_never_mutate(vault, path, properties):
    assert_error(await server.obsidian_create_note(path, "replacement", properties))
    assert vault.notes["N.md"]["text"] == "original"
    assert "N.canvas" not in vault.notes
    assert_no_mutations(vault)


async def test_canvas_invalid_content_never_mutates(vault):
    content = "not json{"
    vault.add("N.canvas", content)
    result = await server.obsidian_canvas_node("N.canvas", text="new node")
    assert result == "Invalid canvas format."
    assert vault.notes["N.canvas"]["text"] == content
    assert_no_mutations(vault)


# --- read-through revision races (device writes between tool read/write) ----


class RacingFake(FakeBackend):
    """A device commit lands right after the tool's read_note.

    The read still returns the pre-device content and revision, so a
    tool that ignores revisions would clobber the device edit, while a
    tool carrying ``expected_rev`` must fail with ConcurrentModification.
    """

    def __init__(self):
        super().__init__()
        self.race_paths = set()

    async def read_note(self, path):
        note = await super().read_note(path)
        if note is not None and path in self.race_paths:
            self.race_paths.discard(path)
            self.add(path, "device edit")
        return note


def racing_vault(monkeypatch, path, text="original"):
    fake = RacingFake()
    fake.add(path, text)
    fake.race_paths = {path}
    monkeypatch.setattr(server, "backend", fake)
    return fake


async def test_update_note_lost_update_prevented(monkeypatch):
    fake = racing_vault(monkeypatch, "N.md")
    result = await server.obsidian_update_note("N.md", "append", "tool edit")
    assert "concurrent" in result.lower()
    assert fake.notes["N.md"]["text"] == "device edit"


async def test_manage_properties_lost_update_prevented(monkeypatch):
    fake = racing_vault(monkeypatch, "N.md")
    result = await server.obsidian_manage_properties("N.md", "set", '{"x": 1}')
    assert "concurrent" in result.lower()
    assert fake.notes["N.md"]["text"] == "device edit"


async def test_manage_tags_lost_update_prevented(monkeypatch):
    fake = racing_vault(monkeypatch, "N.md")
    result = await server.obsidian_manage_tags("N.md", "add", "urgent")
    assert "concurrent" in result.lower()
    assert fake.notes["N.md"]["text"] == "device edit"


async def test_canvas_node_lost_update_prevented(monkeypatch):
    fake = racing_vault(monkeypatch, "B.canvas", '{"nodes": [], "edges": []}')
    result = await server.obsidian_canvas_node("B.canvas", text="tool node")
    assert "concurrent" in result.lower()
    assert fake.notes["B.canvas"]["text"] == "device edit"


async def test_canvas_edge_lost_update_prevented(monkeypatch):
    fake = racing_vault(
        monkeypatch, "B.canvas",
        '{"nodes": [{"id": "n1"}], "edges": []}',
    )
    result = await server.obsidian_canvas_edge(
        "B.canvas", from_node="n1", to_node="n1"
    )
    assert "concurrent" in result.lower()
    assert fake.notes["B.canvas"]["text"] == "device edit"


async def test_read_modify_write_tools_pass_expected_rev(vault):
    # Happy path: the write carries the revision captured by the read.
    await server.obsidian_update_note("N.md", "append", "more")
    write = next(args for m, args in vault.calls if m == "write_note")
    assert write[-1] == "1-x"  # expected_rev from the read
    # Canvas writes carry it too, and creation keeps None (create semantics).
    vault.calls.clear()
    vault.add("B.canvas", "{}")
    await server.obsidian_canvas_node("B.canvas", text="x", node_id="n1")
    write = next(
        args for m, args in vault.calls
        if m == "write_note" and args[0] == "B.canvas"
    )
    assert write[-1] == "1-x"
    assert write[2] is False  # update, not create


async def test_create_tools_keep_last_writer_wins(vault):
    # Plain creates never carry a revision precondition.
    await server.obsidian_create_note("fresh.md", "body")
    write = next(
        args for m, args in vault.calls
        if m == "write_note" and args[0] == "fresh.md"
    )
    assert write[-1] is None
    assert write[2] is True


async def test_move_partial_failure_message(vault):
    from storage_base import RenamePartialError
    vault.errors['rename_note'] = RenamePartialError('N.md', 'Other.md')
    message = await server.obsidian_move_note('N.md', 'Other.md')
    assert 'N.md' in message and 'Other.md' in message
    assert 'both' in message.lower()
    assert 'removing the destination' in message
    assert 'read it again' not in message


def test_conflict_and_stale_revision_messages_differ():
    from storage_base import UnresolvedConflicts
    stale = server._storage_message(ConcurrentModification())
    conflict = server._storage_message(UnresolvedConflicts())
    assert stale != conflict
    assert 'Obsidian' in conflict and 'resolve' in conflict.lower()


async def test_move_unknown_source_state_never_recommends_destination_removal(vault):
    from storage_base import RenamePartialError
    vault.errors['rename_note'] = RenamePartialError('N.md', 'Other.md', source_state='unknown')
    message = await server.obsidian_move_note('N.md', 'Other.md')
    assert 'N.md' in message and 'Other.md' in message
    assert 'could not be confirmed' in message
    assert 'verify both paths' in message.lower()
    assert 'removing the destination' not in message
    assert 'exists at both' not in message
