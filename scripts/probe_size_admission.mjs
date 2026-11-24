// Execute pinned upstream functions; no reimplementation of admission predicates.
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { execFileSync } from 'node:child_process';
const root = process.argv[2];
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
function read(file) {
  const source = fs.readFileSync(`${root}/src/${file}`, 'utf8');
  if (source !== execFileSync('git', ['-C', root, 'show', `${pin}:src/${file}`], {encoding:'utf8'})) throw Error(`Modified ${file}`);
  return source;
}
const vault = read('services/base/VaultService.ts');
const method = vault.slice(vault.indexOf('    isFileSizeTooLarge('), vault.indexOf('\n    /**', vault.indexOf('    isFileSizeTooLarge(')));
const admission = read('managers/StorageEventManager/admission.ts');
const functions = admission.slice(admission.indexOf('async function isSelectedPath'), admission.indexOf('async function selectNormal'));
// Remove trailing comment before the next function, if present.
const end = functions.lastIndexOf('\n/**');
const ctx = vm.createContext({shouldBeIgnored:()=>false, LOG_LEVEL_NOTICE:1, Blob, TextDecoder, Uint8Array});
vm.runInContext(stripTypeScriptTypes(`class Vault { ${method} }; ${functions.slice(0,end).replace(/^export /gm,'')}; globalThis.Vault=Vault;`), ctx);
const result = await vm.runInContext(`(async()=>{
 const vault=new Vault(); vault.settings={syncMaxSizeInMB:1/1048576};
 const effects={isFile:()=>true,isTarget:async()=>true,isTooLarge:s=>vault.isFileSizeTooLarge(s),log:()=>{}};
 const event=await selectRename({oldPath:'a.md',file:{path:'b.md',name:'b.md',stat:{size:2}}}, {}, effects);
 return {rawSize:1,rawOversized:vault.isFileSizeTooLarge(1),blobSize:new Blob([new TextDecoder().decode(new Uint8Array([255]))]).size,rename:event};
})()`,ctx);
if(result.rawOversized || result.blobSize!==3 || result.rename.type!=='DELETE') throw Error('Unexpected upstream behavior');
console.log(JSON.stringify({pin,...result},null,2));
