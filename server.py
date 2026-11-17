"""Obsidian MCP Server — remote MCP server for Obsidian via CouchDB/LiveSync."""

import json
import os
import re
import secrets
import time
import copy
import hashlib
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from config import Config, ConfigError, load_config
from storage_base import (
    StorageBackend, StorageError, NoteNotFound, NoteExists, BinaryNoteError,
    ConcurrentModification, UnsupportedVault, StorageUnavailable,
)
from storage_couchdb import CouchDBBackend
from frontmatter import (
    FrontmatterError, get_properties, split_frontmatter, set_properties,
    delete_properties, add_tags, remove_tags, insert_after_frontmatter,
)
from livesync_format import ForbiddenPathError
from links import build_graph, extract_links, resolve_link
from auth_config import StaticBearerTokenVerifier, select_auth_mode
from oauth_provider import SimpleOAuthProvider

# --- Configuration ---
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
OAUTH_PASSWORD = os.environ.get("OAUTH_PASSWORD", "")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
SERVER_URL = os.environ.get("MCP_SERVER_URL", "https://localhost:8484")

# --- Initialize MCP ---
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
try:
    MCP_PORT = int(os.environ.get("MCP_PORT", "8484"))
except ValueError:
    MCP_PORT = 8484  # load_config rejects the invalid value at startup.
try:
    AUTH_MODE = select_auth_mode(MCP_API_KEY, OAUTH_PASSWORD)
except RuntimeError:
    AUTH_MODE = None  # Import is safe; lifespan still fails closed.


def get_backend(cfg: Config) -> StorageBackend:
    """Construct the selected storage implementation."""
    if cfg.backend == "s3":
        raise RuntimeError("S3 backend arrives in Plan B")
    if cfg.couchdb is None:
        raise ConfigError("CouchDB configuration is required.")
    return CouchDBBackend(
        url=cfg.couchdb.url,
        username=cfg.couchdb.user,
        password=cfg.couchdb.password,
        database=cfg.couchdb.database,
    )


# Guard configuration at import so tools can be tested without credentials.
# Lifespan validates again before serving any requests.
backend: StorageBackend
try:
    backend = get_backend(load_config(os.environ))
except (ConfigError, RuntimeError):
    backend = None  # type: ignore[assignment]


@asynccontextmanager
async def _lifespan(app):
    global backend
    cfg = load_config(os.environ)
    if AUTH_MODE is None:
        raise ConfigError(
            "Authentication was absent at import; restart with configuration set."
        )
    if backend is None:
        backend = get_backend(cfg)
    try:
        await backend.startup_check()
        yield
    finally:
        await backend.close()


mcp_kwargs: dict[str, Any] = {
    "name": "Obsidian MCP Server",
    "instructions": "MCP server for Obsidian vault via CouchDB/LiveSync",
    "host": MCP_HOST,
    "lifespan": _lifespan,
    "port": MCP_PORT,
    "transport_security": TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
}

if AUTH_MODE == "bearer":
    mcp_kwargs["token_verifier"] = StaticBearerTokenVerifier(MCP_API_KEY)
    mcp_kwargs["auth"] = AuthSettings(
        issuer_url=AnyHttpUrl(SERVER_URL),
        resource_server_url=AnyHttpUrl(f"{SERVER_URL}/mcp"),
        required_scopes=["obsidian"],
    )
elif AUTH_MODE == "oauth":
    oauth_provider = SimpleOAuthProvider(
        server_url=SERVER_URL,
        access_password=OAUTH_PASSWORD,
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
    )
    mcp_kwargs["auth_server_provider"] = oauth_provider
    mcp_kwargs["auth"] = AuthSettings(
        issuer_url=AnyHttpUrl(SERVER_URL),
        resource_server_url=AnyHttpUrl(f"{SERVER_URL}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=False,
            valid_scopes=["claudeai"],
            default_scopes=["claudeai"],
        ),
    )

mcp = FastMCP(**mcp_kwargs)

# --- Helper functions ---

