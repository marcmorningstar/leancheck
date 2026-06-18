#!/usr/bin/env python3
"""leancheck — warm Lean diagnostics via the real language server (`lake serve`), with a cold
`lake build` gate. The agent never sees JSON or the LSP protocol: it edits a `.lean` file and
either gets diagnostics for free (PostToolUse hook) or runs `leancheck <file>`.

The engine is `leanclient` (the maintained client `lean-lsp-mcp` is built on), driving one
persistent `lake serve`. `lake serve` owns import resolution, incremental within-file elaboration,
the diagnostics-finalization handshake, stale-import auto-rebuild, and process lifecycle, so this
file is thin plumbing.

WORKTREE-SAFE: the daemon socket key is derived from the (realpath'd) project root, so each
worktree/checkout gets its OWN `lake serve` bound to its own root; workers within one tree share it.
A per-root `flock` makes the daemon a singleton: only one `lake serve` per root can ever bind, so
racing callers never spawn (and orphan) a second multi-GB server. To run genuinely parallel agents
that edit overlapping files, give each its own git worktree: each realpath'd root gets an independent
daemon AND an independent `.lake/build`, so there is no shared build tree to corrupt at all.

SERIALISED: every `lake` writer leancheck DRIVES takes a per-root, writer-priority build lock
(`build_lock`). The cold `lake build` takes it EXCLUSIVE; the daemon's warm checks take it SHARED for
the WHOLE open+elaborate window (a `didOpen` is fire-and-forget — `lake serve` writes the import
closure's `setup.json`/oleans asynchronously while `get_diagnostics` waits — so the lock must span the
wait, not just the send). So a cold build never overlaps another build or a warm elaboration — the race
that truncated `setup.json` on the WSL2/9p mount — while concurrent warm checks still run in parallel,
and a stream of warm opens can't starve the cold build (the turnstile gives the writer priority). The
lock lives on tmpfs (next to the socket), so coordination is reliable even on 9p/drvfs. NOTE this
covers only `lake` that leancheck runs: a `lake build`/`lake serve` you start yourself takes no lock —
stop the daemon first (`leancheck --stop`) or use a separate worktree.

MATHLIB GUARD: if Mathlib's oleans are absent (so a build/serve would recompile Mathlib from source
— HOURS), every entry point ABORTS with a loud warning instead of silently starting the rebuild,
unless `LEANCHECK_ALLOW_MATHLIB_REBUILD=1`.

Modes
-----
  leancheck <file.lean>        warm diagnostics (NON-BLOCKING; a cold file warms in the background)
  leancheck --cold <file|mod>  authoritative `lake build` of the module (the QA gate)
  leancheck --warm [file]      start the daemon (with a file, also start warming it)
  leancheck --stop             stop the daemon (kills `lake serve` + its `lean --server` child)
  leancheck --check-mathlib    report whether Mathlib is built (exit 1 + warning if not)
  leancheck --daemon           (internal) the long-lived server host
  leancheck --selftest         offline unit tests of the pure logic

Config (env): LEANCHECK_ROOT [cwd], LEANCHECK_KEY [derived from root], LEANCHECK_SOCKDIR [/tmp],
              LEANCHECK_MAXFILES [8], LEANCHECK_WARM_MAX [240], LEANCHECK_RECHECK_MAX [55],
              LEANCHECK_BUILD_LOCK_WAIT [570], LEANCHECK_HOOK_LOG [/tmp/leancheck-hook.log],
              LEANCHECK_ALLOW_MATHLIB_REBUILD [unset].

Cross-file note: `lake serve` resolves imports from `.olean`, so a file's check reflects its OWN
current source but sees dependencies as last built — a changed dependency must be rebuilt to be
visible. Each warm re-check re-syncs the file's CURRENT on-disk content into the server (didChange)
before reading diagnostics, so a re-check never returns stale results for the edited file. The cold
`lake build` is the source of truth.
"""
import sys, os, json, socket, subprocess, time, argparse, re, threading, hashlib, contextlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leanmod
try:
    import fcntl                                   # POSIX advisory locks (Linux/macOS); absent on Windows
