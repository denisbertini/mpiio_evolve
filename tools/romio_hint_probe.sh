#!/usr/bin/env bash
# =============================================================================
# tools/romio_hint_probe.sh -- does ROMIO actually READ our hint file?
#
# Proves the whole injection chain end-to-end on the real stack:
#
#   ROMIO_HINTS=<file>  ->  hint parser  ->  ADIO  ->  Lustre layout
#
# Method: a 6-line MPI-IO program creates a file twice -- once with a hint
# file requesting striping_count=4 / striping_unit=1MiB, once without any
# hints (control). Lustre bakes striping hints into the file AT CREATE
# TIME, so `lfs getstripe` afterwards is an undeniable, filesystem-side
# answer: the hint channel either works or it does not.
#
#   ./tools/romio_hint_probe.sh                  # real probe via srun
#   ./tools/romio_hint_probe.sh --image PATH     # other .sif
#   ./tools/romio_hint_probe.sh --partition q    # default: long
#
# Exit 0 = hints honored (romio engine has teeth); exit 1 = ignored ->
# set default_engine: "ompio" in config.yaml before evolving.
# =============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO"

IMAGE="container/plasma_pp.sif"
PARTITION="${MPIIO_EVOLVE_PARTITION:-long}"
EXPECT_COUNT=4
EXPECT_UNIT=1048576

while [ $# -gt 0 ]; do
    case "$1" in
        --image)     IMAGE="$2"; shift ;;
        --partition) PARTITION="$2"; shift ;;
        -h|--help)   sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option '$1' (--help)" >&2; exit 2 ;;
    esac
    shift
done

[ -f "$IMAGE" ] || { echo "image '$IMAGE' not found" >&2; exit 1; }
command -v srun >/dev/null 2>&1 || { echo "srun not found -- run on a login node" >&2; exit 1; }

# Scratch inside the campaign state workspace (Lustre, bind-mounted).
STATE="$(python3 - <<'PYEOF'
import json, pathlib
try:
    c = json.load(open("config.generated.json"))
except Exception:
    c = {}
w = c.get("workspace", {})
print(pathlib.Path(w.get("root", "/lustre/rz/dbertini2")) / w.get("state_dir", "ppio_tune"))
PYEOF
)"
SCRATCH="$STATE/hint_probe"
mkdir -p "$SCRATCH"
HINTS="$SCRATCH/hints.txt"
PROBE_C="$SCRATCH/probe.c"
WITH="$SCRATCH/with_hints.out"
CTRL="$SCRATCH/control.out"

cat > "$PROBE_C" <<'EOF'
/* create (or truncate) one file via MPI-IO; that's all. */
#include <mpi.h>
int main(int argc, char **argv) {
    MPI_File f;
    MPI_Init(&argc, &argv);
    MPI_File_open(MPI_COMM_SELF, argv[1],
                  MPI_MODE_CREATE | MPI_MODE_WRONLY, MPI_INFO_NULL, &f);
    MPI_File_close(&f);
    MPI_Finalize();
    return 0;
}
EOF

# Whitespace-pair format + magic first line == what ROMIO's parser expects.
printf '# IO hints file\n# romio_hint_probe\nstriping_count %s\nstriping_unit %s\n' \
       "$EXPECT_COUNT" "$EXPECT_UNIT" > "$HINTS"
rm -f "$WITH" "$CTRL"

echo "==> compiling + running probe on partition '$PARTITION' (image: $IMAGE)"
# Same launch idiom as the campaign: bare srun --mpi=pmix --export=ALL, one
# task, apptainer WITHOUT --contain. OMPI_MCA_io=romio341 pins the component
# exactly like the real submit.sh does.
srun -p "$PARTITION" -n 1 --mpi=pmix --export=ALL \
    apptainer exec "$IMAGE" bash -c "
        set -e
        mpicc '$PROBE_C' -o '$SCRATCH/probe.run'
        echo '--- run WITH hint file (ROMIO_PRINT_HINTS echoes parsed hints) ---'
        OMPI_MCA_io=romio341 ROMIO_HINTS='$HINTS' ROMIO_PRINT_HINTS=1 \
            '$SCRATCH/probe.run' '$WITH'
        echo '--- run WITHOUT hints (control) ---'
        OMPI_MCA_io=romio341 '$SCRATCH/probe.run' '$CTRL'
        echo '--- probe done ---'
     "

command -v lfs >/dev/null 2>&1 || {
    echo "lfs not available here -- check manually:"
    echo "  lfs getstripe $WITH"; echo "  lfs getstripe $CTRL"; exit 2; }

stripe_of() { lfs getstripe "$1" 2>/dev/null | awk -v k="$2" '$1 ~ k":" {print $2; exit}'; }
C=$(stripe_of "$WITH" stripe_count);  CU=$(stripe_of "$WITH" stripe_size)
K=$(stripe_of "$CTRL" stripe_count);  KU=$(stripe_of "$CTRL" stripe_size)
echo "==> with hints : stripe_count=${C:-?}  stripe_size=${CU:-?}"
echo "==> control    : stripe_count=${K:-?}  stripe_size=${KU:-?}"

if [ "${C:-0}" = "$EXPECT_COUNT" ] && [ "${CU:-0}" = "$EXPECT_UNIT" ]; then
    echo "VERDICT: PASS -- ROMIO_HINTS honored end-to-end; romio engine is live ammo."
    echo "         (clean up: rm -rf $SCRATCH)"
    exit 0
fi
if [ "${C:-x}" = "${K:-y}" ]; then
    echo "VERDICT: FAIL -- hinted file identical to control; this ROMIO ignores the"
    echo "         hint file. Switch config.yaml to default_engine: \"ompio\""
    echo "         (OMPI_MCA_io_ompio_* env channel) before launching a campaign."
else
    echo "VERDICT: INCONCLUSIVE -- layout changed but not as requested; paste output."
fi
echo "         (clean up: rm -rf $SCRATCH)"
exit 1
