#!/usr/bin/env python3
"""lwt — Lean WorkTree: one collision-free, build-warm git worktree per agent.

leancheck already gives every realpath'd project root its OWN `lake serve` daemon and its own
`.lake/build`, so genuinely parallel agents that edit overlapping files don't race a shared build
tree (see the "Concurrency & parallel agents" section of the README). The catch is provisioning:
a naive `git worktree add` has no `.lake`, so its first build recompiles Mathlib *from source*
(hours). `lwt` provisions a worktree that side-steps that and is warm on turn one:

  * .lake/packages  -> SYMLINK to the main checkout's shared, immutable Mathlib/dep cache
                       (multi-GB; read-only at build time; never copied).
  * .lake/build     -> a FULL copy of the main checkout's compiled local libraries, INCLUDING the
                       `.trace`/`.olean.hash` sidecars (Lake's up-to-date check is hash-based — a
                       missing trace forces a from-scratch re-elaboration of every local module).
                       `cp --reflink=auto` makes this a CoW clone when src and dst share one
                       CoW-capable FS (btrfs/xfs/ext4-reflink), else a plain copy. With the sidecars
                       intact the worktree's first `lake build` is a no-op; an agent only rebuilds
                       what it actually edits.
  * warm daemon     -> a per-worktree leancheck `lake serve`, started detached (keyed by the
                       worktree's realpath, so it never collides with any other tree's daemon).

Usage (mirrors `git worktree`):
    lwt add <branch> [base-ref] [--path PATH] [--no-warm] [--warm-file FILE]
    lwt remove <path-or-branch> [--delete-branch]            (alias: rm)
    lwt list                                                 (alias: ls)
    lwt prune

`add` prints the absolute worktree path as the LAST line of stdout (all logs go to stderr), so an
orchestrator can capture it:  WT=$(lwt add my-branch | tail -1).

Env: LWT_MAIN (main checkout; default = detected git common dir, else cwd), LWT_BASE_DIR (where
worktrees are created; default = the main checkout's parent dir — point this at a fast/CoW-capable
filesystem if the main checkout lives on a slow mount such as WSL2 9p/drvfs), LEANCHECK_PROJECT_SUBDIR
(the Lake package's subdir within the repo, for a monorepo whose package is NOT at the repo root —
the worktree is still repo-level but the shared `.lake` and the warm daemon bind to `<tree>/<subdir>`;
empty/unset = package at the repo root), LEANCHECK_SCRIPT (path to leancheck.py; default = sibling of
this file).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time

WT_INFIX = "-lwt-"

# A monorepo's Lake package may live in a SUBDIR, not at the repo root. `git worktree` is always
# repo-level, but the `.lake` to share/warm — and the daemon's root — is the subdir's. SUBDIR names
# it (empty/unset = package at the repo root, the common case). Mirrors leanmod.project_root.
SUBDIR = os.environ.get("LEANCHECK_PROJECT_SUBDIR", "").strip().strip("/\\")


def lake_root(tree: str) -> str:
    """The Lake project root within a checkout/worktree `tree`: its SUBDIR (monorepo) or `tree` itself."""
    return os.path.join(tree, SUBDIR) if SUBDIR else tree


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    log(f"ERROR: {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- repo / path helpers

def main_repo() -> str:
    """The primary (non-worktree) checkout whose .lake we share. Works from any worktree."""
    env = os.environ.get("LWT_MAIN")
    if env:
        return os.path.realpath(env)
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        # The common dir is "<main-worktree>/.git"; its parent is the main checkout.
        return os.path.dirname(os.path.realpath(common))
    except Exception:
        return os.getcwd()


def base_dir(main: str) -> str:
    return os.environ.get("LWT_BASE_DIR") or os.path.dirname(main)


def slug(branch: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in branch)


def default_path(main: str, branch: str) -> str:
    return os.path.join(base_dir(main), os.path.basename(main) + WT_INFIX + slug(branch))


def find_leancheck() -> str | None:
    """Locate leancheck.py (the warm-feedback daemon). It ships beside this file in the plugin."""
    env = os.environ.get("LEANCHECK_SCRIPT")
    if env and os.path.exists(env):
        return env
    sib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leancheck.py")
    if os.path.exists(sib):
        return sib
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        c = os.path.join(root, "scripts", "leancheck.py")
        if os.path.exists(c):
            return c
    return None


def sock_path(wt: str) -> str:
    """The socket leancheck.py derives for a root (mirrors its KEY formula) — for liveness display."""
    root = os.path.realpath(lake_root(wt))
    key = os.environ.get("LEANCHECK_KEY") or ("leancheck-" + hashlib.sha1(root.encode()).hexdigest()[:8])
    return os.path.join(os.environ.get("LEANCHECK_SOCKDIR", "/tmp"), f"leancheck-{key}.sock")


# ---------------------------------------------------------------- provisioning

def provision_lake(main: str, wt: str) -> None:
    # The Lake package (and thus `.lake`) is the SUBDIR's in a monorepo, the tree root's otherwise.
    src_root, dst_root = lake_root(main), lake_root(wt)
    src_pkgs = os.path.join(src_root, ".lake", "packages")
    src_build = os.path.join(src_root, ".lake", "build")
    if not os.path.isdir(src_build):
        die(f"{src_build} missing — build the main checkout first (lake build).")

    os.makedirs(os.path.join(dst_root, ".lake"), exist_ok=True)

    # Shared immutable dependency cache (incl. Mathlib's multi-GB oleans): symlink, never copy.
    # `.lake/packages` is absent for a dependency-free project — then there is nothing to share.
    if os.path.isdir(src_pkgs):
        link = os.path.join(dst_root, ".lake", "packages")
        if os.path.islink(link) or os.path.exists(link):
            (os.unlink if os.path.islink(link) else shutil.rmtree)(link)
        os.symlink(src_pkgs, link)
        log(f">> symlinked .lake/packages -> {src_pkgs}")

    # Private build dir: a faithful copy of every artifact AND its `.trace`/`.olean.hash` sidecar
    # (a missing trace forces a from-scratch re-elaboration of the local libs). `--reflink=auto`
    # = CoW clone when src and dst share a CoW-capable FS, else a plain copy — one command, never
    # partial, never nested. (A CoW clone is impossible across different filesystems, e.g. a 9p
    # workdir vs an overlay/ext4 worktree base — then it simply copies.)
    dst_build = os.path.join(dst_root, ".lake", "build")
    if os.path.exists(dst_build):
        shutil.rmtree(dst_build)
    t0 = time.monotonic()
    subprocess.run(["cp", "-a", "--reflink=auto", src_build, dst_build], check=True)
    log(f">> copied .lake/build ({time.monotonic() - t0:.1f}s)")


def warm(wt: str, seed_file: str | None) -> None:
    """Start the worktree's leancheck daemon (its own `lake serve`) detached, so an agent spawned
    here gets instant first-edit feedback. No-ops gracefully if leancheck/leanclient is unavailable."""
    lc = find_leancheck()
    if not lc:
        log(">> leancheck.py not found — skipping warm (cold `lake build` still works)")
        return
    root = os.path.realpath(lake_root(wt))
    env = dict(os.environ, LEANCHECK_ROOT=root)
    cmd = [sys.executable, lc, "--warm"] + ([seed_file] if seed_file else [])
    logf = open(f"/tmp/lwt-warm-{slug(os.path.basename(wt))}.log", "ab")
    subprocess.Popen(cmd, env=env, stdout=logf, stderr=logf, start_new_session=True, cwd=root)
    # Poll briefly for the socket so we can confirm the daemon started (its `lake serve` keeps
    # importing in the background under leancheck's own warm budget — we don't block on that).
    sp = sock_path(wt)
    for _ in range(24):  # ~12s
        if os.path.exists(sp):
            log(f">> warm leancheck daemon started (socket {sp})")
            return
        time.sleep(0.5)
    log(">> warm daemon launching in background (socket not up yet — first edit will attach)")


def stop_daemon(wt: str) -> None:
    lc = find_leancheck()
    if not lc:
        return
    env = dict(os.environ, LEANCHECK_ROOT=os.path.realpath(lake_root(wt)))
    subprocess.run([sys.executable, lc, "--stop"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(">> stopped worktree leancheck daemon")


# ---------------------------------------------------------------- git worktree wrappers

def worktrees(main: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts {path, branch, head}."""
    out = subprocess.check_output(["git", "-C", main, "worktree", "list", "--porcelain"], text=True)
    res, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                res.append(cur); cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "")
        elif key == "HEAD":
            cur["head"] = val[:9]
    if cur:
        res.append(cur)
    return res


