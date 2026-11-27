"""Deterministic tool-level timeout injection; real process lifecycle is tested separately."""

def timeout_for(monkeypatch, predicate):
    import regex_runner
    original = regex_runner.apply
    def apply(pattern, text, op, timeout=None, **kwargs):
        if predicate(pattern, text, op):
            raise regex_runner.RegexTimeoutError('injected request deadline')
        # The normal-note branch tests real matching without timing process startup.
        return original(pattern, text, op, timeout=30, **kwargs)
    monkeypatch.setattr(regex_runner, 'apply', apply)
