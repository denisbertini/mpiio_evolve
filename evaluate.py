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
import os
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
from parser import (ErrorReport, mean_std, parse_log_files,
                  parse_rep_throughputs, profile_errors, read_log)
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


def resolve_workspace(cfg: Mapping, state_dir: Optional[str] = None,
                      create: bool = True, fallback_local: bool = True) -> Path:
    """Resolve (and optionally create) the per-launcher STATE DIRECTORY.

    Layout on the parallel filesystem::

        <DEPLOY_ROOT>/<state_dir>/          (e.g. /lustre/rz/dbertini2/alice)
        ├── runs/        per-candidate run directories
        ├── .fake_home/  synthetic $HOME exported into every job
        └── tmp/         TMPDIR for jobs, apptainer, pip ...

    Precedence -- DEPLOY_ROOT: --root > MPIIO_EVOLVE_ROOT > config
    workspace.root; state dir: --state-dir > MPIIO_EVOLVE_STATE_DIR > config
    workspace.state_dir. Everything below the state directory is protected by
    ensure_inside(); DEPLOY_ROOT itself is shared but never mutated directly.

    If DEPLOY_ROOT is not creatable (e.g. a dev machine without /lustre) and
    fallback_local is set (dry-run/dev context), a .dev_state/<state_dir>
    under the repo is used with a loud warning instead of failing.
    """
    ws_cfg = cfg.get("workspace", {}) or {}
    root = os.environ.get("MPIIO_EVOLVE_ROOT") or str(ws_cfg.get("root", "."))
    root_path = Path(root)
    if not root_path.is_absolute():
        root_path = (REPO_ROOT / root_path)

    sd = (state_dir or os.environ.get("MPIIO_EVOLVE_STATE_DIR")
          or str(ws_cfg.get("state_dir") or "default"))
    if os.sep in sd or (os.altsep and os.altsep in sd) or ".." in sd:
        raise ValueError(f"state_dir must be a simple name, got {sd!r}")

    ws = root_path / sd
    if create:
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if not fallback_local:
                raise RuntimeError(
                    f"cannot create state directory {ws}: {exc}. Set "
                    f"--root/MPIIO_EVOLVE_ROOT to a writable parallel-filesystem "
                    f"path, or use --dry-run on a development machine.") from exc
            local = (REPO_ROOT / ".dev_state" / sd).resolve()
            logger.warning("state directory %s unavailable (%s) -- "
                           "falling back to local %s", ws, exc, local)
            local.mkdir(parents=True, exist_ok=True)
            return local
    return ws.resolve()


# ---------------------------------------------------------------------------
# Synthetic benchmark (dry-run only) -- keeps the evolutionary loop alive
# on dev machines without Slurm/Lustre, with parameter-sensitive scores.
# ---------------------------------------------------------------------------


def _synthetic_ior_log(io_cfg: IoConfig, ntasks: int, rep_index: int = 1) -> str:
    """Deterministic pseudo-performance log driven by the candidate params.

    rep_index adds a deterministic +/-8 % jitter so multi-repetition dry-runs
    exercise the mean/std/SEM path while staying fully reproducible (an
    evolution trace must never depend on RNG state).
    """
    seed = hashlib.sha256(json.dumps(
        {"r": io_cfg.romio_hints, "o": io_cfg.ompio_mca,
         "s": (io_cfg.stripe.stripe_count, io_cfg.stripe.stripe_size),
         "rep": rep_index},
        sort_keys=True).encode()).hexdigest()
    rnd = int(seed[:8], 16) / 0xFFFFFFFF
    jitter = 1.0 + 0.08 * (2.0 * rnd - 1.0)       # +/- 8 % contention proxy

    sc = io_cfg.stripe.stripe_count if io_cfg.stripe.stripe_count > 0 else 4
    # Sweet-spot model: throughput peaks around 8..32 stripes and good hint combos
    stripe_term = 1.0 - abs(sc - 16) / 32.0
    hint_bonus = 0.15 if (io_cfg.romio_hints.get("romio_cb_write") == "enable"
                          or io_cfg.ompio_mca.get("num_aggregators", 0)) else 0.0
    base = min(ntasks, 64) * 45.0                  # ~MiB/s per task ceiling
    write = max(50.0, base * max(0.2, stripe_term) * (0.85 + 0.3 * rnd)
                * (1 + hint_bonus)) * jitter
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


def _synthetic_multirep(io_cfg: IoConfig, ntasks: int, reps: int) -> str:
    """Concatenate rep-jittered synthetic logs with the launcher markers."""
    return "\n".join(
        f"=== MPIIO_EVOLVE_REP {k} ===\n"
        + _synthetic_ior_log(io_cfg, ntasks, k) for k in range(1, reps + 1))


# ---------------------------------------------------------------------------
# Core evaluation lifecycle
# ---------------------------------------------------------------------------


