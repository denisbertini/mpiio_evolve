#!/usr/bin/env python3
"""
ompio_sweep.py -- direct OMPIO tuning on Lustre (no evolution needed)
=====================================================================

Open MPI 6 removed ROMIO: OMPIO is the only MPI-IO engine. Its tunable
surface on Lustre is SMALL and ORTHOGONAL -- file layout (lfs) times the
fcoll aggregation knobs -- so the honest search strategy is exhaustive or
randomized sampling, not an LLM-in-the-loop evolutionary controller. This
tool is the complete tuner:

    config grid / random sample
      -> one evaluate() per point (full mpiio_evolve lifecycle: fresh dir,
         lfs setstripe, OMPI_MCA_* env, sbatch --wait, reps, rate parsing)
      -> append-only JSONL ledger (resume-safe, crash-safe)
      -> live best-so-far, final ranked table with mean +/- SEM
      -> winner.json + winner.sh (exact reproducible env + lfs commands)

Knobs (OMPIO semantics, cf. docs.open-mpi.org "MPI IO" hints section):
  lustre.stripe_count / stripe_size   layout, applied via lfs setstripe
  ompio.num_aggregators               == cb_nodes hint == io_ompio_num_aggregators
                                       0 = OMPIO auto-select
  ompio.bytes_per_agg                 == cb_buffer_size hint == io_ompio_bytes_per_agg

The tool forces mpi_engine=ompio: build_job_environment() then exports
OMPI_MCA_io=ompio, which the plasma image MUST respect -- its %env pin is
parameterized (${OMPI_MCA_io:-romio341}) for exactly this reason.

Usage on Virgo (controller host):
    python3 tools/ompio_sweep.py                      # 24-point random sample
    python3 tools/ompio_sweep.py --mode grid          # full cartesian product
    python3 tools/ompio_sweep.py --sample 12 --seed 7 # smaller, reproducible
    python3 tools/ompio_sweep.py --summary            # re-render from ledger
    python3 tools/ompio_sweep.py --dry-run            # offline plan, no cluster

Every evaluated point lands in <state>/ompio_sweep/ledger.jsonl; rerunning
skips configs already measured (exit-code-0) unless --fresh. SIGINT stops
after the current job and prints the table of what was measured.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import random
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evaluate import (DEFAULT_CONFIG, evaluate, load_config,  # noqa: E402
                      resolve_workspace)

# ---------------------------------------------------------------------------
# Search space: OMPIO on Lustre, defaults calibrated from the calibration
# ladder (stripe 1..32 straddles the contiguous-phase OST wall; aggregator
# counts bracket auto-select; agg buffers span the two-phase I/O regimes).
# Keys mirror the candidate schema: "section.key" -> candidate[section][key].
# ---------------------------------------------------------------------------
DEFAULT_SPACE = {
    "lustre.stripe_count":   [1, 8, 16, 32],
    "lustre.stripe_size":    ["1M", "4M"],
    # -1 = OMPIO auto-select (the MCA default); 0 is NOT auto -- it is a
    # degenerate aggregator count and stays out of the space.
    "ompio.num_aggregators": [-1, 2, 4, 8, 16, 32],
    "ompio.bytes_per_agg":   ["1M", "4M", "16M"],
}

# Baseline corner: calibrated layout, all OMPIO knobs automatic. Measured
# first, always: it anchors every ranking in absolute terms.
ANCHOR = {"mpi_engine": "ompio",
          "lustre": {"stripe_count": 16, "stripe_size": "4M"}}


def cand_sha(candidate: dict) -> str:
    return hashlib.sha256(json.dumps(
        candidate, sort_keys=True, default=str).encode()).hexdigest()[:12]


def load_space(path: Path | None) -> dict:
    if path is None:
        return dict(DEFAULT_SPACE)
    from simple_yaml import load as yaml_load   # repo's stdlib-only loader
    with open(path, encoding="utf-8") as fh:
        space = yaml_load(fh.read())
    bad = [k for k in space if k.split(".")[0] not in ("lustre", "ompio")]
    if bad:
        raise SystemExit(f"--space: unsupported sections {bad} "
                         f"(use lustre.* / ompio.*)")
    return space


def gen_configs(space: dict, mode: str, sample: int, seed: int,
                profile: str | None) -> list[dict]:
    keys = sorted(space)                          # deterministic order
    grid = list(itertools.product(*(space[k] for k in keys)))
    if mode == "sample" and sample < len(grid):
        rng = random.Random(seed)
        grid = rng.sample(grid, sample)

    configs = []
    for combo in grid:
        cand: dict = {"mpi_engine": "ompio"}
        if profile:
            cand["benchmark_profile"] = profile
        for key, value in zip(keys, combo):
            sec, name = key.split(".", 1)
            cand.setdefault(sec, {})[name] = value
        configs.append(cand)
    # de-duplicate against the anchor (same measured conditions -> one row)
    anchor_sha = cand_sha(ANCHOR | ({"benchmark_profile": profile} if profile else {}))
    configs = [c for c in configs if cand_sha(c) != anchor_sha]
    return configs


# ---------------------------------------------------------------------------
# Ledger (append-only JSONL: survives Ctrl-C, power cuts, reruns)
# ---------------------------------------------------------------------------

def ledger_path(ws: Path, sub: str) -> Path:
    p = ws / "ompio_sweep" / sub
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_ledger(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            done[rec["sha"]] = rec          # last write wins
        except (json.JSONDecodeError, KeyError):
            continue                        # tolerate a torn final line
    return done


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_row(rec: dict) -> str:
    c = rec["candidate"]
    lustre, ompio = c.get("lustre", {}), c.get("ompio", {})
    m = rec["metrics"]
    return (f"{rec['sha']}  stripe {lustre.get('stripe_count','?'):>2}@"
            f"{str(lustre.get('stripe_size','?')):<3}  aggs "
            f"{str(ompio.get('num_aggregators','auto')):>4}  buf "
            f"{str(ompio.get('bytes_per_agg','auto')):>5}  |  "
            f"{m.get('write_mean_mib_sec', 0.0):8.1f} MiB/s "
            f"+/- {m.get('write_sem_mib_sec', 0.0):5.1f}  "
            f"(n={int(m.get('n_write_samples', 0))})")


def summarize(path: Path, out_dir: Path) -> int:
    done = read_ledger(path)
    if not done:
        print("ledger empty -- nothing measured yet", file=sys.stderr)
        return 1
    ok = [r for r in done.values()
          if r["metrics"].get("job_exit_code", 1) == 0]
    ok.sort(key=lambda r: r["metrics"].get("write_mean_mib_sec", 0.0),
            reverse=True)
    print(f"\n=== OMPIO sweep: {len(ok)} configs measured, ranked "
          f"({path}) ===")
    for i, r in enumerate(ok, 1):
        print(f"{i:>3}. {fmt_row(r)}")
    failed = len(done) - len(ok)
    if failed:
        print(f"    ({failed} failed config(s) excluded; see ledger "
              f"job_exit_code)")
    if ok:
        best = ok[0]
        (out_dir / "winner.json").write_text(
            json.dumps(best["candidate"], indent=2), encoding="utf-8")
        c = best["candidate"]
        lu, om = c.get("lustre", {}), c.get("ompio", {})
        exports = ["export OMPI_MCA_io=ompio"]
        if "num_aggregators" in om:
            exports.append("export OMPI_MCA_io_ompio_num_aggregators="
                           f"{om['num_aggregators']}")
        if "bytes_per_agg" in om:
            exports.append("export OMPI_MCA_io_ompio_bytes_per_agg="
                           f"{om['bytes_per_agg']}")
        lfs = (f"lfs setstripe -c {lu.get('stripe_count', 0)} "
               f"-S {lu.get('stripe_size', '1M')} <OUTPUT_DIR>")
        (out_dir / "winner.sh").write_text(
            "# winning OMPIO config -- "
            f"{best['metrics'].get('write_mean_mib_sec', 0.0):.1f} MiB/s "
            f"(sha {best['sha']})\n{chr(10).join(exports)}\n# layout, applied "
            "to the checkpoint directory BEFORE the write:\n"
            f"{lfs}\n", encoding="utf-8")
        print(f"\nbest: {best['sha']} -> {out_dir/'winner.json'}, "
              f"{out_dir/'winner.sh'}")
    return 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Direct OMPIO tuning sweep (grid/random) on Lustre")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--mode", choices=("sample", "grid"), default="sample")
    ap.add_argument("--sample", type=int, default=24,
                    help="configs to draw in --mode sample (default 24)")
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--space", default=None,
                    help="YAML file overriding the default OMPIO space")
    ap.add_argument("--profile", default=None,
                    help="benchmark profile name (default: config active)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no Slurm, no lfs: validate + plan only")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the ledger (re-measure everything)")
    ap.add_argument("--summary", action="store_true",
                    help="render the table from the ledger and exit")
    ap.add_argument("--echo", action="store_true",
                    help="pass evaluate.py's EVAL_METRICS/FITNESS through")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    simulate = bool(args.dry_run)
    ws = resolve_workspace(cfg, state_dir=args.state_dir,
                           fallback_local=simulate)
    led = ledger_path(ws, "ledger.jsonl" if not args.dry_run
                      else "ledger_dryrun.jsonl")

    if args.summary:
        return summarize(led, led.parent)

    space = load_space(Path(args.space) if args.space else None)
    configs = gen_configs(space, args.mode, args.sample, args.seed,
                          args.profile)
    done = {} if args.fresh else read_ledger(led)
    if args.fresh:
        todo = configs
    else:  # resume: skip measured; retry failed (exit-code != 0)
        todo = [c for c in configs
                if cand_sha(c) not in done
                or done[cand_sha(c)]["metrics"].get("job_exit_code", 1) != 0]

    print(f"ompio_sweep: {len(configs)} configs (+1 anchor), "
          f"{len(done)} already in ledger, {len(todo)} to run"
          f"{' [DRY-RUN]' if simulate else ''}")
    print(f"ledger: {led}")

    def record(candidate: dict, metrics: dict) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(),
               "sha": cand_sha(candidate), "candidate": candidate,
               "metrics": metrics}
        with open(led, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    # Anchor first: absolute reference for every ranking below it.
    anchor_cand = dict(ANCHOR)
    if args.profile:
        anchor_cand["benchmark_profile"] = args.profile
    if cand_sha(anchor_cand) in done and not args.fresh:
        print(f"anchor {cand_sha(anchor_cand)} already measured")
    else:
        t0 = time.monotonic()
        buf = io.StringIO()
        with redirect_stdout(buf):
            m = evaluate(anchor_cand, config_path=Path(args.config),
                         dry_run=args.dry_run, state_dir=args.state_dir)
        if not simulate:
            record(anchor_cand, m)
        if args.echo:
            sys.stdout.write(buf.getvalue())
        anchor_row = fmt_row({"sha": cand_sha(anchor_cand),
                              "candidate": anchor_cand, "metrics": m})
        print(f"anchor: {anchor_row} [{time.monotonic()-t0:.0f}s]")

    measured = 0
    try:
        for i, cand in enumerate(todo, 1):
            sha = cand_sha(cand)
            t0 = time.monotonic()
            buf = io.StringIO()
            with redirect_stdout(buf):
                m = evaluate(cand, config_path=Path(args.config),
                             dry_run=args.dry_run, state_dir=args.state_dir)
            if not simulate:
                record(cand, m)
            if args.echo:
                sys.stdout.write(buf.getvalue())
            measured += 1
            row = fmt_row({"sha": sha, "candidate": cand, "metrics": m})
            print(f"[{i}/{len(todo)}] {row} [{time.monotonic()-t0:.0f}s]")
    except KeyboardInterrupt:
        print("\n-- interrupted: summarizing what was measured --")

    if simulate:
        print("(dry-run: no ledger writes; plan validated end-to-end)")
        return 0
    return summarize(led, led.parent)


if __name__ == "__main__":
    sys.exit(main())
