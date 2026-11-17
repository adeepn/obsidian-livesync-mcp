# Универсальный LiveSync MCP — Фундамент + CouchDB-бэкенд (План A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переписать слой хранения CouchDB-бэкенда и слой инструментов по аудиту и дизайн-документу, введя `StorageBackend`-интерфейс и общий модуль формата LiveSync — фундамент, на который в Плане B встанет S3-бэкенд.

**Architecture:** Три слоя: `livesync_format.py` (чистые функции формата LiveSync), `storage_base.py` (протокол `StorageBackend` + исключения), `storage_couchdb.py` (реализация поверх httpx). Инструменты в `server.py` работают только с интерфейсом. Frontmatter и ссылки вынесены в `frontmatter.py` / `links.py`.

**Tech Stack:** Python 3.11+, `mcp[cli]==1.30.0`, httpx, ruamel.yaml, python-dotenv, pydantic; тесты pytest + pytest-asyncio, CouchDB-протокол тестируется через `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-11-17-universal-livesync-mcp-design.md` (разделы §4.1–4.3, §5–§7). План B (S3 journal-пир, §4.4) — отдельный план после сдачи A.

## Global Constraints

- Python 3.11+; PEP 8; имена snake_case; тесты `tests/test_<module>.py`, pytest-стиль (AGENTS.md).
- `mcp[cli]==1.30.0` — точный пин (аудит A1); `pydantic>=2,<3`, `python-dotenv>=1,<2`, `ruamel.yaml>=0.18,<0.19`, `httpx` — из lock; dev: `pytest`, `pytest-asyncio`. Lock-файл `requirements.lock` через `pip freeze`.
- Коммиты Conventional (`feat:`, `fix:`, `chore:`, `docs:`, `test:`), один логический шаг = один коммит.
- Секреты (пароли, токены, `.env`) — никогда в коммитах, логах, сообщениях инструментов (аудит C7/E).
- Инварианты данных из спеки §7: никаких DELETE чанков; soft delete только; `size` = декодированное тело; ответ «успешно» ⇔ изменение зафиксировано.
- Существующий `tests/test_auth.py` должен проходить после каждого tasks.
- Внутренние URL/ревизии CouchDB не попадают в ответы инструментов (C7) — только в лог сервера.

## Review Focus

1. **Общий чанк двух заметок (аудит B1):** запись в `A.md` не должна отправлять ни одного DELETE на chunk-URL. Тест в Task 5 (assert по логу MockTransport).
2. **Параллельное изменение (409 на PUT метадокумента, C1/C2):** инструмент получает `ConcurrentModification` с человеческим текстом, заметка остаётся целой. Тест в Task 5.
3. **Frontmatter-неожиданности (D5/D6):** кавычки, `Re: meeting`, список без отступа, блочный скаляр, `before---after` — round-trip без изменений возвращает текст байт в байт. Тесты в Task 3.
4. **Побег из пространства имён (B7/B10):** `i:.obsidian/…`, `_design/x`, `h:…`, `.obsidian/…` — отказ `ForbiddenPathError` до любого HTTP-вызова. Тест в Task 2.
5. **Деструктивные аргументы инструментов (D1/D2/D3):** move на тот же путь/смену регистра, move/duplicate на существующую цель, update без mode — ни один не мутирует данные. Тесты в Task 6 и Task 9.

---

### Task 1: Пины зависимостей и конфигурация

**Files:**
- Modify: `requirements.txt`, `run.py`
- Create: `config.py`, `tests/test_config.py`, `.env.example`

**Interfaces:**
- Consumes: ничего.
- Produces: `config.load_config(env: Mapping[str, str]) -> Config` (dataclass: `backend: Literal["couchdb", "s3"]`, `couchdb: CouchDBConfig | None`, `s3: S3Config | None` — поля по спеке §6, но в Плане A валидируется только couchdb; `server_url, host, port, api_key, oauth_password, oauth_client_id, oauth_client_secret`); `ConfigError(Exception)` — сообщение человекочитаемо, без значений секретов.

- [ ] **Step 1: Написать тесты конфига**

