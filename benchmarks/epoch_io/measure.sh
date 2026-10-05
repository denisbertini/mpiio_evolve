#!/bin/bash
# =============================================================================
# mpiio_evolve -- post-step measurement for the epoch_io profile.
#
# Run ONCE PER REPETITION by the batch script (host shell on the compute
# node) AFTER srun returned -- srun is the barrier, so every rank has
# closed its files and this measurement is race-free without any per-rank
# coordination.
#
# Layout-agnostic: sums *.sdf whether EPOCH wrote one shared file via
# MPI-IO collective buffering or one file per rank. Emits the generic
# fitness line the parser scores:
#     aggregate write bandwidth: <X> GiB/s
#
# usage: measure.sh <data_dir> <t0_ns> <t1_ns>      (MPIIO_EVOLVE_REP env)
# =============================================================================
set -uo pipefail
DATA_DIR="${1:?usage: measure.sh <data_dir> <t0_ns> <t1_ns>}"
T0="${2:?missing t0 (ns)}"
T1="${3:?missing t1 (ns)}"
REP="${MPIIO_EVOLVE_REP:-1}"
DATA_DIR="$DATA_DIR/rep$REP"

elapsed=$(awk -v a="$T0" -v b="$T1" 'BEGIN{printf "%.3f",(b-a)/1e9}')
# Recursively sum SDF bytes under this rep dir (EPOCH may nest files below
# it; a top-level *.sdf glob silently misses them).
base_dir=$(dirname "$DATA_DIR")            # the shared 'data/' parent
bytes=$(find "$DATA_DIR" -type f \( -name '*.sdf' -o -name '*.sdf.[0-9]*' \) -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
bytes=${bytes:-0}
if [ "$bytes" -eq 0 ]; then
    # Zero under rep<N>: widen the search to the whole data tree for a
    # diagnostic (do NOT score it -- it would cross-contaminate reps).
    echo "measure: no .sdf under $DATA_DIR; sibling tree under $base_dir:" >&2
    find "$base_dir" -maxdepth 2 -type f \( -name '*.sdf' -o -name '*.deck' \) -printf '  %s\t%p\n' 2>/dev/null | head -20 >&2
fi
read -r gib gibs <<EOF2
$(awk -v b="$bytes" -v t="$elapsed" 'BEGIN{
    g=b/1073741824;
    s=(t>0)?g/t:0;
    printf "%.4f %.4f", g, s
}')
EOF2

# Zero-byte guard on the RAW byte count. NOTE: an earlier revision tested
# this with `if awk 'BEGIN{exit (g<=0)}'` -- inverted semantics (awk exit 0
# == shell success), which hijacked the SUCCESS path into the error branch,
# suppressed the bandwidth line and zeroed every real fitness. Never test
# shell conditionals through awk exit codes again.
if [ "$bytes" -eq 0 ]; then
    echo "epoch_io: no .sdf diagnostics under $DATA_DIR -- deck likely rejected" >&2
    exit 1
fi

echo "epoch_io: wrote ${gib} GiB of SDF diagnostics in ${elapsed} s across ${SLURM_NTASKS:-?} ranks (rep ${REP})"
echo "aggregate write bandwidth: ${gibs} GiB/s"
echo "epoch total wallclock: ${elapsed} seconds"
exit 0
