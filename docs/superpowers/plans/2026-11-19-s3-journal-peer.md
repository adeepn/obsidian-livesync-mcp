# S3 Journal Peer (Plan B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать `JournalBackend` — S3 journal-пир Self-hosted LiveSync (replay журналов в локальное состояние, append-записи с rev-цепочками), встраиваемый в универсальный MCP за существующий `StorageBackend`.

**Architecture:** Replay: `ListObjectsV2(StartAfter=checkpoint)` → download → inflate → `journal_codec` → `journal_store` (SQLite rev-деревья + content-addressed чанк-стор на диске). Запись: новая ревизия от локального winner → JSONL+deflate → PUT `<ms>-docs.jsonl.gz` с монотонным именем → контрольное чтение-назад. S3-доступ через тонкий протокол `JournalObjectStorage` (boto3-адаптер + in-memory fake для тестов). NFC path identity — поддерживаемый режим (боевой vault пользователя NFC; поправка спеки 2026-11-19).

**Tech Stack:** Python 3.11+, boto3 (единственная новая runtime-зависимость), sqlite3 (stdlib), zlib (stdlib); тесты pytest/pytest-asyncio + MinIO (docker, env-gated).

**Spec:** `docs/superpowers/specs/2026-11-17-universal-livesync-mcp-design.md` §4.4 (с поправкой о NFC), §4.1, §7; контракты ревизий — `storage_base.py` docstrings (Plan A, волны A/B). Upstream-референс: клон `vrtmrz/livesync-commonlib` (перезаклонировать в /tmp/ls-commonlib, если стёрт; ключевые файлы: `src/replication/journal/{JournalSyncCore,journalRecords,journalReceive}.ts`, `objectstore/MinioStorageAdapter.ts`, `src/common/models/tweak.definition.ts`).

## Global Constraints

- Python 3.11+; PEP 8; тесты `tests/test_<module>.py`; вся-suite зелёная перед каждым коммитом (`./venv/bin/python -m pytest -q`, сейчас 535+1+3; boto3 добавить в requirements.txt с точным пином и в requirements.lock через pip freeze).
- Инварианты спеки §7: НИКОГДА не удаляем и не перезаписываем объекты журнала в S3 (DELETE запрещён даже в тестах прод-путей); success ⇔ прочтённый назад наш файл декодируется; `size` = декодированное тело; имена журналов с ведущим `_` — служебные объекты (milestone, sync-params), никогда не парсить как журнал.
- Контракты Plan A (волны A/B): `NoteContent.rev` — обязательный opaque-токен существующей заметки; `expected_rev` сравнивается с локальным winner под локальным lock'ом; `RenamePartialError(source, destination, completed_phase, hint)`; `iterate_texts(extension=".md") -> AsyncIterator[tuple[NoteMeta, str | None]]` (None = skipped, ошибка залогирована, не поднята).
- Секреты не попадают в коммиты/логи/артефакты; сообщения об ошибках без внутренних URL/rev.
- Conventional Commits; один логический шаг = один коммит; Conventional-префиксы `feat:/fix:/test:/docs:/chore:`.
- Живой Obsidian-клиент в автотестах не участвует — только в Task 10 (ручная приёмка по чек-листу, артефакты обязательны).

## Review Focus

1. **Deflate-обёртка на реальных файлах клиента** (pako vs zlib raw vs zlib-wrapped): inflate должен падать ГРОМКО на несовместимом blob, не выдавать мусор; Task 2 пинит zlib-wrapped (wbits по умолчанию) + тест на capture-файле из Task 10 попадает в suite как golden.
2. **NFD-имя файла при NFC-режиме**: `path2id("cafe\u0301.md", nfc=True)` обязан совпасть с id клиента (`café.md`); Task 1 пинит векторами NFD→NFC, mixed-compose, ASCII.
3. **Имена журналов не сортируются лексикографически как числа** (13-значные ms): порядок обработки — числовой по префиксу имени; одинаковый ms у нашего файла и чужого → монотонный guard имени (Task 7) и числовая сортировка листинга (Task 5), дубликат ключа в листинге не роняет replay.
4. **Epoch/wipe посреди жизни**: смена `protocolVersion:salt` или исчезновение нашего последнего отправленного файла → авто-сброс состояния и полный re-replay, а не вечное чтение призрака (Task 3 + Task 5 тесты).
5. **Sibling-ревизии после нашей записи** (устройство дописало журнал между refresh и write): оба листа живут, winner детерминирован (макс. поколение, tie → лексикографически больший rev id), листинг не дублирует заметку, read возвращает winner (Task 7 тест).

