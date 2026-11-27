# Device selection runtime

`upstream.mjs` contains verbatim functions extracted from commonlib commit
484c164450d61deb2e301582b60cb9b5f9fa977f (MIT, see LICENSE-upstream).
Source URL: https://github.com/vrtmrz/livesync-commonlib/tree/484c164450d61deb2e301582b60cb9b5f9fa977f/src
Source SHA256 values are in upstream-sources.json. Only TypeScript types and
exports are changed. `runner.mjs` supplies ordinary-file allow/ignore ordering.

The four vendored npm dependencies are pinned to the upstream lockfile, with
archive URLs, SHA256 and upstream SHA512 integrity in sources.json. Distributed
ES modules and licenses are retained; no install scripts run. Nothing is fetched
at runtime. Node.js 22+ is required only when selection filters are enabled.

Regenerate from a clean checkout at the exact commonlib pin:

```
python scripts/vendor_selection.py /path/to/commonlib
node scripts/extract_selection_runtime.mjs /path/to/commonlib
node scripts/extract_selection_vectors.mjs /path/to/commonlib tests/fixtures/selection.json
```

The oracle generator executes the upstream EntryManager `isTargetFile`,
`getFileRegExp`, and `isAcceptedAll` directly, independently of runner.mjs.
Node 26.13.1 produced the committed fixtures. Production only executes JS;
TypeScript stripping is needed solely for regeneration (Node 22.13+).

Python regex and glob libraries are deliberately not used. RegExp has only the
upstream `i` flag or no flags: slash-delimited expressions are literal patterns,
not flag syntax. Unicode, global and sticky flags are never supplied by upstream.
Invalid expressions fail closed, matching upstream's thrown compilation error.
Execution failure or the five-second per-evaluation bound also disables writes
instead of approximating or accepting a path.
