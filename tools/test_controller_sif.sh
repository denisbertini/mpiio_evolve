#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- controller SIF smoke tests
#
# Cheap checks first, cluster involvement last:
#   [1] image content : slurm/python binaries present, libs resolvable
#   [2] openevolve    : venv interpreter, full dependency import, library
#                       API (from openevolve import OpenEvolve), console
#                       script, this repo's openevolve_config.yaml parses
#   [3] sinfo         : slurm client + munge against the live slurmctld
#   [4] sbatch  NO --contain : trivial 2-min job, full bind set
#   [5] sbatch  WITH --contain : the run_controller_sif.sh wall (state rw,
#                       repo ro, /etc/slurm ro) -- proves mungepath works
#   [6] evaluator     : evaluate.py --dry-run inside the container (no jobs)
#
# Usage:
#   ./tools/test_controller_sif.sh [path/to/controller.sif]
#   optional: MPIIO_TEST_PARTITION=main MPIIO_TEST_ACCOUNT=...
#             MPIIO_TEST_SKIP_SUBMIT=1  (skip tests 4+5, e.g. noisy queue)
#
# Exit 0 when everything PASSes (SKIPs allowed); 1 on any FAIL.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- locate the SIF: arg > env > images/ > container/ -----------------------
SIF="${1:-${MPIIO_EVOLVE_CONTROLLER_SIF:-}}"
if [[ -z "$SIF" ]]; then
    for cand in "$REPO_ROOT/images/controller.sif" "$REPO_ROOT/container/controller.sif"; do
        [[ -f "$cand" ]] && { SIF="$cand"; break; }
    done
fi
if [[ ! -f "$SIF" ]]; then
    echo "ERROR: controller SIF not found (arg, MPIIO_EVOLVE_CONTROLLER_SIF," >&2
    echo "       images/controller.sif, container/controller.sif all missing)." >&2
    exit 2
fi

command -v apptainer >/dev/null 2>&1 || { echo "ERROR: no apptainer on this node." >&2; exit 2; }

STATE="${MPIIO_EVOLVE_DEPLOY_ROOT:-/lustre/rz/dbertini2}/ppio_tune"
TMPD="$STATE/controller_smoketest"
mkdir -p "$TMPD" || { echo "ERROR: cannot create $TMPD (not on the cluster?)" >&2; exit 2; }

VPY=/venv/controller/bin/python           # interpreter inside the image

# bind sets -------------------------------------------------------------------
# The cluster's /etc/slurm/plugstack.conf.d/singularity-exec.conf makes
# sbatch dlopen the host SPANK plugin at /usr/libexec/slurm-singularity-exec.so
# (client-side plugin-stack init).  The .so lives on the login node only ->
# bind it read-only, same pattern as the munge socket.  Do NOT bake it into
# the image (%files): it must track the cluster's version.
SPANK_PLUGIN=/usr/libexec/slurm-singularity-exec.so
EXTRA_BIND=()
[[ -e "$SPANK_PLUGIN" ]] && EXTRA_BIND+=("-B $SPANK_PLUGIN:ro")

# FULL: everything a slurm client can want (same set validated on ccdev0002).
FULL_BIND=(-B /usr/lib64/slurm -B /etc/slurm -B /var/run/munge
           -B /var/spool/slurm -B /var/lib/sss/pipes
           -B "$STATE" -B "$REPO_ROOT:$REPO_ROOT:ro"
           "${EXTRA_BIND[@]}")
# CONFINED: exactly the run_controller_sif.sh wall.  Munge socket is expected
# from apptainer.conf 'mungepath'; test [5] is what proves or refutes it.
CONF_BIND=(-B "$STATE" -B "$REPO_ROOT:$REPO_ROOT:ro" -B /etc/slurm:/etc/slurm:ro
           "${EXTRA_BIND[@]}")

declare -i npass=0 nfail=0 nskip=0
ok()   { echo "  [PASS] $*"; npass+=1; }
bad()  { echo "  [FAIL] $*"; nfail+=1; }
skip() { echo "  [SKIP] $*"; nskip+=1; }

# check DESC shell-cmd... : run, PASS on rc 0, print first output line on fail
check() {
    local desc="$1"; shift
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [[ $rc -eq 0 ]]; then
        ok "$desc"
    else
        bad "$desc (rc=$rc): $(echo "$out" | grep -v '^$' | head -n1)"
    fi
    echo "$out" | sed 's/^/         | /' | head -n 4
}

echo "=============================================================="
echo " mpiio_evolve controller SIF smoke tests"
echo "   sif   : $SIF"
echo "   state : $STATE"
echo "   repo  : $REPO_ROOT (bound ro)"
echo "=============================================================="

# ---- [1] image content -------------------------------------------------------
echo "-- [1] image content"
check "bins: sbatch srun squeue sinfo python present" \
    apptainer exec "$SIF" bash -c \
    'for b in sbatch srun squeue sinfo python; do command -v $b >/dev/null || { echo "missing: $b"; exit 1; }; done'
check "auth_munge plugin present" \
    apptainer exec "$SIF" test -f /usr/lib64/slurm/auth_munge.so
check "unversioned libslurmfull.so loadable (%files fallback)" \
    apptainer exec "$SIF" bash -c \
    'ldconfig -p | grep -q "libslurmfull.so " || test -f /usr/lib64/libslurmfull.so'

