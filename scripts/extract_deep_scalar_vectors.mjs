// Node 26.13.1+: node --stack-size=16384 scripts/extract_deep_scalar_vectors.mjs COMMONLIB OUTPUT
// Native builtins only: execute pinned size predicate and splitter cap expression.
import fs from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const [root, output] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f', sources = {};
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
function read(file) {
  const text = fs.readFileSync(`${root}/src/${file}`, 'utf8');
  if (text !== execFileSync('git', ['-C',root,'show',`${pin}:src/${file}`], {encoding:'utf8'})) throw Error('Modified source');
  sources[file] = sha(text); return text;
}
const vault = read('services/base/VaultService.ts');
const start = vault.indexOf('    isFileSizeTooLarge(size: number) {');
const size = vm.runInNewContext(stripTypeScriptTypes('function '+vault.slice(start,vault.indexOf('\n    }',start)+6).trim())+';isFileSizeTooLarge');
const base = read('ContentSplitter/ContentSplitterBase.ts');
const constants = read('common/models/shared.const.behabiour.ts');
const cap = Function('settings', constants.split('\n').find(s=>s.startsWith('export const MAX_DOC_SIZE_BIN =')).replace('export ','')+'\n'+base.split('\n').find(s=>s.includes('const maxChunkSize = Math.floor('))+'\nreturn maxChunkSize;');
// chunks.ts:368-370 uses the original minimum on the right of +=.
const chunks = read('string_and_binary/chunks.ts');
const adjustment = chunks.slice(chunks.indexOf('        let xMinimumChunkSize = minimumChunkSize;'),chunks.indexOf('        const org =',chunks.indexOf('        let xMinimumChunkSize = minimumChunkSize;')));
const adjust = Function('minimumChunkSize','textLen', chunks.split('\n').find(s=>s.startsWith('const MAX_ITEMS ='))+'\n'+adjustment+'\nreturn xMinimumChunkSize;');
const recipes = [480,500,1000,5000].map(depth=>({depth,seed:1}));
recipes.push(...[{seed:[null,[null,2],[]]}, {seed:[]}, {cycle:true}, {shared:true}, {undefined:true}]);
const rows = recipes.map(recipe=>{
 let value = recipe.seed;
 for(let i=0;i<(recipe.depth||0);i++) value=[value];
 if(recipe.cycle) {value=[1];value.push(value,null,2);}
 if(recipe.shared) {const a=[1,null];value=[a,a];}
 if(recipe.undefined) value=[undefined,null,[undefined,1]];
 return {recipe,primitive:String(value),number:String(Number(value)),addition:String(value+value),
   piece_size:String(cap({customChunkSize:value})), adjusted:Number(value)>0 ? String(adjust(value,1000)) : null,
   predicates:[1048576,1048577].map(bytes=>({bytes,too_large:size.call({settings:{syncMaxSizeInMB:value}},bytes)}))};
});
fs.writeFileSync(output,JSON.stringify({upstream_commit:pin,node:process.version,node_flags:process.execArgv,sources,dependencies:[],generator_sha256:sha(fs.readFileSync(import.meta.filename)),rows},null,2)+'\n');
