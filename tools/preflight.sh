#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- preflight readiness ladder (login node, stdlib-only)
#
# Tests EVERYTHING the campaign needs, in launch order, and prints a
# PASS/FAIL/WARN/SKIP summary. Safe to run any number of times: writes
# only into a scratch state dir which it cleans up. Never touches $HOME.
#
# Usage:
#   ./tools/preflight.sh            # full ladder (~30 s, no cluster jobs)
#   ./tools/preflight.sh --fast     # skip LLM chat-completion round-trip
#
# Exit code 0 = ready to launch, 1 = at least one hard FAIL.
# =============================================================================
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

# Every external call gets a hard timeout; the script itself may never hang.
T=$(command -v timeout || true)   # empty on exotic systems -> commands run bare

if [ -t 1 ]; then
    G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
    G=""; R=""; Y=""; B=""; N=""
fi
NPASS=0; NFAIL=0; NWARN=0; NSKIP=0
ok()   { echo "  ${G}PASS${N}  $*"; NPASS=$((NPASS+1)); }
bad()  { echo "  ${R}FAIL${N}  $*"; NFAIL=$((NFAIL+1)); }
warn() { echo "  ${Y}WARN${N}  $*"; NWARN=$((NWARN+1)); }
skip() { echo "  ${B}SKIP${N}  $*"; NSKIP=$((NSKIP+1)); }
section() { echo; echo "${B}== $* ==${N}"; }

cfgget() {   # first top-level-ish key value from config.yaml, comments then quotes off
    grep -E "^[[:space:]]*$1:" config.yaml 2>/dev/null | head -1 \
        | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^"//; s/"$//; s/[[:space:]]+$//'
}