`tests/test_config.py`: `test_backend_required` — пустой env → `ConfigError` с упоминанием `LIVESYNC_BACKEND`; `test_backend_unknown` — `LIVESYNC_BACKEND=foo` → `ConfigError`; `test_couchdb_complete` — `LIVESYNC_BACKEND=couchdb` + 4 `COUCHDB_*` → `Config.backend == "couchdb"`; `test_couchdb_missing_creds` — без `COUCHDB_PASSWORD` → `ConfigError`; `test_placeholder_api_key_rejected` — `MCP_API_KEY=your_api_key` → `ConfigError` (спека §6: отказ от плейсхолдеров); `test_no_auth_rejected` — без `MCP_API_KEY` и `OAUTH_*` → `ConfigError`.

- [ ] **Step 2: Прогнать, убедиться в FAIL**

Run: `python -m pytest tests/test_config.py -v` → FAIL (нет `config.py`).

- [ ] **Step 3: Реализовать `config.py`**

`load_config` читает env один раз; валидация в порядке: backend → креды бэкенда → auth; `S3Config` — dataclass-заглушка с полями (endpoint, access_key, secret_key, bucket, region, prefix, uri), парсинг `LIVESYNC_S3_URI` (формат `sls://user:pass@host?bucket=&region=&prefix=`, спека §6) — юнит-тест `test_s3_uri_parsed` на строке `sls://AK:SK@https://minio.local?bucket=vault&region=auto&prefix=` → поля разобраны; выбор s3-бэкенда в Плане A возвращает `ConfigError("S3 backend появится в Плане B")`.

- [ ] **Step 4: Прогнать тесты — PASS; обновить `requirements.txt`, `run.py`, `.env.example`**

`requirements.txt`: точные пины из Global Constraints; `run.py`: `load_dotenv()` первой строкой (аудит A2); `.env.example`: `LIVESYNC_BACKEND=`, взаимоисключающие группы bearer/OAuth закомментированы раздельно (A8). `python -m pytest tests/test_config.py tests/test_auth.py -v` → PASS.

- [ ] **Step 5: Commit**

`git add config.py tests/test_config.py requirements.txt run.py .env.example && git commit -m "feat: strict env config with backend selection and pinned deps"`

### Task 2: `livesync_format.py` — пути и пространство имён

