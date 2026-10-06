# mpiio_evolve — architecture & internals

Usage guide: [`../README.md`](../README.md). This document covers how the tool
works internally: the loop, modules, fitness/statistics, the two container
roles, and the invariants that make it cluster-safe.

## The loop

```
   CPU cluster (login / controller)                    GPU cluster
┌───────────────────────────────────────────┐   ┌──────────────────────┐
│  OpenEvolve controller  (controller.sif)  │   │  llama.cpp / vLLM    │
│   prompt-massager ──► mutation request ───┼───┤  OpenAI-compatible   │
│        ▲                                  │◄──┤  API over HTTP       │
│        │ metrics + natural-language       │   │  (NO_PROXY required) │
│        │ crash feedback                    │   └──────────────────────┘
│  evaluate.py  ◄── candidate dict (JSON)    │
│     │                                      │
│     ├─► infrastructure.py   Lustre layout, ROMIO hint file,
│     │                       OMPIO MCA env, synthetic $HOME, path jail
│     ├─► slurm_launcher.py   #SBATCH compiler + `sbatch --wait`
│     └─► parser.py           MiB/sec extraction + error profiler
└──────────────┬─────────────────────────▲──┘
               │ sbatch batch jobs        │ stdout/stderr logs
               ▼                          │
        Slurm compute nodes ═══ Lustre /lustre/rz/dbertini2 (all state) ══
```

Per iteration: sample parent(s) → LLM writes a mutated candidate JSON →
`evaluation_io.evaluate()` gates on JSON validity, then spawns
`evaluate.py -c <candidate>` → validate against the search space, apply the
Lustre layout, write the ROMIO hint file / OMPIO env, render the `#SBATCH`
script, `sbatch --wait` the benchmark, parse throughput + classify errors →
`EVAL_METRICS {json}` + `FITNESS: <score>` → scored child enters the
population database. Invalid mutants score 0.0 with feedback; the loop never
dies.

## Module map

| Module | Responsibility |
|---|---|
| `config.yaml` | Search-space boundaries, Slurm resource request, benchmark profiles, container declaration, fitness shaping, workspace layout |
| `openevolve_config.yaml` | OpenEvolve controller: iterations, islands, LLM ensemble, prompts |
| `evaluation_io.py` | OpenEvolve↔evaluate.py adapter: `evaluate(program_path) -> metrics dict`, failure dumps to `eval_failures/` |
| `infrastructure.py` | *Storage Hardware Modeler.* `lfs setstripe`, ROMIO plaintext hint files, `OMPI_MCA_io_ompio_*` env, `apptainer exec` prefix, synthetic `$HOME`, `ensure_inside()` path jail, hint whitelists, semantic search-space validation (`_size_key`: `'4M' == '4194304'`) |
| `slurm_launcher.py` | *Dynamic Script Compiler.* Renders the `#SBATCH` script with the cache-isolation export header, submits `sbatch --parsable --wait`, simulates when Slurm is absent |
| `parser.py` | *Log Aggregator & Error Profiler.* Max/Mean write/read speeds (IOR and simulator formats → MiB/sec); stderr classified into OOM / LUSTRE_LAYOUT / ENOSPC / MPI_LAUNCH / TIMEOUT / … with quoted evidence as LLM feedback |
| `evaluate.py` | Full evaluation lifecycle + `EVAL_METRICS`/`FITNESS` protocol; `MPIIO_EVOLVE_ROOT` / `MPIIO_EVOLVE_CONFIG` / `MPIIO_EVOLVE_STATE_DIR` overrides |
| `tools/launch_evolution.py` | OpenEvolve 0.4 library-API launcher: `Config` load, `output_dir`, async `run()`, thinking-kill-switch injection via `extra_body` |
| `tools/run_controller_sif.sh` | Confined launch: SIF discovery, env wiring, the bind wall (state rw / repo ro / `/etc/slurm` ro / munge socket / SPANK plugin) |
| `tools/run_controller_test.sh` | Bounded test profile: derives throwaway configs (`max_iterations: 2`, `repetitions: 1`), separate output tree |
| `tools/test_controller_sif.sh` | 12-check smoke suite (image, OpenEvolve, sinfo, sbatch with/without `--contain`, evaluator dry-run) |
| `tools/llm_thinking_test.sh` | Endpoint probe: does `enable_thinking:false` reach the server |
| `container/controller.def` | Controller image: Rocky 9.7, Python 3.11, OpenEvolve 0.4.0 + pinned deps, Slurm client (`--build-arg SLURM_VERSION`), CRB for `munge-devel` |
| `container/plasma_pp.def` | Benchmark image: Rocky 9.7 + PMIx + UCX + Open MPI (`--with-lustre --with-ucx --with-slurm`) + HDF5 + ADIOS2 + openPMD + EPOCH + WarpX + IOR + OSU |
| `container/build_container.sh` | Login-node plasma builder (`$HOME`-free), publishes `images/current.sif` |
| `benchmarks/epoch_io/` | Real-application fitness: EPOCH 3D LWFA checkpoint-stress deck; wrapper prints `aggregate write bandwidth` |

