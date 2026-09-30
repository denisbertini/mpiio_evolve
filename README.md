# mpiio_evolve

LLM-in-the-loop evolutionary optimization of MPI-IO / Lustre storage stacks.

An [OpenEvolve](https://github.com/codelion/openevolve) controller on the CPU
cluster mutates **I/O configuration candidates** (not code), submits
micro-benchmarks to the local Slurm queue on a Lustre (`/scratch`) filesystem,
and feeds measured MiB/sec plus *semantic crash feedback* back to a
vLLM/Ollama-served model over the login-node reverse tunnel.

```
                     ┌────────────────────────────────────────────┐
 CPU cluster         │  OpenEvolve controller                     │
 (login/compute)     │   mutation prompt  ◄── metrics + feedback  │
        │            └──────────┬─────────────────▲───────────────┘
        │ candidate dict        │                 │ FITNESS / EVAL_METRICS
        ▼                       ▼                 │
┌───────────────── mpiio_evolve ──────────────────┴───────────────┐
│ evaluate.py ──► infrastructure.py ──► slurm_launcher.py         │
│      │              Lustre layout       #SBATCH + env header    │
│      │              ROMIO hints file    sbatch --wait           │
│      │              OMPIO MCA env array                         │
│      └──► parser.py ── MiB/sec extraction + error profiler ─────┘
└──────────────────────────────────────────────────────────────────┘
        │                                      ▲
        ▼                                      │ stdout/stderr logs
   Slurm compute nodes  ══ Lustre /scratch ══  vLLM/Ollama (GPU cluster,
                                               via HTTP/SSE reverse proxy)
```

## Modules

| File | Responsibility |
|---|---|
| `config.yaml` | Search-space boundaries, Slurm resources, benchmark command, fitness shaping |
| `infrastructure.py` | `lfs setstripe` control, ROMIO hint-file writer, `OMPI_MCA_io_ompio_*` env builder, synthetic-`$HOME` isolation |
| `slurm_launcher.py` | Dynamic `#SBATCH` script compiler with cache-isolation header, blocking `sbatch --wait` submission |
| `parser.py` | IOR/generic MiB/sec extraction; OOM / layout / MPI / ENOSPC / timeout stderr profiling into LLM-ready feedback text |
| `evaluate.py` | Lifecycle driver; `EVAL_METRICS {json}` + `FITNESS: <score>` stdout protocol |

## Hard invariants

1. **No `$HOME`, ever.** The deployment host has no writable home directory.
   Every cache (`HF_HOME`, `TRITON_CACHE_DIR`, `XDG_*`, `TMPDIR`, `MPI_TMPDIR`,
   pip/numba/matplotlib caches …) is exported in the *sbatch script header*
   into a synthetic `$HOME` under the workspace. All destructive operations
   pass through `infrastructure.ensure_inside()` — a mutated candidate can
   never touch paths outside `/scratch/.../mpiio_evolve`.
2. **Dual engine.** `mpi_engine: romio|ompio|auto` selects between a ROMIO
   plaintext hint file (`MPIIO_HINTS=…`, MPICH/Intel/Cray) and an
   `OMPI_MCA_io_ompio_*` environment array (Open MPI 4.x). Unknown hint keys
   are dropped with a warning instead of being exported.
3. **The loop never dies.** Every failure mode yields `FITNESS: 0.0` plus a
   classified natural-language explanation (`runs/<id>/feedback.txt` and
   stderr), which is exactly what the mutation model needs to improve the
   next generation.

## Quick start

```bash
pip install -r requirements.txt

# Offline smoke test (no Slurm/Lustre required -- synthesizes a
# parameter-sensitive IOR log so you can validate the whole loop):
python3 evaluate.py --candidate examples/candidate_romio.json --dry-run
python3 evaluate.py --candidate examples/candidate_ompio.json --dry-run

# On the real cluster (module load your MPI + IOR first, set account in config.yaml):
python3 evaluate.py --candidate examples/candidate_romio.json
```

Every run produces `runs/<timestamp>-<hash>/` containing `submit.sh`
(the exact script submitted, auditable), `mpiio_hints`, `stdout.log`,
`stderr.log`, `result.json` (metrics + layout + feedback).

## Candidate schema

```json
{
  "mpi_engine": "romio",
  "lustre":  { "stripe_count": 8, "stripe_size": "4M" },
  "romio":   { "romio_cb_write": "enable", "cb_nodes": 16, "cb_buffer_size": "4M" },
  "ompio":   { "num_aggregators": 8, "io_stripe_size": "1M" },
  "extra_env": { "FI_OFI_RX_SIZE": "16384" }
}
```

Values must lie inside the boundaries declared in `config.yaml → search_space`;
out-of-bounds candidates are rejected pre-submission with a feedback message.
`extra_env` keys must match `env_prefix_allowlist`.

## OpenEvolve integration

* **Function API** — import `evaluate(candidate: dict) -> dict[str, float]`
  and point OpenEvolve's evaluator at it. Metrics: `score`, `write_mib_sec`,
  `read_mib_sec`, `job_exit_code`, `runtime_sec`.
* **Subprocess API** — run `python evaluate.py --candidate <json>` and scrape
  the final `FITNESS: <float>` line. Crash feedback is on stderr and in
  `result.json:context.feedback`; wire that field into your prompt-massager
  so the LLM sees *why* a mutant scored 0.

Suggested prompt hook: append to the mutation system prompt

> You are evolving Lustre/MPI-IO configurations. Previous generation failures:
> {feedback} — adjust stripe_count, ROMIO collective buffering hints, or
> OMPIO aggregator counts accordingly. Stay inside the declared search space.

## Tuning notes for Lustre + MPI-IO

* `stripe_count` should generally sit near a divisor of your OST count; `-1`
  (broadcast, small files) and `0` (filesystem default) are legal probes.
* ROMIO: `romio_cb_write=enable` + `cb_nodes≈#OSTs` is the classic Lustre
  sweet spot; `romio_ds_write=disable` avoids the data-sieving scratch-file
  dance on shared filesystems.
* OMPIO: `num_aggregators≈OST count` with `io_stripe_size` matching the
  Lustre stripe size; `fb_data_size` trades memory for frontier coalescing —
  the OOM profiler explicitly points here when it fires.
* Fitness is `w_write·max_write + w_read·max_read` (see `fitness:` in
  `config.yaml`), so read-side regressions from over-aggressive
  `romio_cb_read` choices are penalized automatically.
