// Node 26.13.1+: COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// Execute pinned dispatcher, grouping, hash managers, and verified dependencies.
// F13 projection is independent JS in extract_settings_oracle.mjs; raw stays raw.
import fs from 'node:fs';
import {gzipSync} from 'node:zlib';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args = process.argv.slice(2), output = args.pop(), wasm = args.pop();
const sha = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const rows = [];
let provenance;
for (const version of ['v1','v2','v2-segmenter','v3-rabin-karp'])
for (const path of ['cut.md','cut.MD'])
for (const hashAlg of ['', 'xxhash32','xxhash64','mixed-purejs','sha1'])
for (const mode of ['cut','safe','groups', ...(version === 'v1' && path.endsWith('.md') ? ['zero'] : [])]) {
    const cap = version === 'v3-rabin-karp' ? 30720 : 124;
    const input = mode === 'groups' ? 'head\n```\na🙂b\n```\n' + 'a'.repeat(cap-1)+'🙂'+'z'.repeat(cap*2+3) :
        mode === 'zero' ? 'a🙂🙂z' : 'a'.repeat(cap-(mode === 'safe'?2:1))+'🙂'+'z'.repeat(cap*2+3);
    const settings = {chunkSplitterVersion:version, customChunkSize:mode === 'zero'?-1:(cap/102400-1), minimumChunkSize:5, hashAlg};
    const payload = {path,text:true,base64:Buffer.from(input).toString('base64')};
    const result = JSON.parse(execFileSync(process.execPath, ['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasm],
        {env:{...process.env,G3_CELL:JSON.stringify({settings,payload})},maxBuffer:8*1048576}));
    if (result.status !== 'defined') throw Error('Nonterminating F13 vector');
    provenance ??= Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k=>[k,result[k]]));
    for (const key of ['pieces','split_pieces','lossless_pieces','lossless_split_pieces']) result[key] = result[key].map(i=>result.strings[i]);
    for (const key of ['strings',...Object.keys(provenance)]) delete result[key];
    rows.push({id:`${version}:${path}:${hashAlg||'raw'}:${mode}`,version,path,input,settings,...result});
}
// Direct native text functions retain caller lone units, unlike Blob ingress.
const direct_rows = [];
for (const point of ['\\ud800','\\udbff','\\udc00','\\udfff']) {
    const input = 'a'.repeat(123) + JSON.parse('"'+point+'"') + 'z'.repeat(251);
    const settings = {chunkSplitterVersion:'v1',customChunkSize:124/102400-1,minimumChunkSize:5,hashAlg:'sha1'};
    const payload = {path:'lone.MD',text:true,units:input,base64:''};
    const result = JSON.parse(execFileSync(process.execPath,['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasm],
        {env:{...process.env,G3_CELL:JSON.stringify({settings,payload})}}));
    if (result.status !== 'defined') throw Error('Native lone-unit cell');
    direct_rows.push({input,cap:124,pieces:result.pieces.map(i=>result.strings[i]),children:result.children,
        lossless_pieces:result.lossless_pieces.map(i=>result.strings[i]),lossless_children:result.lossless_children});
}
fs.writeFileSync(output,gzipSync(JSON.stringify({generator_sha256:sha(import.meta.filename),
    harness_sha256:sha('scripts/extract_settings_oracle.mjs'),provenance,rows,direct_rows},null,2)+'\n'));
