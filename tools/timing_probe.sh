#!/usr/bin/env bash
# =============================================================================
# tools/timing_probe.sh -- ONE real simulation, measured. Zero typing.
#
#     ./tools/timing_probe.sh --dry-run     # PREVIEW: render submit.sh only
#     ./tools/timing_probe.sh               # REAL: submit, wait, report
#
# Options (all optional, any order):
#   --dry-run           build the overlay + run evaluate.py in dry-run mode,
#                       then print the exact submit.sh that WOULD be sent.
#                       Submits nothing; safe anywhere (no sbatch needed).
#   --image PATH        container image   (default container/plasma_pp.sif)
#   --candidate PATH    candidate JSON    (default examples/candidate_romio.json)
#   -h | --help         this text
#
# What the probe does:
#   * copies config.generated.json -> timing_probe.generated.json overriding
#     ONLY: container.image, fitness.repetitions=1, repeat_mode=in_job
#     (config.yaml is never touched);
#   * runs ONE evaluation through the real pipeline -- the same submit.sh
#     generation, env header, striping, hints and bare srun that the
#     evolution controller will use thousands of times;
#   * wraps itself in a tmux session 'probe' so an SSH drop cannot lose the
#     blocking sbatch --wait (Ctrl-b d to detach, `tmux a -t probe` to return);
#   * reports: epoch total wallclock, SDF GiB written, achieved bandwidth,
#     and the last measurements.jsonl entry.
#
# Answers before the controller starts: how long is ONE 3D LWFA simulation
# (-> size cluster.time_limit), is the deck accepted by epoch3d_lstr, and
# what is the per-candidate Lustre footprint.
# =============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

usage() { sed -n '3,29p' "$0" | sed 's/^# \{0,1\}//'; }

DRY="" ; SIF="container/plasma_pp.sif" ; CAND="examples/candidate_romio.json"
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY=1 ;;
        --image)     SIF="$2"; shift ;;
        --candidate) CAND="$2"; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "timing_probe: unknown option '$1' (--help)" >&2; exit 2 ;;
    esac
    shift
done

[ -f "$CAND" ] || { echo "timing_probe: candidate '$CAND' not found" >&2; exit 1; }
[ -f config.generated.json ] || python3 tools/compile_config.py

# ---- 1. overlay: the ONLY deviations from the production config ------------
python3 - "$SIF" "$CAND" "${DRY:+dry}" <<'PYEOF'
import json, pathlib, sys
sif = pathlib.Path(sys.argv[1])
mode = "dry-run (nothing will be submitted)" if len(sys.argv) > 3 else "REAL submission"
if len(sys.argv) == 3 and not sif.is_file():
    sys.exit(f"timing_probe: image '{sif}' not found")   # preview may proceed without it
cfg = json.loads(pathlib.Path("config.generated.json").read_text())
cfg.setdefault("container", {})["image"] = str(sif.resolve())
fit = cfg.setdefault("fitness", {})
fit["repetitions"] = 1
fit["repeat_mode"] = "in_job"
pathlib.Path("timing_probe.generated.json").write_text(json.dumps(cfg, indent=2))
ws = cfg.get("workspace", {})
print(f"mode         = {mode}")
print(f"image        = {cfg['container']['image']}")
print(f"candidate    = {sys.argv[2]}")
print(f"time_limit   = {cfg.get('cluster', {}).get('time_limit')}")
print(f"state dir    = {pathlib.Path(ws.get('root', '.')) / ws.get('state_dir', 'ppio_tune')}")
PYEOF

# ---- 2. dry-run: render and show, no cluster involved -----------------------
if [ -n "$DRY" ]; then
    echo "==> dry-run: rendering submit.sh (no submission)"
    python3 evaluate.py -c "$CAND" --config timing_probe.generated.json --dry-run
    RUNDIR=$(python3 <<'PYEOF'
import json, pathlib
st = json.load(open("timing_probe.generated.json")).get("workspace", {}).get("state_dir", "ppio_tune")
roots = [pathlib.Path(json.load(open("timing_probe.generated.json")).get("workspace", {}).get("root", ".")) / st,
         pathlib.Path(".dev_state") / st]
runs = sorted((r for root in roots for r in root.glob("runs/*")),
              key=lambda p: p.stat().st_mtime, reverse=True)
print(runs[0] if runs else "")
PYEOF
)
    [ -n "$RUNDIR" ] && { echo "--- generated $RUNDIR/submit.sh ---"; cat "$RUNDIR/submit.sh"; }
    exit 0
fi

# ---- 3. real run: needs Slurm, wants tmux -----------------------------------
command -v sbatch >/dev/null 2>&1 || {
    echo "timing_probe: sbatch not found -- run this ON THE LOGIN NODE" >&2
    echo "              (or preview anywhere with: $0 --dry-run)" >&2
    exit 1
}
if [ -z "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
    echo "==> re-launching inside tmux session 'probe' (Ctrl-b d detaches)"
    exec tmux new-session -s probe -- "$0" --image "$SIF" --candidate "$CAND"
fi

echo "==> submitting ONE real evaluation via sbatch --wait (blocking)..."
rc=0
python3 evaluate.py -c "$CAND" --config timing_probe.generated.json || rc=$?

# ---- 4. report ----------------------------------------------------------------
echo "==> evaluate.py exit code: $rc"
STATE=$(python3 -c "import json,pathlib; c=json.load(open('timing_probe.generated.json')); w=c.get('workspace',{}); print(pathlib.Path(w.get('root','.'))/w.get('state_dir','ppio_tune'))")
RUNLOG=$(ls -t "$STATE"/runs/*/stdout.log 2>/dev/null | head -1 || true)
if [ -n "${RUNLOG:-}" ]; then
    echo "--- timing lines from $RUNLOG ---"
    grep -E "epoch total wallclock|aggregate write bandwidth|wrote .* GiB" "$RUNLOG" \
        || echo "(no timing lines -- likely a deck/binary failure; inspect: $(dirname "$RUNLOG"))"
fi
echo "--- last measurements ---"
tail -n 2 "$STATE/measurements.jsonl" 2>/dev/null || echo "(no ledger entry)"
