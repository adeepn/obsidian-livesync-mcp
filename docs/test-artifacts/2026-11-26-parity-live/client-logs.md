# LiveSync client log excerpts pasted by the owner

Self-hosted LiveSync **1.0.50**, Obsidian on macOS, vault `TESTVault`, remote "S3 plan-b-it"
(disposable local S3 journal). Lines are verbatim; unrelated rule lines are elided where marked.

## Step 0 — ordinary sync against the empty test bucket (2026-11-25 22:15)

```
25.11.2026, 22:15:11->[ModuleLiveSyncMain]  Self-hosted LiveSync v1.0.50 1.0.50
25.11.2026, 22:15:24->Switched to remote: S3 plan-b-it and starting replication...
25.11.2026, 22:15:24->Could not retrieve remote sync parameters
25.11.2026, 22:15:24->Synchronisation parameters not found, creating new ones
25.11.2026, 22:15:24->Initial synchronisation parameters stored successfully, retrying fetch
25.11.2026, 22:15:24->Fetched synchronisation parameters
25.11.2026, 22:15:24->Synchronisation parameters do not have PBKDF2 salt, generating a new salt
25.11.2026, 22:15:24->Synchronisation parameters fetched successfully
25.11.2026, 22:15:24->Journal epoch changed and remote wipe confirmed. Clearing dedupe caches.
25.11.2026, 22:15:24->Receiving Journal: Getting list of remote journal
25.11.2026, 22:15:24->Receiving Journal: No journals needs to be downloaded
25.11.2026, 22:15:24->Packing Journal: Start sending
25.11.2026, 22:15:24->Packing Journal: No journals to be packed!
25.11.2026, 22:15:46->Log window opened
```

Observation: the device created the service records in the empty bucket but did not re-send its files.
The owner then ran "Overwrite Server Data with This Device's Files" (Maintenance and Recovery);
the bucket received one journal with the vault and an updated milestone (bucket listing in the controller report).

## B100 — Maximum file size raised to 100 (2026-11-26 11:32)

```
26.11.2026, 11:32:42->Some tweak values have been changed. syncMaxSizeInMB
26.11.2026, 11:32:42->Modified: 1795681962199
26.11.2026, 11:32:42->[ReplicateResultProcessor] Requeued 8 stored document(s) after the reflection filters changed
26.11.2026, 11:32:49->Some tweak values have been changed. syncMaxSizeInMB
26.11.2026, 11:32:49->Modified: 1795681969510
26.11.2026, 11:32:49->[ReplicateResultProcessor] Requeued 8 stored document(s) after the reflection filters changed
26.11.2026, 11:33:05->Switched to remote: S3 plan-b-it and starting replication...
26.11.2026, 11:33:05->Receiving Journal: Getting list of remote journal
26.11.2026, 11:33:05->Receiving Journal: 1
26.11.2026, 11:33:05->Packing Journal: Start sending
26.11.2026, 11:33:05->Packing Journal: No journals to be packed!
```

## B — large files synced by the device (2026-11-26 11:34–11:46)

```
26.11.2026, 11:34:41->The storage file has been changed but exceeds the maximum size. Skipping: parity-live/above.bin
26.11.2026, 11:34:47->[ServiceDatabaseFileAccess]  STORAGE -> DB (newnote) parity-live/device-40m.bin
26.11.2026, 11:34:55->[ServiceDatabaseFileAccess]  STORAGE -> DB (newnote) parity-live/exact.bin
26.11.2026, 11:46:50->Fetched synchronisation parameters
26.11.2026, 11:46:50->Synchronisation parameters fetched successfully
26.11.2026, 11:46:50->Receiving Journal: Getting list of remote journal
26.11.2026, 11:46:50->Receiving Journal: No journals needs to be downloaded
26.11.2026, 11:46:50->Packing Journal: Start sending
26.11.2026, 11:46:50->Packing Journal: 16 / 16
26.11.2026, 11:46:50->Uploading journal: 1 / ...
26.11.2026, 11:46:50->Packing Journal: Finished. Processed 16 doc(s) into 1 chunk(s)
26.11.2026, 11:46:50->Uploading journal: All 1 chunk(s) uploaded
```

