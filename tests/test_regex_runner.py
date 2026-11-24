import time
import pytest


def test_timeout_terminates_worker():
    import regex_runner
    start = time.monotonic()
    with pytest.raises(regex_runner.RegexTimeoutError):
        regex_runner.apply('(a+)+$', 'a' * 1024 + '!', 'findall', .2)
    assert time.monotonic() - start < 5


def test_operations_and_output_limit():
    import regex_runner
    assert regex_runner.apply('(a)', 'Ab', 'findall', 2)['matches'] == ['A']
    assert regex_runner.apply('a', 'abc', 'search', 2)['context'] == 'abc'
    assert regex_runner.apply('a', 'aa', 'subn', 2, replacement='x')['result'] == 'xx'
    with pytest.raises(ValueError, match='large'):
        regex_runner.apply('', 'a' * 10, 'subn', 2, replacement='xx', max_output_bytes=20)


def test_findall_unmatched_groups_match_python_contract():
    import regex_runner
    assert regex_runner.apply('(a)|(b)', 'ab', 'findall', 2)['matches'] == [
        ('a', ''), ('', 'b')]


def test_run_entrypoint_is_safe_during_regex_spawn(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = tmp_path / 'spawn_probe.py'
    script.write_text(f"""
import runpy
import sys
runpy.run_path({str(root / 'run.py')!r}, run_name='__mp_main__')
assert 'server' not in sys.modules
assert 'dotenv' not in sys.modules
if __name__ == '__main__':
    import regex_runner
    assert regex_runner.apply('a', 'abc', 'search')['context'] == 'abc'
""")
    env = dict(os.environ, PYTHONPATH=str(root))
    env.pop('LIVESYNC_BACKEND', None)
    result = subprocess.run([sys.executable, str(script)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_persistent_worker_reuses_one_process(monkeypatch):
    import regex_runner
    import subprocess
    regex_runner.close()
    starts = []
    original = subprocess.Popen
    def start(*args, **kwargs):
        starts.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', start)
    try:
        for _ in range(12):
            assert regex_runner.apply('a', 'abc', 'findall')['matches'] == ['a']
        assert len(starts) == 1
        assert starts[0]['close_fds'] is True
        assert starts[0]['env'] == {}
    finally:
        regex_runner.close()


def test_timeout_reaps_worker_and_next_request_restarts():
    import regex_runner
    regex_runner.close()
    regex_runner.apply('a', 'a', 'findall')
    process = regex_runner._process
    with pytest.raises(regex_runner.RegexTimeoutError):
        regex_runner.apply('(a+)+$', 'a' * 1024 + '!', 'findall', .1)
    assert process.poll() is not None
    assert regex_runner.apply('b', 'b', 'findall')['matches'] == ['b']
    assert regex_runner._process.pid != process.pid
    regex_runner.close()
    assert regex_runner._process is None


def test_crashed_worker_restarts_without_echoing_input():
    import regex_runner
    regex_runner.close()
    regex_runner.apply('a', 'a', 'findall')
    process = regex_runner._process
    process.kill()
    process.wait()
    assert regex_runner.apply('b', 'b', 'findall')['matches'] == ['b']
    assert regex_runner._process.pid != process.pid
    regex_runner.close()


def test_timeout_and_output_errors_do_not_echo_text_or_pattern():
    import regex_runner
    secret = 'private-note-marker'
    for operation in ('findall', 'subn'):
        with pytest.raises(regex_runner.RegexTimeoutError) as error:
            regex_runner.apply('(?=private-note-marker)(?:.*a+)+$',
                               secret + 'a' * 1024 + '!', operation, .1)
        assert secret not in str(error.value)
        assert secret not in error.value.format()
    with pytest.raises(regex_runner.RegexOutputExceeded) as error:
        regex_runner.apply('a', 'aaa', 'subn', replacement=secret, max_output_bytes=1)
    assert secret not in str(error.value)
    assert secret not in error.value.format()
    assert regex_runner.apply('x', 'x', 'findall')['matches'] == ['x']
    regex_runner.close()


def test_abrupt_owner_loop_close_reaps_worker():
    import asyncio
    import regex_runner
    regex_runner.close()
    loop = asyncio.new_event_loop()
    try:
        regex_runner.bind_loop(loop)
        first_close = loop.close
        regex_runner.bind_loop(loop)
        assert loop.close is first_close
        result = loop.run_until_complete(asyncio.to_thread(
            regex_runner.apply, 'a', 'abc', 'findall'))
        assert result['matches'] == ['a']
        process = regex_runner._process
        loop.close()
        assert loop.is_closed()
        assert process.poll() is not None
        assert regex_runner._process is None
    finally:
        loop.close()
        regex_runner.close()


def test_close_interrupts_inflight_request_without_waiting_for_deadline(monkeypatch):
    import threading
    import regex_runner
    regex_runner.close()
    regex_runner.apply('a', 'a', 'findall')
    process = regex_runner._process
    written = threading.Event()
    original = regex_runner.os.write
    def write(fd, data):
        count = original(fd, data)
        written.set()
        return count
    monkeypatch.setattr(regex_runner.os, 'write', write)
    failures = []
    def apply():
        try:
            regex_runner.apply('(a+)+$', 'a' * 1024 + '!', 'findall', 30)
        except ValueError as error:
            failures.append(type(error).__name__)
    operation = threading.Thread(target=apply)
    closer = threading.Thread(target=regex_runner.close)
    operation.start()
    try:
        assert written.wait(2)
        closer.start()
        closer.join(1)
        assert not closer.is_alive(), 'shutdown waited for the regex deadline'
        assert process.poll() is not None
        operation.join(1)
        assert not operation.is_alive()
        assert failures == ['ValueError']
        assert regex_runner._process is None
    finally:
        if process.poll() is None:
            process.kill()
        operation.join(2)
        if closer.ident is not None:
            closer.join(2)
        regex_runner.close()
    assert regex_runner.apply('b', 'b', 'findall')['matches'] == ['b']
    regex_runner.close()
