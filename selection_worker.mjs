// Transport wrapper; evaluation body mirrors the unchanged pinned runner.
import readline from 'node:readline';
import {verifySelectionFiles} from './scripts/selection_provenance.mjs';
verifySelectionFiles();
const {parseCustomRegExpList,isAcceptedAll}=await import('./vendor/selection/upstream.mjs');
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

const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const line of lines) {
 const request = JSON.parse(line);
 const result = request.op === 'selftest'
   ? {ok: (await evaluate({path:'selftest.md',only_patterns:'md$',ignore_patterns:'',case_sensitive:true})).allowed === true
       && (await evaluate({path:'selftest.txt',only_patterns:'md$',ignore_patterns:'',case_sensitive:true})).allowed === false}
   : await evaluate(request.payload);
 console.log(JSON.stringify({id:request.id,result}));
}