def _storage_message(error: StorageError) -> str:
    """Never forward backend exception text (URLs/revisions) to clients."""
    messages = {
        NoteNotFound: "Note not found.",
        NoteExists: "Error: note already exists.",
        BinaryNoteError: "Error: this operation requires a text note.",
        ConcurrentModification: (
            "Error: note changed concurrently; read it again and retry."
        ),
        UnsupportedVault: "Error: unsupported vault configuration.",
        StorageUnavailable: "Error: storage is unavailable; try again later.",
    }
    for kind, message in messages.items():
        if isinstance(error, kind):
            return message
    return "Error: unable to access vault storage."


def _tool_errors(handler):
    @wraps(handler)
    async def guarded(*args, **kwargs):
        if backend is None:
            return "Error: server storage configuration is missing or invalid."
        try:
            return await handler(*args, **kwargs)
        except StorageError as error:
            return _storage_message(error)
        except ForbiddenPathError:
            return "Error: forbidden vault path."
        except FrontmatterError:
            return "Error: invalid YAML frontmatter."
    return guarded


# Compatibility readers for canvas tools and shared text validation.
# These deliberately reference the module backend, never a captured client.
async def _legacy_content(path: str) -> str | None:
    note = await backend.read_note(path)
    if note is None:
        return None
    if not note["is_text"]:
        raise BinaryNoteError()
    return note["text"]


async def _graph() -> dict:
    paths = [meta["path"] for meta in await backend.list_notes()]
    texts, skipped = [], []
    async for meta, text in backend.iterate_texts():
        if text is None:
            skipped.append(meta["path"])
        else:
            texts.append((meta["path"], text))
    return {**build_graph(texts, all_paths=paths), "skipped": skipped}


async def _tags() -> dict:
    counts, skipped, errors = {}, [], {}
    async for meta, text in backend.iterate_texts():
        path = meta["path"]
        if text is None:
            skipped.append(path)
            continue
        try:
            props = get_properties(text)
            _, body = split_frontmatter(text)
        except FrontmatterError:
            skipped.append(path)
            errors[path] = "Error: invalid YAML frontmatter."
            continue
        tags = props.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        elif not isinstance(tags, list):
            tags = []
        for tag in tags + re.findall(r'(?<!\w)#([\w/\-]+)', body):
            if isinstance(tag, str) and tag:
                counts[tag] = counts.get(tag, 0) + 1
    result = {
        "tags": dict(sorted(counts.items(), key=lambda item: item[1],
                            reverse=True)),
        "skipped": skipped,
    }
    if errors:
        result["errors"] = errors
    return result


# === TOOLS ===

# 1. obsidian-search
@mcp.tool()
@_tool_errors
async def obsidian_search(query: str, regex: bool = False) -> str:
    """Search notes in the vault by content.

    Args:
        query: Search query (text or regex pattern)
        regex: If true, treat query as regex pattern
    """
    try:
        pattern = re.compile(query, re.IGNORECASE) if regex else None
    except re.error:
        return "Invalid regular expression."
    results, skipped = [], []
    async for meta, text in backend.iterate_texts():
        if text is None:
            skipped.append(meta["path"])
            continue
        if pattern is not None:
            matches = pattern.findall(text)
            if matches:
                results.append({"path": meta["path"], "matches": matches[:10]})
        elif query.lower() in text.lower():
            index = text.lower().index(query.lower())
            results.append({
                "path": meta["path"],
                "context": text[max(0, index - 50):index + len(query) + 50],
            })
    return json.dumps(
        {"results": results, "skipped": skipped}, ensure_ascii=False, indent=2
    )


