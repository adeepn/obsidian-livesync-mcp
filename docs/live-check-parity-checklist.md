# Приёмка с настоящим Obsidian

Automated real-server acceptance: **DONE** —
[261 checks](test-artifacts/2026-11-25-parity-docker/summary.txt).
Live Obsidian acceptance: **DONE** for the main scenarios, client **1.0.50**, S3
journal, **2026-11-26**: [record](test-artifacts/2026-11-26-parity-live/summary.txt).
The additional scenarios of section 7 and the cleanup of section 8 were
not executed. **1.0.52 has not been exercised live**.
First owner observation (2026-11-25): ordinary sync against a confirmed wiped
bucket creates service records (milestone and sync parameters) but does not
re-send files; see step 0 for the explicitly authorized device-to-server rebuild
sequence.

Это инструкция проверки, а не отчёт: результаты выполнения — в записи выше.
Контроллер сначала вычитывает инструкцию, затем передаёт владельцу.

## 1. Подготовка вместе с контроллером

1. Использовать только отдельное тестовое хранилище с резервной копией.
   В Obsidian открыть Settings → Community plugins → Self-hosted LiveSync.
   Remote Configuration → Connection settings → Saved connections → строка
   действующего подключения → Configure (значок гаечного ключа).
   Проверить выбранную базу или bucket и Folder Prefix, затем нажать Cancel.
   Не присылать снимок этого окна: там секреты. Сообщить «тестовое подключение проверено».
   <!-- P:src/modules/features/SettingDialogue/SettingsPageCatalogue.ts:137-143;
   PaneRemoteConfig.ts:187-188,389-417; SetupWizard/dialogs/SetupRemoteBucket.svelte:155-252. -->
2. Записать версии: Settings → About (Obsidian и ОС); Settings → Community
   plugins → Installed plugins → Self-hosted LiveSync (версия плагина).
   Передать только эти версии. Если интерфейс переведён, временно выбрать
   английский язык в Settings → General → Language: далее приведены английские
   подписи из исходников, а не предположения о переводе.
3. Контроллер в терминале из корня репозитория выполняет одну строку:
   `work=.superpowers/sdd/2026-11-23-settings-parity/live-candidate; mkdir -p "$work"; read -r 'vault?Абсолютный путь тестового vault: '; state="$work/state"`.
   На Linux вместо `read -r 'vault?...'` использовать `read -r vault` и ввести путь.
   Проверить, что это каталог открытого тестового хранилища Obsidian.
4. Команды ниже рассчитаны на одноразовый локальный S3: `127.0.0.1:19000`,
   bucket `plan-b-it`, prefix `livetest/`, публичные тестовые credentials из
   helper. Для другого подключения контроллер локально добавляет одинаковые
   connection-флаги к каждому вызову по [инструкции подключения](connecting-storage.md).
   Helper **не читает .env**. Его `--help` перечисляет поддерживаемые CLI-поля;
   JWT, custom headers и выбор S3 addressing в этом helper не настраиваются.
   Для такого тестового сервера сначала нужна отдельно согласованная подготовка
   подключения; не отключать защиту рабочего сервера ради этой инструкции.
   Секреты не вставлять в переписку, запись терминала или историю команд.
5. Проверить справку: `./venv/bin/python scripts/live_check_parity.py reconnect --help`.
   `--only-regex` и `--ignore-regex` задаются контроллером только при локальных
   regex-настройках MCP. Настройки ignore-файлов по умолчанию берутся из vault;
   `--use-ignore-files` / `--no-use-ignore-files` и `--ignore-files` — явные
   переопределения. Для активных фильтров необходим Node.js 22+.

**Как выполнить синхронизацию в следующих шагах:** в Obsidian открыть палитру
команд Ctrl+P (macOS Cmd+P), набрать `Sync with a saved connection`, выбрать
команду Self-hosted LiveSync, затем в окне `Select a remote to replicate with`
выбрать то же тестовое подключение, проверенное в шаге 1. Дождаться окончания
индикации синхронизации и отсутствия нового сообщения об ошибке. Сообщить код
шага и «готово»; при ошибке остановиться и прислать её безопасное описание.
<!-- C:src/serviceFeatures/remoteConfig.ts:388-435,458-470 registers exact command
and selection title, applies selected configuration and calls replicate(true). -->

Имена и байты файлов `.bin` проверяет контроллер на диске: Obsidian по умолчанию
не показывает такие файлы в дереве. Владелец проверяет в дереве только заметки.

