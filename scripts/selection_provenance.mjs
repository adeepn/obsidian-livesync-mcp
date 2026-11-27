// Local installed-byte policy; no third-party code may import before verification.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const manifest='vendor/selection/installed-sha256.json';
function filesUnder(dir) {
 const result=[];
 for(const entry of fs.readdirSync(path.join(root,dir),{withFileTypes:true})) {
  const name=dir+'/'+entry.name;
  if(entry.isSymbolicLink()) throw Error('selection provenance mismatch');
  if(entry.isDirectory()) result.push(...filesUnder(name));
  else if(entry.isFile() && name!==manifest) result.push(name);
 }
 return result;
}
function installedFiles() {
 return [...filesUnder('vendor/selection'),'selection_worker.mjs','scripts/selection_provenance.mjs'].sort();
}
export function writeSelectionManifest() {
 const files=Object.fromEntries(installedFiles().map(name=>[name,createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex')]));
 fs.writeFileSync(path.join(root,manifest),JSON.stringify({version:1,files},null,2)+'\n');
}
export function verifySelectionFiles() {
 try {
  const value=JSON.parse(fs.readFileSync(path.join(root,manifest)));
  if(value.version!==1 || Object.keys(value).sort().join(',')!=='files,version') throw Error();
  const names=installedFiles();
  if(JSON.stringify(Object.keys(value.files).sort())!==JSON.stringify(names)) throw Error();
  for(const name of names) {
   const full=path.join(root,name);
   if(fs.lstatSync(full).isSymbolicLink() || createHash('sha256').update(fs.readFileSync(full)).digest('hex')!==value.files[name]) throw Error();
  }
 } catch {
  throw Error('selection provenance mismatch; regenerate the verified installation');
 }
}
