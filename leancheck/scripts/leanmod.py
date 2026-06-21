#!/usr/bin/env python3
"""Shared file<->module mapping that honours each Lake `lean_lib`'s `srcDir`.

Lake module names are relative to a library's *source directory*, not to the project root: a lib
declared with `srcDir = "test"` exposes `test/AxiomAudit.lean` as module `AxiomAudit`, **not**
`test.AxiomAudit`. Deriving a module name (or its source path) by naively dotting the
project-relative path therefore targets a non-existent module for any lib with a non-default
`srcDir`, which made the cold-build gate fail spuriously (`lake build test.AxiomAudit` -> "no such
file or directory") on such files. These helpers read the declared `srcDir`s from the lakefile so
the mapping is correct; the common `srcDir = "."` (default) case is unchanged. `--selftest` runs
offline unit tests."""
import os, re, sys, functools

# Matches the `srcDir` field in both lakefile.toml (`srcDir = "x"`) and lakefile.lean (`srcDir := "x"`).
_SRCDIR_RE = re.compile(r'srcDir\s*:?=\s*"([^"]+)"')

@functools.lru_cache(maxsize=64)
def src_dirs(root):
    """The declared library source dirs (normalised, longest first), always including `''` — the
    project root / default `srcDir`. Longest-first so the most specific lib wins on a prefix match."""
    dirs = {""}
    for name in ("lakefile.toml", "lakefile.lean"):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                for d in _SRCDIR_RE.findall(f.read()):
                    dirs.add(d.strip().strip("/\\").replace("/", os.sep))
        except OSError:
            pass
    return sorted(dirs, key=lambda d: -len(d))

def file_to_module(path, root):
    """`<root>/<srcDir>/A/B.lean` -> `A.B`, stripping the longest matching lib `srcDir`."""
    rel = os.path.relpath(os.path.abspath(path), root)
    if rel.endswith(".lean"):
        rel = rel[:-5]
    for d in src_dirs(root):                       # longest first -> most specific srcDir wins
        if d and (rel == d or rel.startswith(d + os.sep)):
            rel = rel[len(d):].lstrip(os.sep)
            break
    return rel.replace(os.sep, ".")

def module_source(mod, root):
    """First existing source path for `mod` across the declared srcDirs, or `None`. Tries
    `<root>/<srcDir>/A/B.lean` for each srcDir (default `''` = the project root). The resolved
    candidate must lie strictly UNDER `root`; otherwise `None`. This guards against a mangled token
    such as `......home.vscode.wt.Foo`, whose dot->sep expansion is an ABSOLUTE `/home/...` path that
    `os.path.join(root, ...)` silently resolves OUTSIDE the project (e.g. into a sibling git
    worktree) — which used to make the cold gate try to `lake build` a foreign module (`unknown
    target`)."""
    sub = mod.replace(".", os.sep) + ".lean"
    if os.path.isabs(sub):                          # leading-dot mangle -> absolute path: never ours
        return None
    rootabs = os.path.realpath(root)
    for d in src_dirs(root):
        cand = os.path.join(root, d, sub) if d else os.path.join(root, sub)
        candabs = os.path.realpath(cand)
        if (candabs == rootabs or candabs.startswith(rootabs + os.sep)) and os.path.exists(cand):
            return cand                             # accept only sources strictly under the root
    return None

def project_root(base, subdir=None):
    """The Lake project root inside a checkout: `base/<subdir>` when a subdir is given (a monorepo
    whose Lake package is NOT at the repo root), else `base` unchanged. `subdir` defaults to the
    `LEANCHECK_PROJECT_SUBDIR` env var; an empty/whitespace value means 'no subdir' (root == base),
    so a single-package repo (the common case) is wholly unaffected. This is the one canonical
    definition; the shell SessionStart hook and `lwt.py` mirror it inline (no Python import there)."""
    if subdir is None:
        subdir = os.environ.get("LEANCHECK_PROJECT_SUBDIR", "")
    subdir = subdir.strip().strip("/\\")
    return os.path.join(base, subdir) if subdir else base


def selftest():
    import tempfile, shutil
    root = tempfile.mkdtemp()
    try:
        # a project with a default-srcDir lib and a `srcDir = "test"` lib (the AxiomAudit case)
        with open(os.path.join(root, "lakefile.toml"), "w") as f:
            f.write('[[lean_lib]]\nname = "MyLib"\n\n[[lean_lib]]\nname = "Audit"\nsrcDir = "test"\n')
        os.makedirs(os.path.join(root, "MyLib", "Sub"))
        os.makedirs(os.path.join(root, "test"))
        open(os.path.join(root, "MyLib", "Sub", "Foo.lean"), "w").close()
        open(os.path.join(root, "test", "Audit.lean"), "w").close()
        src_dirs.cache_clear()
        assert src_dirs(root) == ["test", ""], src_dirs(root)
        # file -> module (the default lib mirrors the path; the srcDir lib strips its prefix)
        assert file_to_module(os.path.join(root, "MyLib", "Sub", "Foo.lean"), root) == "MyLib.Sub.Foo"
        assert file_to_module(os.path.join(root, "test", "Audit.lean"), root) == "Audit"
        # module -> source (existence search across srcDirs)
        assert module_source("MyLib.Sub.Foo", root) == os.path.join(root, "MyLib", "Sub", "Foo.lean")
        assert module_source("Audit", root) == os.path.join(root, "test", "Audit.lean")
        assert module_source("Nope.Gone", root) is None
        # a mangled sibling-worktree token expands (dot->sep) to an ABSOLUTE path that os.path.join
        # would resolve OUTSIDE the project; it must be rejected (the Stop-gate `unknown target` bug)
        assert module_source("......home.vscode.wt.Foo", root) is None
        # no lakefile -> only the default root srcDir; module mirrors the path
        empty = tempfile.mkdtemp()
        try:
            src_dirs.cache_clear()
            assert src_dirs(empty) == [""], src_dirs(empty)
            assert file_to_module(os.path.join(empty, "A", "B.lean"), empty) == "A.B"
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        src_dirs.cache_clear()
        shutil.rmtree(root, ignore_errors=True)
    # project_root: explicit subdir joins; empty/whitespace = base unchanged (single-package repo);
    # default reads LEANCHECK_PROJECT_SUBDIR (unset -> base unchanged).
    assert project_root("/repo", "sub") == os.path.join("/repo", "sub")
    assert project_root("/repo", "") == "/repo" and project_root("/repo", "   ") == "/repo"
    assert project_root("/repo", "/sub/") == os.path.join("/repo", "sub")
    os.environ["LEANCHECK_PROJECT_SUBDIR"] = "pkg/lean"
    try:
        assert project_root("/repo") == os.path.join("/repo", "pkg", "lean")
    finally:
        del os.environ["LEANCHECK_PROJECT_SUBDIR"]
    assert project_root("/repo") == "/repo"
    print("leanmod selftest OK")
    return 0

if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else 0)