Все проверки без разрешающих флагов только читают удалённое хранилище.
`prepare-*` создают новые файлы локально эксклюзивно: существующий файл — FAIL,
перезаписи нет. Obsidian может автоматически отправить эти локальные файлы,
поэтому даже подготовка выполняется только в согласованном тестовом vault.
Запись на сервер требует `--allow-remote-write`; переименование и мягкое
удаление (запись отметки об удалении без очистки истории) дополнительно требуют
`--allow-remote-delete`. В обоих случаях `--confirm-target` обязан точно
совпасть с `bucket/prefix` (для примера `plan-b-it/livetest/`) или именем базы
CouchDB. Неверное подтверждение останавливает команду до подключения.
Не выполнять wipe или удаление объектов S3. Reset/Rebuild разрешён только для явно подтверждённого тестового bucket в шаге 0 ниже.

**0. Однократная инициализация пустого тестового S3.** Контроллер подтверждает, что запущен отдельный пустой тестовый bucket с теми же endpoint, bucket и Folder Prefix, что в сохранённом тестовом подключении. Владелец отвечает: «разрешаю первую синхронизацию и создание служебных записей и тестовых файлов в этом bucket». Не использовать рабочее хранилище.

В Obsidian открыть Ctrl+P (macOS Cmd+P), выбрать `Self-hosted LiveSync: Sync with a saved connection` («синхронизировать с сохранённым подключением»), затем в `Select a remote to replicate with` («выберите подключение для синхронизации») выбрать проверенное тестовое подключение. Дождаться окончания синхронизации без ошибки. Наблюдение владельца 2026-11-25 (клиент 1.0.50): после `Journal epoch changed and remote wipe confirmed` обычная синхронизация создаёт milestone и параметры, но не отправляет файлы повторно (`Packing Journal: No journals to be packed!`). Наличие только служебных записей не доказывает восстановление файлов.

После этой однократной обычной синхронизации, только для явно подтверждённого отдельного тестового bucket после wipe, открыть Settings → Self-hosted LiveSync → `Maintenance and recovery` («Обслуживание и восстановление») → `Reset Synchronisation information` («Сбросить информацию о синхронизации») → `Overwrite Server Data with This Device's Files` («Перезаписать данные сервера файлами с этого устройства») → `Schedule and Restart` («Запланировать и перезапустить»). Подтвердить диалог, если показан; дождаться `Rebuild everything operation completed.`. Это сообщение остаётся английским; название вкладки в pinned source использует строчное `recovery` (Maintenance and Recovery в описании пути). Эта процедура перезаписывает сервер локальными файлами и не разрешена для рабочего bucket или неизвестного удалённого состояния. Проверить наличие журналов и файлов, а не только milestone/параметров, перед переходом к A.

Если уже открыто окно `No Synchronisation Settings Found` («настройки синхронизации не найдены»), это другой путь первоначальной настройки. Только для подтверждённого пустого тестового bucket выбрать `Use this device's settings` («использовать настройки этого устройства»). Если ожидались существующие данные или настройки — `Cancel` («Отмена»), остановиться и сообщить контроллеру. Первые две подписи на этом pin остаются английскими и при русском интерфейсе.

Сообщить «0 готово, rebuild завершён, журналы и файлы проверены» либо безопасное описание ошибки без секретов. Контроллер выполняет команду reconnect из A и сохраняет A-reconnect.json. После успешной инициализации не повторять её отдельно: перейти к A.

<!-- P:modules/features/SettingDialogue/PaneMaintenance.ts:110-139;
common/messages/combinedMessages.prod.ts:7538-7541,7912-7917,9394-9399,9773-9778;
serviceFeatures/redFlag.ts:518 (untranslated completion log).
C:remoteConfig.ts:388-423,458-470; SyncParamsHandler.ts:95-136;
JournalSyncCore.ts:347-352; LiveSyncJournalReplicator.ts:209-249;
LiveSyncDBFunctions.ts:60-79,124-174; P:redFlag.ts:249-265;
LiveSyncProvisionalMessages.ts:273-291; translation.ts:70-91. -->

## 2. Повторное подключение (A)

