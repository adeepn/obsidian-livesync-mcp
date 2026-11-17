"""Canvas tool robustness per audit D16 (spec-valid `{}`, edge cases)."""

import json
import re

import pytest

import server
from tests.fake_backend import FakeBackend


@pytest.fixture
def vault(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(server, "backend", fake, raising=False)
    return fake


def assert_no_mutations(vault):
    mutations = {"write_note", "rename_note", "copy_note", "delete_note"}
    assert not any(method in mutations for method, _ in vault.calls)


def writes_for(vault, path):
    return [args for method, args in vault.calls
            if method == "write_note" and args[0] == path]


async def test_canvas_empty_objects(vault):
    vault.add("Empty.canvas", "{}")
    result = await server.obsidian_canvas_edge(
        "Empty.canvas", action="remove", edge_id="e1")
    assert "not found" in result.lower()
    assert_no_mutations(vault)

    result = await server.obsidian_canvas_node(
        "Empty.canvas", text="first", node_id="n1")
    assert result == "Added text node 'n1' to 'Empty.canvas'."
    canvas = json.loads(vault.notes["Empty.canvas"]["text"])
    assert [n["id"] for n in canvas["nodes"]] == ["n1"]
    assert canvas["edges"] == []

    vault.add("NoEdges.canvas",
              '{"nodes": [{"id": "n1", "type": "text", "text": "x"}]}')
    result = await server.obsidian_canvas_edge(
        "NoEdges.canvas", from_node="n1", to_node="n1", edge_id="e1")
    assert result == "Added edge 'e1' (n1 -> n1) to 'NoEdges.canvas'."
    canvas = json.loads(vault.notes["NoEdges.canvas"]["text"])
    assert canvas["edges"] == [{"id": "e1", "fromNode": "n1", "toNode": "n1"}]
    assert canvas["nodes"][0]["text"] == "x"


async def test_canvas_missing_file_add_creates(vault):
    result = await server.obsidian_canvas_node("New.canvas", text="root")
    match = re.fullmatch(r"Added text node '([0-9a-f]{16})' to 'New.canvas'.",
                         result)
    assert match, result
    writes = writes_for(vault, "New.canvas")
    assert len(writes) == 1
    path, content, create = writes[0][:3]
    assert create is True
    canvas = json.loads(content)
    assert canvas["nodes"][0]["id"] == match.group(1)
    assert canvas["nodes"][0]["text"] == "root"
    assert json.loads(vault.notes["New.canvas"]["text"]) == canvas

    result = await server.obsidian_canvas_node(
        "New.canvas", text="second", node_id="n2")
    assert result == "Added text node 'n2' to 'New.canvas'."
    assert writes_for(vault, "New.canvas")[-1][2] is False

    result = await server.obsidian_canvas_node(
        "Missing.canvas", action="remove", node_id="n1")
    assert result == "Canvas 'Missing.canvas' not found."
    result = await server.obsidian_canvas_edge(
        "Missing.canvas", from_node="a", to_node="b")
    assert result == "Canvas 'Missing.canvas' not found."
    assert writes_for(vault, "Missing.canvas") == []


async def test_canvas_node_requires_id_for_remove(vault):
    vault.add("B.canvas",
              '{"nodes": [{"id": "n1", "type": "text"}], "edges": []}')
    result = await server.obsidian_canvas_node("B.canvas", action="remove")
    assert result == "node_id required for remove action."
    result = await server.obsidian_canvas_edge("B.canvas", action="remove")
    assert result == "edge_id required for remove action."
    assert vault.notes["B.canvas"]["text"] == \
        '{"nodes": [{"id": "n1", "type": "text"}], "edges": []}'
    assert_no_mutations(vault)


@pytest.mark.parametrize("content", ["not json{", "[]", '"str"', "null", "3",
                                     "", "   "])
async def test_canvas_invalid_json_error(vault, content):
    vault.add("Bad.canvas", content)
    calls = (
        lambda: server.obsidian_canvas_node("Bad.canvas", text="x"),
        lambda: server.obsidian_canvas_node(
            "Bad.canvas", action="remove", node_id="n1"),
        lambda: server.obsidian_canvas_edge(
            "Bad.canvas", from_node="a", to_node="b"),
        lambda: server.obsidian_canvas_edge(
            "Bad.canvas", action="remove", edge_id="e1"),
    )
    for call in calls:
        assert await call() == "Invalid canvas format."
    assert vault.notes["Bad.canvas"]["text"] == content
    assert_no_mutations(vault)


async def test_canvas_empty_file_add_refused_not_created(vault):
    vault.add("Blank.canvas", "")
    result = await server.obsidian_canvas_node("Blank.canvas", text="x")
    assert result == "Invalid canvas format."
    result = await server.obsidian_canvas_edge(
        "Blank.canvas", from_node="a", to_node="b")
    assert result == "Invalid canvas format."
    assert vault.notes["Blank.canvas"]["text"] == ""
    assert_no_mutations(vault)


@pytest.mark.parametrize(
    "content",
    ['{"nodes": {"a": 1}, "edges": []}',
     '{"nodes": [], "edges": "x"}',
     '{"nodes": null}'],
)
async def test_canvas_non_list_fields_refused(vault, content):
    vault.add("M.canvas", content)
    calls = (
        lambda: server.obsidian_canvas_node("M.canvas", text="x"),
        lambda: server.obsidian_canvas_node(
            "M.canvas", action="remove", node_id="n1"),
        lambda: server.obsidian_canvas_edge(
            "M.canvas", from_node="a", to_node="b"),
        lambda: server.obsidian_canvas_edge(
            "M.canvas", action="remove", edge_id="e1"),
    )
    for call in calls:
        assert await call() == "Invalid canvas format."
    assert vault.notes["M.canvas"]["text"] == content
    assert_no_mutations(vault)


async def test_canvas_write_roundtrip(vault):
    original = {
        "schemaVersion": "1.0",
        "metadata": {"origin": "audit"},
        "nodes": [
            {"id": "keep", "type": "text", "text": "stay", "color": "#ff0000",
             "extra": [1, {"deep": True}]},
        ],
        "edges": [
            {"id": "e0", "fromNode": "keep", "toNode": "keep",
             "label": "loop"},
        ],
    }
    vault.add("RT.canvas", json.dumps(original))

    await server.obsidian_canvas_node("RT.canvas", text="temp", node_id="tmp")
    added = json.loads(vault.notes["RT.canvas"]["text"])
    assert added["schemaVersion"] == "1.0"
    assert added["metadata"] == {"origin": "audit"}
    assert added["nodes"][0] == original["nodes"][0]
    assert added["edges"] == original["edges"]

    result = await server.obsidian_canvas_node(
        "RT.canvas", action="remove", node_id="tmp")
    assert result == "Removed node 'tmp' from 'RT.canvas'."
    assert json.loads(vault.notes["RT.canvas"]["text"]) == original


async def test_canvas_remove_missing_reports_not_found(vault):
    vault.add("C.canvas", '{"nodes": [{"id": "n1"}], "edges": []}')
    result = await server.obsidian_canvas_node(
        "C.canvas", action="remove", node_id="nope")
    assert result == "Node 'nope' not found in 'C.canvas'."
    result = await server.obsidian_canvas_edge(
        "C.canvas", action="remove", edge_id="nope")
    assert result == "Edge 'nope' not found in 'C.canvas'."
    assert vault.notes["C.canvas"]["text"] == \
        '{"nodes": [{"id": "n1"}], "edges": []}'
    assert_no_mutations(vault)


async def test_canvas_remove_tolerates_entries_without_ids(vault):
    vault.add(
        "D.canvas",
        '{"nodes": [{"type": "group"}, {"id": "n1", "type": "text"}],'
        ' "edges": [{"fromNode": "n1"},'
        '           {"id": "e1", "fromNode": "n1", "toNode": "n2"}]}')
    result = await server.obsidian_canvas_node(
        "D.canvas", action="remove", node_id="n1")
    assert result == "Removed node 'n1' from 'D.canvas'."
    canvas = json.loads(vault.notes["D.canvas"]["text"])
    assert canvas["nodes"] == [{"type": "group"}]
    assert canvas["edges"] == []


async def test_canvas_duplicate_id_refused(vault):
    original = ('{"nodes": [{"id": "n1"}],'
                ' "edges": [{"id": "e1", "fromNode": "n1", "toNode": "n1"}]}')
    vault.add("E.canvas", original)
    result = await server.obsidian_canvas_node(
        "E.canvas", text="dup", node_id="n1")
    assert result == "Node 'n1' already exists in 'E.canvas'."
    result = await server.obsidian_canvas_edge(
        "E.canvas", from_node="n1", to_node="n1", edge_id="e1")
    assert result == "Edge 'e1' already exists in 'E.canvas'."
    assert vault.notes["E.canvas"]["text"] == original
    assert_no_mutations(vault)


async def test_canvas_edge_endpoints_validated(vault):
    vault.add("F.canvas", '{"nodes": [{"id": "n1"}], "edges": []}')
    result = await server.obsidian_canvas_edge(
        "F.canvas", from_node="n1", to_node="ghost")
    assert result == "Node 'ghost' not found in 'F.canvas'."
    result = await server.obsidian_canvas_edge(
        "F.canvas", from_node="ghost", to_node="n1")
    assert result == "Node 'ghost' not found in 'F.canvas'."
    assert vault.notes["F.canvas"]["text"] == \
        '{"nodes": [{"id": "n1"}], "edges": []}'
    assert_no_mutations(vault)
