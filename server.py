"""Obsidian MCP Server — remote MCP server for Obsidian via CouchDB/LiveSync."""

from diagnostic_hygiene import (escape_path, DiagnosticPath, path_field, recommendation_fields, assessment_fields, PublicDiagnostic, DiagnosticCode, cause_name)

import json
import inspect
import asyncio
import regex_runner
import logging
import os
import re
import secrets
import sys
import time
import copy
import base64
import binascii
import hashlib
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from scan_budget import ScanBudget, active_scan_budget
from replay_limits import ReplayBudgetExceeded
from text_eligibility import is_text_characters, DEFAULT_TEXT_SCAN_MAX_BYTES, enforce_text_budget, text_size
from storage_base import TextBudgetExceeded
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
    WriteSkipped, MutationNotApplied,
    RenameRefused,
    RenamePartialError, UnresolvedConflicts,
    OutcomeUnknown, RETRY_IDEMPOTENT, retry_guidance, WriterUnsupported,
    StorageBackend, StorageError, NoteNotFound, NoteExists,
    ConcurrentModification, UnsupportedVault, StorageUnavailable, NoteContent,
)
from storage_couchdb import CouchDBBackend
from storage_journal import JournalBackend
from object_storage import Boto3ObjectStorage
from frontmatter import (
    FrontmatterError, FrontmatterLimits, frontmatter_limits, get_properties, split_frontmatter, set_properties,
    delete_properties, add_tags, remove_tags, insert_after_frontmatter,
)
from livesync_format import (ForbiddenPathError, PATH_IDENTITY_LEGACY,
                             resolve_doc_type, plain_ingress_utf8, InvalidPlainTextBytes)
from links import GraphBuilder, extract_links, resolve_link
from auth_config import StaticBearerTokenVerifier, select_auth_mode
from oauth_provider import SimpleOAuthProvider


def _configure_device_policy(result, cfg):
    result.disable_checking_config_mismatch = getattr(cfg, 'disable_checking_config_mismatch', False)
    result.auto_accept_compatible_tweak = getattr(cfg, 'auto_accept_compatible_tweak', True)
    result.frontmatter_max_nodes = getattr(cfg, 'frontmatter_max_nodes', 20_000)
    result.frontmatter_max_bytes = getattr(cfg, 'frontmatter_max_bytes', 4 * 1024**2)
    result.frontmatter_max_depth = getattr(cfg, 'frontmatter_max_depth', 200)
    result.selection_timeout_seconds = getattr(cfg, 'selection_timeout_seconds', 5.0)
    result.selection_only = getattr(cfg, 'selection_only', '')
    result.selection_ignore = getattr(cfg, 'selection_ignore', '')
    result.selection_use_ignore_files = getattr(cfg, 'selection_use_ignore_files', None)
    result.selection_ignore_files = getattr(cfg, 'selection_ignore_files', None)


def get_backend(cfg: Config) -> StorageBackend:
    """Construct the selected storage implementation."""
    if cfg.backend == "s3":
        if cfg.s3 is None:
            raise ConfigError("S3 configuration is required.")
        result = JournalBackend(
            Boto3ObjectStorage(cfg.s3, max_object_bytes=cfg.replay_limits.max_object_bytes),
            Path(cfg.s3.state_dir),
            cfg.s3.refresh_min_interval_sec,
            state_size_warn_mb=cfg.s3.state_size_warn_mb,
            **{key: value for key, value in vars(cfg.replay_limits).items()
               if key != 'max_object_bytes'},
            text_verdict_cache_entries=cfg.text_verdict_cache_entries,
        )
        result.sync_max_size_mb_override = cfg.sync_max_size_mb
        if cfg.sync_max_size_mb is not None:
            result.sync_max_size_mb = cfg.sync_max_size_mb
        result.text_scan_max_bytes = cfg.text_scan_max_bytes
        result.regex_max_output_bytes = getattr(cfg, "regex_max_output_bytes", 0)
        result.regex_timeout_seconds = getattr(cfg, "regex_timeout_seconds", regex_runner.DEFAULT_TIMEOUT)
        _configure_device_policy(result, cfg)
        return result
    if cfg.couchdb is None:
        raise ConfigError("CouchDB configuration is required.")
    result = CouchDBBackend(
        url=cfg.couchdb.url,
        username=cfg.couchdb.user,
        password=cfg.couchdb.password,
        database=cfg.couchdb.database,
        custom_headers=cfg.couchdb.custom_headers,
        connect_timeout_seconds=cfg.couchdb.connect_timeout_seconds,
        io_timeout_seconds=cfg.couchdb.io_timeout_seconds,
        jwt_config=cfg.couchdb if cfg.couchdb.jwt_algorithm else None,
        text_verdict_cache_entries=cfg.text_verdict_cache_entries,
        scan_max_notes=cfg.scan_max_notes,
        scan_max_bytes=cfg.scan_max_bytes,
        scan_max_seconds=getattr(cfg, "scan_max_seconds", 20),
    )
    result.couchdb_max_inflated_bytes = cfg.couchdb_max_inflated_bytes
    result.sync_max_size_mb_override = cfg.sync_max_size_mb
    if cfg.sync_max_size_mb is not None:
        result.sync_max_size_mb = cfg.sync_max_size_mb
    result.text_scan_max_bytes = cfg.text_scan_max_bytes
    result.regex_max_output_bytes = getattr(cfg, "regex_max_output_bytes", 0)
    result.regex_timeout_seconds = getattr(cfg, "regex_timeout_seconds", regex_runner.DEFAULT_TIMEOUT)
    _configure_device_policy(result, cfg)
    return result