1. Владелец выполняет синхронизацию указанным выше способом; сообщает «A готово».
2. Контроллер вставляет: `./venv/bin/python scripts/live_check_parity.py reconnect --state-dir "$state" --vault "$vault" --output "$work/A-reconnect.json"`.
3. Ожидается PASS для ожидаемых обычных файлов: байты, размер и порядок частей
   совпадают. Отдельно указаны `expected`, `excluded_by_policy` (только числа по
   классам правил), `missing`, `size_or_hash_mismatch`. Игнорируемые и слишком
   большие файлы не обязаны находиться на сервере. Скрытые файлы посчитаны
   отдельно, их синхронизация этой проверкой не подтверждается.
4. Контроллер передаёт владельцу PASS/FAIL и эти числа. Неизвестные имена
   настроек не печатаются: только `<unknown keys withheld>`, число и SHA256
   отсортированных имён. Ненулевое число даёт FAIL; контроллер исследует его
   локально и сверяет с README, не пересылая исходные настройки.
   Передать после проверки секретов `A-reconnect.json`; полный документ
   настроек и идентификаторы устройств не пересылать.

## 3. Большие файлы (B)

1. Владелец: Settings → Self-hosted LiveSync → Selector → Normal Files →
   Maximum file size: ввести `100`, закончить ввод, закрыть настройки,
   выполнить синхронизацию; сообщить «B100 готово». Если Selector скрыт,
   включить General Settings → Extra menus → Enable advanced features. Не менять остальные параметры.
   <!-- P:SettingDialogue/SettingsPageCatalogue.ts:155-161, PaneSelector.ts:43-75;
   C:common/settingConstants.ts:207-211,398-401; defaults.ts:147. -->
2. Контроллер: `./venv/bin/python scripts/live_check_parity.py prepare-size --state-dir "$state" --vault "$vault" --output "$work/B-prepared.json" --limit-mib 100`.
   Ожидаются три сообщения PASS о новых файлах в `parity-live`.
3. Владелец выполняет синхронизацию; сообщает «B файлы готовы».
4. Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-size --state-dir "$state" --vault "$vault" --output "$work/B-size.json" --limit-mib 100`.
   Ожидается PASS: 40 MiB и ровно 100 MiB отправлены, 100 MiB + 1 байт
   остался только локально. Передать безопасный `B-size.json`.
5. **Отдельное разрешение на запись:** владелец отвечает «разрешаю B-upload в
   тестовое хранилище». После этого контроллер:
   `./venv/bin/python scripts/live_check_parity.py write-large --state-dir "$state" --vault "$vault" --output "$work/B-upload.json" --allow-remote-write --confirm-target plan-b-it/livetest/`.
   Ожидается PASS проверки прочитанных обратно 40 MiB.
6. Владелец выполняет синхронизацию и присылает хвост лога. Контроллер проверяет
   на диске `parity-live/peer-40m.bin` (размер, время изменения, байты) и передаёт
   результат: Obsidian не показывает `.bin` в дереве файлов.
   Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/B-visible.json" --path parity-live/peer-40m.bin`.
   Передать `B-upload.json`, `B-visible.json` после проверки секретов.
7. Владелец тем же путём Selector → Normal Files → Maximum file size вводит
   `50`, закрывает настройки, выполняет синхронизацию; сообщает «B50 готово».
   `exact.bin` остаётся 100 MiB: понижение порога не уменьшает существующий файл.
8. **Отдельное разрешение на попытку переименования:** владелец отвечает
   «разрешаю B-refusals, включая попытку переименования, в тестовом хранилище».
   Контроллер: `./venv/bin/python scripts/live_check_parity.py oversize --state-dir "$state" --vault "$vault" --output "$work/B-refusals.json" --source parity-live/exact.bin --limit-mib 50 --allow-remote-write --allow-remote-delete --confirm-target plan-b-it/livetest/`.
   Ожидается PASS: загрузка 50 MiB + 1 пропущена, переименование отказано;
   исходные байты и версия сохранены, `skipped.bin` и `refused-move.bin` отсутствуют.
9. Владелец выполняет синхронизацию и присылает хвост лога. Контроллер проверяет
   на диске, что `skipped.bin` и `refused-move.bin` отсутствуют, а `exact.bin` не
   изменился, и передаёт `B-refusals.json` с результатом; владелец отвечает
   «B без изменений».

## 4. Создание, изменение, переименование и удаление (C)

1. Это **отдельно разрешаемый шаг удаления только в тестовом vault**.
   Владелец прекращает редактирование и отвечает «разрешаю C: создать,
   изменить, переименовать и мягко удалить синтетические заметки».
