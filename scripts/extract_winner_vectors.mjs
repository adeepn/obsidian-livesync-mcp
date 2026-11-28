// Node >=26.13; COMMONLIB_ROOT TAR_DIR OUTPUT
// Executes the published PouchDB 9.0.0 database a device holds its notes in: the core, the
// IDB adapter (unmodified, on the pinned fake-indexeddb) and the memory adapter (the level
// core over an in-memory store), opened with the device's options
// (LiveSyncLocalDB.ts:293-298). Each journal file is received through the plugin's own
// functions (journalReceive.ts: partition, chunk selection, revision diff, metadata selection)
// and the adapter's own bulkDocs/revsDiff/get. Nothing of the database is replaced. Expected
// values are read back from the adapter's stored revision tree with the published pouchdb-merge
// functions, so the peer is compared with what a device holds after every file.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import zlib from 'node:zlib';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {createRequire, stripTypeScriptTypes} from 'node:module';
import {pathToFileURL} from 'node:url';
const [root, tarDir, out] = process.argv.slice(2);
if (!root || !tarDir || !out) throw Error('Usage: extract_winner_vectors.mjs COMMONLIB_ROOT TAR_DIR OUTPUT');
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const git = (...args) => execFileSync('git', ['-C', root, ...args], {encoding: 'utf8'});
if (git('rev-parse', 'HEAD').trim() !== pin) throw Error('Wrong pin');
const lock = JSON.parse(git('show', `${pin}:package-lock.json`)).packages;

// Every package the imported code can reach, resolved as Node does: a package's own
// node_modules first, then each enclosing scope. The browser adapters are inspected, not run.
const enclosing = location => {
  const cut = location.lastIndexOf('/node_modules/');
  return cut > 0 ? location.slice(0, cut) : '';
};
function resolve(location, name) {
  for (let scope = location; ; scope = enclosing(scope)) {
    const candidate = (scope ? `${scope}/` : '') + `node_modules/${name}`;
    if (candidate in lock) return candidate;
    if (!scope) return null;
  }
}
const needed = {};
for (const start of ['node_modules/pouchdb-core', 'node_modules/pouchdb-adapter-memory',
  'node_modules/pouchdb-adapter-idb', 'node_modules/pouchdb-adapter-indexeddb']) {
  for (const stack = [start]; stack.length;) {
    const location = stack.pop();
    if (location in needed) continue;
    const entry = lock[location];
    if (entry.link) throw Error(`Linked package ${location}`);
    needed[location] = entry;
    for (const name of Object.keys(entry.dependencies || {})) {
      const found = resolve(location, name);
      if (!found) throw Error(`No lock entry for ${name} required by ${location}`);
      stack.push(found);
    }
  }
}

