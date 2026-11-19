"""Obsidian MCP Server — remote MCP server for Obsidian via CouchDB/LiveSync."""

import json
import asyncio
import regex_runner
import logging
import os
import re
import secrets
import sys
import time
import copy
import hashlib
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from scan_budget import ScanBudget
from functools import wraps
from pathlib import Path
from typing import Any

# httpx/httpcore log every request line — full internal CouchDB URLs —
# at INFO. Pin them to WARNING so server logs never carry those URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from config import Config, ConfigError, load_config
from storage_base import (
    RenamePartialError, UnresolvedConflicts,
    StorageBackend, StorageError, NoteNotFound, NoteExists, BinaryNoteError,
    ConcurrentModification, UnsupportedVault, StorageUnavailable, NoteContent,
)
from storage_couchdb import CouchDBBackend
from storage_journal import JournalBackend
from object_storage import Boto3ObjectStorage
from frontmatter import (
    FrontmatterError, get_properties, split_frontmatter, set_properties,
    delete_properties, add_tags, remove_tags, insert_after_frontmatter,
)
from livesync_format import ForbiddenPathError
from links import GraphBuilder, extract_links, resolve_link
from auth_config import StaticBearerTokenVerifier, select_auth_mode
from oauth_provider import SimpleOAuthProvider

def get_backend(cfg: Config) -> StorageBackend:
    """Construct the selected storage implementation."""
    if cfg.backend == "s3":
        if cfg.s3 is None:
            raise ConfigError("S3 configuration is required.")
        return JournalBackend(
            Boto3ObjectStorage(cfg.s3),
            Path(cfg.s3.state_dir),
            cfg.s3.refresh_min_interval_sec,
        )
    if cfg.couchdb is None:
        raise ConfigError("CouchDB configuration is required.")
    return CouchDBBackend(
        url=cfg.couchdb.url,
        username=cfg.couchdb.user,
        password=cfg.couchdb.password,
        database=cfg.couchdb.database,
        scan_max_notes=cfg.scan_max_notes,
        scan_max_bytes=cfg.scan_max_bytes,
    )


# Direct Python callers use the module backend; registered tools bind their
# app's backend per call, so separate factory instances cannot cross vaults.
backend: StorageBackend | None = None
_active_backend: ContextVar[StorageBackend | None] = ContextVar("active_backend")
_tool_handlers = []
_active_scan_limits = ContextVar("active_scan_limits", default=(10000, 64 * 1024 * 1024))


def _backend() -> StorageBackend | None:
    return _active_backend.get(backend)


def _tool(handler):
    _tool_handlers.append(handler)
    return handler


def create_app(cfg: Config) -> FastMCP:
    """Build auth, transport and backend startup from one validated config.

    Configuration is immutable for the lifetime of this application. Build
    another application to consume changed configuration.
    """
    state = {"backend": None}

    @asynccontextmanager
    async def lifespan(app):
        global backend
        startup_backend = get_backend(cfg)
        state["backend"] = startup_backend
        is_default = app is globals().get("mcp")
        if is_default:
            backend = startup_backend
        try:
            await startup_backend.startup_check()
            yield
        finally:
            try:
                await startup_backend.close()
            finally:
                state["backend"] = None
                if is_default:
                    backend = None

    kwargs: dict[str, Any] = {
        "name": "Obsidian MCP Server",
        "instructions": "MCP server for Obsidian vault via CouchDB/LiveSync",
        "host": cfg.host,
        "port": cfg.port,
        "lifespan": lifespan,
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    }
    mode = select_auth_mode(cfg.api_key, cfg.oauth_password)
    if mode == "bearer":
        kwargs["token_verifier"] = StaticBearerTokenVerifier(
            cfg.api_key, resource_server_url=f"{cfg.server_url}/mcp"
        )
        kwargs["auth"] = AuthSettings(
            validate_token_resource=True,
            issuer_url=AnyHttpUrl(cfg.server_url),
            resource_server_url=AnyHttpUrl(f"{cfg.server_url}/mcp"),
            required_scopes=["obsidian"],
        )
    else:
        kwargs["auth_server_provider"] = SimpleOAuthProvider(
            server_url=cfg.server_url,
            access_password=cfg.oauth_password,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
        )
        kwargs["auth"] = AuthSettings(
            validate_token_resource=True,
            issuer_url=AnyHttpUrl(cfg.server_url),
            resource_server_url=AnyHttpUrl(f"{cfg.server_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=False,
                valid_scopes=["claudeai"],
                default_scopes=["claudeai"],
            ),
        )
    app = FastMCP(**kwargs)

    def bind(handler):
        @wraps(handler)
        async def bound(*args, **kwargs):
            token = _active_backend.set(state["backend"])
            limits_token = _active_scan_limits.set((cfg.scan_max_notes, cfg.scan_max_bytes))
            try:
                return await handler(*args, **kwargs)
            finally:
                _active_backend.reset(token)
                _active_scan_limits.reset(limits_token)
        return bound

    for handler in _tool_handlers:
        app.add_tool(bind(handler))
    return app


