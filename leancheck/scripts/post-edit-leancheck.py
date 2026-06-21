#!/usr/bin/env python3
"""PostToolUse hook: after an Edit/Write/MultiEdit of a project Lean source file, run the warm
`leancheck` and return its compiler-style diagnostics to the agent as `additionalContext`. leancheck
is itself non-blocking (a cold file warms in the background and reports "warming") and owns the
per-project-root daemon key and the Mathlib-not-built guard — so this hook is just: filter, record
the touched module for the Stop cold-gate, run leancheck, surface its output. Logs to a /tmp debug
log for observability. `--selftest` runs offline unit tests."""
import sys, os, json, subprocess, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leanmod

DEBUG = os.environ.get("LEANCHECK_HOOK_LOG", "/tmp/leancheck-hook.log")

def is_target(tool, path):
    """True iff this is an edit of a project Lean source file: a `.lean` outside `.lake/`
    (dependency sources are skipped). Set LEANCHECK_INCLUDE to a path substring to restrict to a
    subtree (e.g. `LEANCHECK_INCLUDE=MyLib` to check only files whose path contains `MyLib`)."""
    if (tool not in ("Edit", "Write", "MultiEdit")
            or not isinstance(path, str) or not path.endswith(".lean")):
        return False
    if (os.sep + ".lake" + os.sep) in path:        # skip dependency sources (Mathlib, std, ...)
        return False
    inc = os.environ.get("LEANCHECK_INCLUDE")
    return (inc in path) if inc else True

def hook_output(ctx):
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx}}

def dbg(msg):
    try:
        with open(DEBUG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [post-edit pid={os.getpid()}] {msg}\n")
    except Exception:
        pass

def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    tool = d.get("tool_name")
    ti = d.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ""
    if not is_target(tool, path):
        return 0
    # The Lake package is `$CLAUDE_PROJECT_DIR` for a single-package repo, or its subdir for a
    # monorepo (LEANCHECK_PROJECT_SUBDIR) — project_root resolves both. Everything below (the
    # outside-root skip, LEANCHECK_ROOT, the relpath, the module mapping) keys off this one `proj`.
    proj = leanmod.project_root(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    # Skip edits to files OUTSIDE this project root (e.g. a sibling git worktree under /home/...):
    # `file_to_module` would relpath them to `../../home/...` and record a mangled `......home...`
    # token that pollutes the touched-list and breaks the cold gate. Such a file belongs to another
    # root's daemon, not this one.
    pathabs, projabs = os.path.realpath(path), os.path.realpath(proj)
    if not (pathabs == projabs or pathabs.startswith(projabs + os.sep)):
        dbg(f"skip edit outside project root ({proj}): {path}")
        return 0
    leancheck = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leancheck.py")
    session = d.get("session_id", "default")
    env = dict(os.environ, LEANCHECK_ROOT=proj)        # leancheck derives the per-root key from this
    rel = os.path.relpath(os.path.abspath(path), proj)
    # record the touched module for the Stop cold-gate (srcDir-aware: a lib with `srcDir = "test"`
    # exposes `test/Audit.lean` as module `Audit`, not `test.Audit`)
    try:
        mod = leanmod.file_to_module(path, proj)
        touch = f"/tmp/leancheck-touched-{session}.txt"
        seen = set(open(touch).read().split()) if os.path.exists(touch) else set()
        seen.add(mod); open(touch, "w").write("\n".join(sorted(seen)))
    except Exception as e:
        dbg(f"touch-list error: {e}")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, leancheck, path], env=env,
                           capture_output=True, text=True, timeout=80)
        report = (r.stdout or "").strip()
        dbg(f"{rel}: leancheck exit={r.returncode} bytes={len(report)} in {time.time()-t0:.2f}s")
    except Exception as e:
        dbg(f"{rel}: leancheck error: {e}")
        report = f"leancheck unavailable ({e}); rely on the cold build."
    if not report:
        return 0
    print(json.dumps(hook_output("leancheck — " + os.path.basename(path) + ":\n" + report)))
    return 0

def selftest():
    assert is_target("Edit", "/r/MyLib/A.lean")
    assert is_target("Write", "/r/src/Sub/A.lean")
    assert not is_target("Edit", "/r/.lake/packages/mathlib/Mathlib/A.lean")  # dependency skipped
    assert not is_target("Edit", "/r/MyLib/A.txt")
    assert not is_target("Read", "/r/MyLib/A.lean")
    os.environ["LEANCHECK_INCLUDE"] = "MyLib"                                 # restrict to a subtree
    assert is_target("Edit", "/r/MyLib/A.lean")
    assert not is_target("Edit", "/r/OtherLib/A.lean")
    del os.environ["LEANCHECK_INCLUDE"]
    env = hook_output("hi")
    assert env["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert env["hookSpecificOutput"]["additionalContext"] == "hi"
    json.loads(json.dumps(env))
    print("post-edit-leancheck selftest OK")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main() or 0)
