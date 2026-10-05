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
#   EPOCH_BIN         epoch binary            (default: epoch3d)
#   MPIIO_EVOLVE_REP  in-job repetition index (default: 1)
# =============================================================================
set -uo pipefail

DATA_DIR="${1:?usage: run_bench.sh <data_dir>}"
# Default matches Denis's validated run_file.sh (plain epoch3d); our run
# paths stay well under the 256-char limit, so the long-path _lstr rebuild
# is not needed -- switch with EPOCH_BIN=epoch3d_lstr if paths grow.
EPOCH_BIN="${EPOCH_BIN:-epoch3d}"
REP="${MPIIO_EVOLVE_REP:-1}"

# One directory per repetition (created + input.deck-symlinked BEFORE the
# MPI step by setup_run.sh); reps never share files.
DATA_DIR="$DATA_DIR/rep$REP"
cd "$DATA_DIR" || exit 2

command -v "$EPOCH_BIN" >/dev/null 2>&1 || {
    echo "epoch_io: benchmark binary '$EPOCH_BIN' not found in container" >&2
    exit 127
}

# EPOCH 4.20 startup protocol: it prompts "Specify output directory" on
# stdin, then opens <outdir>/input.deck. The rep dir (cwd) already contains
# input.deck -- symlinked by setup_run.sh before srun. Answer the prompt
# exactly like the production run_file.sh:
echo "." | "$EPOCH_BIN"
rc=$?
# Breadcrumb for zero-measurement forensics (cheap; 32 short blocks per rep):
echo "run_bench[rank ${SLURM_PROCID:-?}]: rc=$rc cwd=$(pwd)"
find . -maxdepth 2 -type f -name '*.sdf' -printf '  %s\t%p\n' 2>/dev/null | head -4
exit $rc