2. Контроллер: `./venv/bin/python scripts/live_check_parity.py write-phase --state-dir "$state" --vault "$vault" --output "$work/C-write.json" --allow-remote-write --allow-remote-delete --confirm-target plan-b-it/livetest/`.
   Ожидается PASS каждой операции и повторного чтения. При FAIL не повторять
   автоматически: часть операций могла уже завершиться.
3. Владелец выполняет синхронизацию, открывает `parity-live/moved.md` и видит
   «Обновлено.». В дереве нет `created.md` и `deleted.md`; сообщает «C вижу».
4. Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/C-visible.json" --path parity-live/moved.md`.
   Передать `C-write.json`, `C-visible.json` и подтверждение отсутствия двух имён.

## 5. Чтение необычных байтов устройством (D)

1. Контроллер проверяет, что `parity-live/invalid.md`, `text.png`, `upper.MD`
   ещё не существуют ни локально, ни на сервере. Затем:
   `./venv/bin/python scripts/live_check_parity.py prepare-decoding --state-dir "$state" --vault "$vault" --output "$work/D-prepared.json"`.
   Ожидается PASS эксклюзивного создания трёх маленьких файлов.
2. Владелец **не открывает и не пересохраняет invalid.md**, выполняет
   синхронизацию; сообщает «D готово» или безопасное описание ошибки.
3. Контроллер: `./venv/bin/python scripts/live_check_parity.py inspect-decoding --state-dir "$state" --vault "$vault" --output "$work/D-device-decoding.json"`.
   Передать этот отчёт после проверки секретов. Неизвестный результат — FAIL
   с длиной и SHA256, без чужого содержимого; это наблюдение, его нельзя
   заменять предполагаемым ответом. Точные части публикуются только для
   распознанных публичных синтетических входов. Отсутствие файла после отказа
   устройства тоже результат эксперимента.

## 6. Вместимость, время и оставшиеся файлы

MiB = 1 048 576 байт. Время — ориентир при локальном сервере, **не таймаут
приёмки**; удалённая сеть и число частей могут увеличить его многократно.
На время проверки оставить свободно несколько GiB на устройстве и сервере.

| Шаг | Создаётся локально | Создаётся на сервере | Ориентир | Что остаётся |
|---|---|---|---|---|
| A | Отчёт, SQLite/кеш S3 при чтении | Ничего | 1–10 мин; первый replay дольше | Отчёт и отдельный state-dir |
| B prepare/verify | 240 MiB + 1 байт | До 140 MiB исходных данных от устройства | 2–15 мин | device-40m.bin, exact.bin, above.bin |
| B upload | После приёма ещё 40 MiB | Ещё 40 MiB от MCP | 1–10 мин | peer-40m.bin |
| B refusals | Отчёт; исходный exact.bin не меняется | Ничего при ожидаемом PASS | 1–5 мин | Нет skipped.bin/refused-move.bin |
| C | Маленькая moved.md после приёма | Версии трёх заметок и отметки удаления | 1–5 мин | moved.md; история created.md/deleted.md |
| D | Три файла суммарно менее 1 KiB | Их записи и части, если устройство допустило | 1–5 мин | invalid.md, text.png, upper.MD |
| Дополнительные проверки | См. отдельные сценарии §7 | См. таблицу §7 | См. таблицу §7 | Имена и отдельная очистка в §7 |

Для B суммарно до 180 MiB исходных удалённых данных: base64 в журналах может
занимать около 240 MiB до сжатия, плюс версии, служебные записи, SQLite и кеш.
При понижении порога `exact.bin` остаётся 100 MiB, `above.bin` локальный;
первая проверка не должна объявлять их потерянными.

## 7. Дополнительные сценарии — только с отдельным разрешением

Эти шаги ещё **не выполнены**. Они не входят автоматически в разрешение A–D.
Контроллер сначала готовит отдельные пустые тестовые подключения и локальные
каталоги; владелец проверяет их по пункту 1.1. Все команды ниже относятся к
публичным синтетическим данным, не к личным заметкам. В результате отдельно
отмечать PASS/FAIL/не выполнено по каждому сценарию.

### 7.1. Уже отправленный файл вырос выше порога (B2)

1. Владелец подтверждает «разрешаю B2, создание и увеличение growth.md».
   Settings → Self-hosted LiveSync → Selector → Normal Files → Maximum file
   size: `50`. Закрыть настройки и выполнить команду синхронизации из §1.
2. Контроллер создаёт новый файл ровно 1 MiB:
   `./venv/bin/python -c 'from pathlib import Path; import sys; from scripts.live_check_parity import local_prepare; raise SystemExit(0 if local_prepare(Path(sys.argv[1]), {"growth.md": b"x"*1048576}) else 1)' "$vault"`.
   Ожидается PASS; существующий файл не перезаписывается.
