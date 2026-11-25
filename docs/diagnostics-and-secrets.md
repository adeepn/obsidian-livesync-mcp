# Diagnostics and secrets

Tool diagnostics contain note paths as you supplied them, known setting key
names, sizes/counts, outcome/retry guidance and cause **class names**. Path
controls (C0/C1, CR/LF/tab, U+2028/U+2029, bidi U+202A–U+202E and U+2066–U+2069) become visible escapes; paths are
never credential-redacted or shortened. Paths read from remote metadata also
use NFC normalization. Zero-width U+200B/U+200C/U+200D and BOM U+FEFF pass
through unchanged: they can belong to a note identity and do not reorder text.
Structured JSON path fields keep the exact path; free-text lines are escaped.
Note contents remain note contents.

HTTP 400 and 413 advise against retrying unchanged requests; 413 suggests
splitting content or raising the server limit. HTTP 405 reports a method/configuration
mismatch, and 429 advises waiting before retrying. HTTP 408 reports a timeout:
for mutating PUT/POST/DELETE requests the write outcome is unknown, so re-read
before retrying. Reads can be retried later. HTTP 500/503 retain the existing
unknown-write-outcome or unavailable-read guidance; unfamiliar statuses retain
generic guidance.

Transport failures retain the HTTP status and operation (`GET`, `PUT`, `POST`,
`DELETE`, `HEAD`, `LIST`) without response bodies or headers. Numeric diagnostic
fields declare their type and range: counts, byte sizes/limits, depths and HTTP
statuses are integers in `0 <= x < 2**63`; measured limits
(`syncMaxSizeInMB`) and elapsed seconds accept integers or finite floats in the
same range. Process exit statuses are integers in `[-256, 256]`, including
negative signal statuses. Booleans are excluded everywhere. Invalid values
become `<invalid>` at construction and are revalidated at rendering. Measured
values render as decimal numbers without exponent notation or trailing `.0`. Cause
classes use `[A-Za-z_][A-Za-z0-9_]{0,63}`; other names become
`<unexpected cause>`. Setting keys, budget names, reasons and algorithms use
closed inventories; operation names use the closed inventory above. If outcome
reporting itself fails, the tool reports that the operation's result is unknown
and asks the caller to re-read before retrying. Exception details stay in logs.

Diagnostics never include remote exception text, response bodies/headers,
configured URLs, buckets/regions, passwords, passphrases/salts, setting values,
selection patterns or ignore-file contents. Unknown keys are `<unexpected key>`;
unknown journal names expose only their length. Known journal filename shapes
and closed algorithm/enum names are allowed. `backend_info.epoch` is a short
SHA-256 fingerprint, never the journal salt. Local cache paths/database names
are hidden, remote update sequences are fingerprinted, and unfamiliar metadata
fields are counted rather than printed.

Storage and selection logs omit payloads and tracebacks by default. Reporting
failures log full exception details. Lifespan exceptions propagate to the hosting
framework, which controls startup traceback logging. `LIVESYNC_UNSAFE_DEBUG=1`,
combined with a DEBUG log level, explicitly enables the bounded last-line debug
sink. **Unsafe for production:** it admits unstructured diagnostic inputs to a
debug path; never enable it to collect credentials. The current last-line
redactor inspects at most 2048 characters and withholds the entire nonempty
free-text value; tool diagnostics do not use this sink. This is deliberate: a detector
cannot reliably identify every possible secret encoding.


Custom proxy header credentials (`COUCHDB_CUSTOM_HEADERS`, `S3_CUSTOM_HEADERS`)
never enter the public diagnostic inventory. Configuration errors contain only
the variable, entry ordinal and violated rule/recovery guidance. Connection
observability adds `custom_headers_configured` (boolean) and
`custom_headers_count` (nonnegative integer) only. SDK/HTTP loggers are silenced
process-wide after constructing a connection with custom headers, including
free-form transport debug tracebacks; application logs retain typed fields.

Startup configuration validation errors name the variable and its closed rule or
supported choices, never the supplied value. This includes invalid `MCP_PORT`
and `LIVESYNC_BACKEND` values printed by `run.py` or `server.py`; correct the
named variable and restart. Replay-limit validation passes an explicit public
message field to configuration validation rather than formatting its exception.
