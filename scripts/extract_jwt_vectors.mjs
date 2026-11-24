// Node 26.13.1+: node scripts/extract_jwt_vectors.mjs COMMONLIB WHEELS_DIR WHEELS_TARBALL OUTPUT
// Execute the pinned JWTTokenGenerator, real published Computed and binary helpers.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {createHash, webcrypto} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {splitterDependencies} from './splitter_oracle_dependencies.mjs';
const [root,wheels,archive,out] = process.argv.slice(2);
const pin='484c164450d61deb2e301582b60cb9b5f9fa977f', sources={};
const sha=b=>createHash('sha256').update(b).digest('hex');
function read(file) {
 const s=fs.readFileSync(path.join(root,file),'utf8');
 if(s!==execFileSync('git',['-C',root,'show',`${pin}:${file}`],{encoding:'utf8'})) throw Error('Source differs from pin');
 sources[file]=sha(s);return s;
}
const {dependency}=await splitterDependencies(root,wheels,archive);
function verify(member) {
 if(dependency.source_sha256[member]) return;
 const s=fs.readFileSync(path.join(wheels,member));
 if(!s.equals(execFileSync('tar',['-xOf',archive,member]))) throw Error('Modified dependency');
 dependency.source_sha256[member]=sha(s);
 for(const m of s.toString().matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g)) {
  if(!m[1].startsWith('.')) throw Error('Unverified dependency');
  verify(path.posix.normalize(path.posix.join(path.posix.dirname(member),m[1])));
 }
}
verify('package/dist/dataobject/Computed.js');
const {Computed}=await import(pathToFileURL(path.resolve(wheels,'package/dist/dataobject/Computed.js')));
const binary=await import(pathToFileURL(path.resolve(wheels,'package/dist/binary/base64.js')));
let now=1806000000;
class FixedDate extends Date {constructor(...a){super(...(a.length?a:[now*1000]));}}
const ctx=vm.createContext({Computed,crypto:webcrypto,Date:FixedDate,atob,btoa,Uint8Array,Buffer,
 compatGlobal:{atob,Buffer},writeString:binary.writeString,
 arrayBufferToBase64SingleInternal:binary.arrayBufferToBase64Single});
const convert=read('src/string_and_binary/convert.ts');
vm.runInContext(stripTypeScriptTypes(convert.slice(convert.indexOf('export async function arrayBufferToBase64Single('),convert.indexOf('export { uint8Array')).replace('export ','')),ctx);
const src=read('src/replication/httplib.ts');
read('src/common/models/setting.const.defaults.ts');
const Generator=vm.runInContext(stripTypeScriptTypes(src.slice(src.indexOf('export class JWTTokenGenerator'),src.indexOf('export class AuthorizationHeaderGenerator')).replace('export ',''))+';JWTTokenGenerator',ctx);
const pem=(label,b)=>`-----BEGIN ${label}-----\n${Buffer.from(b).toString('base64')}\n-----END ${label}-----`;
const vectors=[], refresh=[];
for(const alg of ['HS256','HS512','ES256','ES512']) {
 let key,public_key;
 if(alg.startsWith('HS')) key=Buffer.from(webcrypto.getRandomValues(new Uint8Array(64))).toString('base64');
 else {
  const pair=await webcrypto.subtle.generateKey({name:'ECDSA',namedCurve:alg==='ES256'?'P-256':'P-521'},true,['sign','verify']);
  key=pem('PRIVATE KEY',await webcrypto.subtle.exportKey('pkcs8',pair.privateKey));
  public_key=pem('PUBLIC KEY',await webcrypto.subtle.exportKey('spki',pair.publicKey));
 }
 for(const [kid,sub,duration,instant] of [['','owner',5,1806000000],['test-id',' café ',0.125,1806000000],['','owner',1e20,1806000000],['','owner',1e308,1806000000],['','owner',5,2200000000]]) {
  now=instant;
  const auth={type:'jwt',jwtAlgorithm:alg,jwtKey:key,jwtKid:kid,jwtSub:sub,jwtExpDuration:duration};
  const generator=new Generator(), jwt=await generator.getJWT(auth);
  vectors.push({auth,now,header_json:JSON.stringify(jwt.header),payload_json:JSON.stringify(jwt.payload),token:jwt.token,public_key});
  if (instant !== 1806000000 || duration > 100) continue;
  for(const offset of [-1,0,1]) {
   now=Math.floor(jwt.payload.exp-10)+offset;
   // Invoke the exact refresh predicate, without mutating its cached baseline.
   const regenerate=await generator._jwtParams._shouldForceUpdate(auth,auth,jwt);
   refresh.push({iat:jwt.payload.iat,exp:jwt.payload.exp,now,regenerate});
  }
 }
}
const key_inputs=['AA','AA=','AA==','AAAA====','  AA==\n','AA-_','', ' \ufeff '];
const key_vectors=[];
for(const key of key_inputs) {
 const auth={...vectors[0].auth,jwtKey:key};
 let token=null, error=null;
 try { token=(await new Generator().getJWT(auth)).token; } catch(e) { error=e.constructor.name; }
 key_vectors.push({key,token,error,now});
}
fs.writeFileSync(out,JSON.stringify({key_vectors,note:'Synthetic test keys, not secrets. Never use these keys outside tests.',upstream_commit:pin,sources,node:process.version,dependencies:[dependency],vectors,refresh},null,2)+'\n');