HOST=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo "?")
PARTITION=$(cfgget partition); PARTITION=${PARTITION:-main}
TIME_LIMIT=$(cfgget time_limit); TIME_LIMIT=${TIME_LIMIT:-00:45:00}
CONSTRAINT=$(cfgget constraint)
WS_ROOT=$(cfgget root); WS_ROOT=${WS_ROOT:-/lustre/rz/dbertini2}
LLM_BASE=$(grep -m1 -E "^[[:space:]]*base_url:" openevolve_config.yaml 2>/dev/null \
           | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/"//g')
LLM_BASE=${LLM_BASE:-http://ccdev0022.hpc.gsi.de:8781/v1}
LLM_MODEL=$(grep -m1 -E "^[[:space:]]*model:" openevolve_config.yaml 2>/dev/null \
            | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/"//g')

echo "${B}mpiio_evolve preflight${N}  host=$HOST  target-partition=$PARTITION${CONSTRAINT:+/$CONSTRAINT}"
echo "repo=$(pwd)  state-root=$WS_ROOT"

# ---------------------------------------------------------------- 1. basics
section "1. Host basics"
v=$(python3 --version 2>&1) && ok "python3: $v" || bad "python3 missing"
command -v tmux >/dev/null 2>&1 && ok "tmux available ($(tmux -V 2>/dev/null))" \
                                 || warn "tmux missing -- controller must run under tmux/screen"
command -v git >/dev/null 2>&1 && ok "git: $(git --version 2>/dev/null | head -1)" \
                                 || warn "git missing (informational)"
BR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
echo "$BR" | grep -qE "controller-launch|^main$" && ok "branch: $BR" \
                                                 || warn "branch '$BR' (expected feature/controller-launch or main)"
# github.com is firewalled from the virgo4 logins BY POLICY -- a fetch here
# black-holes forever. Skip the freshness check for github remotes unless
# MPIIO_GITHUB=1; sync the shared Lustre checkout from a login that CAN
# reach github (e.g. a hydra login) instead.
ORIGIN_URL=$(git config --get remote.origin.url 2>/dev/null)
if [[ "$ORIGIN_URL" == *github* && "${MPIIO_GITHUB:-0}" != "1" ]]; then
    HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null)
    skip "origin freshness (github blocked from this login by policy; HEAD=$HEAD_SHA -- pull via a hydra login, Lustre is shared)"
else
    GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10" \
        ${T:+$T 20} git fetch -q origin 2>/dev/null \
      && { BEHIND=$(git rev-list --count HEAD..@{upstream} 2>/dev/null || echo "?")
           [ "$BEHIND" = "0" ] && ok "up to date with origin" \
                               || warn "$BEHIND commits behind origin -- sync from a github-capable login"; } \
      || warn "git fetch origin failed/timed out (offline login? fine for running)"
fi

# ------------------------------------------------------- 2. package network
section "2. Python package network (bootstrap dependency)"
env | grep -qiE '^https_proxy=' && ok "proxy env set (${https_proxy:-$HTTPS_PROXY})" \
                                 || warn "no https_proxy exported -- pip may have no route to PyPI"
code=$(curl -m 10 -sS -o /dev/null -w '%{http_code}' https://pypi.org/simple/ 2>/dev/null)
if [ "${code:-000}" = "200" ]; then
    ok "pypi.org reachable via current proxy (HTTP 200)"
else
    bad "pypi.org unreachable via proxy (HTTP $code) -- bootstrap will hang"
    echo "        probing internal mirror candidates (no_proxy list suggests one exists)..."
    FOUND=""
    for u in http://cluster-mirror.hpc.gsi.de/pypi/simple/ \
             https://cluster-mirror.hpc.gsi.de/pypi/simple/ \
             http://cluster-mirror.hpc.gsi.de/simple/ \
             https://cluster-mirror.hpc.gsi.de/pypi/web/simple/ ; do
        c=$(curl -m 6 --noproxy '*' -sS -o /dev/null -w '%{http_code}' "$u" 2>/dev/null)
        [ "$c" = "200" ] && { FOUND=$u; break; }
    done
    if [ -n "$FOUND" ]; then
        ok "internal PyPI mirror alive: $FOUND"
        echo "        fix:  export PIP_INDEX_URL=$FOUND   (then rerun bootstrap)"
    else
        warn "no known mirror path answered -- ask sysadmins for the pip index URL,"
        warn "or bootstrap from a hydra login (shared Lustre venv works from virgo4)"
    fi
fi

# ------------------------------------------------------- 3. LLM endpoint
section "3. LLM endpoint ($LLM_BASE)"
models=$(curl -m 8 --noproxy '*' -sS "$LLM_BASE/models" 2>/dev/null \
         | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(",".join(m.get("id","?") for m in d.get("data",[])))
except Exception: pass' 2>/dev/null)
if [ -n "$models" ]; then
    ok "/v1/models responds -- serving: ${models:0:90}"
    case "$models" in *"$LLM_MODEL"*) ok "configured model id present in /v1/models" ;;
                    *) warn "configured model '$LLM_MODEL' not in /v1/models list" ;; esac
else
    bad "GET $LLM_BASE/models failed (down, wrong port, or Squid intercepted it)"
fi
if [ "$FAST" = "1" ]; then
    skip "chat-completion round-trip (--fast)"
elif command -v python3 >/dev/null 2>&1; then
    python3 - "$LLM_BASE" "$LLM_MODEL" <<'PYEOF'
import json, sys, time, urllib.request
base, model = sys.argv[1], sys.argv[2]
body = json.dumps({
    "model": model,
    "messages": [{"role": "user",
                  "content": 'Reply with exactly this JSON and nothing else: {"ok": true}'}],
    "max_tokens": 2048, "temperature": 0,
}).encode()
req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # bypass Squid
try:
    t0 = time.time()
    with opener.open(req, timeout=120) as r:
        d = json.load(r)
    lat = time.time() - t0
    msg = d["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reason = bool((msg.get("reasoning_content") or "").strip())
    print(f"STAT ok latency={lat:.1f}s content_len={len(content)} reasoning={reason}")
    print(f"HEAD {content[:70]!r}")
    sys.exit(0 if content else 3)
except urllib.error.HTTPError as e:
    print(f"STAT http {e.code} {e.read()[:120]!r}")
except Exception as e:
    print(f"STAT error {type(e).__name__}: {e}")
sys.exit(1)
PYEOF
    rc=$?
    case $rc in
        0) ok "chat completion returned NON-EMPTY content (reasoning budget OK)" ;;
        3) bad "model returned EMPTY content at max_tokens=2048 -- reasoning ate the budget" ;;
        *) bad "chat completion failed -- see STAT line above" ;;
    esac
else
    skip "chat-completion test (no python3)"
fi

# ---------------------------------------------------- 4. Lustre workspace
section "4. Lustre workspace"
if [ -d "$WS_ROOT" ]; then
    tmpf="$WS_ROOT/.preflight_$$"
    if touch "$tmpf" 2>/dev/null; then rm -f "$tmpf"; ok "$WS_ROOT writable"; else bad "$WS_ROOT not writable"; fi
    avail=$(df -h "$WS_ROOT" 2>/dev/null | awk 'NR==2{print $4}')
    [ -n "$avail" ] && ok "free space on $WS_ROOT: $avail"
    command -v lfs >/dev/null 2>&1 && ok "Lustre client present ($(lfs version 2>/dev/null | head -1))" \
                                   || warn "no lfs command (informational)"