3. Владелец выполняет синхронизацию, сообщает «B2 до роста готово».
   Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/B2-before.json" --path parity-live/growth.md`.
   Ожидается PASS, `remote_size=1048576`; сохранить `remote_sha256`.
4. Только после разрешения шага 1 контроллер дописывает **этот** файл до
   50 MiB + 1, проверяя его прежний размер:
   `./venv/bin/python -c 'from pathlib import Path; import sys; p=Path(sys.argv[1])/"parity-live/growth.md"; assert p.stat().st_size==1048576; f=p.open("ab"); f.write(b"x"*(50*1048576+1-1048576)); f.close(); print("local bytes:", p.stat().st_size)' "$vault"`.
   Ожидается `local bytes: 52428801`.
5. Владелец выполняет синхронизацию, сообщает «B2 после роста готово».
   Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/B2-after.json" --path parity-live/growth.md`.
   Здесь **ожидается FAIL сравнения с локальным файлом**: на сервере остаётся
   прежний 1 MiB. Сравнить `remote_size`, `remote_sha256` и `piece_count`
   с B2-before; при изменении остановиться. Исходный локальный увеличенный файл
   остаётся на устройстве. Передать оба безопасных JSON и ответ владельца;
   нельзя переименовать ожидаемое расхождение в общий PASS helper.

### 7.2. Второй Obsidian принимает большой файл; повышение порога и ноль (B3)

1. Отдельное разрешение: «разрешаю B3 на двух тестовых устройствах».
   Контроллер заранее подключает два пустых тестовых vault к одному отдельному
   тестовому remote через обычный Setup Wizard. Владелец в каждом проверяет
   Saved connections → Configure → Cancel, как в §1.1. Второй каталог далее
   вводится контроллером: `state="$work/B3-state"; read -r 'vault?Абсолютный путь первого B3 vault: '; read -r 'vault2?Абсолютный путь второго B3 vault: '`.
   Не использовать прежний state-dir для другого remote. Для Linux использовать
   `state="$work/B3-state"; read -r vault; read -r vault2`. Перед продолжением оба каталога
   должны открываться в разных окнах Obsidian.
2. На первом устройстве: Selector → Normal Files → Maximum file size: `100`;
   на втором: тот же путь, значение `50`. Закрыть настройки. Синхронизировать
   оба окна командой §1, выбирая одинаковое отдельное тестовое подключение.
3. Контроллер в первом каталоге эксклюзивно создаёт файл 60 MiB:
   `./venv/bin/python -c 'from pathlib import Path; import sys; from scripts.live_check_parity import local_prepare; raise SystemExit(0 if local_prepare(Path(sys.argv[1]), {"receive-60m.bin": 60*1048576}) else 1)' "$vault"`.
   Владелец синхронизирует первое окно, затем второе и сообщает «B3 при 50
   синхронизировано»; отсутствие `parity-live/receive-60m.bin` на втором устройстве
   контроллер проверяет на диске (в дереве файлов Obsidian `.bin` не виден).
4. Контроллер читает удалённый файл, сравнивая с первым локальным каталогом:
   `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/B3-remote.json" --path parity-live/receive-60m.bin`.
   Ожидается PASS. Connection-флаги здесь должны указывать на отдельный B3
   remote. Отсутствие на втором диске проверить:
   `./venv/bin/python -c 'from pathlib import Path; import sys; print("receiver exists:", (Path(sys.argv[1])/"parity-live/receive-60m.bin").exists())' "$vault2"`.
   Ожидается `receiver exists: False`.
5. На втором устройстве по тому же UI-пути установить `100`, закрыть настройки,
   выполнить синхронизацию. Ожидается появление файла; если нет, **не выполнять
   Rebuild**: записать отсутствие и безопасное описание уведомления.
   Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault2" --output "$work/B3-raised.json" --path parity-live/receive-60m.bin`.
   Ожидается PASS. Передать B3-remote/B3-raised и ответ владельца.
6. Проверка нуля: на втором устройстве Maximum file size: `0` (без ограничения),
   закрыть настройки и синхронизировать. Контроллер создаёт **другой** файл
   на первом устройстве:
   `./venv/bin/python -c 'from pathlib import Path; import sys; from scripts.live_check_parity import local_prepare; raise SystemExit(0 if local_prepare(Path(sys.argv[1]), {"receive-zero.bin": 60*1048576}) else 1)' "$vault"`.
   Владелец синхронизирует первое, затем второе окно, сообщает «B3 при нуле вижу».
