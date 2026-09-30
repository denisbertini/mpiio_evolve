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
        Slurm compute nodes ═══ Lustre /scratch (all state lives here)
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
| `benchmarks/epoch_io/` | Real-application fitness: EPOCH1D checkpoint-stress deck + rank-aware runner that reports `aggregate write bandwidth: <x> GiB/s` |

## Benchmark profiles

The fitness source is a profile selected by `benchmark.active` (or per
candidate via `{"benchmark_profile": "..."}`):

| Profile | What it is | Fitness signal |
|---|---|---|
| `epoch_io` *(default)* | EPOCH1D laser–solid run with dense field+particle SDF dumps — real MPI-IO behavior of the production application | `total SDF bytes / wall time`, printed by the wrapper as `aggregate write bandwidth` |
| `ior_canary` | IOR MPI-IO driver, independent files, `fsync` on write | IOR `Max/Mean Write/Read` lines — kept as the **Lustre health check**: if EPOCH scores tank but the canary is stable, the config is bad, not the filesystem |

## Containerized execution

Nothing runs bare: benchmark commands are automatically wrapped as

```
srun --mpi=pmix -n <ntasks> apptainer exec --home <ws>/.fake_home \
     --contain --bind <ws>  images/current.sif  <benchmark command>
```

Build the image **on the login node** (the def needs ~40 GB of scratch, not
`$HOME` — the script guarantees this):

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
EVAL_METRICS {"score": 2066.25, "write_mib_sec": 1406.44, "read_mib_sec": 1319.62, ...}
FITNESS: 2066.2500
```

`score = w_write·max_write + w_read·max_read` (weights and `max|mean` choice
live under `fitness:` in `config.yaml`). Any crash yields `FITNESS: 0.0` — and
never a dead loop: the exit code is always 0 for the controller, and the
reason for failure is delivered three ways:

1. stderr (captured by the controller),
2. `runs/<id>/feedback.txt` (classified, LLM-ready prose),
3. `runs/<id>/result.json` → `context.feedback`.

Example feedback a mutant receives after an OOM:

> `[OOM]` The job was OOM-killed: per-rank memory footprint exceeded the
> `--mem-per-cpu` allocation. Reduce buffer sizes (`cb_buffer_size`,
> `fb_data_size`) or aggregate fewer ranks per node. *(+ quoted log evidence)*

## Hard invariants

1. **No `$HOME`, ever.** The deployment host has no writable home directory.
   Every generated batch script begins with an export block redirecting
   `HOME`, `HF_HOME`, `TRITON_CACHE_DIR`, `TORCH_EXTENSIONS_DIR`,
   `NUMBA_CACHE_DIR`, `MPLCONFIGDIR`, `PIP_CACHE_DIR`, `XDG_*`, `TMPDIR`,
   `MPI_TMPDIR`, … into `.fake_home/` inside the Lustre workspace.
2. **Path jail.** All filesystem mutations pass through
   `infrastructure.ensure_inside()`: no candidate, log path, or cleanup
   operation can resolve outside the repository root on `/scratch`.
3. **The loop never dies.** Validation errors, `lfs` rejections, scheduler
   failures and novel crash modes all degrade to `FITNESS: 0.0` plus feedback.
4. **Auditable runs.** Each attempt is a self-contained `runs/<timestamp>-<hash>/`
   holding the exact `submit.sh` submitted, the materialized `mpiio_hints`,
   `stdout.log`, `stderr.log`, `result.json` and (on failure) `feedback.txt`.
   Data files are deleted after scoring unless `workspace.keep_data: true`;
   old runs are garbage-collected beyond `workspace.keep_runs`.

## Quick start

```bash
pip install -r requirements.txt          # PyYAML; everything else is stdlib

# Offline validation (no Slurm/Lustre needed). --dry-run renders the real
# submit.sh and synthesizes a parameter-sensitive IOR log so the whole
# measure → score → feedback loop can be exercised on a dev machine:
python3 evaluate.py --candidate examples/candidate_romio.json --dry-run
python3 evaluate.py --candidate examples/candidate_ompio.json --dry-run

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
├── infrastructure.py           # Lustre / ROMIO / OMPIO / container translation
├── slurm_launcher.py           # sbatch script compiler + --wait submit
├── parser.py                   # throughput regexes + error profiler
├── evaluate.py                 # OpenEvolve entrypoint (FITNESS: protocol)
├── container/
│   ├── plasma_pp.def           # full plasma HPC stack (Virgo2 production image)
│   └── build_container.sh      # login-node builder ($HOME-free)
├── benchmarks/
│   └── epoch_io/               # EPOCH1D checkpoint-stress fitness benchmark
├── examples/
│   ├── candidate_romio.json    # ROMIO-engine starting candidate (romio341 too)
│   └── candidate_ompio.json    # Open MPI io_ompio starting candidate
├── images/                     # .sif images + current.sif symlink (gitignored)
├── runs/                       # per-run artifacts (gitignored)
└── .fake_home/                 # synthetic $HOME cache tree (gitignored)
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
