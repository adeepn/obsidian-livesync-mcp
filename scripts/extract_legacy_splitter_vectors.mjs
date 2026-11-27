// Node >=26.13.1; usage: node scripts/extract_legacy_splitter_vectors.mjs COMMONLIB WASM_ESM OUTPUT
// Executes pinned source verbatim (not a JS reimplementation). Fixed seed 0x3c202611.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {deflateSync} from 'node:zlib';
const [root,wasmFile,out]=process.argv.slice(2), pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const sha=b=>createHash('sha256').update(b).digest('hex'), sources={};
function read(p) {
 const local=fs.readFileSync(path.join(root,'src',p),'utf8');
 if(local!==execFileSync('git',['-C',root,'show',`${pin}:src/${p}`],{encoding:'utf8'})) throw Error(`Modified ${p}`);
 sources[p]=sha(local); return local;
}
if(execFileSync('git',['-C',root,'rev-parse','HEAD'],{encoding:'utf8'}).trim()!==pin) throw Error('Wrong pin');
if(sha(fs.readFileSync(wasmFile))!=='e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a') throw Error('Wrong hash oracle');
const wasm=await (await import(pathToFileURL(path.resolve(wasmFile)))).default();
const ids=p=>'h:'+wasm.h64(`${p}-${p.length}`).toString(36);
let messages=[];
const end='END';
const ctx=vm.createContext({Blob,Uint8Array,AbortController,TextDecoder,Buffer,
 postBack:(_key,_seq,value)=>messages.push(value),END_OF_DATA:end,
 arrayBufferToBase64Single:async b=>Buffer.from(b).toString('base64')});
const source=read('string_and_binary/chunks.ts');
// Only unreachable sentence helpers are omitted; all active legacy functions are verbatim.
let code=source.slice(0,source.indexOf('// eslint-disable-next-line @typescript-eslint/no-unsafe-assignment'))+
 source.slice(source.indexOf('export function splitPiecesText('),source.indexOf('export async function splitPiecesRabinKarp('));
code=code.replace(/^import .*;\n/gm,'').replace(/export /g,'');
vm.runInContext(stripTypeScriptTypes(code),ctx);
const worker=read('worker/bg.worker.splitting.ts').replace(/^import .*;\n/gm,'').replace(/export /g,'');
vm.runInContext(stripTypeScriptTypes(worker),ctx);
for(const p of ['ContentSplitter/ContentSplitterBase.ts','ContentSplitter/ContentSplitterV1.ts','ContentSplitter/ContentSplitterV2.ts','ContentSplitter/ContentSplitters.ts','managers/EntryManager/EntryManagerImpls.ts']) read(p);
const cases=[];
function add(name,raw,text,path,custom,minimum) {cases.push({name,raw,text,path,custom,minimum});}
const texts=['','a\n','a\r\nb\r\n','#\ntext\n# title\nend','```js\na;b,c:d<e\n```\ntail',
 ' ```\nabc=\n ```\nlast','  ```\n'+ 'x'.repeat(2050)+'\n  ```\n',
 '   ```\n```\n#\nnext','    ```\nx;y\n    ```','🙂漢\n'.repeat(3000),
 'x'.repeat(102399)+'🙂tail','x'.repeat(204799)+'🙂', 'tiny\n'.repeat(30000),
 'x'.repeat(300000),'\ufeffBOM\n\ud800\udc00\ud800Z', 'x\n'.repeat(100), 'x\n'.repeat(101)];