---

### Task 1: NFC path identity + S3-поля конфига

**Files:**
- Modify: `livesync_format.py`, `config.py`, `tests/test_livesync_format.py`, `tests/test_config.py`
- Reference: /tmp/ls-commonlib/src/common/models/tweak.definition.ts (pathNormalizationVersion), src/string_and_binary/path.ts

**Interfaces:**
- Produces: `path2id(path: str, *, nfc: bool = False) -> str` (NFC-вариант: `unicodedata.normalize("NFC", path)` до существующей нормализации/lowercase; семантика идентична upstream v1); `PATH_IDENTITY_LEGACY = "legacy"`, `PATH_IDENTITY_NFC = "nfc"`; `resolve_path_identity(milestone_settings: dict) -> str` — из разрешённых настроек milestone (PREFERRED→устройства) возвращает "nfc"|"legacy", бросает `UnsupportedVault` (причина в тексте) на `encrypt`, `usePathObfuscation`, `handleFilenameCaseSensitive`; `S3Config` расширяется полями `state_dir: str = "./data"` и `refresh_min_interval_sec: float = 5.0` (env `S3_STATE_DIR`, `S3_REFRESH_MIN_INTERVAL_SEC`, валидация: положительное число, путь не внутри репо; LoadError тексты без значений секретов).

- [ ] **Step 1: failing tests** — `test_path2id_nfc_composes`: `path2id("cafe\u0301.md", nfc=True) == path2id("café.md", nfc=True)`; `test_path2id_nfc_ascii_unchanged`; `test_path2id_legacy_keeps_nfd` (без nfd-схлопывания в legacy); `test_resolve_identity_preferred_nfc`, `test_resolve_identity_device_fallback`, `test_resolve_identity_refuses_encrypt/obfuscation/case_sensitive` (все → UnsupportedVault с названной причиной), `test_resolve_identity_no_settings_fails_closed`; конфиг: `test_s3_state_dir_default_and_env`, `test_s3_refresh_interval_validation`.
- [ ] **Step 2: verify FAIL** → Run: `./venv/bin/python -m pytest tests/test_livesync_format.py tests/test_config.py -q`
- [ ] **Step 3: implement** (NFC — единственная новая нормализация; не менять legacy-поведение — существующие тесты Section Task-2-Plan-A остаются зелёными без правок).
- [ ] **Step 4: verify PASS** → тот же прогон + полная suite.
- [ ] **Step 5: Commit** — `feat: nfc path identity mode and s3 state config`

### Task 2: journal_codec

**Files:**
- Create: `journal_codec.py`, `tests/test_journal_codec.py`
- Reference: /tmp/ls-commonlib/src/replication/journal/journalRecords.ts (UNIT_SPLIT=`\u001f`, RECORD_SPLIT=`\n`, компакт-чанки `~<id>\x1f<escaped>`, метаданные — JSON; escape-функции — сверить в src/common/utils.ts: escapeNewLineFromString/unescape), JournalSyncCore.ts (`wrappedDeflate` level 8)

**Interfaces:**
- Produces: `@dataclass JournalFile: name: str; chunks: dict[str, str]; docs: list[dict]`; `serialize_journal(chunks: list[tuple[str, str]], docs: list[dict]) -> bytes` (JSONL-байты: сначала чанки компакт-форматом, потом метаданные; метадок обязан иметь `_rev` строку — ValueError иначе); `deflate_journal(payload: bytes) -> bytes` (zlib level 8, обёртка zlib по умолчанию); `inflate_journal(blob: bytes) -> bytes` (ValueError «incompatible or corrupt journal blob» на ошибку zlib — громко, не мусор); `parse_journal(data: bytes) -> JournalFile` (построчный разбор; чанк без `_rev`; невалидная запись → ValueError с именем файла и номером строки); `journal_filename(ms: int, counter: int = 0) -> str` → `"{ms}-{counter:04d}-docs.jsonl.gz"` если counter>0 иначе `"{ms}-docs.jsonl.gz"`.

