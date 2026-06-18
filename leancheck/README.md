# leancheck — warm Lean feedback (`lake serve`) + cold-build gate

A Claude Code **plugin** that gives an agent automatic, per-edit Lean 4 diagnostics by driving the
**real Lean language server** (`lake serve`) — the same engine every Lean editor uses — and enforces
an authoritative cold `lake build` before the agent is allowed to finish. The agent just writes Lean
and reads compiler-style errors; it never sees JSON or the LSP protocol.

Works with any Lean 4 + Lake project. It is **Mathlib-aware** (it refuses to silently trigger a
multi-hour from-source Mathlib recompile) but does **not** require Mathlib.

## What you get

- **Per-edit diagnostics.** After every `Edit`/`Write`/`MultiEdit` of a project `.lean` file, the
  agent receives compiler-style `file:line:col: error/warning: …` diagnostics as context — for free.
- **Warm and fast.** One persistent `lake serve` loads the project's `.olean` closure once; the first
  open of a file elaborates it (tens of seconds for a Mathlib-heavy file), and every re-check after
  that is ~instant incremental elaboration that reflects your latest on-disk edit.
- **Never blocks.** A check on a not-yet-warm file returns a "warming" note immediately and elaborates
  on a background thread; the real diagnostics arrive on the next edit. The hook never hangs.
- **Authoritative cold gate.** Before the agent stops, every Lean module it touched this session is
  cold-built with `lake build`; if any fails, the stop is blocked and the errors are handed back so
  the agent must keep working. The warm feedback is for speed; the cold build is the source of truth.
