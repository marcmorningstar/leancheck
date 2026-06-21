#!/usr/bin/env python3
"""SubagentStop / Stop hook: before an agent is allowed to finish, run the AUTHORITATIVE
cold `lake build` of every Lean module it touched this session. If any fails, block the stop
and hand the cold errors back so the agent must keep working. If all pass (or nothing was
touched), allow the stop — so the orchestrator is only notified when the work is *really*
verified and the agent cannot "forget" the cold gate.

Every cold build is serialised behind the per-root build lock (in leancheck.py), so two agents'
gates — or a gate and the warm daemon — never race the same `setup.json`. A build that is merely
WAITING for the lock reports DEFERRED (exit EX_LOCKBUSY), which is treated as "another build is
verifying this tree", NOT as a build failure: a busy lock never blocks a stop.

ORCHESTRATED RUNS: set `LEANCHECK_STOP_GATE=off` to skip the per-stop build entirely, so a workflow
orchestrator can let sub-agents finish on warm feedback alone and run ONE authoritative cold build
itself after the work settles — instead of every sub-agent re-running a near-full build on each stop.

Loop guard: blocks at most MAX_TRIES times per session; after that it allows the stop but
prepends a loud UNVERIFIED banner so the failure is surfaced, never hung.

Appends one line per invocation to the /tmp debug log. `--selftest` runs offline unit tests."""
import sys, os, json, subprocess, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leanmod
import leancheck                                   # for EX_LOCKBUSY (the deferred-gate sentinel)

MAX_TRIES = 6
EX_LOCKBUSY = leancheck.EX_LOCKBUSY
DEBUG = os.environ.get("LEANCHECK_HOOK_LOG", "/tmp/leancheck-hook.log")

# ---------------------------------------------------------------- pure logic (unit-tested)

def gate_disabled(val):
    """True iff LEANCHECK_STOP_GATE asks to SKIP the per-stop cold build (default/empty -> gate ON),
    so an orchestrator can own a single authoritative build instead of every sub-agent re-running it."""
    return (val or "build").strip().lower() in ("off", "none", "no", "0", "false", "disabled")

def classify_result(returncode, lockbusy=EX_LOCKBUSY):
    """A cold-build exit code -> 'ok' (clean) / 'deferred' (lock busy, another build is verifying) /
    'fail' (real build errors)."""
    if returncode == 0:
        return "ok"
    if returncode == lockbusy:
        return "deferred"
    return "fail"

def read_modules(touch):
    """The touched-module list for the cold gate (deduped tokens), or [] if none."""
    if not os.path.exists(touch):
        return []
    return [m for m in open(touch).read().split() if m]

def filter_present(modules, proj):
    """Split the touched modules into (present, skipped) by whether their source still exists.
    Existence is resolved srcDir-aware (a lib with `srcDir = "test"` keeps module `Audit` at
    `test/Audit.lean`), so a real module is never mistaken for a phantom. A throwaway probe module
    that was edited (landing in the touched-list) and then deleted leaves a PHANTOM with no source:
    `lake build` of it fails with 'no source' and would wrongly block the stop (which previously
    forced agents to leave probe files lying around), so we skip such entries (and log them); a real
    module is still always built."""
    present, skipped = [], []
    for m in modules:
        (present if leanmod.module_source(m, proj) else skipped).append(m)
    return present, skipped

def block_reason(failures, tries, max_tries):
    """Decision: return (reason_text_or_None, allow_stop). `failures` are per-module error blocks."""
    if not failures:
        return (None, True)
    body = "\n\n".join(failures)
    if tries <= max_tries:
        return ("Cold `lake build` of your edited module(s) FAILED — you cannot finish yet. "
                "Fix these and continue:\n\n" + body, False)
    return (f"UNVERIFIED after {max_tries} cold-build attempts — report this as a FAILED/open node "
            f"in your final message (do NOT claim success):\n\n" + body, True)

def defer_reason(modules, tries, max_tries):
    """Decision when the cold build was DEFERRED (build lock busy): the module(s) are NOT yet verified,
    so block-and-retry (the next stop re-attempts once the lock frees) rather than passing the gate.
    After max_tries the lock is still busy: allow the stop but loudly mark it UNVERIFIED."""
    mods = " ".join(modules)
    if tries <= max_tries:
        return ("Cold `lake build` was DEFERRED — the build lock is held by another build, so your "
                f"module(s) are NOT yet verified ({mods}). The gate will retry: wait briefly or keep "
                "working, then stop again.", False)
    return (f"UNVERIFIED after {max_tries} attempts — the build lock stayed busy, so these module(s) "
            f"were never cold-built this session: {mods}. Report this as UNVERIFIED (do NOT claim "
            "success).", True)

def read_tries(path):
    """The per-session retry counter, robust to a missing/empty/corrupt file: any unreadable value
    reads as 0 so a bad counter (e.g. an interrupted truncating write leaving it empty, which made
    `int('')` raise) never crashes the Stop hook."""
    try:
        return int(open(path).read().strip())
    except (ValueError, OSError):
        return 0

# ---------------------------------------------------------------- side effects

