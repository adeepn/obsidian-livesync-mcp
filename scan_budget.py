"""Operational scan admission and deadlines, independent of device size policy.

Upstream commonlib serviceModules/ServiceFileAccessBase.ts:287-290 returns all
files. These optional peer budgets must never silently hide device files.
"""
from contextvars import ContextVar
import time

active_scan_budget = ContextVar('scan_budget', default=None)


class ScanBudget:
    def __init__(self, max_notes=0, max_bytes=0, max_seconds=20, *, clock=None):
        self.max_notes, self.max_bytes = max_notes, max_bytes
        self.max_seconds = max_seconds
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.paths = set()
        self.bytes = 0
        self.truncated = False
        self.stopped_because = None
        self.last_path = ''
        self.after = ''
        self.limit = 0
        self.sizes = {}

    def stop(self, reason):
        if not self.truncated:
            self.stopped_because = reason
        self.truncated = True
        return False

    def remaining(self):
        if not self.max_seconds:
            return None
        return max(0.0, self.max_seconds - (self.clock() - self.started))

    def expired(self):
        return bool(self.max_seconds and self.remaining() == 0)

    def admit(self, path, size):
        if self.expired():
            return self.stop('time')
        # A multi-pass consumer accounts each path once at its largest input.
        additional = max(0, size - self.sizes.get(path, 0))
        if self.max_notes and len(self.paths) + (path not in self.paths) > self.max_notes:
            return self.stop('notes')
        if self.max_bytes and self.bytes + additional > self.max_bytes:
            return self.stop('bytes')
        self.paths.add(path)
        self.sizes[path] = max(size, self.sizes.get(path, 0))
        self.bytes += additional
        return True

    def report(self):
        return {'notes': len(self.paths), 'bytes': self.bytes,
                'elapsed_ms': int(max(0, self.clock() - self.started) * 1000),
                'max_notes': self.max_notes, 'max_bytes': self.max_bytes,
                'max_seconds': self.max_seconds,
                'stopped_because': self.stopped_because}
