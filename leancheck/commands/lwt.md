---
description: Lean WorkTree — provision/tear down a collision-free, build-warm git worktree per agent
argument-hint: add <branch> [base] | remove <branch|path> [--delete-branch] | list | prune
allowed-tools: Bash(python3:*)
---

Run the leancheck **Lean WorkTree** tool with the arguments the user supplied:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lwt.py" $ARGUMENTS
```

What it does:
- `add <branch> [base]` — create a git worktree off `base` (default `HEAD`), symlink the shared
  Mathlib/dependency cache, copy the compiled local `.lake/build` (so the worktree's first build is
  a no-op, not a from-scratch Mathlib recompile), and start a detached per-worktree `lake serve`
  warm daemon. Prints the absolute worktree path as the **last** line of stdout.
- `remove <branch|path> [--delete-branch]` — stop that worktree's warm daemon and tear the tree
  down cleanly (the shared cache is left untouched).
- `list` — show every worktree with whether it is lwt-provisioned and whether its daemon is warm.
- `prune` — `git worktree prune`.

After running:
- For `add`, report the captured worktree path so the user (or an orchestrator) can `cd` an agent
  into it. The agent that works there gets warm first-edit diagnostics immediately and only
  rebuilds the modules it actually edits.
- Surface any `ERROR:` line from stderr verbatim.

Tuning via env (set before invoking if needed): `LWT_BASE_DIR` (where worktrees are created —
point it at a fast/CoW-capable filesystem if the main checkout is on a slow mount such as WSL2
9p/drvfs), `LWT_MAIN` (the main checkout whose `.lake` is shared).
