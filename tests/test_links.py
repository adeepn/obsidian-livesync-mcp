"""Pure-text link extraction, resolution and graph regression tests."""

import pytest

from links import Link, build_graph, extract_links, resolve_link


@pytest.mark.parametrize("text", [
    "[[Note#Heading]]", "[[Note#^block]]", "[[Note|alias]]",
    "[x](Note.md)", "[[Note^block]]",
])
def test_extract_strips_anchors(text):
    assert extract_links(text)[0].target == "Note"


def test_embed_kind():
    assert extract_links("![[img.png]]") == [Link("img.png", "embed")]


def test_extract_kinds_and_order():
    assert extract_links("[[A]] [b](B.md#part) ![c](img.png)") == [
        Link("A", "wikilink"), Link("B", "markdown"),
        Link("img.png", "embed"),
    ]


def test_ignores_external_and_empty_targets():
    assert extract_links("[x](https://example.com) [[#Heading]] [x](#part)") == []


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_ignores_fenced_code_blocks(fence):
    text = f"[[Before]]\n{fence}python\n[[Hidden]] [x](Hidden.md)\n{fence}\n[[After]]"
    assert extract_links(text) == [Link("Before", "wikilink"),
                                   Link("After", "wikilink")]


def test_unclosed_fence():
    assert extract_links("```\n[[Hidden]]") == []


def test_resolve_by_basename():
    assert resolve_link("Hub", ["proj/Hub.md"]) == "proj/Hub.md"


@pytest.mark.parametrize("target", ["PROJ/HUB", "PROJ/HUB.MD"])
def test_exact_path_precedes_basename(target):
    assert resolve_link(target, ["other/Hub.md", "proj/Hub.md"]) == "proj/Hub.md"


def test_resolve_shortest_unique():
    paths = ["work/proj/Hub.md", "other/Hub.md"]
    assert resolve_link("proj/Hub", paths) == "work/proj/Hub.md"
    assert resolve_link("Hub", paths) is None


def test_ambiguous_and_missing_paths():
    paths = ["a/proj/Hub.md", "b/proj/Hub.md"]
    assert resolve_link("proj/Hub", paths) is None
    assert resolve_link("Missing", paths) is None
    assert resolve_link("", paths) is None
    assert resolve_link("roj/Hub", paths) is None


def test_attachment_extension_is_preserved():
    assert resolve_link("IMG.PNG", ["assets/img.png"]) == "assets/img.png"


def test_graph_resolves_deduplicates_and_drops_missing():
    texts = iter([
        ("Start.md", "[[Hub]] [[Hub#Part]] [x](proj/Hub.md) ![[Hub]] [[Missing]]"),
        ("proj/Hub.md", "[[Start]]"),
        ("Orphan.md", ""),
    ])
    assert build_graph(texts) == {
        "nodes": ["Start.md", "proj/Hub.md", "Orphan.md"],
        "edges": [{"from": "Start.md", "to": "proj/Hub.md"},
                  {"from": "proj/Hub.md", "to": "Start.md"}],
    }


def test_empty_graph():
    assert build_graph(iter([])) == {"nodes": [], "edges": []}


@pytest.mark.parametrize("prefix", [
    "[" * 10_000, "[x](" * 10_000 + "\n", "[x](<" * 10_000 + "\n",
])
def test_unmatched_brackets_are_bounded(prefix):
    from time import perf_counter

    start = perf_counter()
    links = extract_links(prefix + "[[Visible]]")
    elapsed = perf_counter() - start
    assert links == [Link("Visible", "wikilink")]
    assert elapsed < 1.0


@pytest.mark.parametrize("delimiter", ["`", "``", "```"])
def test_inline_code_does_not_create_links_or_edges(delimiter):
    text = (f"Before {delimiter}[[Hidden]] [x](Hidden.md){delimiter} "
            "[[Visible]]")
    assert extract_links(text) == [Link("Visible", "wikilink")]
    assert build_graph([
        ("Start.md", text), ("Hidden.md", ""), ("Visible.md", ""),
    ])["edges"] == [{"from": "Start.md", "to": "Visible.md"}]


def test_inline_code_requires_matching_delimiter_length():
    assert extract_links("``code ` [[Hidden]]`` [[Visible]]") == [
        Link("Visible", "wikilink"),
    ]
    assert extract_links("`unclosed [[Visible]]") == [
        Link("Visible", "wikilink"),
    ]


def test_nested_image_label_uses_outer_destination():
    assert extract_links("![caption [inner](Hidden.md)](Image.png)") == [
        Link("Image.png", "embed"),
    ]
