// Extract executable recipes and EXPECTED arrays from pinned upstream specs.
// Usage: node (22.13+) scripts/extract_splitter_vectors.mjs UPSTREAM_CHECKOUT OUTPUT_JSON XXHASH_WASM_ESM WHEELS_DIR WHEELS_TARBALL
// Dependencies are supplied as verified published artifacts. The algorithm is loaded verbatim; assertions supply the oracle.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { deflateSync, inflateSync } from 'node:zlib';
import { execFileSync } from 'node:child_process';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const root = process.argv[2];
const {dependency, ...primitives} = await splitterDependencies(root, process.argv[5], process.argv[6]);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
if (execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], {encoding:'utf8'}).trim() !== pin) throw Error('Wrong upstream revision');
const sourcePath = 'string_and_binary/chunks.ts';
const specPath = 'string_and_binary/chunks.rabin-karp.unit.spec.ts';
const read = p => {
    const local = fs.readFileSync(path.join(root, 'src', p), 'utf8');
    const pinned = execFileSync('git', ['-C',root,'show',`${pin}:src/${p}`], {encoding:'utf8'});
    if (local !== pinned) throw Error(`Modified upstream source: ${p}`);
    return local;
};
const tasks = [], vectors = [];
let pending, title;
const ctx = {
    Blob, Uint8Array, Buffer, AbortController,
    ...primitives,
    arrayBufferToBase64Single: async b => Buffer.from(b).toString('base64'),
    describe: (_name, fn) => fn(),
    expect: actual => ({
        toBe(expected) { if (actual !== expected) throw Error(`Assertion failed: ${title}`); },
        toEqual(expected) {
            if (JSON.stringify(actual) !== JSON.stringify(expected)) throw Error(`Assertion failed: ${title}`);
            if (Array.isArray(expected) && expected.every(Number.isInteger)) {
                vectors.push({...pending, expected_lengths:expected});
            }
        },
    }),
};
function it(name, fn) {
    // These exercise lazy IO/cancellation, absent from the synchronous Python API.
    if (/reads|aborted/.test(name)) return;
    tasks.push([name, fn]);
}
it.each = values => (name, fn) => values.forEach(v => it(name, () => fn(v)));
ctx.it = it;
vm.createContext(ctx);
vm.runInContext(stripTypeScriptTypes(read(sourcePath).slice(0,read(sourcePath).indexOf('import '))),ctx);
const algorithm = read(sourcePath).slice(read(sourcePath).indexOf('export async function splitPiecesRabinKarp('));
vm.runInContext(stripTypeScriptTypes(algorithm.split('export async function splitPiecesRabinKarpOld(')[0].replace('export ','')), ctx);
const original = ctx.splitPiecesRabinKarp;
ctx.splitPiecesRabinKarp = async (blob, maximum, plain, minimum) => {
    pending = {name:title, source:specPath, maximum, minimum, text:blob.type==='text/plain', plain,
        input_deflate_b64:deflateSync(Buffer.from(await blob.arrayBuffer())).toString('base64')};
    return original(blob,maximum,plain,minimum);
};
const spec = read(specPath).replace(/^import .*;\n/gm,'');
vm.runInContext(stripTypeScriptTypes(spec), ctx);
for (const [name,fn] of tasks) { title=name; await fn(); }
// Extract the numeric read-path table too, without reproducing its IO spies.
// Locate this table from its first literal; earlier it.each blocks are unrelated.
const tableStart = spec.indexOf('        { size: 0, wholeReads:');
const tableEnd = spec.indexOf('])("reads a $size-byte', tableStart);
const cases = vm.runInNewContext('[' + spec.slice(tableStart, tableEnd) + ']', {readBlockBytes:1024*1024});
for (const item of cases) {
    vectors.push({name:`read-path size ${item.size}`,source:specPath,maximum:1024*1024,minimum:20,text:false,plain:false,
        input_deflate_b64:deflateSync(Buffer.alloc(item.size)).toString('base64'),expected_lengths:item.chunkSizes});
}
const differential = [];
for (let i=0;i<10;i++) {
    const text = i % 2 === 0;
    const input = text ? Buffer.from(('漢🙂alpha\n').repeat(1000+i*200))
        : Buffer.from(Array.from({length:140000+i*1000},(_,j)=>(j*31+i)%251));
    const maximum = 102400*(i%3+1), minimum = i%3===0 ? 900 : 20;
    const blob = new Blob([input], {type:text?'text/plain':'application/octet-stream'});
    const factory = await original(blob,maximum,false,minimum);
    const pieces = [];
    for await (const piece of factory()) pieces.push(piece);
    differential.push({name:`differential ${i}`,source:sourcePath,maximum,minimum,text,plain:false,
        input_deflate_b64:deflateSync(input).toString('base64'),
        expected_lengths:pieces.map(p=>text?Buffer.byteLength(p):Buffer.from(p,'base64').length)});
}
const hashPath = 'managers/HashManager/HashManager.unit.spec.ts';
const hashSpec = read(hashPath);
const compatibility = hashSpec.match(/const CompatibilityPlain = \{([\s\S]*?)\} as const;/)[1];
const hashes = [...compatibility.matchAll(/\[HashAlgorithms\.(\w+)\]: "([^"]+)"/g)].map(m=>({algorithm:m[1],piece:hashSpec.match(/it\("should compute correct hashes"[\s\S]*?const piece = "([^"]+)"/)[1],expected:m[2]}));
const repeatInputs = JSON.parse(hashSpec.match(/it\("should produce consistent plain hashes"[\s\S]*?const inputs = (\[[^;]+\])/)[1]);
const directPath = 'ContentSplitter/ContentSplitterDirectWorker.unit.spec.ts';
const directPiece = JSON.parse('"'+read(directPath).match(/new Blob\(\["([^"\n]+)"\]/)[1]+'"');
const encryptedCompatibility = hashSpec.match(/const CompatibilityEncrypted = \{([\s\S]*?)\} as const;/)[1];
const encryptedHashes = [...encryptedCompatibility.matchAll(/\[HashAlgorithms\.(\w+)\]: "([^"]+)"/g)]
    .map(m=>({algorithm:m[1],piece:hashes[0].piece,expected:m[2]}));
const hashLiteralsOutsideScope = [...new Set([...hashSpec.matchAll(/(?:\[HashAlgorithms\.\w+\]:|toBe\()\s*"([+a-zA-Z0-9=/-]{20,})"/g)].map(m=>m[1]))]
    .filter(value=>!hashes.some(h=>h.expected===value));
const sourceHashes = Object.fromEntries([sourcePath,specPath,hashPath,directPath].map(p=>[p,createHash('sha256').update(read(p)).digest('hex')]));
const result = {decoder_dependency:dependency, source_sha256:sourceHashes, out_of_scope_encrypted_hashes:encryptedHashes, out_of_scope_hash_literals:hashLiteralsOutsideScope, upstream_commit:pin,source:sourcePath, vectors, differential, hashes, hash_source:hashPath, repeat_inputs:repeatInputs,
    direct_worker:{source:directPath,input:directPiece,contains:[...read(directPath).matchAll(/toContain\("([^"]+)"\)/g)].map(m=>m[1])}};
if (process.argv[4]) {
    // Optional independent hash oracle: xxhash-wasm 1.1.0 from upstream lock.
    const hashModule = process.argv[4];
    if (createHash('sha256').update(fs.readFileSync(hashModule)).digest('hex') !==
        'e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a') throw Error('Wrong hash oracle');
    const wasm = await (await import(pathToFileURL(path.resolve(hashModule)).href)).default();
    const hash = piece => 'h:' + wasm.h64(`${piece}-${piece.length}`).toString(36);
    if (hash('helloWorld') !== 'h:' + hashes.find(h=>h.algorithm==='XXHASH64').expected) throw Error('Hash oracle mismatch');
    for (const vector of [...vectors,...differential]) {
        const raw = inflateSync(Buffer.from(vector.input_deflate_b64,'base64'));
        let offset=0;
        vector.expected_ids = vector.expected_lengths.map(size=>{
            const piece = raw.subarray(offset,offset+size); offset+=size;
            return hash(vector.text ? primitives.readString(piece) : piece.toString('base64'));
        });
    }
    result.hash_oracle = {package:'xxhash-wasm',version:'1.1.0',
        source:'https://registry.npmjs.org/xxhash-wasm/-/xxhash-wasm-1.1.0.tgz',
        module_sha256:createHash('sha256').update(fs.readFileSync(hashModule)).digest('hex')};
    result.additional_hash_vectors = ['🙂','漢🙂','\ud800','\ud83d\ude42','', 'aGVsbG8='].map(piece=>({piece,expected:hash(piece)}));
}
fs.writeFileSync(process.argv[3],JSON.stringify(result,null,2)+'\n');
console.log(`${vectors.length} boundary vectors; ${hashes.length} hash algorithm literals (1 plaintext xxhash64); ${repeatInputs.length} consistency inputs; ${differential.length} differential cases; ${result.additional_hash_vectors?.length ?? 0} additional Node hash vectors`);
