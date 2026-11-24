// Offline regeneration only. Node >=26.13.1.
// Usage: node scripts/extract_settings_oracle.mjs COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL OUTPUT.json.gz
// Inputs come from tests/settings_oracle_inputs.py, with no production imports.
// Executes pinned migration, dispatcher, parameter calculation, splitters and hash
// managers verbatim. Real octagonal-wheels decoder and IO, verified by archive.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {gzipSync} from 'node:zlib';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root, wasmFile, wheels, archive, out] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const sha = x => createHash('sha256').update(x).digest('hex');
const sources = {};
function read(name) {
    const local = fs.readFileSync(path.join(root, 'src', name), 'utf8');
    if (local !== execFileSync('git', ['-C', root, 'show', `${pin}:src/${name}`], {encoding:'utf8'})) throw Error(`Modified ${name}`);
    sources[name] = sha(local);
    return local;
}
if (execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], {encoding:'utf8'}).trim() !== pin) throw Error('Wrong upstream pin');
const {dependency, ...primitives} = await splitterDependencies(root, wheels, archive);
// Verify all relative hash-module dependencies, too, before importing.
const seen = new Set();
function verify(member) {
    if (seen.has(member)) return;
    seen.add(member);
    const local = fs.readFileSync(path.join(wheels, member));
    if (!local.equals(execFileSync('tar', ['-xOf', archive, member]))) throw Error(`Modified ${member}`);
    sources[member] = sha(local);
    for (const m of local.toString().matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g)) {
        if (!m[1].startsWith('.')) throw Error(`Unverified import ${m[1]}`);
        verify(path.posix.normalize(path.posix.join(path.posix.dirname(member), m[1])));
    }
}
verify('package/dist/hash/purejs.js');
const binary = await import(pathToFileURL(path.resolve(wheels, 'package/dist/binary/base64.js')));
const hashes = await import(pathToFileURL(path.resolve(wheels, 'package/dist/hash/purejs.js')));
if (sha(fs.readFileSync(wasmFile)) !== 'e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a') throw Error('Wrong WASM');
const wasm = await (await import(pathToFileURL(path.resolve(wasmFile)))).default();
const ctx = vm.createContext({Blob, Uint8Array, Buffer, AbortController, TextDecoder, TextEncoder,
    ...primitives, ...hashes, HashManagerCore: class {},
    compatGlobal: {navigator:{language:'en'}, Buffer}, wrapByDefault: (fn, fallback) => {try {return fn();} catch(e) {return fallback(e);}},
    Logger: () => {}, LOG_LEVEL_VERBOSE: 0,
    createTextBlob: data => new Blob([data], {type:'text/plain'}),
    arrayBufferToBase64SingleInternal: binary.arrayBufferToBase64Single});