else
    bad "workspace root $WS_ROOT does not exist on this host (shared Lustre mount missing?)"
fi

# --------------------------------------------------- 5. repo consistency
section "5. Repository consistency"
missing=0
for f in evaluate.py evaluation_io.py slurm_launcher.py infrastructure.py \
         config.yaml openevolve_config.yaml config.generated.json \
         benchmarks/epoch_io/epoch3d_lwfa.deck benchmarks/epoch_io/run_bench.sh \
         benchmarks/epoch_io/setup_run.sh benchmarks/epoch_io/measure.sh \
         examples/candidate_romio.json; do
    [ -f "$f" ] || { bad "missing: $f"; missing=1; }
done
[ "$missing" = 0 ] && ok "all core files present"
grep -q "t_end = 20\*femto" benchmarks/epoch_io/epoch3d_lwfa.deck 2>/dev/null \
    && ok "deck is POC length (t_end=20 fs)" \
    || warn "deck t_end unexpected: $(grep -m1 t_end benchmarks/epoch_io/epoch3d_lwfa.deck 2>/dev/null)"
if [ -f config.yaml ] && [ -f config.generated.json ]; then
    [ config.yaml -nt config.generated.json ] \
        && warn "config.yaml newer than config.generated.json -- run: python3 tools/compile_config.py" \
        || ok "config.generated.json fresh"
fi
python3 -c 'import yaml' 2>/dev/null \
    && python3 -c "import yaml,simple_yaml,sys
a=yaml.safe_load(open('config.yaml').read()); b=simple_yaml.load(open('config.yaml').read())
sys.exit(0 if a==b else 1)" 2>/dev/null \
        && ok "yaml/stdlib parser parity" || skip "parser parity (PyYAML not on login python -- fine, stdlib path is the target)"

# ------------------------------------------------------------ 6. Slurm
section "6. Slurm scheduling path"
if command -v sbatch >/dev/null 2>&1; then
    ok "sbatch: $(sbatch --version 2>/dev/null | head -1)"
    scontrol ping >/dev/null 2>&1 && ok "slurmctld reachable" || bad "slurmctld unreachable (scontrol ping)"
    sinfo -p "$PARTITION" -h -o "%P" >/dev/null 2>&1 \
        && ok "partition '$PARTITION' exists ($(sinfo -p $PARTITION -h -o '%D nodes %T' 2>/dev/null | head -1))" \
        || bad "partition '$PARTITION' not visible to sinfo"
    if [ -n "$CONSTRAINT" ]; then
        # Slurm >= 23 prints "AvailableFeatures=", older "AvailFeatures" --
        # match both. Feature tokens sit in a comma list introduced by '='
        # and the field ends with a space/newline, so the token boundary is
        # [=,] on the left and [,space-EOL] on the right. (Build the regex
        # OUTSIDE the classes: "[," c "[,]" would swallow c into the class.)
        tot=$(scontrol show nodes -o 2>/dev/null | awk -v c="$CONSTRAINT" '/Avail(able)?Features/ && $0 ~ ("[=,]" c "([,[:space:]]|$)") {n++} END{print n+0}')
        idle=$(sinfo -N -p "$PARTITION" -h -o "%e %t" 2>/dev/null | awk -v c="$CONSTRAINT" '$1 ~ ("(^|,)" c "(,|$)") && $2 ~ /^idle/' | wc -l)
        if [ "${tot:-0}" -gt 0 ]; then
            ok "nodes with feature '$CONSTRAINT': $tot total, $idle idle on $PARTITION"
            [ "${idle:-0}" -eq 0 ] && warn "0 idle $CONSTRAINT nodes now -- first submission will queue"
        else
            bad "no node anywhere advertises feature '$CONSTRAINT' -- wrong cluster or feature renamed"
        fi
    fi
    acct=$(cfgget account)
    if [ -n "$acct" ]; then
        if sacctmgr -nP show assoc where user="$USER" format=account partition 2>/dev/null \
             | grep -qE "\|.*\b$PARTITION\b|\b$acct\b.*\|"; then
            ok "account '$acct' + partition '$PARTITION' in your associations"
        else
            warn "could not confirm account '$acct' for $PARTITION (sacctmgr may be restricted) -- first sbatch will tell"
        fi
    fi
    mine=$(squeue -u "$USER" -h 2>/dev/null | wc -l)
    [ "$mine" -gt 0 ] && warn "you already have $mine job(s) queued -- make sure no stale campaign/probe is running" \
                       || ok "no jobs of yours currently queued"
else
    skip "Slurm checks (no sbatch on this host -- dry-run mode will be used)"
