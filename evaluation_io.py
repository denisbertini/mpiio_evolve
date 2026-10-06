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
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:  # OpenEvolve >= 0.4: evaluate() may return (metrics, artifacts); the
    # artifacts are stored with the program and rendered into the mutation
    # prompt of its descendants (config.include_artifacts).  Without this,
    # a scored zero is *indistinguishable* from a timeout or a crash to the
    # LLM -- it can only learn from failure reasons it can actually read.
    from openevolve.evaluation_result import EvaluationResult
except ImportError:  # pragma: no cover - fall back to the bare dict contract
    EvaluationResult = None

REPO = Path(__file__).resolve().parent

_FITNESS_RE = re.compile(r"^FITNESS:\s*([0-9.eE+\-]+)", re.MULTILINE)
_METRICS_RE = re.compile(r"^EVAL_METRICS\s+(\{.*\})", re.MULTILINE)

# POC deck (t_end=10 fs): measured Orion ~13.5 min per 20 fs -> ~7 min/rep
# at 10 fs; 3 in-job reps ~25 min + queue.
# Must exceed cluster.time_limit so Slurm, not the client, remains the
# authority on a running job.
_EVAL_TIMEOUT_S = 3600

# Feedback artifacts go straight into LLM prompts: keep them informative
# but bounded.
_FEEDBACK_CAP = 2400

# Wall-clock timeouts on a shared Lustre are only *partly* the candidate's
# fault.  Tell the mutator so it does not abandon a good family because of
# somebody else's I/O storm (observed: contention windows moving throughput
# 20-30% and killing otherwise-healthy evaluations at the time limit).
_CONTENTION_NOTE = (
    "\nNOTE: wall-clock timeouts on a shared filesystem are sometimes "
    "caused by cluster-wide Lustre contention, not by this configuration. "
    "If similar configurations scored well earlier, treat this as a "
    "possibly-unlucky measurement window, not proof of a bad candidate."
)


# evaluate.py interleaves its classified feedback (lines starting with
# [CATEGORY]) with timestamped logging output on stderr; only the former is
# meaningful inside a mutation prompt.
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _clean_feedback(err):
    """Keep classified feedback / human text, drop timestamped log lines."""
    keep = [ln for ln in err.splitlines() if not _LOG_LINE_RE.match(ln)]
    return "\n".join(keep).strip()


def _result(metrics, feedback=None):
    """Wrap metrics + optional text feedback into the richest container
    OpenEvolve will accept; degrade to the plain dict if unavailable."""
    if EvaluationResult is None or not feedback:
        return metrics
    return EvaluationResult(
        metrics=metrics,
        artifacts={"evaluation_feedback": feedback[:_FEEDBACK_CAP]},
    )


def evaluate(program_path):
    """OpenEvolve entrypoint. Returns a metric dict (with classified-failure
    text as an artifact when available) including combined_score."""
    path = Path(program_path)

    # Cheap gate first: the temp file must be valid JSON before burning an
    # entire Slurm allocation on it.
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _result(
            {"combined_score": 0.0, "valid_json": 0.0},
            f"[INVALID_JSON] The candidate is not valid JSON and was never "
            f"submitted: {exc}. Emit a syntactically valid candidate object "
            f"inside the declared search space.",
        )

    cmd = [sys.executable, str(REPO / "evaluate.py"), "-c", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(REPO), timeout=_EVAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _result(
            {"combined_score": 0.0, "valid_json": 1.0, "eval_timeout": 1.0,
             "timed_out": 1.0},
            "[EVAL_TIMEOUT] The evaluator's watch-dog expired while the "
            "batch job was still running; the job consumed its allocation "
            "and scored zero." + _CONTENTION_NOTE,
        )

    out = proc.stdout or ""
    err = proc.stderr or ""
    metrics = {"valid_json": 1.0}
    m = _FITNESS_RE.search(out)
    if m is None:
        # OpenEvolve discards the subprocess output -- without this dump a
        # zero score is undiagnosable.  Keep stdout+stderr of every failed
        # evaluation under <output>/eval_failures/ (next to the run logs).
        try:
            faildir = Path(os.environ.get("MPIIO_EVOLVE_OUTPUT_DIR",
                                          Path.cwd())) / "eval_failures"
            faildir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            (faildir / f"{stamp}_{proc.returncode}.log").write_text(
                f"$ {' '.join(cmd)}\n--- rc={proc.returncode}\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n",
                encoding="utf-8")
        except OSError:
            pass  # diagnostics must never break the loop
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

    # evaluate.py prints its classified failure diagnosis (CONFIG_ERROR /
    # TIMEOUT / OOM / MPI_LAUNCH / ... with quoted evidence) to stderr.
    # Feed it to the mutator verbatim -- this is the ONLY channel by which
    # the model learns which moves are wrong and why.
    feedback = _clean_feedback(err)
    if feedback:
        metrics["has_feedback"] = 1.0
        if "TIMEOUT" in feedback:
            metrics["timed_out"] = 1.0
            feedback += _CONTENTION_NOTE
    return _result(metrics, feedback)
