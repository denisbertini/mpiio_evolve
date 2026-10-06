#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- BOUNDED test run of the containerized controller.
#
# Derives throwaway test-profile copies of BOTH configs from the committed
# production ones (no hand-editing, nothing to remember to revert):
#
#   openevolve_config.yaml -> max_iterations: 2   (stop after 2 generations)
#   config.yaml            -> repetitions: 1      (one benchmark rep per eval)
#
# The copies live on the rw state dir (the repo is read-only inside the
# container) and are wired in via:
#   MPIIO_EVOLVE_CONFIG       -> evaluate.py (+ its subprocesses)
#   argument to the runner    -> OpenEvolve config
#   MPIIO_EVOLVE_OUTPUT_DIR   -> separate checkpoint/log tree for tests
#
# Usage:
#   tmux new -s evolve_test
#   ./tools/run_controller_test.sh
#
# Real runs are unaffected: just use ./tools/run_controller_sif.sh.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${MPIIO_EVOLVE_DEPLOY_ROOT:-/lustre/rz/dbertini2}/ppio_tune"
PROFILE="$STATE/controller_test"
mkdir -p "$PROFILE"

OE_TEST="$PROFILE/openevolve_config.test.yaml"
MPI_TEST="$PROFILE/config.test.yaml"

sed -E 's/^max_iterations:.*/max_iterations: 2               # TEST PROFILE/' \
    "$REPO_ROOT/openevolve_config.yaml" > "$OE_TEST"
sed -E 's/^  repetitions:.*/  repetitions: 1                # TEST PROFILE/' \
    "$REPO_ROOT/config.yaml" > "$MPI_TEST"

echo "mpiio_evolve TEST PROFILE (bounded run):"
echo "  openevolve: $(grep -m1 '^max_iterations' "$OE_TEST")"
echo "  evaluation: $(grep -m1 '  repetitions' "$MPI_TEST")"
echo "  configs   : $OE_TEST"
echo "              $MPI_TEST"
echo "  output    : $PROFILE/openevolve_output"
echo

export MPIIO_EVOLVE_CONFIG="$MPI_TEST"
export MPIIO_EVOLVE_OUTPUT_DIR="$PROFILE/openevolve_output"
exec "$REPO_ROOT/tools/run_controller_sif.sh" "$OE_TEST"
