// Node >=26.13.1. Execute pinned source, with in-memory host/DB/chunk seams.
// Usage: node scripts/probe_ingested_blob_size.mjs COMMONLIB WHEELS ARCHIVE OUTPUT
import fs from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root, wheels, archive, output] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f', hashes = {};
function read(file) {
    const source = fs.readFileSync(`${root}/src/${file}`, 'utf8');
    if (source !== execFileSync('git', ['-C', root, 'show', `${pin}:src/${file}`], {encoding:'utf8'})) throw Error(`Modified ${file}`);
    hashes[file] = createHash('sha256').update(source).digest('hex');
    return source;
}
const primitives = await splitterDependencies(root, wheels, archive);
const {toBlob} = await import(pathToFileURL(`${wheels}/package/dist/binary/blob.js`));
const ctx = vm.createContext({Blob, Uint8Array, Buffer, TextDecoder, AbortController,
    ...primitives, toBlob, segmenter:null,
    arrayBufferToBase64Single:async b=>Buffer.from(b).toString('base64'),
    Logger:()=>{}, LOG_LEVEL_VERBOSE:0, LOG_LEVEL_NOTICE:1,
    serialized:async (_key, fn)=>fn(), stripAllPrefixes:p=>p,
    isErrorOfMissingDoc:e=>e.status===404, isLegacyNote:()=>false,
    REMOTE_COUCHDB:'couchdb', RemoteTypes:{REMOTE_MINIO:'minio'},
    IDPrefixes:{Chunk:'h:',Obfuscated:'f:'}, ICHeader:'i:',ICXHeader:'ix:',PSCHeader:'ps:',
    isFolderInfo:()=>false, ContentReadError:class extends Error {},
    prepareIncomingStorageWrite:async ()=>({kind:'complete',result:'accepted'}),
    contentEvidence:async ()=>({})});
function run(code) { vm.runInContext(stripTypeScriptTypes(code.replace(/^export /gm,'')), ctx); }
const lines = (file, start, end)=>read(file).split('\n').slice(start-1,end).join('\n');
run(lines('string_and_binary/path.ts',164,175));
run(lines('common/utils.ts',83,85)+'\n'+lines('common/utils.ts',97,99)+'\n'+lines('common/utils.ts',114,124)+'\n'+lines('common/utils.ts',132,138));
// Host-read dispatch is real; the native adapter returns the requested string/bytes.
run('class Access {\n'+lines('serviceModules/FileAccessBase.ts',201,207)+'\n}');
run('var access = new Access(); access.getPath=x=>x; access._readOp=async(p,f)=>f(p);');
const chunks = read('string_and_binary/chunks.ts');
run((chunks.slice(0,chunks.indexOf('// eslint-disable-next-line @typescript-eslint/no-unsafe-assignment'))+
    chunks.slice(chunks.indexOf('export function splitPiecesText('),chunks.indexOf('export async function splitPiecesRabinKarpOld(')))
    .replace(/^import .*;\n/gm,''));
const entries = read('managers/EntryManager/EntryManagerImpls.ts');
run(entries.slice(entries.indexOf('function readFromDatabase'),entries.indexOf('export async function getDBEntryMetaByPath')));
run(entries.slice(entries.indexOf('export function canUseOnDemandChunking'),entries.indexOf('export async function getDBEntryByPath')));
const reflection = read('serviceModules/ServiceFileHandlerBase/reflection.ts');
run('class IncompleteFileReflectionError extends Error {}\n'+reflection.slice(reflection.indexOf('export async function applyDatabaseEntryToStorage('),reflection.indexOf('async function reconcileAdvancedIncomingRevision(')));
const vectors = [];
for (const version of ['v1','v2','v2-segmenter','v3-rabin-karp']) {
    for (const input of (process.argv.includes('--edges')
        ? ['\ufeff', 'a'.repeat(102399)+'🙂B', 'a'.repeat(204799)+'🙂B']
        : ['abc','\ufeffabc','Привет','A🙂B'])) {
        const leaves = new Map(), logs = [];
        let metadata;
        ctx.access.adapter={vault:{read:()=>input,readBinary:()=>Buffer.from(input).buffer}};
        const blob=ctx.createBlob(await ctx.access.vaultReadAuto('note.md'));
        const fn=version==='v1'?ctx.splitPieces2:version==='v3-rabin-karp'?ctx.splitPiecesRabinKarp:ctx.splitPieces2V2;
        const settings={syncInternalFiles:false, syncOnlyRegEx:'',syncIgnoreRegEx:'',processSizeMismatchedFiles:false};
        const host={services:{path:{id2path:id=>id},setting:{currentSettings:()=>settings}}};
        const managers={log:m=>logs.push(m), hashManager:{usesIndependentIdKey:()=>false,getChunkIDFromCache:()=>false,
            computeHash:p=>createHash('sha256').update(p).digest('hex')},
            splitter:{initialised:Promise.resolve(),splitContent:async note=>(await fn(note.data,102400,true,20,'note.md',version==='v2-segmenter'))()},
            localDatabase:{get:async ()=>{throw {status:404};},put:async doc=>{metadata=doc;return {ok:true,id:doc._id,rev:'1-device'};}},
            chunkManager:{transaction:fn=>fn(), getChunkIDFromCache:()=>false,
                write:async docs=>{docs.forEach(d=>leaves.set(d._id,d));return {result:true,processed:{written:docs.length,cached:0,duplicated:0}};},
                read:async ids=>ids.map(id=>leaves.get(id))}};
        await ctx.putDBEntryInternal(host,managers,{_id:'note.md',path:'note.md',ctime:1,mtime:1,data:blob},false,{mode:'latest'});
        const loaded=await ctx.getDBEntryFromMeta(host,managers,metadata);
        const text=ctx.readContent(loaded);
        const reflectionContext={host:{getPath:()=>metadata.path,db:()=>({readConflictedRevs:async ()=>[],readEntryFromMeta:async ()=>loaded}),
            storage:()=>({getStub:async ()=>null}),log:m=>logs.push(m)},setting:{currentSettings:()=>settings}};
        let reflected;
        try { reflected=await ctx.applyDatabaseEntryToStorage(reflectionContext,metadata,null); }
        catch(e) { if(e.constructor.name!=='IncompleteFileReflectionError') throw e; reflected='size-mismatch'; }
        vectors.push({version,input,size:metadata.size,pieces:loaded.data,text,reflected,logs});
        if(metadata.size!==Buffer.byteLength(input)) throw Error('Blob size mismatch');
    }
}
fs.writeFileSync(output,JSON.stringify({pin,source_sha256:hashes,dependency:primitives.dependency,vectors},null,2)+'\n');
console.log(`${vectors.length} pinned writer/read/reflection vectors`);
