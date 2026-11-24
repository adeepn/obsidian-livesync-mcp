// Node >=26.13.1. Run from the repository root:
// node scripts/extract_text_budget_vectors.mjs COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// Execute the pinned size predicate and the existing integrity-verified splitter
// harness; no production Python imports. C:services/base/VaultService.ts:73-81.
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args = process.argv.slice(2), output = args.pop(), wasmArchive = args.pop();
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const file = 'src/services/base/VaultService.ts';
const source = execFileSync('git', ['-C', args[0], 'show', `${pin}:${file}`], {encoding:'utf8'});
if (source !== fs.readFileSync(`${args[0]}/${file}`, 'utf8')) throw Error('Modified size source');
const method = source.slice(source.indexOf('    isFileSizeTooLarge(size: number) {'), source.indexOf('\n    /**', source.indexOf('    isFileSizeTooLarge(size: number) {')));
const predicate = new Function('size', method.replace('    isFileSizeTooLarge(size: number) {', '').replace(/\}\s*$/, ''));
const policy = [];
for (const mb of [0, -1, 50, 256, 300.25, 300.2500001, 1e308]) {
    const product = mb * 1024 * 1024;
    for (const size of Number.isFinite(product) ? [0, Math.floor(product), Math.floor(product) + 1] : [0, 1])
        policy.push({mb, size, too_large: predicate.call({settings:{syncMaxSizeInMB:mb}}, size)});
}
// A small environment recipe avoids OS argument limits for the 12 MiB input.
const settings = {chunkSplitterVersion:'v2', hashAlg:'xxhash64', customChunkSize:0, minimumChunkSize:20};
const payload = {path:'large.json', text:false, recipe:`g4a:text-budget:${12*1024*1024}:0:-1`};
const result = JSON.parse(execFileSync(process.execPath, ['scripts/extract_settings_oracle.mjs', ...args, '/dev/null', wasmArchive], {
    env:{...process.env, G3_CELL:JSON.stringify({settings,payload})}, maxBuffer:64*1024*1024, timeout:60000, encoding:'utf8'}));
if (result.status !== 'defined') throw Error('Undefined splitter result');
const fixture = {upstream_commit:pin, node:process.version, policy,
    source_sha256:{[file]:sha(source), ...result.source_sha256},
    decoder_dependency:result.decoder_dependency, wasm_dependency:result.wasm_dependency,
    large:{settings, payload, children:result.children,
        piece_sha256:result.pieces.map(i=>sha(result.strings[i]))}};
fs.writeFileSync(output, JSON.stringify(fixture, null, 2)+'\n');