# 2. obsidian-fetch
@mcp.tool()
@_tool_errors
async def obsidian_fetch(path: str, include_properties: bool = True, include_stats: bool = False) -> str:
    """Fetch the content and metadata of a note, base, or canvas file.

    Args:
        path: Path to the file (e.g. "My Note.md", "DB.base", "Board.canvas")
        include_properties: Include frontmatter properties in response
        include_stats: Include word/character count statistics
    """
    note = await backend.read_note(path)
    if note is None:
        return f"Note '{path}' not found."
    meta = note["meta"]
    result = {
        key: meta.get(key)
        for key in ("path", "ctime", "mtime", "type", "size")
    }
    if not note["is_text"]:
        data = note["data"]
        if data is None:
            raise StorageError()
        result.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    else:
        content = note["text"]
        if content is None:
            raise StorageError()
        result["content"] = content
        if include_properties:
            result["properties"] = (
                get_properties(content) if path.endswith(".md") else {}
            )
        if include_stats:
            text = (
                split_frontmatter(content)[1]
                if path.endswith(".md") else content
            )
            result["stats"] = {
                "words": len(text.split()), "characters": len(text),
                "lines": text.count("\n") + 1, "links": len(extract_links(text)),
            }
    return json.dumps(result, ensure_ascii=False, indent=2)


# 3. obsidian-create-note
@mcp.tool()
@_tool_errors
async def obsidian_create_note(
    path: str,
    content: str = "",
    properties: str = "",
) -> str:
    """Create a new note (.md) or base (.base) file.

    Args:
        path: File path (e.g. "folder/My Note.md", "Tasks.base")
        content: Note content (markdown for .md, YAML for .base)
        properties: JSON string of frontmatter properties (for .md files)
    """
    full_content = content
    if properties:
        if not path.lower().endswith(".md"):
            return "Error: properties require a .md file."
        try:
            props = json.loads(properties)
        except json.JSONDecodeError:
            return "Error: invalid JSON in properties parameter."
        if not isinstance(props, dict):
            return "Error: properties must be a JSON object."
        full_content = set_properties(content, props)
    await backend.write_note(path, full_content, create=True)
    return f"Created '{path}'."


# 4. obsidian-update-note
@mcp.tool()
@_tool_errors
async def obsidian_update_note(
    path: str,
    mode: str,
    content: str = "",
    search: str = "",
    replace: str = "",
    heading: str = "",
    regex: bool = False,
) -> str:
    """Update a note's content.

    Args:
        path: Path to the note
        mode: Required: overwrite, append, prepend, after-heading, search-replace
        content: New content (for overwrite/append/prepend/after-heading modes)
        search: Search string (for search-replace mode)
        replace: Replacement string (for search-replace mode)
        heading: Heading text to insert after (for after-heading mode)
        regex: Use regex for search-replace
    """
    modes = "overwrite, append, prepend, after-heading, search-replace"
    if mode not in modes.split(", "):
        return f"Error: unknown mode: {mode}. Use: {modes}"
    if mode == "overwrite":
        if not content:
            return "Error: empty overwrite content is refused."
        if search or replace or heading or regex:
            return "Error: search, replace, heading and regex require their own mode."
    existing = await _legacy_content(path)
    if existing is None:
        return f"Note '{path}' not found."
    matches = None
    if mode == "overwrite":
        new_content = content
    elif mode == "append":
        new_content = existing + "\n" + content
    elif mode == "prepend":
        new_content = insert_after_frontmatter(existing, content + "\n")
    elif mode == "after-heading":
        wanted = heading.lstrip("#").strip().casefold()
        if not wanted:
            return "Error: heading parameter required for after-heading mode."
        _, prose = split_frontmatter(existing)
        offset = len(existing) - len(prose)
        fence_char = ""
        fence_length = 0
        for line in prose.splitlines(keepends=True):
            fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
            if fence_char:
                if (fence and fence[1][0] == fence_char
                        and len(fence[1]) >= fence_length
                        and not fence[2].strip()):
                    fence_char = ""
            elif fence:
                fence_char, fence_length = fence[1][0], len(fence[1])
            else:
                match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
                if match and re.sub(r"\s+#+$", "", match[1]).casefold() == wanted:
                    end = offset + len(line)
                    separator = "" if line.endswith("\n") else "\n"
                    new_content = (
                        existing[:end] + separator + content + "\n"
                        + existing[end:]
                    )
                    break
            offset += len(line)
        else:
            return f"Error: heading '{heading}' not found in '{path}'."
    else:
        if not search:
            return "Error: search parameter required for search-replace mode."
        try:
            pattern = search if regex else re.escape(search)
            new_content, matches = re.subn(pattern, lambda match: replace, existing)
        except re.error:
            return "Error: invalid regex search pattern."
        if not matches:
            return "No matches found for search string."
    await backend.write_note(path, new_content)
    if matches is not None:
        return f"Updated '{path}': {matches} matches replaced."
    return f"Updated '{path}' ({mode})."


