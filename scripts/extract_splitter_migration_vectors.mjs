// Node >=26.13.1; COMMONLIB OUTPUT. Execute the pinned migration branch once.
import fs from 'node:fs';
import vm from 'node:vm';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';
const [root,out]=process.argv.slice(2),pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
function read(file) {
 const source=execFileSync('git',['-C',root,'show',`${pin}:src/${file}`],{encoding:'utf8'});
 if(source!==fs.readFileSync(`${root}/src/${file}`,'utf8')) throw Error(`Modified ${file}`);
 return source;
}
const source=read('services/base/SettingService.ts');
const begin=source.indexOf('if (settings.chunkSplitterVersion == "")');
const end=source.indexOf('return Promise.resolve(settings);',begin);
const constants=read('common/models/setting.const.ts');
const names=constants.slice(constants.indexOf('export const ChunkAlgorithmNames'),constants.indexOf('export const ChunkAlgorithms'));
const vectors=[];
for(const version of ['', 'v1','v2','v2-segmenter','v3-rabin-karp','unknown',undefined,null,0,false,[],[''],[null],['v1'],{},'toString','__proto__'])
 // SettingService.ts:143-150 uses JS truthiness; include empty-string equality.
 for(const enable of (version === '' ? [false,true,'',null,0] : [false,true]))
 for(const segmenter of (version === '' ? [false,true,'',null,0] : [false,true])) {
 const input={chunkSplitterVersion:version,enableChunkSplitterV2:enable,useSegmenter:segmenter};
 const settings=structuredClone(input);
 vm.runInNewContext(stripTypeScriptTypes(names.replace('export ',''))+'\n'+source.slice(begin,end),{settings});
 vectors.push({input,expected:settings});
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,source_sha256:createHash('sha256').update(source).digest('hex'),vectors},null,2)+'\n');
console.log(`${vectors.length} one-pass migration vectors`);
