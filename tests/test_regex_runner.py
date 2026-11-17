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
        regex_runner.apply('', 'a' * 100, 'subn', 2, replacement='x' * 200000)


def test_findall_unmatched_groups_match_python_contract():
    import regex_runner
    assert regex_runner.apply('(a)|(b)', 'ab', 'findall', 2)['matches'] == [
        ('a', ''), ('', 'b')]