# 5. obsidian-delete-note
@mcp.tool()
@_tool_errors
async def obsidian_delete_note(path: str) -> str:
    """Delete a note from the vault.

    Args:
        path: Path to the note to delete
    """
    doc_id = path
    success = await backend.delete_note(doc_id)
    if success:
        return f"Deleted '{path}'."
    return f"Note '{path}' not found."


# 6. obsidian-move-note
@mcp.tool()
@_tool_errors
async def obsidian_move_note(path: str, new_path: str) -> str:
    """Move or rename a note.

    Args:
        path: Current path of the note
        new_path: New path for the note
    """
    await backend.rename_note(path, new_path)
    return f"Moved '{path}' -> '{new_path}'."


# 7. obsidian-duplicate-note
@mcp.tool()
@_tool_errors
async def obsidian_duplicate_note(path: str, new_path: str = "") -> str:
    """Duplicate a note.

    Args:
        path: Path to the note to duplicate
        new_path: Path for the copy (default: adds ' copy' before extension)
    """
    if not new_path:
        base, ext = os.path.splitext(path)
        new_path = f"{base} copy{ext}"
    await backend.copy_note(path, new_path)
    return f"Duplicated '{path}' -> '{new_path}'."


# 8. obsidian-list-notes
@mcp.tool()
@_tool_errors
async def obsidian_list_notes(
    folder: str = "",
    extension: str = "",
) -> str:
    """List notes in the vault, optionally filtered by folder and extension.

    Args:
        folder: Folder prefix (e.g. "projects/"); includes descendants recursively.
            Empty lists the entire vault.
        extension: Filter by extension (e.g. ".md", ".base", ".canvas")
    """
    notes = await backend.list_notes(
        folder=folder, extension=extension or None, recursive=True
    )
    if not notes:
        return "No notes found."
    return json.dumps([
        {"path": n["path"], "size": n.get("size") or 0, "modified": n.get("mtime")}
        for n in notes
    ], ensure_ascii=False, indent=2)


# 9. obsidian-manage-properties
@mcp.tool()
@_tool_errors
async def obsidian_manage_properties(
    path: str,
    action: str = "get",
    properties: str = "",
    keys: str = "",
) -> str:
    """Manage frontmatter properties of a note.

    Args:
        path: Path to the .md note
        action: One of: get, set, delete
        properties: JSON string of properties to set (for 'set' action)
        keys: Comma-separated property keys to delete (for 'delete' action)
    """
    content = await _legacy_content(path)
    if content is None:
        return f"Note '{path}' not found."
    if not path.lower().endswith(".md"):
        return "Error: properties require a .md file."
    if action == "get":
        return json.dumps(get_properties(content), ensure_ascii=False, indent=2)
    if action == "set":
        try:
            props = json.loads(properties)
        except json.JSONDecodeError:
            return "Error: invalid JSON in properties parameter."
        if not isinstance(props, dict):
            return "Error: properties must be a JSON object."
        new_content = set_properties(content, props)
    elif action == "delete":
        if not keys:
            return "Error: keys parameter required for delete."
        new_content = delete_properties(
            content, [key.strip() for key in keys.split(",")]
        )
    else:
        return f"Error: unknown action: {action}. Use: get, set, delete"
    if new_content == content:
        return f"No property changes in '{path}'."
    await backend.write_note(path, new_content)
    return f"Updated properties of '{path}'."


