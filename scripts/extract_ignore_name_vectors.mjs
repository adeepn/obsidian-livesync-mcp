// Node 26.13.1+: COMMONLIB DEPENDENCY_TARBALL_DIRECTORY OUTPUT
// Execute targetFilter.ts:90-145 and path.ts:241-265 with published minimatch.
import fs from 'node:fs';
import vm from 'node:vm';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';
import {verifySelectionFiles} from './selection_provenance.mjs';
verifySelectionFiles();
const [root,archives,out]=process.argv.slice(2),pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const source_sha256={},sha=b=>createHash('sha256').update(b).digest('hex');
function read(file){const s=fs.readFileSync(root+'/src/'+file,'utf8');if(s!==execFileSync('git',['-C',root,'show',pin+':src/'+file],{encoding:'utf8'}))throw Error('Modified source');source_sha256[file]=sha(s);return s;}
const lock=JSON.parse(execFileSync('git',['-C',root,'show',pin+':package-lock.json'],{encoding:'utf8'}));
const dependencies=JSON.parse(fs.readFileSync('vendor/selection/sources.json'));
for(const [name,dep] of Object.entries(dependencies)){
 const archive=archives+'/'+name.replaceAll('/','_')+'.tgz';
 if(dep.integrity!==lock.packages['node_modules/'+name].integrity||'sha512-'+createHash('sha512').update(fs.readFileSync(archive)).digest('base64')!==dep.integrity)throw Error('Dependency integrity');
 for(const member of execFileSync('tar',['-tzf',archive],{encoding:'utf8'}).trim().split('\n').filter(p=>p.includes('/dist/esm/')&&p.endsWith('.js'))){if(!fs.readFileSync('vendor/selection/node_modules/'+name+'/'+member.slice(8)).equals(execFileSync('tar',['-xOf',archive,member])))throw Error('Modified dependency');}
}
const {Minimatch}=await import('file://'+process.cwd()+'/vendor/selection/node_modules/minimatch/dist/esm/index.js');
const paths=read('string_and_binary/path.ts'),filter=read('serviceFeatures/targetFilter.ts');
const ctx=vm.createContext({Minimatch,unique:a=>[...new Set(a)],getStoragePathFromUXFileInfo:f=>f,LOG_LEVEL_DEBUG:0,LOG_LEVEL_VERBOSE:0});
vm.runInContext(stripTypeScriptTypes(paths.slice(paths.indexOf('const matchOpts:'),paths.indexOf('\n}',paths.indexOf('export async function isAcceptedAll('))+2)+filter.slice(filter.indexOf('export function isAcceptedByIgnoreFilesFactory('),filter.indexOf('\nexport function useTargetFilters('))).replaceAll('export ',''),ctx);
const factory=vm.runInContext('isAcceptedByIgnoreFilesFactory',ctx),rows=[];
for(const names of ['', 'a,,b',' .gitignore , ','../rules','/rules','rules/../.gitignore','a\\b','\ufeff.gitignore\ufeff','a\u0085,b'])for(const use of [false,true])for(const mode of ['missing','unreadable','readable']){
 const settings={ignoreFiles:names,useIgnoreFiles:use},lookups=[];
 const host={services:{setting:{currentSettings:()=>settings,onSettingRealised:{addHandler(){}}},appLifecycle:{onLoaded:{addHandler(){}}}},serviceModules:{storageAccess:{isExistsIncludeHidden:async p=>{lookups.push(p);return mode!=='missing';},readHiddenFileText:async()=>{if(mode==='unreadable')throw Error('unreadable');return '*.md\n!keep.md';}}}};
 const allowed=await factory(host,()=>{})('note.md');rows.push({settings,mode,names:names.split(',').map(v=>v.trim()),allowed,lookups});
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,source_sha256,dependencies,rows},null,2)+'\n');