## B-upload — the client applies the peer-written 40 MiB file (2026-11-26 12:19)

```
26.11.2026, 12:19:42->Switched to remote: S3 plan-b-it and starting replication...
26.11.2026, 12:19:42->Fetched synchronisation parameters
26.11.2026, 12:19:42->Synchronisation parameters fetched successfully
26.11.2026, 12:19:42->Receiving Journal: Getting list of remote journal
26.11.2026, 12:19:42->Receiving Journal: 1
26.11.2026, 12:19:42->Processing journal: 1795684739923-ee3d70427a0833fc858be3ba5af029d8-docs.jsonl.gz has been processed
26.11.2026, 12:19:42->Packing Journal: Start sending
26.11.2026, 12:19:42->Packing Journal: Finished. Processed 0 doc(s) into 0 chunk(s)
26.11.2026, 12:19:42->[ServiceFileHandler]  Processing parity-live/peer-40m.bin (parity-l :1-22d) : Done
```

## D — unusual files synced by the device (2026-11-26 12:45)

```
26.11.2026, 12:45:17->Fetched synchronisation parameters
26.11.2026, 12:45:17->Synchronisation parameters fetched successfully
26.11.2026, 12:45:17->Receiving Journal: Getting list of remote journal
26.11.2026, 12:45:17->Receiving Journal: No journals needs to be downloaded
26.11.2026, 12:45:17->Packing Journal: Start sending
26.11.2026, 12:45:17->Packing Journal: 15 / 15
26.11.2026, 12:45:17->Uploading journal: 1 / ...
26.11.2026, 12:45:17->Packing Journal: Finished. Processed 6 doc(s) into 1 chunk(s)
26.11.2026, 12:45:17->Uploading journal: All 1 chunk(s) uploaded
```

## B50 — Maximum file size lowered back to 50 (2026-11-26 15:22)