# 10. obsidian-manage-tags
@mcp.tool()
@_tool_errors
async def obsidian_manage_tags(
    path: str,
    action: str = "list",
    tags: str = "",
) -> str:
    """Manage frontmatter tags and report inline tags without editing them.

    Args:
        path: Path to the note
        action: One of: list, add, remove
        tags: Comma-separated tags (without #) for add/remove
    """
    content = await _legacy_content(path)
    if content is None:
        return f"Note '{path}' not found."
    if not path.lower().endswith(".md"):
        return "Error: tags require a .md file."
    props = get_properties(content)
    _, body = split_frontmatter(content)
    value = props.get("tags", [])
    fm_tags = [] if value is None else [str(tag) for tag in (
        value if isinstance(value, list) else [value])]
    if action == "list":
        inline_tags = re.findall(r'(?<!\w)#([\w/\-]+)', body)
        return json.dumps({
            "frontmatter_tags": fm_tags,
            "inline_tags": sorted(set(inline_tags)),
        }, ensure_ascii=False, indent=2)
    tag_list = [
        tag.strip().lstrip("#") for tag in tags.split(",")
        if tag.strip().lstrip("#")
    ]
    if action not in ("add", "remove"):
        return f"Error: unknown action: {action}. Use: list, add, remove"
    if not tag_list:
        return "Error: tags parameter required."
    new_content = (add_tags if action == "add" else remove_tags)(content, tag_list)
    if new_content == content:
        return f"No frontmatter tag changes in '{path}'; inline tags untouched."
    await backend.write_note(path, new_content)
    return f"Updated frontmatter tags in '{path}'; inline tags untouched."


# 11. obsidian-get-all-tags
@mcp.tool()
@_tool_errors
async def obsidian_get_all_tags() -> str:
    """Get all tags across the vault with usage counts."""
    return json.dumps(await _tags(), ensure_ascii=False, indent=2)


# 12. obsidian-get-links
@mcp.tool()
@_tool_errors
async def obsidian_get_links(
    path: str,
    direction: str = "both",
) -> str:
    """Get links for a note.

    Args:
        path: Path to the note
        direction: One of: out (outgoing), in (backlinks), both
    """
    if direction not in ("out", "in", "both"):
        return "Invalid direction. Use: out, in, both."
    graph = await _graph()
    resolved = resolve_link(path, graph["nodes"])
    result = {"path": resolved or path, "skipped": graph["skipped"]}
    if direction in ("out", "both"):
        result["outgoing"] = [
            e["to"] for e in graph["edges"] if e["from"] == resolved
        ]
    if direction in ("in", "both"):
        result["backlinks"] = [
            {"path": e["from"]} for e in graph["edges"] if e["to"] == resolved
        ]
    return json.dumps(result, ensure_ascii=False, indent=2)


# 13. obsidian-get-link-graph
@mcp.tool()
@_tool_errors
async def obsidian_get_link_graph() -> str:
    """Get the full link graph of the vault (all nodes and edges)."""
    return json.dumps(await _graph(), ensure_ascii=False, indent=2)


# 14. obsidian-find-orphaned-notes
@mcp.tool()
@_tool_errors
async def obsidian_find_orphaned_notes() -> str:
    """Find notes that have no incoming or outgoing links."""
    graph = await _graph()
    connected = set()
    for edge in graph["edges"]:
        connected.add(edge["from"])
        connected.add(edge["to"])
    # Unreadable bodies may contain outgoing links, so cannot prove orphanhood.
    skipped = set(graph["skipped"])
    orphans = [n for n in graph["nodes"] if n not in connected and n not in skipped]
    if graph["skipped"]:
        return json.dumps(
            {"orphans": orphans, "skipped": graph["skipped"]},
            ensure_ascii=False, indent=2,
        )
    if not orphans:
        return "No orphaned notes found."
    return json.dumps(orphans, ensure_ascii=False, indent=2)


