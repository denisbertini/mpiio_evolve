"""
slurm_launcher.py -- Dynamic Script Compiler
============================================

Assembles a cluster-native sbatch shell script on the fly for each candidate:

    #!/bin/bash
    #SBATCH ...resource headers...
    #SBATCH -o <run>/stdout.log -e <run>/stderr.log

    # ---- hard $HOME isolation header (NO real home dir exists) ----
    export HOME=/lustre/rz/dbertini2/<state>/.fake_home
    export HF_HOME=... .triton ... XDG_* ... TMPDIR ...
    # ---- engine settings (MPIIO_HINTS file or OMPI_MCA_* array) ----

    srun -n <ntasks> <benchmark command>
    exit $?

Submission uses ``subprocess.run(["sbatch", "--wait", script])`` so the
controller blocks until the run finishes and inherits the job's exit code.
When Slurm is unavailable (development laptop / head-node tests) the launcher
degrades to a *dry-run* backend that renders the script for inspection and
synthesizes a plausible benchmark log so the whole pipeline stays testable.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from infrastructure import ensure_inside, render_env_exports

logger = logging.getLogger("mpiio_evolve.slurm")

SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SlurmResources:
    """Everything that becomes an ``#SBATCH`` header line."""

    job_name: str = "mpiio_evolve"
    partition: str = "compute"
    account: Optional[str] = None
    qos: Optional[str] = None
    nodes: int = 1
    tasks_per_node: int = 32
    cpus_per_task: int = 1
    mem_per_cpu: Optional[str] = None
    time_limit: str = "00:10:00"
    constraint: Optional[str] = None
    extra_sbatch: list = field(default_factory=list)   # raw "--foo=bar" lines

    @classmethod
    def from_config(cls, cluster: Mapping, job_name: str) -> "SlurmResources":
        return cls(
            job_name=job_name[:64],
            partition=str(cluster.get("partition", "compute")),
            account=cluster.get("account"),
            qos=cluster.get("qos"),
            nodes=int(cluster.get("nodes", 1)),
            tasks_per_node=int(cluster.get("tasks_per_node", 1)),
            cpus_per_task=int(cluster.get("cpus_per_task", 1)),
            mem_per_cpu=cluster.get("mem_per_cpu"),
            time_limit=str(cluster.get("time_limit", "00:10:00")),
            constraint=cluster.get("constraint"),
            extra_sbatch=list(cluster.get("extra_sbatch") or []),
        )

    def header_lines(self, stdout: Path, stderr: Path) -> list:
        lines = [
            f"#SBATCH --job-name={self.job_name}",
            f"#SBATCH --partition={self.partition}",
            f"#SBATCH --nodes={self.nodes}",
            f"#SBATCH --ntasks-per-node={self.tasks_per_node}",
            f"#SBATCH --cpus-per-task={self.cpus_per_task}",
            f"#SBATCH --time={self.time_limit}",
            f"#SBATCH --output={stdout}",
            f"#SBATCH --error={stderr}",
        ]
        if self.account:
            lines.append(f"#SBATCH --account={self.account}")
        if self.qos:
            lines.append(f"#SBATCH --qos={self.qos}")
        if self.mem_per_cpu:
            lines.append(f"#SBATCH --mem-per-cpu={self.mem_per_cpu}")
        if self.constraint:
            lines.append(f"#SBATCH --constraint={self.constraint}")
        for extra in self.extra_sbatch:
            extra = extra if extra.startswith("--") else f"--{extra}"
            lines.append(f"#SBATCH {extra}")
        return lines