7. Контроллер: `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault2" --output "$work/B3-zero.json" --path parity-live/receive-zero.bin`.
   Ожидается PASS. Передать B3-zero; сохраняются оба файла по 60 MiB на двух
   устройствах и сервере. После окончания вернуть исходные пороги отдельным
   согласованным действием, не удаляя файлы автоматически.

### 7.3. Различение регистра и Unicode-имён (E)

1. Отдельное разрешение: «разрешаю E в новом пустом тестовом vault».
   Не менять идентичность существующей реплики. Контроллер подготавливает
   пустое подключение; владелец открывает его Settings → Self-hosted LiveSync →
   General Settings → Extra menus → Enable advanced features и Enable edge
   case treatment features. Затем General Settings → File identity → Path
   identity mode → Unicode NFC path identity; Patches → Compatibility
   (Database structure) → Handle files as Case-Sensitive: включить.
   Если появляются требования перестроить **непустое** хранилище, остановиться:
   это не предусмотренное пустое подключение.
   <!-- P:GeneralSettingSpecs.ts:64-91; SettingsPageCatalogue.ts:181-189;
   PanePatches.ts:34-123; C:common/settingConstants.ts:386-409;
   common/models/shared.definition.configNames.ts:86-90. -->
2. Контроллер вводит путь этого нового vault:
   `state="$work/E-state"; read -r 'vault?Абсолютный путь нового E vault: '`
   (Linux: `state="$work/E-state"; read -r vault`) и добавляет его connection-флаги. Создаёт четыре имени без перезаписи:
   `./venv/bin/python -c 'from pathlib import Path; import sys; from scripts.live_check_parity import local_prepare; raise SystemExit(0 if local_prepare(Path(sys.argv[1]), {n:b"public identity probe\n" for n in ("Case.md", "case.md", "Caf\u00e9.md", "Cafe\u0301.md")}) else 1)' "$vault"`.
   Если файловая система считает имена одним файлом, эксклюзивное открытие
   остановится. Сохранить это наблюдение; не повторять с перезаписью.
3. Владелец выполняет синхронизацию по §1. Ожидается различение регистра;
   два NFC-эквивалентных имени могут дать конфликт идентичности. Сообщить
   «E два имени» либо «E файловая система объединяет имена» и безопасное
   описание уведомления. Контроллер:
   `./venv/bin/python scripts/live_check_parity.py reconnect --state-dir "$state" --vault "$vault" --output "$work/E-identity.json"`.
   Передать отчёт вместе с наблюдением. Невозможность создать два физических
   имени не доказывает проверку двух отдельных удалённых документов.
4. Для отдельной проверки V2 на ещё одном пустом подключении включить Enable
   advanced features; выбрать отдельный `state="$work/E-v2-state"`; Advanced → Local Database Tweak → Chunk Splitter:
   выбрать V2: Simple (Default). Не менять используемую реплику. Повторить A и отдельно
   авторизованный C с новым `$state`; передать оба отчёта с указанием V2.
   <!-- P:AdvancedSettingSpecs.ts:17-24;
   C:common/settingConstants.ts:442-445; common/models/setting.const.ts:57-62. -->

### 7.4. Исходные журналы настоящего Obsidian (только отдельный S3)

1. Владелец отдельно разрешает получение публичных синтетических журналов.
   Контроллер подготавливает **пустой** одноразовый локальный S3 из §1;
   в него не подключены посторонние устройства. Контроллер выбирает `state="$work/native-state"` и вводит путь нового
   тестового vault в `$vault` через `read -r vault`. На нём
   Advanced → Local Database Tweak → Chunk Splitter: выбрать V3: Fine deduplication; Selector →
   Normal Files → Maximum file size: `100`. Закрыть настройки и выполнить
   синхронизацию. Для другого сервера команды ниже предварительно адаптирует
   контроллер; секреты не записываются в текст сценария.
