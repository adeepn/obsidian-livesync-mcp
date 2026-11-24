// Node 26.13.1+: node scripts/extract_header_vectors.mjs COMMONLIB OUTPUT
// utils.ts:459-469 has no package dependencies; execute its exact pinned body.
import fs from 'node:fs';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
const [root, output] = process.argv.slice(2);
const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
const file = 'src/common/utils.ts';
const source = fs.readFileSync(`${root}/${file}`, 'utf8');
if (source !== execFileSync('git', ['-C', root, 'show', `${pin}:${file}`], {encoding:'utf8'})) throw Error('Source differs from pin');
const body = source.slice(source.indexOf('export function parseHeaderValues('), source.indexOf('/***', source.indexOf('export function parseHeaderValues(')));
const {stripTypeScriptTypes} = await import('node:module');
const parse = vm.runInNewContext(stripTypeScriptTypes(body.replace('export ', '')) + ';parseHeaderValues');
const inputs = ['', '\n \n', ' X-Test : first \nX-Test: last\nOther: ok ', 'No colon\n: absent\nEmpty: \nValid: yes', 'Url: https://proxy.example/path', 'A: one\na: two', '\ufeffKey\u00a0: \ufeffvalue\u00a0', '__proto__: ignored\nconstructor: set'];
const remoteFile = 'src/services/base/RemoteService.ts';
const remote = fs.readFileSync(`${root}/${remoteFile}`, 'utf8');
if (remote !== execFileSync('git', ['-C', root, 'show', `${pin}:${remoteFile}`], {encoding:'utf8'})) throw Error('Remote source differs from pin');
const start = remote.indexOf('const headers = new Headers(opts?.headers);');
const mergeBody = remote.slice(start, remote.indexOf('const cachedRequest', start));
const merges = [
  {base:{Authorization:'Basic test', 'Content-Type':'application/json'}, custom:{Authorization:'proxy', 'Content-Type':'custom', 'X-Proxy':'secret'}},
  {base:{Authorization:'Basic test'}, custom:{'X-Proxy':'  spaced  ', 'X-Latin':'café'}},
];
const merge_vectors = merges.map(({base,custom})=>({base,custom,expected:vm.runInNewContext(mergeBody+';Object.fromEntries(headers.entries())', {Headers,opts:{headers:base},customHeaders:custom,auth:{username:'u'}})}));
fs.writeFileSync(output, JSON.stringify({remote_source:remoteFile+':360-369',remote_source_sha256:createHash('sha256').update(remote).digest('hex'),merge_vectors,upstream_commit:pin, source:file+':459-469', source_sha256:createHash('sha256').update(source).digest('hex'), node:process.version, dependencies:[], vectors:inputs.map(input=>({input, expected:parse(input)}))}, null, 2)+'\n');
