# Живая приёмка S3 с TESTVault

Исполняют пользователь и контроллер после подготовки пакета. Реальный клиент:
Obsidian + Self-hosted LiveSync 1.0.50. LEGACY identity оставить как есть;
NFC покрыта unit-тестами. Локальный SeaweedFS `plan-b-s3` уже запущен.
Ключи ниже одноразовые тестовые. Использовать только TESTVault.

1. **Пользователь:** в `/Users/adeep/Documents/TESTVault` переключить remote
   LiveSync со scratch CouchDB на Object Storage:
   endpoint `http://127.0.0.1:19000`, region `us-east-1`, access key `testkey`,
   secret `testsecret`, bucket `plan-b-it`, path prefix (`bucketPrefix`)
   `livetest/`. Если спрашивает — path-style. Шифрование выключено.
2. Дождаться полного синка: в бакете появились журналы клиента. Сказать
   контроллеру **«синкнуто»**. До конца серверной записи не редактировать vault.
3. **Контроллер:** из корня репозитория выполнить sweep с новым пустым state-dir,
   затем verify. Передать пользователю результат. Все файлы `.md` и `.png`
   вне скрытых каталогов сравниваются по байтам, включая UTF-8 и переводы строк.

   ```bash
   mkdir -p docs/test-artifacts/2026-11-20-live-s3
   set -o pipefail
   STATE=$(mktemp -d /tmp/live-s3-acceptance.XXXXXX)
   venv/bin/python scripts/live_check_s3.py sweep-notes --state-dir "$STATE" --prefix livetest/ 2>&1 | tee docs/test-artifacts/2026-11-20-live-s3/sweep.txt
   venv/bin/python scripts/live_check_s3.py verify-vault --state-dir "$STATE" --prefix livetest/ 2>&1 | tee docs/test-artifacts/2026-11-20-live-s3/verify.txt
   ```

   Продолжать только при exit 0 и `PASS overall` обеих команд. Sweep сохраняет
   первый журнал (исходные байты) в `tests/fixtures/live_client_journal_sample.bin`.
   Он должен содержать хотя бы один doc. Затем прогнать golden-тест:
   `venv/bin/python -m pytest tests/test_journal_codec.py` и сохранить вывод.
   До commit fixture проверить, что в нём только тестовые данные, без секретов.
4. **Контроллер:** выполнить один раз, сообщить результат:

   ```bash
   venv/bin/python scripts/live_check_s3.py write-phase --state-dir "$STATE" --prefix livetest/ 2>&1 | tee docs/test-artifacts/2026-11-20-live-s3/write.txt
   ```

   Требуется `PASS overall`. При частичном FAIL сначала разобрать уже
   выполненные операции по логу; скрипт не откатывает опубликованные журналы.
5. **Пользователь:** Replicate в Obsidian, дождаться применения и проверить:

   - Появилась `Папка S3/Переехавшая.md` с серверным текстом.
   - В `Тестовая заметка.md` появился блок **«Дописано сервером (S3)»**.
   - `На удаление s3.md` отсутствует.
   - `Серверная S3.md` отсутствует в корне.
   - В логе LiveSync нет `size-mismatch` / `corrupted`, отказов применения.
   - Сделать **скриншот лога** после Replicate и подтверждение файлов.
6. **Пользователь:** создать или изменить заметку в клиенте, дождаться отправки,
   сказать **«написал»** и назвать точный путь. **Контроллер:** принудительно
   обновить backend и проверить содержимое по локальному файлу:

   ```bash
   venv/bin/python scripts/live_check_s3.py verify-visible --state-dir "$STATE" --prefix livetest/ --path 'Имя клиентской заметки.md' 2>&1 | tee docs/test-artifacts/2026-11-20-live-s3/visible.txt
   ```

   `--path` повторяемый; указать реально изменённый путь. Требуется exit 0 и
   `PASS overall verify-visible`. После завершения Replicate можно повторить
   `verify-vault` для полной сверки.
7. **Артефакты:** сохранить выводы команд, golden-теста и скриншоты в
   `docs/test-artifacts/2026-11-20-live-s3/`. Записать туда `result.txt` с одной
   итоговой строкой `PASS live-client S3 acceptance` только после всех проверок;
   иначе `FAIL live-client S3 acceptance: <причина>`. Вывод скрипта сам по себе
   не подтверждает приём серверных записей Obsidian: нужно подтверждение шага 5.

## Если клиент отверг нашего писателя

Зафиксировать FAIL, сохранить лог/скриншот, остановить приёмку. Следующий шаг —
**план-Д: регистрация узла в milestone**, отдельная задача. Это не часть
данного чек-листа; не менять milestone в ходе приёмки.