function run(source) {
    vm.runInContext(stripTypeScriptTypes(source.replace(/^import .*;\n/gm, '').replace(/export /g, '')), ctx);
}
const constants = read('common/models/setting.const.ts');
run(constants.slice(constants.indexOf('export const ChunkAlgorithmNames'), constants.indexOf('export const ChunkAlgorithms')));
// ChunkAlgorithms is a closed literal; execute its declaration from upstream.
run(constants.slice(constants.indexOf('export const ChunkAlgorithms'), constants.indexOf('export const ChunkAlgorithms') + constants.slice(constants.indexOf('export const ChunkAlgorithms')).indexOf(';') + 1));
const behaviour = read('common/models/shared.const.behabiour.ts');
run(behaviour.match(/export const MAX_DOC_SIZE_BIN = [^;]+;/)[0]);
run('const MAX_CHUNKS_SIZE_ON_UI = 0;'); // worker disabled below, never consulted
const defaults = read('common/models/setting.const.defaults.ts');
const defaultMinimum = Number(defaults.match(/minimumChunkSize: (\d+)/)[1]);
const defaultCustom = Number(defaults.match(/customChunkSize: (\d+)/)[1]);
const paths = read('string_and_binary/path.ts');
run(paths.slice(paths.indexOf('export function shouldSplitAsPlainText('), paths.indexOf('\n}', paths.indexOf('export function shouldSplitAsPlainText(')) + 2));
const convert = read('string_and_binary/convert.ts');
const convertBegin = convert.indexOf('export async function arrayBufferToBase64Single(');
run(convert.slice(convertBegin, convert.indexOf('\nexport {', convertBegin)));
run(read('string_and_binary/chunks.ts'));
for (const name of ['ContentSplitterBase', 'ContentSplitterV1', 'ContentSplitterV2', 'ContentSplitterRabinKarp', 'ContentSplitters']) run(read(`ContentSplitter/${name}.ts`));
for (const name of ['XXHashHashManager', 'PureJSHashManager']) run(read(`managers/HashManager/${name}.ts`));
const managers = {};
for (const [alg, name] of Object.entries({'':'XXHash32RawHashManager', xxhash32:'FallbackWasmHashManager', xxhash64:'XXHash64HashManager', 'mixed-purejs':'PureJSHashManager', sha1:'SHA1HashManager'})) {
    const manager = vm.runInContext(`new ${name}({})`, ctx);
    manager.xxhash = wasm;
    managers[alg] = manager;
}
const migration = read('services/base/SettingService.ts');
const migrateCode = migration.slice(migration.indexOf('if (settings.chunkSplitterVersion == "")'), migration.indexOf('return Promise.resolve(settings);', migration.indexOf('if (settings.chunkSplitterVersion == "")')));
const input = JSON.parse(execFileSync(process.env.PYTHON || './venv/bin/python', ['-m', 'tests.settings_oracle_inputs'], {maxBuffer:32*1024*1024}));
// Intern entire pieces, preserving their exact strings (including surrogate cuts),
// ordered references and repeated children. No payload bytes are stored separately.
const pieces = [], pieceIndex = new Map(), vectors = [];
const cache = new Map();
async function vector(settings, payload) {
    const cacheKey = JSON.stringify([settings, payload]);
    if (cache.has(cacheKey)) return cache.get(cacheKey);
    ctx.settings = {customChunkSize:defaultCustom, minimumChunkSize:defaultMinimum, ...settings, disableWorkerForGeneratingChunks:true};
    run(migrateCode);
    const effective = ctx.settings;
    ctx.options = {settingService:{currentSettings: () => effective}};
    const splitter = vm.runInContext('new ContentSplitter(options)', ctx);
    const raw = Buffer.from(payload.base64, 'base64');
    const blob = new Blob([raw], {type:payload.text?'text/plain':'application/octet-stream'});
    const result = [], children = [], splitPieces = [], splitChildren = [];
    for await (const p of await splitter.splitContent({path:payload.path, data:blob})) {
        // Empty worker sentinel is not a persisted leaf (EntryManager contract).
        if (!pieceIndex.has(p)) {pieceIndex.set(p, pieces.length); pieces.push(p);}
        const id = 'h:' + await managers[settings.hashAlg].computeHashWithoutEncryption(p);
        splitPieces.push(pieceIndex.get(p));
        splitChildren.push(id);
        if (p !== '') {result.push(pieceIndex.get(p)); children.push(id);}
    }
    const v = {pieces:result, children, split_pieces:splitPieces, split_children:splitChildren};
    cache.set(cacheKey, v);
    return v;
}
for (const row of input.matrix) {
    const payloads = {};
    for (const [id, payload] of Object.entries({...row.payloads, ...input.auxiliary_payloads})) payloads[id] = await vector(row.settings, payload);
    vectors.push({id:row.id, settings:row.settings, payloads});
}
const grid = [];
for (const row of input.grid) grid.push({...row, ...await vector(row.settings, input.payloads[row.payload])});
const fixture = {upstream_commit:pin, node:process.version, decoder_dependency:dependency, source_sha256:sources,
    wasm_sha256:sha(fs.readFileSync(wasmFile)), inputs_sha256:sha(fs.readFileSync('tests/settings_oracle_inputs.py')),
    pieces, matrix:vectors, grid};
fs.writeFileSync(out, gzipSync(JSON.stringify(fixture)+'\n', {level:9}));
console.log(JSON.stringify({matrix:vectors.length, grid:grid.length, pieces:pieces.length, bytes:fs.statSync(out).size}));
