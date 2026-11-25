// Node 26.13.1+: COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// Native chunks.ts:367-370 repeatedly concatenates the original array minimum.
// The shared harness verifies source pins, published dependency integrities and
// imported archive members before executing the actual splitters and hashes.
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
const args = process.argv.slice(2), output = args.pop(), wasmArchive = args.pop();
const sha = name => createHash('sha256').update(fs.readFileSync(name)).digest('hex');
const rows = [];
let provenance;
// Neighbours of 100*11, 100*111 and 100*1111 UTF-16 units expose the
// second, third and fourth += evaluations. Include the review's 401-line case.
for (const repeats of [219,220,221,400,401,402,2219,2220,2221,22219,22220,22221]) {
    const settings = {chunkSplitterVersion:'v2', hashAlg:'sha1', customChunkSize:0, minimumChunkSize:[1]};
    const payload = {path:'concatenate.md', text:true, base64:Buffer.from('abcd\n'.repeat(repeats)).toString('base64')};
    const child = spawnSync(process.execPath, ['scripts/extract_settings_oracle.mjs', ...args, '/dev/null', wasmArchive], {
        encoding:'utf8', timeout:10000, maxBuffer:8*1024*1024,
        env:{...process.env, G3_CELL:JSON.stringify({settings,payload})},
    });
    if (child.error || child.status !== 0) throw Error(child.error?.name || child.stderr);
    const result = JSON.parse(child.stdout);
    if (result.status !== 'defined') throw Error('Upstream cell did not terminate');
    provenance ??= Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k => [k,result[k]]));
    for (const key of ['pieces','split_pieces']) result[key] = result[key].map(index => result.strings[index]);
    for (const key of ['strings', ...Object.keys(provenance)]) delete result[key];
    rows.push({repeats, settings, ...result});
}
fs.writeFileSync(output, gzipSync(JSON.stringify({
    generator_sha256:sha('scripts/extract_minimum_concatenation_vectors.mjs'),
    harness_sha256:sha('scripts/extract_settings_oracle.mjs'),
    dependency_loader_sha256:sha('scripts/splitter_oracle_dependencies.mjs'),
    provenance, rows,
}, null, 2) + '\n'));
