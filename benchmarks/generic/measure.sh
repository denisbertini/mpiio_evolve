#!/bin/bash
# =============================================================================
# mpiio_evolve -- GENERIC post-step measurement (application-agnostic).
#
# Run ONCE PER REPETITION by the batch script (host shell on the compute
# node) AFTER srun returned -- srun is the barrier, so every rank has
# closed its files: race-free without per-rank coordination.
#
# Emits the one line the fitness parser scores:
#     aggregate write bandwidth: <X> GiB/s
#
# usage: measure.sh [--strategy STRATEGY] [<data_dir> <t0_ns> <t1_ns>]
#        (MPIIO_EVOLVE_REP env, default 1; rep dir = <data_dir>/rep<N>)
#
# STRATEGIES (onboarding ladder for applications we do not know):
#   jsonl          sum two numeric fields over JSON-lines metric files
#                  (default keys: bytes/seconds -- pio-bench's ledger).
#                  Most precise: app-reported payload and app-measured
#                  I/O time, excluding process startup/teardown.
#   filesys_delta  du --apparent-size of the fresh rep dir over the srun
#                  wall time. ZERO application cooperation required --
#                  works for any binary that writes into the rep dir.
#                  Includes startup/sync overhead (conservative floor).
#   regex          measure NOTHING: the app already prints a throughput
#                  line and parser.py scans the whole job log for the
#                  known patterns (IOR-style, "aggregate write
#                  bandwidth: ...", ...). Custom patterns go in parser.py.
#   (planned) darshan  io_only_bw from a darshan-runtime wrapped run:
#                  universal, profiler-grade, no app cooperation at all.
# =============================================================================
set -uo pipefail

STRATEGY="jsonl"
METRICS_FILE="pio_metrics.jsonl"
BYTES_KEY="bytes"
SECONDS_KEY="seconds"

while [ "${1:-}" = "--strategy" ] || [ "${1:-}" = "--file" ] \
      || [ "${1:-}" = "--bytes-key" ] || [ "${1:-}" = "--seconds-key" ]; do
    case "$1" in
        --strategy)    STRATEGY="$2"; shift 2;;
        --file)        METRICS_FILE="$2"; shift 2;;
        --bytes-key)   BYTES_KEY="$2"; shift 2;;
        --seconds-key) SECONDS_KEY="$2"; shift 2;;
    esac
done

DATA_DIR="${1:?usage: measure.sh [--strategy S] <data_dir> <t0_ns> <t1_ns>}"
T0="${2:?missing t0 (ns)}"
T1="${3:?missing t1 (ns)}"
REP="${MPIIO_EVOLVE_REP:-1}"
RUN_DIR="$DATA_DIR/rep$REP"

elapsed=$(awk -v a="$T0" -v b="$T1" 'BEGIN{printf "%.3f",(b-a)/1e9}')

emit() {  # bytes seconds label
    local bytes="$1" secs="$2" label="$3"
    # Zero-byte guard on RAW counts (never score an empty measurement; cf.
    # the epoch measure.sh awk-exit-code incident documented there).
    awk -v b="$bytes" 'BEGIN{exit !(b>0)}' || {
        echo "generic[$label]: no bytes measured under $RUN_DIR -- app failed or wrote elsewhere" >&2
        exit 1
    }
    awk -v t="$secs" 'BEGIN{exit !(t>0)}' || {
        echo "generic[$label]: non-positive measurement time (${secs} s)" >&2
        exit 1
    }
    local gibs
    gibs=$(awk -v b="$bytes" -v t="$secs" 'BEGIN{printf "%.4f", b/1073741824/t}')
    echo "generic[$label]: $(awk -v b="$bytes" 'BEGIN{printf "%.4f", b/1073741824}') GiB in ${secs} s (rep ${REP}, ${SLURM_NTASKS:-?} ranks)"
    echo "aggregate write bandwidth: ${gibs} GiB/s"
    exit 0
}

case "$STRATEGY" in
regex)
    echo "generic[regex]: app-printed rates are scored from the job log by parser.py; nothing to add"
    exit 0
    ;;

jsonl)
    JSON="$RUN_DIR/$METRICS_FILE"
    [ -s "$JSON" ] || {
        echo "generic[jsonl]: metrics file missing/empty: $JSON" >&2
        ls -la "$RUN_DIR" >&2 2>/dev/null
        exit 1
    }
    # Sum "<BYTES_KEY>" and "<SECONDS_KEY>" numeric fields over all lines:
    # aggregate rate = total payload / total measured I/O time.
    read -r bytes secs nchk <<EOF2
$(awk -v bk="$BYTES_KEY" -v sk="$SECONDS_KEY" '
    {
        if (match($0, "\"" bk "\":[0-9.eE+-]+")) {
            v = substr($0, RSTART + length(bk) + 3, RLENGTH - length(bk) - 3)
            b += v; n++
        }
        if (match($0, "\"" sk "\":[0-9.eE+-]+")) {
            v = substr($0, RSTART + length(sk) + 3, RLENGTH - length(sk) - 3)
            t += v
        }
    }
    END { printf "%.0f %.6f %d", b+0, t+0, n+0 }
' "$JSON")
EOF2
    [ "${nchk:-0}" -gt 0 ] 2>/dev/null || {
        echo "generic[jsonl]: no \"$BYTES_KEY\" fields in $JSON" >&2
        exit 1
    }
    echo "generic[jsonl]: ${nchk} records from $(basename "$METRICS_FILE")"
    emit "$bytes" "$secs" "jsonl"
    ;;

filesys_delta)
    # Apparent size of the FRESH rep dir == bytes the app created.
    bytes=$(du -sB1 --apparent-size "$RUN_DIR" 2>/dev/null | awk '{print $1+0}')
    emit "${bytes:-0}" "$elapsed" "filesys_delta"
    ;;

*)
    echo "generic: unknown --strategy '$STRATEGY' (jsonl|filesys_delta|regex)" >&2
    exit 2
    ;;
esac
