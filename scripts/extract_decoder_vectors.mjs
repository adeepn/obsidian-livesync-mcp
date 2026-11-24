// Node >=26.13.1; COMMONLIB WHEELS_DIR WHEELS_TARBALL OUTPUT
// Execute pinned splitter functions and the real published decoder. No ported oracle.
import fs from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {deflateSync} from 'node:zlib';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root,wheels,archive,out] = process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const {dependency,...primitives}=await splitterDependencies(root,wheels,archive);
const src=execFileSync('git',['-C',root,'show',`${pin}:src/string_and_binary/chunks.ts`],{encoding:'utf8'});
if(src!==fs.readFileSync(root+'/src/string_and_binary/chunks.ts','utf8')) throw Error('Modified source');
const ctx=vm.createContext({Blob,Uint8Array,Buffer,TextDecoder,...primitives,
 arrayBufferToBase64Single:async b=>Buffer.from(b).toString('base64')});
const code=src.slice(0,src.indexOf('// eslint-disable-next-line @typescript-eslint/no-unsafe-assignment'))+
 src.slice(src.indexOf('export function splitPiecesText('),src.indexOf('export async function splitPiecesRabinKarpOld('));
vm.runInContext(stripTypeScriptTypes(code.replace(/^import .*;\n/gm,'').replace(/export /g,'')),ctx);
const cases=[];
function add(name,parts,path='x.md') {
 const blob=new Blob(parts,{type:'text/plain'});
 cases.push({name,blob,path});
}
for(const [name,s] of Object.entries({bom:'\ufeff',leading:'\ufeffabc',middle:'abc\ufeffdef',trailing:'abc\ufeff',repeated:'\ufeff\ufeff',nul:'\0a\0',loneHigh:'\ud800',loneLow:'\udc00',badPair:'\ud800A\udc00',pair:'\ud83d\ude42'})) {
 for(const n of [0,127,128,129,1023]) add(`${name}-${n}`,['x'.repeat(n),s]);
}
for(const n of [0,120,128,129]) {
 for(const bytes of [[0xff,0x80,0xfe],[0xc0,0xaf],[0xe2,0x82],[0xed,0xa0,0x80],[0xf4,0x90,0x80,0x80],[0xf7,0xbf,0xbf,0xbf],[0xc2,0x41],[0xef,0xbb,0xbf,0,0xff]])
  add(`malformed-${n}-${bytes.join('-')}`,['x'.repeat(n),new Uint8Array(bytes)]);
}
for(const n of [102398,102399,102400]) add(`nonplain-pair-cut-${n}`,['a'.repeat(n),'🙂\ud800Z'],'x.html');
add('bom-at-each-v3-cut',['\ufeff'.repeat(2000)]);
const vectors=[];
for(const c of cases) for(const version of ['v1','v2','v2-segmenter','v3-rabin-karp']) {
 const fn=version==='v1'?ctx.splitPieces2:version==='v3-rabin-karp'?ctx.splitPiecesRabinKarp:ctx.splitPieces2V2;
 const pieces=[];
 for await(const p of (await fn(c.blob,102400,/\.(md|txt|canvas)$/.test(c.path),20,c.path,version==='v2-segmenter'))()) pieces.push(p);
 vectors.push({name:c.name,version,path:c.path,input_deflate_b64:deflateSync(Buffer.from(await c.blob.arrayBuffer())).toString('base64'),
 expected_utf16_deflate_b64:pieces.map(p=>deflateSync(Buffer.from(p,'utf16le')).toString('base64'))});
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,dependency,source_sha256:createHash('sha256').update(src).digest('hex'),case_count:cases.length,vectors},null,2)+'\n');
console.log(`${cases.length} inputs x 4 algorithms = ${vectors.length} vectors`);