def resolve_worktree(main: str, target: str) -> str:
    """Map a path OR a branch name to an existing worktree path."""
    if os.path.isdir(target):
        return os.path.realpath(target)
    for wt in worktrees(main):
        if wt.get("branch") == target or os.path.realpath(wt["path"]) == os.path.realpath(target):
            return os.path.realpath(wt["path"])
    die(f"no worktree matches path/branch: {target}")


# ---------------------------------------------------------------- commands

def cmd_add(a) -> None:
    main = main_repo()
    wt = os.path.realpath(a.path) if a.path else default_path(main, a.branch)
    if os.path.exists(wt):
        die(f"worktree path already exists: {wt}")

    log(f">> git worktree add -b {a.branch} {wt} {a.base or 'HEAD'}")
    if subprocess.run(["git", "-C", main, "worktree", "add", "-b", a.branch, wt, a.base or "HEAD"]).returncode != 0:
        die(f"git worktree add failed (branch {a.branch!r} may already exist)")

    provision_lake(main, wt)
    if not a.no_warm:
        warm(wt, a.warm_file)
    log(f">> worktree ready: {wt}")
    print(wt)  # last stdout line = the path, for orchestrator capture


def cmd_remove(a) -> None:
    main = main_repo()
    wt = resolve_worktree(main, a.target)
    branch = next((w.get("branch") for w in worktrees(main)
                   if os.path.realpath(w["path"]) == wt), None)

    stop_daemon(wt)
    # Drop the packages symlink first so `git worktree remove` never recurses into the shared cache.
    link = os.path.join(wt, ".lake", "packages")
    if os.path.islink(link):
        os.unlink(link)
        log(">> removed .lake/packages symlink (shared cache untouched)")

    subprocess.run(["git", "-C", main, "worktree", "remove", "--force", wt], check=True)
    log(f">> removed worktree: {wt}")
    subprocess.run(["git", "-C", main, "worktree", "prune"], stderr=subprocess.DEVNULL)

    if a.delete_branch and branch:
        subprocess.run(["git", "-C", main, "branch", "-D", branch])
        log(f">> deleted branch: {branch}")


