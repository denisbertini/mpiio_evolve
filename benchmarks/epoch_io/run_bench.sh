#!/bin/bash
# =============================================================================
# mpiio_evolve -- EPOCH 3D LWFA rank shim (srun_direct mode)
#
# Executed ONCE PER MPI RANK inside the container:
#   srun --mpi=pmix  ->  one apptainer exec per task  ->  this script -> epoch3d
# srun IS the MPI launcher (spawn + PMIx bootstrap of MPI_COMM_WORLD at
# MPI_Init); no mpirun is needed or wanted inside the container.
#
# This shim deliberately measures NOTHING and coordinates with no rank:
# after srun returns (srun is the barrier), the batch script runs
# measure.sh once per repetition. No stamps, no wait loops, no races.
#
# Env (all optional):
#   EPOCH_BIN         epoch binary            (default: epoch3d_lstr)
#   EPOCH_DECK        deck file in this dir   (default: epoch3d_lwfa.deck)
#   MPIIO_EVOLVE_REP  in-job repetition index (default: 1)
# =============================================================================
set -uo pipefail

DATA_DIR="${1:?usage: run_bench.sh <data_dir>}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DECK="${EPOCH_DECK:-epoch3d_lwfa.deck}"
# _lstr variant: same build, string_length=100000 for long Lustre paths
EPOCH_BIN="${EPOCH_BIN:-epoch3d_lstr}"
REP="${MPIIO_EVOLVE_REP:-1}"

# One directory per repetition: reps never share files.
DATA_DIR="$DATA_DIR/rep$REP"
mkdir -p "$DATA_DIR" || exit 2
cd "$DATA_DIR" || exit 2

# Deck read in place from the repo (read-only); SDF diagnostics land in cwd.
DECK_PATH="$BENCH_DIR/$DECK"
[ -f "$DECK_PATH" ] || { echo "epoch_io: deck $DECK_PATH missing" >&2; exit 2; }
command -v "$EPOCH_BIN" >/dev/null 2>&1 || {
    echo "epoch_io: benchmark binary '$EPOCH_BIN' not found in container" >&2
    exit 127
}

# EPOCH owns the I/O pattern entirely -- collective buffering to a shared
# file (the MPI-IO path our ROMIO hints tune) or per-rank SDFs, per deck.
# The shim only places cwd on the striped Lustre directory and execs.
exec "$EPOCH_BIN" "$DECK_PATH"