- [ ] **Step 1: failing tests** — roundtrip serialize→deflate→inflate→parse (2 чанка с `\n` и `\u001f` внутри data + 2 метадока с кириллицей); векторы формата записи: точные байты `~h:abc\x1fdata\n` и JSON-строки; `test_parse_rejects_missing_rev`, `test_parse_line_number_in_error`, `test_inflate_garbage_raises_loudly`, `test_filename_monotonic_suffix`; пара (serialize+deflate) стабильно короче сырых payload (сжатие реально).
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS** (unit-прогон + suite).
- [ ] **Step 5: Commit** — `feat: journal record codec`

### Task 3: journal_store (локальное состояние)

**Files:**
- Create: `journal_store.py`, `tests/test_journal_store.py`

**Interfaces:**
- Consumes: `livesync_format` (классификаторы документов, `assemble_text/assemble_binary`, `extract_inline_chunks`, `legacy_body`, path identity из Task 1).
- Produces: `pick_winner(leaves: list[dict]) -> dict` (макс. поколение rev `<int>-<hash>`, tie → лексикографически больший hash; чистая функция); `class JournalStore(state_dir: Path, path_identity: str)`:
  - `apply_journal(name: str, chunks: dict[str, str], docs: list[dict]) -> None` — идемпотентно (повтор файла ничего не меняет): upsert чанков (content-addressed файлы `chunks/hh/xxxx…` в state_dir, дедуп по id), метадоки — insert-if-missing leaf в rev-дерево SQLite (`docs(doc_id, gen, rev_hash, doc_json)` + индекс), транзакция на файл;
  - `processed(name) -> bool` / `mark_processed(name)`; `last_sent_key: str` (get/set); `epoch: str` (get/set; смена → `reset()` — чистит SQLite+чанки, сам сброс логируется);
  - `winner(doc_id) -> dict | None`; `note_ids() -> list[str]` (документы-заметки по классификаторам livesync_format, tombstone-winner → не входит); `read_note(doc_id) -> tuple[dict, str | None, bytes | None, bool] | None` (meta, text, data, is_text; сборка children+eden с порядком);
  - `iterate_notes(extension: str)` — генератор (meta, text|None) с изоляцией ошибок per-note.
  - SQLite: WAL, `PRAGMA synchronous=NORMAL`, схема миграций не нужна (v1).

- [ ] **Step 1: failing tests** — таблица winner-выбора (gen 3>2; tie → больший hash; пусто → None); merge двух журналов: второй файл с sibling-листом той же заметки → оба листа в БД, winner один, `note_ids` без дублей; tombstone-победитель скрывает заметку; идемпотентность `apply_journal`; `epoch`-смена стирает всё (проверка файлов чанков тоже); NFC-режим: doc_id с NFC уже приходит готовый из журнала — store не производит id (только хранит) — тест, что store не ломает unicode id; изоляция: битая заметка (отсутствующий чанк) → text=None, остальные читаются.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS**.
- [ ] **Step 5: Commit** — `feat: journal local store with revision trees`

### Task 4: JournalObjectStorage — протокол, fake, boto3

**Files:**
- Create: `object_storage.py`, `tests/test_object_storage.py`, `tests/fake_object_storage.py`
- Modify: `requirements.txt`, `requirements-dev.txt` (нет), `requirements.lock`

**Interfaces:**
- Produces: `class JournalObjectStorage(Protocol)` с `get(key: str) -> bytes | None`, `put(key: str, data: bytes) -> None`, `list(start_after: str = "", limit: int | None = None) -> list[str]`, `exists(key: str) -> bool`; `class Boto3ObjectStorage(cfg: S3Config)` (endpoint/path-style `True` по умолчанию/region/prefix; таймауты connect=5/read=30; list — ListObjectsV2 со StartAfter и пагинацией, ключи БЕЗ префикса; get — NoSuchKey → None; никаких delete-методов в протоколе ВООБЩЕ); `class FakeObjectStorage` (in-memory dict, валидация ключей, scriptable failures: `fail_next_get(n)`, задержки для тестов конкурентности).