## The two container roles

| | `controller.sif` (the brain) | `images/current.sif` (the simulator) |
|---|---|---|
| where | login node, `apptainer exec --contain` | compute nodes, inside the generated job script |
| carries | Python 3.11, OpenEvolve, Slurm client | EPOCH, OpenMPI/ROMIO, UCX, HDF5/ADIOS2 |
| builds | `apptainer build --build-arg SLURM_VERSION=…` | `./container/build_container.sh` |
| deliberately lacks | Lustre client, nested apptainer | evolution stack, sbatch work |

**Confinement wall** (`run_controller_sif.sh`, all proven by
`tools/test_controller_sif.sh`): the controller sees only the Lustre state
directory (rw), the repo (ro), `/etc/slurm` (ro), the munge socket (ro,
explicit bind — `mungepath` inactive on this cluster) and the cluster SPANK
plugin (ro). `$HOME` and the rest of `/lustre` are invisible; submitted jobs
run entirely outside it. Consequences encoded in code: `MPIIO_EVOLVE_ROOT` is
pinned into the state dir; the `lfs`-probe is skipped inside the container
(`lustre_strict: false` → hints-only tuning; run the bare-metal controller to
evolve striping).

**MPI-IO component subtlety (plasma image):** `%environment` sets
`OMPI_MCA_io=romio341` — Open MPI 5.x with the **embedded ROMIO** component,
so ROMIO hint files (`MPIIO_HINTS`) are the primary tuning surface.
`evaluate.py` probes the image once per session; `"mpi_engine": "ompio"`
exports `OMPI_MCA_io=ompio` to switch surfaces.

**LLM quirks handled by the launcher:** thinking/reasoning models consume the
whole `max_tokens` before answering → `chat_template_kwargs.enable_thinking=
false` injected via the SDK's `extra_body` (the SDK rejects unknown kwargs);
`NO_PROXY` set for the endpoint host (login-node Squid); `random_seed` is
passed to the endpoint, so a fixed seed yields deterministic (twin) mutants —
use `null` for exploration.

## Candidate schema

```json
{
  "mpi_engine": "romio",
  "lustre":  { "stripe_count": 8, "stripe_size": "4M" },
  "romio":   { "romio_cb_write": "enable", "cb_nodes": 16, "cb_buffer_size": "4M" },
  "ompio":   { "num_aggregators": 8, "io_stripe_size": "1M", "fb_data_size": "1M" },
  "extra_env": { "FI_OFI_RX_SIZE": "16384" }
}
```

Values must lie inside `config.yaml → search_space` (semantic size matching);
unknown hint keys are dropped with a warning; `extra_env` keys must match
`env_prefix_allowlist` (`I_`, `ROMIO_`, `FI_`, `UCX_`, `MPIIO_`).

## Fitness protocol

```
EVAL_METRICS {"score": 26.4, "write_mean_mib_sec": 26.4, "write_sem_mib_sec": 1.1, "n_repetitions": 3, ...}
FITNESS: 26.4192
```

`score = w_write·write_mean + w_read·read_mean` (weights under `fitness:`).
Any failure yields `FITNESS: 0.0` with the reason delivered three ways:
stderr capture, `runs/<id>/feedback.txt` (classified, LLM-ready), and
`result.json → context.feedback`.

## Statistics: measuring, not guessing

One pass on shared Lustre is an anecdote (contention moves throughput
20–30%; our own seed re-measured 25.6 → 24.9 MiB/s, a ≈3% noise floor at
n=1). Every candidate is measured `fitness.repetitions` times and scored on
the **mean**, never the max:

| Mode | Samples | Cost |
|---|---|---|
| `in_job` | N passes inside one allocation | 1 job |
| `across_jobs` | N independent jobs — also queue/contention drift (honest) | N jobs |

`write_sem_mib_sec` (σ/√n) ships in the metrics so real gains separate from
lucky reads. Safeguards: `measurements.jsonl` ledger (every measurement ever,
one `pandas.read_json(lines=True)` away from drift audits), and optional
reference-candidate renormalization against hour-scale filesystem drift.

## Hard invariants