def dbg(msg):
    try:
        with open(DEBUG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [stop-gate pid={os.getpid()}] {msg}\n")
    except Exception:
        pass

def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    if gate_disabled(os.environ.get("LEANCHECK_STOP_GATE")):
        dbg("stop-gate disabled via LEANCHECK_STOP_GATE -> allow stop (orchestrator owns the cold build)")
        return 0
    session = d.get("session_id", "default")
    # Single-package repo: the Lake package is `$CLAUDE_PROJECT_DIR`. Monorepo: its subdir
    # (LEANCHECK_PROJECT_SUBDIR). project_root resolves both; the cold build + module existence
    # checks below all key off this one `proj`.
    proj = leanmod.project_root(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    leancheck_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leancheck.py")
    modules = read_modules(f"/tmp/leancheck-touched-{session}.txt")
    modules, skipped = filter_present(modules, proj)
    if skipped:                                    # deleted throwaway/probe modules -> never gate on them
        dbg(f"skipped {len(skipped)} touched module(s) with no source: {' '.join(skipped)}")
    if not modules:
        return 0                                   # nothing (still) present to gate

    env = dict(os.environ, LEANCHECK_ROOT=proj)
    # Build ALL touched modules under a SINGLE exclusive lock acquisition (one `lake build mod…`): the
    # build lock is waited for once per stop, not once per module, so the whole gate stays within the
    # Stop-hook timeout even under contention. cold_check DEFERS (not fails) if the lock stays busy.
    r = subprocess.run([sys.executable, leancheck_py, "--cold", *modules], env=env,
                       capture_output=True, text=True)
    kind = classify_result(r.returncode)
    dbg(f"cold-gate: {len(modules)} module(s) -> {kind}")
    if kind == "ok":
        return 0                                   # verified clean -> allow stop

    triesf = f"/tmp/leancheck-tries-{session}.txt"
    tries = read_tries(triesf) + 1
    open(triesf, "w").write(str(tries))
    if kind == "deferred":                         # lock busy -> NOT verified: retry, never a clean pass
        reason, allow = defer_reason(modules, tries, MAX_TRIES)
    else:                                           # real build errors
        reason, allow = block_reason([(r.stdout or "").strip()], tries, MAX_TRIES)
    if not allow:
        print(json.dumps({"decision": "block", "reason": reason}))
        dbg(f"BLOCK stop (try {tries}, {kind})")
    else:
        # exit 0 with the banner on stderr -> stop allowed, but the failure is loudly surfaced
        print(json.dumps({"decision": "block", "reason": reason}), file=sys.stderr)
        dbg(f"gave up after {tries} tries ({kind}) -> allow stop with UNVERIFIED banner")
    return 0

# ---------------------------------------------------------------- offline self-test

def selftest():
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write("MyLib.A\nMyLib.B\n\n"); f.close()
    assert read_modules(f.name) == ["MyLib.A", "MyLib.B"], read_modules(f.name)
    os.remove(f.name)
    assert read_modules("/no/such/file") == []
    # filter_present: a deleted/probe module (no source) is skipped; a real one is kept and gated
    proj = tempfile.mkdtemp()
    os.makedirs(os.path.join(proj, "MyLib", "Sub"))
    open(os.path.join(proj, "MyLib", "Sub", "Real.lean"), "w").close()
    present, skipped = filter_present(["MyLib.Sub.Real", "MyLib.Sub.Ghost"], proj)
    assert present == ["MyLib.Sub.Real"], present
    assert skipped == ["MyLib.Sub.Ghost"], skipped
    assert filter_present([], proj) == ([], [])
    # srcDir-aware: a lib with `srcDir = "test"` keeps module `Audit` at test/Audit.lean (present,
    # not a phantom) — the case that previously made the gate fail on `lake build test.Audit`.
    with open(os.path.join(proj, "lakefile.toml"), "w") as lf:
        lf.write('[[lean_lib]]\nname = "Audit"\nsrcDir = "test"\n')
    os.makedirs(os.path.join(proj, "test"))
    open(os.path.join(proj, "test", "Audit.lean"), "w").close()
    leanmod.src_dirs.cache_clear()
    assert filter_present(["Audit", "Ghost"], proj) == (["Audit"], ["Ghost"])
    assert block_reason([], 1, 6) == (None, True)
    reason, allow = block_reason(["### M\nerr"], 1, 6)
    assert allow is False and "FAILED" in reason and "err" in reason, reason
    reason, allow = block_reason(["### M\nerr"], 7, 6)
    assert allow is True and "UNVERIFIED" in reason, reason
    # gate toggle: default/empty/"build" -> ON; explicit off-ish values -> disabled (orchestrator owns it)
    assert gate_disabled(None) is False and gate_disabled("") is False and gate_disabled("build") is False
    for v in ("off", "OFF", " none ", "0", "false", "no", "disabled"):
        assert gate_disabled(v) is True, v
    # cold-build exit code: 0 -> clean, EX_LOCKBUSY -> deferred (lock busy), anything else -> real failure
    assert classify_result(0) == "ok"
    assert classify_result(EX_LOCKBUSY) == "deferred"
    assert classify_result(1) == "fail" and classify_result(2) == "fail"
    # deferred is NOT a clean pass: block-and-retry until max_tries, then allow with an UNVERIFIED banner
    reason, allow = defer_reason(["MyLib.A", "MyLib.B"], 1, 6)
    assert allow is False and "DEFERRED" in reason and "not yet verified" in reason.lower(), reason
    assert "MyLib.A" in reason and "MyLib.B" in reason, reason
    reason, allow = defer_reason(["MyLib.A"], 7, 6)
    assert allow is True and "UNVERIFIED" in reason, reason
    # retry counter: robust to missing/empty/corrupt files -> 0 (never crash the Stop hook on int(''))
    assert read_tries("/no/such/leancheck-tries") == 0
    tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False); tf.write(""); tf.close()
    assert read_tries(tf.name) == 0                 # empty (interrupted write) -> 0, not ValueError
    open(tf.name, "w").write("  bogus "); assert read_tries(tf.name) == 0   # corrupt -> 0
    open(tf.name, "w").write(" 3\n"); assert read_tries(tf.name) == 3       # valid -> parsed
    os.remove(tf.name)
    print("stop-coldbuild selftest OK")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main() or 0)
