// Node 26.13.1+: node scripts/extract_acceptance_vectors.mjs COMMONLIB WASM WHEELS WHEELS_TGZ WASM_TGZ OUTPUT
// Execute the integrity-verifying native harness, not the Python implementation.
// chunks.ts:342-433 (binary), ContentSplitterV2.ts:20-32 (dispatcher).
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const [root, wasm, wheels, archive, wasmArchive, output] = process.argv.slice(2);
const sha = data => createHash('sha256').update(data).digest('hex');
const settings = {chunkSplitterVersion:'v2', hashAlg:'xxhash64', customChunkSize:60,
    minimumChunkSize:100, syncMaxSizeInMB:200};
const rows = [];
for (const size of [40, 100]) {
    const bytes = size * 1048576;
    const payload = {recipe:`eq:png:${bytes}:0:-1`, path:`large-${size}.png`, text:false};
    const result = JSON.parse(execFileSync(process.execPath, ['--max-old-space-size=2048',
        'scripts/extract_settings_oracle.mjs', root, wasm, wheels, archive, '/unused', wasmArchive],
        {env:{...process.env, G3_CELL:JSON.stringify({settings,payload})}, maxBuffer:256*1048576}));
    if (result.status !== 'defined') throw Error('Native acceptance splitter did not finish');
    rows.push({size_bytes:bytes, path:payload.path, recipe:payload.recipe, settings,
        children:result.children,
        pieces:result.pieces.map(i => ({sha256:sha(Buffer.from(result.strings[i], 'base64')),
            data_sha256:sha(Buffer.from(result.strings[i])), bytes:Buffer.from(result.strings[i], 'base64').length})),
        source_sha256:result.source_sha256, decoder_dependency:result.decoder_dependency,
        wasm_dependency:result.wasm_dependency, upstream_commit:result.upstream_commit, node:result.node});
}
fs.writeFileSync(output, JSON.stringify({generator_sha256:sha(fs.readFileSync(import.meta.filename)),
    harness_sha256:sha(fs.readFileSync('scripts/extract_settings_oracle.mjs')), rows}, null, 2)+'\n');
console.log(JSON.stringify({rows:rows.length, sha256:sha(fs.readFileSync(output))}));
