# Proposal: guardrails against long-running CPU-bound processes on login nodes

## Summary

A login node on the GPU cluster currently hosts an interactive shell consuming **one full CPU core continuously for weeks**. Nobody is at fault — runaway loops happen — but the incident exposes a structural gap: **login nodes have no automatic protection against sustained CPU (or memory) abuse**, and our users' workloads have changed: login nodes are no longer only places to edit files and type `sbatch`; they now host long-running automation and service endpoints that other users depend on. I propose a small set of detection + enforcement measures, with a grace ladder so legitimate work is never harmed.

## Observed situation (evidence)

```
USER      PID    %CPU  TIME+       COMMAND
klinger  502045  99.7  35499:49    bash
```

- `TIME+` 35,499 min ≈ **592 h ≈ 24.7 CPU-days** of a *single* process, at ~100 % of one core — i.e. running flat out since roughly three and a half weeks, unnoticed and unmitigated.
- The process is a plain `bash` — most likely an infinite loop in a script or a `PROMPT_COMMAND`/watch-style loop, not deliberate misuse.
- Reproduce/verify with:
  ```bash
  ps -o pid,user,%cpu,etime,lstart,cmd -p <PID>
  cat   /proc/<PID>/cmdline ; ls -l /proc/<PID>/cwd
  ps -eo user,pid,pcpu,etime,comm --sort=-pcpu | head   # the full picture
  ```

## Why this matters more than "one lost core"

1. **Login nodes are shared control-plane services.** Every user's `sbatch`/`srun`, module loads, file operations, and monitoring sessions traverse them. Sustained saturation adds latency to everyone and turns interactive use into a slot machine.
2. **The usage pattern has changed.** Automated, long-lived workflows now run on login nodes — e.g. our evolutionary I/O-optimization controller (hours–days, submitting Slurm jobs continuously), and an **LLM inference proxy served from this very login node** that several automation pipelines call. A hung or sluggish login node silently breaks campaigns that nobody is watching interactively. The classic assumption — "nobody runs real work on a login node" — no longer describes reality; the platform should make that assumption *enforceable*, not nostalgic.
3. **It scales badly.** One tolerated 100 % process teaches everyone that login nodes tolerate compute. The same gap admits ten such processes, fork bombs, memory hogs, or (worst case for security teams) genuinely hostile long-running processes. Unattended processes with multi-week CPU time are also exactly the forensic signature your security monitoring would want flagged.
4. **The cost asymmetry is perverse.** Detection and mitigation cost a sysadmin minutes *if tooling exists* — and nothing exists today: this went on ~25 days because intervention requires a human to notice and a root login to `kill`.

## Proposed mitigations (layered, lowest-effort first)

### A. Immediate (minutes)
- Terminate the offending process (root): `kill 502045`; notify the owner with the evidence lines above.
- Add the one-liner watchdog to the daily ops routine until automation exists:
  ```bash
  ps -eo user,pid,pcpu,etimes,comm --sort=-pcpu | awk '$3>80 && $4>7200 && $1!~/^(root|slurm|_?nscd|rpc)/'
  ```

### B. Grace-ladder watchdog (recommended core piece, ~1 hour of work)
A cron/systemd-timer script on login nodes with an escalating, **reversible** ladder:

1. > 30 min sustained > 80 % CPU → warn the process owner by e-mail (PID, cmd, elapsed).
2. Still running after another 30 min → **`SIGSTOP` the process** + e-mail owner *and* admins, including the exact command to resume it if a legitimate use case exists: `kill -CONT <PID>`.
3. Owner unresponsive / repeat offender → admins `kill` and, if needed, discuss the case individually.

`SIGSTOP` is the key idea: mitigation without destruction — the user's session state survives, a false positive is one command away from undone, and the offending core is recovered instantly.

### C. Hard, automatic limits via cgroup v2 user slices (the real fix — detailed design)

#### C.1 Mechanism

On any systemd + cgroup v2 system (Rocky/Alma 9 default), every SSH login is already
placed by `pam_systemd` into a per-user slice:

```
user.slice
└── user-<UID>.slice          ← ALL limits belong here (per USER, all sessions aggregated)
    ├── session-17.scope      ← one ssh session (+ its tmux server, scripts, children…)
    ├── session-23.scope      ← another session
    └── user@<UID>.service    ← the user's private systemd instance
```

Setting resource controls on `user-.slice` therefore caps **every interactive process
of every user automatically**, with no per-session bookkeeping. Throttling (CPU/IO) is
graceful; memory is contained with the blast radius restricted to the offending user.

#### C.2 Prerequisites (one-time check on the login nodes)

