"""Shared limits for vault scans and the compact CouchDB metadata index."""
class ScanBudget:
    def __init__(self, max_notes=10000, max_bytes=64 * 1024 * 1024):
        self.max_notes, self.max_bytes = max_notes, max_bytes
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
