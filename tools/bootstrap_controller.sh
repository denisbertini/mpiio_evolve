#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- controller/launcher environment builder (login node)
#
# Builds the self-contained Python environment for the launcher + OpenEvolve
# controller entirely on the Lustre workspace. Every cache (HOME, TMPDIR,
# XDG, pip, uv) is pinned inside the deployment tree before pip is ever
# invoked -- the login-node $HOME is never written to, keeping the whole
# deployment relocatable on /lustre.
#
# Usage:
#   ./tools/bootstrap_controller.sh                  # venv + pip (+ best-effort PyYAML)
#   ./tools/bootstrap_controller.sh --openevolve     # also install OpenEvolve
#   ./tools/bootstrap_controller.sh --python python3.12
#   ./tools/bootstrap_controller.sh --state-dir alice   # pre-create state dir
#
# State directories live under the deployment root (config workspace.root,
# default /lustre/rz/dbertini2): <root>/<state_dir>/{runs,tmp,.fake_home}.
#
# Then start the evolutionary loop (inside tmux -- it is long-lived):
#   tmux new -s evolve
#   ./tools/run_controller.sh openevolve_config.yaml
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="${MPIIO_EVOLVE_ENV_DIR:-$REPO_ROOT/.controller_env}"
PY=python3
WITH_OPENEVOLVE=0
STATE_DIR="${MPIIO_EVOLVE_STATE_DIR:-${USER:-default}}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --openevolve) WITH_OPENEVOLVE=1 ;;
        --python)     shift; PY="${1:?--python requires a path}" ;;
        --state-dir)  shift; STATE_DIR="${1:?--state-dir requires a name}" ;;
        -h|--help)    sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# Deployment root: config value unless overridden (same precedence as evaluate.py)
DEPLOY_ROOT="$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from evaluate import load_config, resolve_workspace
    ws = resolve_workspace(load_config(), create=False)
    print(ws.parent)
except Exception:
    print("")
PY
)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/lustre/rz/dbertini2}"

# ---- $HOME-free caches: same discipline as the container builder -----------
export HOME="$REPO_ROOT/.fake_home"
export TMPDIR="$REPO_ROOT/tmp"
export XDG_CACHE_HOME="$REPO_ROOT/.cache"
export PIP_CACHE_DIR="$REPO_ROOT/.pip_cache"
export UV_CACHE_HOME="$REPO_ROOT/.uv_cache"
mkdir -p "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR"

echo "=============================================="
echo " mpiio_evolve controller environment"
echo "   repo        : $REPO_ROOT"
echo "   deploy root : $DEPLOY_ROOT"
echo "   state dir   : $DEPLOY_ROOT/$STATE_DIR"
echo "   venv        : $ENV_DIR"
echo "   python      : $PY ($("$PY" --version 2>&1))"
echo "   caches      : HOME=$HOME"
echo "                 PIP=$PIP_CACHE_DIR  TMP=$TMPDIR"
echo "=============================================="

# ---- create the state directory (idempotent, if the root is reachable) ------
if ! mkdir -p "$DEPLOY_ROOT/$STATE_DIR" 2>/dev/null; then
    echo "  [warn] cannot create $DEPLOY_ROOT/$STATE_DIR (not on the cluster?)"
    echo "         dry-runs will fall back to $REPO_ROOT/.dev_state/$STATE_DIR"
fi

# ---- create venv (idempotent) -------------------------------------------------
if [[ ! -x "$ENV_DIR/bin/python" ]]; then
    "$PY" -m venv "$ENV_DIR" || {
        echo "ERROR: '$PY -m venv' failed." >&2
        echo "       On EL9/Rocky this usually means ensurepip is missing:" >&2
        echo "       ask the admin for python3-pip, or pass a newer python3" >&2
        echo "       (e.g. --python python3.12 if a module provides one)." >&2
        exit 1
    }
    echo "created venv"
else
    echo "venv already exists -- reusing (idempotent)"
fi
VPY="$ENV_DIR/bin/python"
PIP="$ENV_DIR/bin/pip"

# Lustre: a venv is tens of thousands of small files -- spread it over OSTs so
# concurrent import/stat traffic is not pinned to one target. No-op elsewhere.
if command -v lfs >/dev/null 2>&1; then
    lfs setstripe -c 4 "$ENV_DIR" 2>/dev/null \
        && echo "set Lustre stripe (-c 4) on venv dir" || true
fi

# ---- packages ------------------------------------------------------------------
"$PIP" install --upgrade pip wheel setuptools >/dev/null
echo "  [ok] pip        : $("$PIP" --version | awk '{print $2}')"

# PyYAML is a pure nicety: the evaluator works without it (simple_yaml.py and
# config.generated.json), so a failure here is a warning, never fatal.
if "$PIP" install PyYAML >/dev/null 2>&1; then
    echo "  [ok] PyYAML     : $("$VPY" -c 'import yaml; print(yaml.__version__)')"
else
    echo "  [warn] PyYAML install failed (offline?) -- evaluator will use the"
    echo "         bundled simple_yaml parser; nothing is broken."
fi

# sanity: the evaluator must run under this interpreter, writing into the
# real state directory when the Lustre root is reachable
MPIIO_EVOLVE_STATE_DIR="$STATE_DIR" \
    "$VPY" "$REPO_ROOT/evaluate.py" \
    --candidate '{"lustre":{"stripe_count":1,"stripe_size":"1M"}}' \
    --dry-run 2>/dev/null | tail -n1 | sed 's/^/  [ok] evaluator  : /'

if [[ "$WITH_OPENEVOLVE" == "1" ]]; then
    # OpenEvolve pulls a large dependency tree (numpy/pandas/matplotlib/
    # openai...): through a proxy this takes several minutes. It MUST print
    # progress -- a silent multi-minute step looks exactly like a hang.
    echo "==> installing openevolve via pip (large dependency tree, be patient)..."
    if "$PIP" install openevolve; then
        :
    else
        # Fallback to GitHub -- but github is firewalled from cluster logins,
        # so bound it: batch mode (no credential prompt) + hard timeout, and
        # fail loudly instead of black-holing on a dropped SYN.
        echo "    pip/PyPI install failed -- trying GitHub (needs github access; 3 min cap)..."
        GIT_TERMINAL_PROMPT=0 ${TMO:-timeout} 180 "$PIP" install \
            "git+https://github.com/codelion/openevolve.git" \
            || { echo "ERROR: openevolve install FAILED (see pip output above)." >&2
                 echo "       Likely: PyPI wheel unavailable AND github blocked" >&2
                 echo "       from this login. Bootstrap from a github-capable" >&2
                 echo "       login (shared Lustre venv) or ask for a PyPI mirror." >&2
                 exit 1; }
    fi
    echo "  [ok] openevolve : $("$ENV_DIR/bin/openevolve" --version 2>/dev/null || echo installed)"
fi

cat <<EOF

Done. Everything lives on Lustre; \$HOME was not written to.

  Launcher under this interpreter:
    MPIIO_EVOLVE_STATE_DIR=$STATE_DIR $VPY evaluate.py \\
        --candidate examples/candidate_romio.json --dry-run

  Evolutionary loop (login node, inside tmux):
    tmux new -s evolve
    MPIIO_EVOLVE_STATE_DIR=$STATE_DIR ./tools/run_controller.sh openevolve_config.yaml
EOF
