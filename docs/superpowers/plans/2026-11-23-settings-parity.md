# Settings-Parity: пир ведёт себя идентично плагину при любых настройках — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить все самодельные ограничения и решения пира: поведение при ЛЮБЫХ настройках vault (syncMaxSizeInMB, chunk-сплиттер, case-sensitive, NFC) идентично поведению плагина — те же чанки, те же id, те же решения plain/newnote, те же правила размеров; никаких самодельных капов.

**Architecture:** Три слоя работы: (1) извлечение точных правил из upstream-источников в spec-паритетности (артефакт с file:line цитатами); (2) перенос правил в чистые модули (`livesync_format` — identity/type/size rules; новый `content_splitter.py` — сплиттер v3 + xxhash64 id derivation); (3) бэкенды потребляют правила; самодельные капы и denylist'ы удаляются; лимиты replay выводятся из настроек vault'а.

**Tech Stack:** Python 3.11+, xxhash (новая зависимость — upstream `hashAlg: "xxhash64"`), pytest; upstream-референс /tmp/ls-commonlib.

**Spec:** источник истины — upstream-код (клон /tmp/ls-commonlib); артефакт Task 1 = `docs/superpowers/specs/2026-11-23-settings-parity.md` (таблица «настройка → поведение устройства → наш статус → план»); базовая спека `docs/superpowers/specs/2026-11-17-universal-livesync-mcp-design.md` правится Task 7.

## Global Constraints

- «Ни капли отличий»: каждое правило пишется ПО upstream-коду с цитатой file:line; вектор-тесты берутся из upstream-юнит-спек, где есть.
- Приложение-протокол не меняется: journal-файлы, имена ключей, escape — как в Plan B (B-4/B-8/B-10/B-11/B-13 остаются).
- Self-imposed капы запрещены: любой лимит либо выведен из настроек vault (milestone tweak_values), либо env-override без дефолтного потолка, либо обоснован цитатой из upstream.
- Область: case-sensitive и syncMaxSizeInMB и сплиттер и типы — В области; E2EE и path-obfuscation — остаются отказами (крипто-объём вне этого плана; вопрос владельцу на ревью плана).
- Существующие 932+ тестов зелёные после каждого tasks (поведенческие изменения — только там, где upstream требует).
- Conventional Commits; пин upstream-версии обновляется в docs/upstream-pin.md этим же планом (плагин 1.0.52, commonlib HEAD на дату работ).

## Review Focus

1. **Расхождение сплиттера с upstream на синтетике** — наш сплиттер обязан давать на одном входе те же границы кусков, что upstream ContentSplitter (векторы из upstream-спеков + свойства: min/max размеры куска по настройкам). Тест: Task 4.
2. **xxhash64-id derivation** — id чанков нашего писателя должны совпадать с тем, что сминтил бы клиент на том же куске (иначе дедупликация с устройствами не сработает — не порча, но явное отличие). Тест: Task 4 (вектор из upstream `idDerivation`-спеков).
3. **case-sensitive path identity** — `path2id` без lowercase при identity=case-sensitive: конфликтующие регистры должны работать как у устройства (два разных doc), включая NFC-комбинацию. Тест: Task 3.
4. **syncMaxSizeInMB — правило устройства для oversized** — Task 1 должен установить ТОЧНОЕ поведение устройства (refuse-add? skip-sync? send-local?) и наш write_attachment/write_note обязан воспроизвести его, а не наш прежний BinaryNoteError. Тест: Task 6.
5. **plain/newnote решение** — перенос правил upstream doc-type (по содержимому/расширению) вместо нашего denylist; регрессии: все прежние текстовые/бинарные сценарии + векторы upstream. Тест: Task 5.

---

### Task 1: Извлечение правил поведения из upstream (research, артефакт)

**Files:**
- Create: `docs/superpowers/specs/2026-11-23-settings-parity.md`

