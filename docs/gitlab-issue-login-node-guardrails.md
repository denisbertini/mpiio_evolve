# Login-node guardrails against sustained CPU abuse — evidence, design, and
# a phased proposal (follow-up to the cluster meeting of 2026-10-02)

/label ~"platform: login nodes" ~"policy" ~performance ~monitoring

## 0. Origin

Per **[sysadmin]'s request at the cluster meeting on 2026-10-02**, I am
documenting the runaway-process incident discussed there, together with a
technical mitigation proposal. This issue consolidates:

1. the evidence,
2. the concerns raised during the meeting (restated as faithfully as I can, §3),
3. a concrete, layered design answering those concerns (§4–§7),
4. a phased rollout with a pilot offer, so the decision can be small (§8).

Nothing here attributes fault to any user; the subject is the platform gap
that let a routine bug run unnoticed for weeks.

## 1. What happened (evidence)

```
USER      PID    %CPU  TIME+       COMMAND
userA   502045   99.7  35499:49    bash
```

- `TIME+` ≈ 35,500 min ≈ **592 h ≈ 24.7 CPU-days** of a single `bash`, at
  ~100 % of one core, for roughly three and a half weeks.
- Almost certainly a runaway loop in a script or prompt hook — a routine bug,
  which is exactly why the defense should be automated rather than social.
- Detection latency: **weeks**, via user complaint. No alert exists that would
  have fired.
- Observation one-liner for the incident class:
  `ps -eo user,pid,pcpu,etimes,comm --sort=-pcpu | awk '$3>80 && $4>7200'`

## 2. Why it matters now (as discussed in the meeting)

1. **Login nodes are control-plane infrastructure** — everyone's `sbatch`,
   module and file traffic traverses them.
2. **Usage has changed**: multi-day automation now runs there (job-submitting
   controllers, and an LLM inference proxy served from a GPU-cluster login
   node that pipelines call). A sluggish login node silently breaks unattended
   work.
3. **It scales badly**: one tolerated 100 %-forever process defines the norm;
   the same gap admits N of them, fork bombs, memory hogs — or the classic
   security signature of a hostile process.
4. **Cost asymmetry**: prevention costs a file + a reload today; the current
   design costs 25 CPU-days per incident, silently.

## 3. Concerns raised in the meeting — and my responses

**Concern 1 (raised by [sysadmin]): a busy login node is normal.**
Agreed for transient load — nothing proposed here touches that. What the
evidence shows is not load but an *incident no system reported for 25 days*.
The proposal therefore targets **detection latency (→ hours) and containment
(→ automatic, per user)**, not normal usage.

**Concern 2: such usage cannot effectively be prevented on login nodes.**
Respectfully, this one is testable, and the mechanism is already present and
enabled in the OS these nodes run: Rocky 9 is cgroup v2 unified by default,
`pam_systemd` already places every SSH session into a per-user slice
(`ps -o cgroup= -p <PID>` → `/user.slice/user-<uid>.slice/…`), and systemd
resource control caps exactly that object (`systemd.resource-control(5)`).
The same kernel facility governs our compute nodes via Slurm's cgroup plugin
and underlies every container we run. On login nodes it is simply unused.
Key safety property: **CPUQuota throttles — it never kills** — so the classic
"punishing the innocent" failure mode does not exist in this design; users
only notice a ceiling (4 cores/user aggregated) that no legitimate interactive
work approaches.

**Concern 3: understanding how to prevent this is a long-term undertaking.**
The technical change is one drop-in file + `systemctl daemon-reload`; running
sessions can be capped live with one `systemctl set-property`; rollback is
deleting the file. The genuinely non-trivial part is change management —
which is why §6 proposes a one-week observe-only phase, memory-first rollout
on a single node, a generous initial CPU cap with tightening only after
observed data, an exemption mechanism for root/service accounts, and a
`--runtime` escape hatch for legitimate temporary needs. I read the meeting
discussion as: nobody wants to own a sudden policy change — the phased plan
is designed so nobody has to.

## 4. Proposal layers (C is the core; all are reversible)

- [ ] **A. Immediate:** cap the current offender live —
      `systemctl set-property user-<uid>.slice CPUQuota=400%` — and add the
      ps-check (§1) to daily ops until tooling exists.
- [ ] **B. Grace-ladder watchdog (optional):** >30 min sustained >80 % CPU →
      warn owner; +30 min → **SIGSTOP** (resume = `kill -CONT <PID>`, fully
      reversible) + notify; then escalate. Reversibility is the point.
- [ ] **C. cgroup v2 user-slice caps (recommended core):** design in §5.
- [ ] **D. Alerting:** per-user cgroup-throttle Prometheus rule + the §1
      predicate routed to ops. Target detection latency: < 1 h.