@dataclass
class SlurmResult:
    """Outcome of one blocking submission."""

    job_id: Optional[int]
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    simulated: bool = False
    raw_submit_output: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class SlurmLauncher:
    """Builds and submits per-candidate batch scripts inside *workspace*."""

    SCRIPT_NAME = "submit.sh"
    STDOUT_NAME = "stdout.log"
    STDERR_NAME = "stderr.log"

    def __init__(self, workspace_root: Path, dry_run: Optional[bool] = None) -> None:
        self.workspace = Path(workspace_root).resolve()
        if dry_run is None:
            dry_run = shutil.which("sbatch") is None
            if dry_run:
                logger.warning("sbatch not found -- using dry-run backend")
        self.dry_run = dry_run

    # ---- script compilation ------------------------------------------------

    def build_script(
        self,
        run_dir: Path,
        resources: SlurmResources,
        command: str,
        env: Mapping[str, str],
        ntasks: int,
        extra_srun_args: str = "",
    ) -> Path:
        """Render the complete sbatch script into *run_dir* and return its path.

        *env* is injected as an export block in the script HEADER, before any
        python/tooling can run, guaranteeing nothing writes to a missing $HOME.
        """
        run_dir = ensure_inside(self.workspace, run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / self.STDOUT_NAME
        stderr_path = run_dir / self.STDERR_NAME

        parts = ["#!/bin/bash", "# Generated by mpiio_evolve -- edit the candidate, not me."]
        parts += resources.header_lines(stdout_path, stderr_path)
        parts += [
            "",
            "# ---- hard $HOME isolation: caches redirected into the parallel fs ----",
            render_env_exports(env),
            "",
            "set -u",
            'echo "mpiio_evolve: job started on $(hostname) at $(date -Is)"',
            'echo "mpiio_evolve: MPIIO_HINTS=${MPIIO_HINTS:-<unset>} '
            'OMPI_MCA_io_ompio_num_aggregators=${OMPI_MCA_io_ompio_num_aggregators:-<unset>}"',
            "",
            f"srun {extra_srun_args} -n {ntasks} {command}".replace("  ", " "),
            "exit $?",
            "",
        ]

        script_path = run_dir / self.SCRIPT_NAME
        script_path.write_text("\n".join(parts), encoding="utf-8")
        script_path.chmod(0o750)
        logger.info("compiled submission script -> %s", script_path)
        return script_path

    # ---- submission ----------------------------------------------------------

    def submit(self, script_path: Path, wait: bool = True,
               timeout: int = 3600) -> SlurmResult:
        """``sbatch --wait`` the script; returns exit code + log paths.

        sbatch --wait propagates the *job's* exit status, which we surface as
        SlurmResult.exit_code for the fitness/crash classifier downstream.
        """
        script_path = ensure_inside(self.workspace, script_path)
        run_dir = script_path.parent
        stdout_path = run_dir / self.STDOUT_NAME
        stderr_path = run_dir / self.STDERR_NAME

        if self.dry_run:
            return self._simulate(run_dir, script_path)

        argv = ["sbatch", "--parsable"] + (["--wait"] if wait else []) + [str(script_path)]
        logger.info("submitting: %s", " ".join(argv))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.error("sbatch --wait exceeded %ds wall clock", timeout)
            return SlurmResult(None, 124, stdout_path, stderr_path,
                               raw_submit_output="(launcher timeout)")

        m = SUBMITTED_RE.search(proc.stdout)
        job_id = int(m.group(1)) if m else None
        if job_id is None:  # older slurm without --parsable, plain text form
            m = re.search(r"Submitted batch job (\d+)", proc.stdout)
            job_id = int(m.group(1)) if m else None

        if proc.returncode != 0 and job_id is None:
            logger.error("sbatch submission failed: %s", proc.stderr.strip())

        return SlurmResult(
            job_id=job_id,
            exit_code=proc.returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            raw_submit_output=(proc.stdout + proc.stderr).strip(),
        )

    # ---- dry-run backend -----------------------------------------------------

    def _simulate(self, run_dir: Path, script_path: Path) -> SlurmResult:
        """Render-only mode: echo the script to stdout.log so it is inspectable."""
        stdout_path = run_dir / self.STDOUT_NAME
        stderr_path = run_dir / self.STDERR_NAME
        stdout_path.write_text(
            "[dry-run] sbatch unavailable; compiled script was:\n\n"
            + script_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        if not stderr_path.exists():
            stderr_path.write_text("", encoding="utf-8")
        return SlurmResult(job_id=None, exit_code=0, stdout_path=stdout_path,
                           stderr_path=stderr_path, simulated=True,
                           raw_submit_output="[dry-run] not submitted")
