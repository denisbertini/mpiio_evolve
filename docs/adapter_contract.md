# Application Adapter Contract (v1)

How a new I/O application or benchmark — **including ones we have never
seen** — becomes a fitness instrument for mpiio_evolve. The evolution
machinery (candidates, search space, Slurm, statistics, drift reference)
is application-agnostic by construction; an adapter only answers three
questions: *how do you start it, where does it write, how do we count its
bytes*.

## What the harness guarantees every run

| Guarantee | Mechanism |
|---|---|
| Fresh per-rep output dir | `setup.sh` creates `<data_dir>/rep<k>`; the run's writes are exactly its contents |
| Lustre layout under test | `lfs setstripe -c … -S …` applied to `<data_dir>` before the job (candidate `lustre.*`) |
| MPI hints under test | `ROMIO_HINTS` file / `OMPI_MCA_io_ompio_*` exported into the job (candidate `romio.*` / `ompio.*`) |
| Rank count | owned by the `#SBATCH` header; `srun` steps inherit it |
| Container execution | every rank: `apptainer exec <image> <command>` via `srun --mpi=pmix` |
| Barrier semantics | `srun` returning == all ranks closed their files; `measure_script` runs after, race-free, host shell |
| Repetition index | `MPIIO_EVOLVE_REP` env in setup/run/measure of the same rep |
| Wall stamps | `measure_script <data_dir> <t0_ns> <t1_ns>` bracket the srun step |

## What the adapter must provide (one YAML profile block)

```yaml
profiles:
  my_app:
    command:       "bash {repo}/benchmarks/generic/run.sh {data_dir} -- /path/to/my_app [args]"
    setup_script:  "bash {repo}/benchmarks/generic/setup.sh {data_dir}"
    measure_script: "bash {repo}/benchmarks/generic/measure.sh --strategy <S> {data_dir}"
```

Templates expand `{repo}`, `{data_dir}`, `{ntasks}` — no other `{}` in
these strings (they pass through `str.format`).

The **only fitness interface** is one line, by any means:

```
aggregate write bandwidth: <X> GiB/s
```

## Measurement strategies (onboarding ladder)

Pick by how little the application needs to change — the goal is *zero*:

1. **`regex`** — the app already prints throughput (IOR-style lines are
   recognized by `parser.py` out of the box). `measure_script` may even be
   omitted if the pattern matches; the whole job log is scanned.
2. **`filesys_delta`** — app writes files into its rep dir but prints
   nothing. `du --apparent-size` over the srun wall time. No cooperation
   at all; includes startup overhead (honest conservative floor).
3. **`jsonl`** — app emits JSON-lines metrics with numeric payload/time
   fields (`--bytes-key`, `--seconds-key`, `--file` configurable;
   defaults match pio-bench). Most precise: app-measured I/O time only.
4. **`darshan`** *(planned, fitness v2)* — wrap the binary with
   `darshan-runtime` (already in the image recipe); score `io_only_bw`
   from the Darshan log. Truly universal: per-API bytes and timing
   without the app knowing anything.

Never score on `max`; repetitions are averaged (see `fitness:` — the
shared-Lustre rule).

## Rules for the application itself

* Pass **empty/NULL `MPI_Info`** to `MPI_File_open` (or don't set hints
  internally) unless the flags were given — otherwise it silently
  overrides the candidate's `ROMIO_HINTS`. pio-bench ≥ v0.2.1 complies
  (`make_romio_info` is pass-through by default); this is the single most
  common way a "tuned" benchmark secretly ignores the tuning.
* Write inside the rep dir (cwd, or take `{data_dir}` as an argument).
* No interactive prompts inside ranks except stdin-fed (see epoch shim).
* Keep correctness checking (`--verify`-style) OUT of the fitness runs;
  verification traffic pollutes the measured window. Correctness belongs
  to smoke tests and to the drift reference's occasional verified runs.

## Onboarding checklist for an unknown app

1. Build it **against the image's toolchain** (same container, or mount a
   user SIF via a per-profile image override — see `container.image`).
2. One profile block above; choose the *laziest sufficient* strategy
   (regex → filesys_delta → jsonl → darshan).
3. Sanity-check outside evolution: `simulate: true` run, then one real
   sbatch with the seed candidate.
4. Confirm the seed's measured bytes match the app's own accounting
   (±startup overhead for filesys_delta) — measure before believing.
5. Pin the workload size so one rep fits well inside `cluster.time_limit`.

Existing exemplars: `epoch_io` (real app, custom measure over SDF files),
`pio_bench` (instrument, jsonl), and anything fitting the generic trio
needs no repo code at all.
