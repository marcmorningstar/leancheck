# leancheck

A [Claude Code](https://code.claude.com) plugin marketplace providing **leancheck** — warm, per-edit
Lean 4 diagnostics from the real `lake serve` language server, plus an authoritative cold `lake build`
gate before an agent may finish. Every `lake` build leancheck runs (the warm daemon and the cold gate)
is serialised behind a per-root, writer-priority lock, so the daemon and the gate never race the same
build artifacts.

The agent writes Lean and reads compiler-style errors automatically; it never sees the LSP protocol.
Works with any Lean 4 + Lake project, and is Mathlib-aware (it won't silently start a multi-hour
Mathlib recompile) without requiring Mathlib.

## Install

```sh
claude plugin marketplace add marcmorningstar/leancheck
claude plugin install leancheck@lean-tools
```

(or interactively: `/plugin marketplace add marcmorningstar/leancheck`, then
`/plugin install leancheck@lean-tools`)

For warm feedback, install the Lean LSP client: `pip install leanclient` (the cold-build gate works
without it). See **[`leancheck/README.md`](leancheck/README.md)** for full documentation,
requirements, configuration, and behavior.

## Layout

```
.claude-plugin/marketplace.json   # the "lean-tools" marketplace (lists the plugin)
leancheck/                        # the plugin
├── .claude-plugin/plugin.json
├── hooks/hooks.json              # SessionStart + PostToolUse + Stop
├── scripts/                      # leancheck.py engine + the three hook scripts
├── tests/run-tests.sh            # offline self-tests
└── README.md
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