except ImportError:                               # pragma: no cover - non-POSIX: locking degrades to no-op
    fcntl = None

ROOT = os.path.realpath(os.environ.get("LEANCHECK_ROOT", os.getcwd()))
KEY = os.environ.get("LEANCHECK_KEY") or ("leancheck-" + hashlib.sha1(ROOT.encode()).hexdigest()[:8])
SOCK = os.path.join(os.environ.get("LEANCHECK_SOCKDIR", "/tmp"), f"leancheck-{KEY}.sock")
LOCKFILE = SOCK + ".lock"                       # flock target: makes the daemon a per-root singleton
PIDFILE = SOCK + ".pid"                          # records daemon + lake-serve pids for orphan reaping
BUILDLOCK = SOCK + ".build.lock"                # flock target: serialises every lake writer of .lake/build
MAXFILES = int(os.environ.get("LEANCHECK_MAXFILES", "8"))
WARM_MAX = float(os.environ.get("LEANCHECK_WARM_MAX", "240"))      # first-open ceiling (background)
RECHECK_MAX = float(os.environ.get("LEANCHECK_RECHECK_MAX", "55")) # bounded under the 80s hook budget
# How long a cold `lake build` waits for the build lock before deferring. Kept >= WARM_MAX so a single
# in-flight warm open always drains within it (no false defer), and well under the 600s Stop-hook budget
# so the wait + an incremental build fit. On contention it queues and DEFERS — it never fails.
COLD_LOCK_WAIT = float(os.environ.get("LEANCHECK_BUILD_LOCK_WAIT", "300"))
ALLOW_REBUILD = os.environ.get("LEANCHECK_ALLOW_MATHLIB_REBUILD") == "1"
EX_LOCKBUSY = 75   # cold-build exit code meaning "build lock held by another build; deferred, NOT a failure"
# prevent leanclient/`lake serve` from auto-fetching the Mathlib cache on startup (default on:
# the plugin assumes the project is already built). Set LEANCHECK_PREVENT_CACHE_GET=0 to allow it.
PREVENT_CACHE_GET = os.environ.get("LEANCHECK_PREVENT_CACHE_GET", "1") != "0"

# ---------------------------------------------------------------- build lock (the corruption fix)

class BuildLockTimeout(Exception):
    """Raised when the build lock could not be acquired within the caller's budget. Carries the
    seconds waited so callers can report a DEFERRED gate — never a build failure."""
    def __init__(self, waited):
        super().__init__(f"build lock not acquired after {waited:.0f}s")
        self.waited = waited

