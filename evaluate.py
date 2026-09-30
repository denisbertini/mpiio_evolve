#!/usr/bin/env python3
"""
evaluate.py -- OpenEvolve entrypoint for mpiio_evolve
=====================================================

Drives the full candidate lifecycle and reports fitness:

    candidate (dict from OpenEvolve / JSON file)
      -> validate against config.yaml search boundaries
      -> lfs setstripe on a fresh per-run data directory
      -> materialize ROMIO hint file  OR  OMPI_MCA_* env array
      -> compile sbatch script (with $HOME-isolation header) and submit
         with `sbatch --wait`
      -> parse MiB/sec from the job's stdout
      -> on crash: profile stderr into natural-language feedback
      -> print `EVAL_METRICS {...}` then `FITNESS: {score}`

Integration with OpenEvolve
---------------------------
* Function API: ``from evaluate import evaluate`` -- OpenEvolve's python
  evaluator calls ``evaluate(candidate_dict) -> dict[str, float]``. The
  returned dict is numeric; the crash feedback string is additionally
  printed to stderr and stored in ``<run_dir>/feedback.txt`` so a custom
  prompt-massager can attach it to the next mutation prompt.
* Subprocess API: ``python evaluate.py --candidate cand.json`` scrapes the
  ``FITNESS:`` line exactly as the framework expects.

Any internal failure still exits 0 with ``FITNESS: 0.0`` + feedback, so the
evolutionary loop never dies on a bad mutant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from infrastructure import (
    IoConfig,
    LustreConfigurator,
    build_job_environment,
    container_prefix,
    write_romio_hints,
    ROMIO_HINTS_FILE,
    ensure_inside,
)
from parser import ErrorReport, Throughput, parse_log_files, profile_errors
from slurm_launcher import SlurmLauncher, SlurmResources

logger = logging.getLogger("mpiio_evolve.evaluate")

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    """Load config from YAML or JSON with ZERO hard third-party deps.

    The evaluator must run on a login node with a frozen system Python 3.9
    and no guaranteed PyYAML, so the loader chain is:

        *.json               -> stdlib json
        *.yaml with PyYAML   -> yaml.safe_load (richest parser)
        *.yaml without it    -> bundled simple_yaml (config.yaml subset)

    If a YAML config fails to parse on this host and a sibling
    config.generated.json exists (see tools/compile_config.py), that is used
    automatically as the stdlib-only escape hatch.
    """
    path = Path(path)
    try:
        return _load_config_one(path)
    except Exception:
        generated = path.parent / "config.generated.json"
        if path.suffix.lower() in (".yaml", ".yml") and generated.exists():
            logger.warning("could not parse %s -- falling back to %s",
                           path, generated)
            return _load_config_one(generated)
        raise


def _load_config_one(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        cfg = json.loads(text)
    else:
        try:
            import yaml                                   # type: ignore
        except ImportError:
            from simple_yaml import load as _simple_load
            cfg = _simple_load(text)
        else:
            cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ValueError(f"config {path} did not parse to a mapping")
    return cfg


def resolve_engine(cfg: Mapping, candidate: Mapping,
                   container: str = "") -> str:
    """romio / ompio selection: candidate > config explicit > probe > default.

    When a container prefix is given, the probe runs INSIDE the plasma image:
    its %environment sets OMPI_MCA_io=romio341, i.e. the embedded ROMIO is the
    active MPI-IO component and hint files are the effective tuning surface
    even though the MPI flavor is Open MPI.
    """
    explicit = candidate.get("mpi_engine") or cfg["cluster"].get("mpi_engine", "auto")
    if explicit in ("romio", "ompio"):
        return explicit

    probe = _probe_mpi(container)
    if probe:
        return probe
    return str(cfg["cluster"].get("default_engine", "romio"))


_MPI_PROBE_CACHE: dict = {}


def _probe_mpi(container: str) -> Optional[str]:
    """Detect the effective MPI-IO component; cached per probe target."""
    key = container or "host"
    if key in _MPI_PROBE_CACHE:
        return _MPI_PROBE_CACHE[key]
    result = None
    try:
        if container:
            probe_cmd = (f"{container} sh -c "
                         "'echo __IO=${OMPI_MCA_io:-unset}; "
                         "mpirun --version 2>/dev/null | head -n 1'")
            out = subprocess.run(probe_cmd, shell=True, capture_output=True,
                                 text=True, timeout=300).stdout
        else:
            exe = shutil.which("mpiexec") or shutil.which("mpirun")
            out = ""
            if exe:
                out = subprocess.run([exe, "--version"], capture_output=True,
                                     text=True, timeout=20).stdout
        low = out.lower()
        if "__io=romio" in low:
            result = "romio"          # e.g. OMPI_MCA_io=romio341 (embedded ROMIO)
        elif "__io=omp" + "io" in low:
            result = "omp" + "io"
        elif "mpich" in low or "intel" in low or "cray" in low:
            result = "romio"
        elif "open mpi" in low:
            result = "omp" + "io"     # OMPI default io component
    except (OSError, subprocess.TimeoutExpired):
        result = None
    _MPI_PROBE_CACHE[key] = result
    return result


def workspace_root(cfg: Mapping) -> Path:
    root = Path(cfg.get("workspace", {}).get("root", "."))
    return (root if root.is_absolute() else REPO_ROOT / root).resolve()


# ---------------------------------------------------------------------------
# Synthetic benchmark (dry-run only) -- keeps the evolutionary loop alive
# on dev machines without Slurm/Lustre, with parameter-sensitive scores.
# ---------------------------------------------------------------------------


def _synthetic_ior_log(io_cfg: IoConfig, ntasks: int) -> str:
    """Deterministic pseudo-performance log driven by the candidate params."""
    seed = hashlib.sha256(json.dumps(
        {"r": io_cfg.romio_hints, "o": io_cfg.ompio_mca,
         "s": (io_cfg.stripe.stripe_count, io_cfg.stripe.stripe_size)},
        sort_keys=True).encode()).hexdigest()
    rnd = int(seed[:8], 16) / 0xFFFFFFFF           # 0..1, stable per candidate

    sc = io_cfg.stripe.stripe_count if io_cfg.stripe.stripe_count > 0 else 4
    # Sweet-spot model: throughput peaks around 8..32 stripes and good hint combos
    stripe_term = 1.0 - abs(sc - 16) / 32.0
    hint_bonus = 0.15 if (io_cfg.romio_hints.get("romio_cb_write") == "enable"
                          or io_cfg.ompio_mca.get("num_aggregators", 0)) else 0.0
    base = min(ntasks, 64) * 45.0                  # ~MiB/s per task ceiling
    write = max(50.0, base * max(0.2, stripe_term) * (0.85 + 0.3 * rnd) * (1 + hint_bonus))
    read = write * (0.75 + 0.2 * rnd)

    def fmt(label: str, mib: float) -> str:
        return f"{label}: {mib:.2f} MiB/sec ({mib * 1.048576:.2f} MB/sec)"

    return "\n".join([
        fmt("Max Write", write),
        fmt("Max Read", read),
        fmt("Mean Write", write * 0.93),
        fmt("Mean Read", read * 0.93),
        "",
    ])


# ---------------------------------------------------------------------------
# Core evaluation lifecycle
# ---------------------------------------------------------------------------


def evaluate(candidate: Mapping[str, Any],
             config_path: Path = DEFAULT_CONFIG,
             dry_run: Optional[bool] = None) -> dict:
    """Evaluate one I/O configuration candidate. Never raises on bad input."""
    started = time.monotonic()
    cfg = load_config(config_path)
    ws = workspace_root(cfg)
    ws_cfg = cfg.get("workspace", {})
    fit_cfg = cfg.get("fitness", {})
    bench_cfg = cfg.get("benchmark", {})

    cand = dict(candidate or {})
    run_id = _run_id(cand)
    run_dir = ensure_inside(ws, ws / ws_cfg.get("runs_subdir", "runs") / run_id)
    data_dir = run_dir / "data"
    feedback = ""
    result = None
    engine = "?"
    layout_desc = "(not applied)"
    profile_name = "default"

    try:
        # -- 1. validate ------------------------------------------------------
        # Decide dry-run ONCE: explicit flag or missing Slurm. The same
        # decision governs the launcher, the container checks and probes.
        simulate = bool(dry_run) or shutil.which("sbatch") is None

        container = container_prefix(cfg, ws, REPO_ROOT)
        probe_prefix = ""
        if container and not simulate:
            runtime = container.split()[0]
            if shutil.which(runtime) is None:
                raise RuntimeError(
                    f"container runtime '{runtime}' not found; build the image "
                    f"with container/build_container.sh or disable the "
                    f"'container' block in config.yaml")
            probe_prefix = container

        engine = resolve_engine(cfg, cand, probe_prefix)
        io_cfg = IoConfig.from_candidate(cand, engine)
        io_cfg.stripe.validate(cfg["search_space"]["lustre"])

        # -- 2. Lustre layout ---------------------------------------------------
        lustre = LustreConfigurator(dry_run=dry_run)
        layout_desc = lustre.apply(data_dir, io_cfg.stripe)

        # -- 3. engine artifacts ------------------------------------------------
        hints_file = None
        if engine == "romio":
            hints_file = write_romio_hints(run_dir / ROMIO_HINTS_FILE, io_cfg.romio_hints)
        env = build_job_environment(
            io_cfg, ws, hints_file,
            cfg["search_space"].get("env_prefix_allowlist"),
            home_subdir=str(ws_cfg.get("home_subdir", ".fake_home")),
        )

        # -- 4. compile + submit --------------------------------------------------
        prof_cmd, prof = _resolve_profile(cfg, cand)
        profile_name = str(cand.get("benchmark_profile")
                           or cfg.get("benchmark", {}).get("active", "default"))
        ntasks = int(prof.get("ntasks")
                     or cfg["cluster"]["nodes"] * cfg["cluster"]["tasks_per_node"])
        full_cmd = (f"{container} " if container else "") + str(prof_cmd)
        command = full_cmd.format(
            ntasks=ntasks,
            data_dir=data_dir,
            repo=str(REPO_ROOT),
            transfer_block=prof.get("transfer_block", "1M"),
            block_size=prof.get("block_size", "1G"),
            segment=prof.get("segment", 1),
            repetitions=prof.get("repetitions", 1),
            engine=engine,
            hints_file=hints_file or "",
        )
        launcher = SlurmLauncher(ws, dry_run=simulate)
        script = launcher.build_script(
            run_dir,
            SlurmResources.from_config(cfg["cluster"], job_name=f"ev-{run_id}"),
            command, env, ntasks,
            extra_srun_args=str(cfg["cluster"].get("extra_srun_args", "") or ""),
        )
        slurm_timeout = _time_limit_seconds(cfg["cluster"]) + 120
        result = launcher.submit(script, wait=bool(cfg["cluster"].get("wait", True)),
                                 timeout=slurm_timeout)

        # -- 5. synthetic log in dry-run so parser+fitness still run -------------
        if result.simulated:
            (run_dir / "stdout.log").write_text(
                _synthetic_ior_log(io_cfg, ntasks), encoding="utf-8")

        # -- 6. parse & score ------------------------------------------------------
        throughput, stderr_text, _ = parse_log_files(result.stdout_path, result.stderr_path)
        write_mibs, read_mibs = throughput.best(fit_cfg.get("prefer", "max"))
        no_data = write_mibs is None and read_mibs is None

        report: ErrorReport = profile_errors(
            stderr_text, exit_code=result.exit_code, throughput_zero=no_data)
        if not report.clean:
            feedback = report.feedback
            (run_dir / "feedback.txt").write_text(report.feedback, encoding="utf-8")

        score = _score(write_mibs, read_mibs, result.exit_code, fit_cfg)

    except Exception as exc:                                  # noqa: BLE001
        logger.exception("candidate evaluation aborted")
        score, write_mibs, read_mibs = 0.0, None, None
        feedback = (f"[CONFIG_ERROR] The candidate was rejected before or during "
                    f"submission: {type(exc).__name__}: {exc}. Fix the parameter "
                    f"values and stay inside the declared search space.")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "feedback.txt").write_text(feedback, encoding="utf-8")

    # -- 7. cleanup ------------------------------------------------------------
    _cleanup(run_dir, ws, cfg, keep_data=bool(ws_cfg.get("keep_data", False)))

    metrics = {
        "score": float(score),
        "write_mib_sec": float(write_mibs) if write_mibs is not None else 0.0,
        "read_mib_sec": float(read_mibs) if read_mibs is not None else 0.0,
        "job_exit_code": float(result.exit_code) if result else -1.0,
        "runtime_sec": round(time.monotonic() - started, 3),
    }
    context = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "engine": engine,
        "benchmark_profile": profile_name,
        "layout": layout_desc,
        "feedback": feedback,
    }
    (run_dir / "result.json").write_text(
        json.dumps({"metrics": metrics, "context": context}, indent=2),
        encoding="utf-8")

    # stdout protocol: machine-readable metrics line, then the FITNESS line.
    print(f"EVAL_METRICS {json.dumps({**metrics, 'run_dir': str(run_dir)})}")
    if feedback:
        print(feedback, file=sys.stderr)          # surface to OpenEvolve stderr capture
    print(f"FITNESS: {score:.4f}")
    sys.stdout.flush()
    return metrics


# ---------------------------------------------------------------------------
# Benchmark profiles
# ---------------------------------------------------------------------------


def _resolve_profile(cfg: Mapping, candidate: Mapping) -> tuple:
    """Pick the active benchmark profile (epoch_io | ior_canary | legacy).

    A candidate may switch profiles with {"benchmark_profile": "ior_canary"}.
    Profile keys overlay the shared benchmark: block keys (transfer_block...).
    Returns (command_template, merged_profile_dict).
    """
    bench_cfg = dict(cfg.get("benchmark", {}) or {})
    profiles = bench_cfg.pop("profiles", None)
    if not profiles:                                   # legacy single command
        return bench_cfg["command"], bench_cfg
    name = candidate.get("benchmark_profile") or bench_cfg.get("active") \
        or next(iter(profiles))
    if name not in profiles:
        raise ValueError(f"unknown benchmark_profile {name!r}; "
                         f"choose from {sorted(profiles)}")
    merged = {k: v for k, v in bench_cfg.items() if k != "active"}
    merged.update(profiles[name] or {})
    if "command" not in merged:
        raise ValueError(f"benchmark profile {name!r} has no 'command'")
    return merged["command"], merged


# ---------------------------------------------------------------------------
# Fitness shaping
# ---------------------------------------------------------------------------


def _score(write_mibs, read_mibs, exit_code: int, fit_cfg: Mapping) -> float:
    if exit_code != 0 and write_mibs is None:
        return 0.0
    w = float(fit_cfg.get("weights", {}).get("write", 1.0))
    r = float(fit_cfg.get("weights", {}).get("read", 0.5))
    partial = float(fit_cfg.get("partial_credit", 0.25))

    if write_mibs is None and read_mibs is None:
        return 0.0
    if write_mibs is None:      # partial crash: write phase never reported
        return r * read_mibs * partial
    if read_mibs is None:
        return w * write_mibs
    return w * write_mibs + r * read_mibs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_id(candidate: Mapping) -> str:
    digest = hashlib.sha256(
        json.dumps(candidate, sort_keys=True, default=str).encode()
    ).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{digest}"


def _time_limit_seconds(cluster: Mapping) -> int:
    hh, mm, ss = [int(x) for x in str(cluster.get("time_limit", "00:10:00")).split(":")]
    return hh * 3600 + mm * 60 + ss


def _cleanup(run_dir: Path, ws: Path, cfg: Mapping, keep_data: bool) -> None:
    """Delete benchmark data (always inside ws); garbage-collect old runs."""
    if not keep_data:
        data_dir = run_dir / "data"
        if data_dir.exists():
            try:
                ensure_inside(ws, data_dir)
                shutil.rmtree(data_dir, ignore_errors=True)
            except Exception as exc:                        # noqa: BLE001
                logger.warning("data cleanup skipped: %s", exc)

    keep = int(cfg.get("workspace", {}).get("keep_runs", 0) or 0)
    if keep <= 0:
        return
    runs_root = ensure_inside(ws, ws / cfg["workspace"].get("runs_subdir", "runs"))
    if not runs_root.is_dir():
        return
    run_dirs = sorted(p for p in runs_root.iterdir() if p.is_dir())
    for old in run_dirs[:-keep]:
        logger.info("garbage-collecting old run %s", old.name)
        shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_candidate_arg(text: str) -> dict:
    """Accept a JSON file path, '-' for stdin, or an inline JSON string."""
    if text == "-":
        return json.load(sys.stdin)
    p = Path(text)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(text)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--candidate", "-c", required=True,
                    help="candidate JSON file, inline JSON, or '-' for stdin")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--dry-run", action="store_true",
                    help="render scripts + synthesize a benchmark log (no Slurm/Lustre)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level="DEBUG" if args.verbose else "INFO",
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        candidate = _load_candidate_arg(args.candidate)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"FITNESS: 0.0")
        print(f"[CONFIG_ERROR] could not load candidate: {exc}", file=sys.stderr)
        return 0                                             # never break the loop

    evaluate(candidate, config_path=Path(args.config),
             dry_run=True if args.dry_run else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
