"""
infrastructure.py -- Storage Hardware Modeler
=============================================

Translates an abstract I/O configuration candidate into concrete artifacts:

1. Lustre layout      -> ``lfs setstripe -c {count} -S {size} {dir}``
2. ROMIO hints        -> plaintext hint file exported via ``MPIIO_HINTS``
                         (MPICH / Intel MPI / Cray MPICH)
3. OMPIO MCA vars     -> environment dict prefixed with ``OMPI_MCA_``
                         (Open MPI 4.x io_ompio component)
4. Home isolation     -> a synthetic $HOME + cache tree that lives entirely
                         inside the parallel-filesystem workspace, because the
                         deployment host has no writable user home directory.

Standard library only (os, shutil, subprocess, re, pathlib).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("mpiio_evolve.infrastructure")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InfrastructureError(RuntimeError):
    """Base class for storage-modeling failures."""


class PathIsolationError(InfrastructureError):
    """Raised when an operation would escape the scratch workspace."""


class LustreError(InfrastructureError):
    """Raised when ``lfs setstripe`` fails (bad layout, MDT refused, ...)."""


# ---------------------------------------------------------------------------
# Hard path isolation helpers
# ---------------------------------------------------------------------------


def ensure_inside(base: Path, target: Path) -> Path:
    """Return *target* resolved, asserting it lies inside *base*.

    Every filesystem mutation in this project funnels through here so that a
    buggy/mutated candidate can never delete or write outside the workspace.
    Symlinks are resolved on both sides before the prefix test.
    """
    base_r = Path(base).resolve()
    target_r = Path(target).resolve()
    if base_r != target_r and base_r not in target_r.parents:
        raise PathIsolationError(
            f"path isolation violation: {target_r} is outside workspace {base_r}"
        )
    return target_r


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(i?B)?\s*$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}


def parse_size(text: Any) -> Optional[int]:
    """Parse '4M', '131072', '8 MiB' ... into bytes. ``None`` passes through."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = _SIZE_RE.match(str(text))
    if not m:
        raise ValueError(f"unparseable size: {text!r}")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])


# ---------------------------------------------------------------------------
# Candidate data model
# ---------------------------------------------------------------------------

_UNIT_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _size_key(value: Any):
    """Canonical comparison key for size-like strings.

    '4M', '4 MiB', '4MB' and '4194304' all map to the same byte count, so a
    candidate that spells a declared value differently (LLMs love raw byte
    strings) is matched SEMANTICALLY instead of being rejected on literal
    string inequality. Non-size values compare as stripped strings, as before.
    """
    s = str(value).strip().replace("_", "")
    t = s.upper().rstrip("B").rstrip("I")          # 4MIB -> 4M, 4MB -> 4M
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)", t)
    if m:
        return _UNIT_MULT[m.group(2)] * int(m.group(1))
    return s


@dataclass
class LustreStripeSpec:
    """Target Lustre layout for the run's data directory."""

    stripe_count: int = 0          # -1 broadcast, 0 fs default
    stripe_size: str = "1M"        # passed verbatim to `lfs -S`

    def validate(self, space: Mapping[str, Any]) -> None:
        counts = space.get("stripe_count", [])
        sizes = space.get("stripe_size", [])
        if counts and self.stripe_count not in counts:
            raise ValueError(
                f"stripe_count={self.stripe_count} outside search space {counts}"
            )
        if sizes and not any(_size_key(self.stripe_size) == _size_key(s)
                             for s in sizes):
            raise ValueError(
                f"stripe_size={self.stripe_size!r} outside search space {sizes}"
            )


