// Node >=26; execute the complete pinned assessment module and its REAL imports.
// Usage: node --experimental-vm-modules scripts/extract_tweak_vectors.mjs COMMONLIB PLUGIN OUTPUT
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {gzipSync} from 'node:zlib';
import {stripTypeScriptTypes} from 'node:module';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const [root,plugin,out] = process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const pluginPin='5070b67beae3b60e3a79132386a08da3f1ef213a';
const hashes={};
function read(base, commit, file) {
    const source=execFileSync('git',['-C',base,'show',`${commit}:src/${file}`],{encoding:'utf8'});
    if(source!==fs.readFileSync(path.join(base,'src',file),'utf8')) throw Error(`Modified source ${file}`);
    hashes[(base===root?'C:':'P:')+file]=createHash('sha256').update(source).digest('hex');
    return source;
}
const context=vm.createContext({});
const modules=new Map();
// Node's strip-only runtime rejects the one numeric enum in setting.type.ts.
// Mechanical TypeScript numeric-enum lowering, retaining names and reverse
// mappings from the original source; no compatibility constants are supplied.
function lowerNumericEnums(source) {
    return source.replace(/export enum (\w+) \{([^}]+)\}/g, (_, name, body)=>{
        const members=body.split(',').map(s=>s.trim()).filter(Boolean);
        const statements=members.map(member=>{
            const match=/^(\w+)\s*=\s*(-?\d+)$/.exec(member);
            if(!match) throw Error('Unsupported enum member '+member);
            const [,key,value]=match;
            return `${name}[${name}[${JSON.stringify(key)}]=${value}]=${JSON.stringify(key)};`;
        });
        return `export const ${name} = {};\n${statements.join('\n')}`;
    });
}
function load(file) {
    if(modules.has(file)) return modules.get(file);
    const source=read(root,pin,file);
    const module=new vm.SourceTextModule(stripTypeScriptTypes(lowerNumericEnums(source),{mode:'strip'}),{context,identifier:file});
    modules.set(file,module);
    return module;
}
const entry=load('common/models/tweak.compatibility.ts');
await entry.link((specifier,referencing)=>{
    if(!specifier.startsWith('.')) throw Error(`Unexpected dependency ${specifier}`);
    let file=path.posix.normalize(path.posix.join(path.posix.dirname(referencing.identifier),specifier));
    if(!file.endsWith('.ts')) file+='.ts';
    return load(file);
});
await entry.evaluate();
const {assessTweakCompatibility:assess}=entry.namespace;
const definitions=modules.get('common/models/tweak.definition.ts').namespace;
const constants=modules.get('common/models/setting.const.ts').namespace;
const settingSource=read(root,pin,'services/base/SettingService.ts');
const begin=settingSource.indexOf('if (settings.chunkSplitterVersion == "")');
const end=settingSource.indexOf('return Promise.resolve(settings);',begin);
if(begin<0||end<0) throw Error('Migration boundary');
const migrate=vm.compileFunction(settingSource.slice(begin,end)+'\nreturn settings;', ['settings','ChunkAlgorithmNames']);
const normalize=input=>migrate(structuredClone(input),constants.ChunkAlgorithmNames);
const pluginSource=read(plugin,pluginPin,'modules/coreFeatures/ModuleResolveMismatchedTweaks.ts');
const policyBegin=pluginSource.indexOf('    private _selectNewerTweakSide(');
const policyEnd=pluginSource.indexOf('    /**',policyBegin);
if(policyBegin<0||policyEnd<0) throw Error('Plugin policy boundary');
const Policy=vm.runInNewContext(stripTypeScriptTypes('class Policy {\n'+pluginSource.slice(policyBegin,policyEnd)+'\n}\nPolicy', {mode:'strip'}),{Logger:()=>{}});
let seed=0x5b202611;
const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/2**32;};
const pick=values=>values[Math.floor(random()*values.length)];
const full={...definitions.TweakValuesShouldMatchedTemplate};
const domains={minimumChunkSize:[0,20,40],longLineThreshold:[0,250,300],
    encrypt:[false,true],usePathObfuscation:[false,true],idDerivationVersion:[0,1,2],
    pathNormalizationVersion:[0,1],encryptInternalMetadata:[false,true],
    enableCompression:[false,true],useEden:[false,true],customChunkSize:[0,1,60],
    useDynamicIterationCount:[false,true],hashAlg:['','xxhash32','xxhash64','sha1'],
    enableChunkSplitterV2:[false,true],maxChunksInEden:[0,10,20],
    maxTotalLengthInEden:[0,1024,2048],maxAgeInEden:[0,10,20],usePluginSyncV2:[false,true],
    handleFilenameCaseSensitive:[false,true],useSegmenter:[false,true],E2EEAlgorithm:['v1','v2'],
    chunkSplitterVersion:['','v1','v2','v2-segmenter','v3-rabin-karp','unknown',null,0,false],
    tweakModified:[undefined,-1,0,1,2,99,null,true,'100']};