# --- Helper functions ---

def _storage_message(error: StorageError) -> str:
    """Never forward backend exception text (URLs/revisions) to clients."""
    if isinstance(error, RenamePartialError):
        if error.source_state != "live":
            return (
                f"Error: move target '{error.destination}' was written, but "
                f"retirement of source '{error.source}' could not be confirmed. "
                "Verify both paths and their contents before cleanup."
            )
        return (
            f"Error: move target was written, but source retirement failed; "
            f"the note now exists at both '{error.source}' and "
            f"'{error.destination}'. Verify both paths and their contents first. "
            "Delete one copy, or retry the move "
            "after removing the destination."
        )
    messages = {
        UnresolvedConflicts: (
            "Error: note has unresolved sibling conflicts; resolve them in "
            "Obsidian (or another client) before writing."
        ),
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
        if _backend() is None:
            return "Error: server not started; storage backend is unavailable."
        try:
            await _backend().refresh()
            return await handler(*args, **kwargs)
        except regex_runner.RegexTimeoutError:
            return "Error: regex pattern too complex or input too large."
        except StorageError as error:
            return _storage_message(error)
        except ForbiddenPathError:
            return "Error: forbidden vault path."
        except FrontmatterError:
            return "Error: invalid YAML frontmatter."
    return guarded


# Compatibility readers for canvas tools and shared text validation.
# These deliberately reference the module backend, never a captured client.
async def _read_text_note(path: str) -> NoteContent | None:
    """Textual note content together with the revision it was read at.

    The revision flows back into ``write_note(expected_rev=...)`` so a
    device edit landing between the tool's read and write surfaces as a
    ConcurrentModification error instead of being silently overwritten.
    """
    note = await _backend().read_note(path)
    if note is None:
        return None
    if not note["is_text"]:
        raise BinaryNoteError()
    return note


async def _legacy_content(path: str) -> str | None:
    note = await _read_text_note(path)
    return None if note is None else note["text"]


def _load_canvas(content: str | None) -> dict | None:
    """Parse a JSON Canvas document for in-place mutation.

    Returns ``None`` when the payload is unusable: not JSON (an
    existing-but-empty file included), not an object, or ``nodes``/
    ``edges`` present with a non-list value — callers must then refuse
    without mutating. ``content is None`` (missing file) yields an empty
    document. Spec-valid files without ``nodes``/``edges`` keys
    (e.g. ``{}``) get empty lists (audit D16), so callers load, mutate,
    and dump the original document instead of rebuilding it.
    """
    if content is None:
        canvas = {}
    else:
        try:
            canvas = json.loads(content)
        except json.JSONDecodeError:
            return None
    if not isinstance(canvas, dict):
        return None
    for key in ("nodes", "edges"):
        if key in canvas and not isinstance(canvas[key], list):
            return None
        canvas.setdefault(key, [])
    return canvas


def _entry_id(entry: Any) -> Any:
    """ID of a canvas node/edge entry; non-object entries have none."""
    return entry.get("id") if isinstance(entry, dict) else None


_scan_semaphore = asyncio.Semaphore(2)
_scan_budget = ContextVar("scan_budget", default=None)


def _vault_scan(handler):
    @wraps(handler)
    async def scanned(*args, **kwargs):
        async with _scan_semaphore:
            budget = ScanBudget(*_active_scan_limits.get())
            token = _scan_budget.set(budget)
            try:
                response = await handler(*args, **kwargs)
                if getattr(_backend(), "index_truncated", False):
                    budget.truncated = True
                if budget.truncated:
                    try:
                        result = json.loads(response)
                    except (ValueError, TypeError):
                        result = {"message": response}
                    if not isinstance(result, dict):
                        result = {"results": result}
                    if (handler.__name__ in ("obsidian_list_notes", "obsidian_list_templates")
                            and not budget.paths):
                        result = {
                            "results": [],
                            "message": "Results truncated; narrow the folder/extension filter.",
                        }
                    result.update(truncated=True, scan=budget.report())
                    if hasattr(budget, "total"):
                        result.update(returned=len(result["results"]) if "results" in result
                                      else result.get("total_notes", len(budget.paths)),
                                      total=budget.total)
                        if getattr(_backend(), "index_truncated", False):
                            result["total_is_lower_bound"] = True
                    return json.dumps(result, ensure_ascii=False, indent=2)
                return response
            finally:
                _scan_budget.reset(token)
    return scanned


async def _scan_notes(**filters):
    result = []
    budget = _scan_budget.get()
    notes = await _backend().list_notes(**filters)
    if budget:
        budget.total = len(notes)
    for meta in notes:
        if budget and not budget.admit(meta["path"], len(json.dumps(meta).encode())):
            break
        result.append(meta)
    return result


async def _scan_texts():
    budget = _scan_budget.get()
    async with aclosing(_backend().iterate_texts()) as texts:
        async for meta, text in texts:
            size = len(meta["path"].encode()) + (len(text.encode()) if text else 0)
            if budget and not budget.admit(meta["path"], size):
                break
            yield meta, text
            del text



async def _graph() -> dict:
    paths = [meta["path"] for meta in await _scan_notes()]
    graph = GraphBuilder(paths)
    processed = set()
    skipped = []
    async with aclosing(_scan_texts()) as texts:
        async for meta, text in texts:
            processed.add(meta["path"])
            if text is None:
                skipped.append(meta["path"])
            else:
                graph.add(meta["path"], text)
            del text
    skipped.extend(path for path in paths if path not in processed)
    return {**graph.result(), "skipped": skipped}


async def _tags() -> dict:
    counts, skipped, errors = {}, [], {}
    async with aclosing(_scan_texts()) as texts:
        async for meta, text in texts:
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
@_tool
@_tool_errors
@_vault_scan
async def obsidian_search(query: str, regex: bool = False) -> str:
    """Search notes in the vault by content.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

    Args:
        query: Search query (text or regex pattern)
        regex: If true, treat query as regex pattern
    """
    pattern = query if regex else None
    if regex:
        try:
            await asyncio.to_thread(regex_runner.apply, query, "", "findall")
        except re.error:
            return "Invalid regular expression."
    results, skipped = [], []
    async with aclosing(_scan_texts()) as texts:
        async for meta, text in texts:
            if text is None:
                skipped.append(meta["path"])
                continue
            if pattern is not None:
                try:
                    matches = (await asyncio.to_thread(
                        regex_runner.apply, pattern, text, "findall"))["matches"]
                except re.error:
                    return "Invalid regular expression."
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
@_tool
@_tool_errors
async def obsidian_fetch(path: str, include_properties: bool = True, include_stats: bool = False) -> str:
    """Fetch the content and metadata of a note, base, or canvas file.

    Args:
        path: Path to the file (e.g. "My Note.md", "DB.base", "Board.canvas")
        include_properties: Include frontmatter properties in response
        include_stats: Include word/character count statistics
    """
    note = await _backend().read_note(path)
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
@_tool
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
    await _backend().write_note(path, full_content, create=True)
    return f"Created '{path}'."


# 4. obsidian-update-note
@_tool
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
    """Update a note's content using an explicitly required mode.

    Overwrite refuses empty content and any search, replace, heading or regex
    options. Search-replace uses literal replacement text (no escape or group
    expansion), even with regex searches, and returns the number of matches
    replaced; zero matches are reported without writing. After-heading matches
    headings outside fenced code and YAML frontmatter.

    Args:
        path: Path to the note
        mode: Required: overwrite, append, prepend, after-heading, search-replace
        content: New content (for overwrite/append/prepend/after-heading modes)
        search: Search string (for search-replace mode)
        replace: Literal replacement string (for search-replace mode)
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
    note = await _read_text_note(path)
    if note is None:
        return f"Note '{path}' not found."
    existing = note["text"]
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
            if regex:
                result = await asyncio.to_thread(
                    regex_runner.apply, pattern, existing, "subn",
                    replacement=replace, flags=0)
                new_content, matches = result["result"], result["count"]
            else:
                new_content, matches = re.subn(pattern, lambda match: replace, existing)
        except re.error:
            return "Error: invalid regex search pattern."
        except regex_runner.RegexTimeoutError:
            raise
        except ValueError:
            return "Error: regex replacement output too large."
        if not matches:
            return "No matches found for search string."
    if new_content != existing:
        await _backend().write_note(path, new_content, expected_rev=note["rev"])
    if matches is not None:
        return f"Updated '{path}': {matches} matches replaced."
    return f"Updated '{path}' ({mode})."


# 5. obsidian-delete-note
@_tool
@_tool_errors
async def obsidian_delete_note(path: str) -> str:
    """Delete a note from the vault.

    Args:
        path: Path to the note to delete
    """
    doc_id = path
    success = await _backend().delete_note(doc_id)
    if success:
        return f"Deleted '{path}'."
    return f"Note '{path}' not found."


# 6. obsidian-move-note
@_tool
@_tool_errors
async def obsidian_move_note(path: str, new_path: str) -> str:
    """Move or rename a note.

    Args:
        path: Current path of the note
        new_path: New path for the note
    """
    await _backend().rename_note(path, new_path)
    return f"Moved '{path}' -> '{new_path}'."


# 7. obsidian-duplicate-note
@_tool
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
    await _backend().copy_note(path, new_path)
    return f"Duplicated '{path}' -> '{new_path}'."


# 8. obsidian-list-notes
@_tool
@_tool_errors
@_vault_scan
async def obsidian_list_notes(
    folder: str = "",
    extension: str = "",
) -> str:
    """List notes in the vault, optionally filtered by folder and extension.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

    Args:
        folder: Folder prefix (e.g. "projects/"); includes descendants recursively.
            Empty lists the entire vault.
        extension: Filter by extension (e.g. ".md", ".base", ".canvas")
    """
    notes = await _scan_notes(
        folder=folder, extension=extension or None, recursive=True
    )
    if not notes:
        return "No notes found."
    return json.dumps([
        {"path": n["path"], "size": n.get("size") or 0, "modified": n.get("mtime")}
        for n in notes
    ], ensure_ascii=False, indent=2)


# 9. obsidian-manage-properties
@_tool
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
    note = await _read_text_note(path)
    if note is None:
        return f"Note '{path}' not found."
    content = note["text"]
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
    await _backend().write_note(path, new_content, expected_rev=note["rev"])
    return f"Updated properties of '{path}'."


# 10. obsidian-manage-tags
@_tool
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
    note = await _read_text_note(path)
    if note is None:
        return f"Note '{path}' not found."
    content = note["text"]
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
    await _backend().write_note(path, new_content, expected_rev=note["rev"])
    return f"Updated frontmatter tags in '{path}'; inline tags untouched."


# 11. obsidian-get-all-tags
@_tool
@_tool_errors
@_vault_scan
async def obsidian_get_all_tags() -> str:
    """Get all tags across the vault with usage counts.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
    return json.dumps(await _tags(), ensure_ascii=False, indent=2)


# 12. obsidian-get-links
@_tool
@_tool_errors
@_vault_scan
async def obsidian_get_links(
    path: str,
    direction: str = "both",
) -> str:
    """Get links for a note.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

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
@_tool
@_tool_errors
@_vault_scan
async def obsidian_get_link_graph() -> str:
    """Get the full link graph of the vault (all nodes and edges).

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
    return json.dumps(await _graph(), ensure_ascii=False, indent=2)


# 14. obsidian-find-orphaned-notes
@_tool
@_tool_errors
@_vault_scan
async def obsidian_find_orphaned_notes() -> str:
    """Find notes that have no incoming or outgoing links.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
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
@_tool
@_tool_errors
@_vault_scan
async def obsidian_find_hub_notes(min_links: int = 3) -> str:
    """Find the most connected notes in the vault.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

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
@_tool
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
        node_id: Node ID (required for remove, must be unique for add)
    """
    note = await _read_text_note(path)
    content = None if note is None else note["text"]
    rev = None if note is None else note["rev"]
    if content is None and action != "add":
        return f"Canvas '{path}' not found."

    canvas = _load_canvas(content)
    if canvas is None:
        return "Invalid canvas format."

    if action == "add":
        if node_id and any(_entry_id(n) == node_id for n in canvas["nodes"]):
            return f"Node '{node_id}' already exists in '{path}'."
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
        # An existing canvas is an update guarded by the read revision; a
        # brand-new one is a plain create (no precondition).
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False),
            create=content is None, expected_rev=rev,
        )
        return f"Added {node_type} node '{new_id}' to '{path}'."

    elif action == "remove":
        if not node_id:
            return "node_id required for remove action."
        kept_nodes = [n for n in canvas["nodes"] if _entry_id(n) != node_id]
        if len(kept_nodes) == len(canvas["nodes"]):
            return f"Node '{node_id}' not found in '{path}'."
        canvas["nodes"] = kept_nodes
        canvas["edges"] = [
            e for e in canvas["edges"]
            if not (isinstance(e, dict)
                    and node_id in (e.get("fromNode"), e.get("toNode")))
        ]
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False), expected_rev=rev
        )
        return f"Removed node '{node_id}' from '{path}'."

    return f"Unknown action: {action}. Use: add, remove"


# 17. obsidian-canvas-edge
@_tool
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
        from_node: Source node ID (for add, must reference an existing node)
        to_node: Target node ID (for add, must reference an existing node)
        label: Edge label (optional)
        edge_id: Edge ID (required for remove, must be unique for add)
    """
    note = await _read_text_note(path)
    if note is None:
        return f"Canvas '{path}' not found."
    content = note["text"]

    canvas = _load_canvas(content)
    if canvas is None:
        return "Invalid canvas format."

    if action == "add":
        if not from_node or not to_node:
            return "from_node and to_node required for add action."
        known_nodes = {_entry_id(n) for n in canvas["nodes"]}
        for endpoint in (from_node, to_node):
            if endpoint not in known_nodes:
                return f"Node '{endpoint}' not found in '{path}'."
        if edge_id and any(_entry_id(e) == edge_id for e in canvas["edges"]):
            return f"Edge '{edge_id}' already exists in '{path}'."
        new_id = edge_id or secrets.token_hex(8)
        edge: dict[str, Any] = {
            "id": new_id,
            "fromNode": from_node,
            "toNode": to_node,
        }
        if label:
            edge["label"] = label
        canvas["edges"].append(edge)
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False),
            expected_rev=note["rev"],
        )
        return f"Added edge '{new_id}' ({from_node} -> {to_node}) to '{path}'."

    elif action == "remove":
        if not edge_id:
            return "edge_id required for remove action."
        kept_edges = [e for e in canvas["edges"] if _entry_id(e) != edge_id]
        if len(kept_edges) == len(canvas["edges"]):
            return f"Edge '{edge_id}' not found in '{path}'."
        canvas["edges"] = kept_edges
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False),
            expected_rev=note["rev"],
        )
        return f"Removed edge '{edge_id}' from '{path}'."

    return f"Unknown action: {action}. Use: add, remove"


