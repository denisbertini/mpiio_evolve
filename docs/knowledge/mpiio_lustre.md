# MPI-IO / Lustre tuning handbook (site-verified)

Curated domain knowledge injected into the mutator prompt. Edit like a lab
notebook: every claim carries its status. `[verified]` = measured on THIS
cluster; `[textbook]` = standard wisdom, plausible here but not re-measured;
generic AI/web text is only admitted after correction below.

## 1 · Aggregators (collective buffering)

- `romio_cb_write` / `romio_cb_read` = `enable | disable | automatic` toggle
  collective buffering. Factory state on this stack: `automatic`, which with
  the default `cb_config_list *:1` behaves as essentially OFF. `[verified]`
- `cb_nodes` = total number of aggregator nodes. THE primary lever. The
  optimum sits near the number of OSTs the dump file spans; too few
  bottleneck the collective stream, too many add RPC overhead and collide
  writers per OST. `[textbook, direction verified here]`
- `cb_config_list` = pin WHICH hosts aggregate. NOT in the search space —
  proposing it wastes an iteration (rejected pre-submission). `[site rule]`
- Turning `romio_cb_write=enable` was measured +6-8% over baseline
  (cb_nodes 8-16 family). `[verified 2026-10]`

## 2 · Internal staging buffers

- `cb_buffer_size` = per-aggregator staging buffer before flush. ROMIO's
  default here is **4 MiB** (`ROMIO_CB_BUFFER_SIZE_DFLT`), NOT the "16-32 MB"
  often quoted by generic sources. `[verified in source; generic claim
  corrected]`
- Aligning it with the Lustre stripe size avoids partial-stripe writes:
  buffer >= stripe_size per aggregator is the heuristic. `[textbook]`

## 3 · Lustre-specific ROMIO hints — deliberately EXCLUDED

- `striping_factor` / `striping_unit` (note the exact names — generic text
  says "striping_count", which does not exist) can set layout from the app
  layer *when ROMIO's runtime Lustre detection engages*. `[textbook]`
- They are NOT in the search space on purpose: striping here is applied via
  `lfs setstripe` on the dump directory — a single actuator. Two independent
  paths setting one layout would make every measurement unattributable.
  Proposing these keys = instant rejection. `[site rule]`
- Open question (functional test pending): this libmpi.so carries ROMIO's
  AD-IO-Lustre backend (`romio_lustre_start_iodevice` + `liblustreapi.so.1`)
  `[verified 2026-10-08]`, but an earlier runtime probe reported
  `filesystem_type=UFS`. Until a compute-node striping-hint round-trip
  settles it, treat automatic aggregator alignment as NOT guaranteed:
  explicit `cb_nodes` is the reliable lever. `[site, unresolved]`

## 4 · The coupling rule (search the diagonal)

- `stripe_count` sets how many OSTs a file fans across; `cb_nodes` sets how
  many aggregator processes write it. `[mechanism]`
- `cb_nodes ≈ stripe_count` → one aggregator per OST, full-width streams.
  `cb_nodes << stripe_count` → each aggregator scatters across OSTs
  (fragmented partial writes). `cb_nodes >> stripe_count` → aggregators
  collide per OST (RPC pile-up, RMW amplification). `[textbook]`
- Matched / mismatched / automatic are three DIFFERENT hypotheses — test
  them deliberately, do not treat the difference as noise. `[site rule]`
- `lustre.stripe_count/stripe_size` are REALLY APPLIED per evaluation via
  `lfs setstripe` since 2026-10-08; campaigns before that silently skipped
  them (their stripe reasoning was decorative). `[verified]`

## 5 · Poison combos and hard site facts

- NEVER `romio_cb_write=enable` + `romio_ds_write=disable`: aggregator
  read-modify-write storms, hours-vs-minutes catastrophe. Search space
  enforces it. `[verified, catastrophic]`
- `romio_ds_write=disable` alone: sieving scratch dance avoided; only
  reachable as `automatic`/`enable` here. `[site rule]`
- Hint file is delivered via `ROMIO_HINTS` (the only env hint-file reader in
  this romio341 build; `MPIIO_HINTS` does not exist there). `[verified]`
- Shared Lustre: contention moves throughput 20-30% and can time out
  healthy configs; differences below the reported SEM are noise. Wall-clock
  timeouts may be weather, not the candidate. `[verified]`
- Engine: `romio` (romio341 embedded) is active; `ompio` switches the whole
  surface to `OMPI_MCA_io_ompio_*` (then `fs:lustre` MCA knobs apply —
  different physics, currently outside the search space). `[verified]`
