// Node >=26.13.1. Run from the repository root:
// node scripts/extract_legacy_cells.mjs COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT.json
// Each cell executes the pinned dispatcher, migration, parameters, splitters,
// and real integrity-verified published dependencies in an isolated child.
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args = process.argv.slice(2), output = args.pop(), wasmArchive = args.pop();
const versions = ['v1','v2','v2-segmenter','v3-rabin-karp','legacy-00','legacy-01','legacy-10','legacy-11'];
const minima = [20, 0, -1, '', null, '__G3_NaN__'];
const rows = [];
let provenance;
for (const version of versions) for (const input of ['plain','text','binary'])
for (const custom of [0,-1,-2]) for (const minimum of minima) for (const empty of [false,true]) {
    const settings = {chunkSplitterVersion:version, customChunkSize:custom, minimumChunkSize:minimum, hashAlg:'xxhash64'};
    if (version.startsWith('legacy-')) Object.assign(settings, {chunkSplitterVersion:'',
        enableChunkSplitterV2:version[7] === '1', useSegmenter:version[8] === '1'});
    const data = empty ? '' : 'abc\ndef';
    const payload = {path:input === 'plain'?'cell.md':input === 'text'?'cell.svg':'cell.png',
        text:input !== 'binary', base64:Buffer.from(data).toString('base64')};
    const child = spawnSync(process.execPath, ['scripts/extract_settings_oracle.mjs', ...args, '/dev/null', wasmArchive], {
        env:{...process.env, G3_CELL:JSON.stringify({settings,payload})}, timeout:2000, encoding:'utf8', maxBuffer:1024*1024});
    let result;
    if (child.error?.code === 'ETIMEDOUT') result = {status:'time_limit'};
    else {
        if (child.status !== 0) throw Error(child.stderr);
        result = JSON.parse(child.stdout);
    }
    if (result.source_sha256) {
        provenance ??= Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k=>[k,result[k]]));
        for (const key of ['pieces','split_pieces']) result[key] = result[key].map(i=>result.strings[i]);
        for (const key of ['strings', ...Object.keys(provenance)]) delete result[key];
    }
    const effectiveV1 = version === 'v1' || version.startsWith('legacy-0');
    let proof;
    if (['step_limit','time_limit'].includes(result.status)) {
        if (!effectiveV1 && input === 'plain' && [0,'',null].includes(minimum))
            proof = 'chunks.ts:367-371: textLen / xMinimumChunkSize stays Infinity (null/0) or Infinity after empty-string concatenation; xMinimumChunkSize cannot become positive.';
        else if (input === 'binary')
            proof = effectiveV1 ? 'chunks.ts:473-491: slice is empty; i += piece.length stays zero for a nonpositive cap.' : 'chunks.ts:418-432: splitEnd <= i; i never reaches size for a nonpositive cap.';
        else if (effectiveV1)
            proof = input === 'plain' ? 'chunks.ts:234-241: negative ps increments once to <=0; substring(ps) retains buffer.' : 'chunks.ts:245-250: substring(nonpositive splitSize) retains leftData.';
        else proof = 'chunks.ts:297-313: end is grown to 1 on first iteration, then end equals from=1 (or returns there); from cannot advance beyond 1.';
    }
    rows.push({id:`g3:${version}:${input}:${custom}:${JSON.stringify(minimum)}:${empty?'empty':'nonempty'}`,
        version,input,empty,settings,payload,...result,...(proof?{proof}:{} )});
    if (rows.length % 36 === 0) console.error(`cells ${rows.length}/864`);
}
fs.writeFileSync(output, JSON.stringify({provenance, limits:{steps:128, milliseconds:2000},
    generator_sha256:createHash('sha256').update(fs.readFileSync('scripts/extract_legacy_cells.mjs')).digest('hex'), rows},null,2)+'\n');