@contextlib.contextmanager
def build_lock(exclusive, timeout=None, on_wait=None, path=None):
    """Serialise every process that writes this root's `.lake/build` tree, so two `lake` writers can
    never race the same per-module `setup.json`/olean (the documented WSL2/9p corruption).

    A WRITER-PRIORITY readers-writers lock over TWO flock files (a turnstile), so a stream of warm
    SHARED opens can never starve the cold build's EXCLUSIVE acquire:
      * the cold `lake build` takes it EXCLUSIVE (full-tree rewrite — must run alone);
      * the daemon's file-opens, whose `lake setup-file` writes a module's setup.json, take it SHARED
        (concurrent with each other — preserving warm-check parallelism — but exclusive vs a cold build).
    The GATE file (`.gate`) is the turnstile: a writer holds it EXCLUSIVE for its whole critical section,
    which keeps NEW readers out; a reader holds it SHARED only briefly — just long enough to enter the
    data lock — so the writer waits for readers at the gate (microseconds), not for their elaboration.
    The DATA file is the tree lock: readers SHARED (concurrent), the writer EXCLUSIVE (alone), and the
    writer's EXCLUSIVE acquire drains the readers already inside.

    The lock files live next to the socket (default `/tmp`, a native tmpfs), NOT on the fragile project
    mount, so coordination is reliable even when the data it guards is on 9p/drvfs. A fresh fd per call
    gives correct exclusion across threads (distinct open file descriptions) and processes. On
    contention it polls (non-blocking flock + sleep) to honour `timeout`/`on_wait`; exceeding `timeout`
    raises BuildLockTimeout rather than blocking forever. With no `fcntl` (non-POSIX) it is a no-op so
    the tool still runs, just unserialised."""
    if fcntl is None:
        yield
        return
    base = path or BUILDLOCK
    t0 = time.monotonic()
    warned = [False]

    def _acquire(fd, lock_mode):
        while True:
            try:
                fcntl.flock(fd, lock_mode | fcntl.LOCK_NB)
                return
            except OSError:
                waited = time.monotonic() - t0
                if timeout is not None and waited >= timeout:
                    raise BuildLockTimeout(waited)
                if on_wait is not None and not warned[0]:
                    on_wait(); warned[0] = True
                time.sleep(0.2)

    def _release(fd):
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()

    gate = data = None
    try:
        gate = open(base + ".gate", "w")
        data = open(base, "w")
        if exclusive:
            _acquire(gate, fcntl.LOCK_EX)       # hold the turnstile shut: no NEW reader may enter
            _acquire(data, fcntl.LOCK_EX)        # drain readers already inside, then own the tree alone
            yield
        else:
            _acquire(gate, fcntl.LOCK_SH)        # pass the turnstile (a pending writer holds it EX)
            _acquire(data, fcntl.LOCK_SH)        # shared with peer readers
            _release(gate); gate = None          # leave the turnstile open for peers; keep the data lock
            yield
    finally:
        _release(data)
        _release(gate)

# ---------------------------------------------------------------- Mathlib-rebuild guard

def mathlib_built(root):
    """True iff Mathlib's oleans are present, i.e. a `lake build`/`lake serve` will NOT recompile
    Mathlib from source (a multi-hour operation). Detects the missing-cache case: a fresh checkout,
    or a worktree without the prebuilt `.lake` cache (or its symlink)."""
    pkg = os.path.join(root, ".lake", "packages", "mathlib")
    if not os.path.isdir(pkg):
        return True                                                  # no Mathlib dep -> no rebuild risk
    base = os.path.join(pkg, ".lake", "build", "lib")
    for cand in ("lean/Mathlib/Init.olean", "Mathlib/Init.olean"):   # pinned-toolchain layout first
        if os.path.exists(os.path.join(base, cand)):
            return True
    if os.path.isdir(base):                                          # fallback: any Mathlib olean
        for _dp, _dn, fs in os.walk(base):
            if any(f.endswith(".olean") for f in fs):
                return True
    return False

MATHLIB_WARNING = (
    "================ ⚠️  SERIOUS WARNING: MATHLIB IS NOT BUILT ================\n"
    "Mathlib's compiled oleans are missing under .lake/packages/mathlib, so a `lake build` or\n"
    "`lake serve` here would COMPILE MATHLIB FROM SOURCE — HOURS of CPU, not a quick check.\n"
    "  * On a brand-new checkout this is expected: fetch the prebuilt cache first.\n"
    "  * In a WORKTREE it usually means the prebuilt `.lake` cache (or its symlink) is missing.\n"
    "leancheck ABORTED rather than silently start a multi-hour rebuild. Decide explicitly:\n"
    "  - cancel, fix the cache/symlink, and retry; OR\n"
    "  - accept the from-scratch rebuild by re-running with  LEANCHECK_ALLOW_MATHLIB_REBUILD=1\n"
    "==================================================================================")

def mathlib_guard():
    """Warning text if a from-scratch Mathlib rebuild is imminent and not opted into, else None."""
    if ALLOW_REBUILD or mathlib_built(ROOT):
        return None
    return MATHLIB_WARNING

# ---------------------------------------------------------------- pure logic (unit-tested)

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}

