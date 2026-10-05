#!/bin/bash
# =============================================================================
# mpiio_evolve -- epoch_io PRE-step: run ONCE per repetition by the batch
# script (single process) BEFORE srun launches the MPI job.
#
# Prepares the directory every rank will cd into and places the deck under
# EPOCH's default name:  input.deck  (EPOCH 4.20 reads the output directory
# from stdin, then opens <outdir>/input.deck -- production idiom is
# 'echo "." | epoch3d' with ./input.deck present).
#
# usage: setup_run.sh <data_dir>      (MPIIO_EVOLVE_REP env, default 1)
# =============================================================================
set -uo pipefail
DATA_DIR="${1:?usage: setup_run.sh <data_dir>}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DECK="${EPOCH_DECK:-epoch3d_lwfa.deck}"
REP="${MPIIO_EVOLVE_REP:-1}"

RUN_DIR="$DATA_DIR/rep$REP"
mkdir -p "$RUN_DIR" || exit 2
DECK_PATH="$BENCH_DIR/$DECK"
[ -f "$DECK_PATH" ] || { echo "setup_run: deck $DECK_PATH missing" >&2; exit 2; }

# Copy, do NOT symlink: EPOCH has been observed anchoring its SDF output at
# the deck's REAL path, and a symlink into the repo would scatter diagnostics
# into the checkout (and away from measure.sh). A copy keeps realpath == repdir.
cp -f "$DECK_PATH" "$RUN_DIR/input.deck"
echo "setup_run: rep $REP ready at $RUN_DIR (deck -> $DECK_PATH)"
