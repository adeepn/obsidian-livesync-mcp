// Node 26.13.1+: COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// Execute pinned splitter/hash and journalRecords.ts:69-108, with real dependencies.
import fs from 'node:fs';
import vm from 'node:vm';
import {spawnSync,execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';
const args=process.argv.slice(2), out=args.pop(), wasmArchive=args.pop();
const settings={chunkSplitterVersion:'v1',customChunkSize:-1,minimumChunkSize:-1,hashAlg:'xxhash64'};
const input='🙂\ufffd', path='gate.md';
const child=spawnSync(process.execPath,['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasmArchive],{encoding:'utf8',env:{...process.env,G3_CELL:JSON.stringify({settings,payload:{path,text:true,base64:Buffer.from(input).toString('base64')}})}});
if(child.status!==0) throw Error(child.stderr);
const result=JSON.parse(child.stdout), root=args[0], pin=result.upstream_commit;
const ctx=vm.createContext({Uint8Array,TextEncoder,TextDecoder});
const run=s=>vm.runInContext(stripTypeScriptTypes(s.replace(/^import .*;\n/gm,'').replace(/export /g,'')),ctx);
function read(name){const s=fs.readFileSync(root+'/src/'+name,'utf8');if(s!==execFileSync('git',['-C',root,'show',pin+':src/'+name],{encoding:'utf8'}))throw Error('Modified source');result.source_sha256[name]=createHash('sha256').update(s).digest('hex');return s;}
const utils=read('common/utils.ts');run(utils.slice(utils.indexOf('const map = {'),utils.indexOf('export function escapeMarkdownValue')));
run(read('replication/journal/journalRecords.ts'));
const pieces=result.pieces.map(i=>result.strings[i]);
const docs=pieces.map((data,i)=>({_id:result.children[i],data,type:'leaf',_rev:''}));
const frame=Buffer.concat(docs.map(d=>Buffer.from(ctx.serializeDoc(d))));
const decoded=ctx.decodeJournalRecords(frame);if(!decoded.ok)throw Error('Decode');
fs.writeFileSync(out,JSON.stringify({settings,input,path,...result,pieces,decoded:decoded.docs,frame_b64:frame.toString('base64')},null,2)+'\n');