- [ ] **Step 1: failing tests** — fake: put/get/exists/list с StartAfter (лексикографика S3 — проверить на ключах `9-…` vs `10-…`, документировать ЧИСЛОВУЮ сортировку как обязанность вызывающего); boto3-адаптер: смоук против env-gated MinIO (`MINIO_TEST_ENDPOINT/KEY/SECRET/BUCKET`, skipif), roundtrip put→list→get; lock-файл протокола: `not hasattr(Boto3ObjectStorage, "delete")` пинится тестом (инвариант §7).
- [ ] **Step 2: FAIL → Step 3: implement (boto3 установить в venv, запинить, pip freeze → lock) → Step 4: PASS**.
- [ ] **Step 5: Commit** — `feat: object storage protocol with boto3 adapter`

### Task 5: JournalBackend — startup + replay/refresh

**Files:**
- Create: `storage_journal.py`, `tests/test_storage_journal.py`
- Reference: JournalSyncCore.ts ( `_getRemoteJournals` — StartAfter от максимума полученных; `_obsidian_livesync_journal_sync_parameters.json` — сверить точное имя константы DOCID_JOURNAL_SYNC_PARAMETERS в commonlib types.ts; milestone `_00000000-milestone.json`; `ensureCheckpointCachesAreFresh` — логика epoch-пробы последнего отправленного файла)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `class JournalBackend(object_storage: JournalObjectStorage, state_dir: Path, refresh_min_interval: float = 5.0)` (реализует `StorageBackend` целиком; `JournalStore` создаётся ВНУТРИ при первом `startup_check` — path identity узнаётся из milestone; Task 5 покрывает `startup_check`, `refresh`, `backend_info`; чтения/записи — Tasks 6/7, до них `NotImplementedError`).
  - `startup_check()`: прочитать milestone → `locked` → UnsupportedVault; настроек нет нигде при наличии журналов → UnsupportedVault («Rebuild from the authoritative Vault»); `resolve_path_identity` → создать store (повторный startup с существующим store проверяет совпадение identity — расхождение = ошибка конфигурации state_dir); прочитать sync-params → `epoch` = `f"{protocolVersion}:{pbkdf2salt}"`; проба `last_sent_key` (файл исчез → `store.reset()` + полный re-replay).
  - `refresh()`: троттлинг (не чаще refresh_min_interval; принудительный — `refresh(force=True)`); list новых журнальных ключей (не `_`-префиксных) после последнего обработанного, ЧИСЛОВАЯ сортировка по ms-префиксу; на каждый: get → inflate → parse → `apply_journal` → mark_processed; ошибка одного файла → останов БЕЗ продвижения checkpoint (следующий retry с того же файла), исключение наверх как StorageError с именем файла.

- [ ] **Step 1: failing tests** — startup: locked-мильстон → UnsupportedVault; NFC-мильстон + legacy-store → конфиг-ошибка; sync-params с epoch A, повторный startup с epoch B → reset+re-replay (fake: все журналы читаются заново, state чист); journals-without-milestone → отказ; replay: 3 файла по порядку + повторный refresh ничего не делает; числовой порядок (`"9-docs.jsonl.gz"` обрабатывается раньше `"10-docs.jsonl.gz"`); битый второй файл → StorageError, checkpoint не двинулся, третий не применён; `_`-объекты (milestone/sync-params) не парсятся; троттлинг: второй refresh в интервале — no-op (fake list не вызывался).
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS**.
- [ ] **Step 5: Commit** — `feat: journal backend startup and replay`

### Task 6: JournalBackend — read paths

**Files:**
- Modify: `storage_journal.py`, `tests/test_storage_journal.py`

