#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- Apptainer image builder (run on the Virgo2 LOGIN NODE)
#
# Builds container/plasma_pp.def (OpenMPI 5.0.7/romio341 + UCX + Lustre
# client + HDF5 + ADIOS2 + openPMD + EPOCH + WarpX + IOR + OSU) entirely
# inside the Lustre workspace (repo + caches):
#
#   * synthetic HOME            -> .fake_home/
#   * build temp                -> tmp/
#   * apptainer download cache  -> .apptainer_cache/
#   * build log                 -> logs/container_build-*.log
#
# The host has NO writable user home, so every apptainer-relevant path is
# exported BEFORE the runtime is ever invoked.
#
# Usage:
#   ./container/build_container.sh              # auto: root -> plain,
#                                               #       else --fakeroot
#   ./container/build_container.sh --sudo       # sudo <runtime> build
#   ./container/build_container.sh --fakeroot
#   ./container/build_container.sh --plain      # unprivileged, no fakeroot
#
# Output: images/plasma_pp-<YYYYmmdd-HHMM>_<githash>.sif
#         + refreshed symlink images/current.sif  (what config.yaml points at)
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEF_FILE="$REPO_ROOT/container/plasma_pp.def"
IMAGES_DIR="$REPO_ROOT/images"
LOG_DIR="$REPO_ROOT/logs"

# ---- hard $HOME isolation for the build itself -----------------------------
export HOME="$REPO_ROOT/.fake_home"
export TMPDIR="$REPO_ROOT/tmp"
export APPTAINER_CACHEDIR="$REPO_ROOT/.apptainer_cache"
export SINGULARITY_CACHEDIR="$APPTAINER_CACHEDIR"
export APPTAINER_TMPDIR="$REPO_ROOT/tmp"
export SINGULARITY_TMPDIR="$REPO_ROOT/tmp"
export XDG_CACHE_HOME="$REPO_ROOT/.cache"
mkdir -p "$HOME" "$TMPDIR" "$APPTAINER_CACHEDIR" "$XDG_CACHE_HOME" \
         "$IMAGES_DIR" "$LOG_DIR"

# ---- runtime detection ------------------------------------------------------
if command -v apptainer >/dev/null 2>&1; then
    RUNTIME=apptainer
elif command -v singularity >/dev/null 2>&1; then
    RUNTIME=singularity
else
    echo "ERROR: neither 'apptainer' nor 'singularity' found on this node." >&2
    echo "       Load the module first, e.g.:  module load apptainer" >&2
    exit 1
fi

# ---- privilege mode ----------------------------------------------------------
# %post runs dnf + source builds, so the build needs root: real root,
# --fakeroot (unprivileged user namespaces), or sudo.
MODE="auto"
case "${1:-}" in
    --sudo)     MODE="sudo" ;;
    --fakeroot) MODE="fakeroot" ;;
    --plain)    MODE="plain" ;;
    "")         ;;
    *)  echo "Unknown option: $1" >&2; exit 2 ;;
esac
if [[ "$MODE" == "auto" ]]; then
    if [[ $EUID -eq 0 ]]; then
        MODE="plain"
    elif "$RUNTIME" build --fakeroot --help >/dev/null 2>&1; then
        MODE="fakeroot"
    else
        MODE="sudo"
    fi
fi

TAG="$(date +%Y%m%d-%H%M)"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo local)"
SIF="$IMAGES_DIR/plasma_pp-${TAG}_${GIT_SHA}.sif"
LOG="$LOG_DIR/container_build-${TAG}.log"

echo "=============================================="
echo " mpiio_evolve container build"
echo "   runtime   : $RUNTIME  (mode: $MODE)"
echo "   def file  : $DEF_FILE"
echo "   output    : $SIF"
echo "   log       : $LOG"
echo "   scratch   : HOME=$HOME"
echo "               TMPDIR=$TMPDIR"
echo "               CACHE=$APPTAINER_CACHEDIR"
echo "=============================================="

# ---- build --------------------------------------------------------------------
# NOTE: sudo resets the environment, so re-export the isolation vars through it.
if [[ "$MODE" == "sudo" ]]; then
    sudo env \
        HOME="$HOME" TMPDIR="$TMPDIR" \
        APPTAINER_CACHEDIR="$APPTAINER_CACHEDIR" SINGULARITY_CACHEDIR="$SINGULARITY_CACHEDIR" \
        APPTAINER_TMPDIR="$APPTAINER_TMPDIR"    SINGULARITY_TMPDIR="$SINGULARITY_TMPDIR" \
        "$RUNTIME" build "$DEF_FILE" "$SIF" 2>&1 | tee "$LOG"
elif [[ "$MODE" == "fakeroot" ]]; then
    "$RUNTIME" build --fakeroot "$DEF_FILE" "$SIF" 2>&1 | tee "$LOG"
else
    "$RUNTIME" build "$DEF_FILE" "$SIF" 2>&1 | tee "$LOG"
fi

# ---- publish + symlink ----------------------------------------------------------
ln -sfn "$(basename "$SIF")" "$IMAGES_DIR/current.sif"
echo "Published: $SIF"
echo "Symlink  : images/current.sif -> $(basename "$SIF")"

# ---- post-build validation (same --home trick used for job runs) ---------------
echo "-- validating image --"
"$RUNTIME" exec --home "$HOME" --bind "$REPO_ROOT" "$SIF" bash -lc '
    rc=0
    for b in mpirun mpiexec srun ior epoch1d epoch1d_lstr epoch2d epoch3d epoch3d_lstr warpx_1d python3 h5pcc adios2-config; do
        if command -v "$b" >/dev/null 2>&1; then echo "  [ok]      $b"; else echo "  [MISSING] $b"; rc=1; fi
    done
    echo "  $(mpirun --version 2>/dev/null | head -n1)"
    echo "  MPI-IO component (image default): OMPI_MCA_io=${OMPI_MCA_io:-unset}"
    exit $rc
' || { echo "WARNING: image validation reported problems (see above)."; }

echo
echo "Done. config.yaml already points at images/current.sif."
echo "Next:  python3 evaluate.py --candidate examples/candidate_romio.json"