def format_diagnostics(relpath, diagnostics):
    """LSP diagnostics (list of {range,severity,message}) -> (compiler-style text, n_errors).
    LSP positions are 0-based; we emit 1-based line:col. Messages are first-line-only."""
    out, n_err = [], 0
    for d in diagnostics or []:
        start = (d.get("range") or {}).get("start") or {}
        line = start.get("line", 0) + 1
        col = start.get("character", 0) + 1
        sev = SEVERITY.get(d.get("severity", 1), "info")
        msg = (d.get("message", "") or "").strip().split("\n", 1)[0]
        if sev == "error":
            n_err += 1
        out.append(f"{relpath}:{line}:{col}: {sev}: {msg}")
    if not out:
        out.append("✓ no errors")
    return "\n".join(out), n_err

# ---------------------------------------------------------------- the language-server daemon

class Engine:
    """Owns one persistent `lake serve` (via leanclient) and warms cold files in the background so a
    check never blocks: the first open elaborates a file; until then a check returns "warming";
    afterwards re-checks re-sync the file from disk (didChange) and read fresh diagnostics fast.

    No global server lock: leanclient is internally thread-safe (its diagnostics wait releases the
    file lock on a condition variable while a dedicated reader thread updates state), so concurrent
    checks of different files interleave. A small lock guards only the warming/ready bookkeeping."""
    def __init__(self):
        from leanclient import LeanLSPClient            # imported lazily: only the daemon needs it
        self.client = LeanLSPClient(ROOT, initial_build=False, prevent_cache_get=PREVENT_CACHE_GET,
                                    max_opened_files=MAXFILES)
        self.slock = threading.Lock()                   # guards the warming/ready/deferred sets + lock table
        self.ready = set()
        self.warming = set()
        self.deferred = set()                           # files whose warm was deferred by a cold build
        self.closing = False
        self.locks = {}                                 # per-file locks (created lazily under slock)

    def lakeserve_pid(self):
        try:
            return self.client.process.pid
        except Exception:
            return None

    def _filelock(self, rel):
        """A per-file lock so two concurrent rechecks of the SAME file can't interleave leanclient's
        read-disk / compute-change / apply-change sync — which would apply a change whose end-range
        was computed from a now-stale line count and garble the server's view of the file. Distinct
        files keep distinct locks, so they still elaborate concurrently (no head-of-line block)."""
        with self.slock:
            lk = self.locks.get(rel)
            if lk is None:
                lk = self.locks[rel] = threading.Lock()
            return lk

    def _warm(self, rel):
        # The build lock (SHARED) is held across BOTH open_files AND get_diagnostics: open_files only
        # SENDS a fire-and-forget didOpen, and the `lake serve` process writes the import closure's
        # setup.json/oleans ASYNCHRONOUSLY while get_diagnostics waits for elaboration to finish. The
        # lock must span that whole window (incl. leanclient's stale-import auto-rebuild, which also
        # fires during the wait) or a cold build could still race those writes. `ready` is set ONLY
        # after a real elaboration, so a lock-timeout / open error re-warms next time (never a no-op
        # "ready"). A deferral by a cold build is remembered so check() can explain it.
        ready, deferred = False, False
        try:
            with self._filelock(rel):
                with build_lock(exclusive=False, timeout=WARM_MAX):
                    self.client.open_files([rel])       # first open: load import closure + elaborate
                    self.client.get_diagnostics(rel, inactivity_timeout=15.0, max_timeout=WARM_MAX)
            ready = True                                 # reached only after elaboration actually completed
        except BuildLockTimeout:
            deferred = True                             # a cold build held the lock: re-warm on the next check
        except Exception:
            pass
        with self.slock:
            self.warming.discard(rel)
            if ready:
                self.ready.add(rel); self.deferred.discard(rel)
            elif deferred:
                self.deferred.add(rel)

    def check(self, rel):
        base = os.path.basename(rel)
        if self.closing:
            return "leancheck: daemon shutting down; rely on the cold build."
        with self.slock:
            if rel in self.warming:
                return f"leancheck: still warming {base}; diagnostics shortly."
            first = rel not in self.ready
            was_deferred = rel in self.deferred
            if first:
                self.warming.add(rel)
        if first:
            threading.Thread(target=self._warm, args=(rel,), daemon=True).start()
            if was_deferred:                            # the previous warm was blocked by a cold build
                return (f"leancheck: a cold `lake build` is in progress; warming of {base} is deferred "
                        f"until it finishes — diagnostics appear on a later edit. The cold build is "
                        f"authoritative.")
            return (f"leancheck: warming {base} in the Lean server (first open of a file takes a "
                    f"moment); diagnostics appear on your next edit. The cold `lake build` Stop gate "
                    f"remains authoritative.")
        # Ready: re-sync the file's CURRENT on-disk content into the server (didChange if changed),
        # then read fresh diagnostics. The build lock (SHARED) spans open_files AND get_diagnostics —
        # the server writes setup.json/oleans asynchronously across that whole window — so a cold build
        # never overlaps it. Without the re-sync an already-open file returns its first-open diagnostics,
        # i.e. stale results that ignore the agent's later edits.
        try:
            with self._filelock(rel):
                with build_lock(exclusive=False, timeout=RECHECK_MAX):  # yield to an in-flight cold build
                    self.client.open_files([rel])
                    res = self.client.get_diagnostics(rel, inactivity_timeout=8.0, max_timeout=RECHECK_MAX)
        except BuildLockTimeout:
            return (f"leancheck: a cold `lake build` is in progress; warm recheck of {base} "
                    f"deferred — rely on the cold build.")
        except Exception as e:
            return f"leancheck: recheck error for {base}: {e}; rely on the cold build."
        text, _ = format_diagnostics(rel, getattr(res, "diagnostics", []))
        if getattr(res, "timed_out", False):
            text += (f"\n(leancheck: diagnostics wait timed out at {RECHECK_MAX:.0f}s — report may be "
                     f"incomplete; the cold `lake build` is authoritative)")
        return text

    def close(self):
        self.closing = True
        try:
            self.client.close()                         # leanclient kills lake serve + lean --server
        except Exception:
            pass