**Files:**
- Create: `livesync_format.py`, `tests/test_livesync_format.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `path2id(path: str) -> str` (lowercase, ведущий `_` → `/_`, нормализация `/`, `./`, `\`; спека §4.1, аудит B9); `validate_vault_path(path: str) -> None` ( raises `ForbiddenPathError(Exception)`; префиксы `i: ix: ps: h: f: _`, точечные сегменты, `obsydian_livesync_version`, `syncinfo`; аудит B7/B10); `is_text_doc(doc_type: str | None, path: str) -> bool` (`plain` или расширение из `TEXT_EXTENSIONS = {".md",".txt",".svg",".html",".csv",".css",".js",".xml",".canvas"}`).

- [ ] **Step 1: Тесты путей (случаи сверить с векторами upstream-тестов `src/string_and_binary/path` из `livesync-commonlib` — [REUSE] спеки §10; недостающие векторы добавить)**

`test_path2id_basic`: `"Inbox/Note.md"` → `"inbox/note.md"`; `test_path2id_leading_underscore`: `"_inbox/x.md"` → `"/_inbox/x.md"`; `test_path2id_normalizes`: `"/Inbox//x.md"`, `"./Inbox/x.md"`, `"Inbox\\x.md"` → одинаковый id; `test_validate_forbidden`: `["i:.obsidian/x", "h:abc", ".obsidian/config", "_design/x", "f:abc", "obsydian_livesync_version", "syncinfo"]` — все `ForbiddenPathError`; `test_validate_ok`: `"Inbox/Note.md"`, `"_inbox/x.md"` — не падают; `test_is_text_doc`: `("plain", "x.bin")→True`, `("newnote","a.md")→True`, `("newnote","a.png")→False`, `(None,"a.md")→True`.

- [ ] **Step 2: FAIL → Step 3: реализация (чистые функции, без I/O) → Step 4: PASS**

Run: `python -m pytest tests/test_livesync_format.py -v`

- [ ] **Step 5: Commit** — `feat: add livesync path and namespace rules`

### Task 3: `livesync_format.py` — типы документов и чанки

**Files:**
- Modify: `livesync_format.py`, `tests/test_livesync_format.py`

**Interfaces:**
- Consumes: `is_text_doc`.
- Produces: `assemble_text(chunk_datas: list[str]) -> str`; `assemble_binary(chunk_datas: list[str]) -> bytes` (base64 поканально, аудит B4); `encode_newnote(data: bytes) -> list[str]` (один чанк, v1); `chunk_id_for(data: str) -> str` (`"h:" + sha256(data)[:16]`); `new_metadata_doc(path: str, children: list[str], size: int, doc_type: str, ctime_ms: int, mtime_ms: int) -> dict` (поля `_id, path, children, ctime, mtime, size, type, eden: {}`); `soft_delete_revision(doc: dict, mtime_ms: int) -> dict` (копия с `deleted: True`, тело сохраняется, аудит B2); `doc_type_for(path: str) -> str` — всегда `"plain"` для текста (B3).

- [ ] **Step 1: Тесты**

`test_assemble_binary_concat`: два чанка base64 (`"SGVsbG8="`, `"IFdvcmxk"`) → `b"Hello World"`; `test_assemble_binary_invalid` — не-base64 чанк → `ValueError`; `test_encode_newnote_roundtrip` с `assemble_binary`; `test_chunk_id_deterministic`: одинаковый контент → одинаковый id, начинается с `h:`; `test_metadata_doc_shape`: словарь содержит ровно ключи `{"_id","path","children","ctime","mtime","size","type","eden"}`, `eden == {}`, время в мс-числах; `test_soft_delete_keeps_body`: в копии `deleted is True`, `children` и `type` сохранены, оригинал не изменён; `test_doc_type_for`: `("x.md" и "x.base") → "plain"` (B3: любой текст — plain).

- [ ] **Step 2: FAIL → Step 3: реализация → Step 4: PASS**

Run: `python -m pytest tests/test_livesync_format.py -v`

- [ ] **Step 5: Commit** — `feat: add livesync document and chunk codecs`

### Task 4: `frontmatter.py` — round-trip YAML

**Files:**
- Create: `frontmatter.py`, `tests/test_frontmatter.py`

**Interfaces:**
- Consumes: ничего (ruamel.yaml round-trip).
- Produces: `split_frontmatter(text: str) -> tuple[str, str]` (разделители — только отдельные строки, аудит D6; без блока → `("", text)`); `get_properties(text: str) -> dict`; `set_properties(text: str, props: dict) -> str`; `delete_properties(text: str, keys: list[str]) -> str`; `add_tags(text: str, tags: list[str]) -> str` / `remove_tags(text: str, tags: list[str]) -> str` (теги — список строк, D12); `insert_after_frontmatter(text: str, insertion: str) -> str` (D7). Все мутаторы: если изменений нет — возвращают исходную строку (объектно равную).

- [ ] **Step 1: Тесты на кейсах аудита D5/D6/D12**

`test_roundtrip_unchanged` — тексты со `related: "[[Other]]"`, `title: 'Re: meeting'`, блочным скаляром `|`, вложенным словарём, комментарием, `'42'`, `01234`, пустым `aliases:` — `set_properties(t, {})` (no-op) возвращает исходный текст; `test_delimiters_whole_lines` — `summary: before---after` не разрывает блок (D6); `test_list_without_indent` — `tags:\n- a` разбирается как список; `test_set_preserves_neighbors` — установка одного ключа не меняет форматирование остальных строк; `test_remove_missing_is_noop`; `test_tags_scalar_to_list` — `tags: 2024` → список после add; `test_insert_after_frontmatter` — вставка идёт после `---`, не перед.

- [ ] **Step 2: FAIL → Step 3: реализация на ruamel (round-trip режим, `YAML(typ='rt', preserve_quotes=True)`) → Step 4: PASS**

Run: `python -m pytest tests/test_frontmatter.py -v`

- [ ] **Step 5: Commit** — `feat: round-trip frontmatter editing via ruamel`

### Task 5: `storage_base.py` + CouchDB-бэкенд — чтение

**Files:**
- Create: `storage_base.py`, `storage_couchdb.py`, `tests/test_storage_couchdb.py`
- Test helper: `tests/couchdb_fake.py` — фабрика `make_fake_couchdb(docs: dict[str, dict]) -> tuple[httpx.MockTransport, list[httpx.Request]]`, эмулирует `GET/PUT/DELETE/{db}/{id}`, `POST _all_docs` (и `{keys}`), `GET /{db}`, 404/409/401 по сцене.

**Interfaces:**
- Consumes: `livesync_format` (Task 2/3).
- Produces: исключения `NoteNotFound, NoteExists, BinaryNoteError, ConcurrentModification, UnsupportedVault, StorageUnavailable` (все `StorageError(Exception)`); TypedDict `NoteMeta {path, doc_id, ctime, mtime, size, type}`; `NoteContent {meta: NoteMeta, text: str | None, data: bytes | None, is_text: bool}`; протокол `StorageBackend` с методами спеки §4.2 (`startup_check, refresh, list_notes(folder, extension, recursive), get_meta, read_note, write_note, delete_note, rename_note, copy_note, iterate_texts, backend_info`); класс `CouchDBBackend(url: str, username: str, password: str, database: str, *, transport: httpx.AsyncBaseTransport | None = None)`.

- [ ] **Step 1: Тесты чтения (MockTransport)**

`test_startup_check_ok` — `GET /{db}` 200 → не падает; `test_startup_check_db_missing` — 404 → `StorageUnavailable` с текстом без URL (C7); `test_startup_detects_e2ee` — в `_local/obsydian_livesync_milestone` `tweak_values.encrypt=true` → `UnsupportedVault` (B5/B14); `test_list_notes_filters` — из документов `{h:chunk, i:conf, syncinfo, ix:settings.md, note.md(plain), img.png(newnote)}` возвращает только `note.md` и `img.png` (B8); `test_read_note_plain_chunks` — метадокумент с `children [h:a, h:b]` → склейка; `test_read_note_newnote_base64` — `data`-чанки собираются в байты, `is_text=False`; `test_read_note_deleted_is_none` — `deleted: true` → `read_note → None` (B12); `test_read_legacy_notes_type` — `type: "notes"` c `data` в документе → текст (B13); `test_read_missing_chunk_raises_storageerror` — чанк отсутствует → `StorageError` с путём заметки, не внутренним id (C4-совместимо).

- [ ] **Step 2: FAIL → Step 3: реализация**

`storage_couchdb.py`: низкоуровневый `_request` — единая точка маппинга ошибок (401/403→StorageUnavailable «ошибка авторизации CouchDB», 404→None/NoteNotFound, 409→ConcurrentModification, 5xx→StorageUnavailable; C7); `list_notes` — `POST _all_docs` пачками ключей без `include_docs` на чанках (C5); чтение чанков — `_bulk_get`/пачки `POST _all_docs {keys, include_docs}`; фильтр документов: `type in {plain, newnote, notes}` и id вне служебных префиксов.

- [ ] **Step 4: PASS** — Run: `python -m pytest tests/test_storage_couchdb.py -v`

- [ ] **Step 5: Commit** — `feat: couchdb backend read path over storage interface`

### Task 6: CouchDB-бэкенд — запись, удаление, перемещение

**Files:**
- Modify: `storage_couchdb.py`, `tests/test_storage_couchdb.py`, `tests/couchdb_fake.py` (счётчик ревизий, эмуляция 409)

**Interfaces:**
- Consumes: Task 5; `livesync_format.new_metadata_doc/soft_delete_revision/chunk_id_for/doc_type_for/encode_newnote`.
- Produces: реализация `write_note(path, content, *, create=False, overwrite=False) -> NoteMeta`, `delete_note(path) -> bool`, `rename_note(path, new_path, *, overwrite=False) -> NoteMeta`, `copy_note(path, new_path, *, overwrite=False) -> NoteMeta`.

- [ ] **Step 1: Тесты записи (главные — инварианты §7)**

`test_write_never_deletes_chunks` — предзаданы метадокумент и 2 чанка, `write_note` перезаписывает: в логе запросов ноль DELETE (B1); порядок: PUT чанков до PUT метадокумента (C1); `test_write_409_concurrent` — фейк отвечает 409 на PUT метадокумента → `ConcurrentModification` с текстом «изменена параллельно», чанки-сироты допустимы, метадокумент не тронут; `test_write_conflicts_refused` — GET с `conflicts=true` возвращает `_conflicts` → `ConcurrentModification` (C3); `test_create_existing_refused` — `create=True` на живой заметке → `NoteExists` (D2); `test_overwrite_missing_refused` — заметки нет, `overwrite` без `create` → `NoteNotFound`; `test_write_requires_text` — путь `.png` с текстом → `BinaryNoteError` (спека §5); `test_write_size_is_decoded_bytes` — кириллица: `size == len(content.encode())`; `test_delete_soft` — DELETE-запросов ноль; новая ревизия с `deleted: true`, `type/path/children` сохранены (B2); `test_rename_same_path_noop` — `rename_note("a.md","a.md")` → без HTTP-мутаций, возвращает мета (D1); `test_rename_case_only_updates_path` — `"A.md"→"a.md"`: одна ревизия, поле `path` обновлено, удалений нет (D1); `test_rename_target_exists_refused` без overwrite → `NoteExists` (D2); `test_rename_copies_children_and_ctime` — новый документ ссылается на те же `children`, `ctime` сохранён, контент не пересобирается (B4: бинарный файл переносится поканально); `test_resurrect_requires_create` — soft-deleted + `write_note` без create → `NoteNotFound` (B12).

- [ ] **Step 2: FAIL → Step 3: реализация**

Алгоритм записи: `validate_vault_path` → GET метадокумента (`conflicts=true`) → проверки create/overwrite/deleted → PUT новых чанков → PUT метадокумента с его `_rev` (новая ревизия = старое тело с заменёнными `children/mtime/size`, снятым `deleted`). `rename` = `copy` + `soft delete` старого, оба через метадокументы, чанки не трогаются.

- [ ] **Step 4: PASS** — Run: `python -m pytest tests/test_storage_couchdb.py -v`

- [ ] **Step 5: Commit** — `feat: couchdb backend write path with optimistic locking and soft delete`

### Task 7: `links.py` — резолвер ссылок и граф

**Files:**
- Create: `links.py`, `tests/test_links.py`

**Interfaces:**
- Consumes: ничего (чистые функции над текстом).
- Produces: `extract_links(text: str) -> list[Link]` (`Link = namedtuple("Link", ["target", "kind"])`, kind ∈ `{"wikilink","markdown","embed"}`; срез `#anchor` и `^block`, D8/D9); `resolve_link(target: str, paths: list[str]) -> str | None` (без учёта регистра: точное совпадение пути → basename → кратчайший уникальный путь); `build_graph(texts: Iterable[tuple[str, str]]) -> dict` (`{"nodes": [path], "edges": [{"from","to"}]}` — цели рёбер = разрешённые пути, embeds и повторы не дублируются).