1. **No `$HOME`, ever.** Every generated script starts with an export block
   redirecting `HOME`, cache dirs, `TMPDIR`, … into `<state>/.fake_home/`.
2. **State confinement.** All mutable state under `<root>/<state_dir>`
   (`/lustre/rz/dbertini2/…`), one per launcher (`MPIIO_EVOLVE_STATE_DIR`);
   every filesystem mutation passes `ensure_inside()`.
3. **The loop never dies.** Validation, scheduler, and novel crash modes all
   degrade to `FITNESS: 0.0` + feedback.
4. **Auditable runs.** `<state>/runs/<ts>-<hash>/` keeps the exact
   `submit.sh`, hints, logs, `result.json`, `feedback.txt`; data pruned after
   scoring, old runs GC'd beyond `keep_runs`.

## Runtime environments

| Where | What runs | Python |
|---|---|---|
| controller (login node) | OpenEvolve + evaluator, `sbatch --wait` | 3.11 inside `controller.sif`, or Lustre venv (`bootstrap_controller.sh`) for bare-metal |
| compute nodes | benchmark only, inside `images/current.sif` | none — launcher never executes there |

The evaluator core keeps a **Python ≥3.9, zero-dependency, stdlib-YAML**
guarantee (`simple_yaml.py`, `config.generated.json` mirror via
`tools/compile_config.py`) — so `evaluate.py` alone still runs on a frozen
login node without the SIF.

## Benchmark profiles

| Profile | What it is | Fitness signal |
|---|---|---|
| `epoch_io` *(default)* | EPOCH 3D LWFA run (moving window, tracer + field/particle dumps) — real checkpoint I/O | `total SDF bytes / wall time` as `aggregate write bandwidth` |

*(The `ior_canary` probe is retired; IOR remains in the image for manual spot
checks.)*

## Tuning notes (Lustre + MPI-IO)

* `stripe_count` near a divisor of the OST count; `-1`/`0` legal probes.
* ROMIO: `romio_cb_write=enable` with `cb_nodes ≈ #OSTs` is the classic sweet
  spot (first measured mutant here: +6% at cb_nodes=16, pending SEM);
  `romio_ds_write=disable` avoids the sieving scratch dance — but **never**
  `cb_write=enable` + `ds_write=disable` together (site-measured RMW
  catastrophe; guardrailed).
* OMPIO: `num_aggregators ≈ OST count`, `io_stripe_size` matched to Lustre
  stripe size; `fb_data_size` trades rank memory for coalescing.
* Read-side regressions are penalized via the `w_read` weight.

## Repo layout

```
mpiio_evolve/
├── config.yaml / openevolve_config.yaml   # evaluation-side / controller-side config
├── config.generated.json                  # stdlib mirror (tools/compile_config.py)
├── evaluation_io.py                       # OpenEvolve adapter
├── evaluate.py                            # lifecycle + FITNESS protocol
├── infrastructure.py slurm_launcher.py parser.py simple_yaml.py
├── tools/
│   ├── run_controller_sif.sh              # production confined launch
│   ├── run_controller_test.sh             # bounded test profile
│   ├── run_controller.sh                  # bare-metal alternative
│   ├── launch_evolution.py                # OpenEvolve launcher (0.4 API)
│   ├── test_controller_sif.sh             # 12-check smoke suite
│   ├── llm_thinking_test.sh               # endpoint reasoning probe
│   ├── bootstrap_controller.sh            # bare-metal Lustre venv
│   └── compile_config.py preflight.sh …
├── container/
│   ├── controller.def + controller.sif    # the brain
│   ├── plasma_pp.def + build_container.sh # the simulator (→ images/current.sif)
├── benchmarks/epoch_io/                   # EPOCH 3D fitness deck
├── examples/candidate_{romio,ompio}.json  # seed candidates
├── images/                                # .sif + current.sif symlink (gitignored)
└── <state>/ runs/ tmp/ .fake_home/ …      # at workspace.root, per-launcher
```

## Roadmap

- [x] Containerized execution (plasma image + login-node builder)
- [x] Real-application fitness: EPOCH checkpoint benchmark profile
- [x] Confined containerized controller (`controller.sif`, `--contain` wall,
      smoke-tested submission path)
- [ ] Production deck calibration: mirror the real sim's dump cadence/volume
- [ ] `prompts/` templates encoding Lustre domain priors for the mutator
- [ ] Multi-client sweep (`tasks_per_node` as evolved dimension)
- [ ] `lfs df`/MDC-contention telemetry folded into fitness
- [ ] ADIOS2/openPMD backend profile (beyond raw MPI-IO)
