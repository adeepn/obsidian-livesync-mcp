"""Shared limits for vault scans and the compact CouchDB metadata index."""
import os


def limits(env=None):
    env = os.environ if env is None else env
    values = []
    for key, default in [('LIVESYNC_SCAN_MAX_NOTES', 10000),
                         ('LIVESYNC_SCAN_MAX_BYTES', 64 * 1024 * 1024)]:
        try:
            value = int(env.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f'{key} must be a positive integer') from None
        if value <= 0:
            raise ValueError(f'{key} must be a positive integer')
        values.append(value)
    return tuple(values)


class ScanBudget:
    def __init__(self):
        self.max_notes, self.max_bytes = limits()
        self.paths = set()
        self.bytes = 0
        self.truncated = False

    def admit(self, path, size):
        if (len(self.paths) + (path not in self.paths) > self.max_notes
                or self.bytes + size > self.max_bytes):
            self.truncated = True
            return False
        self.paths.add(path)
        self.bytes += size
        return True

    def report(self):
        return {'notes': len(self.paths), 'bytes': self.bytes,
                'max_notes': self.max_notes, 'max_bytes': self.max_bytes}
