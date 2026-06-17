#!/usr/bin/env bash
# Offline unit tests for the leancheck warm-feedback harness — no Lean toolchain, no daemon, no
# network required. Run from anywhere: `bash tests/run-tests.sh`.
set -euo pipefail
cd "$(dirname "$0")/../scripts"
echo "== leancheck.py   (LSP -> compiler-style formatting, Mathlib-rebuild guard, per-root key/lock/pid) =="
python3 leancheck.py --selftest
echo "== post-edit hook (target detection + LEANCHECK_INCLUDE filter, warm/cold context, JSON envelope) =="
python3 post-edit-leancheck.py --selftest
echo "== stop-coldbuild (touched-module parsing, skip-deleted-source, block-vs-allow + UNVERIFIED banner) =="
python3 stop-coldbuild.py --selftest
echo "ALL LEANCHECK SELFTESTS PASSED"
