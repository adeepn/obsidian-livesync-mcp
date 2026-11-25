// Execute the pinned upstream functions under Node; no peer functions are imported.
// node scripts/extract_selection_vectors.mjs /tmp/settings-parity-commonlib tests/fixtures/selection.json
import fs from 'node:fs';
import vm from 'node:vm';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';
import {verifySelectionFiles} from './selection_provenance.mjs';
verifySelectionFiles();
const {Minimatch}=await import('../vendor/selection/node_modules/minimatch/dist/esm/index.js');
const [root,out]=process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const sources={};
function read(file) {
 const s=execFileSync('git',['-C',root,'show',`${pin}:src/${file}`],{encoding:'utf8'});
 if(s!==fs.readFileSync(`${root}/src/${file}`,'utf8')) throw Error('Modified source '+file);
 sources[file]=createHash('sha256').update(s).digest('hex');return s;
}
const utils=read('common/utils.ts'), paths=read('string_and_binary/path.ts'), entry=read('managers/EntryManager/EntryManagerImpls.ts');
const context=vm.createContext({Minimatch,unique:a=>[...new Set(a)]});
const fragments=[
 utils.slice(utils.indexOf('export function parseCustomRegExp('),utils.indexOf('export function matchRegExp(')),
 utils.slice(utils.indexOf('function parseCustomRegExpList<'),utils.indexOf('export function constructCustomRegExpList<')),
 utils.slice(utils.indexOf('export class CustomRegExp {'),utils.indexOf('/**\n * Copies properties')),
 paths.slice(paths.indexOf('const matchOpts:'),paths.indexOf('\n}',paths.indexOf('export async function isAcceptedAll('))+2),
 read('common/models/fileaccess.const.ts'),
 read('common/models/shared.const.behabiour.ts').match(/export const IDPrefixes = [\s\S]*?\};/)[0],
 entry.slice(entry.indexOf('export function isTargetFile('),entry.indexOf('export async function prepareChunk(')),
];
for(const source of fragments) vm.runInContext(stripTypeScriptTypes(source).replace(/export /g,''),context);
const parser=[];
for(const source of ['', 'a,b', 'a\nb', ' a |[]| b ', '"a"|[]|\'b\'', 'a|[]||[]|a', '\tfoo\r', '!!x']) {
 context.source=source;
 parser.push({source,items:vm.runInContext("parseCustomRegExpList(source,'','|[]|').map(e=>(e.negate?'!!':'')+e.pattern)",context)});
}
const regex=[];
const patterns=['', 'md$', '^a', '\\d', '\\w', '\\s', '\\bélan', '(?<=a)b', '(?<!a)b', '(?=a)', '(a)\\1', '[a-z]', '.', '^.$', '!!x', 'a|[]|b', 'a,b', '"a"', '/a/gi', '\\p{Letter}', '\\u{1F642}', '[', '(?i)a', '\\k<missing>', '(?<x>a)\\k<x>', '^a$', 'a/b', 'a\\\\b'];
for(const path of ['x.md','X.MD','élan.md','١','K','ſ','ab','aa','🙂','a/b','a\\b','a\nb']) {
 for(const pattern of patterns) for(const case_sensitive of [false,true]) {
  for(const key of ['syncOnlyRegEx','syncIgnoreRegEx']) {
   const settings={handleFilenameCaseSensitive:case_sensitive,syncInternalFiles:false,syncOnlyRegEx:'',syncIgnoreRegEx:'',[key]:pattern};
   context.host={services:{setting:{currentSettings:()=>settings}}};context.path=path;
   let allowed=null,error=false;
   try {allowed=vm.runInContext('isTargetFile(host,path)',context);} catch {error=true;}
   regex.push({path,only_patterns:settings.syncOnlyRegEx,ignore_patterns:settings.syncIgnoreRegEx,case_sensitive,allowed,error});
  }
 }
}
for(const [only,ignore] of [['^a','['],['^a','a'],['a|[]|b','!!a']]) for(const path of ['a','x']) {
 const settings={handleFilenameCaseSensitive:false,syncOnlyRegEx:only,syncIgnoreRegEx:ignore};
 context.host={services:{setting:{currentSettings:()=>settings}}};context.path=path;
 let allowed=null,error=false;
 try {allowed=vm.runInContext('isTargetFile(host,path)',context);} catch {error=true;}
 regex.push({path,only_patterns:only,ignore_patterns:ignore,case_sensitive:false,allowed,error});
}
const ignores=[];
const lists=[['*.md'],['folder/','!folder/keep.md'],['**/*.md','!**/keep.md'],['/root.md'],['folder'],['!a.md','*.md'],['# comment',' *.MD '],['a/**','!a/keep.md'],['{a,b}.md'],['@(a|b).md'],['\\!a.md']];
for(const path of ['a.md','A.MD','b.md','root.md','folder/a.md','folder/keep.md','nested/folder/a.md','a/keep.md','nested/a.md']) {
 for(const lines of lists) {
  const files={'.gitignore':lines};context.path=path;context.files=files;
  const allowed=await vm.runInContext("isAcceptedAll(path,['.gitignore'],async p=>files[p.replace(/^\\//,'')]??false)",context);
  ignores.push({path,files,names:['.gitignore'],allowed});
 }
}
for(const files of [
 {'.gitignore':['**/*.md'],'folder/.gitignore':['!keep.md']},
 {'.gitignore':['!**/*.md'],'folder/.gitignore':['*.md']},
 {'.gitignore':['!**/*.md'],'.otherignore':['**/*.md']},
]) for(const path of ['folder/keep.md','folder/no.md','a.md']) {
 context.path=path;context.files=files;
 ignores.push({path,files,names:['.gitignore','.otherignore'],allowed:await vm.runInContext("isAcceptedAll(path,['.gitignore','.otherignore'],async p=>files[p.replace(/^\\//,'')]??false)",context)});
}
fs.writeFileSync(out,JSON.stringify({pin,node:process.version,sources,parser,regex,ignores},null,2)+'\n');
console.log(`${parser.length} parser, ${regex.length} regex, ${ignores.length} ignore cases`);