const inputs=[];
// Focused upstream vectors: known defaults, ID modes, case identity, explicit false/0/"".
inputs.push([{},{}],[{customChunkSize:0},{}],[{pathNormalizationVersion:0},{}],
    [{handleFilenameCaseSensitive:false},{}],[{handleFilenameCaseSensitive:true},{}],
    [{encryptInternalMetadata:true},{}],[{idDerivationVersion:1},{}],
    [{encrypt:true,usePathObfuscation:false,idDerivationVersion:1},{encrypt:true,usePathObfuscation:false,idDerivationVersion:0}],
    [{enableCompression:false,customChunkSize:0,hashAlg:''},{enableCompression:true,customChunkSize:10,hashAlg:'xxhash64'}]);
for(let i=0;i<420;i++) {
    let current={}, preferred={};
    if(i%3===0) {
        current={...full}; preferred={...full};
        const key=pick(Object.keys(domains)); current[key]=pick(domains[key]);
    } else if(i%3===1) {
        current={...full}; preferred={...full};
        for(const key of Object.keys(domains)) {
            if(random()<0.15) current[key]=pick(domains[key]);
            if(random()<0.15) preferred[key]=pick(domains[key]);
        }
    } else {
        for(const key of Object.keys(domains)) {
            if(random()<0.45) current[key]=pick(domains[key]);
            if(random()<0.45) preferred[key]=pick(domains[key]);
        }
    }
    current.passphrase='must-not-leak'; current.unknown='ignored';
    inputs.push([current,preferred]);
}
const vectors=[];
for(const [current,preferred] of inputs) {
    // Round-trip first: fixture omission has exactly the same meaning as input.
    const [c,p]=JSON.parse(JSON.stringify([current,preferred]));
    const normalizedCurrent=normalize(c),normalizedPreferred=normalize(p);
    const expected=assess(normalizedCurrent,normalizedPreferred);
    vectors.push({current:c,preferred:p,normalizedCurrent,normalizedPreferred,
        rawExpected:assess(c,p),expected});
}
const policies=[];
for(const currentModified of [undefined,-1,0,1,2,99,null,true,'100'])
 for(const preferredModified of [undefined,-1,0,1,2,99,null,true,'100'])
  for(const flag of [undefined,true,false,null,1,'true']) {
    const current={...full,customChunkSize:60,tweakModified:currentModified};
    const preferred={...full,tweakModified:preferredModified};
    const policy=new Policy(); policy.settings={autoAcceptCompatibleTweak:flag};
    let saves=0;policy.services={setting:{saveSettingData:async()=>{saves++;}}};
    const winner=await policy._shouldAutoAcceptCompatibleLossy(assess(current,preferred));
    policies.push({current,preferred,...(flag===undefined?{}:{flag}),winner:winner??null,saves});
  }
