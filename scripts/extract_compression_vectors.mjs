// Node >=26.13.1; COMMONLIB WHEELS_DIR WHEELS_TARBALL FFLATE_TARBALL OUTPUT
// Execute pinned src/pouchdb/compress.ts:9-101 with verified published dependencies.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {deflateRawSync} from 'node:zlib';
import {pathToFileURL} from 'node:url';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root,wheels,archive,fflateArchive,out]=process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const sha=b=>createHash('sha256').update(b).digest('hex');
if(execFileSync('git',['-C',root,'rev-parse','HEAD'],{encoding:'utf8'}).trim()!==pin) throw Error('Wrong pin');
const lock=JSON.parse(execFileSync('git',['-C',root,'show',`${pin}:package-lock.json`],{encoding:'utf8'}));
const fpin=lock.packages['node_modules/fflate'];
if(fpin.version!=='0.8.3' || 'sha512-'+createHash('sha512').update(fs.readFileSync(fflateArchive)).digest('base64')!==fpin.integrity) throw Error('fflate integrity');
const fsource=execFileSync('tar',['-xOf',fflateArchive,'package/esm/index.mjs']);
// Import the verified tar member directly; never trust another local installation.
const fflate=await import('data:text/javascript;base64,'+fsource.toString('base64'));
const {dependency}=await splitterDependencies(root,wheels,archive);
const binary=await import(pathToFileURL(path.resolve(wheels,'package/dist/binary/base64.js')));
const sources={};
function read(name) {
 const source=fs.readFileSync(path.join(root,'src',name),'utf8');
 if(source!==execFileSync('git',['-C',root,'show',`${pin}:src/${name}`],{encoding:'utf8'})) throw Error('Modified source');
 sources[name]=sha(source); return source;
}
const source=read('pouchdb/compress.ts');
read('services/base/RemoteService.ts');
read('common/models/setting.const.defaults.ts');
const ctx=vm.createContext({Blob,Uint8Array,fflate,...binary});
vm.runInContext(stripTypeScriptTypes(source.replace(/^import[\s\S]*?;\n/gm,'').replace(/export /g,'')),ctx);
const marker=vm.runInContext('MARK_SHIFT_COMPRESSED',ctx);
const inputs={ascii:'hello world! '.repeat(100),unicode:'Привет 🙂 世界! '.repeat(100),
 crlf:'hello\r\nworld\r\n'.repeat(100),base64:Buffer.from('binary\0payload'.repeat(100)).toString('base64'),
 wrapped:('QUFB'.repeat(50)+'\r\n').repeat(20),marked:marker+'already marked',empty:'',short:'x',
 number:7,object:{data:'unchanged'},null:null,large:'large Unicode 🙂! '.repeat(60000),
 surrogate_high:'a'.repeat(100)+'\ud800',surrogate_low:'\udfff'+'b'.repeat(100),
 surrogate_pair:'\ud83d\ude42'.repeat(100),bom:'\ufeff'+'hello!'.repeat(100),
 base64_unpadded:'YQ',base64_padbits:'YR==',base64_excess_padding:'YQ===',
 random:Buffer.concat(Array.from({length:128},(_,i)=>createHash('sha256').update('compression-vector-'+i).digest())).toString('base64')};
inputs.level_witness=Array.from({length:500},(_,i)=>'prefix '+createHash('sha256').update(String(i%71)).digest('hex').repeat(1+i%7)+' suffix! ').join('');
inputs.marked_valid=marker+await ctx._compressText(inputs.ascii);
const vectors=[];
for(const [name,input] of Object.entries(inputs)) {
 const compressed=(await ctx.compressDoc({data:input})).data;
 let decompressed, error=null;
 try {decompressed=(await ctx.decompressDoc({data:compressed})).data;} catch(e) {error=e.constructor.name;}
 const v={name,input,compressed,decompressed,error};
 if(typeof input==='string') {
  const converted=binary.tryConvertBase64ToArrayBuffer(input);
  v.form=converted?'binary':'text';
  v.encoding_bytes_b64=Buffer.from(converted || await new Blob([input]).arrayBuffer()).toString('base64');
  v.raw_compressed=await ctx._compressText(input);
  v.raw_decompressed=await ctx._decompressText(v.raw_compressed);
  v.zlib=[];
  v.zlib_level6=marker+(converted?'~':'')+deflateRawSync(converted?new Uint8Array(converted):Buffer.from(input),{level:6}).toString('base64');
  for(const mode of ['text','binary']) {
   const bytes=mode==='binary' && converted ? new Uint8Array(converted) : new Uint8Array(await new Blob([input]).arrayBuffer());
   const wire=marker+(mode==='binary'?'~':'')+deflateRawSync(bytes,{level:8}).toString('base64');
   v.zlib.push({mode,wire,result:(await ctx.decompressDoc({data:wire})).data});
  }
 }
 vectors.push(v);
}
const invalid_utf8=[];
for(const bytes of [[0xed,0xa0,0x80],[0xff,0x61],[0xef,0xbb,0xbf,0x61],[0xf0,0x9f]]) {
 const wire=marker+deflateRawSync(Buffer.from(bytes),{level:8}).toString('base64');
 invalid_utf8.push({wire,result:(await ctx.decompressDoc({data:wire})).data});
}
// Operational-budget witnesses; all projected bytes come from native decode.
const budget_vectors=[];
for (const size of [4096,4097,4*1024*1024+3,16*1024*1024]) {
 for (const binary of [false,true]) {
  const raw=Buffer.alloc(size,65);
  const wire=marker+(binary?'~':'')+deflateRawSync(raw,{level:8}).toString('base64');
  const result=(await ctx.decompressDoc({data:wire})).data;
  budget_vectors.push({size,binary,wire,result_bytes:Buffer.byteLength(result),result_sha256:sha(result)});
 }
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,
 source_sha256:sources,zlib:process.versions.zlib,dependency,
 fflate_dependency:{package:'fflate',version:fpin.version,integrity:fpin.integrity,url:fpin.resolved,
 source_sha256:{'package/esm/index.mjs':sha(fsource)}},marker,vectors,invalid_utf8,budget_vectors},null,2)+'\n');
