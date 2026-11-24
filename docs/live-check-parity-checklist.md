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
./venv/bin/python scripts/live_check_parity.py oversize --state-dir "$state" --vault "$vault" --output "$work/B-refusals.json" --source parity-live/exact.bin
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

## D. Native host decoding (вопросы 4–5 спеки)

1. Контроллер: выполнить prepare-decoding (эксклюзивное создание, без remote writes):

```bash
./venv/bin/python scripts/live_check_parity.py prepare-decoding --state-dir "$state" --vault "$vault" --output "$work/D-prepared.json"
```

2. Владелец: не открывать/не пересохранять invalid.md редактором; выполнить Replicate; ответить **«готово D»**.
3. Контроллер: выполнить inspect-decoding:

```bash
./venv/bin/python scripts/live_check_parity.py inspect-decoding --state-dir "$state" --vault "$vault" --output "$work/D-device-decoding.json"
```

4. Владелец: прислать хвост клиентского лога.
5. Контроллер: записать для invalid.md, text.png, upper.MD фактические type, metadata.size, children и chunk_data; сопоставить с исходными bytes в JSON.
6. Контроллер: ответить на вопросы: что делает native text read с invalid UTF-8; сохраняет ли type-by-suffix текстовую .png и uppercase extension как newnote.

Если устройство не опубликовало invalid.md, FAIL/отсутствие и его лог — результат
эксперимента, а не повод подставить предполагаемые chunks. Не менять production
декодирование по одному неподтверждённому объяснению.

## E. Дополнительный scratch vault — только с согласием

1. Владелец: явно ответить **«согласен на E»**, если нужен дополнительный прогон.
2. Владелец: создать отдельный scratch vault/remote prefix с v2 и/или case-sensitive identity.
3. Контроллер: выбрать новый state-dir, не менять identity текущей реплики.
4. Владелец: создать две case-variant заметки, если файловая система это допускает; выполнить Replicate; ответить **«готово E»**.
5. Контроллер: повторить A и C с новым соединением и зафиксировать ограничения host FS.

## Финальные артефакты

1. Контроллер: проверить результаты и клиентские хвосты на секреты, URL credentials, salt/passphrase/key material.
2. Контроллер: положить только окончательные sanitized JSON/транскрипты/клиентские хвосты в `docs/test-artifacts/<YYYY-MM-DD>-parity-live/` (дата фактической приёмки).
3. Контроллер: добавить итог с PASS/FAIL/pending по A–E, версиями, settings coverage и нерешёнными вопросами; не копировать state, журналы, milestone, suite logs или credentials.
4. Контроллер: обновить docs/upstream-pin.md только фактически проверенной client version/scope/date.
5. Владелец: подтвердить итог **«приёмка подтверждена»**.

До этого Task 8 остаётся pending. Полный suite — рабочее доказательство в
`.superpowers/`, а не final live artifact. Удаление acceptance-файлов/изменение
remote/возврат настроек выполняется отдельно по согласованию с владельцем.
