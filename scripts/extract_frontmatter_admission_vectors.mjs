// Node 26.13.1+: node scripts/extract_frontmatter_admission_vectors.mjs COMMONLIB OUTPUT
// The plugin syncs bytes, not YAML properties. Execute its native size method;
// the peer property projection tests separately define JSON safety semantics.
import fs from 'node:fs';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
const [root, output] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const file = 'src/services/base/VaultService.ts';
const source = fs.readFileSync(`${root}/${file}`, 'utf8');
if (source !== execFileSync('git', ['-C', root, 'show', `${pin}:${file}`], {encoding:'utf8'})) throw Error('Source differs from pin');
const start = source.indexOf('    isFileSizeTooLarge(size: number) {');
const end = source.indexOf('\n    }', start) + 6;
if (start < 0 || end < start) throw Error('Missing size method');
const predicate = vm.runInNewContext(stripTypeScriptTypes('function '+source.slice(start,end).trim())+';isFileSizeTooLarge');
let bomb = 'a0: &a0 [one, two]\n';
for (let i=1; i<10; i++) bomb += `a${i}: &a${i} [` + Array(10).fill(`*a${i-1}`).join(', ') + ']\n';
const inputs = [
 ['nodes-10001', 'items: ['+Array(10001).fill('0').join(',')+']\n'],
 ['depth-34', 'items: '+'['.repeat(34)+'0'+']'.repeat(34)+'\n'],
 ['alias-sequence', 'a: &a [1,2]\nb: *a\n'],
 ['alias-scalar', 'a: &a scalar\nb: *a\n'],
 ['alias-mapping', 'a: &a {x: 1}\nb: *a\n'],
 ['alias-chain', 'a: &a [1]\nb: &b [*a]\nc: *b\n'],
 ['alias-cycle', 'a: &a [*a]\n'],
 ['billion-laughs', bomb],
 ['depth-1000', 'a: '+'['.repeat(1000)+'0'+']'.repeat(1000)+'\n'],
 ['million-nodes', 'items: ['+Array(999997).fill('0').join(',')+']\n'],
];
const digest = x => createHash('sha256').update(x).digest('hex');
const vectors = inputs.map(([name,raw])=>{
 const text = `---\n${raw}---\nbody\n`;
 const size = Buffer.byteLength(text);
 return {name, size, input_sha256:digest(text), too_large:predicate.call({settings:{syncMaxSizeInMB:50}},size)};
});
fs.writeFileSync(output,JSON.stringify({generator_sha256:digest(fs.readFileSync(import.meta.filename)),upstream_commit:pin,node:process.version,dependencies:[],sources:[{file,sha256:digest(source)}],citations:['src/services/base/VaultService.ts:73-81','src/managers/EntryManager/EntryManagerImpls.ts:253-280'],vectors},null,2)+'\n');