# ---------------------------------------------------------------- process / liveness helpers

def _unix_sock():
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

def _daemon_alive():
    """Liveness by an ACTUAL connection, not mere socket-file existence: a socket left by a
    crashed/SIGKILLed daemon exists on disk but refuses connections (stale-socket safe)."""
    if not os.path.exists(SOCK):
        return False
    try:
        c = _unix_sock(); c.settimeout(2.0); c.connect(SOCK); c.close()
        return True
    except OSError:
        return False

def _proc_ctime(pid):
    """Process create-time (epoch secs) for `pid`, or None — a PID-reuse-safe identity stamp."""
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None

def _reap_orphan_lakeserve():
    """Called right after winning the singleton lock — which proves any daemon recorded in PIDFILE
    is already dead. Kill its orphaned `lake serve` (and children) if still alive, covering a
    SIGKILLed/OOM-killed/crashed predecessor that could not run its own cleanup. Identity is matched
    by the recorded create-time, NOT argv: the project path here contains the substring "lean", so a
    cmdline check would match almost any recycled pid — create-time is the reliable PID-reuse guard."""
    try:
        with open(PIDFILE) as f:
            rec = json.load(f)
    except Exception:
        return
    spid = rec.get("serve")
    if not spid:
        return
    try:
        import psutil
        p = psutil.Process(spid)
        sct = rec.get("serve_ctime")
        if sct is not None:
            same = abs(p.create_time() - sct) < 1.0            # exact process, not a recycled pid
        else:                                                   # legacy pidfile: fall back to argv[0]
            argv = p.cmdline()
            same = bool(argv) and os.path.basename(argv[0]) in ("lake", "lean")
        if same:
            for c in p.children(recursive=True):
                try: c.kill()
                except Exception: pass
            p.kill()
    except Exception:
        pass

_LOCKF = None   # the held-open flock fd; kept alive for the daemon's whole lifetime