- **Concurrency-safe builds.** Every `lake` build leancheck drives is serialised behind a per-root,
  writer-priority lock, so a cold build never races the warm daemon (or another leancheck build) over
  the same `setup.json`/`.olean` — the data race that can truncate build artifacts (notably on WSL2
  9p/drvfs mounts). Concurrent *warm* checks still run in parallel; only the full-tree cold build runs
  alone. (A `lake` you run yourself takes no lock — see [Concurrency & parallel agents](#concurrency--parallel-agents).)
  For genuinely parallel agents that edit overlapping files, give each its own git worktree (each gets
  an independent daemon **and** build tree).

## Install

```sh
# add this repo as a plugin marketplace, then install the plugin
claude plugin marketplace add marcmorningstar/leancheck
claude plugin install leancheck@lean-tools
```

or interactively inside Claude Code: `/plugin marketplace add marcmorningstar/leancheck` then
`/plugin install leancheck@lean-tools`.

Verify it registered: `claude plugin list` (look for `leancheck@lean-tools`, enabled).

## Requirements

- **A built Lake project.** `lake serve` resolves `import`s from compiled `.olean`, so the project
  must already be built (run `lake build`, and for Mathlib projects fetch the cache with
  `lake exe cache get`). leancheck reflects a file's own current source but sees its dependencies as
  last built.
- **`python3`** on `PATH` (the hooks are Python/bash).
- **`leanclient`** (pip) — only for the *warm* feedback:
  ```sh
  pip install leanclient        # or: pip install --user leanclient
  ```
  `leanclient` is the maintained Lean LSP client (the same one `lean-lsp-mcp` is built on); it owns
  import resolution, incremental elaboration, the diagnostics-finalization handshake, and
  `lean --server` lifecycle, so this plugin is thin plumbing, not a re-implementation of a checker.

  **Graceful degradation:** if `leanclient` is not installed, warm feedback is disabled with a clear
  one-line note and the **cold `lake build` stop-gate still works** (it has no Python dependencies).

## Hooks (what the plugin wires)

| Event | Script | Role |
|---|---|---|
| `SessionStart` | `scripts/warm-leancheck.sh` | start the `lake serve` daemon (one per project root) in the background |
| `PostToolUse` (`Edit\|Write\|MultiEdit`) | `scripts/post-edit-leancheck.py` | warm-check the edited `.lean`, return diagnostics as context, record the module for the cold gate |
| `Stop` | `scripts/stop-coldbuild.py` | cold-build every touched module (serialised behind the per-root build lock); block the stop on failure (loop-guarded; gives up after 6 tries with a loud UNVERIFIED banner). Set `LEANCHECK_STOP_GATE=off` to let an orchestrator own one final build instead. |

The engine + CLI is `scripts/leancheck.py`:

```sh
leancheck <file.lean>        # warm diagnostics (non-blocking; a cold file warms in the background)
leancheck --cold <file|mod>  # authoritative `lake build` of the module (the gate)
leancheck --warm [file]      # start the daemon (with a file, also begin warming it)
leancheck --stop             # stop the daemon (kills lake serve + its lean --server child)
leancheck --check-mathlib    # report whether Mathlib is built (exit 1 + warning if a rebuild looms)
leancheck --selftest         # offline unit tests of the pure logic
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LEANCHECK_INCLUDE` | *(unset)* | restrict per-edit checks to files whose path contains this substring (e.g. `MyLib` to skip scratch files). Unset = every project `.lean` outside `.lake/`. |
| `LEANCHECK_ROOT` | `cwd` / `$CLAUDE_PROJECT_DIR` | the project root the daemon binds to. |
| `LEANCHECK_PREVENT_CACHE_GET` | `1` | keep `lake serve` from auto-fetching the Mathlib cache on startup (the plugin assumes the project is already built). Set `0` to allow it. |
| `LEANCHECK_ALLOW_MATHLIB_REBUILD` | *(unset)* | set `1` to opt past the Mathlib-rebuild guard and accept a from-scratch compile. |
| `LEANCHECK_STOP_GATE` | `build` | the Stop cold-build gate. `off`/`none`/`0`/`false` disables it, so an orchestrator can run one authoritative build itself instead of every sub-agent re-building on each stop. |
| `LEANCHECK_BUILD_LOCK_WAIT` | `300` | seconds a cold build waits for the per-root build lock before *deferring* (never failing) — ≥ `LEANCHECK_WARM_MAX` so one in-flight warm always drains, and well under the 600 s Stop-hook budget. |
| `LEANCHECK_WARM_MAX` | `240` | background first-open ceiling (seconds). |
| `LEANCHECK_RECHECK_MAX` | `55` | re-check diagnostics-wait ceiling (kept under the post-edit hook budget). |
| `LEANCHECK_MAXFILES` | `8` | max files held open in the server before idle ones are closed. |
| `LEANCHECK_SOCKDIR` | `/tmp` | directory for the daemon's per-root `<key>.sock` / `.lock` / `.pid`. |
| `LEANCHECK_HOOK_LOG` | `/tmp/leancheck-hook.log` | hook debug log. |

## Worktree-safe & robust

The daemon socket key is derived from the realpath'd project root, so each worktree/checkout gets its
**own** `lake serve` while all callers within one tree share it. A per-root `flock` makes the daemon a
singleton (no double-spawn of a multi-GB server), liveness is probed by an actual socket connection
(stale sockets from a crashed daemon are swept and the daemon respawned), and a pidfile lets a fresh
daemon reap a predecessor's orphaned `lake serve`.

## Concurrency & parallel agents

Lake keeps **no** cross-invocation build lock, so two `lake` processes building/serving the same tree
can write the same per-module `setup.json`/`.olean` at once. On a fragile mount (WSL2 9p/drvfs) that
race can truncate an artifact — `failed to load header … unexpected end of input` — on a file you
never touched. leancheck closes this:

- **A per-root, writer-priority build lock** (a `flock`, kept on tmpfs next to the socket so it's
  reliable even when the project tree is on 9p). The cold `lake build` takes it **exclusive** — a
  full-tree rewrite must run alone; the daemon's warm checks take it **shared** — concurrent with each
  other, so warm-check parallelism is preserved, but mutually exclusive with any cold build.
- **The shared lock spans the whole open+elaborate window**, not just the open. A `didOpen` is
  fire-and-forget: `lake serve` writes the import closure's `setup.json`/oleans *asynchronously* while
  `get_diagnostics` waits for elaboration (this also covers leanclient's stale-import auto-rebuild,
  which fires during that wait). Holding the lock across the wait is what actually overlaps-excludes
  those writes from a cold build.
- **A turnstile gives the writer priority**, so a stream of warm opens can't starve the cold build:
  while a cold build is waiting, new warm opens queue behind it instead of jumping ahead.
- **Waiting is never failure.** A cold build that can't get the lock immediately *queues* (up to
  `LEANCHECK_BUILD_LOCK_WAIT`, default 300 s) and logs that it's waiting; if it ever times out it
  reports the gate as **deferred** — distinct from a build error. The Stop gate treats *deferred* as
  *not yet verified* (it retries on the next stop, and only allows the stop with a loud `UNVERIFIED`
  banner after `MAX_TRIES`), so a busy lock is never mistaken for clean code, nor for broken code.
- The Stop gate builds **all** touched modules under a **single** lock acquisition (`lake build mod…`),
  so the lock is waited for once per stop — not once per module — keeping the gate within its timeout.

This protects only the `lake` that **leancheck itself runs** (the warm daemon and the cold gate). A
`lake build` or `lake serve` you start **yourself** does not take the lock and can still race the live
daemon — stop the daemon first (`leancheck --stop`) before a manual build, or work in a separate
worktree. (One harmless edge: two near-simultaneous edits to the *same* file during a cold build can
make the second warm check report "unavailable" instead of "deferred"; it self-corrects on the next
edit and never affects the authoritative cold build.)

For **genuinely parallel agents that edit overlapping files and build concurrently**, the clean
isolation is a **git worktree per agent**: each realpath'd root gets its own daemon *and* its own
`.lake/build`, so there is no shared build tree to race at all. (Cost: each worktree needs Mathlib's
prebuilt `.lake` cache present — see the Mathlib-rebuild guard.) When you instead run an **orchestrated
workflow** where one coordinator drives the edits and a final build, set `LEANCHECK_STOP_GATE=off` so
sub-agents finish on warm feedback and the orchestrator runs the single authoritative `lake build`
after the work settles.

## Cross-file behavior (honest)

`lake serve` resolves `import`s from compiled `.olean`, **not** from other files' live buffers. So a
warm check reflects the edited file's own current source but sees its dependencies as **last built** —
editing a dependency does not make a dependent see the change until the dependency is rebuilt. This is
a Lean fundamental (imports are compiled artifacts), true of the LSP, the REPL, and `lake` alike. The
cold `lake build` stop-gate is what catches cross-file staleness.

## Mathlib-rebuild guard

If a project depends on Mathlib but Mathlib's oleans are missing (a fresh checkout, or a worktree
without the prebuilt `.lake` cache/symlink), a `lake build`/`lake serve` would recompile Mathlib from
source — hours of CPU. Every leancheck entry point checks for this and **aborts with a loud warning**
instead of silently starting the rebuild. Opt in with `LEANCHECK_ALLOW_MATHLIB_REBUILD=1`, or run
`leancheck --check-mathlib` to see the status. Projects that don't depend on Mathlib are unaffected.

## Subagents

The plugin's hooks run in the main session. Claude Code subagents run their own tool calls; to give a
custom subagent the same warm/cold wiring, reference the same scripts from the agent's frontmatter
hooks (pointing at `${CLAUDE_PLUGIN_ROOT}/scripts/...`).

## Tests

```sh
bash tests/run-tests.sh        # offline unit tests of all three scripts (no Lean, no daemon, no network)
```

These cover the pure logic only — LSP→compiler-style formatting, the Mathlib-rebuild guard, the
per-root daemon key/lock/pid derivation, the build lock's readers-writers exclusion (shared opens
coexist; a cold build excludes both), target detection + the `LEANCHECK_INCLUDE` filter, and the
cold-gate's touched-module parsing / skip-deleted-source / block-vs-allow / gate-toggle / deferred-vs-
failed decisions. End-to-end warm behavior needs a real `lake serve` and is exercised against a live
project.

## License

Apache-2.0.