**Interfaces:**
- Consumes: store-методы Task 3; контракты `NoteMeta/NoteContent/iterate_texts` из storage_base (rev = строка winner `<gen>-<hash>`).
- Produces: реализация `list_notes(folder, extension, recursive)`, `get_meta`, `read_note`, `iterate_texts` (окнами, изоляция per-note — Ruling-1), `backend_info` ({"backend": "s3-journal", "path_identity": …, "epoch": …, "files_processed": N}); `close()`.

- [ ] **Step 1: failing tests** — поверх fixture из 5 заметок (2 папки, кириллица, бинарник newnote, tombstone): list с фильтрами/recursive=False/без регистра (folder-фильтр lowercase — паритет с CouchDB-бэкендом); read_note текст/бинарник/отсутствующая/tombstone→None; rev непустой у существующих; iterate_texts: text=None не роняет итератор, skipped виден; канонические пути отображения (display path из doc.path).
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS**.
- [ ] **Step 5: Commit** — `feat: journal backend read paths`

### Task 7: JournalBackend — write path

**Files:**
- Modify: `storage_journal.py`, `tests/test_storage_journal.py`

**Interfaces:**
- Consumes: journal_codec, pick_winner, path identity.
- Produces: реализация `write_note(path, content, *, create=False, overwrite=False, expected_rev=None)`, `delete_note(path, expected_rev=None)`, `rename_note`, `copy_note` (поднятие `RenamePartialError` маппится на фазы: у нас фаза одна — «target_published»; семантика rename в append-мире: новый док + soft-delete старого ОДНИМ журнальным файлом — атомарно по файлу).
  - Алгоритм записи: validate_vault_path → локальный winner (expected_rev несовпадение → ConcurrentModification с текстом по контракту B1: «изменена параллельно; перечитайте и повторите») → новая ревизия `<gen+1>-<32 hex>` + `_revisions`-цепочка от winner → чанк(и) по chunk_id_for → serialize+deflate → имя `max(now_ms, числовой_максимум_всех_виденных_ключей + 1)` с монотонным counter процесса → put → НЕМЕДЛЕННО get+inflate+parse (несовпадение байтов → StorageError, «success» не возвращается) → apply_journal к store + mark_processed + last_sent_key.
  - Soft delete = ревизия с `deleted:true`, `type/path/children/eden` сохранены. Бинарные пути: BinaryNoteError (write — только текст; copy/rename переносят children+eden как есть).
  - Sibling-семантика: если после нашей записи приходит чужой журнал с конкур-листом — оба живут, winner детерминирован; ответ «успех» означает durable publication (контракт B1), не финального победителя.

- [ ] **Step 1: failing tests** — create/exists→NoteExists; overwrite-missing→NoteNotFound; resurrect только через create (B12); expected_rev stale → ConcurrentModification, заметка не изменена, НИКАКИХ put; полный цикл write→read-back (success только после контрольного чтения; fake с подменой контента при get → StorageError, не success); delete → tombstone-ревизия в store, read → None; rename same-path no-op / case-only / с overwrite-отказом; copy сохраняет children+eden+ctime (fixture с eden-only заметкой); монотонное имя: fake уже содержит ключ с now_ms → наш файл строго больше; sibling: после write применяем чужой журнал с конкурирующим листом → оба листа, winner по pick_winner, note_ids без дублей; `not hasattr`-инвариант: write-путь не вызывает delete у storage.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS**.
- [ ] **Step 5: Commit** — `feat: journal backend append-only write path`

### Task 8: фабрика, env, документация строк конфига

**Files:**
- Modify: `server.py` (get_backend: `"s3"` → JournalBackend; убрать RuntimeError-заглушку), `tests/test_tools_read.py` (test_get_backend_s3_pending → test_get_backend_s3), `.env.example`, `README.md`, `CHANGELOG.md`, `AGENTS.md`

**Interfaces:**
- Consumes: всё; `config.load_config` уже валидирует s3-группу (Task 1).
- Produces: `get_backend(cfg)` строит `Boto3ObjectStorage(cfg.s3) + JournalStore(Path(cfg.s3.state_dir), path_identity)`; path_identity не известен до startup_check → JournalBackend создаёт store лениво внутри startup_check (выбрасывает UnsupportedVault из factory-вызова startup); README: раздел S3 (URI или переменные, state dir, первый полный replay, обновление перед операциями, конфликт-семантика журнала), CHANGELOG unreleased, AGENTS.md — новые модули.

