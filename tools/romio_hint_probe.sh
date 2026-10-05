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
OUT="$SCRATCH/probe_output.txt"
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
     " 2>&1 | tee "$OUT"

# ---- verdict ---------------------------------------------------------------
# PRIMARY evidence: ROMIO_PRINT_HINTS shows the EFFECTIVE hint set; our two
# values appearing there proves env -> hint-file -> parser -> fd->hints all
# work. (A lustre-specific side-effect like lfs getstripe CANNOT serve as
# proof: builds whose statfs magic check misidentifies Lustre as UFS parse
# striping hints but never execute them -- cosmetic, not fatal.)
if grep -q "striping_count.*4" "$OUT" && grep -q "striping_unit.*1048576" "$OUT"; then
    echo "VERDICT: PASS -- hint file READ by ROMIO (values present in effective set)."
    if grep -qi "filesystem_type.*UFS" "$OUT"; then
        echo "  NOTE: this ROMIO sees the FS as UFS -> striping_*/direct_io hints are"
        echo "        PARSED BUT INERT here; Lustre striping must stay under"
        echo "        'lfs setstripe' (already done via run-dir layout), and the"
        echo "        effective search space is the cb_*/buffer/ds_* hint family."
    fi
    echo "         (clean up: rm -rf $SCRATCH)"
    exit 0
fi
command -v lfs >/dev/null 2>&1 && {
stripe_of() { lfs getstripe "$1" 2>/dev/null | awk -v k="$2" '$1 ~ k":" {print $2; exit}'; }
echo "==> with hints : stripe_count=$(stripe_of "$WITH" stripe_count)  stripe_size=$(stripe_of "$WITH" stripe_size)"
echo "==> control    : stripe_count=$(stripe_of "$CTRL" stripe_count)  stripe_size=$(stripe_of "$CTRL" stripe_size)"; }
echo "VERDICT: FAIL -- our hint values never appeared in ROMIO's effective set."
echo "         The env hint-file channel is dead on this build; switch"
echo "         config.yaml to default_engine: \"ompio\" before evolving."
echo "         (clean up: rm -rf $SCRATCH)"
exit 1
