// Node 26.13.1+: COMMONLIB OUTPUT. Native-only getParamsFor expression oracle.
// ContentSplitterBase.ts:68 evaluates this before any splitter dispatch.
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const [root,out]=process.argv.slice(2),pin='484c164450d61deb2e301582b60cb9b5f9fa977f',source_sha256={};
function read(name){const s=fs.readFileSync(root+'/src/'+name,'utf8');if(s!==execFileSync('git',['-C',root,'show',pin+':src/'+name],{encoding:'utf8'}))throw Error('Modified source');source_sha256[name]=createHash('sha256').update(s).digest('hex');return s;}
const base=read('ContentSplitter/ContentSplitterBase.ts'),constants=read('common/models/shared.const.behabiour.ts');
const constant=constants.split('\n').find(v=>v.startsWith('export const MAX_DOC_SIZE_BIN =')).replace('export ','');
const expression=base.split('\n').find(v=>v.includes('const maxChunkSize = Math.floor('));
const parameters=Function('settings',constant+'\n'+expression+'\nreturn maxChunkSize;');
const values=[0,0.5,1,-1,-2,true,false,null,'0',' 0.5 ','\ufeff0x1\ufeff','1e0','0b10','0o10','+1.5','bad','','Infinity','-Infinity',[],[0],[1],[1.0],[0.5],[1,2],{}, {toString:0}, {valueOf:1}];
const inputs=[...values.map(v=>JSON.stringify(v)), '[1e309]', '[-1e309]', '[1e21]', '[1e-7]', '[1.0]', '0.000001', '[0.000001]', '-0', '[-0]', '1e308', '1'+'0'.repeat(400), '-1'+'0'.repeat(400)];
const rows=inputs.map(input_json=>{const value=JSON.parse(input_json);try{
 const n=parameters({customChunkSize:value}), number=Number(value), addition=value+value;
 return {input_json,piece_size:Number.isFinite(n)?n:String(n),primitive:String(value),number:String(number),addition:typeof addition==='number'?String(addition):addition,addition_type:typeof addition};
}catch(error){return {input_json,error_class:error.constructor.name};}});
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,node:process.version,source_sha256,generator_sha256:createHash('sha256').update(fs.readFileSync('scripts/extract_chunk_parameter_vectors.mjs')).digest('hex'),dependencies:[],runtime:'Native ECMAScript Math/truthiness/ToNumber; no package dependencies',rows},null,2)+'\n');