# 15. obsidian-find-hub-notes
@mcp.tool()
@_tool_errors
async def obsidian_find_hub_notes(min_links: int = 3) -> str:
    """Find the most connected notes in the vault.

    Args:
        min_links: Minimum total links (in + out) to be considered a hub
    """
    graph = await _graph()
    link_counts: dict[str, dict[str, int]] = {}
    for node in graph["nodes"]:
        link_counts[node] = {"in": 0, "out": 0}
    for edge in graph["edges"]:
        src = edge["from"]
        tgt = edge["to"]
        if src in link_counts:
            link_counts[src]["out"] += 1
        if tgt in link_counts:
            link_counts[tgt]["in"] += 1
    hubs = []
    for node, counts in link_counts.items():
        total = counts["in"] + counts["out"]
        if total >= min_links:
            hubs.append({"path": node, "incoming": counts["in"], "outgoing": counts["out"], "total": total})
    hubs.sort(key=lambda x: x["total"], reverse=True)
    if graph["skipped"]:
        return json.dumps(
            {"hubs": hubs, "skipped": graph["skipped"]},
            ensure_ascii=False, indent=2,
        )
    if not hubs:
        return f"No notes with {min_links}+ links found."
    return json.dumps(hubs, ensure_ascii=False, indent=2)


# 16. obsidian-canvas-node
@mcp.tool()
@_tool_errors
async def obsidian_canvas_node(
    path: str,
    action: str = "add",
    node_type: str = "text",
    text: str = "",
    file: str = "",
    url: str = "",
    x: int = 0,
    y: int = 0,
    width: int = 250,
    height: int = 60,
    node_id: str = "",
) -> str:
    """Add or remove a node in a canvas file.

    Args:
        path: Path to the .canvas file
        action: "add" or "remove"
        node_type: Type of node: text, file, link, group
        text: Text content (for text/group nodes)
        file: File path (for file nodes)
        url: URL (for link nodes)
        x: X position
        y: Y position
        width: Node width
        height: Node height
        node_id: Node ID (required for remove)
    """
    doc_id = path
    content = await _legacy_content(doc_id)
    if content is None and action != "add":
        return f"Canvas '{path}' not found."

    try:
        canvas = json.loads(content) if content else {"nodes": [], "edges": []}
    except json.JSONDecodeError:
        return "Invalid canvas format."

    if action == "add":
        new_id = node_id or secrets.token_hex(8)
        node: dict[str, Any] = {
            "id": new_id,
            "type": node_type,
            "x": x, "y": y,
            "width": width, "height": height,
        }
        if node_type == "text":
            node["text"] = text
        elif node_type == "file":
            node["file"] = file
        elif node_type == "link":
            node["url"] = url
        elif node_type == "group":
            node["label"] = text
        canvas["nodes"].append(node)
        # Task 10: retain add-to-existing behavior under the new create contract.
        await backend.write_note(
            path, json.dumps(canvas, ensure_ascii=False), create=content is None
        )
        return f"Added {node_type} node '{new_id}' to '{path}'."

    elif action == "remove":
        if not node_id:
            return "node_id required for remove action."
        canvas["nodes"] = [n for n in canvas["nodes"] if n["id"] != node_id]
        canvas["edges"] = [e for e in canvas["edges"] if e.get("fromNode") != node_id and e.get("toNode") != node_id]
        await backend.write_note(path, json.dumps(canvas, ensure_ascii=False))
        return f"Removed node '{node_id}' from '{path}'."

    return f"Unknown action: {action}. Use: add, remove"