def cmd_list(a) -> None:
    main = main_repo()
    for w in worktrees(main):
        path = w["path"]
        prov = "lwt" if os.path.islink(os.path.join(lake_root(path), ".lake", "packages")) else "—"
        flame = "warm" if os.path.exists(sock_path(path)) else "cold"
        print(f"{path}  [{w.get('branch', w.get('head', '?'))}]  {prov}  {flame}")


def cmd_prune(a) -> None:
    subprocess.run(["git", "-C", main_repo(), "worktree", "prune"])
    log(">> pruned stale worktree administrative files")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lwt", description="Lean WorkTree: per-agent git worktrees with warm Lean builds.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="create + provision + warm a worktree")
    pa.add_argument("branch", help="new branch name (also names the worktree)")
    pa.add_argument("base", nargs="?", help="base ref (default HEAD)")
    pa.add_argument("--path", help="worktree path (default <LWT_BASE_DIR>/<repo>-lwt-<branch>)")
    pa.add_argument("--no-warm", action="store_true", help="don't start the leancheck daemon")
    pa.add_argument("--warm-file", help="also pre-open this file in the daemon")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", aliases=["rm"], help="stop daemon + tear down a worktree")
    pr.add_argument("target", help="worktree path or branch name")
    pr.add_argument("--delete-branch", action="store_true", help="also delete the branch")
    pr.set_defaults(func=cmd_remove)

    pl = sub.add_parser("list", aliases=["ls"], help="list worktrees (provisioned? warm?)")
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("prune", help="git worktree prune")
    pp.set_defaults(func=cmd_prune)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
