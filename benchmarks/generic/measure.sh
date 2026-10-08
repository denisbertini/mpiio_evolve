#!/bin/bash
# =============================================================================
# mpiio_evolve -- GENERIC post-step measurement (application-agnostic).
#
# Run ONCE PER REPETITION by the batch script (host shell on the compute
# node) AFTER srun returned -- srun is the barrier, so every rank has
# closed its files: race-free without per-rank coordination.
#
# Emits the lines the fitness parser scores:
#     aggregate write bandwidth: <X> GiB/s        (app-reported / filesystem)
#     aggregate io-only bandwidth: <X> GiB/s      (darshan, v2 strategies)
# Phrasing matters: the io-only line deliberately contains neither the
# word "write" nor "read" so the generic app-rate regex cannot capture it
# (see parser.py _IO_ONLY).
#
# usage: measure.sh [--strategy STRATEGY] [<data_dir> <t0_ns> <t1_ns>]
#        (MPIIO_EVOLVE_REP env, default 1; rep dir = <data_dir>/rep<N>)
#
# STRATEGIES (onboarding ladder for applications we do not know):
#   jsonl          sum two numeric fields over JSON-lines metric files
#                  (default keys: bytes/seconds -- pio-bench's ledger).
#                  Most precise app-side number: payload and app-measured
#                  I/O time, excluding process startup/teardown.
#   filesys_delta  du --apparent-size of the fresh rep dir over the srun
#                  wall time. ZERO application cooperation required --
#                  works for any binary that writes into the rep dir.
#                  Includes startup/sync overhead (conservative floor).
#   regex          measure NOTHING: the app already prints a throughput
#                  line and parser.py scans the whole job log for the
#                  known patterns (IOR-style, "aggregate write
#                  bandwidth: ...", ...). Custom patterns go in parser.py.
#   darshan        FITNESS v2. The run must have been wrapped with
#                  darshan-runtime (profile command: "run.sh <dir> --
#                  darshan-runtime <app>"); per-rank logs land in
#                  $RUN_DIR/darshan_logs. Aggregate:
#                      io_only_bw = SUM(cumulative_bytes_written)
#                                   / MAX(io_only_time across ranks)
#                  bytes are additive; the wall the application waits is
#                  the slowest rank -- exactly what hint tuning targets.
#                  Also runs the jsonl aggregation when the metrics file
#                  exists, so fitness.metric "auto" can choose per-run.
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

# emit <bytes> <seconds> <label> <line-kind: write|io-only>
# Zero-guards on RAW counts (never score an empty measurement; cf. the
# epoch measure.sh awk-exit-code incident documented there).
emit() {
    local bytes="$1" secs="$2" label="$3" kind="${4:-write}"
    awk -v b="$bytes" 'BEGIN{exit !(b>0)}' || {
        echo "generic[$label]: no bytes measured under $RUN_DIR -- app failed or wrote elsewhere" >&2
        return 1
    }
    awk -v t="$secs" 'BEGIN{exit !(t>0)}' || {
        echo "generic[$label]: non-positive measurement time (${secs} s)" >&2
        return 1
    }
    local gibs
    gibs=$(awk -v b="$bytes" -v t="$secs" 'BEGIN{printf "%.4f", b/1073741824/t}')
    echo "generic[$label]: $(awk -v b="$bytes" 'BEGIN{printf "%.4f", b/1073741824}') GiB in ${secs} s (rep ${REP}, ${SLURM_NTASKS:-?} ranks)"
    case "$kind" in
        write)   echo "aggregate write bandwidth: ${gibs} GiB/s";;
        io-only) echo "aggregate io-only bandwidth: ${gibs} GiB/s";;
    esac
}

jsonl_lines() {   # prints app-rate lines; returns non-zero if no file
    local JSON="$RUN_DIR/$METRICS_FILE" bytes secs nchk
    [ -s "$JSON" ] || return 1
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
    [ "${nchk:-0}" -gt 0 ] 2>/dev/null || return 1
    echo "generic[jsonl]: ${nchk} records from $(basename "$METRICS_FILE")"
    emit "$bytes" "$secs" "jsonl" write
}