- [ ] **Step 1: Тесты**

`test_extract_strips_anchors` — `[[Note#Heading]]`, `[[Note#^block]]`, `[[Note|alias]]`, `[x](Note.md)` → цель `Note`; `test_embed_kind` — `![[img.png]]` → kind embed; `test_resolve_by_basename` — `proj/Hub.md` разрешается из `[[Hub]]`; `test_resolve_shortest_unique`.

- [ ] **Step 2: FAIL → Step 3: реализация → Step 4: PASS**

Run: `python -m pytest tests/test_links.py -v`

- [ ] **Step 5: Commit** — `feat: obsidian-compatible link resolver`

### Task 8: `server.py` — инструменты чтения

**Files:**
- Modify: `server.py`
- Create: `tests/test_tools_read.py`, `tests/fake_backend.py` (in-memory `StorageBackend`: dict path→(meta,text|bytes); запись лога вызовов; мутабельные ошибки по сценарию)

**Interfaces:**
- Consumes: `StorageBackend` (Task 5), `frontmatter` (Task 4), `links` (Task 7), `config` (Task 1).
- Produces: `get_backend(cfg) -> StorageBackend` (фабрика в `server.py`; для `s3` — `RuntimeError` до Плана B); инструменты `obsidian_fetch, obsidian_search, obsidian_list_notes, obsidian_get_all_tags, obsidian_get_links, obsidian_get_link_graph, obsidian_find_orphaned_notes, obsidian_find_hub_notes, obsidian_vault_stats, obsidian_refresh` — сигнатуры (имена, параметры) не меняются.