```bash
stat -fc %T /sys/fs/cgroup              # must print: cgroup2fs  (v2 unified)
cat /sys/fs/cgroup/cgroup.controllers   # must contain: cpu memory pids io
ssh <login> 'ps -o cgroup= -p $$'       # expect /user.slice/user-<uid>.slice/session-N.scope
                                        # (if not → pam_systemd missing: fix PAM first)
```

#### C.3 The drop-in (the whole enforcement, one file)

```ini
# /etc/systemd/system/user-.slice.d/90-login-guardrails.conf
# DEPLOY ON LOGIN NODES ONLY (config-management group 'login'), never on
# compute nodes: there Slurm's cgroup plugin owns the hierarchy.
[Slice]
CPUQuota=400%       # hard cap, per user, aggregated over ALL their processes
MemoryHigh=16G      # > 16G: kernel throttles + reclaims aggressively (warning lane)
MemoryMax=24G       # hard wall: OOM-kill confined to this user's slice, node safe
TasksMax=2048       # pids.max: fork-bomb containment
IOWeight=50         # under contention, interactive sessions lose to system daemons
```

Activation:

```bash
systemctl daemon-reload                      # applies to all NEW sessions
# existing sessions, without waiting for re-login:
systemctl set-property user-$(id -u <runaway>).slice CPUQuota=400% MemoryMax=24G
```

#### C.4 What each control does — and why the value

| Control | Behavior when exceeded | Rationale |
|---|---|---|
| `CPUQuota=400%` | **Throttled**, never killed: CPU bandwidth is a hard ceiling (CFS quota); invisible until abused. 4 cores/user is generous for editors, `sbatch`, monitoring, light scripting. | The incident class here is *rate* abuse (one core for 25 days). Quotas defeat it exactly, and cannot hurt normal interactive use. |
| `MemoryHigh=16G` | Immediate writeback/reclaim pressure + throttling → strong backpressure before the wall. | Grace lane: mis-sized jobs self-notice. |
| `MemoryMax=24G` | OOM killer fires **inside the user's slice** — worst case loses that user's own processes, never sshd/sssd or the node. | Memory (not CPU) is what actually kills login nodes; this makes it non-lethal. |
| `TasksMax=2048` | New forks get `EAGAIN` for that user. | Fork bombs become a private, survivable incident. |
| `IOWeight=50` | I/O scheduler de-prioritizes the slice under contention. | A `find /lustre` storm can't starve system daemons. |

#### C.5 Exemptions and sanctioned exceptions (keep it politicaly safe)

```ini
# /etc/systemd/system/user-0.slice.d/override.conf      (root; pattern repeats
# for any service account, e.g. user-900.slice.d/, like an LLM-proxy account)
[Slice]
CPUQuota=infinity
MemoryHigh=infinity
MemoryMax=infinity
IOWeight=100
```

Temporary, auditable exceptions on request — runtime-only, dies at reboot, one command
to grant, one to revoke:

```bash
systemctl set-property --runtime user-1042.slice CPUQuota=800%
```

#### C.6 Interaction with the existing environment (why nothing else breaks)

- **Slurm**: untouched. `slurmctld`/`slurmdbd`/`sssd`/`sshd` run in `system.slice`,
  outside the user slices. Slurm's own cgroup management exists only on compute
  nodes — hence the deploy-group restriction.
- **tmux/screen**: the server inherits cgroup membership at spawn and *keeps it*
  after the ssh session detaches — a runaway inside tmux stays capped. Good property,
  not a bug.
- **No unprivileged escape**: moving a process between cgroups requires write access
  to the cgroup filesystem (root-only). `systemd-run --user` also lands inside the
  user's slice. cron/at jobs too.
- **Login scripts, prompts, module spam** (a classic cause of our incident): capped
  like everything else; the user still gets a working, merely slowed shell.
- **Kernel/systemd versions**: Rocky 9 ships unified v2 + systemd ≥ 252; all knobs
  above are supported. Prerequisite check C.2 guards older nodes.

#### C.7 Rollout plan (low drama)

1. **Phase 0 — observe (1 week):** collect per-user slice usage
   (`node_exporter --collector.cgroups` or cgroup-exporter; per-user CPU/mem/task
   panels). Validate that 400 %/24 G are far above legitimate peaks.
2. **Phase 1 — memory first, one node:** `MemoryHigh/Max` only. Memory is the
   node-killer; CPU can wait a week. Watch `memory.events` (`high`, `max`, `oom`).
3. **Phase 2 — CPU generous, then tighten:** enable with `CPUQuota=800%`; watch
   `cpu.stat → nr_throttled` per user; if no legitimate user is throttled after
   ~2 weeks, tighten to 400 %.
4. **Communicate with the change** (MOTD + docs, see policy section E): "login nodes
   are capped at N cores/user; compute belongs on Slurm; here is the debug partition
   for anything long-running."

