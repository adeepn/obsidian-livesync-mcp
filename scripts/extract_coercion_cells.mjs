// Node 26.13.1+: COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL WASM_TARBALL OUTPUT
// ContentSplitterBase.ts:68-71; chunks.ts:218-493,527-545. Isolated real upstream cells.
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const args=process.argv.slice(2),output=args.pop(),wasm=args.pop();
const custom=process.env.ZERO_EDGES ? [] : process.env.OVERFLOW_EDGES ? [[Infinity],[-Infinity]] : process.env.INGRESS_EDGES ? [] : process.env.CROSS_EDGES ? [] : process.env.COERCION_EDGES ? [{toString:0},'-Infinity'] : [0,'0',null,true,false,' 0.5 ','0x1','1e0','bad',[],[0],[1,2],{},'Infinity'];
const minimum=process.env.ZERO_EDGES ? ['0','00','0.0','+0',' 0 ','0x0','1e-1000',['0.0'],[],[0],'\t'] : process.env.OVERFLOW_EDGES ? [[Infinity],[-Infinity]] : process.env.INGRESS_EDGES ? [{toString:0}] : process.env.CROSS_EDGES ? [{toString:0},[{toString:0}]] : process.env.COERCION_EDGES ? [{toString:null},[0.5]] : process.env.INCLUDE_MINIMUM ? [20,'1','bad',true,false,null,[1],[2],[1,2],{},0.5] : [];
let provenance;
const rows=[];
const lexicalJSON=v=>JSON.stringify(v,(_,x)=>x===Infinity?'__F1_POS_INF__':x===-Infinity?'__F1_NEG_INF__':x).replaceAll('"__F1_POS_INF__"','1e309').replaceAll('"__F1_NEG_INF__"','-1e309');
for(const key of ['customChunkSize','minimumChunkSize'])for(const value of key==='customChunkSize'?custom:minimum)
for(const version of ['v1','v2','v2-segmenter','v3-rabin-karp'])for(const mode of ['plain','text','binary'])
for(const ingress of process.env.INGRESS_EDGES ? ['empty','bom'] : ['normal']) {
 const settings={chunkSplitterVersion:version,hashAlg:'sha1',customChunkSize:process.env.ZERO_EDGES||process.env.OVERFLOW_EDGES||process.env.INGRESS_EDGES?0:process.env.CROSS_EDGES?'bad':key==='minimumChunkSize'?-0.999:0,minimumChunkSize:20,[key]:value};
 let text=value==='-Infinity' ? 'A' : 'AAAA\n'.repeat(65);
 if(key==='customChunkSize' && value==='bad' && version==='v3-rabin-karp'){let seed=4216;text='';for(let i=0;i<32768;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;text+=String.fromCharCode(32+(seed>>>16)%95);}}
 if(ingress==='empty')text='';if(ingress==='bom')text='\ufeff';
 const payload={path:mode==='plain'?'coerce.md':mode==='text'?'coerce.svg':'coerce.png',text:mode!=='binary',base64:Buffer.from(text).toString('base64')};
 const child=spawnSync(process.execPath,['scripts/extract_settings_oracle.mjs',...args,'/dev/null',wasm],{encoding:'utf8',timeout:3000,env:{...process.env,G3_CELL:lexicalJSON({settings,payload})}});
 let result;
 if(child.error?.code==='ETIMEDOUT')result={status:'time_limit'};
 else {if(child.status!==0)throw Error(child.stderr);result=JSON.parse(child.stdout);}
 if(result.source_sha256){provenance??=Object.fromEntries(['source_sha256','wasm_dependency','decoder_dependency','upstream_commit','wasm_sha256','node'].map(k=>[k,result[k]]));for(const k of ['pieces','split_pieces','lossless_pieces','lossless_split_pieces'])result[k]=result[k].map(i=>result.strings[i]);for(const k of ['strings',...Object.keys(provenance)])delete result[k];}
 rows.push({key,value,version,mode,settings,settings_json:lexicalJSON(settings),payload,...result});
}
fs.writeFileSync(output,JSON.stringify({provenance,generator_sha256:createHash('sha256').update(fs.readFileSync('scripts/extract_coercion_cells.mjs')).digest('hex'),rows},null,2)+'\n');