def daemon():
    import atexit, signal, fcntl
    global _LOCKF
    # Singleton: only one daemon per project root may proceed. A loser exits at the lock — BEFORE
    # constructing an Engine or touching the socket — so it can never delete a live daemon's socket
    # or orphan a second lake serve (the TOCTOU double-spawn that leaked multi-GB servers).
    _LOCKF = open(LOCKFILE, "w")
    try:
        fcntl.flock(_LOCKF, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os._exit(0)
    _reap_orphan_lakeserve()                    # winning the lock => predecessor is dead; reap its server
    eng = Engine()                              # starts lake serve (the first FILE open is the slow part)

    def _cleanup(*_):
        eng.close()
        for p in (SOCK, PIDFILE):
            try: os.remove(p)
            except OSError: pass

    atexit.register(_cleanup)
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: (_cleanup(), os._exit(0)))
    if os.path.exists(SOCK):
        os.remove(SOCK)
    srv = _unix_sock(); srv.bind(SOCK); srv.listen(64)
    try:
        spid = eng.lakeserve_pid()
        with open(PIDFILE, "w") as f:
            json.dump({"daemon": os.getpid(), "serve": spid, "serve_ctime": _proc_ctime(spid)}, f)
    except Exception:
        pass
    # One thread per connection: a slow check (warm/recheck) never stalls the accept loop or other
    # in-flight checks (the single-threaded inline loop was the head-of-line block).
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        threading.Thread(target=_serve_conn, args=(conn, eng), daemon=True).start()

def _serve_conn(conn, eng):
    try:
        req = json.loads(_recv_all(conn) or "{}")
        f = req.get("file")
        if f in (None, "", "__ping__"):                 # liveness probe (bare connect / ping)
            try: conn.sendall(b"pong")
            except OSError: pass
            return
        if f == "__stop__":
            try: conn.sendall(b"stopping")
            except OSError: pass
            conn.close()
            eng.close()
            for p in (SOCK, PIDFILE):
                try: os.remove(p)
                except OSError: pass
            os._exit(0)
        rel = os.path.relpath(os.path.abspath(f), ROOT)
        conn.sendall(eng.check(rel).encode())
    except Exception as e:
        try: conn.sendall(f"leancheck daemon error: {e}".encode())
        except OSError: pass
    finally:
        try: conn.close()
        except OSError: pass

# ---------------------------------------------------------------- client / CLI

def _recv_all(conn):
    chunks = []
    while True:
        b = conn.recv(65536)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks).decode()

def ensure_daemon():
    if _daemon_alive():
        return
    # Do NOT unlink the socket from here: only the flock-winning daemon may remove a stale socket
    # (it does so before bind, while holding the lock), so a client can never race-unlink a LIVE
    # daemon's socket — which would wedge the channel and leak its lake serve.
    g = mathlib_guard()
    if g:
        raise SystemExit(g)                   # backstop: never start lake serve on an unbuilt tree
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--daemon"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=ROOT)
    for _ in range(900):                      # readiness = a real successful connection (~90s budget)
        if _daemon_alive():
            return
        time.sleep(0.1)
    raise SystemExit("leancheck: daemon did not come up")

def leanclient_available():
    import importlib.util
    return importlib.util.find_spec("leanclient") is not None

WARM_UNAVAILABLE = ("leancheck: warm feedback unavailable — run `pip install leanclient` to enable\n"
                    "it. The cold `lake build` gate still applies.")

def warm_check(path):
    g = mathlib_guard()
    if g:
        print(g); return 1
    if not leanclient_available():
        print(WARM_UNAVAILABLE); return 0
    ensure_daemon()
    out = "leancheck: daemon unreachable; rely on the cold build."
    for attempt in (1, 2):                    # one respawn retry if the daemon died/staled mid-call
        try:
            c = _unix_sock(); c.settimeout(120.0); c.connect(SOCK)
            c.sendall(json.dumps({"file": os.path.abspath(path)}).encode()); c.shutdown(socket.SHUT_WR)
            out = _recv_all(c); c.close()
            break
        except OSError:
            # Daemon unreachable: let ensure_daemon probe and respawn only if it is truly dead.
            # Never unlink the socket from the client side (could race-kill a live daemon's socket).
            if attempt == 1:
                ensure_daemon()
    print(out)
    return 1 if re.search(r": error:", out or "") else 0

