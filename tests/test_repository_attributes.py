"""Line endings of a checkout are fixed by .gitattributes, not by local Git configuration."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, check=True, cwd=ROOT).stdout


def tracked_text_files():
    listing = git('ls-files', '--eol').decode()
    return [line.split('\t', 1)[1] for line in listing.splitlines() if line.startswith('i/lf')]


def test_checkout_keeps_committed_bytes_under_autocrlf():
    # Generators, harnesses and fixtures are verified by the hash of their bytes.
    # A checkout made with core.autocrlf=true must reproduce the committed bytes.
    names = tracked_text_files()
    assert len(names) > 200
    for name in names:
        committed = git('cat-file', 'blob', 'HEAD:' + name)
        checked_out = git('-c', 'core.autocrlf=true', 'cat-file', '--filters', '--path=' + name, 'HEAD:' + name)
        assert checked_out == committed, name