```
26.11.2026, 15:22:18->Some tweak values have been changed. syncMaxSizeInMB
26.11.2026, 15:22:18->Modified: 1795695738485
26.11.2026, 15:22:18->[ReplicateResultProcessor] Requeued 17 stored document(s) after the reflection filters changed
26.11.2026, 15:22:18->[ReplicateResultProcessor] Processing parity-live/device-40m.bin has been skipped due to file size exceeding the limit
26.11.2026, 15:22:18->[ReplicateResultProcessor] Processing parity-live/exact.bin has been skipped due to file size exceeding the limit
26.11.2026, 15:22:18->[ReplicateResultProcessor] Processing parity-live/peer-40m.bin has been skipped due to file size exceeding the limit
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/created.md (parity-l :3-69f) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/deleted.md (parity-l :2-a40) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/invalid.md (parity-l :1-64e) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/moved.md (parity-l :1-1e3) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/text.png (parity-l :1-b84) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing parity-live/upper.MD (parity-l :1-91a) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing test-image.png (test-ima :1-d4e) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Welcome.md (welcome. :1-1c4) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Картинка.md (картинка :1-58a) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing От клиента.md (от клиен :1-8f3) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Папка S3/Переехавшая.md (папка s3 :1-f53) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Папка/Переехавшая.md (папка/пе :1-14a) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Связанная заметка.md (связанна :1-47a) : Done
26.11.2026, 15:22:18->[ServiceFileHandler]  Processing Тестовая заметка.md (тестовая :1-641) : Done
26.11.2026, 15:22:20->Some tweak values have been changed. syncMaxSizeInMB
26.11.2026, 15:22:20->Modified: 1795695740400
26.11.2026, 15:22:20->[ReplicateResultProcessor] Requeued 17 stored document(s) after the reflection filters changed
26.11.2026, 15:22:20->[ReplicateResultProcessor] Processing parity-live/exact.bin has been skipped due to file size exceeding the limit
26.11.2026, 15:22:20->[ServiceFileHandler]  Processing parity-live/created.md (parity-l :3-69f) : Done
26.11.2026, 15:22:20->[ServiceFileHandler]  Processing parity-live/deleted.md (parity-l :2-a40) : Done
26.11.2026, 15:22:20->[ServiceFileHandler]  Processing parity-live/invalid.md (parity-l :1-64e) : Done
26.11.2026, 15:22:20->[ServiceFileHandler]  Processing parity-live/moved.md (parity-l :1-1e3) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing parity-live/text.png (parity-l :1-b84) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing parity-live/upper.MD (parity-l :1-91a) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing test-image.png (test-ima :1-d4e) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing parity-live/device-40m.bin (parity-l :1-1b2) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Welcome.md (welcome. :1-1c4) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Картинка.md (картинка :1-58a) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing От клиента.md (от клиен :1-8f3) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Папка S3/Переехавшая.md (папка s3 :1-f53) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Папка/Переехавшая.md (папка/пе :1-14a) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing parity-live/peer-40m.bin (parity-l :1-22d) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Связанная заметка.md (связанна :1-47a) : Done
26.11.2026, 15:22:21->[ServiceFileHandler]  Processing Тестовая заметка.md (тестовая :1-641) : Done
26.11.2026, 15:22:27->Switched to remote: S3 plan-b-it and starting replication...
26.11.2026, 15:22:27->Fetched synchronisation parameters
26.11.2026, 15:22:27->Synchronisation parameters fetched successfully
26.11.2026, 15:22:27->Receiving Journal: Getting list of remote journal
26.11.2026, 15:22:27->Receiving Journal: 1
26.11.2026, 15:22:27->Packing Journal: Start sending
26.11.2026, 15:22:27->Packing Journal: No journals to be packed!
```

Observation: after each edit the device re-queued its stored documents and skipped the ones
above the limit at that moment (`device-40m.bin`, `exact.bin`, `peer-40m.bin` after the first
edit; only `exact.bin` after the second). The local files stay. The sync sent no journal: the
device's own map in the milestone carries the new value (`tweakModified` 1795695740400);
PREFERRED keeps 50 with its older `tweakModified` 1795001730894.
Controller check after this log (peer snapshot `2cb6eee`, `B50-reconnect.json`): reconnect
PASS, 14 expected files byte-exact, `above.bin` and `exact.bin` excluded by the vault size
policy, missing 0, size or hash mismatches 0.

## B-refusals — device sync after the peer refused an oversized upload and rename (2026-11-26 16:20)

```
26.11.2026, 16:20:30->Fetched synchronisation parameters
26.11.2026, 16:20:30->Synchronisation parameters fetched successfully
26.11.2026, 16:20:30->Receiving Journal: Getting list of remote journal
26.11.2026, 16:20:30->Receiving Journal: No journals needs to be downloaded
26.11.2026, 16:20:30->Packing Journal: Start sending
26.11.2026, 16:20:30->Packing Journal: No journals to be packed!
```

The owner could not check the names in Obsidian's file explorer, which hides
`.bin` files by default, and asked the controller to check the files instead
(«файлы сам проверяй - я же их не вижу в обсидиане»). Controller checks on disk
after this sync: `parity-live` holds the same eight files with unchanged sizes and
modification times; `skipped.bin` and `refused-move.bin` are absent; `exact.bin` is
104,857,600 bytes with the SHA-256 it had before. No file outside `.obsidian` was
modified after 16:00. Reconnect (`B-refusals-reconnect.json`): PASS, 14 expected
files byte-exact, 2 excluded by the vault size policy, missing 0, size or hash
mismatches 0. The bucket still holds 11 objects.
