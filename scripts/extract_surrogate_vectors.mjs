// Node >=26.13.1; COMMONLIB WHEELS_DIR WHEELS_TARBALL OUTPUT POUCHDB_TARBALL
// Execute journalRecords.ts:69-108, chunks.ts:243-252 and the real published IO/hash dependencies.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root,wheels,archive,out,pouchArchive]=process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f', sources={};
const sha=b=>createHash('sha256').update(b).digest('hex');
function read(name) {
 const source=fs.readFileSync(path.join(root,'src',name),'utf8');
 if(source!==execFileSync('git',['-C',root,'show',`${pin}:src/${name}`],{encoding:'utf8'})) throw Error('Modified source');
 sources[name]=sha(source);return source;
}
if(execFileSync('git',['-C',root,'rev-parse','HEAD'],{encoding:'utf8'}).trim()!==pin) throw Error('Wrong pin');
const lock=JSON.parse(execFileSync('git',['-C',root,'show',`${pin}:package-lock.json`],{encoding:'utf8'}));
const couchdbDependency=lock.packages['node_modules/pouchdb-adapter-http'];
if('sha512-'+createHash('sha512').update(fs.readFileSync(pouchArchive)).digest('base64')!==couchdbDependency.integrity) throw Error('PouchDB integrity');
const couchdbSource=execFileSync('tar',['-xOf',pouchArchive,'package/lib/index.es.js']);
const couchdb_dependency={package:'pouchdb-adapter-http',version:couchdbDependency.version,
 integrity:couchdbDependency.integrity,url:couchdbDependency.resolved,source_sha256:sha(couchdbSource),
 source:'lib/index.es.js:765-773',oracle:'native JSON.stringify'};
const {dependency,...primitives}=await splitterDependencies(root,wheels,archive);
for(const name of ['hash/purejs.js','binary/hex.js','binary/encodedUTF16.tables.internal.js']) {
 const member='package/dist/'+name, local=fs.readFileSync(path.join(wheels,member));
 if(!local.equals(execFileSync('tar',['-xOf',archive,member]))) throw Error('Modified dependency');
 dependency.source_sha256[member]=sha(local);
}
const hashes=await import(pathToFileURL(path.resolve(wheels,'package/dist/hash/purejs.js')));
const ctx=vm.createContext({Blob,Uint8Array,TextEncoder,TextDecoder,Buffer,...primitives,...hashes,
 HashManagerCore:class {},HashAlgorithms:{SHA1:'sha1',MIXED_PUREJS:'mixed-purejs'}});
const run=s=>vm.runInContext(stripTypeScriptTypes(s.replace(/^import .*;\n/gm,'').replace(/export /g,'')),ctx);
const utils=read('common/utils.ts');run(utils.slice(utils.indexOf('const map = {'),utils.indexOf('export function escapeMarkdownValue')));
run(read('replication/journal/journalRecords.ts'));
const split=read('string_and_binary/chunks.ts');
run(split.slice(0,split.indexOf('// eslint-disable-next-line @typescript-eslint/no-unsafe-assignment'))+split.slice(split.indexOf('export function splitPiecesText('),split.indexOf('export async function splitPiecesRabinKarp(')));
run(read('managers/HashManager/PureJSHashManager.ts'));
const manager=vm.runInContext('new SHA1HashManager({})',ctx);
const input='a'.repeat(123)+'🙂z', pieces=[];
for await(const p of (await ctx.splitPieces2(new Blob([input],{type:'text/plain'}),124,false,5))()) pieces.push(p);
const splitter_controls=[];
for(const version of ['v2','v2-segmenter']) {
 const result=[];
 for await(const p of (await ctx.splitPieces2V2(new Blob([input],{type:'text/plain'}),124,false,5,'witness.MD',version==='v2-segmenter'))()) result.push(p);
 splitter_controls.push({version,pieces:result});
}
const ids=await Promise.all(pieces.map(async p=>'h:'+await manager.computeHashWithoutEncryption(p)));
const vectors=[];
function add(name,docs) {
 const frame=Buffer.concat(docs.map(d=>Buffer.from(ctx.serializeDoc(d))));
 const decoded=ctx.decodeJournalRecords(new Uint8Array(frame));if(!decoded.ok) throw Error('decode');
 const transport_replacements=docs.reduce((n,d,i)=>n+(d._id.startsWith('h:') ? d.data.split('').filter((c,j)=>c!==decoded.docs[i].data[j]).length : 0),0);
 vectors.push({name,docs,transport_replacements,frame_b64:frame.toString('base64'),decoded:decoded.docs,couchdb_json:docs.map(d=>JSON.stringify(d))});
}
add('witness',pieces.map((data,i)=>({_id:ids[i],data,type:'leaf',_rev:''})));
add('high-low-pieces',[{_id:'h:high',data:'\ud83d',type:'leaf',_rev:''},{_id:'h:low',data:'\ude42',type:'leaf',_rev:''}]);
for(const [name,data] of Object.entries({start:'\ud800abc',middle:'ab\udc00cd',end:'abc\udfff',high:'\ud83d',low:'\ude42',ascii:'ASCII',cyrillic:'Привет',emoji:'🙂',bom:'\ufeff',crlf:'a\r\nb'})) {
 add(name,[{_id:'h:'+name,data,type:'leaf',_rev:''}]);
 add('metadata-'+name,[{_id:'note-'+name,_rev:'1-test',value:data}]);
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,source_sha256:sources,dependency,couchdb_dependency,
 splitter_controls,witness:{input,path:'witness.MD',settings:{chunkSplitterVersion:'v1',hashAlg:'sha1',customChunkSize:-0.9987841796875,minimumChunkSize:5},pieces,ids},vectors},null,2)+'\n');