2. Контроллер сохраняет список существующих журналов, не содержимое настроек:
   `./venv/bin/python -c 'import json,sys; from pathlib import Path; from config import S3Config; from object_storage import Boto3ObjectStorage; from storage_journal import is_journal_key; s=Boto3ObjectStorage(S3Config("http://127.0.0.1:19000","testkey","testsecret","plan-b-it","us-east-1","livetest/","")); p=Path(sys.argv[1]); p.mkdir(exist_ok=False); (p/"before.json").write_text(json.dumps([k for k in s.list() if is_journal_key(k)])); print("baseline saved")' "$work/native-capture"`.
   Ожидается `baseline saved`. Каталог создаётся один раз; повтор без разбора
   запрещён. Список служебный, владельцу не пересылается.
3. Контроллер эксклюзивно создаёт три текстовых файла около границы 4 MiB:
   `./venv/bin/python -c 'from pathlib import Path; import sys; from scripts.live_check_parity import local_prepare; raise SystemExit(0 if local_prepare(Path(sys.argv[1]), {f"threshold-{n}.md": b"x"*n for n in (4194303,4194304,4194305)}) else 1)' "$vault"`.
   Также выполнить B prepare-size, затем синхронизировать **только устройство**;
   write-large/write-phase не запускать в этом интервале. Владелец сообщает
   «исходные файлы отправлены» и точную версию клиента.
4. Контроллер получает только новые журналы чтением, сохраняя bytes под
   собственными числовыми именами (удалённое имя никогда не становится путём):
   `./venv/bin/python -c 'import json,sys,hashlib; from pathlib import Path; from config import S3Config; from object_storage import Boto3ObjectStorage; from storage_journal import is_journal_key; p=Path(sys.argv[1]); s=Boto3ObjectStorage(S3Config("http://127.0.0.1:19000","testkey","testsecret","plan-b-it","us-east-1","livetest/","")); old=set(json.loads((p/"before.json").read_text())); blobs=[s.get(k) for k in s.list() if is_journal_key(k) and k not in old]; assert blobs and all(isinstance(b,bytes) for b in blobs); [(p/f"journal-{i}.gz").open("xb").write(b) for i,b in enumerate(blobs)]; (p/"digests.json").open("x").write(json.dumps([hashlib.sha256(b).hexdigest() for b in blobs])); print("captured journals:",len(blobs))' "$work/native-capture"`.
   Ожидается положительное число журналов. Команда не пишет на сервер и не
   меняет tracked fixtures. Не запускать старый sweep helper для этой цели.
5. Контроллер проверяет состав через `inflate_journal` / `parse_journal`
   (`journal_codec.py`): только эти синтетические документы/части, без чужих
   заметок/секретов, и сравнивает порядок частей, IDs, Blob sizes через
   `verify-visible` для каждого `--path parity-live/threshold-4194303.md`,
   `--path parity-live/threshold-4194304.md`, `--path parity-live/threshold-4194305.md`.
   Одна команда проверки:
   `./venv/bin/python scripts/live_check_parity.py verify-visible --state-dir "$state" --vault "$vault" --output "$work/native-inputs.json" --path parity-live/threshold-4194303.md --path parity-live/threshold-4194304.md --path parity-live/threshold-4194305.md`.
   Передать только безопасный отчёт; непроверенный журнал владельцу не посылать.
6. Владелец подтверждает публичный синтетический состав. Только после проверки
   контроллер добавляет исходные журналы в `tests/fixtures/` отдельным изменением
   с provenance: версия клиента, дата, SHA256 журналов/входов, settings/splitter
   и offline replay regression. Это последующая разработческая работа после
   фактической приёмки, не действие владельца. В final acceptance directory
   помещается безопасный итог, а не raw journal. Синтетическая замена запрещена.

| Дополнительный сценарий | Локально | На сервере (исходные байты) | Ориентир | Что сохраняется |
|---|---|---|---|---|
| B2 | 50 MiB + 1 после роста | Прежний 1 MiB + история | 2–10 мин | growth.md и два отчёта |
| B3 | По 120 MiB на двух устройствах | До 120 MiB + base64/история | 5–30 мин | receive-60m.bin, receive-zero.bin, три отчёта |
| E | Четыре маленьких файла, если FS допускает | Крошечные записи/конфликты | 5–15 мин | Case.md, case.md, два Café-имени |
| Исходные журналы | 12 MiB threshold + 240 MiB + 1 B-файлы, скачанные журналы | Около 152 MiB до base64/истории | 5–30 мин | threshold-файлы, B-файлы, native-capture, отчёты |