// Preserve original JSON literals across the fixture boundary: constructing
// these as JS numbers first would erase the precision boundary before Python reads it.
const numberVectors=[];
for(const mode of ['assessment','policy']) {
    let inputJson=JSON.stringify(mode==='assessment'
        ? [{...full,minimumChunkSize:'HIGH'}, {...full,minimumChunkSize:'LOW'}]
        : [{...full,customChunkSize:60,tweakModified:'HIGH'}, {...full,tweakModified:'LOW'}]);
    inputJson=inputJson.replace('"HIGH"','9007199254740993').replace('"LOW"','9007199254740992');
    const [current,preferred]=JSON.parse(inputJson);
    const expected=assess(normalize(current),normalize(preferred));
    const policy=new Policy();policy.settings={autoAcceptCompatibleTweak:true};
    policy.services={setting:{saveSettingData:async()=>{}}};
    numberVectors.push({inputJson,expected,winner:(await policy._shouldAutoAcceptCompatibleLossy(expected))??null});
}
// C:serviceFeatures/targetFilter.ts:93-97, execute the actual refresh closure.
const filterSource=read(root,pin,'serviceFeatures/targetFilter.ts');
const refreshBegin=filterSource.indexOf('    const refreshSettings = () => {');
const refreshEnd=filterSource.indexOf('    const invalidateIgnoreFileCache', refreshBegin);
if(refreshBegin<0||refreshEnd<0) throw Error('Ignore refresh boundary');
const splitIgnore=vm.compileFunction('let ignoreFiles=[]; const host={services:{setting:{currentSettings:()=>settings}}};\n'
    +filterSource.slice(refreshBegin,refreshEnd)+'\nrefreshSettings(); return ignoreFiles;', ['settings']);
// C:targetFilter.ts:102-127 catches storage read failures and returns false.
const readBegin=filterSource.indexOf('    const getIgnoreFile = async');
const readEnd=filterSource.indexOf('    host.services.setting.onSettingRealised', readBegin);
if(readBegin<0||readEnd<0) throw Error('Ignore read boundary');
const readIgnore=vm.compileFunction(stripTypeScriptTypes(
    'const ignoreFileCacheMap=new Map(); const log=()=>{}; const LOG_LEVEL_DEBUG=0, LOG_LEVEL_VERBOSE=0;\n'
    +filterSource.slice(readBegin,readEnd),{mode:'strip'})+'\nreturn getIgnoreFile(".gitignore");', ['host']);
const unreadable=await readIgnore({serviceModules:{storageAccess:{
    isExistsIncludeHidden:async()=>true,readHiddenFileText:async()=>{throw Error('synthetic read failure');}}}});
const defaults=modules.get('common/models/setting.const.defaults.ts').namespace.DEFAULT_SETTINGS;
const selection={unreadable,templateKeys:Object.keys(definitions.TweakValuesTemplate),
    defaults:{useIgnoreFiles:defaults.useIgnoreFiles,ignoreFiles:defaults.ignoreFiles},
    names:['.gitignore',' a, b ','a,,b','\ufeffa\ufeff,\u0085b\u0085',' .gitignore , rules '].map(ignoreFiles=>
        ({input:ignoreFiles,expected:splitIgnore({ignoreFiles})})),
    dependencies:[]}; // Only real upstream modules and Node built-ins; no npm imports.
fs.writeFileSync(out,gzipSync(JSON.stringify({upstream_commit:pin,plugin_commit:pluginPin,node:process.version,
    seed:'0x5b202611',source_sha256:hashes,selection,rules:definitions.TweakCompatibilityRules,vectors,policies,numberVectors})+'\n'));
console.log(`${vectors.length} assessment pairs (raw + migrated) + ${numberVectors.length} numeric boundary pairs; ${policies.length} plugin policy cases; ${modules.size} real upstream modules`);
