#!/bin/bash
# =============================================================================
# mpiio_evolve -- EPOCH1D MPI-IO benchmark runner
#
# Runs INSIDE the apptainer image, launched by srun: ONE wrapper per rank
# (srun_direct mode). All ranks execute epoch1d; rank 0 then aggregates the
# total SDF diagnostic volume and elapsed wall time and prints the
# generic-parsable fitness line:
#
#     aggregate write bandwidth: <X> GiB/s
#
# Env (all optional):
#   EPOCH_BIN   epoch binary               (default: epoch1d_lstr)
#   EPOCH_DECK  deck file in this folder   (default: epoch1d_io.input)
# =============================================================================
set -uo pipefail

DATA_DIR="${1:?usage: run_bench.sh <data_dir>}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DECK="${EPOCH_DECK:-epoch1d_io.input}"
EPOCH_BIN="${EPOCH_BIN:-epoch1d_lstr}"
RANK="${SLURM_PROCID:-0}"
NTASKS="${SLURM_NTASKS:-1}"

if ! command -v "$EPOCH_BIN" >/dev/null 2>&1; then
    echo "epoch_io: benchmark binary '$EPOCH_BIN' not found in container" >&2
    exit 127
fi

mkdir -p "$DATA_DIR" || exit 2
cd "$DATA_DIR" || exit 2
rm -f .rank_done.* .rank_rc.* 0*.sdf
cp "$BENCH_DIR/$DECK" ./ || { echo "epoch_io: deck $BENCH_DIR/$DECK missing" >&2; exit 2; }

# ---- every rank runs the simulation (EPOCH is one MPI rank per process) ----
epoch_log="epoch_stdout_rank${RANK}.log"
start_ns=$(date +%s%N)
"$EPOCH_BIN" "$DECK" > "$epoch_log" 2>&1
rc=$?
echo "$rc" > ".rank_rc.$RANK"
touch ".rank_done.$RANK"

if [ "$RANK" != "0" ]; then
    exit "$rc"
fi

# ---- rank 0: wait for stragglers, then measure ------------------------------
deadline=$(( $(date +%s) + 180 ))
while [ "$(ls .rank_done.* 2>/dev/null | wc -l)" -lt "$NTASKS" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "epoch_io: rank0 timed out waiting for rank completion stamps" >&2
        break
    fi
    sleep 2
done
end_ns=$(date +%s%N)
elapsed=$(awk -v a="$start_ns" -v b="$end_ns" 'BEGIN{printf "%.3f",(b-a)/1e9}')

fail=$(cat .rank_rc.* 2>/dev/null | awk '{s+=$1} END{print s+0}')
if [ "$fail" != "0" ]; then
    echo "epoch_io: EPOCH failed on one or more ranks (sum of exit codes = $fail)" >&2
    tail -n 40 "$epoch_log" >&2
    exit 1
fi

# Total diagnostic bytes written to Lustre during the trial
bytes=$(du -cb -- *.sdf 2>/dev/null | tail -n1 | awk '{print $1+0}')
bytes=${bytes:-0}
read -r gib gibs mibs <<EOF2
$(awk -v b="$bytes" -v t="$elapsed" 'BEGIN{
    g=b/1073741824;
    s=(t>0)?g/t:0;
    printf "%.4f %.4f %.2f", g, s, s*1024
}')
EOF2

if awk -v g="$gib" 'BEGIN{exit (g<=0)}'; then
    echo "epoch_io: EPOCH exited 0 but produced no .sdf diagnostics -- deck likely rejected" >&2
    tail -n 40 "$epoch_log" >&2
    exit 1
fi

echo "epoch_io: wrote ${gib} GiB of SDF diagnostics in ${elapsed} s across ${NTASKS} ranks"
echo "aggregate write bandwidth: ${gibs} GiB/s"
echo "epoch total wallclock: ${elapsed} seconds"
exit 0
