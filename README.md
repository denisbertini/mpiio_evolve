# mpiio_evolve

**Evolutionary optimization of MPI-IO and Lustre storage layouts, driven by an
LLM in the loop.**

`mpiio_evolve` is an autonomous search framework for HPC I/O tuning. An
[OpenEvolve](https://github.com/codelion/openevolve) controller running on the
CPU side of a Slurm cluster mutates *I/O configuration candidates* — Lustre
striping layouts, ROMIO hints, Open MPI `io_ompio` MCA variables — submits
micro-benchmarks (IOR, or a custom plasma physics simulation's I/O phase) to
the local Slurm queue, measures achieved MiB/sec, and feeds both the numbers
and *semantic crash diagnoses* back to a vLLM/Ollama-served model over an
HTTP/SSE reverse-proxy tunnel from the login node to the GPU cluster.

The unit of evolution is **configuration, not code**: every candidate is a
small JSON/YAML mapping that stays inside explicit search boundaries declared
in `config.yaml`.

## Why this exists

Lustre + MPI-IO tuning is a large, poorly documented, strongly coupled
parameter space:

* **Lustre layout** — `lfs setstripe -c <count> -S <size>` interacts with OST
  count, file size, and access pattern.
* **ROMIO** (MPICH / Intel MPI / Cray MPICH) — collective buffering hints
  (`romio_cb_write`, `cb_nodes`, `cb_buffer_size`, `romio_ds_write`, …) live
  in a plaintext hint file discovered through `MPIIO_HINTS`.
* **OMPIO** (Open MPI 4.x) — the same intent is expressed as an entirely
  different surface: `OMPI_MCA_io_ompio_num_aggregators`, `io_stripe_size`,
  `fb_data_size`, `coll_opt`, …

Grid search is unaffordable (each trial is a real batch job), and manual
tuning stalls on cross-engine knowledge silos. An LLM-in-the-loop evolutionary
search fills that gap: it proposes structured mutants, learns from *classified
failure text* (OOM kills, rejected stripe layouts, ENOSPC, MPI launch
failures), and converges on the throughput sweet spot for the actual
filesystem under real contention.

## Architecture

```
   CPU cluster (login / controller)                    GPU cluster
┌───────────────────────────────────────────┐   ┌──────────────────────┐
│  OpenEvolve controller                    │   │  vLLM / Ollama       │
│   prompt-massager ──► mutation request ───┼───┤  OpenAI-compatible   │
│        ▲                                  │◄──┤  API over HTTP/SSE   │
│        │ metrics + natural-language       │   │  reverse tunnel      │
│        │ crash feedback                    │   └──────────────────────┘
│  evaluate.py  ◄── candidate dict (JSON)    │
│     │                                      │
│     ├─► infrastructure.py   Lustre layout, ROMIO hint file,
│     │                       OMPIO MCA env, synthetic $HOME, path jail
│     ├─► slurm_launcher.py   #SBATCH compiler + `sbatch --wait`
│     └─► parser.py           MiB/sec extraction + error profiler
└──────────────┬─────────────────────────▲──┘
               │ srun batch jobs          │ stdout/stderr logs
               ▼                          │
        Slurm compute nodes ═══ Lustre /lustre/rz/dbertini2 (all state) ══
```

### Module map

| Module | Responsibility |
|---|---|
| `config.yaml` | Search-space boundaries, Slurm resource request, benchmark profiles, container declaration, fitness shaping, workspace layout |
| `infrastructure.py` | *Storage Hardware Modeler.* Applies `lfs setstripe -c {count} -S {size} {dir}`; writes ROMIO plaintext hint files; builds `OMPI_MCA_io_ompio_*` environment arrays; builds the `apptainer exec` prefix; creates the synthetic `$HOME` cache tree; enforces `ensure_inside()` path jail; whitelists known hints/keys |
| `slurm_launcher.py` | *Dynamic Script Compiler.* Renders a complete `#SBATCH` script per run with the cache-isolation export header, submits it with `sbatch --parsable --wait` (job exit code propagates), and simulates submission when Slurm is absent |
| `parser.py` | *Log Aggregator & Error Profiler.* Extracts Max/Mean write/read speeds (IOR and generic simulator formats, all units → MiB/sec); classifies stderr into OOM / LUSTRE_LAYOUT / ENOSPC / MPI_LAUNCH / TIMEOUT / HOME_WRITE / … with quoted log evidence formatted as LLM feedback |
| `evaluate.py` | *OpenEvolve entrypoint.* Validates the candidate, drives the full lifecycle, scores fitness, garbage-collects old runs, and speaks the `EVAL_METRICS {json}` + `FITNESS: <score>` stdout protocol |
| `container/plasma_pp.def` | Apptainer definition: Rocky 9.5 + PMIx 5.0.6 + UCX 1.18 + Open MPI 5.0.7 (`--with-lustre --with-ucx --with-slurm`) + Lustre client 2.16 + HDF5 + ADIOS2 + openPMD + EPOCH 4.19.5 + WarpX 25.01 + IOR + OSU |
| `container/build_container.sh` | Login-node image builder: isolates `HOME`/`TMPDIR`/`APPTAINER_CACHEDIR` into the workspace (no user home exists), supports root/`--fakeroot`/`--sudo`, publishes `images/current.sif`, validates the stack post-build |
| `benchmarks/epoch_io/` | Real-application fitness: EPOCH 3D LWFA checkpoint-stress deck + rank-aware runner that reports `aggregate write bandwidth: <x> GiB/s` |

## Benchmark profiles

The fitness source is a profile selected by `benchmark.active` (or per
candidate via `{"benchmark_profile": "..."}`):

| Profile | What it is | Fitness signal |
|---|---|---|
| `epoch_io` *(default)* | EPOCH 3D LWFA run (moving window, tracer dumps every 1 fs + field/particle dumps every 5 fs) — real checkpoint I/O of the production application, as used by our LWFA users | `total SDF bytes / wall time`, printed by the wrapper as `aggregate write bandwidth` |

*(The `ior_canary` Lustre-health probe is retired from the test suite; IOR
remains installed in the image for manual spot checks.)*

## Containerized execution

Nothing runs bare: benchmark commands are automatically wrapped as

```
srun --mpi=pmix -n <ntasks> apptainer exec --home <ws>/.fake_home \
     --contain --bind <ws>  images/current.sif  <benchmark command>
```

Build the image **on the login node** (the def needs ~40 GB of Lustre space,
not `$HOME` — the script guarantees this):

```bash
./container/build_container.sh            # auto mode: root / --fakeroot / --sudo
# → images/plasma_pp-<date>_<githash>.sif + symlink images/current.sif
```

**MPI-IO component subtlety (Virgo2 image):** `%environment` sets
`OMPI_MCA_io=romio341`, so although the MPI flavor is Open MPI 5.0.7, the
active MPI-IO component is the **embedded ROMIO** — ROMIO hint files
(`MPIIO_HINTS`) are the primary tuning surface. `evaluate.py` probes the
image once per session to detect this; a candidate that selects
`"mpi_engine": "ompio"` additionally exports `OMPI_MCA_io=ompio` so the
`OMPI_MCA_io_ompio_*` aggregator variables actually take effect.

## Candidate schema

```json
{
  "mpi_engine": "romio",
  "lustre":  { "stripe_count": 8, "stripe_size": "4M" },
  "romio":   { "romio_cb_write": "enable", "cb_nodes": 16, "cb_buffer_size": "4M",
               "romio_ds_write": "disable" },
  "ompio":   { "num_aggregators": 8, "io_stripe_size": "1M", "fb_data_size": "1M" },
  "extra_env": { "FI_OFI_RX_SIZE": "16384" }
}
```

* Every value must lie inside `config.yaml → search_space`; out-of-bounds
  mutants are rejected *before* submission with corrective feedback text.
* Unknown hint keys are dropped with a warning — a hallucinated hint can never
  silently reach the wire.
* `extra_env` keys must match `env_prefix_allowlist` (e.g. `I_`, `ROMIO_`,
  `FI_`, `UCX_`, `MPIIO_`).
* `mpi_engine` selects the dual-engine surface: `romio` (hint file +
  `MPIIO_HINTS=…`), `ompio` (`OMPI_MCA_io_ompio_*` exports), or `auto`
  (probe `mpiexec --version`).

## Fitness protocol

```
EVAL_METRICS {"score": 1684.48, "write_mean_mib_sec": 1186.98, "write_std_mib_sec": 223.53, "write_sem_mib_sec": 129.05, "read_mean_mib_sec": 994.99, "n_repetitions": 3, ...}
FITNESS: 1684.4767
```

`score = w_write·write_mean + w_read·read_mean` (weights and the per-rep
`max|mean` line choice live under `fitness:` in `config.yaml`). Any crash
yields `FITNESS: 0.0` — and never a dead loop: the exit code is always 0 for
the controller, and the reason for failure is delivered three ways:

1. stderr (captured by the controller),
2. `runs/<id>/feedback.txt` (classified, LLM-ready prose),
3. `runs/<id>/result.json` → `context.feedback`.

Example feedback a mutant receives after an OOM:

> `[OOM]` The job was OOM-killed: per-rank memory footprint exceeded the
> `--mem-per-cpu` allocation. Reduce buffer sizes (`cb_buffer_size`,
> `fb_data_size`) or aggregate fewer ranks per node. *(+ quoted log evidence)*

## Statistics: measuring, not guessing

A single pass on shared Lustre is an anecdote — contention alone can move
throughput 20–30%. So **every candidate is measured `fitness.repetitions`
times and scored on the arithmetic mean**, with the error bar reported, never
maximized away:

| Mode | What it samples | Cost |
|---|---|---|
| `in_job` | N benchmark passes inside **one** allocation (intra-run noise only; Slurm `--time` must cover all N) | 1 job |
| `across_jobs` | N **independent** sbatch jobs — also samples queue + contention drift (the honest estimator) | N jobs |

`EVAL_METRICS` carries `write_mean_mib_sec ± write_std_mib_sec` and the
standard-error-of-the-mean `write_sem_mib_sec` (σ/√n), so the controller can
tell a real +8% gain from a lucky read. Two further safeguards:

* **`measurements.jsonl`** — a per-state-directory JSONL ledger of every
  measurement ever taken (candidate hash, engine, profile, mean/std/n).
  Drift audits and elite re-validation are one `pandas.read_json(lines=True)`
  away.
* **Reference normalization** (opt-in) — a fixed `reference_candidate` is
  re-measured whenever older than `reference_max_age_min`; its contemporaneous
  write mean rides along as `reference_write_mean_mib_sec`, giving a yardstick
  that cancels hour-scale filesystem drift so generations stay comparable.

## Hard invariants

1. **No `$HOME`, ever.** The deployment host has no writable home directory.
   Every generated batch script begins with an export block redirecting
   `HOME`, `HF_HOME`, `TRITON_CACHE_DIR`, `TORCH_EXTENSIONS_DIR`,
   `NUMBA_CACHE_DIR`, `MPLCONFIGDIR`, `PIP_CACHE_DIR`, `XDG_*`, `TMPDIR`,
   `MPI_TMPDIR`, … into `<state_dir>/.fake_home/` inside the Lustre
   state directory.
2. **Deployment root & per-launcher state directory.** All mutable state
   lives under `<DEPLOY_ROOT>/<state_dir>` — on Virgo2,
   `/lustre/rz/dbertini2/<state_dir>` (there is no `/scratch`). Each user or
   controller instance launches with its own state directory
   (`--state-dir NAME`, `MPIIO_EVOLVE_STATE_DIR`, or `workspace.state_dir`),
   so concurrent evolutions never collide. All filesystem mutations pass
   through `infrastructure.ensure_inside()`: no candidate, log path, or
   cleanup operation can resolve outside its own state directory. Off-cluster
   `--dry-run` invocations fall back to `.dev_state/<state_dir>` in the repo.
3. **The loop never dies.** Validation errors, `lfs` rejections, scheduler
   failures and novel crash modes all degrade to `FITNESS: 0.0` plus feedback.
4. **Auditable runs.** Each attempt is a self-contained
   `<state_dir>/runs/<timestamp>-<hash>/`
   holding the exact `submit.sh` submitted, the materialized `mpiio_hints`,
   `stdout.log`, `stderr.log`, `result.json` and (on failure) `feedback.txt`.
   Data files are deleted after scoring unless `workspace.keep_data: true`;
   old runs are garbage-collected beyond `workspace.keep_runs`.

## Runtime environments (who runs what, and with which Python)

| Where | What runs | Python need |
|---|---|---|
| **Login node** (has `$HOME`) | OpenEvolve controller + `evaluate.py` / `infrastructure.py` / `slurm_launcher.py` / `parser.py`, `lfs setstripe`, `sbatch --wait` | **frozen system Python 3.9, zero dependencies** (optional Lustre venv via `tools/bootstrap_controller.sh`) |
| **Compute nodes** (NO `$HOME`) | only the benchmark, inside `images/current.sif` (`srun apptainer exec …`) | none — the launcher never executes there |

Because the compute-side `$HOME` does not exist, the generated batch script's
export header (`HOME`, caches, `TMPDIR`, …) is what makes jobs run; no
launcher Python is involved on the compute side.

**Python 3.9 / no-PyYAML guarantee for the evaluator** (enforced by tests):

* `simple_yaml.py` — bundled stdlib loader for the YAML subset used by
  `config.yaml`; `load_config()` chain is
  `*.json → PyYAML (if present) → simple_yaml → config.generated.json`.
* `tools/compile_config.py` — regenerates the committed `config.generated.json`
  mirror (`--check` verifies sync). Use
  `python3 evaluate.py --config config.generated.json` on a strictly bare node.
* Syntax gate: every module parses under `ast.parse(feature_version=(3,9))`;
  no 3.10+ syntax or stdlib APIs.

## Controller deployment

> **"But the login node has no GPU!"** — correct, and it does not need one.
> OpenEvolve is a pure-Python orchestrator: HTTP/SSE client for the LLM,
> string diffing, `sbatch` spawning, log parsing, a small artifact DB. It
> idles waiting on the network and on Slurm. All token generation happens on
> the **GPU cluster** (vLLM/Ollama), all benchmark I/O on the **compute
> nodes**. Controller footprint: <1 core, ~0.5-2 GB RAM — login-node legal
> courtesy (tmux + nice) rather than policy problem.


The heavy Python stack (OpenEvolve, `openai` client) must **not** go into the
plasma `.def` (it is rebuilt only for physics changes, and it never hosts
`sbatch`/`lfs` work). Two supported shapes:

1. **Lustre venv (recommended):** `./tools/bootstrap_controller.sh
   --openevolve` creates `.controller_env/` with every cache pinned into the
   workspace (`$HOME` untouched, `lfs setstripe -c 4` on the venv for
   small-file fan-out) and best-effort PyYAML — the evaluator still works if
   the install fails offline. Launch with `tools/run_controller.sh` inside
   tmux; it defaults `MPIIO_EVOLVE_STATE_DIR=$USER` and points
   `OPENAI_API_BASE` at the login-node tunnel. OpenEvolve supports Python
   ≥ 3.9, so the frozen system interpreter suffices.
2. **Thin controller container + mailbox:** a small `python:3.12-slim`-based
   image runs OpenEvolve only; it never calls `sbatch`. It writes candidate
   JSON into `queue/pending/`; a pure-stdlib 3.9 host process (`evaluate.py`)
   consumes, evaluates, and writes results to `queue/done/`. This keeps Slurm
   clients host-native (no version-drift bind-mounts, no nested Apptainer).

## Quick start

```bash
# No install needed on the login node (stdlib Python 3.9). Optional dev extra:
pip install PyYAML                        # only for the fuller YAML parser

# Offline validation (no Slurm/Lustre needed). --dry-run renders the real
# submit.sh and synthesizes a parameter-sensitive IOR log; if the configured
# Lustre root is unreachable it falls back to .dev_state/<state_dir>/:
python3 evaluate.py --candidate examples/candidate_romio.json --dry-run

# Per-launcher state directory under the Lustre deployment root
# (/lustre/rz/dbertini2/<you>):
python3 evaluate.py --candidate examples/candidate_romio.json --state-dir "$USER"
# Dev machine without /lustre:  --root .   (state goes into the repo)

# On the cluster: build the image once (login node), then:
./container/build_container.sh
python3 evaluate.py --candidate examples/candidate_romio.json
python3 evaluate.py --candidate - --dry-run < my_mutant.json   # JSON via stdin
```

## OpenEvolve integration

* **Subprocess API** — run `python evaluate.py --candidate <json>` per trial
  and scrape the `FITNESS:` line; attach `result.json:context.feedback` to the
  next mutation prompt.
* **Function API** — `from evaluate import evaluate`;
  `evaluate(candidate: dict) -> dict[str, float]` returns numeric metrics
  (`score`, `write_mib_sec`, `read_mib_sec`, `job_exit_code`, `runtime_sec`).

Suggested mutation-prompt preamble:

> You are evolving Lustre/MPI-IO configurations for a plasma-physics I/O
> benchmark. Previous-generation failures: {feedback}. Adjust stripe_count,
> ROMIO collective-buffering hints, or OMPIO aggregator counts accordingly.
> Stay strictly inside the declared search space.

## Tuning notes (Lustre + MPI-IO)

* `stripe_count` generally wants to sit near a divisor of the filesystem's OST
  count; `-1` (broadcast) and `0` (fs default) are legal probes for small or
  metadata-heavy files.
* ROMIO: `romio_cb_write=enable` with `cb_nodes ≈ #OSTs` is the classic Lustre
  sweet spot; `romio_ds_write=disable` avoids the data-sieving scratch-file
  dance on shared filesystems.
* OMPIO: set `num_aggregators ≈ OST count` and match `io_stripe_size` to the
  Lustre stripe size; `fb_data_size` trades rank memory for frontier
  coalescing — the OOM profiler explicitly points here when it fires.
* Read-side regressions from over-aggressive `romio_cb_read` choices are
  penalized automatically via the `w_read` fitness weight.

## Repo layout

```
mpiio_evolve/
├── config.yaml                 # search space + cluster + container + profiles
├── config.generated.json       # stdlib-parseable mirror (tools/compile_config.py)
├── infrastructure.py           # Lustre / ROMIO / OMPIO / container translation
├── slurm_launcher.py           # sbatch script compiler + --wait submit
├── parser.py                   # throughput regexes + error profiler
├── evaluate.py                 # OpenEvolve entrypoint (FITNESS: protocol)
├── simple_yaml.py              # stdlib-only YAML-subset loader (no PyYAML needed)
├── tools/
│   └── compile_config.py       # config.yaml -> config.generated.json (+ --check)
├── container/
│   ├── plasma_pp.def           # full plasma HPC stack (Virgo2 production image)
│   └── build_container.sh      # login-node builder ($HOME-free)
├── benchmarks/
│   └── epoch_io/               # EPOCH 3D LWFA checkpoint-stress fitness benchmark
├── examples/
│   ├── candidate_romio.json    # ROMIO-engine starting candidate (romio341 too)
│   └── candidate_ompio.json    # Open MPI io_ompio starting candidate
├── images/                     # .sif images + current.sif symlink (gitignored)
├── .controller_env/            # Lustre venv for controller (gitignored)
└── <state_dir>/                # per-launcher state at workspace.root, e.g.
    ├── runs/  tmp/  .fake_home/   /lustre/rz/dbertini2/alice/...
```

## Roadmap

- [x] Containerized execution (plasma image + login-node builder)
- [x] Real-application fitness: EPOCH checkpoint benchmark profile
- [ ] Production deck calibration: mirror the real sim's dump cadence/volume
- [ ] `prompts/` templates encoding Lustre domain priors for the mutator
- [ ] Native OpenEvolve evaluator config wired to the subprocess protocol
- [ ] Multi-client sweep (vary `tasks_per_node` as an evolved dimension)
- [ ] `lfs df`/MDC-contention telemetry folded into the fitness signal
- [ ] ADIOS2/openPMD backend profile (beyond raw MPI-IO)

## Requirements

* Python ≥ 3.9, PyYAML (rest is standard library)
* Real runs: Slurm (`sbatch ≥ 17.02` for `--parsable --wait`), Lustre client
  (`lfs`), IOR (or any benchmark whose stdout matches the parser patterns)
* Controller side: OpenEvolve + an OpenAI-compatible endpoint (vLLM/Ollama)
