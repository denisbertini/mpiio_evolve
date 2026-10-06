#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- run the controller CONFINED inside the controller SIF.
#
# The image (container/controller.def -> images/controller.sif) carries
# OpenEvolve (+ its full pinned dependency tree) + a version-matched Slurm
# client.  No Lustre client: set workspace.lustre_strict: false to tune
# MPI-IO hints from the container (see container/controller.def %help).
# With --contain the
# container sees ONLY:
#   * ppio_tune          rw   same path  (all mutable state)
#   * the repo           ro   same path  (code cannot modify itself)
#   * /etc/slurm         ro              (client needs slurmctld address)
#   * munge socket       ro  (bound explicitly by this script -- this
#                        cluster's apptainer.conf has no active 'mungepath')
# $HOME and the rest of /lustre are INVISIBLE.  Submitted benchmark jobs
# run outside this container on compute nodes and are unaffected.
#
# Usage:
#   tmux new -s evolve
#   ./tools/run_controller_sif.sh [openevolve_config.yaml]
#
# Build the image first (github/internet build node or via proxy).  The
# Slurm client defaults to the cluster's 26.05.4; override with
# --build-arg SLURM_VERSION=<x.yy.z> only for a cluster running another:
#   sudo http_proxy=$PROXY https_proxy=$PROXY \
#     apptainer build images/controller.sif container/controller.def
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIF="${MPIIO_EVOLVE_CONTROLLER_SIF:-}"
if [[ -z "$SIF" ]]; then
    for cand in "$REPO_ROOT/images/controller.sif" "$REPO_ROOT/container/controller.sif"; do
        [[ -f "$cand" ]] && { SIF="$cand"; break; }
    done
    SIF="${SIF:-$REPO_ROOT/images/controller.sif}"   # for the error message
fi
CFG="${1:-openevolve_config.yaml}"

if [[ ! -f "$SIF" ]]; then
    echo "controller SIF not found: $SIF" >&2
    echo "  build:  sudo apptainer build images/controller.sif container/controller.def" >&2
    echo "  (check the Slurm VERSION= in the .def against 'sbatch --version' first)" >&2
    exit 1
fi

STATE_ROOT="${MPIIO_EVOLVE_DEPLOY_ROOT:-/lustre/rz/dbertini2}"
STATE="$STATE_ROOT/ppio_tune"

# Writable CWD outside the read-only repo: OpenEvolve's default
# ./openevolve_output (checkpoints, logs) lands HERE, not on :ro code.
RUNDIR="$STATE/controller_run"
mkdir -p "$RUNDIR" "$STATE/.fake_home" "$STATE/tmp"

# NOTE: under --contain the container's cwd is $HOME (the fake home), NOT
# wherever this script cd'd -- so the output dir cannot be derived from cwd
# inside the container; pass it explicitly (host env propagates through
# 'apptainer exec').
export MPIIO_EVOLVE_OUTPUT_DIR="$RUNDIR/openevolve_output"

# LLM endpoint (identical semantics to the bare-metal run_controller.sh).
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}ccdev0022.hpc.gsi.de,localhost,127.0.0.1"
export no_proxy="$NO_PROXY"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://ccdev0022.hpc.gsi.de:8781/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-unused}"

# One state directory per launcher/user under the deployment root.
export MPIIO_EVOLVE_STATE_DIR="${MPIIO_EVOLVE_STATE_DIR:-${USER:-default}}"

# Apptainer plumbing: same lessons as the benchmark launcher (no $HOME).
export APPTAINER_CONFIGDIR="/tmp/${USER}"
export APPTAINER_TMPDIR="/tmp/${USER}"
export APPTAINER_HOME="$STATE/.fake_home"
mkdir -p "$APPTAINER_CONFIGDIR"
export TMPDIR="$STATE/tmp"

# ---- the confinement wall: state rw, code ro, slurm conf.  Nothing else. ----
export APPTAINER_BINDPATH="$STATE,$REPO_ROOT:${REPO_ROOT}:ro,/etc/slurm:/etc/slurm:ro"

# munge socket: this cluster's apptainer.conf has NO active 'mungepath', so
# --contain hides it and sbatch dies with 'Munge encode failed /
# /var/run/munge/munge.socket.2: No such file or directory' (proven by
# test [5] of tools/test_controller_sif.sh).  Bind it explicitly.
if [[ -S /var/run/munge/munge.socket.2 ]]; then
    APPTAINER_BINDPATH="$APPTAINER_BINDPATH,/var/run/munge/munge.socket.2:/var/run/munge/munge.socket.2:ro"
fi

# sbatch initializes the SPANK plugin stack CLIENT-side from the cluster's
# /etc/slurm/plugstack.conf.d/*.conf; RLX lists
# /usr/libexec/slurm-singularity-exec.so there, which exists on the host but
# not in the image -> sbatch aborts ("Failed to initialize plugin stack")
# unless the .so is bound read-only, same pattern as the munge socket.
if [[ -e /usr/libexec/slurm-singularity-exec.so ]]; then
    APPTAINER_BINDPATH="$APPTAINER_BINDPATH,/usr/libexec/slurm-singularity-exec.so:/usr/libexec/slurm-singularity-exec.so:ro"
fi

echo "mpiio_evolve CONTAINER controller"
echo "  sif   : $SIF"
echo "  state : $STATE (dir: $MPIIO_EVOLVE_STATE_DIR)"
echo "  cwd   : $RUNDIR   (openevolve_output lands here)"
echo "  llm   : $OPENAI_API_BASE"
echo "  binds : $APPTAINER_BINDPATH"

cd "$RUNDIR"
exec nice -n 5 apptainer exec --contain "$SIF" \
    python "$REPO_ROOT/tools/launch_evolution.py" "$CFG"
