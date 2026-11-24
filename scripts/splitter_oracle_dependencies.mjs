// Load published decoder/IO primitives only after verifying the pinned archive
// and every imported relative dependency against its corresponding tar member.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
export async function splitterDependencies(root, wheels, archive) {
    if (!wheels || !archive) throw Error('Supply wheels directory and published tarball');
    const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
    const lock = JSON.parse(execFileSync('git', ['-C', root, 'show', `${pin}:package-lock.json`], {encoding:'utf8'}));
    const integrity = 'sha512-' + createHash('sha512').update(fs.readFileSync(archive)).digest('base64');
    if (integrity !== lock.packages['node_modules/octagonal-wheels'].integrity) throw Error('Wheels integrity');
    const seen = new Set(), hashes = {};
    function verify(member) {
        if (seen.has(member)) return;
        seen.add(member);
        const original = execFileSync('tar', ['-xOf', archive, member]);
        const local = fs.readFileSync(path.join(wheels, member));
        if (!original.equals(local)) throw Error(`Modified dependency ${member}`);
        hashes[member] = createHash('sha256').update(local).digest('hex');
        for (const match of local.toString().matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g)) {
            if (!match[1].startsWith('.')) throw Error(`Unverified dependency ${match[1]}`);
            verify(path.posix.normalize(path.posix.join(path.posix.dirname(member), match[1])));
        }
    }
    const modules = ['binary/base64.js', 'binary/blob.js', 'binary/typedArray.js'];
    for (const name of modules) verify('package/dist/' + name);
    const [binary, blob, typed] = await Promise.all(modules.map(name => import(pathToFileURL(path.resolve(wheels, 'package/dist', name)))));
    return {readString:binary.readString, iterateBlobBlocks:blob.iterateBlobBlocks,
        concatUInt8Array:typed.concatUInt8Array, dependency:{package:'octagonal-wheels',version:'0.1.57',integrity,source_sha256:hashes}};
}

export function verifyWasmDependency(root, moduleFile, archive) {
    const pin = '484c164450d61deb2e301582b60cb9b5f9fa977f';
    const lock = JSON.parse(execFileSync('git', ['-C',root,'show',`${pin}:package-lock.json`], {encoding:'utf8'}));
    const entry = lock.packages['node_modules/xxhash-wasm-102'];
    const integrity = 'sha512-' + createHash('sha512').update(fs.readFileSync(archive)).digest('base64');
    if (integrity !== entry.integrity) throw Error('xxhash-wasm integrity');
    const original = execFileSync('tar', ['-xOf',archive,'package/esm/xxhash-wasm.js']);
    if (!original.equals(fs.readFileSync(moduleFile))) throw Error('Modified xxhash-wasm ESM');
    return {package:entry.name,version:entry.version,source:entry.resolved,integrity,
        module_sha256:createHash('sha256').update(original).digest('hex')};
}
