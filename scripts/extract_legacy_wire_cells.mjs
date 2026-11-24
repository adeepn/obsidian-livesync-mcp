// Node >=26.13.1. COMMONLIB MAIN_ORACLE.json.gz OUTPUT.json
// Execute pinned journalRecords.ts:69-108 on the upstream splitter outputs.
// No Python production encoders or hand-written transport expectations.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {gunzipSync} from 'node:zlib';
const [root,input,output]=process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f', sources={};
const sha=b=>createHash('sha256').update(b).digest('hex');
function read(name) {
    const source=fs.readFileSync(path.join(root,'src',name),'utf8');
    if (source!==execFileSync('git',['-C',root,'show',`${pin}:src/${name}`],{encoding:'utf8'})) throw Error('Modified upstream source');
    sources[name]=sha(source);return source;
}
const ctx=vm.createContext({Uint8Array,TextEncoder,TextDecoder});
const run=s=>vm.runInContext(stripTypeScriptTypes(s.replace(/^import .*;\n/gm,'').replace(/export /g,'')),ctx);
const utils=read('common/utils.ts');
run(utils.slice(utils.indexOf('const map = {'),utils.indexOf('export function escapeMarkdownValue')));
run(read('replication/journal/journalRecords.ts'));
const oracle=JSON.parse(gunzipSync(fs.readFileSync(input)));
if (oracle.upstream_commit!==pin) throw Error('Wrong splitter oracle pin');
const rows=[];
for (const vector of oracle.grid.filter(v=>v.payload==='g3-surrogate')) {
    const docs=vector.pieces.map((index,i)=>({_id:vector.children[i],data:oracle.pieces[index],type:'leaf',_rev:''}));
    const frame=Buffer.concat(docs.map(doc=>Buffer.from(ctx.serializeDoc(doc))));
    const decoded=ctx.decodeJournalRecords(new Uint8Array(frame));
    if (!decoded.ok) throw Error('Upstream journal decode');
    rows.push({id:vector.id,pieces:decoded.docs.map(d=>d.data),children:decoded.docs.map(d=>d._id),frame_base64:frame.toString('base64')});
}
fs.writeFileSync(output,JSON.stringify({upstream_commit:pin,node:process.version,source_sha256:sources,
    inputs_sha256:sha(fs.readFileSync(input)),splitter_dependency:oracle.decoder_dependency,
    hash_dependency:oracle.wasm_dependency,rows},null,2)+'\n');