def stop_daemon():
    import signal
    if os.path.exists(SOCK):
        try:
            c = _unix_sock(); c.settimeout(10.0); c.connect(SOCK)
            c.sendall(b'{"file":"__stop__"}'); _recv_all(c); c.close()
        except OSError:
            pass
    elif os.path.exists(PIDFILE):             # socket gone but the daemon may still live: signal it
        try:
            with open(PIDFILE) as f:
                rec = json.load(f)
            dpid = rec.get("daemon")
            if dpid:
                os.kill(dpid, signal.SIGTERM)
        except Exception:
            pass
    for p in (SOCK, PIDFILE):
        if os.path.exists(p):
            try: os.remove(p)
            except OSError: pass
    return 0

def module_of(target):
    if not target.endswith(".lean"):
        return target                              # already a module name
    return leanmod.file_to_module(target, ROOT)    # srcDir-aware (honours each lib's `srcDir`)

def cold_check(targets):
    """Cold-build one or more modules under a SINGLE exclusive build-lock acquisition (one `lake build
    mod…` invocation), so the lock is waited for once per gate — not once per module — and the whole
    cold gate fits the Stop-hook budget. On lock contention it DEFERS (EX_LOCKBUSY), never fails."""
    g = mathlib_guard()
    if g:
        print(g); return 1
    if isinstance(targets, str):
        targets = [targets]
    mods = [module_of(t) for t in targets]
    def _waiting():
        print(f"leancheck: the build tree is busy (another build, or a warm file-open); waiting for "
              f"the build lock before building {' '.join(mods)}…", file=sys.stderr)
    try:
        # EXCLUSIVE: a full-tree build must be the only lake writer. On contention it queues (up to
        # COLD_LOCK_WAIT) with writer priority; a timeout means another build still holds the tree —
        # report DEFERRED (EX_LOCKBUSY), never a build failure, so the gate can't mistake "busy" for
        # "broken". DEFERRED is NOT "verified": the caller must retry, not treat it as a clean pass.
        with build_lock(exclusive=True, timeout=COLD_LOCK_WAIT, on_wait=_waiting):
            r = subprocess.run(["lake", "build", *mods], cwd=ROOT, capture_output=True, text=True)
    except BuildLockTimeout as e:
        print(f"leancheck: the build lock stayed busy for >{e.waited:.0f}s (another build or warm "
              f"file-open); DEFERRED the cold gate for {' '.join(mods)} — not yet verified, not a failure.")
        return EX_LOCKBUSY
    diags = [l for l in (r.stdout + r.stderr).split("\n") if re.search(r"error:|warning:", l)]
    print("\n".join(diags) if diags else "✓ cold build clean")
    return r.returncode

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*")             # one file/module for warm; one OR MORE for --cold
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--warm", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--check-mathlib", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check_mathlib:
        if mathlib_built(ROOT):
            print("✓ Mathlib oleans present (a build/serve will NOT recompile Mathlib)"); return 0
        print(MATHLIB_WARNING); return 1
    if a.daemon:
        return daemon()
    if a.stop:
        return stop_daemon()
    if a.warm:
        g = mathlib_guard()
        if g:
            print(g); return 1
        if not leanclient_available():
            print(WARM_UNAVAILABLE); return 0
        ensure_daemon()
        if a.targets:
            return warm_check(a.targets[0])
        print("warm"); return 0
    if not a.targets:
        ap.error("need a file/module (or a mode flag)")
    return cold_check(a.targets) if a.cold else warm_check(a.targets[0])

# ---------------------------------------------------------------- offline self-test