for(let i=0;i<texts.length;i++) for(const custom of [0,1,5,60]) {
 const raw=Buffer.from(await new Blob([texts[i]]).arrayBuffer());
 add(`text-${i}-c${custom}`,raw,true,['note.md','note.txt','note.canvas','note.MD','note.bin'][i%5],custom,[1,20,21,100,1000][i%5]);
}
let seed=0x3c202611;
const rand=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed;};
for(let i=0;i<80;i++) {
 const text=Array.from({length:100+rand()%1500},()=>['a\n','b\r\n','#\n','```\n','🙂,',';:<','Z=\n','漢'][rand()%8]).join('');
 add(`seed-${i}`,Buffer.from(text),true,['x.md','x.txt','x.canvas','x.MD'][i%4],[0,1,5,60][i%4],[1,2,19,20,21,99,100,101][i%8]);
}
for(const extension of ['pdf','json','bin','PDF']) for(const size of [0,99,100,101,99999,100000,100001,204801]) for(const custom of [0,1,5,60]) {
 const raw=Buffer.alloc(size,0xff);
 for(const offset of [0,9,99,100,999,1000,9999,10000,10001,102399,102400,102401,150000]) if(offset<size) raw[offset]=[0,47,44,10][Math.floor(offset/7)%4];
 add(`binary-${extension}-${size}-c${custom}`,raw,false,`x.${extension}`,custom,20);
}
// Clamp boundaries plus surrogate cuts for every maximum and the binary fallback quirks.
for(const size of [99999999,100000000,100000001]) add(`upper-clamp-${size}`,Buffer.alloc(size,0xff),false,'x.json',60,20);
for(const custom of [0,1,5,60]) add(`surrogate-cut-${custom}`,Buffer.from('a'.repeat(102400*(custom+1)-1)+'🙂Z'),true,'x.txt',custom,20);
for(const ext of ['pdf','json','bin']) {
 const raw=Buffer.alloc(200000,0xff),delim={pdf:47,json:44,bin:0}[ext]; raw[11000]=10;raw[150000]=delim;
 add(`distant-preferred-${ext}`,raw,false,`x.${ext}`,0,20);
 const lf=Buffer.alloc(150000,0xff);lf[11000]=10;add(`lf-only-${ext}`,lf,false,`x.${ext}`,0,20);
}
for(const threshold of [125,1562.5,19531.25,244140.625,3051757.8125,38146972.65625]) {
 for(const size of new Set([Math.floor(threshold),Math.ceil(threshold),Math.ceil(threshold)+1])) {
  const raw=Buffer.alloc(size,0xff); if(size>11000) raw[11000]=44;
  add(`minimum-transition-${threshold}-${size}`,raw,false,'x.json',0,20);
 }
}
for(const indent of ['', ' ', '  ', '   ']) {
 for(const suffix of ['a;b,c:d<e\n```\ntail','a;b=','a;'.repeat(1100)+'\n```']) {
  add(`fence-${indent.length}-${suffix.length}`,Buffer.from(indent+'```\n'+suffix),true,'x.md',0,100);
 }
}
add('js-line-terminators',Buffer.from('```\na\rb;c\u2028d,e\u2029f:<\n```'),true,'x.md',0,20);
add('fractional-minimum',Buffer.from('\n'.repeat(199)),true,'x.md',0,.2);
const vectors=[];
for(const c of cases) {
 const blob=new Blob([c.raw],{type:c.text?'text/plain':'application/octet-stream'}), maximum=102400*(c.custom+1), plain=/\.(md|txt|canvas)$/.test(c.path);
 const common={name:c.name,text:c.text,path:c.path,custom:c.custom,minimum:c.minimum,input_deflate_b64:deflateSync(c.raw).toString('base64')};
 for(const version of ['v1','v2','v2-segmenter']) {
  const fn=version==='v1'?ctx.splitPieces2:ctx.splitPieces2V2;
  const pieces=[];for await(const p of (await fn(blob,maximum,plain,c.minimum,c.path,version==='v2-segmenter'))()) pieces.push(p);
  messages=[];await ctx.processSplit({key:1,dataSrc:blob,pieceSize:maximum,plainSplit:plain,minimumChunkSize:c.minimum,filename:c.path,useSegmenter:version==='v2-segmenter',splitVersion:version==='v1'?1:2});
  const workerPieces=messages.filter(m=>m!==end);
  if(JSON.stringify(workerPieces)!==JSON.stringify(pieces.length?pieces:[''])) throw Error(`Worker mismatch ${c.name}`);
  vectors.push({...common,version,expected_lengths:pieces.map(p=>c.text?p.length:Buffer.from(p,'base64').length),expected_ids:pieces.map(ids),
   expected_piece_sha256:pieces.map(p=>sha(Buffer.from(p,'utf16le'))),worker_empty_sentinel:pieces.length===0});
 }
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,seed:'0x3c202611',source_sha256:sources,case_count:cases.length,vectors},null,2)+'\n');
console.log(`${cases.length} inputs; ${vectors.length} direct and ${vectors.length} worker executions`);
