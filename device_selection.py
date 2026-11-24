"""Local device selection shared by both storage writers."""
from diagnostic_hygiene import writer_diagnostic, settings_exception_diagnostic

import asyncio
import hashlib
import json
from collections import OrderedDict

from livesync_format import (canonical_path, TargetVerdict, resolve_ignore_settings,
                             UnsupportedVaultSettings)
from selection_engine import SelectionWorker, EngineFailure
from storage_base import SelectionRefused, WriterUnsupported, UnsupportedVault


class DeviceSelection:
    selection_only = ''
    selection_ignore = ''
    selection_use_ignore_files = None
    selection_ignore_files = None
    selection_cache_entries = 4096

    def resolve_selection_settings(self, milestone):
        # Capture the operator's independent tri-state choices before replacing
        # public attributes with resolved values; repeated refreshes keep sources.
        if not hasattr(self, '_selection_overrides'):
            self._selection_overrides = (self.selection_use_ignore_files, self.selection_ignore_files)
        use, names = self._selection_overrides
        try:
            self.selection_use_ignore_files, self.selection_ignore_files = resolve_ignore_settings(
                milestone, use_override=use, names_override=names)
        except UnsupportedVaultSettings as exc:
            self._selection_resolution_diagnostic = settings_exception_diagnostic(exc)
            raise
        self._selection_resolution_diagnostic = None

    def _selection_active(self):
        return bool(self.selection_only or self.selection_ignore or self.selection_use_ignore_files)

    def _selection_init(self):
        if not hasattr(self, '_selection_worker'):
            self._selection_worker = SelectionWorker()
            # Admit before to_thread: parallel tools cannot grow a pool of
            # blocked threads or Node processes. One in-flight request per backend.
            self._selection_limit = asyncio.Semaphore(1)
            self._selection_cache = OrderedDict()
            self._selection_engine_state = 'off'
            self._selection_engine_reason = None
            self._selection_diagnostic = None
            self._selection_rules = None

    def _selection_payload(self, path='', files=None):
        identity = getattr(self, '_path_identity', None)
        if identity is None:
            identity = getattr(getattr(self, '_store', None), 'path_identity', 'legacy')
        return dict(path=path, only_patterns=self.selection_only,
                    ignore_patterns=self.selection_ignore,
                    case_sensitive=identity in ('case-sensitive', 'nfc-case-sensitive'),
                    ignore_files=files, ignore_names=self.selection_ignore_files or ())

    def _selection_configuration(self):
        return (self.selection_only, self.selection_ignore,
                self.selection_use_ignore_files, tuple(self.selection_ignore_files or ()))

    async def selection_diagnostics(self):
        if (self._selection_active() and getattr(self, '_selection_checked_configuration', None)
                != self._selection_configuration()):
            try:
                await self.selection_startup()
            except WriterUnsupported:
                pass
        return self.selection_info()

    async def selection_startup(self):
        self._selection_checked_configuration = self._selection_configuration()
        if not self._selection_active():
            return
        verdict = await self._selection_evaluate(self._selection_payload())
        if not verdict.writer_supported:
            await self.selection_close()
            raise WriterUnsupported(getattr(self, "_selection_diagnostic", None) or verdict.reason)
        # Check ignore compilation too, even if the only list short circuits.
        if self.selection_ignore:
            payload = self._selection_payload()
            payload['only_patterns'] = ''
            verdict = await self._selection_evaluate(payload)
            if not verdict.writer_supported:
                await self.selection_close()
                raise WriterUnsupported(getattr(self, "_selection_diagnostic", None) or verdict.reason)

    async def _selection_evaluate(self, payload):
        self._selection_init()
        async with self._selection_limit:
            # Shield a running thread from cancellation: retain the admission
            # permit until it stops, so cancelled calls cannot multiply workers.
            task = asyncio.create_task(asyncio.to_thread(self._selection_worker.evaluate, payload))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except EngineFailure:
                    break
            try:
                verdict = task.result()
            except EngineFailure as exc:
                self._selection_engine_state = 'unsupported'
                self._selection_engine_reason = str(exc)
                self._selection_diagnostic = exc.diagnostic
                if cancelled:
                    raise asyncio.CancelledError
                return TargetVerdict(False, writer_supported=False, reason=str(exc))
            if cancelled:
                raise asyncio.CancelledError
            self._selection_engine_state = 'ready' if verdict.writer_supported else 'unsupported'
            self._selection_engine_reason = verdict.reason
            self._selection_diagnostic = None
            return verdict

    def selection_info(self):
        self._selection_init()
        active = self._selection_active()
        overrides = getattr(self, '_selection_overrides',
                            (self.selection_use_ignore_files, self.selection_ignore_files))
        resolution_error = getattr(self, '_selection_resolution_diagnostic', None)
        # Read the last observed capability without launching a process.
        reason = writer_diagnostic(getattr(self, "_selection_diagnostic", None) or self._selection_engine_reason) if active and self._selection_engine_reason else None
        if resolution_error is not None:
            reason = resolution_error.format()
        state = self._selection_engine_state if active else 'off'
        return {
            'selection_engine_state': 'unsupported' if resolution_error else state,
            'selection_engine_reason': reason,
            'filters_active': active,
            'selection_only_source': 'explicit',
            'selection_ignore_source': 'explicit',
            'selection_use_ignore_files_source': 'explicit' if overrides[0] is not None else 'vault',
            'selection_ignore_files_source': 'explicit' if overrides[1] is not None else 'vault',
            'selection_writer_supported': reason is None,
            'selection_unsupported_reason': reason,
        }

    async def selection_close(self):
        if hasattr(self, '_selection_worker'):
            async with self._selection_limit:
                await asyncio.to_thread(self._selection_worker.close)
            self._selection_cache.clear()

    async def _selection_lines(self, path):
        # targetFilter.ts getIgnoreFile treats missing/unreadable files as absent.
        try:
            text = await self._read_selection_file(path)
        except (StorageError, ValueError):
            return False
        return False if text is None else text.split('\n')

    async def refresh_selection_files(self):
        if not self.selection_use_ignore_files:
            return
        self._selection_root_snapshot = {
            path: await self._selection_lines(path) for path in self.selection_ignore_files
        }

    def selection_cache_size(self):
        store = getattr(self, '_store', None)
        if store is not None:
            return store.selection_cache_size()
        return len(getattr(self, '_selection_cache', {}))

    async def selection_for(self, path):
        if getattr(self, '_selection_resolution_diagnostic', None) is not None:
            raise UnsupportedVault(self._selection_resolution_diagnostic)
        if getattr(self, '_selection_checked_configuration', None) != self._selection_configuration():
            await self.selection_startup()
        if not self._selection_active():
            return TargetVerdict(True)
        self._selection_init()
        path = canonical_path(path)
        files = None
        if self.selection_use_ignore_files:
            files = {}
            # Read through before lookup: a changed ignore file changes the key.
            parts = path.split('/')[:-1]
            for n in range(len(parts), -1, -1):
                for name in self.selection_ignore_files:
                    candidate = '/'.join(parts[:n] + [name])
                    files[candidate] = await self._selection_lines(candidate)
        payload = self._selection_payload(path, files)
        def fingerprint(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        rules = fingerprint([payload['only_patterns'], payload['ignore_patterns'],
                             self.selection_use_ignore_files, self.selection_ignore_files,
                             payload['case_sensitive'], 'selection-worker-v1'])
        key = fingerprint([path, rules, payload['case_sensitive'], fingerprint(files)])
        store = getattr(self, '_store', None)
        if self._selection_rules != rules:
            self._selection_cache.clear()
            self._selection_rules = rules
        if store is not None:
            cached = store.selection_cache_get(key, rules, self.selection_cache_entries)
        else:
            cached = self._selection_cache.get(key)
            if cached is not None:
                self._selection_cache.move_to_end(key)
        if cached is not None and self._selection_engine_state == 'ready':
            return TargetVerdict(**cached)
        verdict = await self._selection_evaluate(payload)
        # Failures are never cached: repairing the runtime permits recovery.
        if verdict.writer_supported:
            value = dict(allowed=verdict.allowed, rule=verdict.rule,
                         writer_supported=verdict.writer_supported, reason=verdict.reason)
            if store is not None:
                store.selection_cache_put(key, rules, value, self.selection_cache_entries)
            else:
                self._selection_cache[key] = value
                while len(self._selection_cache) > self.selection_cache_entries:
                    self._selection_cache.popitem(last=False)
        return verdict

    async def check_selection(self, path):
        verdict = await self.selection_for(path)
        if not verdict.writer_supported:
            raise WriterUnsupported(getattr(self, "_selection_diagnostic", None) or verdict.reason)
        if not verdict.allowed:
            raise SelectionRefused(path, verdict.rule)