@dataclass
class IoConfig:
    """A fully materialised, validated candidate configuration."""

    engine: str                                    # "romio" | "ompio"
    stripe: LustreStripeSpec
    romio_hints: dict = field(default_factory=dict)
    ompio_mca: dict = field(default_factory=dict)
    extra_env: dict = field(default_factory=dict)

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, Any], engine: str) -> "IoConfig":
        """Build an IoConfig from a raw (LLM-mutated) candidate mapping.

        Accepted candidate shapes::

            {"lustre": {"stripe_count": 8, "stripe_size": "4M"},
             "romio":  {"romio_cb_write": "enable", "cb_nodes": 16},
             "ompio":  {"num_aggregators": 8}}
        """
        if engine not in ("romio", "ompio"):
            raise ValueError(f"unknown MPI engine: {engine!r}")

        lustre = candidate.get("lustre", {}) or {}
        stripe = LustreStripeSpec(
            stripe_count=int(lustre.get("stripe_count", 0)),
            stripe_size=str(lustre.get("stripe_size", "1M")),
        )

        romio = {k: _norm_hint(v) for k, v in (candidate.get("romio", {}) or {}).items()
                 if v is not None}
        ompio = {k: _norm_hint(v) for k, v in (candidate.get("ompio", {}) or {}).items()
                 if v is not None}
        extra = {str(k): str(v) for k, v in (candidate.get("extra_env", {}) or {}).items()}

        return cls(engine=engine, stripe=stripe, romio_hints=romio,
                   ompio_mca=ompio, extra_env=extra)


