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
