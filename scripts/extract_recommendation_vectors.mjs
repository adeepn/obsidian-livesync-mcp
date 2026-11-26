// F14: Node builtins only; no published package dependencies are imported.
// Node >=26; execute the complete pinned assessment module and its REAL imports.
// Usage: node --experimental-vm-modules scripts/extract_tweak_vectors.mjs COMMONLIB PLUGIN OUTPUT
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
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
const recommendedOnly=Object.keys(definitions.TweakValuesRecommendedTemplate)
    .filter(key=>!definitions.TweakCompatibilityRules.some(rule=>rule.key===key));
const pluginSource=read(plugin,pluginPin,'modules/coreFeatures/ModuleResolveMismatchedTweaks.ts');
// Execute the native method's matched early-return path. Fail if control
// reaches the real replicator acquisition; no replacement adoption algorithm.
const start=pluginSource.indexOf('    async _askUseRemoteConfiguration(');
const end=pluginSource.indexOf('        const publication =',start);
if(start<0||end<0) throw Error('Matched branch boundary');
const signatureStart=pluginSource.indexOf('function resolutionSettingsSignature(');
const signatureEnd=pluginSource.indexOf('export class ',signatureStart);
const gateStart=pluginSource.indexOf('    private requiresIdConfigurationReview(');
const gateEnd=pluginSource.indexOf('    private _selectNewerTweakSide(',gateStart);
if([signatureStart,signatureEnd,gateStart,gateEnd].some(x=>x<0)) throw Error('Native guard boundary');
const native=stripTypeScriptTypes(pluginSource.slice(signatureStart,signatureEnd)
    +'class Witness {\n'+pluginSource.slice(gateStart,gateEnd)+pluginSource.slice(start,end)
    +'throw Error("unexpected mismatched branch");\n}\n}\nWitness',{mode:'strip'});
const Witness=vm.runInNewContext(native,{assessTweakCompatibility:assess,
    Logger:()=>{throw Error('unexpected ID mismatch');}, LOG_LEVEL_NOTICE:0});
const full={...definitions.TweakValuesShouldMatchedTemplate};
const vectors=[];
for(const [key,currentValue,preferredValue] of [
    ['syncMaxSizeInMB',100,50],['useIgnoreFiles',true,false],['ignoreFiles','new','old']]) {
    const current={...full,[key]:currentValue},preferred={...full,[key]:preferredValue};
    const witness=new Witness(); witness.settings=structuredClone(current);
    witness._log=()=>{}; // Disable logging only; all decision functions are native.
    const assessment=assess(current,preferred);
    const result=await witness._askUseRemoteConfiguration(current,preferred);
    vectors.push({key,current,preferred,alignment:assessment.alignment,
        differences:assessment.entries.filter(e=>e.relation==='different').map(e=>e.key),
        result,retained:witness.settings[key]});
}
fs.writeFileSync(out,JSON.stringify({upstream_commit:pin,plugin_commit:pluginPin,
    node:process.version,source_sha256:hashes,dependencies:[],
    dependency_note:'Only pinned upstream modules and Node builtins; no npm dependencies',
    recommendedOnly,vectors},null,2)+'\n');