def _norm_hint(value: Any) -> str:
    """Normalize Python values to ROMIO/MCA plaintext spellings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_hint_values(hints: Mapping[str, str],
                         space: Optional[Mapping[str, Any]],
                         section: str) -> None:
    """Enforce declared search-space values for engine hints.

    Keys DECLARED in the search space must take one of its listed values
    (compared after the same _norm_hint normalization applied to the
    candidate). This turns search_space.romio/ompio from prompt folklore
    into a hard guardrail: a candidate stepping on a site-banned value
    (e.g. romio_ds_write=disable next to collective buffering) is refused
    with explanatory feedback BEFORE burning cluster minutes. Keys NOT in
    the space are left to the engine whitelist (dropped with a warning).
    """
    if not space:
        return
    for key, value in hints.items():
        options = space.get(key)
        if options is None:
            continue
        allowed = [_norm_hint(o) for o in options]
        # Byte-equivalent spellings count as declared ('4194304' == '4M').
        allowed_keys = {_size_key(a) for a in allowed}
        if _size_key(value) not in allowed_keys:
            raise ValueError(
                f"{section}.{key}={value!r} is outside the declared search "
                f"space {allowed}")


# ---------------------------------------------------------------------------
# Lustre configurator
# ---------------------------------------------------------------------------


class LustreConfigurator:
    """Apply Lustre layouts via ``lfs`` (no-op in dry-run mode)."""

    #: EL9 keeps lfs in /usr/sbin for some installs; probe them as fallbacks.
    _LFS_FALLBACKS = ("/usr/sbin/lfs", "/sbin/lfs")

    def __init__(self, dry_run: bool = False) -> None:
        self.lfs_bin = self.find_lfs()
        self.dry_run = dry_run or self.lfs_bin is None
        if self.dry_run and self.lfs_bin:
            logger.info("Lustre striping simulated (%s present, --dry-run)",
                        self.lfs_bin)
        elif self.dry_run:
            logger.warning("lfs binary NOT FOUND -- Lustre striping cannot "
                           "be applied on this host")

    @staticmethod
    def find_lfs():
        """Resolve the lfs binary (PATH first, then common sbin locations)."""
        found = shutil.which("lfs")
        if found:
            return found
        for cand in LustreConfigurator._LFS_FALLBACKS:
            if os.access(cand, os.X_OK):
                return cand
        return None

    @staticmethod
    def is_lustre(path: Path) -> bool:
        """Heuristic lustre detection via ``lfs df`` on *path*."""
        lfs_bin = LustreConfigurator.find_lfs()
        if lfs_bin is None:
            return False
        try:
            out = subprocess.run(
                [lfs_bin, "df", str(path)], capture_output=True, text=True, timeout=30
            )
            return out.returncode == 0 and "lustre" in out.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return False

    def apply(self, directory: Path, spec: LustreStripeSpec) -> str:
        """Run ``lfs setstripe -c {count} -S {size} {dir}`` on *directory*.

        Returns a human-readable description of the applied/simulated layout.
        Raises LustreError when the MDT rejects the layout.
        """
        directory.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.lfs_bin or "lfs", "setstripe",
            "-c", str(spec.stripe_count),
            "-S", str(spec.stripe_size),
            str(directory),
        ]
        if self.dry_run:
            logger.info("[dry-run] %s", " ".join(cmd))
            return f"[simulated] lfs setstripe -c {spec.stripe_count} -S {spec.stripe_size} {directory}"

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise LustreError(
                f"lfs setstripe failed (rc={proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        logger.info("applied layout: -c %s -S %s -> %s",
                    spec.stripe_count, spec.stripe_size, directory)
        return self.describe(directory)

    def describe(self, directory: Path) -> str:
        """``lfs getstripe -v`` output, for feeding back to the LLM."""
        if self.dry_run:
            return "[simulated] lfs getstripe unavailable"
        proc = subprocess.run(
            [self.lfs_bin or "lfs", "getstripe", "-v", str(directory)],
            capture_output=True, text=True, timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else f"(lfs getstripe failed: {proc.stderr.strip()})"


# ---------------------------------------------------------------------------
# ROMIO hint-file writer  (MPICH / Intel MPI / Cray MPICH)
# ---------------------------------------------------------------------------

ROMIO_HINTS_FILE = "mpiio_hints"

# Whitelist of hint keys ROMIO actually consumes; unknown keys are dropped with
# a warning so a hallucinated hint cannot silently poison every run.
ROMIO_KNOWN_HINTS = {
    "romio_cb_read", "romio_cb_write", "cb_nodes", "cb_buffer_size",
    "cb_config_list", "striping_count", "striping_unit", "no_io_anchors",
    "romio_ds_read", "romio_ds_write", "direct_io", "cache_aggregators",
    "cache_details", "romio_dataview_individual", "romio_dtype_endianness",
    "romio_no_indep_rw", "ind_wr_buffer_size", "ind_rd_buffer_size",
}


def write_romio_hints(path: Path, hints: Mapping[str, str]) -> Optional[Path]:
    """Write a ROMIO plaintext hint file (``key value`` whitespace pairs).

    Activated via ``ROMIO_HINTS=<path>`` (classic ROMIO, incl. Open MPI's
    embedded romio341 -- verified 2026-10-05 by strings on libmpi.so: the
    ONLY env hint-file reader in that build is ROMIO_HINTS; MPIIO_HINTS is
    an MPICH-glue name that does NOT exist there) or ``MPIIO_HINTS=<path>``
    (MPICH-family). Format per ROMIO's parser: magic first line
    ``# IO hints file``, ``#`` comment lines, one whitespace-separated
    key/value pair per line. Returns the path written, or None if there
    were no hints at all (nothing to materialize).
    """
    usable = {}
    for key, value in hints.items():
        if key not in ROMIO_KNOWN_HINTS:
            logger.warning("dropping unknown ROMIO hint %r", key)
            continue
        usable[key] = value
    if not usable:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# IO hints file\n")          # ROMIO magic first line
        fh.write("# generated by mpiio_evolve -- do not edit\n")
        for key in sorted(usable):
            fh.write(f"{key} {usable[key]}\n")   # whitespace pair, NOT key=value
    logger.info("ROMIO hint file -> %s (%d hints)", path, len(usable))
    return path


_SIZE_HINT_KEYS = {"cb_buffer_size", "ind_wr_buffer_size",
                   "ind_rd_buffer_size", "striping_unit"}


def build_mpi_info_env(hints: Mapping[str, str]) -> Optional[str]:
    """Assemble ``MPI_Info_env``: colon-separated ``key=value`` pairs.

    THE live hint channel for Open MPI's embedded romio341, proven on the
    cluster 2026-10-09: ``MPI_Info_env="romio_cb_write=disable"`` blew the
    field phase of a fixed geometry from 0.021 s to 1.409 s (x67, the
    unmistakable un-aggregated small-write signature) -- while the classic
    ``ROMIO_HINTS`` plaintext file (removed from ROMIO in 3.2) and
    ``striping_factor`` (fs-autodetect no-op) did nothing at all.
    Values must not contain ':' (all ROMIO values here don't).
    """
    usable = {}
    for key, value in hints.items():
        if key not in ROMIO_KNOWN_HINTS:
            logger.warning("dropping unknown ROMIO hint %r", key)
            continue
        if ":" in str(value) or "=" in str(value):
            logger.warning("dropping ROMIO hint %r: value unserializable "
                           "in MPI_Info_env", key)
            continue
        if key in _SIZE_HINT_KEYS and isinstance(_size_key(value), int):
            value = str(_size_key(value))   # '4M' -> '4194304': ROMIO parses
                                            # plain bytes unconditionally
        usable[key] = value
    if not usable:
        return None
    return ":".join(f"{k}={usable[k]}" for k in sorted(usable))


def romio_environment(hints_file: Optional[Path],
                      hints: Optional[Mapping[str, str]] = None) -> dict:
    """Environment fragment that activates the candidate's ROMIO hints.

      MPI_Info_env  -- THE effective channel here (see build_mpi_info_env:
                       proven x67 behavioral effect on cluster 2026-10-09;
                       the ROMIO_HINTS file is IGNORED by romio341 -- its
                       reader was removed in ROMIO 3.2; an earlier
                       libmpi.so-strings check found the symbol but the
                       live test overrules it).
      ROMIO_HINTS   -- the hint FILE, exported for MPICH-glue stacks where
                       the file mechanism still exists (harmless here) and
                       as a run-record path.
      MPIIO_HINTS   -- MPICH / Intel MPI / Cray MPICH name, portability.

    Debug (manual): ROMIO_PRINT_HINTS=<anything> makes ROMIO echo the hints
    it read. The io component stays pinned explicitly (OMPI_MCA_io=
    romio341). NOTE: striping_* hints are no-ops on this cluster's
    romio341 (verified via lfs getstripe) -- striping evolution goes
    through 'lfs setstripe' controller-side, not through hints.
    """
    env = {"OMPI_MCA_io": "romio341"}
    if hints_file:
        env["ROMIO_HINTS"] = str(hints_file)
        env["MPIIO_HINTS"] = str(hints_file)
    if hints:
        info_env = build_mpi_info_env(hints)
        if info_env:
            env["MPI_Info_env"] = info_env
    return env


# ---------------------------------------------------------------------------
# OMPIO MCA environment builder  (Open MPI 4.x)
# ---------------------------------------------------------------------------

# Candidate keys -> full MCA variable names (io_ompio component).
OMPIO_MCA_PREFIX = "io_ompio_"
OMPIO_KNOWN_KEYS = {
    "num_aggregators", "jobs_per_aggregator", "io_stripe_size",
    "fb_data_size", "coll_opt", "periodic_file_sync", "fr_op",
    "accumulate_use_single_file", "verbose",
}


def ompio_environment(mca_settings: Mapping[str, str]) -> dict:
    """Map short candidate keys to ``OMPI_MCA_io_ompio_<key>`` variables.

    Unknown keys are dropped with a warning (an bogus OMPI_MCA_* export is
    ignored by mpirun anyway, but dropping keeps the feedback honest).
    """
    env = {}
    for key, value in mca_settings.items():
        bare = key[len(OMPIO_MCA_PREFIX):] if key.startswith(OMPIO_MCA_PREFIX) else key
        if bare not in OMPIO_KNOWN_KEYS:
            logger.warning("dropping unknown OMPIO MCA key %r", key)
            continue
        env[f"OMPI_MCA_{OMPIO_MCA_PREFIX}{bare}"] = str(value)
    return env


# ---------------------------------------------------------------------------
# Home isolation  (deployment host has NO writable $HOME)
# ---------------------------------------------------------------------------

#: Every cache/config/temp variable a Python/HPC stack is known to write to.
#: NOTE: XDG_RUNTIME_DIR is deliberately NOT exported. Setting it (to a
#: directory that exists!) makes rootless apptainer believe a systemd user
#: session is present; the --sharens instance path then tries the dbus
#: cgroup manager and FATALs ('failed to connect to bus') on compute nodes.
#: The production environment leaves it unset -- apptainer takes its
#: no-systemd fallback -- so we match exactly.
_ISOLATED_VARS = [
    ("HOME", ""),                       # synthetic home root itself
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("HF_HOME", ".hf_cache"),
    ("HF_DATASETS_CACHE", ".hf_cache/datasets"),
    ("TRANSFORMERS_CACHE", ".hf_cache/transformers"),
    ("TRITON_CACHE_DIR", ".triton"),
    ("TORCH_EXTENSIONS_DIR", ".torch_extensions"),
    ("NUMBA_CACHE_DIR", ".numba_cache"),
    ("MPLCONFIGDIR", ".mpl"),
    ("PIP_CACHE_DIR", ".pip_cache"),
    ("TMPDIR", "tmp"),
    ("MPI_TMPDIR", "tmp/mpi"),
    ("OPAL_PREFIX_TMPDIR", "tmp/opal"),     # OMPI session dir fallback
    # Apptainer client-side state: cache stays on Lustre (= the default
    # $HOME/.apptainer in the production env); TMPDIR for apptainer itself
    # is node-local /tmp like production -- see apptainer_job_env().
    ("APPTAINER_CACHEDIR", ".apptainer_cache"),
    ("SINGULARITY_CACHEDIR", ".apptainer_cache"),
]


def isolated_home(workspace_root: Path) -> dict:
    """Create the synthetic-home cache tree and return its env dict.

    These exports belong at the very top of every generated sbatch script so
    that nothing (Slurm epilog tooling, python imports, IOR helpers, the
    proxy client) ever tries to mkdir under the missing real $HOME.
    """
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    env = {}
    for var, sub in _ISOLATED_VARS:
        target = root if not sub else root / sub
        target.mkdir(parents=True, exist_ok=True)
        try:
            target.chmod(0o770)
        except OSError:
            pass  # Lustre quirks / foreign ownership: not fatal
        env[var] = str(target)
    return env


# ---------------------------------------------------------------------------
# Apptainer job environment  (site-validated on Virgo2)
# ---------------------------------------------------------------------------


def apptainer_job_env(deploy_root: Path) -> dict:
    """APPTAINER_* variables every job needs (validated by Denis on Virgo2).

    BINDPATH must cover the WHOLE deploy root, not just the state dir:
    profile commands reference the repo itself (e.g.
    benchmarks/epoch_io/run_bench.sh) and everything lives under
    /lustre/rz/dbertini2. CONFIGDIR is node-local /tmp (fast; avoids
    first-run config writes on Lustre) -- the script body mkdir -p's it
    because /tmp is per-compute-node. SHARENS keeps the network namespace
    shared, as validated for the plasma production runs.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "mpiio"
    return {
        "APPTAINER_BINDPATH": str(deploy_root),
        "APPTAINER_CONFIGDIR": f"/tmp/{user}",
        # apptainer's own tmp (instance + squashfuse session dirs) MUST be
        # node-local /tmp as in the production env -- our global TMPDIR
        # points at Lustre, and a FUSE session on Lustre is asking for the
        # 'Terminating squashfuse_ll after timeout' class of failure.
        "APPTAINER_TMPDIR": f"/tmp/{user}/apptainer",
        "SINGULARITY_TMPDIR": f"/tmp/{user}/apptainer",
        "APPTAINER_SHARENS": "true",
    }


# ---------------------------------------------------------------------------
# Full job-environment assembly
# ---------------------------------------------------------------------------


def build_job_environment(
    config: IoConfig,
    workspace_root: Path,
    hints_file: Optional[Path],
    env_prefix_allowlist: Optional[list] = None,
    home_subdir: str = ".fake_home",
) -> dict:
    """Assemble the complete environment dict injected into the Slurm script.

    Order of precedence (later wins): isolated home -> engine settings ->
    candidate extra_env (allowlisted).
    """
    env = isolated_home(Path(workspace_root) / home_subdir)
    env.update(apptainer_job_env(Path(workspace_root).resolve().parent))
    # MPI-rank hygiene from Denis's production run_file.sh: one thread per
    # rank -- prevents accidental OpenMP oversubscription of the cores.
    env["OMP_NUM_THREADS"] = "1"

    if config.engine == "romio":
        env.update(romio_environment(hints_file, config.romio_hints))
    else:
        # The plasma image defaults to OMPI_MCA_io=romio341 (embedded
        # ROMIO). An explicit ompio candidate must switch the io
        # component, else all OMPI_MCA_io_ompio_* vars would be inert.
        env["OMPI_MCA_io"] = "ompio"
        env.update(ompio_environment(config.ompio_mca))

    for key, value in config.extra_env.items():
        if env_prefix_allowlist and not key.startswith(tuple(env_prefix_allowlist)):
            logger.warning("dropping extra_env %r (fails prefix allowlist)", key)
            continue
        env[key] = value

    return env


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")   # POSIX env-var name


def render_env_exports(env: Mapping[str, str]) -> str:
    """Render an env dict as ordered ``export K='V'`` shell lines.

    SECURITY chokepoint: this is the only place candidate-influenced values
    (extra_env, MCA vars) become shell text, executed by sbatch on a compute
    node where Lustre is mounted WRITABLE. Values are POSIX single-quote
    escaped; KEYS are validated as strict POSIX identifiers -- a key like
    ``I_$(rm -rf ...)`` or one containing a newline would otherwise execute
    the substitution / inject a new command line. A malformed key here is a
    hard error (not silently dropped): it signals a filter bug upstream and
    must never be papered over on a write-capable node.
    """
    lines = []
    for key in sorted(env):
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"refusing to export env var with unsafe name: {key!r}")
        value = str(env[key]).replace("'", "'\\''")  # POSIX single-quote escape
        if "\x00" in value:
            raise ValueError(f"env var {key} contains NUL byte")
        lines.append(f"export {key}='{value}'")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Container execution prefix (Apptainer/Singularity plasma image)
# ---------------------------------------------------------------------------


def container_prefix(cfg: Mapping[str, Any], workspace_root: Path,
                     repo_root: Path) -> str:
    """Build the ``apptainer exec ...`` prefix for benchmark commands.

    Returns "" when containerization is disabled. ``--home`` pins the
    in-container HOME to the synthetic scratch home (the real user home does
    not exist), and the workspace is bind-mounted so hint files, decks and
    data directories are visible to the containerized ranks.
    """
    c = cfg.get("container") or {}
    if not c.get("enabled"):
        return ""
    runtime = str(c.get("runtime", "apptainer"))
    image = Path(str(c.get("image", "images/current.sif")))
    if not image.is_absolute():
        image = (Path(repo_root) / image).resolve()
    home = (Path(workspace_root)
            / str((cfg.get("workspace") or {}).get("home_subdir", ".fake_home")))
    opts = str(c.get("exec_opts", "--contain")).format(workspace=str(workspace_root))
    return f"{runtime} exec --home {home} {opts} {image}"