# Direct Python callers use the module backend; registered tools bind their
# app's backend per call, so separate factory instances cannot cross vaults.
backend: StorageBackend | None = None
_active_backend: ContextVar[StorageBackend | None] = ContextVar("active_backend")
_tool_handlers = []
_active_scan_limits = ContextVar("active_scan_limits", default=(0, 0, 20))


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
    from hosting_logging import install_hosting_log_filters
    install_hosting_log_filters()
    state = {"backend": None}

    @asynccontextmanager
    async def lifespan(app):
        global backend
        startup_backend = None
        is_default = app is globals().get("mcp")
        try:
            startup_backend = get_backend(cfg)
            state["backend"] = startup_backend
            if is_default:
                backend = startup_backend
            await startup_backend.startup_check()
            yield
        except Exception as error:
            _log_boundary_failure('application lifespan failed', error)
            raise RuntimeError('Application failed; inspect typed logs and configuration, then restart') from None
        finally:
            try:
                regex_runner.close()
                if startup_backend is not None:
                    await startup_backend.close()
            except Exception as error:
                _log_boundary_failure('application shutdown failed', error)
                raise RuntimeError('Application shutdown failed; inspect typed logs before restarting') from None
            finally:
                state["backend"] = None
                if is_default:
                    backend = None

    kwargs: dict[str, Any] = {
        "name": "Obsidian MCP Server",
        "instructions": "MCP server for Obsidian vault via CouchDB/LiveSync",
        "host": cfg.host,
        "port": cfg.port,
        # Installed mcp 1.30.0 fastmcp/server.py:179,985 passes this to
        # transport_security.py:147-211. SDK requires positive bytes;
        # sys.maxsize covers every body representable by Python when unlimited.
        "max_request_body_size": cfg.max_request_bytes or sys.maxsize,
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
            limits_token = _active_scan_limits.set((cfg.scan_max_notes, cfg.scan_max_bytes, cfg.scan_max_seconds))
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

def _safe_storage_detail(error: Exception) -> str:
    if isinstance(error, StorageError):
        return error.format()
    return 'cause: ' + cause_name(type(error))


def _storage_message(error: StorageError, *, caller_paths=()) -> str:
    return _storage_message_body(error, caller_paths=caller_paths)


def _storage_message_body(error: StorageError, *, caller_paths=()) -> str:
    """Map device outcomes/errors; expose only explicitly safe diagnostics."""
    if isinstance(error, TextBudgetExceeded):
        return error.format()
    if isinstance(error, MutationNotApplied):
        return f"Error: note is unchanged; {_safe_storage_detail(error)}"
    if isinstance(error, OutcomeUnknown):
        return error.message()
    if isinstance(error, ReplayBudgetExceeded):
        return f"Error: {_safe_storage_detail(error)}"
    if isinstance(error, WriteSkipped):
        return json.dumps(error.as_dict(), ensure_ascii=False)
    if isinstance(error, (WriterUnsupported, RenameRefused)):
        return f"Error: {_safe_storage_detail(error)}"
    if isinstance(error, RenamePartialError):
        source = escape_path(caller_paths[0]) if caller_paths else path_field(getattr(error, 'source_path', None))
        destination = escape_path(caller_paths[1]) if len(caller_paths) > 1 else path_field(getattr(error, 'destination_path', None))
        if error.source_state != "live":
            return (
                f"Error: move target '{destination}' was written, but "
                f"retirement of source '{source}' could not be confirmed. "
                "Verify both paths and their contents before cleanup."
            )
        return (
            f"Error: move target was written, but source retirement failed; "
            f"the note now exists at both '{source}' and "
            f"'{destination}'. Verify both paths and their contents first. "
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
        _TextRequiredError: "Error: this operation requires a text note.",
        ConcurrentModification: (
            "Error: note changed concurrently; read it again and retry."
        ),
        UnsupportedVault: "Error: unsupported vault configuration.",
        StorageUnavailable: "Error: storage is unavailable; try again later.",
    }
    if type(error) in messages:
        public = getattr(error, 'diagnostic', None)
        if type(error) is StorageUnavailable and isinstance(public, PublicDiagnostic) and public.status_guidance() is not None:
            return 'Error: ' + public.format()
        message = messages[type(error)]
        public = getattr(error, 'diagnostic', None)
        if isinstance(public, PublicDiagnostic) and (public.code is not DiagnosticCode.FAILED or public.cause or public.operation is not None):
            return message.rstrip('.') + ': ' + public.format()
        return message
    return f"Error: {_safe_storage_detail(error)}"


def _log_boundary_failure(message, error):
    # P-19: hosting frameworks otherwise render remote/configuration exception
    # chains. Only the typed diagnostic projection may cross this boundary.
    public = getattr(error, 'diagnostic', None)
    detail = PublicDiagnostic.format(public) if type(public) is PublicDiagnostic else 'no typed diagnostic'
    logging.getLogger(__name__).error('%s; cause: %s; %s', message, cause_name(type(error)), detail)


def _reporting_failure(error):
    # Reporting follows a possibly completed mutation; never claim non-application.
    _log_boundary_failure('internal error while reporting a storage outcome', error)
    return (f"Error: internal error while reporting a storage outcome ({cause_name(type(error))}); "
            "the operation's result is unknown — re-read before retrying")


def _publication_text(result, counts):
    """Compatibility projection for typed journal counts; preserve all fields."""
    from publication_notices import notice_text
    count = sum(n for n in counts if type(n) is int and n > 0)
    return notice_text(result, [{'code': 'journal_surrogates_replaced', 'count': count}])


def _tool_errors(
    handler=None, *, force_refresh: bool = False, writer: bool = False,
    read_action: str | None = None,
    operation: str | None = None, operation_arg: str | None = None,
):
    if handler is None:
        return lambda fn: _tool_errors(
            fn, force_refresh=force_refresh, writer=writer, read_action=read_action,
            operation=operation, operation_arg=operation_arg,
        )

    signature = inspect.signature(handler)

    @wraps(handler)
    async def guarded(*args, **kwargs):
        signature.bind(*args, **kwargs)  # Preserve Python argument validation before execution.
        if _backend() is None:
            return "Error: server not started; storage backend is unavailable."
        try:
            regex_runner.bind_loop(asyncio.get_running_loop())
            is_write = writer
            if is_write and read_action is not None:
                arguments = signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                is_write = arguments.arguments['action'] != read_action
            if is_write:
                _backend().check_writer_support()
            if force_refresh:
                await _backend().refresh(force=True)
            else:
                await _backend().refresh()
            from publication_notices import publication_notices, notice_text
            notices = []
            token = publication_notices.set(notices)
            fm_token = frontmatter_limits.set(FrontmatterLimits(
                max_nodes=getattr(_backend(), 'frontmatter_max_nodes', 20_000),
                max_bytes=getattr(_backend(), 'frontmatter_max_bytes', 4 * 1024**2),
                max_depth=getattr(_backend(), 'frontmatter_max_depth', 200)))
            try:
                result = await handler(*args, **kwargs)
                return notice_text(result, notices)
            finally:
                frontmatter_limits.reset(fm_token)
                publication_notices.reset(token)
        except (regex_runner.RegexTimeoutError, regex_runner.RegexOutputExceeded) as error:
            return error.format()
        except StorageError as error:
            try:
                if isinstance(error, (OutcomeUnknown, WriteSkipped, RenameRefused, WriterUnsupported, MutationNotApplied)):
                    arguments = signature.bind(*args, **kwargs)
                    arguments.apply_defaults()
                    paths = [arguments.arguments[key] for key in
                             ('path', 'template_path', 'new_path')
                             if arguments.arguments.get(key)]
                    if handler.__name__ == 'obsidian_duplicate_note' and len(paths) == 1:
                        base, ext = os.path.splitext(paths[0])
                        paths.append(f"{base} copy{ext}")
                    context = ' -> '.join(escape_path(p) for p in paths)
                    if isinstance(error, OutcomeUnknown):
                        outcome_context = context
                    elif isinstance(error, WriteSkipped):
                        response = error.as_dict(caller_path=DiagnosticPath(paths[0], remote=False) if paths else None)
                        response['message'] = f"{context}: {response['message']}"
                        return json.dumps(response, ensure_ascii=False)
                    elif isinstance(error, MutationNotApplied):
                        return f"Error: {context}: note is unchanged; {_safe_storage_detail(error)}"
                    else:
                        return f"Error: {context}: {_safe_storage_detail(error)}"
                if isinstance(error, OutcomeUnknown) and operation is not None:
                    name = operation
                    if operation_arg is not None:
                        arguments = signature.bind(*args, **kwargs)
                        arguments.apply_defaults()
                        value = arguments.arguments[operation_arg]
                        name = f"{name} {value}" if name else value
                        if name == "after-heading":
                            name = "after-heading insertion"
                    if name in RETRY_IDEMPOTENT:
                        return error.message(name, context=outcome_context)
                arguments = signature.bind(*args, **kwargs)
                caller_paths = [arguments.arguments[k] for k in ('path', 'template_path', 'new_path')
                                if arguments.arguments.get(k)]
                return _storage_message(error, caller_paths=caller_paths)
            except Exception as reporting_error:
                return _reporting_failure(reporting_error)

        except ForbiddenPathError:
            return "Error: forbidden vault path."
        except FrontmatterError as error:
            return error.for_creation() if operation == "note create" else error.public_message
        except Exception as error:
            try:
                arguments = signature.bind(*args, **kwargs)
                context = ' -> '.join(escape_path(arguments.arguments[k]) for k in
                    ('path', 'template_path', 'new_path') if arguments.arguments.get(k))
                return f"Error: {context + ': ' if context else ''}storage operation failed; cause: {cause_name(type(error))}"
            except Exception as reporting_error:
                return _reporting_failure(reporting_error)

    if writer:
        if operation_arg is None:
            operations = [operation] if operation is not None else []
        elif operation == "":
            operations = ["overwrite", "append", "prepend", "search-replace", "after-heading insertion"]
        else:
            operations = [name for name in RETRY_IDEMPOTENT if name.startswith(operation + " ")]
        guarded.__doc__ = (handler.__doc__ or "") + (
            "\n\nMutation outcomes: success confirms application; refusal means no change. "
            "An 'outcome unknown' result means the change MAY have been applied. "
            + " ".join(retry_guidance(name) for name in operations)
        )
    return guarded


# Compatibility readers for canvas tools and shared text validation.
# These deliberately reference the module backend, never a captured client.
class _TextRequiredError(StorageError):
    """A text tool received bytes that are not valid UTF-8."""


def _text_representation(note: NoteContent) -> NoteContent:
    """Expose valid UTF-8 newnotes as text without changing storage type.

    P-8: use the same UTF-8, controls and byte-budget rule as scans.
    Ineligible bodies retain metadata/download behavior; copy backend state.
    """
    enforce_text_budget(note, getattr(_backend(), "text_scan_max_bytes", DEFAULT_TEXT_SCAN_MAX_BYTES))
    if note["is_text"]:
        return note
    data = note["data"]
    if data is None:
        raise StorageError()
    try:
        text = data.decode("utf-8")
        if not is_text_characters(text):
            return note
    except UnicodeDecodeError:
        return note
    return {**note, "text": text, "is_text": True}


async def _read_text_note(path: str) -> NoteContent | None:
    """Textual note content together with the revision it was read at.

    The revision flows back into ``write_note(expected_rev=...)`` so a
    device edit landing between the tool's read and write surfaces as a
    ConcurrentModification error instead of being silently overwritten.
    """
    reader = getattr(_backend(), 'read_text_note', _backend().read_note)
    note = await reader(path)
    if note is None:
        return None
    if note["data"] is not None or note["is_text"]:
        note = _text_representation(note)
    if not note["is_text"]:
        raise _TextRequiredError()
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
_scan_budget = active_scan_budget
_scan_clock = time.monotonic


def _decode_cursor(cursor):
    if not cursor:
        return ''
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
        if not isinstance(value, dict) or value.get('v') != 1 or not isinstance(value.get('after'), str):
            raise ValueError
        return value['after']
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('Invalid scan cursor; restart without cursor.') from None


def _encode_cursor(path):
    return base64.urlsafe_b64encode(json.dumps(
        {'v': 1, 'after': path}, ensure_ascii=True).encode()).decode()


def _vault_scan(handler):
    @wraps(handler)
    async def scanned(*args, **kwargs):
        bound = inspect.signature(handler).bind(*args, **kwargs)
        bound.apply_defaults()
        cursor = bound.arguments.get('cursor', '')
        limit = bound.arguments.get('limit', 0)
        try:
            after = _decode_cursor(cursor)
        except ValueError:
            return json.dumps({'reason': 'invalid_cursor', 'message':
                               'Invalid scan cursor; restart without cursor.'})
        if type(limit) is not int or limit < 0:
            return json.dumps({'reason': 'invalid_limit', 'message': 'limit must be a nonnegative integer.'})
        budget = ScanBudget(*_active_scan_limits.get(), clock=_scan_clock)
        budget.after, budget.last_path, budget.limit = after, after, limit
        acquired = False
        token = _scan_budget.set(budget)
        try:
            remaining = budget.remaining()
            try:
                if remaining is None:
                    await _scan_semaphore.acquire()
                else:
                    await asyncio.wait_for(_scan_semaphore.acquire(), remaining)
                acquired = True
            except TimeoutError:
                return json.dumps({'reason': 'scans_busy', 'message':
                    'Scans busy; retry, or increase LIVESYNC_SCAN_MAX_SECONDS.'})
            response = await handler(*args, **kwargs)
            if budget.expired():
                budget.stop('time')
            index_incomplete = (getattr(_backend(), 'index_truncated', False)
                                or any(view[1] for view in getattr(budget, 'index_views', {}).values()))
            if index_incomplete:
                budget.stop('index')
                budget.stopped_because = 'index'
            try:
                result = json.loads(response)
            except (ValueError, TypeError):
                result = {'message': response}
            if not isinstance(result, dict):
                result = {'results': result}
            if handler.__name__ in ('obsidian_list_notes', 'obsidian_list_templates') and 'results' not in result:
                result['results'] = []
            if budget.truncated and handler.__name__ in ('obsidian_list_notes', 'obsidian_list_templates') and not budget.paths:
                result = {'results': [], 'message': 'Results truncated; narrow the folder/extension filter, retry with next_cursor or raise the scan budget.'}
            if index_incomplete:
                if 'index_complete' in result:
                    result['index_complete'] = False
                result['index_incomplete'] = True
                result['remedy'] = ('Continue calls until index_complete is true in obsidian_vault_stats, '
                                    'then restart pagination without cursor; partial index paths can arrive behind a cursor.')
            result['scan'] = budget.report()
            if budget.truncated:
                result.update(truncated=True, next_cursor=_encode_cursor(budget.last_path))
                if hasattr(budget, 'total'):
                    result.update(returned=len(result['results']) if 'results' in result
                                  else result.get('total_notes', len(budget.paths)), total=budget.total)
                    if index_incomplete:
                        result['total_is_lower_bound'] = True
            if handler.__name__ in ('obsidian_get_links', 'obsidian_find_hub_notes',
                                    'obsidian_find_orphaned_notes'):
                result['scope'] = 'scanned pages only'
            return json.dumps(result, ensure_ascii=False, indent=2)
        finally:
            if acquired:
                _scan_semaphore.release()
            _scan_budget.reset(token)
    return scanned


async def _scan_await(awaitable):
    budget = _scan_budget.get()
    remaining = budget.remaining() if budget else None
    try:
        return await awaitable if remaining is None else await asyncio.wait_for(awaitable, remaining)
    except TimeoutError:
        if budget:
            budget.stop('time')
        return None


async def _scan_notes(**filters):
    result = []
    budget = _scan_budget.get()
    notes = await _scan_await(_backend().list_notes(**filters))
    if notes is None:
        return result
    notes = sorted(notes, key=lambda meta: meta['path'])
    if budget:
        budget.total = len(notes)
    for meta in notes:
        path = meta['path']
        if budget and path <= budget.after:
            continue
        if budget and budget.limit and len(result) >= budget.limit:
            budget.stop('limit')
            break
        if budget and not budget.admit(path, len(json.dumps(meta).encode())):
            break
        result.append(meta)
        if budget:
            budget.last_path = path
    return result


async def _scan_texts():
    budget = _scan_budget.get()
    count = 0
    iterator = (_backend().iterate_texts(after=budget.after) if budget and budget.after
                else _backend().iterate_texts())
    async with aclosing(iterator) as texts:
        while True:
            if budget and budget.expired():
                budget.stop('time')
                break
            try:
                row = await _scan_await(anext(texts))
            except StopAsyncIteration:
                break
            if row is None:
                break
            meta, text = row
            del row
            path = meta['path']
            if budget and path <= budget.after:
                continue
            if budget and budget.limit and count >= budget.limit:
                budget.stop('limit')
                break
            size = len(path.encode()) + (len(text.encode()) if text else 0)
            if budget and not budget.admit(path, size):
                break
            count += 1
            if budget:
                budget.last_path = path
            yield meta, text
            del text


async def _graph(namespace=None) -> dict:
    # Resolve each page's out-links against the COMPLETE available metadata,
    # without spending the same admission budget in a separate metadata pass.
    metadata = await _scan_await(_backend().list_notes()) or []
    paths = sorted(meta['path'] for meta in metadata)
    if namespace is not None:
        namespace.extend(paths)
    identity = (await _scan_await(_backend().backend_info()) or {}).get(
        "path_identity", PATH_IDENTITY_LEGACY)
    graph = GraphBuilder(paths, path_identity=identity)
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
    result = graph.result()
    if _scan_budget.get() is not None:
        result['nodes'] = [path for path in paths if path in processed]
    return {**result, "skipped": skipped}


async def _tags(metadata=None) -> dict:
    counts, skipped, errors = {}, [], {}
    async with aclosing(_scan_texts()) as texts:
        async for meta, text in texts:
            path = meta["path"]
            if metadata is not None:
                metadata.append(meta)
            if text is None:
                skipped.append(path)
                continue
            try:
                props = get_properties(text)
                _, body = split_frontmatter(text)
            except FrontmatterError as error:
                skipped.append(path)
                errors[path] = error.public_message
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


async def _regex_apply(pattern, text, op):
    timeout = getattr(_backend(), 'regex_timeout_seconds', regex_runner.DEFAULT_TIMEOUT)
    budget = _scan_budget.get()
    if budget and budget.remaining() is not None:
        timeout = min(timeout, max(.000001, budget.remaining()))
    return await asyncio.to_thread(regex_runner.apply, pattern, text, op, timeout=timeout)


# === TOOLS ===

# 1. obsidian-search
@_tool
@_tool_errors
@_vault_scan
async def obsidian_search(query: str, regex: bool = False, cursor: str = "", limit: int = 200) -> str:
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
            await _regex_apply(query, "", "findall")
        except regex_runner.RegexTimeoutError:
            budget = _scan_budget.get()
            if budget is not None and budget.expired():
                budget.stop('time')
                return json.dumps({'results': [], 'skipped': []})
            raise
        except re.error:
            return "Invalid regular expression."
    results, skipped, errors = [], [], {}
    budget = _scan_budget.get()
    if budget:
        budget.limit = 0
    async with aclosing(_scan_texts()) as texts:
        async for meta, text in texts:
            if text is None:
                skipped.append(meta["path"])
                continue
            if pattern is not None:
                try:
                    matches = (await _regex_apply(pattern, text, "findall"))["matches"]
                except regex_runner.RegexTimeoutError as error:
                    errors[meta['path']] = error.format()
                    continue
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
            if limit and len(results) >= limit:
                if budget:
                    budget.stop('limit')
                break
    result = {"results": results, "skipped": skipped}
    if errors:
        result['errors'] = errors
    return json.dumps(result, ensure_ascii=False, indent=2)


@_tool
@_tool_errors(writer=True, operation='attachment upload')
async def obsidian_upload_attachment(
    path: str, content_b64: str, *, overwrite: bool = False, lossy_text: bool = False,
) -> str:
    """Upload a binary attachment (PDF, scan, image) from strict base64.

    Oversized input returns status=skipped under syncMaxSizeInMB. Device path
    rules select plain or newnote storage. Existing files require overwrite=True. Malformed UTF-8
    on plain paths is refused unless lossy_text=True explicitly converts it.
    Returns stored size and SHA-256, and replaced_bytes for lossy conversion.
    """
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return "Error: invalid base64 content (characters or padding)."
    # The backend gates original disk bytes before host-style decoding.
    # C:serviceModules/ServiceFileHandlerBase/events.ts:63-69.
    try:
        meta = await _backend().write_attachment(
            path, data, create=True, overwrite=overwrite, lossy_text=lossy_text,
        )
    except InvalidPlainTextBytes as error:
        return f'Error: {escape_path(path)}: {error.format()}'
    replaced_bytes = 0
    if lossy_text and resolve_doc_type(path, data) == 'plain':
        _, replaced_bytes = plain_ingress_utf8(data, lossy_text=True)
    # Legacy Blob.text() can strip BOM. Report the saved revision's bytes,
    # rather than the caller's original input or the pre-split Blob size.
    stored = await _backend().read_note(meta['path'])
    if stored is None or stored['rev'] != meta['rev']:
        raise ConcurrentModification()
    stored_bytes = stored['text'].encode('utf-8') if stored['is_text'] else stored['data']
    return json.dumps({
        "path": meta["path"], "size": len(stored_bytes),
        "sha256": hashlib.sha256(stored_bytes).hexdigest(),
        **({'replaced_bytes': replaced_bytes} if replaced_bytes else {}),
    }, ensure_ascii=False)


@_tool
@_tool_errors
async def obsidian_download_attachment(path: str, max_bytes: int = 0) -> str:
    """Download a complete binary attachment as base64 with size and SHA-256.

    A positive max_bytes imposes an explicit caller limit; zero/negative
    disables it. Files are complete or refused, never truncated.
    Plain documents use obsidian_fetch. UTF-8 newnotes also remain downloadable.
    """
    note = await _backend().read_note(path)
    if note is None:
        return f"Note '{escape_path(path)}' not found."
    budget = getattr(_backend(), 'text_scan_max_bytes', DEFAULT_TEXT_SCAN_MAX_BYTES)
    if note["is_text"] and not (budget and text_size(note) > budget):
        return "Error: textual note; use obsidian_fetch."
    data = note['text'].encode('utf-8') if note['is_text'] else note['data']
    if data is None:
        raise StorageError()
    size = len(data)
    if max_bytes > 0 and size > max_bytes:
        return (
            f"Error: attachment size {size} bytes exceeds "
            f"max_bytes ({max_bytes} bytes)."
        )
    return json.dumps({
        "path": note["meta"]["path"], "size": size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }, ensure_ascii=False)


# 2. obsidian-fetch
@_tool
@_tool_errors
async def obsidian_fetch(path: str, include_properties: bool = True, include_stats: bool = False, start_line: int | None = None, end_line: int | None = None) -> str:
    """Fetch the content and metadata of a note, base, or canvas file.

    Args:
        path: Path to the file (e.g. "My Note.md", "DB.base", "Board.canvas")
        include_properties: Include frontmatter properties in response
        include_stats: Include word/character count statistics
    """
    if any(value is not None and (type(value) is not int or value < 1)
           for value in (start_line, end_line)) or (start_line is not None and end_line is not None and end_line < start_line):
        return json.dumps({'reason': 'invalid_line_window', 'message':
                           'Use positive 1-based inclusive lines with end_line >= start_line.'})
    refusal = None
    try:
        reader = getattr(_backend(), 'read_text_note', _backend().read_note)
        note = await reader(path)
        if note is None:
            return f"Note '{escape_path(path)}' not found."
        if not note['is_text'] and note['data'] is None:
            note = await _backend().read_note(path)
        note = _text_representation(note)
        meta = note['meta']
    except TextBudgetExceeded as error:
        refusal = error.as_dict()
        meta = await _backend().get_meta(path)
    result = {
        key: meta.get(key)
        for key in ("path", "ctime", "mtime", "type", "size")
    }
    if refusal is not None:
        result.update(refusal)
    elif not note["is_text"]:
        data = note["data"]
        if data is None:
            raise StorageError()
        result.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    else:
        content = note["text"]
        if content is None:
            raise StorageError()
        result["content"] = content
        # commonlib ServiceFileAccessBase.ts:141-148 reads whole text; a window
        # changes only peer transport, never validation or properties/stats.
        if start_line is not None or end_line is not None:
            lines = content.splitlines(keepends=True)
            selected = lines[(start_line or 1) - 1:end_line]
            result.update(content=''.join(selected), total_lines=len(lines),
                          returned_lines=len(selected), truncated=len(selected) < len(lines))
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
@_tool_errors(writer=True, operation='note create')
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
    return f"Created '{escape_path(path)}'."


# 4. obsidian-update-note
@_tool
@_tool_errors(writer=True, operation='', operation_arg='mode')
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
        return f"Error: unknown mode: {escape_path(mode)}. Use: {modes}"
    if mode == "overwrite":
        if not content:
            return "Error: empty overwrite content is refused."
        if search or replace or heading or regex:
            return "Error: search, replace, heading and regex require their own mode."
    note = await _read_text_note(path)
    if note is None:
        return f"Note '{escape_path(path)}' not found."
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
            return f"Error: heading '{escape_path(heading)}' not found in '{escape_path(path)}'."
    else:
        if not search:
            return "Error: search parameter required for search-replace mode."
        try:
            pattern = search if regex else re.escape(search)
            if regex:
                result = await asyncio.to_thread(
                    regex_runner.apply, pattern, existing, "subn",
                    replacement=replace, flags=0,
                    timeout=getattr(_backend(), 'regex_timeout_seconds', regex_runner.DEFAULT_TIMEOUT),
                    max_output_bytes=(getattr(_backend(), 'regex_max_output_bytes', 0)
                                      or getattr(_backend(), 'text_scan_max_bytes', DEFAULT_TEXT_SCAN_MAX_BYTES)))
                new_content, matches = result["result"], result["count"]
            else:
                new_content, matches = re.subn(pattern, lambda match: replace, existing)
        except re.error:
            return "Error: invalid regex search pattern."
        except (regex_runner.RegexTimeoutError, regex_runner.RegexOutputExceeded):
            raise
        except ValueError:
            return "Error: regex replacement output too large."
        if not matches:
            return "No matches found for search string."
    if new_content != existing:
        await _backend().write_note(path, new_content, expected_rev=note["rev"])
    if matches is not None:
        return f"Updated '{escape_path(path)}': {matches} matches replaced."
    return f"Updated '{escape_path(path)}' ({escape_path(mode)})."


# 5. obsidian-delete-note
@_tool
@_tool_errors(writer=True, operation='delete')
async def obsidian_delete_note(path: str) -> str:
    """Delete a note from the vault.

    Args:
        path: Path to the note to delete
    """
    doc_id = path
    success = await _backend().delete_note(doc_id)
    if success:
        return f"Deleted '{escape_path(path)}'."
    return f"Note '{escape_path(path)}' not found."


# 6. obsidian-move-note
@_tool
@_tool_errors(writer=True, operation='move')
async def obsidian_move_note(path: str, new_path: str) -> str:
    """Move or rename a note.

    Oversized destinations are refused before mutation: this peer lacks the
    retained disk copy that makes source retirement safe on a device.

    Args:
        path: Current path of the note
        new_path: New path for the note
    """
    await _backend().rename_note(path, new_path)
    return f"Moved '{escape_path(path)}' -> '{escape_path(new_path)}'."


# 7. obsidian-duplicate-note
@_tool
@_tool_errors(writer=True, operation='copy')
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
    return f"Duplicated '{escape_path(path)}' -> '{escape_path(new_path)}'."


# 8. obsidian-list-notes
@_tool
@_tool_errors
@_vault_scan
async def obsidian_list_notes(
    folder: str = "",
    extension: str = "",
    cursor: str = "", limit: int = 200,
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
@_tool_errors(writer=True, read_action="get", operation='properties', operation_arg='action')
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
        return f"Note '{escape_path(path)}' not found."
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
        return f"Error: unknown action: {escape_path(action)}. Use: get, set, delete"
    if new_content == content:
        return f"No property changes in '{escape_path(path)}'."
    await _backend().write_note(path, new_content, expected_rev=note["rev"])
    return f"Updated properties of '{escape_path(path)}'."


# 10. obsidian-manage-tags
@_tool
@_tool_errors(writer=True, read_action="list", operation='tags', operation_arg='action')
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
        return f"Note '{escape_path(path)}' not found."
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
        return f"Error: unknown action: {escape_path(action)}. Use: list, add, remove"
    if not tag_list:
        return "Error: tags parameter required."
    new_content = (add_tags if action == "add" else remove_tags)(content, tag_list)
    if new_content == content:
        return f"No frontmatter tag changes in '{escape_path(path)}'; inline tags untouched."
    await _backend().write_note(path, new_content, expected_rev=note["rev"])
    return f"Updated frontmatter tags in '{escape_path(path)}'; inline tags untouched."


# 11. obsidian-get-all-tags
@_tool
@_tool_errors
@_vault_scan
async def obsidian_get_all_tags(cursor: str = "", limit: int = 200) -> str:
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
    cursor: str = "", limit: int = 200,
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
    namespace = []
    graph = await _graph(namespace)
    identity = (await _scan_await(_backend().backend_info()) or {}).get(
        "path_identity", PATH_IDENTITY_LEGACY)
    resolved = resolve_link(path, namespace, path_identity=identity)
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
async def obsidian_get_link_graph(cursor: str = "", limit: int = 200) -> str:
    """Get the full link graph of the vault (all nodes and edges).

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
    return json.dumps(await _graph(), ensure_ascii=False, indent=2)


# 14. obsidian-find-orphaned-notes
@_tool
@_tool_errors
@_vault_scan
async def obsidian_find_orphaned_notes(cursor: str = "", limit: int = 200) -> str:
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
    if graph["skipped"] or (_scan_budget.get() and _scan_budget.get().truncated):
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
async def obsidian_find_hub_notes(min_links: int = 3, cursor: str = "", limit: int = 200) -> str:
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
    if graph["skipped"] or (_scan_budget.get() and _scan_budget.get().truncated):
        return json.dumps(
            {"hubs": hubs, "skipped": graph["skipped"]},
            ensure_ascii=False, indent=2,
        )
    if not hubs:
        return f"No notes with {min_links}+ links found."
    return json.dumps(hubs, ensure_ascii=False, indent=2)


# 16. obsidian-canvas-node
@_tool
@_tool_errors(writer=True, operation='canvas node', operation_arg='action')
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
        return f"Canvas '{escape_path(path)}' not found."

    canvas = _load_canvas(content)
    if canvas is None:
        return "Invalid canvas format."

    if action == "add":
        if node_id and any(_entry_id(n) == node_id for n in canvas["nodes"]):
            return f"Node '{escape_path(node_id)}' already exists in '{escape_path(path)}'."
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
        return f"Added {escape_path(node_type)} node '{escape_path(new_id)}' to '{escape_path(path)}'."

    elif action == "remove":
        if not node_id:
            return "node_id required for remove action."
        kept_nodes = [n for n in canvas["nodes"] if _entry_id(n) != node_id]
        if len(kept_nodes) == len(canvas["nodes"]):
            return f"Node '{escape_path(node_id)}' not found in '{escape_path(path)}'."
        canvas["nodes"] = kept_nodes
        canvas["edges"] = [
            e for e in canvas["edges"]
            if not (isinstance(e, dict)
                    and node_id in (e.get("fromNode"), e.get("toNode")))
        ]
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False), expected_rev=rev
        )
        return f"Removed node '{escape_path(node_id)}' from '{escape_path(path)}'."

    return f"Unknown action: {escape_path(action)}. Use: add, remove"


# 17. obsidian-canvas-edge
@_tool
@_tool_errors(writer=True, operation='canvas edge', operation_arg='action')
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
        return f"Canvas '{escape_path(path)}' not found."
    content = note["text"]

    canvas = _load_canvas(content)
    if canvas is None:
        return "Invalid canvas format."

    if action == "add":
        if not from_node or not to_node:
            return f"from_node and to_node required for add action in '{escape_path(path)}'."
        known_nodes = {_entry_id(n) for n in canvas["nodes"]}
        for endpoint in (from_node, to_node):
            if endpoint not in known_nodes:
                return f"Node '{escape_path(endpoint)}' not found in '{escape_path(path)}'."
        if edge_id and any(_entry_id(e) == edge_id for e in canvas["edges"]):
            return f"Edge '{escape_path(edge_id)}' already exists in '{escape_path(path)}'."
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
        return f"Added edge '{escape_path(new_id)}' ({escape_path(from_node)} -> {escape_path(to_node)}) to '{escape_path(path)}'."

    elif action == "remove":
        if not edge_id:
            return "edge_id required for remove action."
        kept_edges = [e for e in canvas["edges"] if _entry_id(e) != edge_id]
        if len(kept_edges) == len(canvas["edges"]):
            return f"Edge '{escape_path(edge_id)}' not found in '{escape_path(path)}'."
        canvas["edges"] = kept_edges
        await _backend().write_note(
            path, json.dumps(canvas, ensure_ascii=False),
            expected_rev=note["rev"],
        )
        return f"Removed edge '{escape_path(edge_id)}' from '{escape_path(path)}'."

    return f"Unknown action: {escape_path(action)}. Use: add, remove"


# 18. obsidian-vault-stats
@_tool
@_tool_errors
@_vault_scan
async def obsidian_vault_stats(cursor: str = "", limit: int = 0) -> str:
    """Get aggregate statistics for the entire vault.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.
    """
    metadata = await _scan_await(_backend().list_notes()) or []
    budget = _scan_budget.get()
    if budget:
        budget.total = len(metadata)
    notes = []
    tags = await _tags(notes)
    by_ext = {}
    for note in notes:
        path = note["path"]
        ext = path.rsplit(".", 1)[-1] if "." in path else "unknown"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    info = await _scan_await(_backend().backend_info()) or {}
    local_state = {key: info[key] for key in
                   ('text_budget_bytes', 'regex_max_output_bytes', 'regex_timeout_seconds')
                   if key in info and type(info[key]) in (int, float)}
    for field in ('index_ids_collected', 'index_docs_indexed', 'index_next_skip'):
        if field in info:
            value = info[field]
            local_state[field] = value if type(value) is int and value >= 0 else 0
    if 'index_complete' in info:
        local_state['index_complete'] = info['index_complete'] if type(info['index_complete']) is bool else False
    if info.get("backend") == "s3-journal":
        size = info["state_dir_bytes"]
        threshold = info["state_size_warn_mb"]
        warning = threshold > 0 and size > threshold * 1024 * 1024
        local_state["local_state"] = {
            "path": info["state_dir_path"], "bytes": size,
            "human_readable_size": f"{size / (1024 * 1024):.2f} MiB",
            "warn_threshold_mb": threshold, "warning": warning,
        }
        if warning:
            local_state["hint"] = (
                f"local peer state exceeds {threshold} MB; "
                "see README 'Local cache' for monitoring and reset"
            )
    if 'recommendation_policy' in info:
        local_state['recommendation_policy'] = recommendation_fields(info['recommendation_policy'])
    # Public selection observability is a closed projection, never arbitrary
    # backend settings or exception text (P-19/P-21/P-22).
    for field in ('selection_only_source', 'selection_ignore_source',
                  'selection_use_ignore_files_source', 'selection_ignore_files_source'):
        if field in info:
            value = info[field]
            local_state[field] = value if type(value) is str and value in {'explicit', 'vault'} else '<unexpected source>'
    if 'selection_engine_state' in info:
        value = info['selection_engine_state']
        local_state['selection_engine_state'] = value if type(value) is str and value in {'off', 'ready', 'unsupported'} else 'unsupported'
    for field in ('filters_active', 'selection_writer_supported', 'enableCompression', 'custom_headers_configured'):
        if field in info:
            local_state[field] = info[field] if type(info[field]) is bool else False
    if "auth_mode" in info:
        mode = info["auth_mode"]
        local_state["auth_mode"] = mode if type(mode) is str and mode in {'basic', 'jwt'} else None
        algorithm = info.get("jwt_algorithm")
        local_state["jwt_algorithm"] = algorithm if type(algorithm) is str and algorithm in {'HS256', 'HS512', 'ES256', 'ES512'} else None
    for field in ('custom_headers_count', 'selection_unresolved_ignore_names'):
        if field in info:
            count = info[field]
            local_state[field] = count if type(count) is int and 0 <= count < 2**63 else 0
    if "tweak_assessment" in info:
        assessment = info["tweak_assessment"]
        local_state["tweak_assessment"] = assessment_fields(assessment)
    return json.dumps({
        **local_state,
        "total_notes": len(notes), "by_extension": by_ext,
        "total_size_bytes": sum(n.get("size") or 0 for n in notes),
        "total_tags": len(tags["tags"]), "backend": info.get("backend"),
        "skipped": tags["skipped"],
        **({"errors": tags["errors"]} if "errors" in tags else {}),
    }, ensure_ascii=False, indent=2)


# 19. obsidian-create-from-template
@_tool
@_tool_errors(writer=True, operation='template create')
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
        return f"Template '{escape_path(template_path)}' not found."
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
    return f"Created '{escape_path(new_path)}' from template '{escape_path(template_path)}'."


# 20. obsidian-list-templates
@_tool
@_tool_errors
@_vault_scan
async def obsidian_list_templates(folder: str = "templates", cursor: str = "", limit: int = 200) -> str:
    """List available templates in the templates folder.

    Best-effort scan in backend iteration order; snapshot consistency is
    not guaranteed. Concurrent changes may be interleaved during the scan.

    Args:
        folder: Templates folder path (default: "templates")
    """
    notes = await _scan_notes(folder=folder)
    if not notes:
        return f"No templates found in '{escape_path(folder)}/'."
    return json.dumps(
        [{"path": n["path"], "size": n["size"]} for n in notes],
        ensure_ascii=False, indent=2,
    )


@_tool
@_tool_errors(force_refresh=True)
async def obsidian_refresh() -> str:
    """Refresh the backend's local vault representation."""
    # The shared tool wrapper performs the refresh, including error mapping.
    return "Vault refreshed."


def run_host(action):
    """Prevent entry-point failures from handing external text to a host logger."""
    try:
        return action(transport="streamable-http")
    except Exception as error:
        _log_boundary_failure('application host failed', error)
        raise SystemExit(1) from None


# run.py loads dotenv before this single environment-consumption point.
try:
    mcp = create_app(load_config())
except ConfigError as error:
    if __name__ != "__main__":
        raise
    print(f"Configuration error: {error}", file=sys.stderr)
    raise SystemExit(2) from None

except Exception as error:
    _log_boundary_failure('application construction failed', error)
    raise RuntimeError('Application construction failed; inspect typed logs and configuration') from None


# --- Main ---
if __name__ == "__main__":
    run_host(mcp.run)