def selftest():
    import tempfile
    diags = [
        {"range": {"start": {"line": 158, "character": 0}}, "severity": 1,
         "message": "Not a definitional equality\n  detail"},
        {"range": {"start": {"line": 3, "character": 7}}, "severity": 2,
         "message": "declaration uses 'sorry'"},
    ]
    text, nerr = format_diagnostics("MyLib/Sub/Foo.lean", diags)
    assert nerr == 1, nerr
    assert "Foo.lean:159:1: error: Not a definitional equality" in text, text
    assert "detail" not in text, "first-line-only"
    assert "Foo.lean:4:8: warning: declaration uses 'sorry'" in text, text
    clean, n0 = format_diagnostics("T.lean", [])
    assert n0 == 0 and clean == "✓ no errors", clean
    # Mathlib guard: a project WITHOUT a Mathlib dependency never triggers the rebuild guard
    d = tempfile.mkdtemp()
    assert mathlib_built(d) is True, "no Mathlib dependency -> no from-source rebuild risk"
    # ...but Mathlib present with NO oleans would recompile from source -> guard fires
    d2 = tempfile.mkdtemp()
    os.makedirs(os.path.join(d2, ".lake", "packages", "mathlib"))
    assert mathlib_built(d2) is False, "Mathlib present but unbuilt must read as not-built"
    # Per-root key derivation is deterministic and path-sensitive (worktree-safe)
    k = lambda p: "leancheck-" + hashlib.sha1(p.encode()).hexdigest()[:8]
    assert k("/repo/a") != k("/repo/b"), "different roots must yield different daemon keys"
    assert k("/repo/a") == k("/repo/a"), "same root must yield the same key"
    # Daemon-control files are derived from the socket path (singleton lock + orphan-reap pidfile)
    assert LOCKFILE == SOCK + ".lock" and PIDFILE == SOCK + ".pid", (LOCKFILE, PIDFILE)
    assert BUILDLOCK == SOCK + ".build.lock", BUILDLOCK
    # build_lock: a readers-writers flock serialising cold builds against warm opens and each other.
    if fcntl is not None:
        lp = os.path.join(tempfile.mkdtemp(), "b.lock")
        with build_lock(False, timeout=2, path=lp):           # two SHARED holders coexist (warm parallelism)
            with build_lock(False, timeout=2, path=lp):
                pass
        held = threading.Event(); release = threading.Event()
        def _holder():
            with build_lock(True, timeout=5, path=lp):         # take EXCLUSIVE and hold it
                held.set(); release.wait(5)
        th = threading.Thread(target=_holder); th.start()
        assert held.wait(5), "holder failed to take the exclusive lock"
        for excl in (True, False):                             # neither EX nor SH may enter while EX held
            try:
                with build_lock(excl, timeout=0.4, path=lp):
                    raise AssertionError(f"acquired {'EX' if excl else 'SH'} while EX held")
            except BuildLockTimeout as e:
                assert e.waited >= 0.4, e.waited
        release.set(); th.join(5)
        with build_lock(True, timeout=2, path=lp):             # lock is free again after the holder released
            pass
        # WRITER PRIORITY: a continuous stream of SHARED holders must NOT starve an EXCLUSIVE acquire
        # (the turnstile). Without it, an LOCK_NB poll would be starved indefinitely by overlapping SH.
        stop_churn = threading.Event()
        def _churn():
            while not stop_churn.is_set():
                try:
                    with build_lock(False, timeout=5, path=lp):
                        time.sleep(0.01)
                except BuildLockTimeout:
                    pass
        churns = [threading.Thread(target=_churn) for _ in range(6)]
        for t in churns:
            t.start()
        time.sleep(0.3)                                        # let the readers saturate the lock
        t1 = time.monotonic()
        with build_lock(True, timeout=10, path=lp):            # must win despite the churn
            got = time.monotonic() - t1
        stop_churn.set()
        for t in churns:
            t.join(5)
        assert got < 8, f"exclusive acquire starved by shared churn: waited {got:.1f}s"
    print("leancheck selftest OK")
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