- [ ] **Step 1: Тесты (вызов реальных async-функций из server.py с FakeBackend)**

`test_fetch_returns_raw_content` — `content` = полный текст файла включая frontmatter, `properties` — отдельное поле (D4); `test_fetch_binary_meta` — `.png`: метаданные + `size` + `sha256`, без тела (спека §5); `test_search_isolated` — одна заметка кидает `StorageError` → поиск возвращает результаты + `skipped: [path]` (C4); `test_list_notes_recursive_default` и `folder` фильтр по префиксу (D15: docstring честный, рекурсия по умолчанию); `test_vault_stats_no_size_crash` — заметка без `size` не роняет (B8); `test_hub_incoming` — сценарий аудита D8: пять заметок с `[[Hub]]` → `find_hub_notes` даёт `incoming == 5`, `Hub` не в сиротах; `test_backlinks_finds_anchor_links` (D9); `test_refresh_calls_backend_refresh` (no-op для CouchDB).

- [ ] **Step 2: FAIL → Step 3: реализация: переподключить инструменты на `backend`; `search/get_all_tags` — через `iterate_texts` (C4/C5); граф и обратные ссылки — `links.build_graph`/`resolve_link` из Task 7 → Step 4: PASS**

Run: `python -m pytest tests/test_tools_read.py tests/test_links.py -v`