- [ ] **E. Policy + a sanctioned home for automation:** login nodes =
      interactive + submission; long-lived services get a `debug`/`service`
      slot on the GPU cluster (the CPU cluster already has `debug`).
      Announce with the change (MOTD + docs).

## 5. Design: cgroup v2 user-slice guardrails

### 5.1 Mechanism
```
user.slice
└── user-<UID>.slice            ← limits live HERE (per user, all sessions)
    ├── session-17.scope        ← ssh session + tmux server + everything spawned
    ├── session-23.scope
    └── user@<UID>.service
```

### 5.2 Prerequisites (one-time, per login node)
```bash
stat -fc %T /sys/fs/cgroup              # expect: cgroup2fs
cat /sys/fs/cgroup/cgroup.controllers   # expect cpu memory pids io
ssh <login> 'ps -o cgroup= -p $$'       # expect /user.slice/user-<uid>.slice/session-N.scope
```

### 5.3 The enforcement (one file)
```ini
# /etc/systemd/system/user-.slice.d/90-login-guardrails.conf
# LOGIN NODES ONLY — compute nodes belong to Slurm's cgroup controller.
[Slice]
CPUQuota=400%       # per-user CPU ceiling: throttled, never killed
MemoryHigh=16G      # warning lane: reclaim pressure before any kill
MemoryMax=24G       # wall: OOM confined to the offender's own slice
TasksMax=2048       # fork-bomb containment
IOWeight=50         # sessions yield I/O to system daemons under contention
```
Activation: `systemctl daemon-reload` (new sessions); live capping of existing
sessions: `systemctl set-property user-<uid>.slice CPUQuota=400% MemoryMax=24G`.

### 5.4 Exemptions & sanctioned exceptions
```ini
# /etc/systemd/system/user-0.slice.d/override.conf   (root; repeat per service acct)
[Slice]
CPUQuota=infinity
MemoryHigh=infinity
MemoryMax=infinity
```
Temporary, auditable exception that dies at reboot:
`systemctl set-property --runtime user-1042.slice CPUQuota=800%`

### 5.5 Non-interference
- Slurm daemons run in `system.slice` → untouched; Slurm's own cgroups exist
  only on compute nodes → deploy restricted to the login-node group.
- tmux/screen servers inherit the slice and **remain capped after detach**.
- Escaping requires root (cgroup-fs writes); `systemd-run --user`, cron, at
  all land inside the slice.
- Rocky 9: cgroup v2 + systemd ≥ 252 support all knobs (5.2 guards outliers).

### 5.6 Verification & monitoring
```bash
systemctl show user-1042.slice -p CPUQuotaPerSecUSec,MemoryHigh,MemoryMax,TasksMax
cat /sys/fs/cgroup/user.slice/user-1042.slice/cpu.stat        # nr_throttled
cat /sys/fs/cgroup/user.slice/user-1042.slice/memory.events   # high/max/oom
# functional test (test node/user): should pin ~400 %, not 1200 %
systemd-run --user bash -c 'for i in $(seq 12); do : & done; wait'
```
```yaml
- alert: LoginUserSustainedThrottle
  expr: increase(node_cgroup_cpu_stat_nr_throttled{job=~".*login.*"}[15m]) > 50
  for: 30m
  labels: {severity: warning}
```

## 6. Phased rollout (designed so each step is small)

- [ ] Phase 0 (1 wk): export per-user slice CPU/mem/pids (node_exporter cgroup
      collector); confirm proposed caps are far above legitimate peaks.
- [ ] Phase 1: memory-only caps (`MemoryHigh/Max`) on ONE login node; watch
      `memory.events`.
- [ ] Phase 2: `CPUQuota=800%` on all login nodes; watch `cpu.stat
      nr_throttled` for ~2 weeks; tighten to 400 % only if data shows no
      legitimate throttling.
- [ ] Communication: MOTD + user docs + answer to "where does my long-running
      job live?" (item E).
- [ ] Config management (Ansible sketch):
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

## 7. Safety & rollback

| Situation | Action |
|---|---|
| Legitimate user throttled | `set-property user-<uid>.slice CPUQuota=infinity` — live, no logout |
| Revoke policy entirely | delete drop-in + `daemon-reload` (stateless) |
| Node without v2 unified | §5.2 check fails → excluded from deploy group |
| Admin heavy task on a login node | root exempt; or `systemd-run --scope` |

## 8. Requested next steps (following the meeting)

1. **Now (minutes):** cap the current offender via `set-property` (§4A).
2. **Pilot (offered):** I will test the §5.3 drop-in + §5.6 verification on a
   non-critical node or test allocation and report results here.
3. **Decision item for the next cluster meeting:** approve Phase 0 + Phase 1,
   or propose modifications. Data from the pilot and observation week should
   settle any remaining disagreement about thresholds and false positives.

I'm glad to present the pilot results at the next meeting. Thank you for
asking that this be tracked properly.
