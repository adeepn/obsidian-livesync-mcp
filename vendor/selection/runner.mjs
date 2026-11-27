import fs from 'node:fs';
import {parseCustomRegExpList,isAcceptedAll} from './upstream.mjs';
const request=JSON.parse(fs.readFileSync(0,'utf8'));
async function evaluate(input) {
try {
 const flags=input.case_sensitive?'':'i';
 let rule=null;
 if(input.ignore_files && !await isAcceptedAll(input.path,input.ignore_names,async p=>input.ignore_files[p.replace(/^\//,'')]??false)) rule='ignoreFiles';
 if(!rule) {
  const only=parseCustomRegExpList(input.only_patterns,flags,'|[]|');
  if(only.length && !only.some(p=>p.test(input.path))) rule='syncOnlyRegEx';
 }
 if(!rule) {
  const ignore=parseCustomRegExpList(input.ignore_patterns,flags,'|[]|');
  if(ignore.some(p=>p.test(input.path))) rule='syncIgnoreRegEx';
 }
 return {allowed:!rule,rule,writer_supported:true,reason:null};
} catch(e) {
 return {allowed:false,rule:null,writer_supported:false,reason:'Invalid device selection pattern: '+e.message};
}

}
console.log(JSON.stringify(Array.isArray(request)?await Promise.all(request.map(evaluate)):await evaluate(request)));