- [ ] **Step 5: Commit** — `refactor: read tools over storage backend with link resolver`

### Task 9: `server.py` — пишущие инструменты

**Files:**
- Modify: `server.py`
- Create: `tests/test_tools_write.py`

**Interfaces:**
- Consumes: `StorageBackend`, `frontmatter`.
- Produces: `obsidian_create_note, obsidian_update_note, obsidian_delete_note, obsidian_move_note, obsidian_duplicate_note, obsidian_manage_properties, obsidian_manage_tags, obsidian_create_from_template, obsidian_list_templates` — сигнатуры сохраняются, кроме: `obsidian_update_note(mode: str, ...)` — `mode` без значения по умолчанию (D3).

- [ ] **Step 1: Тесты (FakeBackend; сценарии разделов 5 аудита как регрессии)**

`test_update_requires_mode` — вызов без mode → ошибка валидации; `test_update_empty_overwrite_refused` — `overwrite` с пустым `content` → ошибка, заметка не изменена (D3); `test_update_search_args_with_overwrite_refused` — `search`+`overwrite` → ошибка (D3); `test_search_replace_literal` — замена `\frac` вставляется буквально (D10); `test_search_replace_counts_matches` — `replace == search` не даёт ложное «No matches» (D11); `test_invalid_regex_handled` — невалидный шаблон → понятная ошибка, не traceback (D11); `test_fetch_edit_overwrite_keeps_frontmatter` — цикл fetch→overwrite сырого content не теряет свойства (D4); `test_prepend_after_frontmatter` (D7); `test_properties_set_on_canvas_refused` — только `.md` (D14); `test_properties_json_type_checked` — список вместо dict → ошибка (D14); `test_tags_add_on_binary_refused`; `test_after_heading_real_headings` — `#project` и код-блоки не заголовки, `## Tasks` находится (D13); `test_move_case_only`, `test_move_onto_existing_refused`, `test_duplicate_onto_existing_refused` (D1/D2); `test_binary_move_allowed` — фото переносится (FakeBackend с байтами); `test_success_only_after_write` — при `ConcurrentModification` от бэкенда инструмент возвращает ошибку, «Updated» не возникает (§7.6).

- [ ] **Step 2: FAIL → Step 3: реализация → Step 4: PASS**

Run: `python -m pytest tests/test_tools_write.py -v`

- [ ] **Step 5: Commit** — `fix: write tools data-safety semantics per audit`

### Task 10: Canvas-инструменты и мелкие фиксы

**Files:**
- Modify: `server.py`
- Create: `tests/test_tools_canvas.py`

**Interfaces:**
- Consumes: `StorageBackend`.
- Produces: `obsidian_canvas_node/obsidian_canvas_edge` — поведение по D16/D18/D19.