# ---- [2] openevolve ----------------------------------------------------------
echo "-- [2] openevolve install"
check "venv interpreter (python >= 3.10)" \
    apptainer exec "$SIF" "$VPY" -c \
    'import sys; assert sys.version_info >= (3,10), sys.version; print(sys.version.split()[0])'
check "openevolve + full dependency tree imports" \
    apptainer exec "$SIF" "$VPY" -c \
    'from openevolve import OpenEvolve
import openai, yaml, numpy, tqdm, flask, dacite, cloudpickle'
check "openevolve version reports" \
    apptainer exec "$SIF" "$VPY" -c \
    'import importlib.metadata as m; print("openevolve", m.version("openevolve"))'
check "console script (openevolve-run or openevolve)" \
    apptainer exec "$SIF" bash -c \
    'command -v openevolve-run >/dev/null || command -v openevolve >/dev/null'
check "repo openevolve_config.yaml parses" \
    apptainer exec -B "$REPO_ROOT:$REPO_ROOT:ro" "$SIF" "$VPY" -c \
    "import yaml; c=yaml.safe_load(open('$REPO_ROOT/openevolve_config.yaml')); assert isinstance(c, dict) and c; print('keys:', ', '.join(list(c)[:6]))"

# ---- [3] sinfo (live controller, full binds) ---------------------------------
echo "-- [3] slurm client against live slurmctld (full binds)"
check "sinfo returns a partition table" \
    apptainer exec "${FULL_BIND[@]}" "$SIF" bash -c \
    'sinfo -h -o "%P %a %l %D" | grep -q up'

# ---- trivial job -------------------------------------------------------------
JOB="$TMPD/smoke_hi.sh"
{
    echo '#!/bin/bash'
    echo '#SBATCH -J mpiio_sif_smoke'
    echo '#SBATCH -t 00:02:00'
    [[ -n "${MPIIO_TEST_PARTITION:-}" ]] && echo "#SBATCH -p $MPIIO_TEST_PARTITION"
    [[ -n "${MPIIO_TEST_ACCOUNT:-}"   ]] && echo "#SBATCH -A $MPIIO_TEST_ACCOUNT"
    echo 'echo "mpiio_smoke host=$(hostname) uid=$(id -u) job=$SLURM_JOB_ID"'
} > "$JOB"

submit_wait() {  # $@ = apptainer bind/contain args; submits from $TMPD
    # (writable cwd for the default slurm-<id>.out; env -C keeps this
    #  shell's counters intact -- a '( cd ... && ... )' subshell would
    #  eat the PASS/FAIL increments of check)
    env -C "$TMPD" timeout "${MPIIO_TEST_TIMEOUT:-600}" \
        apptainer exec "$@" "$SIF" \
        sbatch --wait --quiet "$JOB"
}

# ---- [4] sbatch WITHOUT --contain (full binds) -------------------------------
echo "-- [4] sbatch --wait, NO --contain (full binds)"
if [[ "${MPIIO_TEST_SKIP_SUBMIT:-0}" == "1" ]]; then
    skip "submission tests (MPIIO_TEST_SKIP_SUBMIT=1)"
else
    # default job stdout (slurm-<id>.out) lands in the submission cwd ($TMPD)
    check "job ran and returned" submit_wait "${FULL_BIND[@]}"
fi

# ---- [5] sbatch WITH --contain (run_controller_sif.sh wall) ------------------
echo "-- [5] sbatch --wait, WITH --contain (state rw / repo ro / /etc/slurm ro)"
if [[ "${MPIIO_TEST_SKIP_SUBMIT:-0}" == "1" ]]; then
    skip "confined submission"
else
    prev=$nfail
    check "confined job ran and returned" submit_wait --contain "${CONF_BIND[@]}"
    if (( nfail > prev )); then
        echo "         -> if it mentions MUNGESOCKET: add '-B /var/run/munge'"
        echo "            to APPTAINER_BINDPATH in tools/run_controller_sif.sh"
        echo "            (apptainer.conf 'mungepath' not active here)."
        echo "         -> if it still mentions slurm-singularity-exec.so: the"
        echo "            plugin's own library deps are missing in the image;"
        echo "            run  ldd /usr/libexec/slurm-singularity-exec.so"
        echo "            on the host and bind the missing libs ro as well."
    fi
fi

# ---- [6] evaluator dry-run inside the container ------------------------------
echo "-- [6] evaluate.py --dry-run inside the container (no jobs submitted)"
out="$(apptainer exec --contain "${CONF_BIND[@]}" "$SIF" "$VPY" "$REPO_ROOT/evaluate.py" \
       --candidate "$REPO_ROOT/examples/candidate_romio.json" --dry-run 2>&1)"
if grep -q "FITNESS:" <<<"$out"; then
    ok "dry-run printed a FITNESS line"
    echo "$out" | grep "FITNESS:" | sed 's/^/         | /' | head -n 2
else
    bad "dry-run produced no FITNESS line"
    echo "$out" | tail -n 5 | sed 's/^/         | /'
fi

# ---- summary -----------------------------------------------------------------
echo "=============================================================="
echo " result: $npass PASS, $nfail FAIL, $nskip SKIP"
echo "=============================================================="
[[ $nfail -eq 0 ]]
