// Node 26.13.1+: COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// ContentSplitterBase.ts:68-71; chunks.ts:218-493,527-545. Isolated real upstream cells.
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args=process.argv.slice(2),output=args.pop(),wasm=args.pop();
const custom=[0,'0',null,true,false,' 0.5 ','0x1','1e0','bad',[],[0],[1,2],{},'Infinity'];
const minimum=process.env.INCLUDE_MINIMUM ? [20,'1','bad',true,false,null,[1],[2],[1,2],{},0.5] : [];
let provenance;
const rows=[];
for(const key of ['customChunkSize','minimumChunkSize'])for(const value of key==='customChunkSize'?custom:minimum)
for(const version of ['v1','v2','v2-segmenter','v3-rabin-karp'])for(const mode of ['plain','text','binary']) {
 const settings={chunkSplitterVersion:version,hashAlg:'sha1',customChunkSize:key==='minimumChunkSize'?-0.999:0,minimumChunkSize:20,[key]:value};
 const payload={path:mode==='plain'?'coerce.md':mode==='text'?'coerce.svg':'coerce.png',text:mode!=='binary',base64:Buffer.from('AAAA\n'.repeat(65)).toString('base64')};
 const child=spawnSync(process.execPath,['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasm],{encoding:'utf8',timeout:3000,env:{...process.env,G3_CELL:JSON.stringify({settings,payload})}});
 let result;
 if(child.error?.code==='ETIMEDOUT')result={status:'time_limit'};
 else {if(child.status!==0)throw Error(child.stderr);result=JSON.parse(child.stdout);}
 if(result.source_sha256){provenance??=Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k=>[k,result[k]]));for(const k of ['pieces','split_pieces'])result[k]=result[k].map(i=>result.strings[i]);for(const k of ['strings',...Object.keys(provenance)])delete result[k];}
 rows.push({key,value,version,mode,settings,payload,...result});
}
fs.writeFileSync(output,JSON.stringify({provenance,generator_sha256:createHash('sha256').update(fs.readFileSync('scripts/extract_coercion_cells.mjs')).digest('hex'),rows},null,2)+'\n');