Ansible sketch for config management:

```yaml
- name: Login-node user-slice guardrails
  copy:
    dest: /etc/systemd/system/user-.slice.d/90-login-guardrails.conf
    owner: root
    mode: "0644"
    content: |
      [Slice]
      CPUQuota=400%
      MemoryHigh=16G
      MemoryMax=24G
      TasksMax=2048
      IOWeight=50
  notify: systemd daemon-reload
  when: "'login_nodes' in group_names"
```

#### C.8 Verification & monitoring

```bash
# cap active?
systemctl show user-1042.slice -p CPUQuotaPerSecUSec,MemoryHigh,MemoryMax,TasksMax

# who is being throttled (→ Grafana; the honest "policy is working" metric):
cat /sys/fs/cgroup/user.slice/user-1042.slice/cpu.stat        # nr_throttled ↑
cat /sys/fs/cgroup/user.slice/user-1042.slice/memory.events   # high/max/oom

# functional test (test user on a test node): should pin at ~400 %, not 1200 %
systemd-run --user bash -c 'for i in $(seq 12); do : & done; wait'
```

Prometheus alert (with the cgroup collector exported):

```yaml
- alert: LoginUserSustainedThrottle
  expr: increase(node_cgroup_cpu_stat_nr_throttled{job=~".*login.*"}[15m]) > 50
  for: 30m
  labels: {severity: warning}
  annotations:
    summary: "User slice {{ $labels.cgroup_path }} is CPU-throttled — sustained abuse or mis-sized cap"
```

#### C.9 Failure modes & rollback

| Concern | Answer |
|---|---|
| Cap set too low, legitimate pain | `systemctl set-property user-<uid>.slice CPUQuota=infinity` — live, no logout. |
| Need to undo the whole policy | Delete the drop-in + `systemctl daemon-reload`. Stateless, instant. |
| Node without v2 unified | Prerequisite C.2 fails → excluded from the deploy group. |
| Admin needs heavy work on a login node | Root is exempt (C.5), or better: `systemd-run --scope <cmd>` runs in `system.slice`. |

#### C.10 Why this beats the alternatives

| Alternative | Fatal flaw |
|---|---|
| `RLIMIT_CPU` via `limits.conf` | Kills instead of throttles; counts children (collateral); no memory/pids/IO; no aggregation. |
| `cpulimit`/`cpulv` polling daemons | Race-prone, per-process (escapable by re-exec), unmaintained, no accounting. |
| `nice`/`renice` watchdog | Only reorders the queue; an idle cluster still loses the core forever. |
| Manual `kill` when someone complains | **Status quo: 25 days undetected.** |

`cgroup v2` user slices are declarative (one file in config management), automatic
(no daemon to babysit), aggregate **per user** (not per process — the natural unit of
fairness), cover CPU **and** memory **and** pids **and** IO, throttle rather than
kill, and are already the substrate your distro, Slurm, and containers are built on.

### D. Alerting
If node_exporter/process-exporter is deployed: alert on `sum by (user) (rate(process_cpu_seconds_total{hostname=~"login.*"}[10m])) > 1` sustained 2 h, routed to the ops channel. The goal is that the *system notices in hours what humans notice in weeks*.

### E. Policy clarity (zero-cost, high-leverage)
- State explicitly in the user documentation + login MOTD: login nodes are for interactive work and job submission only; anything that runs > ~10 minutes of CPU belongs on Slurm — and **say where automation belongs**. A tiny `service`/`debug` partition or one dedicated service node for long-lived user automation (controllers, proxies, dashboards) removes the incentive to park daemons on login nodes.
  - On the CPU cluster this already exists (`debug` partition, 30 min) — the GPU cluster lacks an equivalent sanctioned slot, which is precisely why services end up on its login nodes.

## Recommended minimum package

If only one thing is adopted, take **C (user-slice caps)** — it is declarative, automatic, and fair. The realistic package is **B + C + E**: the watchdog handles the current world, cgroup caps make the next incident impossible, and the policy text plus a sanctioned place for automation remove the root cause.

Happy to help concretely: I can prototype and test the watchdog script and the slice drop-in on a non-critical node, and I know the automation-side perspective well (our own controller is exactly the kind of long-lived workload that motivated this issue — we will move it off login nodes given a suitable partition).

## Appendix: incident-class facts for reference

- Offending process class: interactive shell running a compute loop (`bash`, 99.7 % of one core, ~24.7 CPU-days).
- Detection latency without tooling: **~25 days**. Target detection latency with B/C/D: **< 1 hour, automatic**.
- Mitigation risk with SIGSTOP ladder: reversible (`kill -CONT`); with CPUQuota: self-limited per user, no cross-user impact.