# 18. obsidian-vault-stats
@_tool
@_tool_errors
@_vault_scan
async def obsidian_vault_stats() -> str:
    """Get aggregate statistics for the entire vault.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
    notes = await _scan_notes()
    by_ext = {}
    for note in notes:
        path = note["path"]
        ext = path.rsplit(".", 1)[-1] if "." in path else "unknown"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    tags = await _tags()
    info = await _backend().backend_info()
    return json.dumps({
        "total_notes": len(notes), "by_extension": by_ext,
        "total_size_bytes": sum(n.get("size") or 0 for n in notes),
        "total_tags": len(tags["tags"]), "backend": info.get("backend"),
        "skipped": tags["skipped"],
        **({"errors": tags["errors"]} if "errors" in tags else {}),
    }, ensure_ascii=False, indent=2)


# 19. obsidian-create-from-template
@_tool
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
    await _backend().write_note(new_path, content, create=True)
    return f"Created '{new_path}' from template '{template_path}'."


# 20. obsidian-list-templates
@_tool
@_tool_errors
@_vault_scan
async def obsidian_list_templates(folder: str = "templates") -> str:
    """List available templates in the templates folder.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

    Args:
        folder: Templates folder path (default: "templates")
    """
    notes = await _scan_notes(folder=folder)
    if not notes:
        return f"No templates found in '{folder}/'."
    return json.dumps(
        [{"path": n["path"], "size": n["size"]} for n in notes],
        ensure_ascii=False, indent=2,
    )


@_tool
@_tool_errors
async def obsidian_refresh() -> str:
    """Refresh the backend's local vault representation."""
    # The shared tool wrapper performs the refresh, including error mapping.
    return "Vault refreshed."


# run.py loads dotenv before this single environment-consumption point.
try:
    mcp = create_app(load_config())
except ConfigError as error:
    if __name__ != "__main__":
        raise
    print(f"Configuration error: {error}", file=sys.stderr)
    raise SystemExit(2) from None


# --- Main ---
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
