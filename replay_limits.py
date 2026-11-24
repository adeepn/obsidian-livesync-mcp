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
import math

from storage_base import StorageError


class ReplayBudgetExceeded(StorageError, ValueError):
    """Safe public diagnostic; observed can be a bounded lower bound."""

    def __init__(self, budget: str, observed: int | float, limit: int | float,
                 *, decoder_capacity: int | None = None):
        self.budget, self.observed, self.limit = budget, observed, limit
        self.variable = 'LIVESYNC_JOURNAL_' + budget.upper()
        if decoder_capacity is not None:
            super().__init__(
                f'{budget} budget decoder capacity exhausted: observed {observed}, '
                f'configured {limit}; {self.variable} controls admission but '
                f'the Python JSON decoder is constrained by recursion limit '
                f'{decoder_capacity} (actual capacity depends on call-stack depth); '
                'raising the variable alone cannot admit this input; reduce nesting'
            )
            return
        super().__init__(
            f'{budget} budget limit reached: observed {observed}, configured {limit}; '
            f'raise {self.variable}'
        )


@dataclass(frozen=True)
class ReplayLimits:
    max_line_bytes: int = 256 * 1024**2
    max_inflated_bytes: int = 8 * 1024**3
    max_records: int = 1_000_000
    max_json_depth: int = 512
    max_object_bytes: int = 8 * 1024**3
    max_refresh_keys: int = 1_000_000
    max_refresh_bytes: int = 64 * 1024**3
    max_refresh_seconds: float = 3600.0

    @classmethod
    def from_env(cls, env):
        values = {}
        for field in fields(cls):
            variable = 'LIVESYNC_JOURNAL_' + field.name.upper()
            raw = env.get(variable, field.default)
            try:
                value = float(raw) if field.name == 'max_refresh_seconds' else int(raw)
                if value <= 0 or (isinstance(value, float) and not math.isfinite(value)):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f'{variable} must be positive and finite') from None
            values[field.name] = value
        return cls(**values)
