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

# --------------------------------------------------------------------------
# Contention circuit breaker.
#
# OpenEvolve evaluates candidates one at a time with NO memory of how the
# last submissions ended: during a Lustre contention storm every job dies at
# the wall-clock limit, yet the loop cheerfully fires the next allocation
# into the same storm -- censored zeros plus wasted cluster time.  The
# adapter owns the moment-of-submission decision, so the breaker lives here:
#
#   * after _STORM_STREAK consecutive timeouts it OPENS;
#   * while open, before submitting, it holds and re-probes filesystem mood
#     with a tiny O_DIRECT write into the state dir (no allocation, seconds;
#     the login node sees the same MDS/OSS weather);
#   * it resumes submitting when the probe recovers to _PROBE_OK_RATIO of
#     the fair-weather baseline, or after _HOLD_MAX_S (then submits anyway,
#     stamping the hold duration into the metrics for the ledger);
#   * healthy probe + timeouts => the configs genuinely are too slow, not
#     the weather: the probe is the arbiter, and holding stops.
#
# Budget: OpenEvolve kills any evaluate() call after evaluator.timeout
# (4200 s), so _HOLD_MAX_S + worst-case job must stay under it; a breach is
# self-correcting (the framework-side kill counts as a timeout and the
# breaker simply holds again next iteration).
# Disable with MPIIO_EVOLVE_BREAKER=0 (e.g. for bare-metal debugging).
# --------------------------------------------------------------------------
_STORM_STREAK  = 2                  # consecutive timeouts that open the breaker
_PROBE_BYTES   = 64 * 1024 * 1024   # 64 MiB direct write (worst case in a
                                    # 3 MiB/s storm: ~20 s, polls are 5 min)
_PROBE_OK_RATIO = 0.75              # mood must reach this fraction of baseline
_HOLD_POLL_S   = 300                # probe every 5 min while holding
_HOLD_MAX_S    = 1500               # give up holding after 25 min (< 4200 s)
_BASELINE_MIN_SAMPLES = 8           # fair-weather baseline needs history


def _state_dir():
    root = os.environ.get("MPIIO_EVOLVE_ROOT")
    if not root:
        return None
    p = Path(root)
    return p if p.is_dir() else None


def _breaker_enabled():
    return (os.environ.get("MPIIO_EVOLVE_BREAKER", "1") != "0"
            and _state_dir() is not None)


def _probe_mood():
    """Direct-write bandwidth probe, MiB/sec -- the filesystem mood.

    O_DIRECT bypasses the page cache; where unsupported or misaligned, a
    buffered write + fsync is a consistent substitute. Returns None on any
    I/O failure -- sensing must never break the loop."""
    sd = _state_dir()
    f = sd / ".mood_probe"
    buf = bytes(4 * 1024 * 1024)
    t0 = time.monotonic()
    bw = None

    def _drain(fd):
        while time.monotonic() - t0 < 120:        # never probe > 2 min
            if os.write(fd, buf) < len(buf):
                break

    try:
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_DIRECT", 0), 0o600)
        try:
            _drain(fd)
        finally:
            os.close(fd)
    except OSError:
        # O_DIRECT unavailable / alignment issue: buffered + fsync retry.
        try:
            t0 = time.monotonic()
            fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            _drain(fd)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            try:
                f.unlink()
            except OSError:
                pass
            return None
    dt = time.monotonic() - t0
    try:
        f.unlink()
    except OSError:
        pass
    if dt <= 0:
        return None
    bw = _PROBE_BYTES / (1024 * 1024) / dt
    # ledger of every probe: the mood curve for later correction/figures
    try:
        with open(sd / "mood_probes.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "mib_sec": round(bw, 2)}) + "\n")
    except OSError:
        pass
    return bw


def _mood_baseline():
    """Fair-weather reference: 75th percentile of probe history, ratcheted
    in a small file so a long storm cannot redefine its own normal."""
    sd = _state_dir()
    hist = []
    try:
        for line in (sd / "mood_probes.jsonl").read_text(
                encoding="utf-8").splitlines()[-500:]:
            try:
                hist.append(float(json.loads(line)["mib_sec"]))
            except Exception:
                continue
    except OSError:
        pass
    if len(hist) < _BASELINE_MIN_SAMPLES:
        return None
    s = sorted(hist)
    p75 = s[int(0.75 * (len(s) - 1))]
    base_file = sd / "mood_baseline.txt"
    cur = 0.0
    try:
        cur = float(base_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    if p75 > cur:
        cur = p75
        try:
            base_file.write_text(f"{cur:.2f}\n", encoding="utf-8")
        except OSError:
            pass
    return cur


_timeout_streak = 0
_last_hold_s = 0.0


def _storm_hold():
    """Block (polling the mood probe) while the breaker is open.

    Returns seconds held. Never blocks longer than _HOLD_MAX_S; when there
    is no baseline history to judge by it takes one grace period and
    proceeds -- the breaker must never become a deadlock."""
    global _last_hold_s
    _last_hold_s = 0.0
    if _timeout_streak < _STORM_STREAK or not _breaker_enabled():
        return 0.0
    t0 = time.monotonic()
    while True:
        remaining = _HOLD_MAX_S - (time.monotonic() - t0)
        if remaining <= 0:
            break                                   # proceed, flagged
        bw, base = _probe_mood(), _mood_baseline()
        if bw is not None and base is not None:
            if bw >= _PROBE_OK_RATIO * base:
                break                               # weather recovered
        time.sleep(min(_HOLD_POLL_S, remaining))
    _last_hold_s = round(time.monotonic() - t0, 1)
    return _last_hold_s

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
    global _timeout_streak
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
    hold_s = _storm_hold()          # breaker open => wait for calm, don't
    #                                         burn an allocation on a censored zero
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(REPO), timeout=_EVAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _timeout_streak += 1
        return _result(
            {"combined_score": 0.0, "valid_json": 1.0, "eval_timeout": 1.0,
             "timed_out": 1.0, "storm_hold_s": hold_s},
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

    # Circuit-breaker memory: timeouts accumulate, a genuinely scored
    # candidate proves the weather is fine again.  Instant rejections
    # (CONFIG_ERROR, invalid JSON) carry no weather information and leave
    # the streak untouched.
    if metrics.get("timed_out"):
        _timeout_streak += 1
    elif metrics.get("combined_score", 0.0) > 0:
        _timeout_streak = 0
    if hold_s:
        metrics["storm_hold_s"] = hold_s
    return _result(metrics, feedback)
