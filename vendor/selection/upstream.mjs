// Generated from pinned commonlib. Regenerate with scripts/extract_selection_runtime.mjs.
import {Minimatch} from './node_modules/minimatch/dist/esm/index.js';
const unique = a => [...new Set(a)];
export function parseCustomRegExp(regexp                    )                     {
    if (regexp.startsWith("!!")) {
        return [true, regexp.slice(2)];
    }
    return [false, regexp];
}
function parseCustomRegExpList                  (list                           , flags         , delimiter    ) {
    const d = delimiter ?? ",";
    const source = `${list ?? ""}`;
    const items = source
        .replace(/\n| /g, "")
        .split(d)
        .filter((e) => e);
    return items.map((e) => new CustomRegExp(e                                 , flags));
}

export class CustomRegExp {
    regexp        ;
    negate         ;
    pattern        ;
    constructor(regexp                    , flags         ) {
        const [negate, exp] = parseCustomRegExp(regexp);
        this.pattern = exp;
        this.regexp = new RegExp(exp, flags);
        this.negate = negate;
    }
    test(str        ) {
        return this.negate ? !this.regexp.test(str) : this.regexp.test(str);
    }
}

const matchOpts                   = { platform: "linux", dot: true, flipNegate: true, nocase: true };
const matcherCache = new WeakMap                                  ();

function matchIgnoredPath(path        , pattern        , ignore          )          {
    let matchers = matcherCache.get(ignore);
    if (matchers === undefined) {
        matchers = new Map();
        matcherCache.set(ignore, matchers);
    }
    let matcher = matchers.get(pattern);
    if (matcher === undefined) {
        matcher = new Minimatch(pattern, matchOpts);
        matchers.set(pattern, matcher);
    }
    return matcher.match(path);
}

/**
 * returns whether the given path is accepted (not ignored) by the `.gitignore`.
 * @param path path of the file which is relative from `.gitignore` file
 * @param ignore lines of `.gitignore`
 * @returns true when accepted.
 * false when not accepted.
 * undefined when the path is not mentioned in the `.gitignore` file.
 */
export function isAccepted(path        , ignore          )                      {
    if (path.indexOf("./") !== -1 || path.indexOf("../") !== -1) {
        // We do not accept this for handle the cases which ends with `/` by wildcard
        return false;
    }
    const patterns = ignore.map((e) => e.trim()).filter((e) => e.length > 0 && !e.startsWith("#"));
    let result = undefined;
    for (const pattern of patterns) {
        if (pattern.endsWith("/")) {
            // If the path ends with `/` and matched to the path. we do not handle more patterns to negate the result.
            if (matchIgnoredPath(path, `${pattern}**`, ignore)) {
                return false;
            }
        }
        const newResult = pattern.startsWith("!");
        const matched =
            matchIgnoredPath(path, pattern, ignore) ||
            (!pattern.endsWith("/") && matchIgnoredPath(path, pattern + "/**", ignore));

        if (matched) {
            result = newResult;
        }
    }
    return result;
}

/**
 * Checks whether the path is accepted by all ignored files.
 * @param path path of target file
 * @param ignoreFiles list of ignore files. i.e. [".gitignore", ".dockerignore"]
 * @param getList function to retrieve the file.
 * @returns true when accepted. false when should be ignored.
 */
export async function isAcceptedAll(
    path        ,
    ignoreFiles          ,
    getList                                             
) {
    const pathBase = path.substring(0, path.lastIndexOf("/"));
    const intermediatePaths = unique(
        pathBase
            .split("/")
            .reduce((p, c) => [...p, p[p.length - 1] + "/" + c], [""])
            .map((e) => e.substring(1))
    ).reverse();

    for (const intermediatePath of intermediatePaths) {
        for (const ignoreFile of ignoreFiles) {
            const ignoreFilePath = intermediatePath + "/" + ignoreFile;
            const list = await getList(ignoreFilePath);
            if (list === false) continue;
            const result = isAccepted(path.substring(intermediatePath.length ? intermediatePath.length + 1 : 0), list);
            if (result !== undefined) {
                return result;
            }
        }
    }
    return true;
}
export {parseCustomRegExpList};
