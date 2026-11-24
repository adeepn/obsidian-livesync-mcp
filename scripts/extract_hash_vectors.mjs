// Execute pinned manager methods verbatim, using verified published primitives.
// Usage: node scripts/extract_hash_vectors.mjs COMMONLIB WHEELS_DIR WASM_ESM OUTPUT
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const [root,wheels,wasmFile,out] = process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const sha = data => createHash('sha256').update(data).digest('hex');
const sources={};
function read(name) {
    const file=fs.readFileSync(path.join(root,'src',name),'utf8');
    const pinned=execFileSync('git',['-C',root,'show',`${pin}:src/${name}`],{encoding:'utf8'});
    if(file!==pinned) throw Error(`Modified source ${name}`);
    sources[name]=sha(file); return file;
}
if(execFileSync('git',['-C',root,'rev-parse','HEAD'],{encoding:'utf8'}).trim()!==pin) throw Error('Wrong pin');
const lock=JSON.parse(fs.readFileSync(path.join(root,'package-lock.json')));
// Verify the downloaded archive against the pinned lock, before importing it.
const tar=fs.readFileSync(path.join(path.dirname(wheels),'task3b-wheels.tgz'));
if('sha512-'+createHash('sha512').update(tar).digest('base64')!==lock.packages['node_modules/octagonal-wheels'].integrity) throw Error('Wheels integrity');
for(const name of ['hash/purejs.js','binary/base64.js','binary/hex.js','binary/encodedUTF16.tables.internal.js']) {
    const member='package/dist/'+name;
    const original=execFileSync('tar',['-xOf',path.join(path.dirname(wheels),'task3b-wheels.tgz'),member]);
    const local=fs.readFileSync(path.join(wheels,member));
    if(!original.equals(local)) throw Error(`Modified dependency ${name}`);
    sources[member]=sha(local);
}
if(sha(fs.readFileSync(wasmFile))!=='e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a') throw Error('Wasm pin');
const primitives=await import(pathToFileURL(path.resolve(wheels,'package/dist/hash/purejs.js')));
const wasm=await (await import(pathToFileURL(path.resolve(wasmFile)))).default();
const ctx=vm.createContext({...primitives,TextEncoder,HashManagerCore:class {},HashAlgorithms:{LEGACY:'',XXHASH64:'xxhash64',SHA1:'sha1',MIXED_PUREJS:'mixed-purejs'}});
for(const file of ['XXHashHashManager.ts','PureJSHashManager.ts']) {
    const source=read('managers/HashManager/'+file).replace(/^import .*;\n/gm,'').replace(/export /g,'');
    vm.runInContext(stripTypeScriptTypes(source),ctx);
}
const classes={'':'XXHash32RawHashManager',xxhash32:'FallbackWasmHashManager',xxhash64:'XXHash64HashManager','mixed-purejs':'PureJSHashManager',sha1:'SHA1HashManager'};
const managers=Object.fromEntries(Object.entries(classes).map(([alg,name])=>{const m=vm.runInContext(`new ${name}({})`,ctx);m.xxhash=wasm;return [alg,m];}));
const recipes=[...['','helloWorld','abc-123-0','Привет мир','漢字中文','🙂🚀','\ud800','\udc00','\ud800A','\udc00\ud800','\ud83d\ude42','\r\n---\n{}[]%PDF-1.7'].map(text=>({text}))];
for(const n of [123,124,125,126,127,128,129,130]) {
    for(const tail of ['','🙂','\ud800','\udc00A']) recipes.push({repeat:'a',count:n-tail.length,tail});
}
recipes.push({repeat:'YWJjMTIz+/==',count:5461,tail:'YWJj'}); // exactly 64 KiB
// Deterministic varied UTF-16 corpus catches ordinary multiply rounding regressions.
for(let i=0;i<24;i++) recipes.push({text:Array.from({length:i*7+1},(_,j)=>String.fromCharCode((j*7919+i*1049)&65535)).join('')});
const vectors=[];
for(const recipe of recipes) {
    const piece=recipe.text??recipe.repeat.repeat(recipe.count)+(recipe.tail??'');
    const ids={};for(const [alg,m] of Object.entries(managers)) ids[alg]='h:'+await m.computeHashWithoutEncryption(piece);
    vectors.push({recipe,utf16_length:piece.length,ids});
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,wheels:'0.1.57',source_sha256:sources,vectors},null,2)+'\n');
console.log(`${vectors.length} pieces, ${vectors.length*5} IDs`);