- [ ] **Step 1: failing tests** — `test_get_backend_s3` возвращает JournalBackend (с тестовым state_dir); env-пример валиден `load_config`; grep-assert: в коде и пользовательской документации не осталось заглушки недоступного S3 backend.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS + полная suite + smoke `python -c "import server"` без env (fail-closed по конфигу, не traceback).**
- [ ] **Step 5: Commit** — `feat: wire s3 journal backend into factory and docs`

### Task 9: Integration A — MinIO, автоматическая (env-gated)

**Files:**
- Create: `tests/test_integration_minio.py`

**Interfaces:**
- Consumes: JournalBackend + Boto3ObjectStorage.
- Produces: env-gated suite (`MINIO_TEST_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET`): (а) полный цикл: writer-бэкенд (state_dir №1) создаёт 5 текстовых заметок (frontmatter, кириллица, папки) → reader-бэкенд (свежий state_dir №2) делает startup+refresh → пофайловая сверка содержимого; (б) wipe: очистка bucket + смена sync-params epoch → startup reader'а → авто-сброс состояния и корректный пустой стейт; (в) прерывание: scripted-падение на 2-м из 3 файлов → рестарт → сходится; (г) sibling-сценарий: два writer'а с разными state_dir пишут одну заметку → оба листа в дереве, winner один, ни одна запись не потеряна. Артефакт: `docs/test-artifacts/2026-11-19-minio-integration.txt` (реальный вывод, креды отредактированы).

- [ ] **Step 1: написать тесты (skipif без env)**; **Step 2: docker MinIO** (`docker run -d --rm -p 19000:9000 -e MINIO_ROOT_USER=admin -e MINIO_ROOT_PASSWORD=<gen> minio/minio server /data` + mc create bucket), прогон с env, артефакт; **Step 3: без env — suite зелёная, интеграция SKIP**; **Step 4: Commit** — `test: env-gated minio integration for journal backend`.

### Task 10: Integration B — живой клиент, ручная приёмка (пользователь)

**Files:**
- Create: `docs/live-check-s3-checklist.md`; артефакты в `docs/test-artifacts/2026-11-19-live-s3/` (входят лог LiveSync + скриншоты + выводы скриптов)

**Interfaces:**
- Consumes: рабочий JournalBackend (Tasks 5–8), тестовый vault `/Users/adeep/Documents/TESTVault`, docker MinIO из Task 9.
- Produces: чек-лист и скрипты приёмки; КРИТЕРИЙ ГОТОВНОСТИ спеки §8.2. Шаги в чек-листе (исполнение — пользователь + контроллер):
  (а) пользователь переключает TESTVault на object storage (endpoint/bucket/ключи MinIO, prefix `livetest`), полный синк — bucket наполняется журналами НАСТОЯЩЕГО клиента;
  (б) контроллер: свежий state_dir, startup+refresh, сверка всех файлов vault хеш-в-хеш (скрипт по образцу live_check_phase1) + capture первого журнального файла как golden для codec-теста (committed fixture);
  (в) сервер пишет (create/update/move/soft-delete — скрипт phase2-аналог) → пользователь Replicate → подтверждение применения (в т.ч. что LiveSync НЕ отверг нашего «невидимого писателя»: лог без ошибок — иначе план-Д: регистрация узла в milestone, отдельная задача);
  (г) пользователь пишет с клиента → сервер refresh → видит;
  (д) артефакты: скриншот лога LiveSync после (в), выводы скриптов, итоговая строка PASS/FAIL.
- [ ] **Step 1: написать чек-лист + скрипты (`/tmp` или `scripts/live_check_s3.py` — по образцу couchdb-фазы)**; **Step 2: прогнать (а)-(д) с пользователем**; **Step 3: зафиксировать артефакты + golden fixture в tests/fixtures/**; **Step 4: Commit** — `docs: live s3 acceptance checklist and artifacts`.
