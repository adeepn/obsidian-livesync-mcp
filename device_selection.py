"""Local device selection shared by both storage writers."""
import asyncio

from livesync_format import canonical_path, is_target_file
from storage_base import StorageError, WriterUnsupported


class DeviceSelection:
    selection_only = ''
    selection_ignore = ''
    selection_use_ignore_files = False
    selection_ignore_files = ()

    async def selection_startup(self):
        from selection_engine import capability_check, EngineFailure
        self._selection_engine_state = 'off'
        self._selection_engine_reason = None
        if not (self.selection_only or self.selection_ignore or self.selection_use_ignore_files):
            return
        try:
            await asyncio.to_thread(capability_check)
            verdict = await asyncio.to_thread(self.selection_verdict)
            if not verdict.writer_supported:
                raise WriterUnsupported(verdict.reason)
        except (EngineFailure, WriterUnsupported) as exc:
            self._selection_engine_state = 'unsupported'
            self._selection_engine_reason = str(exc)
            raise WriterUnsupported(str(exc)) from None
        self._selection_engine_state = 'ready'

    def selection_verdict(self, path='', **kwargs):
        identity = getattr(self, '_path_identity', None)
        if identity is None:
            identity = getattr(getattr(self, '_store', None), 'path_identity', 'legacy')
        return is_target_file(
            path, only_patterns=self.selection_only, ignore_patterns=self.selection_ignore,
            case_sensitive=identity in ('case-sensitive', 'nfc-case-sensitive'),
            **kwargs,
        )

    def selection_info(self):
        verdict = self.selection_verdict(
            ignore_files={} if self.selection_use_ignore_files else None,
            ignore_names=self.selection_ignore_files,
        )
        if verdict.writer_supported and self.selection_ignore:
            verdict = is_target_file('', ignore_patterns=self.selection_ignore)
        return {
            'selection_engine_state': getattr(self, '_selection_engine_state', 'off'),
            'selection_engine_reason': getattr(self, '_selection_engine_reason', None),
            'filters_active': bool(self.selection_only or self.selection_ignore or self.selection_use_ignore_files),
            'selection_writer_supported': verdict.writer_supported,
            'selection_unsupported_reason': verdict.reason,
        }

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

    async def selection_for(self, path):
        path = canonical_path(path)
        files = None
        if self.selection_use_ignore_files:
            files = {}
            # Same candidates as isAcceptedAll. Read through on every write:
            # changed ignore files cannot remain stale between refresh calls.
            parts = path.split('/')[:-1]
            for n in range(len(parts), -1, -1):
                for name in self.selection_ignore_files:
                    candidate = '/'.join(parts[:n] + [name])
                    files[candidate] = await self._selection_lines(candidate)
        return await asyncio.to_thread(
            self.selection_verdict, path, ignore_files=files,
            ignore_names=self.selection_ignore_files,
        )

    async def check_selection(self, path):
        verdict = await self.selection_for(path)
        if not verdict.writer_supported:
            raise WriterUnsupported(verdict.reason)
        if not verdict.allowed:
            raise StorageError(f"path excluded by the vault's {verdict.rule}; devices would not synchronize it: {path}")
