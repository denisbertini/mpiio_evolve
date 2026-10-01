#!/usr/bin/env bash
# =============================================================================
# tools/timing_probe.sh -- ONE real simulation, measured.
#
# Purpose: before unleashing the evolution controller, run the 3D LWFA
# fitness benchmark exactly ONCE through the REAL pipeline (same submit.sh
# generation, env header, striping, hint file, bare srun) using an
# already-built image, to find out:
#   * how long one simulation takes (calibrate cluster.time_limit),
#   * whether the deck is accepted by epoch3d_lstr (ndims guard!),
#   * the SDF volume per rep (quota sizing),
#   * and that the container/exec wiring works end to end.
#
# Run this ON THE LOGIN NODE, inside tmux (it blocks until the job ends):
#   ./tools/timing_probe.sh [image.sif] [candidate.json]
# Defaults: container/plasma_pp.sif   examples/candidate_romio.json
#
# It overrides ONLY three config keys via a JSON overlay (never edits
# config.yaml): container.image -> given .sif, fitness.repetitions -> 1,
# fitness.repeat_mode -> in_job. Everything else -- cluster, search space,
# profile -- is exactly what the evolution loop would use.
# =============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SIF="${1:-container/plasma_pp.sif}"
CAND="${2:-examples/candidate_romio.json}"
PROBE_CFG="$REPO/timing_probe.generated.json"

command -v sbatch >/dev/null 2>&1 || {
    echo "timing_probe: sbatch not found -- this probe runs on the LOGIN node." >&2
    echo "              (on a laptop use: python3 evaluate.py --dry-run)" >&2
    exit 1
}
[ -f "$SIF" ]  || { echo "timing_probe: image '$SIF' not found" >&2; exit 1; }
[ -f "$CAND" ] || { echo "timing_probe: candidate '$CAND' not found" >&2; exit 1; }
[ -f config.generated.json ] || python3 tools/compile_config.py

python3 - "$SIF" <<'PYEOF'
import json, pathlib, sys
cfg = json.loads(pathlib.Path("config.generated.json").read_text())
cfg.setdefault("container", {})["image"] = str(pathlib.Path(sys.argv[1]).resolve())
fit = cfg.setdefault("fitness", {})
fit["repetitions"] = 1
fit["repeat_mode"] = "in_job"
pathlib.Path("timing_probe.generated.json").write_text(json.dumps(cfg, indent=2))
ws = cfg.get("workspace", {})
print("probe config: image      =", cfg["container"]["image"])
print("              reps       = 1 (in_job)")
print("              time_limit =", cfg.get("cluster", {}).get("time_limit"))
print("              state dir  =", pathlib.Path(ws.get("root", ".")) / ws.get("state_dir", "ppio_tune"))
PYEOF

echo "==> submitting ONE real evaluation via sbatch --wait (stay in tmux)..."
rc=0
python3 evaluate.py -c "$CAND" --config "$PROBE_CFG" || rc=$?

echo "==> evaluate.py exit code: $rc"
# Point at the freshest run log for the wall-clock answer
STATE=$(python3 -c "import json,pathlib; c=json.load(open('$PROBE_CFG')); w=c.get('workspace',{}); print(pathlib.Path(w.get('root','.'))/w.get('state_dir','ppio_tune'))")
RUNLOG=$(ls -t "$STATE"/runs/*/stdout.log 2>/dev/null | head -1 || true)
if [ -n "${RUNLOG:-}" ]; then
    echo "--- timing lines from $RUNLOG ---"
    grep -E "epoch total wallclock|aggregate write bandwidth|wrote .* GiB" "$RUNLOG" || echo "(no timing lines -- check the log, likely a deck/binary failure: $(dirname "$RUNLOG"))"
fi
echo "--- measurements ledger ---"
tail -n 2 "$STATE/measurements.jsonl" 2>/dev/null || echo "(no ledger entry this run)"
