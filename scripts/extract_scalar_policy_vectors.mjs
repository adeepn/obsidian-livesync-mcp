// Node 26.13.1+: node scripts/extract_scalar_policy_vectors.mjs COMMONLIB OUTPUT
// Execute native pinned method/gate; they use ECMAScript builtins only.
import fs from 'node:fs';
import vm from 'node:vm';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
const [root, output] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const files = ['src/services/base/VaultService.ts', 'src/serviceFeatures/targetFilter.ts'];
const sources = files.map(file => {
  const bytes = fs.readFileSync(`${root}/${file}`, 'utf8');
  if (bytes !== execFileSync('git', ['-C', root, 'show', `${pin}:${file}`], {encoding:'utf8'})) throw Error('Source differs from pin');
  return bytes;
});
const start = sources[0].indexOf('    isFileSizeTooLarge(size: number) {');
const end = sources[0].indexOf('\n    }', start) + 6;
if (start < 0 || end < start) throw Error('Missing size method');
const sizeFunction = vm.runInNewContext(stripTypeScriptTypes('function '+sources[0].slice(start, end).trim())+';isFileSizeTooLarge');
const gateStart = sources[1].indexOf('        if (!settings.useIgnoreFiles) {');
const gateEnd = sources[1].indexOf('\n        const filepath', gateStart);
if (gateStart < 0 || gateEnd < gateStart) throw Error('Missing selection gate');
const activeFunction = vm.runInNewContext('(function(settings){'+sources[1].slice(gateStart, gateEnd)+';return false;})');
const inputs = [null, false, true, 0, -0, -1, .5, 1, 50, 300.25, 1e308,
  '', ' ', '\ufeff\u00a0', '0', '-1', '0.5', '1', '50', '300.25', '0x10', '0b10', '0o10',
  '1e-6', '1e309', 'Infinity', '-Infinity', 'NaN', 'inf', '1_0', '+0', 'PRIVATE_INVALID',
  [], [null], [false], [true], [1], ['2'], [[['3']]], [1,2], {}, {valueOf:1},
  {toString:null}, [{toString:'PRIVATE_VALUE'}]];
const sizes = [...new Set([0, 1, 2, 524288, 524289, 1048576, 1048577, 52428800, 52428801, 314834944, 314834945, Number.MAX_SAFE_INTEGER, ...inputs.flatMap(input => {
  try { const n = Number(input) * 1024 * 1024;
    return Number.isFinite(n) && n >= 0 && n < Number.MAX_SAFE_INTEGER ? [Math.floor(n), Math.floor(n)+1] : [];
  } catch { return []; }
})])];
const vectors = inputs.map(input => {
  let predicate, cause;
  try { predicate = sizes.map(size => ({size, tooLarge:sizeFunction.call({settings:{syncMaxSizeInMB:input}}, size)})); }
  catch (error) { cause = error.constructor.name; }
  return {input, ignoreActive:!activeFunction({useIgnoreFiles:input}), ...(cause ? {cause} : {predicate})};
});
// Pairwise expectations compare native executions; boundary sizes above are
// input recipes only, never expected values or peer-derived outputs.
const equivalence = inputs.flatMap((left, a) => inputs.map((right, b) => {
  const l = vectors[a], r = vectors[b];
  return {a,b,ignoreAgrees:l.ignoreActive===r.ignoreActive,
    ...(l.cause || r.cause ? {cause:'TypeError'} :
      {sizeAgrees: l.predicate.every((p,i)=>p.tooLarge===r.predicate[i].tooLarge)})};
}));
fs.writeFileSync(output, gzipSync(JSON.stringify({generator_sha256:createHash('sha256').update(fs.readFileSync(import.meta.filename)).digest('hex'),upstream_commit:pin,node:process.version,
 dependencies:[],sources:files.map((file,i)=>({file,sha256:createHash('sha256').update(sources[i]).digest('hex')})),
 citations:['src/services/base/VaultService.ts:73-81','src/serviceFeatures/targetFilter.ts:132-137'],vectors,equivalence},null,2)+'\n'));