Очистка каждого дополнительного сценария отдельно разрешается владельцем.
Контроллер выполняет одну строку, выбрав соответствующий код B2/B3/E/THRESHOLD:
`./venv/bin/python -c 'from pathlib import Path; import sys; groups={"B2":["growth.md"],"B3":["receive-60m.bin","receive-zero.bin"],"E":["Case.md","case.md","Caf\u00e9.md","Cafe\u0301.md"],"THRESHOLD":[f"threshold-{n}.md" for n in (4194303,4194304,4194305)]}; names=groups[sys.argv[2]]; root=Path(sys.argv[1])/"parity-live"; assert input("Type CLEANUP TEST VAULT: ")=="CLEANUP TEST VAULT"; [(root/n).unlink() for n in names if (root/n).is_file()]; print("remaining:",sum((root/n).exists() for n in names))' "$vault" B2`.
Для B3 повторить с `$vault2` и кодом B3 только после отдельного разрешения на
второе устройство. Ожидается `remaining: 0`; затем синхронизировать оба устройства
по §1 и проверить отсутствие имён. История на сервере остаётся. Папку
native-capture, state и отчёты сохранять до архивирования и отдельного решения;
физическую очистку S3 эти команды не выполняют.

## 8. Отдельно согласованная очистка и итог

1. Сначала сохранить проверенные отчёты. Владелец отдельно подтверждает
   «разрешаю удалить перечисленные файлы приёмки из тестового vault».
   Локальное удаление при работающем LiveSync может распространиться на сервер.
   Не удалять всю папку: в ней могли появиться другие файлы.
2. Только после этого контроллер выполняет одну строку, которая ещё раз
   требует локального подтверждения и удаляет **только** перечисленные имена:
   `./venv/bin/python -c 'from pathlib import Path; import sys; root=Path(sys.argv[1])/"parity-live"; names="device-40m.bin exact.bin above.bin peer-40m.bin moved.md invalid.md text.png upper.MD".split(); assert input("Type CLEANUP TEST VAULT: ")=="CLEANUP TEST VAULT"; [(root/n).unlink() for n in names if (root/n).is_file()]; print("remaining acceptance files:", sum((root/n).exists() for n in names))' "$vault"`.
   Ожидается `remaining acceptance files: 0`. Если C завершился частично,
   отдельно проверить `created.md` / `deleted.md`; не удалять их автоматически.
3. Владелец выполняет синхронизацию указанной командой палитры, проверяет
   отсутствие удалённых имён, сообщает «очистка завершена». История удалений,
   части и журналы на сервере остаются; команд физической очистки сервера нет.
   При отдельно выполненных дополнительных сценариях их файлы, включая
   threshold-файлы, удаляются только по новому перечисленному согласованию.
4. Контроллер проверяет JSON и описания ошибок на секреты. Только окончательные
   безопасные отчёты и подтверждения помещает в
   `docs/test-artifacts/<date>-parity-live/`, обновляет [статус](upstream-pin.md)
   фактической датой, версиями и PASS/FAIL/не выполнено по каждому пункту.
   SQLite, кеш, исходные журналы, полный документ настроек, credentials и
   рабочие suite logs туда не включать. Владелец читает итог и отвечает
   «приёмка подтверждена».
5. Отдельный локальный state-dir и рабочие отчёты не удаляются автоматически.
   После архивирования и отдельного разрешения контроллер проверяет его
   абсолютный путь и только затем удаляет средствами файлового менеджера.
   Это не очистка удалённого хранилища. Возврат Maximum file size к исходному
   значению — тем же путём Selector → Normal Files → Maximum file size,
   с повторной синхронизацией и подтверждением владельца.

## Reconnect ingestion comparison (F15-1)

Reconnect compares the device-ingested body, document type and pre-split Blob
metadata size, then checks piece IDs. It reports `ingestion_transformed: true`
and `invalid_bytes_replaced` when malformed UTF-8 in a plain-suffix file is
replaced with U+FFFD, following the observed plugin 1.0.50 host behaviour
(2026-11-26). That host observation is not an upstream parser oracle.
`splitter_projection` identifies other body changes, including leading BOM
removal by legacy splitters; metadata still includes that BOM. Non-plain files,
including `text.png` and `upper.MD`, retain byte-exact comparison.

Each record retains local `size` / `sha256` and remote `remote_size` /
`remote_sha256`; `byte_exact` means equality with the expected device body.
A transformed body only passes when the remote body, type, metadata and piece
IDs agree. A per-file exception produces FAIL with a cause class, continues
with the other files and makes the overall reconnect fail. Inspect that file,
repair the local read/storage problem or resync a mismatching device file,
then rerun reconnect. No new configuration or remote write is introduced.
