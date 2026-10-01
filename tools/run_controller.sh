#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- start the OpenEvolve evolutionary loop (login node, tmux!)
#
# Runs the controller from the Lustre-based venv (.controller_env). It spawns
# evaluate.py per candidate, so this process needs the full login-node trio:
# sbatch client, lfs, and network access to the LLM proxy on the GPU cluster.
#
# Usage:
#   tmux new -s evolve
#   MPIIO_EVOLVE_STATE_DIR=$USER ./tools/run_controller.sh openevolve_config.yaml
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="${MPIIO_EVOLVE_ENV_DIR:-$REPO_ROOT/.controller_env}"
CFG="${1:?usage: run_controller.sh <openevolve-config.yaml>}"

if [[ ! -x "$ENV_DIR/bin/openevolve" ]]; then
    echo "OpenEvolve is not installed in $ENV_DIR." >&2
    echo "Run first:  ./tools/bootstrap_controller.sh --openevolve" >&2
    exit 1
fi

# Keep all controller-side state on Lustre, off $HOME.
export HOME="$REPO_ROOT/.fake_home"
export TMPDIR="$REPO_ROOT/tmp"
export XDG_CACHE_HOME="$REPO_ROOT/.cache"

# LLM endpoint: GPU-cluster llama.cpp behind the uvicorn proxy on the GPU
# login node. NO_PROXY is MANDATORY on ccsub*: without it the corporate
# Squid proxy silently swallows the requests (error page instead of JSON).
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}ccdev0022.hpc.gsi.de,localhost,127.0.0.1"
export no_proxy="$NO_PROXY"
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://ccdev0022.hpc.gsi.de:8781/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-unused}"

# One state directory per launcher/user under the deployment root.
export MPIIO_EVOLVE_STATE_DIR="${MPIIO_EVOLVE_STATE_DIR:-${USER:-default}}"

echo "mpiio_evolve controller: state=$MPIIO_EVOLVE_STATE_DIR  llm=$OPENAI_API_BASE"
echo "config: $CFG"

cd "$REPO_ROOT"
exec nice -n 5 "$ENV_DIR/bin/python" "$REPO_ROOT/tools/launch_evolution.py" "$CFG"