def evaluate(candidate: Mapping[str, Any],
             config_path: Path = DEFAULT_CONFIG,
             dry_run: Optional[bool] = None,
             state_dir: Optional[str] = None,
             _is_reference: bool = False) -> dict:
    """Evaluate one I/O configuration candidate. Never raises on bad input."""
    started = time.monotonic()
    cfg = load_config(config_path)
    # Decide dry-run ONCE: explicit flag or missing Slurm. Governs launcher,
    # container checks, probes AND workspace fallback on dev machines.
    simulate = bool(dry_run) or shutil.which("sbatch") is None
    ws = resolve_workspace(cfg, state_dir=state_dir, fallback_local=simulate)
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
    exit_code = -1.0
    reps = max(1, int(fit_cfg.get("repetitions", 1)))
    repeat_mode = str(fit_cfg.get("repeat_mode", "in_job"))
    w_mean = w_std = r_mean = r_std = None
    n_w = n_r = 0

    try:
        # -- 1. validate ------------------------------------------------------
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
        lustre = LustreConfigurator(dry_run=simulate)
        if (not simulate and lustre.dry_run
                and ws_cfg.get("lustre_strict", True)):
            raise RuntimeError(
                "lfs binary not found, but this is a real run: every "
                "stripe_count/stripe_size mutation would silently do nothing "
                "(a meaningless fitness landscape). Load the Lustre client or "
                "add its directory to PATH; or set workspace.lustre_strict: "
                "false to deliberately tune MPI-IO hints only.")
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
        resources = SlurmResources.from_config(
            cfg["cluster"], job_name=f"ev-{run_id}")
        prof_nt = prof.get("ntasks")
        if prof_nt:
            n = int(prof_nt)
            if n % resources.nodes == 0:
                # Rank-count override belongs in the #SBATCH header, so the
                # allocation stays the single source of truth (srun is bare).
                resources.tasks_per_node = n // resources.nodes
            else:
                logger.warning("profile ntasks=%d not divisible by nodes=%d"
                               " -- allocation (%d ranks) wins", n,
                               resources.nodes,
                               resources.nodes * resources.tasks_per_node)
        ntasks = resources.nodes * resources.tasks_per_node
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
        extra_srun = str(cfg["cluster"].get("extra_srun_args", "") or "")
        job_timeout = _time_limit_seconds(cfg["cluster"]) + 120
        wait = bool(cfg["cluster"].get("wait", True))
        prefer = str(fit_cfg.get("prefer", "max"))

        # -- 4b. N-fold sampling --------------------------------------------------
        # Physics-style: one measurement is an anecdote.
        #   in_job      -> `reps` passes inside ONE allocation, delimited by
        #                  === MPIIO_EVOLVE_REP k === markers (cheap; measures
        #                  intra-allocation noise only; --time covers all reps)
        #   across_jobs -> `reps` independent sbatch jobs (captures queue +
        #                  contention drift -- the honest estimator)
        if repeat_mode == "across_jobs":
            samples, stderr_parts, exit_codes = [], [], []
            res_k = None
            for k in range(1, reps + 1):
                rdir = run_dir / f"rep{k}"
                script_k = launcher.build_script(
                    rdir, resources, command, env,
                    extra_srun_args=extra_srun)
                res_k = launcher.submit(script_k, wait=wait, timeout=job_timeout)
                if res_k.simulated:
                    (rdir / "stdout.log").write_text(
                        _synthetic_ior_log(io_cfg, ntasks, k), encoding="utf-8")
                tp_k, err_k, _ = parse_log_files(res_k.stdout_path,
                                                 res_k.stderr_path)
                samples.append(tp_k.best(prefer))
                stderr_parts.append(err_k)
                exit_codes.append(res_k.exit_code)
            result = res_k
            stderr_text = "\n".join(t for t in stderr_parts if t)
            exit_code = max(exit_codes)
        else:
            script = launcher.build_script(
                run_dir, resources, command, env,
                extra_srun_args=extra_srun, repetitions=reps)
            result = launcher.submit(script, wait=wait, timeout=job_timeout)
            if result.simulated:
                (run_dir / "stdout.log").write_text(
                    _synthetic_multirep(io_cfg, ntasks, reps), encoding="utf-8")
            samples = parse_rep_throughputs(read_log(result.stdout_path), prefer)
            stderr_text = read_log(result.stderr_path)
            exit_code = result.exit_code

        # -- 6. statistics & scoring -----------------------------------------------
        # Score on the MEAN; the error bar is reported, never maximized away.
        w_mean, w_std, n_w = mean_std([w for (w, _r) in samples])
        r_mean, r_std, n_r = mean_std([r for (_w, r) in samples])
        no_data = w_mean is None and r_mean is None

        report: ErrorReport = profile_errors(
            stderr_text, exit_code=exit_code, throughput_zero=no_data)
        if not report.clean:
            feedback = report.feedback
            (run_dir / "feedback.txt").write_text(report.feedback, encoding="utf-8")

        score = _score(w_mean, r_mean, exit_code, fit_cfg)

    except Exception as exc:                                  # noqa: BLE001
        logger.exception("candidate evaluation aborted")
        score, w_mean, r_mean = 0.0, None, None
        feedback = (f"[CONFIG_ERROR] The candidate was rejected before or during "
                    f"submission: {type(exc).__name__}: {exc}. Fix the parameter "
                    f"values and stay inside the declared search space.")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "feedback.txt").write_text(feedback, encoding="utf-8")

    # -- 7. cleanup ------------------------------------------------------------
    _cleanup(run_dir, ws, cfg, keep_data=bool(ws_cfg.get("keep_data", False)))

    sem_w = (w_std / (n_w ** 0.5)) if (w_std and n_w > 1) else 0.0
    sem_r = (r_std / (n_r ** 0.5)) if (r_std and n_r > 1) else 0.0
    metrics = {
        "score": float(score),
        "write_mean_mib_sec": float(w_mean) if w_mean is not None else 0.0,
        "write_std_mib_sec": float(w_std) if w_std is not None else 0.0,
        "write_sem_mib_sec": float(sem_w),
        "read_mean_mib_sec": float(r_mean) if r_mean is not None else 0.0,
        "read_std_mib_sec": float(r_std) if r_std is not None else 0.0,
        "read_sem_mib_sec": float(sem_r),
        "n_repetitions": float(reps),
        "n_write_samples": float(n_w),
        "n_read_samples": float(n_r),
        "job_exit_code": float(exit_code),
        "runtime_sec": round(time.monotonic() - started, 3),
    }
    context = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "state_workspace": str(ws),
        "engine": engine,
        "benchmark_profile": profile_name,
        "repeat_mode": repeat_mode,
        "layout": layout_desc,
        "feedback": feedback,
    }

    # -- 8. measurement database + reference normalization ------------------------
    _append_measurement(ws, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "candidate_sha": hashlib.sha256(json.dumps(
            cand, sort_keys=True, default=str).encode()).hexdigest()[:12],
        "engine": engine, "profile": profile_name, "repeat_mode": repeat_mode,
        **metrics})
    if _is_reference:
        (ws / "reference_measure.json").write_text(
            json.dumps(metrics), encoding="utf-8")
    else:
        ref = _ensure_reference(cfg, ws, config_path, dry_run, state_dir)
        if ref:
            metrics["reference_write_mean_mib_sec"] = ref.get(
                "write_mean_mib_sec", 0.0)

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
    """Pick the active benchmark profile (epoch_io | legacy).

    A candidate may switch profiles with {"benchmark_profile": "<name>"}.
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
# Measurement database & reference normalization
# ---------------------------------------------------------------------------


def _append_measurement(ws: Path, record: Mapping) -> None:
    """Append one evaluation to <state_dir>/measurements.jsonl.

    A JSONL ledger of every measurement taken in this state directory: drift
    analysis, elite re-validation, and "was generation 7 just a quiet
    Lustre afternoon?" audits are all one pandas.read_json(lines=True) away.
    """
    try:
        with (ws / "measurements.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(record)) + "\n")
    except OSError as exc:
        logger.warning("measurement db write failed: %s", exc)


def _ensure_reference(cfg: Mapping, ws: Path, config_path: Path,
                      dry_run: Optional[bool],
                      state_dir: Optional[str] = None) -> Optional[dict]:
    """(Re)measure the fixed reference candidate when its stamp is stale.

    The reference gives a contemporaneous yardstick: filesystem contention
    drifts over hours, so absolute means across generations are not directly
    comparable -- means relative to a reference measured nearby in time are.
    Opt-in via fitness.reference_candidate; costs one extra evaluation per
    reference_max_age_min window.
    """
    fit_cfg = cfg.get("fitness", {}) or {}
    ref_spec = fit_cfg.get("reference_candidate")
    if not ref_spec:
        return None
    ref_file = Path(str(ref_spec))
    if not ref_file.is_absolute():
        ref_file = REPO_ROOT / ref_file
    if not ref_file.is_file():
        logger.warning("reference_candidate %s not found -- skipping", ref_file)
        return None
    stamp = ws / "reference_measure.json"
    max_age = float(fit_cfg.get("reference_max_age_min", 60)) * 60.0
    try:
        fresh = stamp.exists() and (time.time() - stamp.stat().st_mtime) < max_age
    except OSError:
        fresh = False
    if not fresh:
        try:
            ref_cand = json.loads(ref_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("reference_candidate unreadable: %s", exc)
            return None
        logger.info("re-measuring reference candidate %s", ref_file.name)
        evaluate(ref_cand, config_path=config_path, dry_run=dry_run,
                 state_dir=state_dir, _is_reference=True)
    try:
        return json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    ap.add_argument("--root", default=None,
                    help="deployment root on the parallel filesystem "
                         "(default: config workspace.root; env MPIIO_EVOLVE_ROOT)")
    ap.add_argument("--state-dir", default=None,
                    help="per-launcher state directory under the root "
                         "(env MPIIO_EVOLVE_STATE_DIR)")
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

    if args.root:
        os.environ["MPIIO_EVOLVE_ROOT"] = args.root
    evaluate(candidate, config_path=Path(args.config),
             dry_run=True if args.dry_run else None,
             state_dir=args.state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
