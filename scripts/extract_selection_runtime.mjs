// Generate the runtime directly from the pinned upstream, never a reimplementation.
// node scripts/extract_selection_runtime.mjs /tmp/settings-parity-commonlib
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';
const root=process.argv[2];
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f';
const hashes={};
function read(file) {
 const source=execFileSync('git',['-C',root,'show',`${pin}:src/${file}`],{encoding:'utf8'});
 if(source!==fs.readFileSync(`${root}/src/${file}`,'utf8')) throw Error('Modified upstream '+file);
 hashes[file]=createHash('sha256').update(source).digest('hex'); return source;
}
const utils=read('common/utils.ts');
const paths=read('string_and_binary/path.ts');
const parser=utils.slice(utils.indexOf('export function parseCustomRegExp('),utils.indexOf('export function matchRegExp('));
const list=utils.slice(utils.indexOf('function parseCustomRegExpList<'),utils.indexOf('export function constructCustomRegExpList<'));
const custom=utils.slice(utils.indexOf('export class CustomRegExp {'),utils.indexOf('type RegExpSettingKey'));
const matcher=paths.slice(paths.indexOf('const matchOpts:'),paths.indexOf('\n}',paths.indexOf('export async function isAcceptedAll('))+2);
const header="// Generated from pinned commonlib. Regenerate with scripts/extract_selection_runtime.mjs.\nimport {Minimatch} from './node_modules/minimatch/dist/esm/index.js';\nconst unique = a => [...new Set(a)];\n";
fs.writeFileSync('vendor/selection/upstream.mjs',header+stripTypeScriptTypes(parser+list+custom+matcher)+'\nexport {parseCustomRegExpList};\n');
fs.writeFileSync('vendor/selection/upstream-sources.json',JSON.stringify({pin,hashes},null,2)+'\n');

// Regeneration changes retained bytes; refresh their installed manifest last.
const {writeSelectionManifest}=await import('./selection_provenance.mjs');
writeSelectionManifest();
