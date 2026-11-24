# Приёмка Settings-parity с Obsidian

Это пакет для Task 8, **не отчёт о состоявшейся живой проверке**.
Порядок A → B → C → D; E только по отдельному согласию владельца.
Работать с тестовым vault/хранилищем, с резервной копией; не делать Rebuild,
wipe или сброс state в рамках этой проверки. Секреты, setup URI, salt,
passphrase, device ID и полный milestone не пересылать.

## Подготовка контроллера

1. Указать абсолютный путь открытого vault и отдельный постоянный state-dir.
2. Указать локальные фильтры теми же CLI-флагами, что настроены у MCP-пира.
3. Использовать `scripts/live_check_parity.py --help` для выбора backend/соединения.
4. Сохранить рабочие результаты в `.superpowers/sdd/2026-11-23-settings-parity/live-candidate/`.

Команды ниже запускает контроллер из корня репозитория. Пример рассчитан на
**локальное одноразовое S3** из прежней приёмки (defaults: localhost:19000,
testkey/testsecret, bucket plan-b-it, prefix livetest/), без загрузки `.env`.
Для другого хранилища передать проверенные `--endpoint`, `--bucket`, `--region`,
`--prefix`, `--access-key`, `--secret-key`; не вставлять секреты в переписку или
транскрипт команд. CouchDB: `--backend couchdb --url ... --user ... --password ...
--database ...`. Реальные значения вводить локально; не сохранять shell history.
Для активных фильтров есть `--only-regex`, `--ignore-regex`, `--use-ignore-files`,
`--ignore-files`; нужен Node 22+. Default без фильтров может отличаться от устройства.

```bash
work=.superpowers/sdd/2026-11-23-settings-parity/live-candidate
mkdir -p "$work"
vault=/absolute/path/to/TESTVault
state="$work/state"
```

К каждому вызову ниже добавлять одни и те же connection/filter флаги.
`--state-dir "$state" --vault "$vault" --output "$work/NAME.json"` обязательны;
подкоманды не печатают connection values или exception text. PASS/FAIL — в stdout,
ненулевой exit означает остановку и разбор, а не повтор write-команды вслепую.

## A. Повторное подключение на текущих настройках

1. Владелец: записать версию Obsidian, ОС и установленную версию LiveSync; прислать их.
2. Владелец: выполнить Replicate и дождаться окончания; ответить **«готово A»**.
3. Контроллер: выполнить reconnect с существующими настройками:

```bash
./venv/bin/python scripts/live_check_parity.py reconnect --state-dir "$state" --vault "$vault" --output "$work/A-reconnect.json"
```

4. Контроллер: проверить byte-exact и ordered piece IDs всех обычных файлов, включая .base/.json/.yaml; изучить каждый FAIL.
5. Контроллер: сверить sanitized milestone с README settings table; разобрать каждый `unknown_keys` до продолжения.
6. Владелец: прислать хвост клиентского лога этой синхронизации без секретов.

JSON хранит ключи всех tweak snapshots (анонимные device labels), только
разрешённые не-секретные scalar/enum values; остальные values — `<withheld>`.
Unknown keys дают FAIL. Скрытые файлы явно посчитаны вне scope обычного пира:
`.obsidian`, i:/ix:/ps: не выдаются за проверенный Hidden File Sync.
Source-audited behavior и результаты реального клиента отмечать отдельно.

## B. Размеры и большие attachments

1. Владелец: установить `syncMaxSizeInMB=100`, сохранить настройки; ответить **«готово B100»**.
2. Контроллер: выполнить prepare-size (создаёт только новые локальные файлы):

```bash
./venv/bin/python scripts/live_check_parity.py prepare-size --state-dir "$state" --vault "$vault" --output "$work/B-prepared.json" --limit-mib 100
```

3. Владелец: выполнить Replicate для `parity-live/device-40m.bin` (40 MiB), `exact.bin` (100 MiB), `above.bin` (100 MiB + 1 byte); ответить **«готово B-файлы»**.
4. Контроллер: перезапустить/переоткрыть peer для загрузки новых tweaks; выполнить verify-size:

```bash
./venv/bin/python scripts/live_check_parity.py verify-size --state-dir "$state" --vault "$vault" --output "$work/B-size.json" --limit-mib 100
```

5. Контроллер: подтвердить байты, размер, piece count/IDs первых двух файлов; above.bin должен остаться только на диске устройства.
6. Контроллер: выполнить write-large (вызывает backend `write_attachment`, а не ручную публикацию журнала):

```bash
./venv/bin/python scripts/live_check_parity.py write-large --state-dir "$state" --vault "$vault" --output "$work/B-upload.json"
```

7. Владелец: выполнить Replicate; убедиться, что появился `parity-live/peer-40m.bin`; ответить **«вижу B-upload»**.
8. Контроллер: выполнить verify-visible для `--path parity-live/peer-40m.bin`; сравнить device bytes и piece IDs с peer.
```bash
./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/B-visible.json" --path parity-live/peer-40m.bin
```

