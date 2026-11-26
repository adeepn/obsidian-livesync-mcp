// Node >=26.13.1. Same arguments as extract_legacy_cells.mjs.
// Probe nonempty text/plain Blobs decoded to an empty string. Blob.text()
// strips the BOM, but V2's early empty-Blob return tests raw size (350-357).
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
const args=process.argv.slice(2), output=args.pop(), wasmArchive=args.pop();
const rows=[];
let provenance;
for (const version of ['v1','v2','v2-segmenter','v3-rabin-karp','legacy-00','legacy-01','legacy-10','legacy-11'])
for (const custom of [0,-1,-2]) for (const minimum of [20,-1]) for (const mode of ['bom','newline']) {
    const settings={chunkSplitterVersion:version,customChunkSize:custom,minimumChunkSize:minimum,hashAlg:'xxhash64'};
    if (version.startsWith('legacy-')) Object.assign(settings,{chunkSplitterVersion:'',enableChunkSplitterV2:version[7]==='1',useSegmenter:version[8]==='1'});
    const payload={path:'bom.md',text:true,base64:Buffer.from(mode === 'bom'?'\ufeff':'\n').toString('base64')};
    const child=spawnSync(process.execPath,['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasmArchive],{
        env:{...process.env,G3_CELL:JSON.stringify({settings,payload})},timeout:2000,encoding:'utf8'});
    let result;
    if (child.error?.code==='ETIMEDOUT') result={status:'time_limit',
        proof:'chunks.ts:288-294,299-309: BOM text or a final empty delimiter tail has strLen=0 > negative maxLength; charCodeAt(end-1) is always NaN and codePointAt is undefined; end increments forever.'};
    else {
        if (child.status!==0) throw Error(child.stderr);
        result=JSON.parse(child.stdout);
        if (result.status === 'defined') {
        provenance ??= Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k=>[k,result[k]]));
        for (const key of ['pieces','split_pieces','lossless_pieces','lossless_split_pieces']) result[key]=result[key].map(i=>result.strings[i]);
        for (const key of ['strings',...Object.keys(provenance)]) delete result[key];
        } else {
            if (result.status !== 'step_limit') throw Error('Unexpected upstream error');
            result.proof='chunks.ts:234-241: negative ps increments once to <=0; substring(ps) retains a nonempty buffer.';
        }
    }
    rows.push({id:`g3-${mode}:${version}:${custom}:${minimum}`,version,input:'plain',empty:false,settings,payload,...result});
}
fs.writeFileSync(output,JSON.stringify({provenance,limits:{steps:128,milliseconds:2000},rows},null,2)+'\n');