**Interfaces:**
- Produces: документ-таблица «настройка/вопрос → поведение upstream (file:line) → наш текущий статус → решение для пира», покрывающая: (а) ContentSplitter v3 (rabin-karp: параметры, для ТЕКСТА и для БИНАРНЫХ данных; minimumChunkSize/customChunkSize/longLineThreshold семантика); (б) chunk-id derivation (hashAlg xxhash64, idDerivationVersion, формат `h:…`); (в) решение plain vs newnote (какие расширения/содержимое); (г) case-sensitive path identity (отличия id derivation, коллизии); (д) syncMaxSizeInMB — точное правило устройства для oversized файлов (что с локальным файлом, что попадает в журнал, как выглядит приём на другом устройстве); (е) maxTotalLengthInEden/maxChunksInEden/useEden=false — влияние на journal-записи; (ж) обоснование: почему эти файлы — источник (версия/коммиты в docs/upstream-pin.md).

- [ ] **Step 1: Исследование** — читать /tmp/ls-commonlib (переклонировать при отсутствии): src/ContentSplitter/*, src/string_and_binary/* (path.ts, chunks.ts), src/common/utils.ts, src/common/models/tweak.definition.ts, src/replication/journal/* (как метаданные больших бинарников выглядят в журналах), docs/ upstream (specs_garbage_collection и др.).
- [ ] **Step 2: Контроль ревьюером** — dispatch codex: верифицировать каждую таблицу против источников, найти пропущенные настройки, влияющие на journal-поведение.
- [ ] **Step 3: Commit** — `docs: settings-parity behavior extraction from upstream`

### Task 2: case-sensitive path identity

**Files:**
- Modify: `livesync_format.py`, `storage_journal.py` (identity из milestone), `tests/test_livesync_format.py`, `tests/test_storage_journal.py`

**Interfaces:**
- Consumes: Task 1 §г.
- Produces: `path2id(path, *, nfc=False, case_sensitive=False)`; `PATH_IDENTITY_CASE_SENSITIVE = "case-sensitive"`; `resolve_path_identity` возвращает его (remove refusal); JournalStore/бэкенды прокидывают identity (все места, где сейчас lowercase-хардкод — включая folder-фильтры, канонические пути, дедупликацию имён).

- [ ] **Step 1: failing tests** — case-sensitive: `"A.md"` ≠ `"a.md"` как id; folder-фильтр учитывает регистр identity-зависимо; коллизии `"A.md"`/`"a.md"` — две разные заметки; NFC-вектор в NFC+case-sensitive комбинации.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** — `feat: case-sensitive path identity per vault settings`

### Task 3: ContentSplitter v3 + xxhash64 chunk ids

**Files:**
- Create: `content_splitter.py`, `tests/test_content_splitter.py`
- Modify: `livesync_format.py` (chunk_id_for → upstream derivation; remove sha256-16), `storage_couchdb.py`, `storage_journal.py` (писатели используют сплиттер), `tests/*`

**Interfaces:**
- Consumes: Task 1 §а/б (параметры и алгоритмы — единственный источник).
- Produces: `class ContentSplitter` (настройки из tweak_values: chunkSplitterVersion, minimumChunkSize, customChunkSize, longLineThreshold, hashAlg) с методами для текста и байтов: `split(data) -> list[piece]` (границы по rabin-karp v3 / правилам upstream, для бинарников — по их правилам); `chunk_id_for_piece(piece: bytes|str) -> str` (upstream xxhash64 derivation). Требование паритета: для любого входа наш сплиттер даёт те же границы, что upstream-сплиттер (вектор-тесты из upstream-юнит-спек; если upstream-векторов мало — построить дифф-тест через node: выполнить upstream-сплиттер под node и сравнить).

- [ ] **Step 1: failing tests** — векторы upstream (если есть в спеках) + node-дифф оракул: `node -e "import {ContentSplitterV3}..."` генерирует границы для 10 случайных входов (текст/бинарник, разные настройки) → наш сплиттер даёт идентичные куски и id.
- [ ] **Step 2: FAIL → Step 3: implement (rabin-karp по upstream-коду, xxhash64 через `xxhash` пакет — пин в requirements) → Step 4: PASS → Step 5: Commit** — `feat: upstream content splitter and chunk id derivation`

Статус Task 3 (поправка 2026-11-24, Ruling P-2/P-3): ядро v3+xxhash64 принято ревью (23 граничных вектора + 10 дифференциальных последовательностей), НО исполнитель ограничил сплиттер значениями `v3-rabin-karp`/`xxhash64` и применил ограничение на старте сервера. Это отменено: upstream выбираемы четыре сплиттера (`v1`, `v2`, `v2-segmenter`, `v3-rabin-karp`) и hashAlg `xxhash32`/`xxhash64`/`mixed-purejs`/`sha1`/`""`. Все воспроизводимы точно на пине (исследование: ledger-файл `codex-legacy-research.out`). Паритет недостижим, пока writer поддерживает одно значение из четырёх — поэтому план расширен задачами 3a–3c.

### Task 3a: чтение не зависит от writer-алгоритма (разделение гейта)

**Files:**
- Modify: `storage_couchdb.py`, `storage_journal.py` (gate на startup), `livesync_format.py` (`resolve_content_splitter`), `content_splitter.py`, `tests/*`

**Interfaces:**
- Produces: startup_check НЕ отказывает на неподдерживаемом writer-алгоритме (чтение, replay и реконструкция контента сплиттером не пользуются — работают для любого); неподдерживаемый для ЗАПИСИ алгоритм → чистая ошибка инструмента (StorageError-семейство, без traceback и без падения lifespan) ровно в момент вызова write-инструмента, с текстом «vault uses chunkSplitterVersion=X / hashAlg=Y; peer writes for this mode are not implemented yet — reads work»; `backend_info` отдаёт `writer_supported: bool` + причину. Numeric validation: `OverflowError`/`ValueError` на аномальных значениях customChunkSize/minimumChunkSize → нормализованы в keyed `ValueError` → UnsupportedVault/StorageError.

- [ ] **Step 1: failing tests** — vault с `chunkSplitterVersion=v2`: startup OK, list/read/replay работают, `writer_supported=false`; write_note → StorageError без публикации/мутаций, сервер (lifespan) не падает; `customChunkSize=1e308`, `minimumChunkSize=10**400` → чистая ошибка без OverflowError; при `v3`+`xxhash64` — `writer_supported=true`, запись работает.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** — `fix: decouple reads from writer algorithm support`

### Task 3b: остальные варианты hashAlg (точный паритет id)

**Files:**
- Modify: `content_splitter.py` (диспетчер хэшей), `tests/test_content_splitter.py`, `tests/fixtures/` (дифференциальные векторы)
- Reference: `C:managers/HashManager/{XXHashHashManager,PureJSHashManager}.ts`, `octagonal-wheels@0.1.57 hash/purejs.js`, `C:managers/HashManager/HashManager.unit.spec.ts`

**Interfaces:**
- Consumes: исследования (таблица точных конструкций id: xxhash32 `B36(XXH32(utf8(p+"-"+L)))`; legacy `""` → `B36(Int32(XXH32(utf8(p)) XOR L))` signed; mixed-purejs → `F(p+"-"+L)` с binary64-округлением и `Math.imul`; sha1 → стандартный base64 SHA-1 через `writeString` с веткой ≤128 UTF-16 юнитов).
- Produces: `chunk_id_for_piece(piece, *, hash_alg)` для всех пяти значений; векторы: `helloWorld` → `1n069eb` (xxhash32), `-vyucau` (legacy), `1b68nom123qfyd` (mixed), `2pGPxorWFQvCZq2k1uh76tH9ABc=` (sha1) + дифференциальный корпус против upstream-кода под Node (UTF-16 cut, суррогаты, пустая строка, CRLF, PDF/JSON-разделители) — та же инфраструктура, что в Task 3.
- Обязательные векторы: из `HashManager.unit.spec.ts:17–31,200–307`.

- [ ] **Step 1: failing tests** — все четыре таблицы-вектора; дифференциальный корпус; `backend_info` writer_supported для каждого hashAlg.
- [ ] **Step 2: FAIL → Step 3: implement (binary64-семантика для mixed через `struct`/`math` — обычное умножение с округлением, `Math.imul` через ctypes/инт) → Step 4: PASS → Step 5: Commit** — `feat: upstream chunk id derivation for all hash algorithms`

### Task 3c: сплиттеры V2, V2.5 и V1 (точный паритет границ)

**Files:**
- Create/Modify: `content_splitter.py` (классы по алгоритмам, общий препроцессинг), `tests/test_content_splitter.py`, `tests/fixtures/` (дифференциальные векторы)
- Reference: `C:string_and_binary/chunks.ts:13–72,218–254` (V1 текст), `:436–493` (V1 бинарник), `:256–323,341–434` (V2 текст/бинарник), `C:ContentSplitter/ContentSplitterV1.ts`, `ContentSplitterV2.ts`

**References:**
- `chunkSplitterVersion` dispatch: V1 принимает `"v1"`, `""`, `undefined`; V2 принимает `"v2"` и `"v2-segmenter"`; Rabin-Karp — `"v3-rabin-karp"`. ВНИМАНИЕ: `v2-segmenter` на этом пине ведёт себя ИДЕНТИЧНО V2 (аргумент `useSegmenter` не читается в `splitPieces2V2`) — реализовывать «лексическую» сегментацию по лейблу UI было бы отличием от устройства; паритет = точная копия V2. ICU-зависимый helper сегментации предложений недостижим при нормальном dispatch и в паритет не входит (если будущий upstream начнёт его использовать — сторож upstream-watch должен это поймать: `chunks.ts` в манифесте, Task 7).

**Interfaces:**
- Produces: `ContentSplitter.split` диспетчеризует по `chunkSplitterVersion` на V1/V2/V3 (V2.5 → V2); общий препроцессинг `pieceSize = floor(102400 × (customChunkSize + 1))`, `plainSplit = path ends with .md/.txt/.canvas` (case-sensitive). Quirks, которые обязаны сохраниться (из исследования): V1 — fence-цикл, regex `/(.*?[;,:<])/g`, cut перед разделителем, LF-fallback без обновления splitSize, пустой бинарник → одна пустая base64-строка; V2 — строгий `>` при flush, adaptive minimum, бинарник ищет preferred-разделитель во всём буфере от `offset+minimum` (дальний preferred подавляет ближний LF); worker vs direct на пустом входе (`""`).
- Доказательство паритета: upstream не содержит точных граничных векторов для V1/V2 → единственное доказательство — дифференциальный корпус против pinned upstream-кода под Node (сгенерированные фикстуры коммитятся: границы+id для корпуса входов).

- [ ] **Step 1: failing tests** — дифференциальный корпус V1/V2/V2.5 (текст с fences/заголовками `#`, plain и не-plain, UTF-16 суррогатные пары на границе cut, CRLF, пустой ввод; бинарники `.pdf`/`.json`/прочие с разделителями NUL, `/`, `,`, LF; переходы minimum; customChunkSize 0/1/N); V2.5 == V2 на всём корпусе; V3 — прежние векторы без регрессии.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** — `feat: upstream v1 and v2 content splitters`

### Task 4: plain/newnote решение по правилам upstream

**Files:**
- Modify: `livesync_format.py` (`doc_type_for` → upstream-правила; `is_writable_text_path` → заменить на upstream-решение), `storage_base.py` (write_note content: `str | bytes`; write_attachment сворачивается в общий путь или остаётся делегатом), `storage_couchdb.py`, `storage_journal.py`, `server.py` (upload/download инструменты), `tests/*`

**Interfaces:**
- Consumes: Task 1 §в; ContentSplitter (Task 3) для newnote-чанков (бинарные piece-и тоже сплиттером).
- Produces: `resolve_doc_type(path: str, data: bytes|str) -> "plain" | "newnote"` (upstream-правила); write-пути обоих бэкендов принимают bytes И str единым методом `write_note(content: str | bytes, ...)` (BinaryNoteError удаляется — решение делает тип сам); `obsidian_upload_attachment` сохраняется как инструмент-обёртка (validate, b64, делегирует); сплиттер применяется и к бинарникам (Task 1 §а).

- [ ] **Step 1: failing tests** — upstream-векторы doc-type (текстовые расширения, бинарные, «текст в бинарном расширении» — как решает устройство); write_note(bytes) на обоих бэкендах → newnote с корректным size; прежние все сценарии (text write, upload, cap-тесты) зелёные без изменений семантики cap'ов (капы удалены в Task 5 — здесь только тип).
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** — `feat: upstream document type resolution`

### Task 5: syncMaxSizeInMB и производные лимиты вместо самодельных капов

**Files:**
- Modify: `storage_journal.py`, `storage_couchdb.py`, `journal_codec.py`, `journal_store.py`, `config.py`, `tests/*`

**Interfaces:**
- Consumes: Task 1 §д (точное правило устройства для oversized); сплиттер (максимум куска выводится из настроек — для лимита строки).
- Produces: удаление MAX_ATTACHMENT_BYTES/MAX_DOWNLOAD_BYTES/линии-64MiB как констант; новые производные: `replay_line_limit` = base64(макс. piece по настройкам vault)×1.37+margin (env-override `S3_REPLAY_LINE_LIMIT`); правило oversized = поведение устройства из Task 1 §д (например, отказ create с тем же смыслом, что отказ устройства добавить файл — формулировка из исследования); бюджеты refresh масштабируются от syncMaxSizeInMB (env-override остаётся); download-лимит = тот же oversized-путь.

- [ ] **Step 1: failing tests** — oversized-файл (сверх syncMaxSizeInMB) → поведение = устройство (из §д); файл в пределах размера → пишется как устройство, включая размер чанков; replay журнала клиента с файлом на границе размера (синтетика по §д-правилам) не падает по нашей линии; budgets scale.
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** — `feat: settings-derived limits replace self-imposed caps`

### Task 6: Settings-matrix test harness

**Files:**
- Create: `tests/test_settings_matrix.py`
- Modify: `tests/fixtures/` (вторая golden journal fixture: клиент с поднятым syncMaxSizeInMB и большим файлом — Task 8)

**Interfaces:**
- Produces: параметризованная матрица (NFC/legacy × case-sensitive × splitter-on/off × syncMaxSizeInMB варианты) на fake-хранилище: полный цикл write→publish→replay→read для текста/бинарника/oversized; asserts паритета: чанки/ids/size/type соответствуют Task 1 правилам при каждой комбинации.

- [ ] **Step 1–4: TDD матрицы → Step 5: Commit** — `test: settings matrix harness for device parity`

### Task 7: спека/README/CHANGELOG + пин

**Files:**
- Modify: `docs/superpowers/specs/2026-11-17-universal-livesync-mcp-design.md` (NFC/case-sensitive/сплиттер/лимиты-из-настроек; E2EE/obfuscation остаются отказами — отмечены как единственное остаточное отличие), `README.md` (раздел Supported settings: таблица «настройка → поддерживается», описание oversized-поведения, лимиты из настроек), `docs/upstream-pin.md`, `CHANGELOG.md`, `AGENTS.md`

- [ ] **Step 1: правки → Step 2: Commit** — `docs: settings parity support matrix`

### Task 8: живая приёмка с поднятыми настройками (пользователь)

**Files:**
- Create: `docs/test-artifacts/2026-11-23-parity-live/`

- [ ] **Step 1: пользователь поднимает syncMaxSizeInMB на TESTVault и кладёт файл >32 MiB (например, 40–80 MiB PDF/скан) → полный синк → golden журнала в fixtures → Step 2: контроллер: sweep+verify (байт-в-байт, размер, чанки по сплиттеру) → Step 3: write-phase повторно (в т.ч. binary write_attachment большого файла, если ≤ настройки) → Step 4: пользователь Replicate → подтверждение → артефакты; Commit** — `docs: parity live acceptance artifacts`