9. Владелец: вернуть `syncMaxSizeInMB=50`, сохранить и Replicate; ответить **«готово B50»**.
10. Контроллер: перезапустить/переоткрыть peer; выполнить oversize:

```bash
./venv/bin/python scripts/live_check_parity.py oversize --state-dir "$state" --vault "$vault" --output "$work/B-refusals.json" --source parity-live/exact.bin --limit-mib 50
```

11. Контроллер: подтвердить skipped upload 50 MiB + 1 и refused rename старого 100 MiB source; source bytes/rev неизменны, destinations отсутствуют.
12. Владелец: выполнить Replicate; подтвердить source на месте, нет `skipped.bin`/`refused-move.bin`; ответить **«вижу B-без изменений»** и прислать хвост лога.

Понижение лимита нужно именно для oversized rename уже существующего файла;
пир не создаёт запрещённый source обходом admission. above.bin на диске
остаётся пользовательским несинхронизированным файлом. Не считать его потерей.

## C. Записи при реальных settings

1. Владелец: приостановить редактирование заметок; ответить **«готово C»**.
2. Контроллер: выполнить write-phase:

```bash
./venv/bin/python scripts/live_check_parity.py write-phase --state-dir "$state" --vault "$vault" --output "$work/C-write.json"
```

3. Владелец: выполнить Replicate.
4. Владелец: проверить `parity-live/moved.md` с текстом «Обновлено.», отсутствие created.md/deleted.md; ответить **«вижу C»**.
5. Владелец: прислать хвост клиентского лога.
6. Контроллер: выполнить verify-visible для moved.md и вручную подтвердить отсутствие двух retired paths на диске.

```bash
./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/C-visible.json" --path parity-live/moved.md
```

write-phase публикует create → update → move → soft-delete и проверяет DB
read-back/tombstone. PASS не заменяет подтверждение отражения устройством.

## D. Native host decoding (вопрос 4 спеки)

1. Контроллер: проверить отсутствие invalid.md/text.png/upper.MD под parity-live в DB; существующие пути означают остановку повторного прогона и выбор нового согласованного scratch remote.
2. Контроллер: выполнить prepare-decoding (эксклюзивное создание, без remote writes):

```bash
./venv/bin/python scripts/live_check_parity.py prepare-decoding --state-dir "$state" --vault "$vault" --output "$work/D-prepared.json"
```

3. Владелец: не открывать/не пересохранять invalid.md редактором; выполнить Replicate; ответить **«готово D»**.
4. Контроллер: выполнить inspect-decoding:

```bash
./venv/bin/python scripts/live_check_parity.py inspect-decoding --state-dir "$state" --vault "$vault" --output "$work/D-device-decoding.json"
```

5. Владелец: прислать хвост клиентского лога.
6. Контроллер: записать для invalid.md, text.png, upper.MD фактические type, metadata.size, children и chunk_data; сопоставить с исходными bytes в JSON.
7. Контроллер: ответить на вопросы: что делает native text read с invalid UTF-8; сохраняет ли type-by-suffix текстовую .png и uppercase extension как newnote.

Helper проверяет фиксированные исходные bytes на диске, enum type, bounded
metadata.size, форму/количество child IDs и общий объём chunks. В sharable JSON
попадают точные chunk_data только для распознанных результатов публичных
синтетических входов; это allowlist публикации, **не спецификация native decode**.
Неизвестный результат даёт FAIL и только длину/SHA-256: владелец исследует bytes
локально и сообщает безопасное описание. Не подменять такой результат известным
вариантом. CLI не печатает непроверенные remote type/size или чужое содержимое.

Если устройство не опубликовало invalid.md, FAIL/отсутствие и его лог — результат
эксперимента, а не повод подставить предполагаемые chunks. Не менять production
декодирование по одному неподтверждённому объяснению.

## E. Дополнительный scratch vault — вопрос 5, только с согласием

1. Владелец: явно ответить **«согласен на E»**, если нужен дополнительный прогон.
2. Владелец: создать отдельный scratch vault/remote prefix с v2 и/или case-sensitive identity.
3. Контроллер: выбрать новый state-dir, не менять identity текущей реплики.
4. Владелец: создать `Case.md` и `case.md`, если файловая система это допускает; выполнить Replicate; ответить **«готово E»**.
5. Владелец: при включённых case-sensitive + NFC попытаться создать `Café.md` (U+00E9) и `Café.md` (U+0065 + U+0301) без перезаписи существующего файла; сообщить **«два имени»** или **«FS создаёт один alias»**.
6. Владелец: прислать collision diagnostics и клиентский хвост без секретов.
7. Контроллер: отдельно записать filesystem aliases и journal IDs/collision behavior; невозможность двух physical Unicode имён не выдавать за проверку двух DB-документов.
8. Контроллер: повторить A и C с новым соединением и зафиксировать ограничения host FS.

D отвечает только на вопрос 4; E — на вопрос 5. Отказ от E оставляет
case/NFC native acceptance pending, даже при зелёных source/oracle тестах.

## Оставшиеся обязательные вопросы спеки — pending до отдельного live шага