case "$STRATEGY" in
regex)
    echo "generic[regex]: app-printed rates are scored from the job log by parser.py; nothing to add"
    exit 0
    ;;

jsonl)
    jsonl_lines || {
        echo "generic[jsonl]: metrics file missing/empty: $RUN_DIR/$METRICS_FILE" >&2
        ls -la "$RUN_DIR" >&2 2>/dev/null
        exit 1
    }
    exit 0
    ;;

filesys_delta)
    # Apparent size of the FRESH rep dir == bytes the app created.
    bytes=$(du -sB1 --apparent-size "$RUN_DIR" 2>/dev/null | awk '{print $1+0}')
    emit "${bytes:-0}" "$elapsed" "filesys_delta" write
    exit 0
    ;;

darshan)
    LOGDIR="${DARSHAN_LOGPATH:-$RUN_DIR/darshan_logs}"
    mapfile -t LOGS < <(find "$LOGDIR" -name '*.darshan.gz' 2>/dev/null)
    if [ "${#LOGS[@]}" -eq 0 ]; then
        echo "generic[darshan]: no logs under $LOGDIR -- was the run wrapped" >&2
        echo "  ('-- darshan-runtime <app>' in the profile command) and is the" >&2
        echo "  image built with darshan-runtime? (fitness v2 requirement)" >&2
        exit 1
    fi
    if command -v darshan-parser >/dev/null 2>&1; then
        PARSER=(darshan-parser)
    elif [ -n "${MPIIO_EVOLVE_SIF:-}" ] && command -v apptainer >/dev/null 2>&1; then
        PARSER=(apptainer exec "$MPIIO_EVOLVE_SIF" darshan-parser)
    else
        echo "generic[darshan]: darshan-parser not on PATH and MPIIO_EVOLVE_SIF unusable" >&2
        exit 1
    fi
    # Aggregate over the per-rank logs (Darshan writes one file per process).
    # 3.5.x --perf prints job-level "key: value" lines; anything else =
    # format drift -> fail loudly with forensics instead of scoring garbage.
    agg=$("${PARSER[@]}" --perf "${LOGS[@]}" 2>/dev/null | awk '
        /^#/ { next }
        {
            key = $1; sub(/:$/, "", key)
            if      (key == "io_only_time")             { n++; if ($2+0 > t) t = $2+0 }
            else if (key == "cumulative_bytes_written") { wb += $2+0 }
            else if (key == "cumulative_bytes_read")    { rb += $2+0 }
        }
        END { printf "%.0f %.0f %.6f %d", wb+0, rb+0, t+0, n+0 }
    ')
    read -r wb rb tmax nrank <<EOF2
$agg
EOF2
    if [ "${nrank:-0}" -lt "${#LOGS[@]}" ] 2>/dev/null || [ "${nrank:-0}" -eq 0 ]; then
        echo "generic[darshan]: io_only_time found in ${nrank:-0}/${#LOGS[@]} logs" >&2
        echo "  -- darshan-parser output format drift? forensic --perf sample:" >&2
        "${PARSER[@]}" --perf "${LOGS[0]}" 2>&1 | head -30 >&2
        exit 1
    fi
    echo "generic[darshan]: ${#LOGS[@]} rank logs, wrote $(awk -v b="$wb" 'BEGIN{printf "%.4f", b/1073741824}') GiB, read $(awk -v b="$rb" 'BEGIN{printf "%.4f", b/1073741824}') GiB, io_only_time max ${tmax} s"
    emit "$wb" "$tmax" "darshan" io-only
    # Companion app-rate line (if the instrument provides one): lets
    # fitness.metric "auto" fall back per-run and keeps both series in
    # the ledger for cross-validation (cumulative-vs-app ratio is itself
    # a hint-behavior signal, e.g. double counting across layers).
    jsonl_lines || true
    exit 0
    ;;

*)
    echo "generic: unknown --strategy '$STRATEGY' (jsonl|filesys_delta|regex|darshan)" >&2
    exit 2
    ;;
esac