- [ ] **Step 1: Тесты:** `test_canvas_empty_objects` — `{}` без `nodes/edges` не падает (D16); `test_canvas_missing_file_add_creates`; `test_canvas_node_requires_id_for_remove`; `test_canvas_invalid_json_error`; `test_canvas_write_roundtrip` — JSON сохраняет ключи файла.
- [ ] **Step 2: FAIL → Step 3: реализация (`nodes = canvas.get("nodes", [])` и т.п.) → Step 4: PASS**

Run: `python -m pytest tests/test_tools_canvas.py -v`

- [ ] **Step 5: Commit** — `fix: canvas tools edge cases`

### Task 11: Auth-фиксы E1/E2 и запуск

**Files:**
- Modify: `oauth_provider.py`, `server.py` (`__main__` → transport как в `run.py`, A3), `README.md`
- Create: `tests/test_oauth_password_gate.py`

**Interfaces:**
- Consumes: существующий `SimpleOAuthProvider`.
- Produces: `/authorize` при заданном `OAUTH_PASSWORD` сначала отдаёт HTML-форму (POST с паролем, `hmac.compare_digest`), неверный пароль — 401; README: совет про DCR удалён, режимы bearer/OAuth описаны как взаимоисключающие (E2/A8).

- [ ] **Step 1: Тест:** `test_authorize_requires_password` — GET `/authorize?…` без сессии → HTML-форма (200); POST неверный пароль → 401; верный → редирект с кодом. `test_bearer_mode_untouched` — существующие тесты `tests/test_auth.py` PASS.
- [ ] **Step 2: FAIL → Step 3: реализация (форма — минимальный inline-HTML, без зависимостей) → Step 4: PASS** — Run: `python -m pytest tests/test_oauth_password_gate.py tests/test_auth.py -v`
- [ ] **Step 5: Commit** — `fix: enforce oauth password gate and remove DCR advice`

### Task 12: Смена бэкенда по конфигу, демонтаж старого клиента, документы

**Files:**
- Modify: `server.py` (замена `CouchDBClient` на фабрику `get_backend(cfg)`), `README.md`, `CHANGELOG.md`, `AGENTS.md`
- Delete: `couchdb.py`

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: сервер стартует от `LIVESYNC_BACKEND`; `vault_stats` содержит `backend`; старый клиент удалён.

- [ ] **Step 1: Тест:** `test_get_backend_couchdb` → экземпляр `CouchDBBackend`; `test_get_backend_s3_pending` → `RuntimeError` про План B; smoke: `python -c "import server"` на реальном `mcp` 1.30.0 в обоих режимах auth (bearer с временным ключом) — импорт без исключений (A1-регрессия).
- [ ] **Step 2: FAIL → Step 3: реализация + `git rm couchdb.py`; значения по умолчанию в коде и README выровнять (COUCHDB_URL → 5984, defaults задокументированы — аудит A4); README: раздел «Backends» (CouchDB сейчас, S3 — План B), таблица env-переменных, честные шаги установки; CHANGELOG: запись о переписи слоя хранения и фиксов аудита; AGENTS.md: новая структура файлов и команды тестов → Step 4: PASS всего набора**

Run: `python -m pytest -v` — все файлы PASS.

- [ ] **Step 5: Commit** — `feat: select storage backend from config; remove legacy couchdb client`

### Task 13: Интеграционный smoke на реальной CouchDB (env-gated)

**Files:**
- Create: `tests/test_integration_couchdb.py`

**Interfaces:**
- Consumes: `CouchDBBackend`.
- Produces: интеграционный прогон, пропускается без `COUCHDB_TEST_URL`.

- [ ] **Step 1: Тест (mark.skipif не задан env):** создать базу `mcp_test_<rand>`; через бэкенд: create → read (текст совпал) → update → rename → duplicate → soft delete; инвариант: после всех операций в `_all_docs` ноль DELETE-следов и ноль удалённых чанков; удалить базу. Креды — `COUCHDB_TEST_URL/USER/PASSWORD`.
- [ ] **Step 2: Запустить локально против docker CouchDB, зафиксировать вывод** — артефакт: `pytest -v -s` лог в `docs/test-artifacts/2026-11-17-couchdb-integration.txt` (анти-фабрикация). В CI без env — SKIP.
- [ ] **Step 3: Commit** — `test: env-gated integration suite against real couchdb`
