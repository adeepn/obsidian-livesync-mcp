"""Exercise checker exit/marker contracts without network or credentials."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("tag,head,changed,api_failure,code,marker", [
    ("1.0.50", "a" * 40, "", False, 0, "UNCHANGED"),
    ("v1.0.52", "a" * 40, "", False, 2, "RELEASE_DRIFT"),
    ("1.0.50", "b" * 40, "", False, 0, "UNCHANGED"),
    ("1.0.50", "b" * 40, "M\tsrc/common/types.ts", False, 3, "PROTOCOL_DRIFT"),
    ("1.0.52", "b" * 40, "D\tsrc/common/types.ts", False, 3, "PROTOCOL_DRIFT"),
    ("1.0.50", "a" * 40, "", True, 4, "ERROR"),
])
def test_checker_status_contract(tmp_path, tag, head, changed, api_failure, code, marker):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts/check_upstream.sh"
    shutil.copy(source, scripts / source.name)
    (docs / "upstream-pin.md").write_text("plugin: 1.0.50\ncommonlib: " + "a" * 40 + "\n")
    (docs / "upstream-protocol-files.txt").write_text("# sample\nsrc/common/types.ts\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Replace only external boundaries. The real checker reads pins, parses the
    # manifest, chooses status precedence, prints results and returns its code.
    (binaries / "gh").write_text('''#!/usr/bin/env bash
set -eu
[[ "$API_FAILURE" == False ]] || exit 1
case "$*" in
  *releases/latest*) printf '%s\\n' "$TAG" ;;
  *commits/HEAD*) printf '%s\\n' "$HEAD_SHA" ;;
  *) exit 1 ;;
esac
''')
    (binaries / "git").write_text('''#!/usr/bin/env bash
set -eu
case "$3" in
  init|fetch|cat-file) exit 0 ;;
  diff) printf '%s' "$CHANGED" ;;
  *) exit 1 ;;
esac
''')
    for binary in binaries.iterdir():
        binary.chmod(0o755)
    result = subprocess.run(
        ["bash", str(scripts / source.name)], capture_output=True, text=True,
        env={**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
             "TAG": tag, "HEAD_SHA": head, "CHANGED": changed,
             "API_FAILURE": str(api_failure)},
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert f"STATUS: {marker}\n" in result.stdout
    if changed:
        assert changed in result.stdout