// Verify every archive against the lockfile before extracting it into a fresh directory.
// Archives are flat files named after the package and version; the lockfile holds no version twice.
const staging = fs.mkdtempSync(path.join(os.tmpdir(), 'pouchdb-winner-'));
try {
  const dependencies = [];
  const archives = new Set();
  for (const [location, entry] of Object.entries(needed).sort()) {
    if (!entry.resolved.startsWith('https://registry.npmjs.org/')) throw Error(`Unexpected registry for ${location}`);
    const archive = path.join(tarDir, `${location.slice(location.lastIndexOf('/') + 1)}-${entry.version}.tgz`);
    if (archives.has(archive)) throw Error(`Two lock locations share ${archive}`);
    archives.add(archive);
    const data = fs.readFileSync(archive);
    const integrity = 'sha512-' + createHash('sha512').update(data).digest('base64');
    if (integrity !== entry.integrity) throw Error(`Integrity mismatch: ${location}`);
    const target = path.join(staging, location);
    fs.mkdirSync(target, {recursive: true});
    execFileSync('tar', ['-xzf', archive, '-C', target, '--strip-components=1']);
    dependencies.push({package: location, version: entry.version, url: entry.resolved, integrity,
      archive_sha256: createHash('sha256').update(data).digest('hex')});
  }
  // IndexedDB does not run in Node. The IDB adapter runs below unmodified on fake-indexeddb,
  // an implementation of the IndexedDB specification; it is pinned here, outside the plugin's
  // lockfile, and verified the same way before it is used.
  const IDB_DOUBLE = {
    package: 'fake-indexeddb', version: '6.2.8',
    url: 'https://registry.npmjs.org/fake-indexeddb/-/fake-indexeddb-6.2.8.tgz',
    integrity: 'sha512-mpVW/HLUqwKXJ6F/s0hMgdK1eQfM4ncTHDioNGHWL0WKV3VcZCoZq1NKgboBbAge07aRCO7L48yziKssL59XEQ=='};
  const doubleArchive = path.join(tarDir, `fake-indexeddb-${IDB_DOUBLE.version}.tgz`);
  const doubleData = fs.readFileSync(doubleArchive);
  const doubleIntegrity = 'sha512-' + createHash('sha512').update(doubleData).digest('base64');
  if (doubleIntegrity !== IDB_DOUBLE.integrity) throw Error(`Integrity mismatch: ${IDB_DOUBLE.package}`);
  fs.mkdirSync(path.join(staging, IDB_DOUBLE.package), {recursive: true});
  execFileSync('tar', ['-xzf', doubleArchive, '-C', path.join(staging, IDB_DOUBLE.package), '--strip-components=1']);
  const indexeddb = {...IDB_DOUBLE, archive_sha256: createHash('sha256').update(doubleData).digest('hex')};
  const require = createRequire(pathToFileURL(path.join(staging, 'node_modules/pouchdb-core/package.json')));
  const globalsOf = require(path.join(staging, IDB_DOUBLE.package, 'build/cjs/index.js'));
  for (const name of Object.keys(globalsOf)) {
    if (/^(indexedDB|IDB)/.test(name) && globalsOf[name] !== undefined) globalThis[name] = globalsOf[name];
  }
  const merge = require('pouchdb-merge');
  const PouchDB = require('pouchdb-core').plugin(require('pouchdb-adapter-memory')).plugin(require('pouchdb-adapter-idb'));

  // The adapters a device can select share the tree code that is executed below. Fail loudly
  // if a future pin stops calling it, so this oracle is not extended to an unwritten claim.
  const digest = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const lib = name => path.join(staging, 'node_modules', name, 'lib/index.js');
  const adapters = {
    'pouchdb-adapter-idb': ['pouchdbAdapterUtils.processDocs(dbOpts.revs_limit'],
    'pouchdb-adapter-indexeddb': ['pouchdbMerge.merge(oldDoc.rev_tree, doc.rev_tree[0], docsRevsLimit(doc))',
      'pouchdbMerge.winningRev(doc)', 'pouchdbAdapterUtils.parseDoc(tmp, opts.new_edits, dbOpts)'],
    'pouchdb-adapter-memory': [],
  };
  for (const [name, calls] of Object.entries(adapters)) {
    const source = fs.readFileSync(lib(name), 'utf8');
    for (const call of calls) if (!source.includes(call)) throw Error(`${name} no longer contains: ${call}`);
  }
  const receivePath = path.join(root, 'src/replication/journal/journalReceive.ts');
  const receive = await import(pathToFileURL(receivePath).href);
  // The decoder imports the plugin's whole utility module. Its source is evaluated as
  // scripts/extract_legacy_wire_cells.mjs does: the pinned bytes of the two escape functions
  // and of the decoder itself, imports stripped, in a context holding only what it uses.
  const pinned = name => {
    const source = fs.readFileSync(path.join(root, 'src', name), 'utf8');
    if (source !== git('show', `${pin}:src/${name}`)) throw Error(`Modified upstream source: ${name}`);
    return source;
  };
  const decoderContext = vm.createContext({Uint8Array, TextEncoder, TextDecoder});
  const evaluate = source => vm.runInContext(
    stripTypeScriptTypes(source.replace(/^import .*;\n/gm, '').replace(/export /g, '')), decoderContext);
  const utilsSource = pinned('common/utils.ts');
  evaluate(utilsSource.slice(utilsSource.indexOf('const map = {'), utilsSource.indexOf('export function escapeMarkdownValue')));
  evaluate(pinned('replication/journal/journalRecords.ts'));
  const decodeJournalRecords = decoderContext.decodeJournalRecords;
  const recordsPath = path.join(root, 'src/replication/journal/journalRecords.ts');

  const device = {revs_limit: 100, deterministic_revs: true, auto_compaction: false};

  // Devices default to the IDB adapter (compatibilitySettings.ts:13-18) and the setting dialogue
  // recommends it for LiveSync. That adapter runs here unmodified, on the IndexedDB
  // implementation installed above: its transaction serves requests in the order they are made
  // and runs each success handler then, its bodies go into the by-sequence store under
  // `<id>::<rev>` and come back through its own decodeDoc, and a merge's stemming compacts
  // through its own compactRevs. The level adapter below is the cross-check: it runs
  // unmodified too and shares the tree code.
  // The build a browser loads decodes attachment data with the platform atob
  // (pouchdb-binary-utils lib/index-browser.js); the Node build demands the canonical spelling
  // instead. Both paths below run on the platform function, as a device does.
  require('pouchdb-binary-utils').atob = text => atob(text);
  // A memory database keeps its content across close() when reopened by name: one name per case.
  let opened = 0;
  const openIdb = () => new PouchDB('idb-' + opened++, {adapter: 'idb', ...device});
  const openLevel = () => new PouchDB('level-' + opened++, {adapter: 'memory', ...device});
  const storedTree = (db, id) => new Promise(resolveTree => db._getRevisionTree(id, (error, tree) =>
    resolveTree(error ? null : tree)));

  // A file is a list of records, written as JSON lines, or the frames themselves when a
  // case exercises the compact chunk records.
  const framesOf = file => typeof file === 'string' ? file : file.map(record => JSON.stringify(record) + '\n').join('');
  // Every document of the file: JSON records, and the document the decoder makes of a compact
  // chunk record (journalRecords.ts:31-50).
  const decodedRecords = file => framesOf(file).split('\n').filter(line => line).map(line =>
    line.startsWith('~') ? {_id: line.slice(1, line.indexOf('\x1f'))} : JSON.parse(line))
    .filter(record => record && typeof record._id === 'string');
  const jsonRecords = file => framesOf(file).split('\n').filter(line => line && !line.startsWith('~'))
    .map(line => JSON.parse(line)).filter(record => record && typeof record._id === 'string');

  // JournalSyncCore.ts:778-840: the plugin's own decoder, selection functions and the adapter's
  // own calls. Returns the stage a device fails at, or null when the file applies.
  async function receiveFile(db, file) {
    const decoded = decodeJournalRecords(new TextEncoder().encode(framesOf(file)));
    if (!decoded.ok) return decoded.stage;
    const docs = decoded.docs;
    const {chunks, docs: metadata} = receive.partitionJournalDocuments(docs);
    const rows = (await db.allDocs({include_docs: true, keys: chunks.map(entry => entry._id)})).rows;
    const saveChunks = receive.selectChunksToSave(chunks, rows);
    let chunkResults;
    try {
      chunkResults = await db.bulkDocs(saveChunks, {new_edits: true});
    } catch (error) {
      return 'chunk-write';
    }
    if (receive.assessChunkWrites(saveChunks, chunkResults).failed) return 'chunk-write';
    // Past this point the chunk batch is committed. The plugin's own selection
    // can still fail: journalMetadataRevisions spreads a plain object, so a
    // document named like a member of Object.prototype is not iterable.
    let saveDocs;
    try {
      const diff = await db.revsDiff(receive.journalMetadataRevisions(metadata));
      saveDocs = receive.selectMetadataToSave(metadata, diff);
    } catch (error) {
      return 'metadata-selection';
    }
    let metadataResults;
    try {
      metadataResults = await db.bulkDocs(saveDocs, {new_edits: false});
    } catch (error) {
      return 'metadata-write';
    }
    return receive.hasJournalWriteErrors(metadataResults) ? 'metadata-write' : null;
  }
  // A number JSON cannot carry keeps its JavaScript spelling: JSON.stringify prints null for it.
  const unprintable = value => typeof value === 'number' && !Number.isFinite(value) ? String(value)
    : Array.isArray(value) ? value.map(unprintable)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).map(key => [key, unprintable(value[key])])) : value;

  // What a device holds for every document of the case after one file,
  // applied or refused.
  async function observe(db, ids, bodies) {
    const view = [];
    for (const id of ids) {
      const tree = await storedTree(db, id);
      if (!tree) {
        view.push({id, tree: null});
        continue;
      }
      // A case may ask which revision of the tree still has a body: a merge stems a revision
      // away from one path while a shorter path keeps it, and the adapter compacts only what
      // no path keeps.
      let slots;
      if (bodies) {
        slots = {};
        const named = [];
        merge.traverseRevTree(tree, (isLeaf, pos, revHash) => named.push(pos + '-' + revHash));
        for (const rev of named) {
          try {
            await db.get(id, {rev});
            slots[rev] = true;
          } catch (error) {
            slots[rev] = false;
          }
        }
      }
      const winner = merge.winningRev({rev_tree: tree});
      let plain;
      try {
        const doc = await db.get(id);
        plain = {id: doc._id, rev: doc._rev, deleted: doc._deleted === true};
      } catch (error) {
        plain = {error: error.reason ?? error.message};
      }
      let read;
      try {
        const doc = await db.get(id, {revs: true, conflicts: true});
        const body = {};
        for (const key of Object.keys(doc).sort()) if (key !== '_revisions' && key !== '_conflicts') body[key] = unprintable(doc[key]);
        read = {rev: doc._rev, revisions: doc._revisions, conflicts: doc._conflicts ?? [], body};
      } catch (error) {
        read = {error: error.reason ?? error.message};
      }
      // Both adapters store a tree as JSON, where a position that is not finite becomes null.
      const unbounded = tree.some(root => root.pos === null || (typeof root.pos === 'number' && !Number.isFinite(root.pos)));
      view.push({id, winner, unbounded, plain, ...(slots ? {slots} : {}),
        deleted: merge.isDeleted({rev_tree: tree}, winner),
        leaves: merge.collectLeaves(tree).map(leaf => [leaf.rev, Boolean(leaf.opts.deleted)]),
        conflicts: merge.collectConflicts({rev_tree: tree}), read});
    }
    return view;
  }
  async function replay(open, files, ids, bodies) {
    const db = open();
    // A core call opens the store; a file the decoder refuses touches nothing else.
    await db.info();
    const applied = [], views = [];
    try {
      for (const file of files) {
        const outcome = await receiveFile(db, file);
        applied.push({records: file, outcome});
        // A refused file leaves what was committed before the refusal.
        views.push(await observe(db, ids, bodies));
        if (outcome) break;
      }
    } finally {
      await db.close();
    }
    return {files: applied, views};
  }
  // JSON.stringify in key order, so that the cross-check compares values, not insertion order.
  const canonical = value => JSON.stringify(value, (key, item) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map(name => [name, item[name]])) : item);

  async function run(files, bodies) {
    const ids = [...new Set(files.flatMap(file => decodedRecords(file).map(record => record._id)))];
    const idb = await replay(openIdb, files, ids, bodies), level = await replay(openLevel, files, ids, bodies);
    // The adapters share the tree code. Where the level adapter still parts ways with IDB is
    // recorded on the case, never hidden: it ignores a record whose revision already has a
    // stored body although the tree does not name it (leveldb-core writeDoc finish, :723-729).
    const at = idb.files.findIndex((file, index) => file.outcome !== level.files[index]?.outcome
      || canonical(idb.views[index]) !== canonical(level.views[index]));
    return {...(at < 0 ? idb : {...idb, level: {file: at, outcome: level.files[at]?.outcome ?? null,
      view: level.views[at] ?? null}}), ids};
  }

  // The database a file is written over: every seed file received, then the base a raw get
  // hands the plugin, or null where that get reports the document missing.
  async function openedOver(open, files) {
    const db = open();
    for (const file of files) {
      const outcome = await receiveFile(db, file);
      if (outcome) throw Error(`a seed file was refused (${outcome}, ${open.name}): ${JSON.stringify(file)}`);
    }
    let base = null;
    try {
      base = await db.get('n.md');
    } catch (error) {
      if (error.status !== 404) throw error;
    }
    return {db, base};
  }
  // What a put left: the chain of the revision it wrote and the leaves beside the winner. core put
  // with force turns the write into an explicit revision with a random id
  // (transformForceOptionToNewEditsOption, pouchdb-core lib/index.js:419-432); only its
  // generation and ancestry are stable. core's bulkDocs hands an invalid revision back as the
  // whole batch's error.
  async function putAndRead(db, doc) {
    let put;
    try {
      put = await db.put(doc, {force: true});
    } catch (error) {
      return {ok: false, status: error.status, error: error.name, id: null, generation: null, ancestors: null, conflicts: null, after: null};
    }
    // Read the revision back by name: a plain get hides a deletion the put wrote.
    const stored = await db.get(put.id, {rev: put.rev, revs: true, conflicts: true});
    const chain = stored._revisions;
    // The revision written has a random id; where it is a conflict, a placeholder stands for it.
    // What the plugin then reads: the winner's slot as a raw get hands it back. A revision the
    // write made is random, so only the identity and the deletion flag are recorded.
    let after;
    try {
      const doc = await db.get(put.id);
      after = {id: doc._id, deleted: doc._deleted === true};
    } catch (error) {
      after = {error: error.reason ?? error.message};
    }
    return {ok: true, status: null, error: null, id: put.id, generation: chain.start, ancestors: chain.ids.slice(1),
      conflicts: (stored._conflicts || []).map(rev => rev === put.rev ? WRITTEN : rev), after};
  }
  const identified = base => base ? {id: base._id, rev: base._rev} : null;
  const WRITTEN = '<the revision written>';
  // The plugin's own write path over each starting state (saving.ts:391-404,
  // createStorageRevision → putDBEntry mode "latest", EntryManagerImpls.ts:287-299): a raw get
  // supplies _rev unless the document is missing or revision-deleted; then put(newDoc, {force: true}).
  async function nativeWrite(open, files) {
    const {db, base} = await openedOver(open, files);
    try {
      const newDoc = {_id: 'n.md', path: 'n.md', type: 'plain', children: [], ctime: 1, mtime: 2, size: 1, eden: {}};
      if (base) newDoc._rev = base._rev;
      return {...await putAndRead(db, newDoc), base: identified(base)};
    } finally {
      await db.close();
    }
  }
  // The plugin's own delete path (deleteDBEntryByPath, EntryManagerImpls.ts:728-790): the object
  // a raw get returns is marked and put back itself, so it is written into the document it names.
  async function nativeDelete(open, files) {
    const {db, base} = await openedOver(open, files);
    try {
      if (!base || base.type === 'leaf') {
        return {base: identified(base), ok: false, status: null, error: 'missing', id: null, generation: null,
          ancestors: null, conflicts: null, after: null};
      }
      if (!base.type || base.type === 'notes') base._deleted = true;
      else { base.deleted = true; base.mtime = 1; }
      return {...await putAndRead(db, base), base: identified(base)};
    } finally {
      await db.close();
    }
  }

  const doc = (rev, ids, flags = {}, start = Number(rev.split('-')[0])) => ({_id: 'n', _rev: rev, type: 'plain',
    path: 'n.md', children: [], ...flags, ...(ids === null ? {} : {_revisions: {start, ids}})});
  const a = doc('1-a', ['a']), b = doc('2-b', ['b', 'a']), c = doc('2-c', ['c', 'a']), d = doc('3-d', ['d', 'c', 'a'], {_deleted: true});
  function perms(list) {
    return list.length ? list.flatMap((value, index) => perms(list.filter((_, other) => other !== index)).map(rest => [value, ...rest])) : [[]];
  }
  const permutations = perms([a, b, c, d]);
  const chain = (leaf, length) => Array.from({length}, (_, index) => 'h' + (leaf - index));
  const chunkK = {_id: 'h:own', _rev: '1-x', type: 'leaf', data: 'own'};
  // Every value a numeric string can take around the receive path's conversions.
  const STARTS = [
    ['negative', '-2'], ['plus', '+2'], ['thin-spaces', '\u20092\u2009'], ['spaced-negative', ' -1 '], ['nbsp-line-separator', '\u00a02\u2028'],
    ['byte-order-mark', '\ufeff2'], ['ideographic-space', '\u30002\u3000'], ['line-paragraph', '\u20282\u2029'],
    ['zero-width-space', '\u200b2'], ['mongolian-vowel-separator', '\u180e2'], ['next-line', '\u00852'],
    ['hex', '0x10'], ['octal', '0o20'], ['binary', '0b100'], ['exponent', '1e1'], ['decimal', '2.5'],
    ['leading-zeros', '0002'], ['negative-zero', '-0'], ['overflow', '1e400'], ['negative-overflow', '-1e400'],
    ['radix-overflow', '0x' + 'f'.repeat(300)], ['binary-overflow', '0b' + '1'.repeat(1100)],
    ['hex-rounded', '0x20000000000001'], ['infinity', 'Infinity'], ['negative-infinity', '-Infinity'],
    ['digits-overflow', '9'.repeat(5000)], ['negative-digits-overflow', '-' + '9'.repeat(5000)],
    ['digits-then-zeros-overflow', '1' + '0'.repeat(5000)], ['zeros-then-one', '0'.repeat(5000) + '1'],
    ['negative-zeros-then-one', '-' + '0'.repeat(5000) + '1'], ['double', '2'],
    ['not-a-number', 'x'], ['empty', ''], ['nan', 'NaN'], ['underscore', '1_0'], ['arabic-digit', '\u0662'],
    ['double-sign', '--2'], ['inner-space', '2 2'], ['signed-hex', '-0x10'], ['infinity-lowercase', 'infinity'],
    ['trailing-junk', '2x'], ['dot', '.'], ['exponent-sign-only', '1e+'],
  ];
  // A start that converts to a double no tree can hold. An adapter stores the position as JSON,
  // where it becomes null; merging a path at position 0 over it, pouchdb-merge subtracts
  // null from 0, takes the root for its own parent and throws (lib/index.js doMerge). So these
  // stay single files, and no random case is chained over them.
  const BOUNDED = STARTS.filter(([kind, start]) => Number.isFinite(Number(start)));
  // One metadata record as raw frames, for the numbers JSON.stringify cannot spell.
  // A hash nested in arrays `depth` deep, and a supplied tree whose single path is `length` long.
  const suppliedChain = length => {
    let ids = ['h0', {status: 'available'}, []];
    for (let index = 1; index < length; index++) ids = ['h' + index, {status: 'missing'}, [ids]];
    return ids;
  };
  const FRAMED = tail => '{"_id":"n","_rev":"1-a","type":"plain","path":"n.md","children":[]' + tail + '}\n';
  const HELLO_DIGEST = 'md5-' + createHash('md5').update('hello', 'binary').digest('base64');
  const withAttachment = attachments => ({_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [], _attachments: attachments});
  // Two documents whose id and revision spell one by-sequence slot. Only a record's own _rev is
  // validated; the revision it is kept under comes from _revisions, so A is kept as
  // "1-x::1-y" and `n` stores under "n::1-x::1-y", where `n::1-x` at "1-y" stores too.
  const A = {_id: 'n', _rev: '1-q', type: 'plain', path: 'n.md', children: [], mark: 'A',
    _revisions: {start: 1, ids: ['x::1-y']}};
  const B = {_id: 'n::1-x', _rev: '1-y', type: 'plain', path: 'n.md', children: [], mark: 'B',
    _revisions: {start: 1, ids: ['y']}};
  const NAMED = [
    ['created-updated-deleted', [[a], [doc('2-b', ['b', 'a'])], [doc('3-f', ['f', 'b', 'a'], {deleted: true})]]],
    ['logical-deletion-vs-edit', [[a], [doc('2-f', ['f', 'a'], {deleted: true})], [b]]],
    ['resolution-keeps-the-edit', [[a], [doc('2-f', ['f', 'a'], {deleted: true})], [b], [doc('3-e', ['e', 'f', 'a'], {_deleted: true})]]],
    ['resolution-discards-the-edit', [[a], [doc('2-f', ['f', 'a'], {deleted: true})], [b], [doc('3-e', ['e', 'b', 'a'], {_deleted: true})]]],
    ['resolution-in-one-file', [[a], [b, doc('2-f', ['f', 'a'], {deleted: true}), doc('3-e', ['e', 'f', 'a'], {_deleted: true})]]],
    ['restored-after-revision-deletion', [[a], [doc('2-f', ['f', 'a'], {_deleted: true})], [doc('3-b', ['b', 'f', 'a'])]]],
    ['ancestor-received-late', [[b], [a]]],
    ['ancestor-resent', [[a, b], [a], [doc('2-a', ['a'])]]],
    ['two-roots', [[a, doc('1-z', ['z'])]]],
    ['late-ancestor', [[b], [a]]],
    ['duplicate', [[a, b, b]]],
    ['duplicate-flags', [[a], [{...a, _deleted: true}]]],
    ['duplicate-flags-first', [[{...a, _deleted: true}], [a]]],
    ['no-chain', [[doc('1-a', null), doc('2-b', null)]]],
    ['mismatch-own', [[a], [doc('2-z', ['x', 'a'])]]],
    ['mismatch-tombstone', [[a], [doc('2-z', ['x', 'a'], {_deleted: true})]]],
    ['empty-own', [[a], [doc('2-b', ['', 'a'])]]],
    ['conflicting-ancestry', [[b], [doc('3-c', ['c', 'b', 'z'])], [a]]],
    ['conflicting-ancestry-reverse', [[doc('3-c', ['c', 'b', 'z'])], [b]]],
    ['hostile-tombstone', [[b], [doc('3-c', ['c', 'b', 'z'], {_deleted: true})]]],
    ['hostile-softdelete', [[b], [doc('3-c', ['c', 'b', 'z'], {deleted: true})]]],
    ['tomb-child', [[a], [doc('2-b', ['b', 'a'], {_deleted: true})], [c]]],
    ['tombstone-resend', [[a], [doc('2-b', ['b', 'a'], {_deleted: true})], [doc('2-b', ['b', 'a'], {_deleted: true})]]],
    ['array-flag', [[a], [doc('2-b', ['b', 'a'], {_deleted: []})]]],
    ['object-flag', [[a], [doc('2-b', ['b', 'a'], {_deleted: {}})]]],
    ['zero-flag', [[a], [doc('2-b', ['b', 'a'], {_deleted: 0})]]],
    ['both-flags', [[a], [doc('2-b', ['b', 'a'], {deleted: true, _deleted: true})]]],
    ['invalid-mid', [[a], [doc('3-c', ['c', null, 'a'], {_deleted: true})]]],
    ['broken-chain', [[a], [doc('3-c', ['c', {}, 'a'], {_deleted: true})]]],
    ['nonstring-parent', [[a], [doc('2-b', ['b', 7])]]],
    ['boolean-start', [[a], [doc('1-z', ['z'], {}, true)]]],
    ['null-start', [[a], [doc('2-z', ['z', 'q'], {}, null)]]],
    ['numeric-string-start', [[doc('3-x', ['x', 'a', 'q'], {}, '3')]]],
    ['float-start', [[doc('2-x', ['x', 'a'], {}, 2.5)]]],
    ['huge-start', [[doc('1-x', ['x'], {}, 1e21)]]],
    ['dashed-ancestor', [[doc('2-x', ['x', 'x-y'])]]],
    ['overflow', [[JSON.parse('{"_id":"n","_rev":"9223372036854775808-z","_revisions":{"start":9223372036854775808,"ids":["z"]},"type":"plain"}')]]],
    // Both records keep the revision "null-b". IDB merges the second one; the level adapter
    // ignores it because a body is already stored under that revision (the recorded divergence).
    ['level-ignored-kept-revision', [[{_id: 'n', _rev: '2-b', _deleted: true, _revisions: {start: null, ids: ['b']}, type: 'plain'}],
      [{_id: 'n', _rev: '3-b', _revisions: {start: null, ids: ['b', '', 'a']}, type: 'plain'}]]],
    ['unicode-tie', [[doc('1-\ue000', ['\ue000'])], [doc('1-😀', ['😀'])]]],
    ['cyclic-hash-names', [[doc('2-a', ['a', 'b'])], [doc('2-b', ['b', 'a'])]]],
    ['array-revisions', [[{_id: 'n', _rev: '1-a', _revisions: ['a']}]]],
    ['string-revisions', [[{_id: 'n', _rev: '1-a', _revisions: 'ab'}]]],
    ['array-like-ids', [[doc('2-x', {length: 2, 0: 'x', 1: 'a'})]]],
    ['string-ids', [[doc('2-b', 'ba')]]],
    ['missing-start', [[{_id: 'n', _rev: '1-x', _revisions: {ids: ['x']}}]]],
    ['empty-ids', [[{_id: 'n', _rev: '2-x', _revisions: {start: 2, ids: []}}]]],
    ['numeric-id', [[doc('2-7', [7, 'a'])]]],
    ['dashed-rev', [[{_id: 'n', _rev: '2-a-b', _revisions: {start: 2, ids: ['a-b', 'q']}}]]],
    ['invalid-rev', [[a], [{_id: 'n', _rev: 'x'}]]],
    ['invalid-rev-known', [[a], [{...a, _rev: 'x', _revisions: {start: 1, ids: ['a']}}]]],
    ['unknown-underscore-key', [[a], [{...b, _custom: 1}]]],
    ['unknown-underscore-key-known', [[a, b], [{...b, _custom: 1}]]],
    ['reserved-id', [[{_id: '_foo', _rev: '1-q'}]]],
    ['design-doc', [[{_id: '_design/dd', _rev: '1-q', language: 'javascript'}], [a]]],
    ['local-doc', [[{_id: '_local/x', _rev: '0-1', value: 1}, a]]],
    ['local-doc-after', [[a], [{_id: '_local/x', _rev: '0-1', value: 1}, b]]],
    ['batch-partial', [[a], [doc('2-b', ['b', 'a']), {_id: 'm', _rev: 'bad'}]]],
    ['stem-100', [[doc('102-h101', chain(101, 102), {_deleted: true})], [doc('1-h0', ['h0'])]]],
    ['stem-100-reverse', [[doc('1-h0', ['h0'])], [doc('102-h101', chain(101, 102), {_deleted: true})]]],
    ['stem-boundary-99', [[doc('100-h99', chain(99, 100))], [doc('1-h0', ['h0'])]]],
    ['stem-boundary-101', [[doc('101-h100', chain(100, 101))], [doc('1-h0', ['h0'])]]],
    ['stem-late-ancestor', [[doc('150-h149', chain(149, 150))], [doc('60-h59', chain(59, 60))]]],
    ['stem-child-of-stemmed', [[doc('150-h149', chain(149, 150))], [doc('61-c', ['c', ...chain(59, 60)])]]],
    ['stem-late-tombstone', [[doc('150-h149', chain(149, 150), {_deleted: true})], [doc('1-h0', ['h0'])], [doc('100-h99', chain(99, 100))]]],
    ['stem-resent-leaf', [[doc('150-h149', chain(149, 150))], [doc('100-h99', chain(99, 100))], [doc('151-e', ['e', ...chain(149, 150)])], [doc('100-h99', chain(99, 100))]]],
    ['chunk-create', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc'}], [{_id: 'h:k', _rev: '1-y', type: 'leaf', data: 'other'}]]],
    ['chunk-duplicate-identical', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc'}, {_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc'}], [a]]],
    ['chunk-duplicate-different', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc'}, {_id: 'h:k', _rev: '1-y', type: 'leaf', data: 'other'}]]],
    ['chunk-deleted-then-live', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', _deleted: true}, {_id: 'h:k', _rev: '1-y', type: 'leaf', data: 'live'}]]],
    ['chunk-deleted', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', _deleted: true}]]],
    ['chunk-logical-delete', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', deleted: true}]]],
    ['chunk-unknown-key', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', _custom: 1}, a]]],
    ['chunk-numbers', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', n: 1.0, m: 1e21, o: 1e-7, p: 0.1, q: -0}]]],
    // A committed chunk batch stays when the metadata batch is refused.
    ...[
      [{...doc('2-b', ['b', 'a']), _custom: 1}], [doc('bad')], [doc('2-a', [1])],
      [doc('1-a', ['a'], {_id: '_local/x'})], [doc('1-a', ['a'], {_id: '_design/x'})],
    ].map((metadata, index) => ['chunk-plus-meta' + index, [[chunkK, ...metadata]]]),
    ...[
      [chunkK], [chunkK, chunkK], [{...chunkK, _deleted: true}], [{...chunkK, deleted: true}],
      [chunkK, doc('1-a', ['a'])], [chunkK, {...chunkK, _deleted: true}], [{...chunkK, _custom: 1}, doc('1-a', ['a'])],
    ].map((records, index) => ['chunks-' + index, [records, records]]),
    // The plugin's own journalMetadataRevisions fails on these names.
    ...['constructor', '__proto__', 'toString', 'hasOwnProperty'].map(name =>
      ['id-' + name, [[doc('1-a', ['a'], {_id: name}), doc('1-a', ['a'], {_id: 'other'})]]]),
    ['id-other', [[doc('1-a', ['a'], {_id: 'other'}), doc('1-a', ['a'], {_id: 'other'})]]],
    ['id-design-no-slash', [[doc('1-a', ['a'], {_id: '_designno'})]]],
    ['rev-arabic-digit', [[doc('\u0661-a', null)]]], ['rev-arabic-digit-chain', [[doc('\u0661-a', ['a'])]]],
    ...['1-', '0-a', '01-a', '1-a-b', 'x1-a', '1.0-a', '-1-a', '1-a\n'].map(rev =>
      ['rev-form-' + JSON.stringify(rev).slice(1, -1), [[doc(rev, null)]]]),
    ...[0, 1, false, true, null, {}, [], [1], {x: 2}, '\ud83d\ude00', '\ue000'].map((hash, index) =>
      ['hash-' + index, [[doc('1-a', [hash])]]]),
    ...[{toString: 1}, {toString: null}, {toString: []}, {toString: {}, valueOf: 1}, {valueOf: 1}].map((hash, index) =>
      ['hash-own-to-string-' + index, [[doc('1-a', [hash])]]]),
    ['ids-array-like-full', [[doc('2-z', {0: 'z', 1: 'a', length: 2})]]],
    ['ids-array-like-short', [[doc('2-z', {0: 'z', length: 1})]]],
    // parseDoc keeps a supplied tree and leaves the revision unset.
    ['supplied-rev-tree', [[{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [],
      _rev_tree: [{pos: 1, ids: ['z', {status: 'available'}, []]}]}]]],
    ['supplied-rev-tree-two-roots', [[{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [],
      _rev_tree: [{pos: 1, ids: ['z', {status: 'available'}, []]}, {pos: 2, ids: ['y', {status: 'available'}, []]}]}]]],
    ['supplied-rev-tree-existing', [[doc('1-a', ['a'])], [{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [],
      _rev_tree: [{pos: 2, ids: ['w', {status: 'available'}, []]}]}]]],
    ['mixed-documents', [[doc('2-b', ['b', 'a']), doc('2-c', ['c', 'a'], {_id: 'other'}), doc('2-c', ['c', 'a'])],
      [doc('1-a', ['a']), doc('1-a', ['a'], {_id: 'other'})]]],
    ['late-stem-130', [[doc('130-h130', chain(130, 130))], [doc('1-h1', ['h1'])], [doc('130-h130', chain(130, 130))]]],
    // Attachments: prepared and verified before any write, stored as stubs.
    ...[
      ['valid', 'aGVsbG8='], ['unpadded', 'aGVsbG8'], ['whitespace', 'aGVs bG8='], ['invalid', '!'],
      ['empty', ''], ['one-character', 'a'], ['padding-inside', 'a=bc'], ['three-pads', 'aGVsbG8==='],
      ['non-ascii', 'aG\u00e9sbG8='], ['trailing-bits', 'AB=='],
    ].map(([kind, data]) => ['attachment-' + kind, [[withAttachment({x: {content_type: 'text/plain', data}})]]]),
    ['attachment-without-content-type', [[withAttachment({x: {data: 'aGVsbG8='}})]]],
    ['attachment-two', [[withAttachment({a: {content_type: 'text/plain', data: 'aGVsbG8='},
      b: {content_type: 'application/octet-stream', data: '//4='}})]]],
    ['attachment-name-underscore', [[withAttachment({_x: {content_type: 'text/plain', data: 'aGVsbG8='}})]]],
    // An attachment that is a string: the device hands a non-string to its hash; engines differ.
    ['attachment-value-string', [[withAttachment({content_type: 'text/plain', data: 'aGVsbG8='})]]],
    ['attachment-value-null', [[withAttachment({x: null})]]],

    ['attachment-stub-unknown', [[withAttachment({x: {stub: true, content_type: 'text/plain', digest: HELLO_DIGEST + '0'}})]]],
    ['attachment-stub-same-batch', [[withAttachment({x: {content_type: 'text/plain', data: 'aGVsbG8='}}),
      doc('1-b', ['b'], {_id: 'm', _attachments: {x: {stub: true, content_type: 'text/plain', digest: HELLO_DIGEST}}})]]],
    ['attachment-stub-later-file', [[withAttachment({x: {content_type: 'text/plain', data: 'aGVsbG8='}})],
      [doc('1-b', ['b'], {_id: 'm', _attachments: {x: {stub: true, content_type: 'text/plain', digest: HELLO_DIGEST}}})]]],
    ['attachment-of-deleted-record', [[doc('1-a', ['a'], {_deleted: true,
      _attachments: {x: {content_type: 'text/plain', data: '!'}}})]]],
    ['attachment-on-chunk-record', [[{...chunkK, _attachments: {x: {content_type: 'text/plain', data: '!'}}}]]],
    // The plugin's own decoder (journalRecords.ts:9-27,80-103) judges every frame first.
    ['decoder-empty-chunk-revision', [[{_id: 'h:own', _rev: '', type: 'leaf', data: 'own'}]]],
    ['decoder-chunk-without-data', [[{_id: 'h:own', _rev: '1-x', type: 'leaf'}]]],
    ['decoder-chunk-without-revision', [[{_id: 'h:own', type: 'leaf', data: 'own'}]]],
    ['decoder-chunk-id-too-short', [[{_id: 'h:', _rev: '1-x', type: 'leaf', data: 'own'}]]],
    ['decoder-array-record', ['[]\n']],
    ['decoder-empty-line', ['\n' + JSON.stringify(doc('1-a', ['a'])) + '\n']],
    ['decoder-missing-revision', [JSON.stringify({_id: 'n', type: 'plain'}) + '\n']],
    // Compact chunk records carry no revision; the device creates the chunk once.
    ['decoder-compact-frames', ['~h:compact\u001fhello\n' + JSON.stringify(doc('1-a', ['a'], {children: ['h:compact']})) + '\n']],
    ['decoder-compact-escaped-frames', ['~h:compact\u001f\\fline\\nnext\n' + JSON.stringify(doc('1-a', ['a'], {children: ['h:compact']})) + '\n']],
    ['chunk-key-order', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', b: 1, 1: 'x', a: 2, 0: 'y'}]]],
    ['chunk-key-order-long-key', [[{_id: 'h:k', _rev: '1-x', type: 'leaf', data: 'abc', b: 1, ['9'.repeat(5000)]: 'x',
      10: 'y', 4294967294: 'z', 4294967295: 'w', a: 3}]]],
    // Numbers spelled as strings, strings used as numbers: ToNumber of _revisions.start and of a
    // radix literal, parseInt of a revision's position, and the concatenations parseDoc makes of
    // them. The negative and the thin-space start are the third-round review's cases; the rest
    // close the class around them. A file then adds a child, so the held tree is used again.
    ...STARTS.map(([kind, start]) => ['start-' + kind, [[doc('1-x', ['x'], {}, start)]]]),
    ...BOUNDED.map(([kind, start]) => ['start-' + kind + '-then-child',
      [[doc('1-x', ['x'], {}, start)], [doc('2-c', ['c', 'x'])]]]),
    ...[
      ['negative-prefix', '-2-x'], ['digits-overflow-prefix', '9'.repeat(5000) + '-x'],
      ['zeros-then-one-prefix', '0'.repeat(5000) + '1-x'], ['thin-space-prefix', '\u20091-x'],
      ['exponent-prefix', '1e2-x'], ['many-hyphens', '1-a-b-c'], ['no-position', '-x'], ['plus-prefix', '+1-x'],
      ['fullwidth-digit-prefix', '\uff11-x'], ['zero-prefix', '0-x'], ['tab-prefix', '\t1-x'],
    ].map(([kind, rev]) => ['own-rev-' + kind, [[doc(rev, null)]]]),
    // A hash with a hyphen: core get compares the part between the first two hyphens.
    ...['a-b', '-', '--', '1-x', 'x-y-z', 'a\n-b'].map((hash, index) =>
      ['hash-with-hyphen-' + index, [[doc('1-a', [hash])]]]),
    ['hash-with-hyphen-ancestor', [[doc('1-a', ['a-b'])], [doc('2-c', ['c', 'a-b'])]]],
    ['hash-with-hyphen-among-siblings', [[doc('2-b', ['b', 'a'])], [doc('2-a-b', ['a-b', 'a'])]]],
    ['hash-with-hyphen-child', [[doc('1-a', ['a'])], [doc('2-a-b', ['a-b', 'a'])], [doc('3-c', ['c', 'a-b', 'a'])]]],
    ['chunk-plus-start-radix-overflow',
      [[chunkK, doc('1-x', ['x'], '0x' + 'f'.repeat(300), '0x' + 'f'.repeat(300))]]],
    // Compact chunk records are documents of the file's chunk batch (journalRecords.ts:31-50,
    // journalReceive.ts:17-29): a chunk the device holds is kept as it is, two records for one
    // chunk in a batch fail it after the first is written, and the revision is the hash of the
    // document the decoder makes. The third-round parity review's cases first.
    ['compact-create', ['~h:compact\x1fhello\n']],
    ['full-then-compact', [[{_id: 'h:mix', _rev: '1-x', type: 'leaf', data: 'json'}],
      '~h:mix\x1fcompact\n' + JSON.stringify(doc('1-a', ['a'], {children: ['h:mix']})) + '\n']],
    ['compact-then-full', ['~h:mix\x1fcompact\n', [{_id: 'h:mix', _rev: '1-x', type: 'leaf', data: 'json'}]]],
    ['compact-full-same', ['~h:mix\x1fcompact\n' + JSON.stringify({_id: 'h:mix', _rev: '1-x', type: 'leaf', data: 'json'})
      + '\n' + JSON.stringify(doc('1-a', ['a'])) + '\n']],
    ['full-compact-same', [JSON.stringify({_id: 'h:mix', _rev: '1-x', type: 'leaf', data: 'json'})
      + '\n~h:mix\x1fcompact\n' + JSON.stringify(doc('1-a', ['a'])) + '\n']],
    ['compact-duplicate', ['~h:mix\x1fcompact\n~h:mix\x1fcompact2\n' + JSON.stringify(doc('1-a', ['a'])) + '\n']],
    ['compact-duplicate-identical', ['~h:mix\x1fcompact\n~h:mix\x1fcompact\n' + JSON.stringify(doc('1-a', ['a'])) + '\n']],
    ['compact-then-compact', ['~h:mix\x1fcompact\n', '~h:mix\x1fcompact2\n']],
    ['compact-then-deleted-full', ['~h:mix\x1fcompact\n',
      [{_id: 'h:mix', _rev: '1-x', type: 'leaf', data: 'json', _deleted: true}], '~h:mix\x1fagain\n']],
    ['compact-twice-files', ['~h:mix\x1fcompact\n', '~h:mix\x1fcompact\n']],
    // Attachments as core's bulkDocs sees them: checked on every record before cleanDocs drops
    // those of a deleted one, projected to the six known keys before the record is hashed, and
    // verified against the store after the chunk batch has committed its own.
    ['chunk-attachment-then-metadata-stub', [[{...chunkK, _id: 'h:cross', _attachments: {x: {content_type: 'text/plain', data: 'aGVsbG8='}}},
      doc('1-a', ['a'], {_attachments: {x: {stub: true, content_type: 'text/plain', digest: HELLO_DIGEST}}})]]],
    ['chunk-attachment-then-metadata-stub-later-file', [[{...chunkK, _id: 'h:cross', _attachments: {x: {content_type: 'text/plain', data: 'aGVsbG8='}}}],
      [doc('1-a', ['a'], {_attachments: {x: {stub: true, content_type: 'text/plain', digest: HELLO_DIGEST}}})]]],
    ['chunk-attachment-name', [[{...chunkK, _attachments: {_bad: {content_type: 'text/plain', data: 'aGVsbG8='}}}]]],
    ['chunk-attachment-null', [[{...chunkK, _attachments: {x: null}}]]],
    ['attachment-array', [[withAttachment([{content_type: 'text/plain', data: 'aGVsbG8='}])]]],
    ['attachment-array-two', [[withAttachment([{content_type: 'text/plain', data: 'aGVsbG8='},
      {content_type: 'application/octet-stream', data: '//4='}])]]],
    ['attachment-array-extra-fields', [[withAttachment([{secret_extra: 1, content_type: 'text/plain', data: 'aGVsbG8=', custom: 'v'},
      {data: '//4=', revpos: 9, content_type: 'application/octet-stream'}])]]],
    ['attachment-array-empty', [[withAttachment([])]]],
    ['attachment-array-element-null', [[withAttachment([null])]]],
    ['attachment-array-element-number', [[withAttachment([5])]]],
    ['attachment-array-stub-unknown', [[withAttachment([{stub: true, content_type: 'text/plain', digest: HELLO_DIGEST + '0'}])]]],
    ['attachment-array-stub-later-file', [[withAttachment([{content_type: 'text/plain', data: 'aGVsbG8='}])],
      [withAttachment([{stub: true, content_type: 'text/plain', digest: HELLO_DIGEST}])]]],
    ['attachment-extra-fields', [[withAttachment({x: {secret_extra: 1, content_type: 'text/plain', data: 'aGVsbG8=', custom: 'v'}})]]],
    ['attachment-value-number', [[withAttachment({x: 5})]]],
    ['attachment-object-value-string', [[withAttachment({x: 's'})]]],
    ['attachment-container-string', [[withAttachment('ab')]]],
    ['attachment-container-number', [[withAttachment(5)]]],
    ['attachment-container-zero', [[withAttachment(0)]]],
    ['chunk-attachment-extra-fields', [[{...chunkK, _attachments: {x: {secret_extra: 1, content_type: 'text/plain', data: 'aGVsbG8=', custom: 'v'}}}]]],
    ['chunk-attachment-key-order', [[{_id: 'h:one', _rev: '1-x', type: 'leaf', data: 'abc', _attachments: {x: {content_type: 'text/plain', data: 'aGVsbG8='}}},
      {_id: 'h:two', _rev: '1-x', type: 'leaf', data: 'abc', _attachments: {x: {data: 'aGVsbG8=', content_type: 'text/plain'}}}]]],
    ['chunk-attachment-deleted', [[{...chunkK, _deleted: true, _attachments: {x: {content_type: 'text/plain', data: 'aGVsbG8='}}}]]],
    ['deleted-metadata-attachment-name', [[doc('1-a', ['a'], {_deleted: true, _attachments: {_bad: {data: 'eA==', content_type: 'text/plain'}}}),
      doc('1-b', ['b'], {_id: 'other'})]]],
    ['deleted-metadata-attachment-null', [[doc('1-a', ['a'], {_deleted: true, _attachments: {x: null}}), doc('1-b', ['b'], {_id: 'other'})]]],
    ['deleted-chunk-attachment-name', [[{...chunkK, _deleted: true, _attachments: {_bad: {data: 'eA==', content_type: 'text/plain'}}}]]],
    ['deleted-chunk-attachment-null', [[{...chunkK, _deleted: true, _attachments: {x: null}}]]],
    ['soft-deleted-metadata-attachment-name', [[doc('1-a', ['a'], {deleted: true, _attachments: {_bad: {data: 'eA==', content_type: 'text/plain'}}})]]],
    // A JSON record carries a lone UTF-16 unit as an escape, and a device holds the chunk and
    // the metadata after it. The fourth-round reviews' case, then the same file again.
    ['lone-chunk-data', [[{_id: 'h:abc', _rev: '', type: 'leaf', data: '\ud800'},
      doc('1-a', ['a'], {children: ['h:abc']})]]],
    ['lone-chunk-data-then-again', [[{_id: 'h:abc', _rev: '', type: 'leaf', data: '\ud800'},
      doc('1-a', ['a'], {children: ['h:abc']})], [{_id: 'h:abc', _rev: '', type: 'leaf', data: '\ud800'},
      doc('1-a', ['a'], {children: ['h:abc']})]]],
    ['lone-chunk-data-two-chunks', [[{_id: 'h:one', _rev: '', type: 'leaf', data: '\ud800'},
      {_id: 'h:two', _rev: '', type: 'leaf', data: 'ok'}, doc('1-a', ['a'], {children: ['h:one', 'h:two']})]]],
    // A revision whose hash holds a colon. The IndexedDB adapter stores a body under `id::rev`
    // and reads `_id` and `_rev` back off that key, cut at its last colon (decodeDoc): a device
    // reads `_rev:"b"` for "1-a:b", its chained read fails and a write over it is refused. The
    // fifth-round parity review's cases, then the shapes around them.
    ['rev-colon', [[doc('1-a:b', ['a:b'])]]],
    ['rev-colon-then-child', [[doc('1-a:b', ['a:b'])], [doc('2-c', ['c', 'a:b'])]]],
    ['rev-colon-own-only', [[doc('1-a:b', null)]]],
    ['rev-colon-hash-only', [[doc('1-a', ['a:b'])]]],
    ['rev-double-colon', [[doc('1-a::b', ['a::b'])]]],
    ['rev-colon-only', [[doc('1-:', [':'])]]],
    ['rev-two-colons', [[doc('1-a:b:c', ['a:b:c'])]]],
    ['rev-colon-losing-leaf', [[doc('2-a:b', ['a:b']), doc('2-c', ['c'])]]],
    ['rev-colon-deleted', [[doc('1-a:b', ['a:b'], {_deleted: true})]]],
    ['rev-colon-padded', [[doc('1-a:b', ['a:b'], {}, ' 1 ')]]],
    ['rev-colon-logical-delete', [[doc('1-a:b', ['a:b'], {deleted: true})]]],
    // Two documents whose id and revision spell one by-sequence slot: the later write
    // takes the slot, as `bySeqStore.put` does.
    ...[
      ['slot-collision', [[A, B]]], ['slot-collision-reverse', [[B, A]]],
      ['slot-collision-two-files', [[A], [B]]], ['slot-collision-two-files-reverse', [[B], [A]]],
      ['slot-collision-replayed', [[A, B], [A]]],
      ['slot-collision-then-child', [[B, doc('2-z', ['z', 'y'], {_id: 'n::1-x'})]]],
      // A merge that stems the shared slot's revision away: IDB compacts the slot, and the
      // other document's body goes with it (finishDoc, compactRevs). The sixth-round reviews' case.
      ...(() => {
        const stem = {_id: 'n', _rev: '101-h100', type: 'plain', path: 'n.md', children: [],
          _revisions: {start: 101, ids: [...chain(100, 100), 'x::1-y']}};
        return [['slot-collision-stemmed', [[A, B], [stem]]], ['slot-collision-stemmed-reverse', [[B, A], [stem]]]];
      })(),
      // The other document's body spells a longer chain: a file put over the first document
      // takes that revision for its parent and nothing above it; a deletion is written into
      // the other document. With a tombstone in the shared slot the raw get of the first
      // document returns it with `_deleted`, as it was stored.
      ...(() => {
        const first = {...A, _revisions: {start: 1, ids: ['x::2-y']}};
        const other = {_id: 'n::1-x', _rev: '2-y', type: 'plain', path: 'n.md::1-x', children: [],
          _revisions: {start: 2, ids: ['y', 'z']}};
        const att = {_attachments: {x: {data: 'eA==', content_type: 'text/plain'}}};
        const tombstone = {...other, _deleted: true};
        return [
          ['slot-collision-deep', [[first, other]]], ['slot-collision-deep-reverse', [[other, first]]],
          ['slot-collision-deep-files', [[first], [other]]],
          ...[false, true].map(withAttachment => ['slot-collision-tombstone-batch' + (withAttachment ? '-attachment' : ''),
            [[{...first, ...(withAttachment ? att : {})}, {...tombstone, ...(withAttachment ? att : {})}]]]),
          ...[false, true].map(withAttachment => ['slot-collision-tombstone-files' + (withAttachment ? '-attachment' : ''),
            [[{...first, ...(withAttachment ? att : {})}], [{...tombstone, ...(withAttachment ? att : {})}]]]),
          ...[false, true].map(withAttachment => ['slot-collision-tombstone-replay' + (withAttachment ? '-attachment' : ''),
            [[{...first, ...(withAttachment ? att : {})}], [{...tombstone, ...(withAttachment ? att : {})}],
              [{...tombstone, ...(withAttachment ? att : {})}, {...first, ...(withAttachment ? att : {})}]]]),
          ...[false, true].map(withAttachment => ['slot-collision-tombstone-reverse' + (withAttachment ? '-attachment' : ''),
            [[{...tombstone, ...(withAttachment ? att : {})}, {...first, ...(withAttachment ? att : {})}]]]),
          // The first document's tree names the revision the tombstone spells: its chained read
          // returns the other document's body, and the plugin finds it deleted.
          ['slot-collision-named-tombstone', [[
            {_id: 'n', _rev: '1-q', type: 'plain', path: 'n.md', children: [], mark: 'A',
              _revisions: {start: 2, ids: ['x::1-y', 'q']}},
            {_id: 'n', _rev: '1-y', type: 'plain', path: 'n.md', children: [], mark: 'A2',
              _revisions: {start: 1, ids: ['y']}},
            {_id: 'n::2-x', _rev: '1-y', type: 'plain', path: 'n::2-x', children: [], mark: 'B', _deleted: true,
              _revisions: {start: 1, ids: ['y']}}]]],
        ];
      })(),
      // An attachment on a revision a merge stems away: no slot maps its digest any more, and a
      // stub that names it later is refused before anything of that batch is written.
      ['attachment-stemmed', [[{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [],
        _attachments: {x: {data: 'eA==', content_type: 'text/plain'}}, _revisions: {start: 1, ids: ['a']}},
        {_id: 'n', _rev: '101-h100', type: 'plain', path: 'n.md', children: [],
          _revisions: {start: 101, ids: [...chain(100, 100), 'a']}}],
        [{_id: 'm', _rev: '1-s', type: 'plain', path: 'm.md', children: [],
          _attachments: {x: {stub: true, content_type: 'text/plain', digest: 'md5-' + createHash('md5').update('x').digest('base64')}},
          _revisions: {start: 1, ids: ['s']}}]]],
      // Records of two documents in one batch: the records of one document are written one after
      // the other while the other document's are written at once, so over a shared slot the
      // second record of the first document is stored last, with attachment data stored before a
      // body and a merge's stemming compacting after it (processDocs, writeAttachments, finishDoc,
      // compactRevs). The seventh-round architecture review's case, then the shapes around it.
      ...(() => {
        const first = doc('1-a', ['a']);
        const shared = {...A, _rev: '1-q', _deleted: true};
        const data = {_attachments: {x: {data: 'eA==', content_type: 'text/plain'}}};
        const long = {...shared, _rev: '101-h100', _revisions: {start: 101, ids: [...chain(100, 100), 'x::1-y']}};
        return [
          ['slot-collision-groups', [[first, shared, B]]],
          ['slot-collision-groups-reverse', [[B, shared, first]]],
          ['slot-collision-groups-first-attachment', [[{...first, ...data}, shared, B]]],
          ['slot-collision-groups-other-attachment', [[first, shared, {...B, ...data}]]],
          ['slot-collision-groups-both-attachment', [[{...first, ...data}, shared, {...B, ...data}]]],
          ['slot-collision-groups-compaction', [[first, long, B]]],
          ['slot-collision-groups-compaction-attachment', [[first, long, {...B, ...data}]]],
          ['slot-collision-groups-live', [[first, {...shared, _deleted: undefined}, B]]],
          // A record imported again under a revision its tree does not name by string puts its
          // body over a taken key: the failed request, its error handler and the lookup each take
          // a step, and the second records of the two documents change places in the queue.
          ['slot-collision-groups-taken', [[doc('1-x', [7])],
            [doc('1-x', [7], {mark: 'AGAIN'}),
              {...A, _rev: '1-q', mark: 'A2', _revisions: {start: 1, ids: ['x::2-w']}},
              {...B, mark: 'B1', _revisions: {start: 1, ids: ['y']}},
              {_id: 'n::1-x', _rev: '2-w', type: 'plain', path: 'n.md::1-x', children: [], mark: 'B2',
                _revisions: {start: 2, ids: ['w', 'y']}}]]],
        ];
      })(),
      // Attachment mappings accumulate on a slot: a body written over the slot later adds
      // its own and removes none, and an attachment is dropped only once no slot maps it. The
      // seventh-round parity review's five files; then two digests of one body parting ways.
      ...(() => {
        const data = {x: {data: 'eA==', content_type: 'text/plain'}};
        const other = {x: {data: 'eQ==', content_type: 'text/plain'}};
        const stub = digest => ({x: {stub: true, content_type: 'text/plain', digest}});
        const digest = text => 'md5-' + createHash('md5').update(text).digest('base64');
        const long = (id, root, flags = {}) => ({_id: id, _rev: '101-h100', type: 'plain', path: id + '.md', children: [],
          ...flags, _revisions: {start: 101, ids: [...chain(100, 100), root]}});
        return [
          ['attachment-mapping-accumulates', [
            [{...A, _attachments: data}],
            [B],
            [{_id: 'c', _rev: '1-c', type: 'plain', path: 'c.md', children: [], _attachments: stub(digest('x')),
              _revisions: {start: 1, ids: ['c']}}],
            [long('c', 'c')],
            [{_id: 'm', _rev: '1-m', type: 'plain', path: 'm.md', children: [], _attachments: stub(digest('x')),
              _revisions: {start: 1, ids: ['m']}}]]],
          ['attachment-two-digests', [
            [{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [], _attachments: {x: data.x, y: other.x},
              _revisions: {start: 1, ids: ['a']}}],
            [{_id: 'k', _rev: '1-k', type: 'plain', path: 'k.md', children: [], _attachments: data,
              _revisions: {start: 1, ids: ['k']}}],
            [long('n', 'a')],
            [{_id: 'm', _rev: '1-m', type: 'plain', path: 'm.md', children: [], _attachments: stub(digest('x')),
              _revisions: {start: 1, ids: ['m']}}],
            [{_id: 'o', _rev: '1-o', type: 'plain', path: 'o.md', children: [], _attachments: stub(digest('y')),
              _revisions: {start: 1, ids: ['o']}}]]],
        ];
      })(),
      // A merge's compaction and another document's attachments in one batch: a request's event
      // is dispatched before the next request is served, so the cursor over the shared slot's
      // mappings opens before the other document's mappings are put, and both attachments stay;
      // a digest nothing holds is still refused. The eighth-round architecture review's witness.
      ...(() => {
        const stub = (id, text) => ({_id: id, _rev: '1-s', type: 'plain', path: id + '.md', children: [],
          _attachments: {x: {stub: true, digest: 'md5-' + createHash('md5').update(text).digest('base64'),
            content_type: 'text/plain'}}, _revisions: {start: 1, ids: ['s']}});
        return [['attachment-compaction-order', [
          [doc('101-h100', [...chain(100, 100), 'x::1-y']),
            {...B, _attachments: {0: {data: 'eQ==', content_type: 'text/plain'},
              1: {data: 'eA==', content_type: 'text/plain'}}}, doc('1-z', ['z'])],
          [stub('stubx', 'x')], [stub('stuby', 'y')], [stub('stubz', 'z')]]]];
      })(),
      // A merge stems the root away from a long path while a shorter path keeps it: the adapter
      // compacts only what no path keeps. The short branch first, then the long chain; and the
      // long chain first, after which the root returns without a body.
      ['stemmed-branch-kept', [[doc('1-r', ['r']), doc('2-b', ['b', 'r'])],
        [{...doc('101-h100', [...chain(100, 100), 'r']), _id: 'n'}]], true],
      ['stemmed-branch-cut-then-named', [[doc('1-r', ['r']), {...doc('101-h100', [...chain(100, 100), 'r']), _id: 'n'}],
        [doc('2-b', ['b', 'r'])]], true],
      // The same root kept within the limit: the stub is admitted.
      ['attachment-stem-kept', [[{_id: 'n', _rev: '1-a', type: 'plain', path: 'n.md', children: [],
        _attachments: {x: {data: 'eA==', content_type: 'text/plain'}}, _revisions: {start: 1, ids: ['a']}},
        {_id: 'n', _rev: '100-h99', type: 'plain', path: 'n.md', children: [],
          _revisions: {start: 100, ids: [...chain(99, 99), 'a']}}],
        [{_id: 'm', _rev: '1-s', type: 'plain', path: 'm.md', children: [],
          _attachments: {x: {stub: true, content_type: 'text/plain', digest: 'md5-' + createHash('md5').update('x').digest('base64')}},
          _revisions: {start: 1, ids: ['s']}}]]],
    ],
    // A record nested as deep as the peer's decoder admits (its budget is 512 levels): a device
    // holds and hashes it like any other. The fifth-round architecture review's cases first, then
    // the same shapes around them; raw frames carry the nesting.
    ...[
      ['deep-chunk', '{"_id":"h:deep","_rev":"","type":"leaf","data":"x","extra":NEST(505, 1)}'],
      ['deep-chunk-after-chunk', '{"_id":"h:plain","_rev":"","type":"leaf","data":"p"}\n{"_id":"h:deep","_rev":"","type":"leaf","data":"x","extra":NEST(505, 1)}'],
      ['deep-chunk-at-the-budget', '{"_id":"h:deep","_rev":"","type":"leaf","data":"x","extra":NEST(507, 1)}'],
      ['deep-hash-after-chunk', '{"_id":"h:abc","_rev":"","type":"leaf","data":"x"}\n{"_id":"n","_rev":"1-a","_revisions":{"start":1,"ids":[NEST(505, 1)]}}'],
      ['deep-hash-array', '{"_id":"n","_rev":"1-a","_revisions":{"start":1,"ids":[[null,"a",[true,[],1.5]]]}}'],
      ['deep-metadata-field', '{"_id":"n","_rev":"1-a","type":"plain","path":"n.md","children":[],"extra":NEST(511, 1)}'],
      ['deep-attachment-digest', '{"_id":"n","_rev":"1-a","type":"plain","path":"n.md","children":[],"_attachments":{"x":{"content_type":"text/plain","data":"eA==","digest":NEST(505, 1)}}}'],
      // Two container levels per link: 250 links stay inside the decoder's budget of 512.
      ['deep-supplied-tree', ['deep', 250]],
    ].map(([kind, frames]) => [kind, [(frames[0] === 'deep'
      ? '{"_id":"n","_rev":"1-a","_rev_tree":[{"pos":1,"ids":' + JSON.stringify(suppliedChain(frames[1])) + '}]}'
      : frames.replace(/NEST\((\d+), 1\)/g, (_, depth) => '['.repeat(Number(depth)) + '"x"' + ']'.repeat(Number(depth)))) + '\n']]),
    // A number beyond every double in a chunk body: the deterministic revision is the hash of
    // JSON.stringify's spelling, which writes such a number as null. Raw frames carry the literal.
    ...[
      ['chunk-nonfinite-hash', '{"_id":"h:abc","_rev":"","type":"leaf","data":"abc","mtime":1e999}'],
      ['chunk-nonfinite-hash-negative', '{"_id":"h:neg","_rev":"","type":"leaf","data":"abc","mtime":-1e999}'],
      ['chunk-nonfinite-hash-array', '{"_id":"h:arr","_rev":"","type":"leaf","data":"abc","x":[1e999]}'],
      ['chunk-nonfinite-hash-object', '{"_id":"h:obj","_rev":"","type":"leaf","data":"abc","x":{"y":1e999}}'],
      ['chunk-nonfinite-hash-many-digits', '{"_id":"h:big","_rev":"","type":"leaf","data":"abc","n":' + '9'.repeat(400) + '}'],
    ].map(([kind, frame]) => [kind, [frame + '\n'
      + JSON.stringify(doc('1-a', ['a'], {children: [JSON.parse(frame)._id]})) + '\n']]),
    // Revision names with a lone UTF-16 unit: a string used as ids names its units, and a hash
    // or an id may be one. JSON carries it as an escape; a compact line could not.
    ['string-ids-astral', [[doc('2-a', '\ud83d\ude00')]]],
    ['string-ids-astral-then-child', [[doc('2-a', '\ud83d\ude00')], [doc('3-c', ['c', '\ud83d'])]]],
    ['lone-surrogate-hash', [[doc('1-a', ['\ud800'])]]],
    ['lone-surrogate-rev', [[doc('1-\ud800', null)]]],
    ['lone-surrogate-id', [[doc('1-a', ['a'], {_id: '\ud800'})]]],
    ['lone-surrogate-attachment-digest', [[withAttachment({x: {stub: true, content_type: 'text/plain', digest: '\ud800'}})]]],
    // Number literals beyond a double's range or precision, which JSON.stringify cannot spell.
    ...[
      ['number-integer-overflow', ',"mtime":' + '9'.repeat(5000)],
      ['number-integer-overflow-short', ',"mtime":' + '9'.repeat(400)],
      ['number-negative-integer-overflow-short', ',"mtime":-' + '9'.repeat(400)],
      ['number-negative-integer-overflow', ',"mtime":-' + '9'.repeat(5000)],
      ['number-exponent-overflow', ',"mtime":1e999'],
      ['number-negative-exponent-overflow', ',"mtime":-1e999'],
      ['number-exact-integer', ',"mtime":9007199254740993,"size":12345678901234567890'],
      ['number-double-integer', ',"mtime":123456789012345680000,"size":-9007199254740992'],
      ['number-negative-zero', ',"mtime":-0,"size":-0.0'],
      ['number-underflow', ',"mtime":1e-999'],
      ['number-nested-overflow', ',"eden":{"x":[' + '7'.repeat(4500) + ',1e500,-2e500]}'],
    ].map(([kind, tail]) => [kind, [FRAMED(tail)]]),
  ];
  for (let index = 0; index < permutations.length; index++) {
    NAMED.push(['perm' + index, permutations[index].map(record => [record])]);
    NAMED.push(['batch' + index, [permutations[index]]]);
  }

  function mulberry32(seed) {
    return () => {
      seed = (seed + 0x6D2B79F5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const seed = 0x3c202611;
  const LEAVES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', '0', '7', 'ab', 'A', '\ue000', '😀', ''];
  const ENTRIES = [...LEAVES, 'x-y', 7, 0, 1.5, true, false, null, [], {}, [1], {length: 1}];
  const FLAGS = [undefined, undefined, undefined, undefined, undefined, {_deleted: true}, {deleted: true},
    {_deleted: []}, {_deleted: {}}, {_deleted: 0}, {_deleted: ''}, {deleted: [], _deleted: 1}];
  const hex = rng => Array.from({length: 32}, () => '0123456789abcdef'[(rng() * 16) | 0]).join('');
  // Short histories with inconsistent ancestry, late and repeated arrivals, both deletion
  // flags and several documents per file, in every order.
  function messy(rng, index) {
    const ids = ['n', 'm', 'p'].slice(0, 1 + ((rng() * 3) | 0));
    const files = [];
    const known = {};
    for (let count = 0, total = 1 + ((rng() * 5) | 0); count < total; count++) {
      const records = [];
      for (let n = 0, size = 1 + ((rng() * 6) | 0); n < size; n++) {
        const id = ids[(rng() * ids.length) | 0];
        (known[id] ??= []).push(...LEAVES.filter(() => rng() < 0.4));
        const pool = known[id].length ? known[id] : ['q'];
        const length = 1 + ((rng() * 4) | 0);
        const chainIds = Array.from({length}, () => pool[(rng() * pool.length) | 0]);
        const record = {_id: id, type: 'plain', path: id + '.md', children: [], ctime: 1, mtime: 1, size: 1};
        const flags = FLAGS[(rng() * FLAGS.length) | 0];
        if (flags) Object.assign(record, flags);
        const roll = rng();
        if (roll < 0.12) {
          record._rev = `${1 + ((rng() * 4) | 0)}-${pool[(rng() * pool.length) | 0]}`;
        } else {
          if (roll > 0.9 && length > 1) chainIds[(1 + rng() * (length - 1)) | 0] = ENTRIES[(rng() * ENTRIES.length) | 0];
          const shape = rng();
          const start = shape < 0.75 ? length : shape < 0.8 ? String(length) : shape < 0.85 ? true : shape < 0.9 ? null
            : shape < 0.95 ? length + 0.5 : 1e21;
          record._revisions = {start, ids: chainIds};
          record._rev = rng() < 0.85 ? `${length}-${chainIds[0]}` : `${1 + ((rng() * 4) | 0)}-${pool[(rng() * pool.length) | 0]}`;
        }
        records.push(record);
      }
      files.push(records);
    }
    return files;
  }
  // Histories around the device's revision limit: leaves, late ancestors that were stemmed
  // away, children of stemmed revisions and tombstones, arriving across several files.
  function deep(rng, index) {
    const length = [99, 100, 101, 102, 120, 150, 205][index % 7];
    const id = 'd' + (index % 7);
    const nodes = [];
    for (let step = 0; step < 9; step++) {
      const leaf = 1 + ((rng() * length) | 0);
      const tombstone = rng() < 0.25;
      nodes.push({_id: id, _rev: `${leaf}-h${leaf - 1}`, type: 'plain', path: id + '.md', children: [],
        ...(tombstone ? {_deleted: true} : {}), _revisions: {start: leaf, ids: chain(leaf, leaf)}});
    }
    const cut = 1 + ((rng() * length) | 0);
    nodes.push({_id: id, _rev: `${cut + 2}-c`, type: 'plain', path: id + '.md', children: [],
      _revisions: {start: cut + 2, ids: ['c', 'k', ...chain(cut, cut)]}});
    nodes.push({_id: id, _rev: `${length}-h${length - 1}`, type: 'plain', path: id + '.md', children: [],
      _revisions: {start: length, ids: chain(length, length)}});
    for (let i = nodes.length - 1; i > 0; i--) {
      const j = (rng() * (i + 1)) | 0;
      [nodes[i], nodes[j]] = [nodes[j], nodes[i]];
    }
    const files = [];
    for (let at = 0; at < nodes.length;) {
      const size = Math.min(nodes.length - at, 1 + ((rng() * 3) | 0));
      files.push(nodes.slice(at, at + size));
      at += size;
    }
    return files;
  }
  // Full-JSON chunk documents mixed with metadata.
  function chunks(rng, index) {
    const chunk = (name, extra = {}) => ({_id: 'h:' + name, _rev: '1-' + name, type: 'leaf', data: 'data-' + name, ...extra});
    const files = [];
    for (let count = 0, total = 1 + ((rng() * 3) | 0); count < total; count++) {
      const records = [];
      for (let n = 0, size = 1 + ((rng() * 4) | 0); n < size; n++) {
        const name = 'k' + ((rng() * 3) | 0);
        const roll = rng();
        records.push(roll < 0.15 ? {...a, _id: 'n'} : chunk(name, roll < 0.3 ? {_deleted: rng() < 0.5} : roll < 0.4 ? {_custom: 1}
          : roll < 0.5 ? {n: 1.0, z: [1, 'x', null]} : {}));
      }
      files.push(records);
    }
    return files;
  }
  // Starts and hashes from the coercion pools, two or three records to a file, a chunk sometimes.
  function coerce(rng, index) {
    // No object without a callable toString: pouchdb-merge names every revision of a stored tree
    // in revsDiff, so the next file would throw inside the unmodified level adapter's callback.
    const HASHES = ['a', 'b', 'x-y', '-', 7, 0, 1.5, true, null, [], {}, [1], '\ue000', '', '1-x'];
    const files = [];
    for (let count = 0, total = 1 + ((rng() * 2) | 0); count < total; count++) {
      const records = rng() < 0.2 ? [chunkK] : [];
      for (let n = 0, size = 1 + ((rng() * 3) | 0); n < size; n++) {
        const start = BOUNDED[(rng() * BOUNDED.length) | 0][1];
        const ids = Array.from({length: 1 + ((rng() * 3) | 0)}, () => HASHES[(rng() * HASHES.length) | 0]);
        const record = doc('1-q', ids, {}, start);
        if (rng() < 0.15) record._deleted = true;
        if (rng() < 0.1) record.deleted = true;
        if (rng() < 0.2) delete record._revisions;
        records.push(record);
      }
      files.push(records);
    }
    return files;
  }
  const RANDOM = [];
  for (let index = 0; index < 110; index++) RANDOM.push({name: 'messy-' + index, files: messy(mulberry32(seed + index * 7919), index)});
  for (let index = 0; index < 28; index++) RANDOM.push({name: 'deep-' + index, files: deep(mulberry32(seed + 1e6 + index * 104729), index)});
  for (let index = 0; index < 30; index++) RANDOM.push({name: 'mixed-chunks-' + index, files: chunks(mulberry32(seed + 2e6 + index * 15485863), index)});
  for (let index = 0; index < 40; index++) RANDOM.push({name: 'coerce-' + index, files: coerce(mulberry32(seed + 3e6 + index * 32452843), index)});

  // The starting states a file can appear over; the native write above is recorded for each.
  const WRITE_STATES = {
    absent: [],
    live: [[doc('2-b', ['b', 'a'], {_id: 'n.md'})]],
    'logical-deletion': [[doc('2-c', ['c', 'a'], {_id: 'n.md', deleted: true})]],
    'revision-tombstone': [[doc('2-d', ['d', 'a'], {_id: 'n.md', _deleted: true})]],
    untyped: [[{_id: 'n.md', _rev: '2-e', path: 'n.md', _revisions: {start: 2, ids: ['e', 'a']}}]],
    conflicted: [[doc('2-b', ['b', 'a'], {_id: 'n.md'}), doc('2-c', ['c', 'a'], {_id: 'n.md'})]],
    'tombstone-above-deletion': [[doc('2-c', ['c', 'a'], {_id: 'n.md', deleted: true}),
      doc('3-d', ['d', 'c', 'a'], {_id: 'n.md', _deleted: true})]],
    'colon-revision': [[{_id: 'n.md', _rev: '1-a:b', type: 'plain', path: 'n.md', children: [],
      _revisions: {start: 1, ids: ['a:b']}}]],
    'slot-collision': [[{...A, _id: 'n.md', path: 'n.md'}, {...B, _id: 'n.md::1-x', path: 'n.md::1-x'}]],
    // Three records of two documents in one batch: the second record of the first document is
    // stored after the other document's, and the shared slot ends with its tombstone.
    'slot-collision-groups': [[doc('1-a', ['a'], {_id: 'n.md', path: 'n.md'}),
      {...A, _id: 'n.md', path: 'n.md', _rev: '1-q', _deleted: true},
      {...B, _id: 'n.md::1-x', path: 'n.md::1-x'}]],
    // The body in the shared slot spells a longer chain: the put names its revision's hash for
    // the only ancestor, and the tree of the document written does not know the rest.
    'slot-collision-deep': [[{...A, _id: 'n.md', path: 'n.md', _revisions: {start: 1, ids: ['x::2-y']}},
      {_id: 'n.md::1-x', _rev: '2-y', type: 'plain', path: 'n.md::1-x', children: [], _revisions: {start: 2, ids: ['y', 'z']}}]],
    'slot-collision-named-tombstone': [[{_id: 'n.md', _rev: '1-q', type: 'plain', path: 'n.md', children: [],
      _revisions: {start: 2, ids: ['x::1-y', 'q']}},
      {_id: 'n.md', _rev: '1-y', type: 'plain', path: 'n.md', children: [], _revisions: {start: 1, ids: ['y']}},
      {_id: 'n.md::2-x', _rev: '1-y', type: 'plain', path: 'n.md::2-x', children: [], _deleted: true,
        _revisions: {start: 1, ids: ['y']}}]],
    'slot-collision-tombstone': [[{...A, _id: 'n.md', path: 'n.md', _revisions: {start: 1, ids: ['x::2-y']}},
      {_id: 'n.md::1-x', _rev: '2-y', type: 'plain', path: 'n.md::1-x', children: [], _deleted: true,
        _revisions: {start: 2, ids: ['y', 'z']}}]],
    'numeric-hash': [[{_id: 'n.md', _rev: '2-7', type: 'plain', path: 'n.md', children: [],
      _revisions: {start: 2, ids: [7, 'a']}}]],
    // The body is stored under the start as it was spelled and the tree names the winner by the
    // number it converts to, so the winner's slot is empty: for a live winner here.
    'missing-body': [[{_id: 'n.md', _rev: '2-x', type: 'plain', path: 'n.md', children: [],
      _revisions: {start: ' 2 ', ids: ['x']}}]],
    // The same with a position that turns negative: the tree does not name the winner either.
    'padded-tombstone': [[{_id: 'n.md', _rev: '1-d', type: 'plain', path: 'n.md', children: [],
      _deleted: true, _revisions: {start: ' -2 ', ids: ['d']}}]],
    // A revision-deleted winner whose revision holds a colon: the raw get reports it missing
    // before the slot's decoding is read, and the write goes below the revision the tree names.
    'colon-tombstone': [[{_id: 'n.md', _rev: '1-a:b', type: 'plain', path: 'n.md', children: [],
      _deleted: true, _revisions: {start: 1, ids: ['a:b']}}]],
    // A revision-deleted winner whose slot is empty and whose revision the tree names.
    'missing-tombstone': [[{_id: 'n.md', _rev: '2-d', type: 'plain', path: 'n.md', children: [],
      _deleted: true, _revisions: {start: ' 2 ', ids: ['d']}}]],
    // A tree whose root is not a first revision: a device stemmed the chain above it.
    'high-root': [[{_id: 'n.md', _rev: '2-x', type: 'plain', path: 'n.md', children: [],
      _revisions: {start: 2, ids: ['x']}}]],
    'high-root-tombstone': [[{_id: 'n.md', _rev: '2-d', type: 'plain', path: 'n.md', children: [],
      _deleted: true, _revisions: {start: 2, ids: ['d']}}]],
  };
  // The platform atob, as the browser build of PouchDB uses it for attachment data.
  const BASE64 = ['', 'aGVsbG8=', 'aGVsbG8', 'aGVs bG8=', 'aGVs\nbG8=', 'aGVsbG8===', 'a=bc', 'a', 'ab', 'abc', 'abcd', '=', '==',
    'a===', '====', '!', 'aGVsbG8h', 'aGVsbG8+', 'aGVsbG8/', 'AB==', 'A===', '/w==', '/w', '//8B', '_w==', '-_', 'aGVsbG8\r\n',
    'aG\u00e9sbG8=', 'YQ', 'YQ=', 'YQ==', 'YQ===', '\u00e9', '\ud800'];
  const base64 = BASE64.map(text => {
    let decoded;
    try {
      decoded = atob(text);
    } catch (error) {
      return {text, ok: false};
    }
    return {text, ok: true, length: decoded.length, digest: 'md5-' + createHash('md5').update(decoded, 'binary').digest('base64')};
  });

  // ToNumber and parseInt of strings, as Node's engine converts them: every code point that
  // ToNumber trims, each alone and around a number, the numeric spellings, long digit strings, and
  // random strings over the characters that can matter (Node builtins only). A double JSON cannot
  // carry is spelled as JavaScript spells it; so is a negative zero, which JSON prints as 0.
  const WHITESPACE = [];
  for (let code = 0; code <= 0x10FFFF; code++) {
    if (code >= 0xD800 && code <= 0xDFFF) continue;
    const char = String.fromCodePoint(code);
    if (Number(char + '1' + char) === 1) WHITESPACE.push(code);
  }
  const NUMBER_WORDS = [
    '', '+', '-', '.', 'e', 'E', 'Infinity', '-Infinity', '+Infinity', 'infinity', 'INFINITY', 'NaN', 'Inf',
    'Infinitye2', 'Infinity2', '0', '1', '2', '007', '-0', '+0', '0.', '.0', '.5', '5.', '1.5', '1.', '1e5',
    '1E5', '1e+5', '1e-5', '1.e5', '.5e5', '5.e-5', '1e', '1e+', '1e-', '1e5.5', '1.2.3', '1,5', '1_000', '0x',
    '0X', '0b', '0o', '0x1f', '0X1F', '0xF', '0xabcdef', '0o17', '0O7', '0b101', '0B1', '0x1p3', '0x+1',
    '-0x1', '+0x1', '00x1', '0b2', '0o8', '0xg', '\u0661\u0662', '\uff11', '\u0661', '12ab', 'ab12', '1 2',
    '  ', '\t', '\n', '9007199254740992', '9007199254740993', '9007199254740995', '18446744073709551616',
    '18446744073709551617', '1.7976931348623157e308', '2.4703282292062327e+324', '1e308', '1e309', '5e-324',
    '2.5e-324', '2.4703282292062327e-324', '1e-324', '0e-324', '1e-400', '1' + '0'.repeat(308),
    '1' + '0'.repeat(309), '9'.repeat(308), '9'.repeat(309), '9'.repeat(310), '9'.repeat(4300),
    '9'.repeat(4301), '9'.repeat(5000), '-' + '9'.repeat(5000), '0'.repeat(5000) + '1',
    '-' + '0'.repeat(5000) + '1', '0.' + '0'.repeat(400) + '1', '0.' + '0'.repeat(330) + '1',
    '1.' + '0'.repeat(400) + '1', '0x' + 'f'.repeat(300), '0x' + 'f'.repeat(13), '0x20000000000001',
    '0b' + '1'.repeat(1024), '0b' + '1'.repeat(1100), '0o' + '7'.repeat(400), '1e' + '9'.repeat(5000),
    '0x' + '0'.repeat(5000) + '1',
  ];
  const NUMBER_STRINGS = [];
  for (const code of WHITESPACE) {
    const char = String.fromCharCode(code);
    NUMBER_STRINGS.push(char, char + '2', '2' + char, char + '2' + char, '1' + char + '2', '-' + char + '2', '2' + char + 'e2');
  }
  NUMBER_STRINGS.push(...NUMBER_WORDS);
  for (const sign of ['', '+', '-']) {
    for (const word of ['2', '0', 'Infinity', '0x10', '1e5', '.5', '5.', '1.5e-3', '1e400', '1e-400',
      '9'.repeat(5000), '0'.repeat(5000) + '1']) NUMBER_STRINGS.push(sign + word);
  }
  const ALPHABET = [...'019+-.eExxaFfoObB _\u0661\uff11IinfityN\t\n\u2028\u00a0\u2009\ufeff\u3000'];
  const random = mulberry32(seed + 7e6);
  for (let index = 0; index < 1500; index++) {
    NUMBER_STRINGS.push(Array.from({length: (random() * 12) | 0}, () => ALPHABET[(random() * ALPHABET.length) | 0]).join(''));
  }
  for (let index = 0; index < 300; index++) {
    NUMBER_STRINGS.push(Array.from({length: (random() * 3) | 0}, () => String.fromCodePoint((random() * 0x110000) | 0)).join(''));
  }
  const spelling = number => number === Infinity ? 'Infinity' : number === -Infinity ? '-Infinity'
    : Number.isNaN(number) ? 'NaN' : Object.is(number, -0) ? '-0' : number;
  const numbers = NUMBER_STRINGS.map(text => ({text, number: spelling(Number(text)), parseInt: spelling(parseInt(text, 10))}));

  // An exception the published code throws from a callback is uncaught, as it would be on a
  // device; name the case it happened in.
  let current = null;
  process.on('uncaughtExceptionMonitor', (error, origin) => {
    console.error(`The published database threw an uncaught exception in case ${JSON.stringify(current)} (${origin}): ${error && error.stack}`);
  });

  async function build() {
    const cases = [];
    for (const [name, files, bodies] of [...NAMED, ...RANDOM.map(entry => [entry.name, entry.files])]) {
      current = name;
      try {
        cases.push({name, ...await run(files, bodies === true)});
      } catch (error) {
        throw Error(`The published database threw in case ${JSON.stringify(name)}: ${error && error.stack}`);
      }
    }
    const writes = [];
    for (const [name, files] of Object.entries(WRITE_STATES)) {
      current = 'write:' + name;
      const both = {};
      for (const [adapter, open] of [['idb', openIdb], ['level', openLevel]]) {
        both[adapter] = {write: await nativeWrite(open, files), delete: await nativeDelete(open, files)};
      }
      writes.push({name, files, idb: both.idb, level: both.level});
    }
    return {generator: 'scripts/extract_winner_vectors.mjs', commonlib: pin, seed,
      plugin_source: [
        {path: 'src/replication/journal/journalReceive.ts', sha256: digest(receivePath)},
        {path: 'src/replication/journal/journalRecords.ts', sha256: digest(recordsPath)},
      ],
      adapters: Object.fromEntries(Object.keys(adapters).map(name => [name, {version: needed['node_modules/' + name].version,
        lib_sha256: digest(lib(name))}])),
      dependencies, indexeddb, base64, whitespace: WHITESPACE, numbers, cases, writes};
  }
  const first = await build();
  if (JSON.stringify(await build()) !== JSON.stringify(first)) throw Error('The oracle is not deterministic');
  fs.writeFileSync(out, zlib.gzipSync(Buffer.from(JSON.stringify(first) + '\n')));
  const tally = {cases: first.cases.length, files: 0, records: 0, refused_files: 0, views: 0, observed: 0,
    deleted_winners: 0, with_conflicts: 0, revision_tombstones: 0, level_differs: 0};
  for (const entry of first.cases) {
    tally.level_differs += entry.level ? 1 : 0;
    tally.files += entry.files.length;
    tally.records += entry.files.reduce((sum, file) => sum + file.records.length, 0);
    tally.refused_files += entry.files.filter(file => file.outcome).length;
    tally.views += entry.views.length;
    for (const view of entry.views) for (const observed of view) {
      if (observed.tree === null) continue;
      tally.observed += 1;
      tally.deleted_winners += observed.deleted ? 1 : 0;
      tally.with_conflicts += observed.conflicts.length ? 1 : 0;
    }
    tally.revision_tombstones += JSON.stringify(entry.files).split('"_deleted":true').length - 1;
  }
  const agree = (entry, key) => JSON.stringify(entry.idb[key]) === JSON.stringify(entry.level[key]);
  console.log(JSON.stringify({...tally, base64_vectors: first.base64.length, whitespace: first.whitespace.length,
    number_strings: first.numbers.length, writes: first.writes.length,
    writes_level_agrees: first.writes.every(entry => agree(entry, 'write')),
    deletes_level_agrees: first.writes.every(entry => agree(entry, 'delete')), packages: dependencies.length}));
} finally {
  fs.rmSync(staging, {recursive: true, force: true});
}
