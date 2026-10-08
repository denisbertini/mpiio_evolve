#!/bin/bash
# =============================================================================
# mpiio_evolve -- GENERIC PRE-step: run ONCE per repetition by the batch
# script (single process, host shell) BEFORE srun launches the MPI job.
#
# Creates the fresh per-repetition directory that run.sh cd's into and that
# filesys_delta measurement counts. A fresh dir per rep is the contract that
# makes "bytes in dir == bytes written by this rep" exact (no leftovers,
# no cross-rep overwrite).
#
# usage: setup.sh <data_dir>      (MPIIO_EVOLVE_REP env, default 1)
# Optional: SETUP_EXTRA_CMD env is executed after mkdir (single process,
# host shell) for app-specific staging (copying input decks, etc.).
# =============================================================================
set -uo pipefail
DATA_DIR="${1:?usage: setup.sh <data_dir>}"
REP="${MPIIO_EVOLVE_REP:-1}"

RUN_DIR="$DATA_DIR/rep$REP"
mkdir -p "$RUN_DIR" || exit 2

if [ -n "${SETUP_EXTRA_CMD:-}" ]; then
    ( cd "$RUN_DIR" && bash -c "$SETUP_EXTRA_CMD" ) || {
        echo "setup.sh: SETUP_EXTRA_CMD failed in $RUN_DIR" >&2
        exit 3
    }
fi

echo "setup.sh: rep $REP ready at $RUN_DIR"
