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
const input = JSON.parse(execFileSync(process.env.PYTHON || './venv/bin/python', ['-m', 'tests.settings_oracle_inputs'], {maxBuffer:96*1024*1024}));
// Intern entire pieces, preserving their exact strings (including surrogate cuts),
// ordered references and repeated children. No payload bytes are stored separately.
const pieces = [], pieceIndex = new Map(), vectors = [];
const cache = new Map(), idCache = new Map();
const candidatePatches = new Map();
function candidatePatch(seed) {
    if (candidatePatches.has(seed)) return candidatePatches.get(seed);
    let state = 0;
    for (let i = 0; i < 45; i++) state = (state * 31 + 97 + seed) >>> 0;
    for (let first = 0; first < 256; first++) for (let second = 0; second < 256; second++) {
        const base = (((state * 31 + first) * 31 + second) * 31) >>> 0;
        const last = ((1 - base) % 25600 + 25600) % 25600;
        if (last < 256 && ((base + last) >>> 0) % 25600 === 1) {
            const patch = Uint8Array.of(first, second, last);
            candidatePatches.set(seed, patch);
            return patch;
        }
    }
    throw Error('Cannot construct binary candidate input');
}
async function vector(settings, payload) {
    const cacheKey = JSON.stringify([settings, payload]);
    if (cache.has(cacheKey)) return cache.get(cacheKey);
    ctx.settings = {customChunkSize:defaultCustom, minimumChunkSize:defaultMinimum, ...settings, disableWorkerForGeneratingChunks:true};
    run(migrateCode);
    const effective = ctx.settings;
    ctx.options = {settingService:{currentSettings: () => effective}};
    const splitter = vm.runInContext('new ContentSplitter(options)', ctx);
    let raw;
    if (payload.recipe) {
        const [, mode, sizeText, seedText, positionText] = payload.recipe.split(':');
        const size = Number(sizeText), position = Number(positionText);
        const char = String.fromCharCode(97 + Number(seedText));
        if (mode === 'candidate-binary') {
            raw = Buffer.alloc(size, char);
            raw.set(candidatePatch(Number(seedText)), position - 3);
        } else if (mode.startsWith('lf-')) {
            raw = Buffer.alloc(size, char);
            raw[position] = 10;
        } else if (mode.startsWith('hint-')) {
            raw = Buffer.from(mode.endsWith('-binary') ? char.repeat(size) :
                'before\n```js\n' + char + ',' + char + ';' + char + '\n```\nafter\n');
        } else if (['json', 'png', 'pdf'].includes(mode)) {
            raw = Buffer.alloc(size, char);
            if (size >= 3000000) for (let i = 99999; i < size; i += 100000) raw[i] = {json:44, png:0, pdf:47}[mode];
            if (position >= 0 && position < size) raw[position] = {json:44, png:0, pdf:47}[mode];
        } else {
            let text;
            if (mode === 'fence') {
                const body = char.repeat(size - 4), middle = Math.floor(body.length / 2);
                text = '```\n' + body.slice(0, middle) + ',' + body.slice(middle + 1) + '\n```\ntail';
            } else if (mode === 'line') text = char.repeat(position - 1) + '\n' + char.repeat(size - position);
            else if (mode.startsWith('pair')) {
                const scalar = {pair:0x1f642, 'pair-low':0x10000, 'pair-high':0x10ffff}[mode];
                text = char.repeat(position) + String.fromCodePoint(scalar) + char.repeat(size - position);
            } else text = char.repeat(size);
            raw = Buffer.from(text);
        }
    } else raw = Buffer.from(payload.base64, 'base64');
    const blob = new Blob([raw], {type:payload.text?'text/plain':'application/octet-stream'});
    const result = [], children = [], splitPieces = [], splitChildren = [];
    for await (const p of await splitter.splitContent({path:payload.path, data:blob})) {
        // Empty worker sentinel is not a persisted leaf (EntryManager contract).
        if (!pieceIndex.has(p)) {pieceIndex.set(p, pieces.length); pieces.push(p);}
        const hashKey = JSON.stringify([settings.hashAlg, pieceIndex.get(p)]);
        if (!idCache.has(hashKey)) idCache.set(hashKey, 'h:' + await managers[settings.hashAlg].computeHashWithoutEncryption(p));
        const id = idCache.get(hashKey);
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
const boundaryGroups = new Map();
const candidateMeasurements = [];
for (const row of input.grid) {
    if (grid.length % 500 === 0) console.error(`grid ${grid.length}/${input.grid.length}`);
    const v = await vector(row.settings, input.payloads[row.payload]);
    grid.push({...row, ...v});
    if (row.payload.startsWith('eq:candidate-binary:')) {
        const requested = Number(row.payload.split(':')[4]);
        const measured = Buffer.from(pieces[v.split_pieces[0]], 'base64').length;
        if (row.settings.minimumChunkSize <= requested && measured !== requested)
            throw Error('Binary candidate probe misses requested upstream minimum');
        candidateMeasurements.push({id:row.id, requested, measured});
    }
    if (row.payload.startsWith('boundary:') && row.payload.includes(':png:none:')) {
        const key = JSON.stringify([row.settings.chunkSplitterVersion, row.settings.enableChunkSplitterV2,
            row.settings.useSegmenter, row.settings.customChunkSize]);
        if (!boundaryGroups.has(key)) boundaryGroups.set(key, []);
        boundaryGroups.get(key).push({size:Buffer.from(input.payloads[row.payload].base64, 'base64').length, v});
    }
}
// Measure the cap from actual pinned splitter output, rather than trusting the
// Python input recipe. The largest probe must split at the measured cap; its
// siblings must be exactly cap-1 and cap and remain single pieces.
const boundaryMeasurements = [];
for (const [settings, probes] of boundaryGroups) {
    probes.sort((a,b) => a.size - b.size);
    const above = probes[2];
    const measured = Buffer.from(pieces[above.v.split_pieces[0]], 'base64').length;
    if (probes.length !== 3 || above.v.split_pieces.length !== 2 ||
        probes.some((p,i) => p.size !== measured + i - 1) ||
        probes.slice(0,2).some(p => p.v.split_pieces.length !== 1)) throw Error(`Boundary probes miss upstream cap: ${settings}`);
    boundaryMeasurements.push({settings:JSON.parse(settings), maximum:measured});
}
const chunks = read('string_and_binary/chunks.ts');
const scalarCode = chunks.slice(chunks.indexOf('    const clampMin ='), chunks.indexOf('    return async function* piecesBlob()'));
run('function binaryMinimumScalar(size, canBeSmall) { const dataSrc = {size}; let minimumChunkSize; ' + scalarCode + 'return minimumChunkSize; }');
const scalarProbes = [];
for (const small of [true, false]) {
    const sizes = [99, 100, 101, 99999, 100000, 100001, 99999999, 100000000, 100000001];
    for (let step = 1; step <= 7; step++) {
        const value = 10 * 12.5 ** step;
        sizes.push(value - 0.000001, value, value + 0.000001);
    }
    for (const size of sizes) scalarProbes.push({size, path:small?'probe.json':'probe.png',
        minimum:vm.runInContext(`binaryMinimumScalar(${size}, ${small})`, ctx)});
}
const fixture = {candidate_measurements:candidateMeasurements, scalar_probes:scalarProbes, upstream_commit:pin, node:process.version, decoder_dependency:dependency, source_sha256:sources,
    wasm_sha256:sha(fs.readFileSync(wasmFile)), inputs_sha256:sha(fs.readFileSync('tests/settings_oracle_inputs.py')),
    boundary_measurements:boundaryMeasurements, pieces, matrix:vectors, grid};
fs.writeFileSync(out, gzipSync(JSON.stringify(fixture)+'\n', {level:9}));
console.log(JSON.stringify({matrix:vectors.length, grid:grid.length, pieces:pieces.length, bytes:fs.statSync(out).size}));