# 17. obsidian-canvas-edge
@mcp.tool()
@_tool_errors
async def obsidian_canvas_edge(
    path: str,
    action: str = "add",
    from_node: str = "",
    to_node: str = "",
    label: str = "",
    edge_id: str = "",
) -> str:
    """Add or remove an edge in a canvas file.

    Args:
        path: Path to the .canvas file
        action: "add" or "remove"
        from_node: Source node ID (for add)
        to_node: Target node ID (for add)
        label: Edge label (optional)
        edge_id: Edge ID (required for remove)
    """
    doc_id = path
    content = await _legacy_content(doc_id)
    if content is None:
        return f"Canvas '{path}' not found."

    try:
        canvas = json.loads(content)
    except json.JSONDecodeError:
        return "Invalid canvas format."

    if action == "add":
        if not from_node or not to_node:
            return "from_node and to_node required for add action."
        new_id = edge_id or secrets.token_hex(8)
        edge: dict[str, Any] = {
            "id": new_id,
            "fromNode": from_node,
            "toNode": to_node,
        }
        if label:
            edge["label"] = label
        canvas["edges"].append(edge)
        await backend.write_note(path, json.dumps(canvas, ensure_ascii=False))
        return f"Added edge '{new_id}' ({from_node} -> {to_node}) to '{path}'."

    elif action == "remove":
        if not edge_id:
            return "edge_id required for remove action."
        canvas["edges"] = [e for e in canvas["edges"] if e["id"] != edge_id]
        await backend.write_note(path, json.dumps(canvas, ensure_ascii=False))
        return f"Removed edge '{edge_id}' from '{path}'."

    return f"Unknown action: {action}. Use: add, remove"


# 18. obsidian-vault-stats
@mcp.tool()
@_tool_errors
async def obsidian_vault_stats() -> str:
    """Get aggregate statistics for the entire vault."""
    notes = await backend.list_notes()
    by_ext = {}
    for note in notes:
        path = note["path"]
        ext = path.rsplit(".", 1)[-1] if "." in path else "unknown"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    tags = await _tags()
    info = await backend.backend_info()
    return json.dumps({
        "total_notes": len(notes), "by_extension": by_ext,
        "total_size_bytes": sum(n.get("size") or 0 for n in notes),
        "total_tags": len(tags["tags"]), "backend": info.get("backend"),
        "skipped": tags["skipped"],
        **({"errors": tags["errors"]} if "errors" in tags else {}),
    }, ensure_ascii=False, indent=2)


# 19. obsidian-create-from-template
@mcp.tool()
@_tool_errors
async def obsidian_create_from_template(
    template_path: str,
    new_path: str,
    variables: str = "",
) -> str:
    """Create a new note from a template, replacing {{variables}}.

    Args:
        template_path: Path to the template note
        new_path: Path for the new note
        variables: JSON object whose values are rendered as JSON scalars.
    """
    content = await _legacy_content(template_path)
    if content is None:
        return f"Template '{template_path}' not found."
    if variables:
        try:
            replacements = json.loads(variables)
        except json.JSONDecodeError:
            return "Error: invalid JSON in variables parameter."
        if not isinstance(replacements, dict):
            return "Error: template variables must be a JSON object."
        if any(isinstance(value, (dict, list)) or value is None
               for value in replacements.values()):
            return "Error: template values must be string, number or boolean JSON scalars."
        content = re.sub(
            r"\{\{\s*([^{}]*?)\s*\}\}",
            lambda match: json.dumps(replacements[match[1]], ensure_ascii=False)
            if match[1] in replacements else match[0], content,
        )
    await backend.write_note(new_path, content, create=True)
    return f"Created '{new_path}' from template '{template_path}'."


# 20. obsidian-list-templates
@mcp.tool()
@_tool_errors
async def obsidian_list_templates(folder: str = "templates") -> str:
    """List available templates in the templates folder.

    Args:
        folder: Templates folder path (default: "templates")
    """
    notes = await backend.list_notes(folder=folder)
    if not notes:
        return f"No templates found in '{folder}/'."
    return json.dumps(
        [{"path": n["path"], "size": n["size"]} for n in notes],
        ensure_ascii=False, indent=2,
    )


@mcp.tool()
@_tool_errors
async def obsidian_refresh() -> str:
    """Refresh the backend's local vault representation."""
    await backend.refresh()
    return "Vault refreshed."


# --- Main ---
if __name__ == "__main__":
    mcp.run(transport="sse")