Эти вопросы не закрываются существующими PASS из A–D. Контроллер согласует
дополнительные действия с владельцем; никаких автоматических изменений vault
или режима синхронизации helper не делает.

### B2. Растущий файл (вопрос 2)

1. Контроллер: сохранить текущую DB revision, children и набор remote journal keys синтетического уже синхронизированного файла.
2. Владелец: увеличить этот файл выше текущего положительного L; выполнить Replicate; ответить **«готово B-growth»**.
3. Контроллер: проверить сохранённые local bytes, неизменность DB revision/children и отсутствие новых journal metadata/chunks для этого изменения.
4. Владелец: прислать notices/log tail.

### B3. Приём oversized, повторное отражение и zero (вопрос 3)

1. Владелец: явно согласовать permissive sender и отдельный receiving scratch vault.
2. Контроллер: выбрать новый state-dir и отдельное согласованное соединение получателя.
3. Владелец: отправить синтетический файл при высоком лимите sender; установить меньший положительный лимит receiver; выполнить Replicate; ответить **«готово B-receive»**.
4. Контроллер: подтвердить сохранение DB records/chunks на receiver и отсутствие физического файла после NOTICE о размере.
5. Владелец: повысить лимит receiver и сохранить настройки; ответить **«готово B-raise»** после materialization.
6. Контроллер: проверить байты/IDs отражённого файла после автоматического requeue; при отсутствии записать filter/conflict/suspend blockers.
7. Владелец: повторить receive с `syncMaxSizeInMB=0`; ответить **«вижу B-unlimited»**.
8. Контроллер: сохранить размеры, notices и клиентские хвосты для обоих проходов.

### Native golden fixture (план Task 8; вопрос 5)

1. Владелец: подтвердить отсутствие постороннего редактирования во время синтетического capture.
2. Контроллер: зафиксировать remote journal keys перед новым native Replicate.
3. Контроллер: создать новые синтетические plain .md файлы ровно 4194303/4194304/4194305 bytes в согласованной папке и сохранить их SHA-256 в рабочем manifest.

Локальная подготовка трёх plain файлов и manifest (без remote writes):

```bash
./venv/bin/python - "$vault" "$work/threshold-inputs.json" <<'PYTHON'
import hashlib, json, sys
from pathlib import Path
from scripts.live_check_parity import local_prepare
files = {f'threshold-{size}.md': b'x' * size for size in (4194303, 4194304, 4194305)}
if not local_prepare(Path(sys.argv[1]), files):
    raise SystemExit(1)
Path(sys.argv[2]).write_text(json.dumps({name: {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in files.items()}, indent=2))
PYTHON
```

4. Владелец: выполнить Replicate при лимите, допускающем эти файлы и большой B attachment; ответить **«готово golden»**.
5. Контроллер: выбрать только новые native journals с этими metadata и binary multi-piece records, а не собственные peer publications.
6. Контроллер: скачать выбранные bytes read-only через `Boto3ObjectStorage.get` в `$work/native-golden/`; не запускать старый sweep helper, сразу пишущий tracked fixture.
7. Контроллер: разобрать journals и сравнить partitions/IDs/Blob sizes с исходными синтетическими файлами; текущий splitter записать из sanitized milestone.
8. Контроллер: подтвердить отсутствие любых посторонних documents/chunks/секретов в выбранных журналах; неподходящий journal отклонить и повторить capture на согласованном scratch remote.
9. Владелец: подтвердить безопасный состав выбранной fixture.
10. Контроллер: перенести только проверенную исходную fixture в `tests/fixtures/` с provenance (client version, дата, source SHA-256, input SHA-256, splitter/settings) и добавить offline replay regression.
11. Контроллер: поместить только sanitized capture summary в final acceptance directory, а не raw journal.

Для V3 тройка файлов проверяет ≥4 MiB threshold; другой splitter не доказывает
V3 transition. Если текущий vault не V3, необходим отдельный согласованный E
проход с V3. Golden capture остаётся pending до реального клиента: готовить
синтетическую замену и называть её native запрещено. Исторический live sample
не заменяется. Изменение source/client pin делается после фактического capture.

## Финальные артефакты

1. Контроллер: проверить результаты и клиентские хвосты на секреты, URL credentials, salt/passphrase/key material.
2. Контроллер: положить только окончательные sanitized JSON/транскрипты/клиентские хвосты в `docs/test-artifacts/<YYYY-MM-DD>-parity-live/` (дата фактической приёмки).
3. Контроллер: добавить итог с PASS/FAIL/pending по A–E, версиями, settings coverage и нерешёнными вопросами; не копировать state, журналы, milestone, suite logs или credentials.
4. Контроллер: обновить docs/upstream-pin.md только фактически проверенной client version/scope/date.
5. Владелец: подтвердить итог **«приёмка подтверждена»**.

До этого Task 8 остаётся pending. Полный suite — рабочее доказательство в
`.superpowers/`, а не final live artifact. Удаление acceptance-файлов/изменение
remote/возврат настроек выполняется отдельно по согласованию с владельцем.
