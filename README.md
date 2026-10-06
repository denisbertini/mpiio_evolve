# mpiio_evolve

**Evolutionary optimization of MPI-IO and Lustre storage layouts, driven by an
LLM in the loop.**

An [OpenEvolve](https://github.com/codelion/openevolve) controller runs
confined on a Slurm login node (inside `controller.sif`), mutates *I/O
configuration candidates* (Lustre striping, ROMIO hints, OMPIO MCA variables),
submits a real benchmark (EPOCH 3D) to the cluster, measures achieved MiB/sec,
and feeds numbers plus classified failure text back to an OpenAI-compatible
LLM endpoint. The unit of evolution is **configuration, not code** — every
candidate is a small JSON that must stay inside the search space declared in
`config.yaml`.

📐 Internals (architecture, modules, fitness/statistics, invariants):
[`docs/architecture.md`](docs/architecture.md)

---

## 1 · One-time setup

```bash
git clone <repo> && cd mpiio_evolve
```

1. **Controller image** — Python 3.11 + OpenEvolve + a cluster-matched Slurm
   client. The Slurm version must match the cluster (default: 26.05.4):

   ```bash
   sbatch --version                      # check the cluster's version
   apptainer build --build-arg SLURM_VERSION=<x.yy.z> \
       container/controller.sif container/controller.def
   ```

2. **Benchmark image** — the plasma-physics stack (EPOCH etc.) the benchmark
   jobs run in. `images/current.sif` must resolve to it (either built here or
   symlinked to an existing plasma `.sif`):

   ```bash
   ./container/build_container.sh        # publishes images/current.sif
   #   ...or:  ln -sfn /abs/plasma_pp.sif images/current.sif
   ```

3. **Configure** — edit `config.yaml`:
   * `cluster.account` / `cluster.partition` — must be valid for you
     (`sacctmgr -nP show assoc user=$USER format=account,partition`)
   * `workspace.root` (default `/lustre/rz/dbertini2`) — writable Lustre area
   * and `openevolve_config.yaml` → `llm.api_base` / model (default
     `http://ccdev0022.hpc.gsi.de:8781/v1`)

## 2 · Smoke test (5 min, no evolution)

```bash
./tools/test_controller_sif.sh        # expect: 12 PASS, 0 FAIL
```

Checks image contents, OpenEvolve install, Slurm client auth, real `sbatch`
submissions with and without `--contain`, and the evaluator dry-run. If
anything fails, its output names the fix. Optional LLM probe:

```bash
./tools/llm_thinking_test.sh          # reasoning off? endpoint reachable?
```

## 3 · Run it

**Bounded test run first** (~15 min: 2 iterations × 1 repetition, throwaway
config copies, separate output tree — nothing to revert):

```bash
tmux new -s evolve_test
./tools/run_controller_test.sh
```

**Production campaign** (40 iterations × 3 repetitions ≈ 10 h):

```bash
tmux new -s evolve
./tools/run_controller_sif.sh openevolve_config.yaml
```

* Stop any time: `Ctrl-C` → graceful shutdown; checkpoints every 5 iterations
  under `…/openevolve_output/checkpoints/` — relaunch resumes the campaign.
* Run inside `tmux` always: a detached ssh session otherwise kills the
  controller mid-benchmark.

## 4 · Watch it

```bash
tail -f $(ls -t /lustre/rz/dbertini2/ppio_tune/*/openevolve_output/logs/* | head -1)
squeue -u $USER
tail -f $(ls -td /lustre/rz/dbertini2/ppio_tune/$USER/runs/* | head -1)/stdout.log
```

| artifact | location |
|---|---|
| live log / iteration scores | tmux + `openevolve_output/logs/` |
| best candidate so far | `openevolve_output/best/best_program.json` |
| resume checkpoints | `openevolve_output/checkpoints/` (every 5 iterations) |
| per-evaluation forensics | `<state>/runs/<id>/`: rendered `submit.sh`, `mpiio_hints`, `stdout.log`, `stderr.log`, `result.json`, `feedback.txt` (on failure) |
| failed-evaluator dumps (bug forensics) | `openevolve_output/eval_failures/` — **should be empty** |
| measurement ledger | `<state>/measurements.jsonl` |

A candidate that scores `0.0` is an *answer*, not a crash: `feedback.txt` /
the CONFIG_ERROR text says why (out-of-space value, submission rejection,
benchmark crash) and the LLM is told so it can correct course.

## 5 · The safety model: what the LLM can — and cannot — do

An LLM in the loop is only acceptable on a shared cluster if its power is
structured away, not promised away. In `mpiio_evolve` the model is treated as
**untrusted input at every step it touches**, and there is exactly *one* thing
it can do in the world:

> **The LLM's only actuator is a JSON configuration candidate.** It never
> writes files, never runs commands, never sees Slurm. Everything downstream
> of it is deterministic, bounded code.

Five layers stand between a hallucinating model and your cluster:

1. **No code execution — by design.** The evolved artifact is a pure-data
   candidate JSON (no diffs, no scripts, no free text reaching any shell).
   Malformed JSON is rejected in microseconds with zero side effects.
2. **Declared search space, enforced before submission.** Every hint key and
   value must appear in `config.yaml → search_space` (with byte-semantic
   matching: `4M ≡ 4194304`). Unknown hints are *dropped*, site-banned poison
   combos (e.g. `cb_write=enable` + `ds_write=disable`) are refused outright,
   and `extra_env` keys must match an explicit prefix allowlist (`ROMIO_`,
   `UCX_`, …). A made-up value cannot silently reach the wire.
3. **OS-level confinement of the controller itself.** The agent loop runs
   inside `controller.sif` under `apptainer exec --contain`: it sees only the
   Lustre state directory (rw), the repo (ro), `/etc/slurm` (ro), the munge
   socket (ro) — and *nothing else*: no `$HOME`, no rest of `/lustre`, no host
   `/usr`. The code cannot modify itself (read-only bind). Every filesystem
   mutation inside the evaluator additionally passes an `ensure_inside()` path
   jail scoped to the state directory. Submitted benchmark jobs run as the
   same user that submits interactively — with no more privilege than usual.
4. **Bounded blast radius per experiment.** One Slurm allocation at a time
   (`parallel_evaluations: 1` by default), a hard `time_limit` per evaluation
   kills runaway jobs, a global iteration budget ends the campaign, the
   controller runs `nice -n 5` (login-node courtesy), and old run data is
   garbage-collected automatically. The worst a catastrophically bad candidate
   can cost is one evaluation window.
5. **The loop never dies — and never hides anything.** Any failure — invalid
   mutant, rejected layout, OOM, MPI crash, scheduler error — degrades to a
   scored zero plus classified, human-readable feedback (`feedback.txt`), and
   the campaign continues. Every attempt remains auditable on disk: the exact
   `submit.sh` that was submitted, the hint files, full job stdout/stderr, and
   the parsed metrics (`result.json`, `measurements.jsonl`).

Net guarantee for the cluster admin, in one sentence: **the agent can only
propose values inside a list you declared legal, can only consume resources
you sized in `config.yaml`, runs inside a container that cannot see anything
you did not explicitly bind into it, and leaves a complete paper trail for
every experiment it ever ran.**

## 6 · Runtime parameters

| file | key | meaning |
|---|---|---|
| `config.yaml` | `cluster.account`, `cluster.partition` | where jobs run |
| | `fitness.repetitions` (3) / `repeat_mode` | statistical confidence per candidate |
| | `search_space.*` | what the LLM may legally mutate |
| | `workspace.lustre_strict` | `true` = require `lfs` (bare-metal controller); container runs are hints-only → `false` |
| | `cluster.time_limit` | damage cap per evaluation |
| `openevolve_config.yaml` | `max_iterations` (40) | campaign length |
| | `llm.api_base`, `llm.models`, `max_tokens` | the endpoint (keep `max_tokens ≥ 8192` for thinking models, or thinking off via launcher) |
| | `random_seed` | `null` for exploration diversity; `42` for exact repro |

## 7 · Troubleshooting

| symptom | cause / fix |
|---|---|
| `sinfo: libslurmfull.so: cannot open shared object` | host binds its `/usr/lib64/slurm` over the image's; launcher binds the plugin — image `%files` carries unversioned copies |
| `Munge encode failed: /var/run/munge/munge.socket.2` | `mungepath` inactive in apptainer.conf; `run_controller_sif.sh` binds the socket explicitly |
| `Failed to initialize plugin stack … singularity-exec.so` | cluster SPANK plugin invisible in container; runner binds `/usr/libexec/slurm-singularity-exec.so:ro` |
| `No valid code found`, `completion_tokens == max_tokens` | thinking model ate the budget; launcher injects `enable_thinking:false` (probe: `tools/llm_thinking_test.sh`) |
| `CONFIG_ERROR … outside the declared search space` for `'4194304'` vs `'4M'` | fixed: validators match byte-equivalent spellings |
| `Read-only file system: /lustre/…` during eval | workspace outside the confinement wall; runners pin `MPIIO_EVOLVE_ROOT` into the bound state dir |
| `container runtime 'apptainer' not found` | probe skipped inside the controller SIF (runtime is needed on compute nodes); `MPIIO_EVOLVE_SKIP_RUNTIME_PROBE=1` to force-skip anywhere |
| Squid 403 from the LLM endpoint | login-node proxy; runners export `NO_PROXY` for the endpoint host |
| job stays `PD` / `Invalid account` | `cluster.account`/`partition` — check with `sacctmgr` (§1.3) |

## 8 · Bare-metal alternative

No container for the controller? `./tools/bootstrap_controller.sh --openevolve`
builds a Lustre-resident venv and `./tools/run_controller.sh` runs the same
loop natively (host `lfs` available → set `workspace.lustre_strict: true` to
evolve striping as well).

## Requirements

* Login node: `apptainer`, `git`; controller stack lives in `controller.sif`
  (Python 3.11, OpenEvolve 0.4.0 pinned, Slurm client 26.05.4)
* Cluster: Slurm ≥ 17.02 (`sbatch --parsable --wait`), Lustre, benchmark image
* LLM: any OpenAI-compatible endpoint (vLLM/llama.cpp/Ollama), reachable
  from the login node (mind the proxy)
