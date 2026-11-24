"""Operational protection, deliberately separate from device file selection.

C:string_and_binary/chunks.ts:527-542,617-633: V3 binary pieces <=4 MiB;
valid text may cross a UTF-8 boundary by 3 bytes. Base64 expands by 4/3,
compact escaping by <=2 (full JSON by <=6). A conservative encoded-piece
unit is 32 MiB: max(ceil(4 MiB/3)*4, (4 MiB+3)*6) <32 MiB. Line default = 8 units
(256 MiB), including metadata with many child refs and legacy/foreign pieces.
Inflate/object = 256 units (8 GiB), refresh = 2048 units (64 GiB).
Record/key counts = 1M (many tiny chunks/objects); 1 hour allows a 64 GiB
refresh at ~19 MiB/s. Depth 512 allows ample metadata nesting (normal <10).
These are NOT protocol maxima. Journals, old splitters/customChunkSize and
vault history have no universal bound; exceptionally large inputs need
operator tuning. None of these defaults depend on syncMaxSizeInMB.
"""
from dataclasses import dataclass, fields

from storage_base import StorageError


_ABSENT = object()


class ReplayBudgetExceeded(StorageError, ValueError):
    """Safe public diagnostic; observed can be a bounded lower bound."""

    def __init__(self, budget: str, observed: int | float, limit: int | float,
                 *, decoder_capacity: int | None = _ABSENT):
        from diagnostic_hygiene import numeric_field, setting_key
        self.budget = setting_key(budget)
        kind = 'measured' if self.budget == 'max_refresh_seconds' else 'integer'
        self.observed, self.limit = numeric_field(observed, kind=kind), numeric_field(limit, kind=kind)
        self.has_decoder_capacity = decoder_capacity is not _ABSENT
        self.decoder_capacity = numeric_field(decoder_capacity) if self.has_decoder_capacity else None
        self.variable = 'LIVESYNC_JOURNAL_' + self.budget.upper()
        super().__init__()

    def format(self):
        from diagnostic_hygiene import setting_key, numeric_field, numeric_text
        budget = setting_key(getattr(self, 'budget', None))
        if budget == '<unexpected key>':
            return 'replay budget exceeded'
        kind = 'measured' if budget == 'max_refresh_seconds' else 'integer'
        variable = 'LIVESYNC_JOURNAL_' + budget.upper()
        text = f'{budget} budget limit reached: observed {numeric_text(self.observed, kind=kind)}, configured {numeric_text(self.limit, kind=kind)}; raise {variable}'
        if getattr(self, 'has_decoder_capacity', False):
            text += (f'; decoder capacity {numeric_field(self.decoder_capacity)} exhausted; '
                     'raising the variable alone cannot admit this input; reduce nesting')
        return text


@dataclass(frozen=True)
class ReplayLimits:
    max_line_bytes: int = 256 * 1024**2
    max_inflated_bytes: int = 8 * 1024**3
    max_records: int = 1_000_000
    max_json_depth: int = 512
    max_object_bytes: int = 8 * 1024**3
    max_refresh_keys: int = 1_000_000
    max_refresh_bytes: int = 64 * 1024**3
    max_refresh_seconds: int = 3600

    @classmethod
    def from_env(cls, env):
        values = {}
        for field in fields(cls):
            variable = 'LIVESYNC_JOURNAL_' + field.name.upper()
            raw = env.get(variable, field.default)
            try:
                if type(raw) not in (str, int):
                    raise ValueError
                value = int(raw)
                if not 0 < value < 2**63:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f'{variable} must be a positive integer below 2**63') from None
            values[field.name] = value
        return cls(**values)
