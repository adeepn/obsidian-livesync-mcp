// Execute exact pinned checkpoint methods; supply only external persistence/IO.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const root=process.argv[2] || '/tmp/settings-parity-commonlib';
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const hashes={};
function source(file) {
    const pinned=execFileSync('git',['-C',root,'show',`${pin}:src/${file}`],{encoding:'utf8'});
    assert.equal(fs.readFileSync(`${root}/src/${file}`,'utf8'),pinned,'source drift');
    hashes[file]=createHash('sha256').update(pinned).digest('hex');
    return pinned;
}
const types=source('replication/journal/JournalSyncTypes.ts');
const core=source('replication/journal/JournalSyncCore.ts');
const defaults=types.slice(0,types.indexOf('/** Processing stages'));
const methods=core.slice(core.indexOf('    async updateCheckPointInfo('),core.indexOf('    async isAvailable():'));
const ctx=vm.createContext({Set,Logger:()=>{},LOG_LEVEL_NOTICE:1,SyncParamsFetchError:Error});
vm.runInContext(stripTypeScriptTypes(defaults.replace(/^export /gm,'')+'\nclass Checkpoints {\n'+methods+'\n}\nglobalThis.Checkpoints=Checkpoints;'),ctx);
const results=[];
for (const scenario of ['same-epoch','no-send-history','sent-still-present','sent-missing','probe-failed','explicit-reset']) {
    let info={lastLocalSeq:42,journalEpoch:'2:old',knownIDs:new Set(['known']),sentIDs:new Set(['sent']),receivedFiles:new Set(['received']),sentFiles:new Set(scenario==='no-send-history'?[]:['123-docs.jsonl.gz'])};
    let probes=0;
    const peer=new ctx.Checkpoints();
    peer.hash='test';peer.checkpointGeneration=0;
    peer.store={get:async()=>info,set:async(k,v)=>{info=v;}};
    peer.storage={listFiles:async()=>{probes++; if(scenario==='probe-failed')throw Error('offline');return scenario==='sent-missing'?[]:['123-docs.jsonl.gz'];}};
    peer.getSyncParamsHandler=()=>({fetch:async()=>({protocolVersion:2,pbkdf2salt:scenario==='same-epoch'?'old':'new'})});
    if(scenario==='explicit-reset')await peer.resetCheckpointInfo();
    else await peer.ensureCheckpointCachesAreFresh();
    const cleared=['sent-missing','probe-failed','explicit-reset'].includes(scenario);
    for(const key of ['knownIDs','sentIDs','receivedFiles'])assert.equal(info[key].size,cleared?0:1);
    assert.equal(info.sentFiles.size,cleared||scenario==='no-send-history'?0:1);
    results.push({scenario,probes,checkpoint:Object.fromEntries(Object.entries(info).map(([k,v])=>[k,typeof v?.size==='number'?[...v]:v]))});
}
console.log(JSON.stringify({pin,node:process.version,hashes,results},null,2));
