"""
evaluation_io.py -- OpenEvolve evaluation adapter for mpiio_evolve
==================================================================

OpenEvolve evolves a "program" (here: a candidate MPI-IO config in JSON) and
calls ``evaluate(program_path) -> metrics_dict`` with the candidate written
to a temp file. We hand it to the battle-tested ``evaluate.py`` CLI (the same
path timing_probe.sh validated), then translate its stdout protocol:

    FITNESS: <score>                 -> combined_score (higher is better)
    EVAL_METRICS {json}              -> secondary metrics (write_mean_mib_sec,
                                        n_repetitions, write_sem_mib_sec, ...)

Invalid mutants can NEVER crash the loop: anything unparseable or failed
returns combined_score 0.0 plus an error marker -- a losing candidate, not
an exception.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

_FITNESS_RE = re.compile(r"^FITNESS:\s*([0-9.eE+\-]+)", re.MULTILINE)
_METRICS_RE = re.compile(r"^EVAL_METRICS\s+(\{.*\})", re.MULTILINE)

# One evaluation = 3 in-job reps (~4-5 min each on Virgo4 CPU nodes)
# + queue + startup margin. Keep consistent with cluster.time_limit.
_EVAL_TIMEOUT_S = 3000


def evaluate(program_path):
    """OpenEvolve entrypoint. Returns a metric dict with combined_score."""
    path = Path(program_path)

    # Cheap gate first: the temp file must be valid JSON before burning an
    # entire Slurm allocation on it.
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"combined_score": 0.0, "valid_json": 0.0}

    cmd = [sys.executable, str(REPO / "evaluate.py"), "-c", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(REPO), timeout=_EVAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"combined_score": 0.0, "valid_json": 1.0, "eval_timeout": 1.0}

    out = proc.stdout or ""
    metrics = {"valid_json": 1.0}
    m = _FITNESS_RE.search(out)
    metrics["combined_score"] = float(m.group(1)) if m else 0.0
    mm = _METRICS_RE.search(out)
    if mm:
        try:
            for k, v in json.loads(mm.group(1)).items():
                if isinstance(v, (int, float)):
                    metrics[k] = float(v)
        except Exception:
            pass  # secondary metrics are advisory; fitness already parsed
    metrics.setdefault("combined_score", 0.0)
    return metrics