fi

# --------------------------------------------------------- 7. container
section "7. Container image"
img=$(cfgget image)
found=""
for cand in "$img" images/current.sif container/plasma_pp.sif; do
    [ -n "$cand" ] && [ -f "$cand" ] && { found=$cand; break; }
done
if [ -n "$found" ]; then
    sz=$(du -h "$found" 2>/dev/null | cut -f1)
    ok "image: $found ($sz)"
    [ "$found" != "${img:-none}" ] && warn "config.yaml image='${img:-?}' not found -- using fallback '$found' (fix config or create symlink images/current.sif)"
else
    bad "no container image found (config: '${img:-?}', also tried images/current.sif and container/plasma_pp.sif)"
fi

# ----------------------------------------------------- 8. controller env
section "8. Controller venv (.controller_env)"
if [ -x .controller_env/bin/python ]; then
    ok "venv exists: $(.controller_env/bin/python --version 2>&1)"
    .controller_env/bin/python -c 'import openevolve' 2>/dev/null \
        && ok "openevolve importable ($(.controller_env/bin/python -c 'import openevolve;print(getattr(openevolve,"__version__","?"))' 2>/dev/null))" \
        || warn "openevolve NOT installed yet -- rerun: ./tools/bootstrap_controller.sh --openevolve"
    .controller_env/bin/python -c 'import openai' 2>/dev/null \
        && ok "openai client importable" || warn "openai not installed (comes with openevolve)"
else
    skip "venv not created yet -- run ./tools/bootstrap_controller.sh --openevolve"
fi

# ----------------------------------------------- 9. evaluator end-to-end
section "9. Evaluator dry-run (config -> submit.sh compiler)"
SD="selftest_preflight_$$"
out=$(python3 evaluate.py -c examples/candidate_romio.json --dry-run --root . \
      --state-dir "$SD" 2>/dev/null | tail -1)
if echo "$out" | grep -q '^FITNESS:'; then
    ok "evaluate.py dry-run scored simulated candidate ($out)"
    SUB=$(ls "$SD"/runs/*/submit.sh 2>/dev/null | head -1)
    if [ -n "$SUB" ]; then
        grep -q "^#SBATCH --partition=$PARTITION" "$SUB" && ok "submit.sh: partition=$PARTITION" || bad "submit.sh: wrong partition"
        grep -q "^#SBATCH --time=$TIME_LIMIT"       "$SUB" && ok "submit.sh: time=$TIME_LIMIT"   || bad "submit.sh: wrong time_limit"
        [ -n "$CONSTRAINT" ] && { grep -q "^#SBATCH --constraint=$CONSTRAINT" "$SUB" && ok "submit.sh: constraint=$CONSTRAINT" || bad "submit.sh: constraint missing"; }
        grep -Eq "srun .*--mpi=pmix"                "$SUB" && ok "submit.sh: bare srun --mpi=pmix" || bad "submit.sh: srun --mpi=pmix missing"
        grep -q  "apptainer exec"                   "$SUB" && ok "submit.sh: apptainer exec present" || bad "submit.sh: apptainer exec missing"
        grep -q  -- "--contain"                     "$SUB" && bad "submit.sh: --contain reappeared (breaks vader shm)" || ok "submit.sh: no --contain"
        grep -q  "XDG_RUNTIME_DIR"                  "$SUB" && bad "submit.sh: XDG_RUNTIME_DIR leaked (fatal for sharens)" || ok "submit.sh: XDG_RUNTIME_DIR not exported"
        if grep -q "setup_run" "$SUB" \
           && grep -q "input.deck" benchmarks/epoch_io/setup_run.sh 2>/dev/null; then
            ok "submit.sh: EPOCH input.deck protocol wired (via setup_run.sh)"
        else
            bad "submit.sh: setup_run/input.deck protocol not wired"
        fi
    else
        bad "dry-run produced no submit.sh under $SD/runs"
    fi
else
    bad "evaluate.py dry-run did not print FITNESS line -- core breakage, paste this whole report"
fi
rm -rf "$SD"

# ------------------------------------------------------------ summary
echo
echo "${B}== Summary ==${N}"
echo "  PASS=$NPASS  FAIL=$NFAIL  WARN=$NWARN  SKIP=$NSKIP"
if [ "$NFAIL" -eq 0 ]; then
    echo "  ${G}READY${N} -- proceed: bootstrap (if skipped above) -> tmux -> ./tools/run_controller.sh openevolve_config.yaml"
    exit 0
else
    echo "  ${R}NOT READY${N} -- fix the FAIL lines above (each has a hint), then rerun this script."
    exit 1
fi
