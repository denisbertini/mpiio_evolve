"""
parser.py -- Log Aggregator & Error Profiler
============================================

Two responsibilities:

1. ``parse_throughput``  -- extract quantitative write/read speeds (MiB/sec)
   from IOR-style or generic plasma-simulation benchmark logs. Handles
   ``Max Write: 1234.56 MiB/sec`` lines, ``Mean`` fallbacks, and a generic
   ``wrote ... 2.1 GiB/s`` pattern for custom simulator output.

2. ``profile_errors``    -- when a run crashes (nonzero exit / 0 B/sec), mine
   the stderr log for OOM kills, Lustre layout rejections, MPI fabric/setup
   failures, ENOSPC, timeouts ... and render them into a compact natural-
   language feedback string that is fed back to the LLM mutation prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("mpiio_evolve.parser")

# ---------------------------------------------------------------------------
# Throughput extraction
# ---------------------------------------------------------------------------

_UNIT_TO_MIB = {
    "b": 1.0 / 2**20,
    "kib": 1.0 / 2**10, "mib": 1.0, "gib": 2**10, "tib": 2**20,
    "kb": 1e6 / 2**20, "mb": 1e6 / 2**20, "gb": 1e9 / 2**20, "tb": 1e12 / 2**20,
}

# IOR canonical lines:
#   Max Write:   3456.78 MiB/sec (3624.55 MB/sec) [N]
#   Mean Read:   1234.56 MiB/sec (1294.52 MB/sec) [N]
_IOR_LINE = re.compile(
    r"^\s*(Max|Mean)\s+(Write|Read)\s*:\s*([0-9][0-9.,]*)\s*"
    r"([KMGT]?i?B)\s*/\s*sec",
    re.IGNORECASE | re.MULTILINE,
)

# Generic simulator lines, e.g.
#   "aggregate write bandwidth: 2.15 GiB/s"   /   "read 1.4 GB/sec"
_GENERIC = re.compile(
    r"\b(write|read|write-bandwidth|read-bandwidth)\b[^0-9\n]{0,40}"
    r"([0-9][0-9.,]*)\s*([KMGT]?i?B)\s*/\s*s(ec)?\b",
    re.IGNORECASE,
)

# Darshan-derived (fitness v2): emitted by benchmarks/generic/measure.sh
# --strategy darshan from the profiled io_only_time. Deliberately phrased
# WITHOUT the words "write"/"read" so _GENERIC cannot also capture it:
#   "aggregate io-only bandwidth: 1.87 GiB/s"
_IO_ONLY = re.compile(
    r"\bio-only\b[^0-9\n]{0,40}([0-9][0-9.,]*)\s*([KMGT]?i?B)\s*/\s*s(ec)?\b",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Throughput:
    """All extracted speeds, normalized to MiB/sec (None = not measured)."""

    max_write: Optional[float] = None
    max_read: Optional[float] = None
    mean_write: Optional[float] = None
    mean_read: Optional[float] = None
    io_only_write: Optional[float] = None   # darshan io_only-time based (v2)
    source: str = ""          # "ior" | "generic" | ""

    def best(self, prefer: str = "max") -> tuple:
        """Return (write_mibs, read_mibs) honoring the config preference.

        Falls back max->mean and mean->max so a partially-formatted log still
        produces a fitness signal.
        """
        w = (self.max_write, self.mean_write) if prefer == "max" else (self.mean_write, self.max_write)
        r = (self.max_read, self.mean_read) if prefer == "max" else (self.mean_read, self.max_read)
        write = next((v for v in w if v is not None), None)
        read = next((v for v in r if v is not None), None)
        return write, read


def _to_mib(value_text: str, unit: str) -> Optional[float]:
    try:
        value = float(value_text.replace(",", ""))
    except ValueError:
        return None
    factor = _UNIT_TO_MIB.get(unit.lower())
    return value * factor if factor else None


def parse_throughput(text: str) -> Throughput:
    """Extract write/read MiB/sec from a benchmark stdout blob."""
    tp = Throughput()
    matched_ior = False

    for value, unit, _ in _IO_ONLY.findall(text):
        mibs = _to_mib(value, unit)
        if mibs is not None:
            tp.io_only_write = (mibs if tp.io_only_write is None
                                else max(tp.io_only_write, mibs))

    for stat, op, value, unit in _IOR_LINE.findall(text):
        mibs = _to_mib(value, unit)
        if mibs is None:
            continue
        matched_ior = True
        key = f"{stat.lower()}_{op.lower()}"          # max_write, mean_read...
        current = getattr(tp, key, None)
        # keep the maximum if a log repeats phases across ranks/aggregates
        setattr(tp, key, mibs if current is None else max(current, mibs))

    if not matched_ior:
        for op, value, unit, _ in _GENERIC.findall(text):
            mibs = _to_mib(value, unit)
            if mibs is None:
                continue
            tp.source = "generic"
            if "write" in op.lower():
                tp.max_write = mibs if tp.max_write is None else max(tp.max_write, mibs)
            else:
                tp.max_read = mibs if tp.max_read is None else max(tp.max_read, mibs)
        if tp.source:
            tp.mean_write, tp.mean_read = tp.max_write, tp.max_read
    else:
        tp.source = "ior"

    return tp


# ---------------------------------------------------------------------------
# Error profiling
# ---------------------------------------------------------------------------

# (category, regex, human explanation template for the LLM prompt)
_ERROR_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("OOM",
     re.compile(r"out of memory|OOM|Cannot allocate memory|Memory limit exceeded|"
                r"oom-kill|Virtual memory exhausted|Do not have enough memory",
                re.IGNORECASE),
     "The job was OOM-killed: per-rank memory footprint exceeded the "
     "--mem-per-cpu allocation. Reduce buffer sizes (cb_buffer_size, "
     "fb_data_size) or aggregate fewer ranks per node."),
    ("OOM_SIGKILL",
     re.compile(r"signal 9|SIGKILL|Killed\b"),
     "A rank died with SIGKILL, which on this cluster almost always means "
     "the cgroup OOM killer fired. Same remedy as an explicit OOM."),
    ("LUSTRE_LAYOUT",
     re.compile(r"invalid stripe|layout.*(invalid|error)|lfs setstripe|"
                r"stripe count|OST[^a-z]|mdt[^a-z]|Structure needs cleaning",
                re.IGNORECASE),
     "A Lustre layout error occurred. The requested stripe_count/stripe_size "
     "combination was rejected or the target MDT/OBD is unhealthy. Try a "
     "different stripe_count within the OST count of the filesystem."),
    ("ENOSPC",
     re.compile(r"No space left on device|ENOSPC|quota exceeded", re.IGNORECASE),
     "The scratch filesystem (or its project quota) is full. The benchmark "
     "file could not be allocated. This is environmental, not a config "
     "defect -- retry or shrink the IOR block size."),
    ("MPI_LAUNCH",
     re.compile(r"pmi[_-]|srun: error|mpirun.*failed|MPI_Abort|"
                r"Unable to start a daemon|orted.*failed|"
                r"error creating shared memory",
                re.IGNORECASE),
     "MPI failed to launch/initialize (PMIx/PMI or orted error). Often a "
     "mismatch between the compiled MPI flavor and the module loaded on the "
     "compute nodes, or /dev/shm exhaustion on the allocation."),
    ("MISSING_BINARY",
     re.compile(r"command not found|No such file or directory.*(?:IOR|ior|srun)",
                re.IGNORECASE),
     "The benchmark binary was not found on the compute-node PATH. A module "
     "load is missing (module load ior/mpi) -- environmental issue."),
    ("TIMEOUT",
     re.compile(r"timed? ?out|Job terminated|CANCELLED by|Preempted|"
                r"Wall clock time limit",
                re.IGNORECASE),
     "The run hit the wall-clock limit or was cancelled before finishing. "
     "Either the configuration is too slow for the requested time_limit, "
     "or the shared filesystem was contended during this measurement "
     "window (check whether similar configs scored well at other times)."),
    ("PERM",
     re.compile(r"Permission denied|Operation not permitted|EPERM", re.IGNORECASE),
     "A permission error occurred -- often an attempt to change striping on "
     "an existing file, or writing outside the project directory."),
    ("HOME_WRITE",
     re.compile(r"(can't|cannot|unable to) (create|write|make|open).*("
                r"home|HOME|\.cache|\.config|\.triton)", re.IGNORECASE),
     "Something tried to write to the (nonexistent) user home directory. "
     "A cache-isolation export (HOME/HF_HOME/TRITON_CACHE_DIR/XDG_*) is "
     "missing from the job environment."),
]

_MAX_CONTEXT_LINES = 4      # log lines quoted per finding
_MAX_FEEDBACK_CHARS = 2200  # keep the LLM feedback prompt lean


@dataclass
class ErrorReport:
    """Structured crash diagnosis for one run."""

    clean: bool
    categories: list = field(default_factory=list)
    feedback: str = ""
    tail: str = ""           # raw stderr tail, last-resort evidence

    def __bool__(self) -> bool:
        return not self.clean


def _context_for(lines: Iterable[str], idx: int) -> str:
    lines = list(lines)
    lo, hi = max(0, idx - 1), min(len(lines), idx + _MAX_CONTEXT_LINES)
    return "\n".join(f"    | {l.strip()[:200]}" for l in lines[lo:hi])


def profile_errors(stderr_text: str, exit_code: int = 0,
                   throughput_zero: bool = False) -> ErrorReport:
    """Mine stderr for known failure signatures; build LLM-ready feedback.

    Returns an ErrorReport; ``clean=True`` means nothing matched and the run
    is presumed healthy (or the failure mode is novel -- the raw stderr tail
    is then attached so the mutation model still gets evidence).
    """
    lines = stderr_text.splitlines() if stderr_text else []
    categories, findings = [], []

    for category, pattern, explanation in _ERROR_PATTERNS:
        for i, line in enumerate(lines):
            if pattern.search(line):
                categories.append(category)
                findings.append(
                    f"[{category}] {explanation}\n"
                    f"  Log evidence:\n{_context_for(lines, i)}"
                )
                break   # first hit per category is enough

    clean = not categories and exit_code == 0 and not throughput_zero
    if clean:
        return ErrorReport(clean=True)

    parts = []
    if not categories:
        if throughput_zero:
            parts.append(
                "[UNCLASSIFIED] The job exited but reported 0 bytes/sec of "
                "throughput and no known error signature. The run produced no "
                "measurable I/O -- inspect the raw log tail below."
            )
        else:
            parts.append(
                f"[UNCLASSIFIED] Nonzero exit code {exit_code} with no known "
                "error signature. Inspect the raw log tail below."
            )
    parts.extend(findings)
    if exit_code != 0:
        parts.append(f"Job exit code: {exit_code}.")

    tail = "\n".join(lines[-25:])[-1200:] if lines else "(stderr was empty)"
    feedback = "\n\n".join(parts)
    if len(feedback) > _MAX_FEEDBACK_CHARS:
        feedback = feedback[:_MAX_FEEDBACK_CHARS] + "\n  ... (truncated)"
    return ErrorReport(clean=False, categories=categories, feedback=feedback, tail=tail)


# ---------------------------------------------------------------------------
# Multi-repetition sampling + physics-style statistics
# ---------------------------------------------------------------------------

# Marker emitted by the launcher's in-job repetition loop.
REP_MARKER = re.compile(r"^=== MPIIO_EVOLVE_REP (\d+) ===\s*$", re.MULTILINE)


def read_log(path: Path, limit: int = 1 << 22) -> str:
    """Public log reader: last *limit* bytes (default 4 MiB)."""
    return _safe_read(path, limit)


def parse_rep_throughputs(text: str, prefer: str = "max") -> list:
    """Split a multi-repetition stdout into per-rep (write, read) MiB/sec.

    Legacy single-run logs (no markers) yield a one-element list. A rep whose
    section produced no numbers yields (None, None) so the caller can count
    failed repetitions instead of silently dropping them.
    """
    marks = list(REP_MARKER.finditer(text))
    if not marks:
        return [parse_throughput(text).best(prefer)]
    samples = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        samples.append(parse_throughput(text[m.end():end]).best(prefer))
    return samples


def parse_rep_io_only(text: str) -> list:
    """Per-repetition darshan io-only bandwidth (MiB/sec), same rep
    markers as parse_rep_throughputs; None where the rep has no value."""
    marks = list(REP_MARKER.finditer(text))
    if not marks:
        return [parse_throughput(text).io_only_write]
    return [parse_throughput(text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]).io_only_write
            for i, m in enumerate(marks)]


def mean_std(values: list) -> tuple:
    """Physics-style mean and sample standard deviation (denominator N-1).

    None entries are ignored. Returns (mean, std, n) with std == 0.0 when
    n < 2; (None, None, 0) when no sample is usable. The standard error of
    the mean is std/sqrt(n), computed by the caller.
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return None, None, 0
    mean = sum(vals) / n
    if n < 2:
        return mean, 0.0, 1
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return mean, var ** 0.5, n


def parse_log_files(stdout_path: Path, stderr_path: Path) -> tuple[Throughput, str, str]:
    """Convenience reader: returns (throughput, stderr_text, stdout_text)."""
    stdout_text = _safe_read(stdout_path)
    stderr_text = _safe_read(stderr_path)
    return parse_throughput(stdout_text), stderr_text, stdout_text


def _safe_read(path: Path, limit: int = 1 << 20) -> str:
    """Read the *tail* of a log file (cheap even for multi-MB stderr)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size > limit:
                fh.seek(size - limit)
            else:
                fh.seek(0)
            return fh.read()
    except OSError:
        return ""
