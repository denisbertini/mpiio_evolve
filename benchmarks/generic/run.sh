#!/bin/bash
# =============================================================================
# mpiio_evolve -- GENERIC per-rank launch shim (application-agnostic).
#
# Executed ONCE PER MPI RANK inside the container:
#   srun --mpi=pmix -> apptainer exec -> this script -> <app command>
# srun IS the MPI launcher (PMIx bootstrap); the shim measures NOTHING and
# coordinates with no rank (srun's return is the barrier; measure.sh scores
# afterwards). Same discipline as the epoch_io shim.
#
# usage: run.sh <data_dir> -- <app command and args...>
#        run.sh <data_dir> <app command and args...>   ('--' optional)
#
# Contract given to the application:
#   * cwd is a FRESH per-repetition directory: <data_dir>/rep<MPIIO_EVOLVE_REP>
#     (created by setup.sh; the app's writes are exactly what lands here,
#     which is what filesys_delta measurement counts).
#   * MPI_Comm is the job's world; rank count comes from the #SBATCH header.
#   * ROMIO/OMPIO hints arrive via the job environment (ROMIO_HINTS file /
#     OMPI_MCA_* exports) -- the app must pass an EMPTY/NULL MPI_Info for
#     them to apply (pio-bench does; EPOCH does).
#   * Lustre layout (lfs setstripe) was applied to <data_dir> pre-job.
#
# The app command is whatever the profile YAML says, e.g.:
#   pio_bench --backend mpiio --local 96 --particles 250000 --steps 5
#   /path/to/user_app  (a Lustre-resident binary, built against the image)
# Unknown applications need no shim changes -- only a profile block.
# =============================================================================
set -uo pipefail

DATA_DIR="${1:?usage: run.sh <data_dir> -- <app...>}"
shift
[ "${1:-}" = "--" ] && shift
[ $# -ge 1 ] || { echo "run.sh: no application command" >&2; exit 2; }

REP="${MPIIO_EVOLVE_REP:-1}"
RUN_DIR="$DATA_DIR/rep$REP"
cd "$RUN_DIR" 2>/dev/null || {
    echo "run.sh: rep dir missing (setup.sh not run?): $RUN_DIR" >&2
    exit 2
}

# Darshan wrap support (fitness v2): when the profile command starts with
# darshan-runtime, logs must land inside the rep dir where measure.sh looks
# for them (def configures the runtime with --with-log-path-by-env=
# DARSHAN_LOGPATH). Harmless for unwrapped apps; mkdir -p is race-safe
# across ranks on the same node.
export DARSHAN_LOGPATH="${DARSHAN_LOGPATH:-$RUN_DIR/darshan_logs}"
mkdir -p "$DARSHAN_LOGPATH" 2>/dev/null || true

# First word must be executable inside the container.
command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || {
    echo "run.sh: '$1' not found/not executable in container" >&2
    exit 127
}

exec "$@"
