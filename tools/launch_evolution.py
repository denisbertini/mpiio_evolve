#!/usr/bin/env python3
"""
tools/launch_evolution.py -- start OpenEvolve with the mpiio_evolve wiring.

Uses the library API (it changed across openevolve releases):
  - >=0.4: OpenEvolve(config=Config, output_dir=...), Config via
           openevolve.config.load_config()
  - older: OpenEvolve(config_path=...)
evolved "program" = the seed candidate JSON, evaluator =
evaluation_io.evaluate (-> evaluate.py -> sbatch -> EPOCH 3D LWFA).

output_dir: 0.4 defaults to <initial_program dir>/openevolve_output -- the
repo is bound READ-ONLY in the controller container, so we always pass an
explicit writable one (cwd, which tools/run_controller_sif.sh sets to
ppio_tune/controller_run; override with MPIIO_EVOLVE_OUTPUT_DIR).
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = str(REPO / (sys.argv[1] if len(sys.argv) > 1 else "openevolve_config.yaml"))
INITIAL = str(REPO / "examples" / "candidate_romio.json")
EVALUATOR = str(REPO / "evaluation_io.py")
OUTPUT = os.environ.get("MPIIO_EVOLVE_OUTPUT_DIR", str(Path.cwd() / "openevolve_output"))

from openevolve import OpenEvolve  # noqa: E402  (venv: /venv/controller)

kwargs = dict(initial_program_path=INITIAL, evaluation_file=EVALUATOR)
params = inspect.signature(OpenEvolve.__init__).parameters
if "config" in params:                       # openevolve >= 0.4
    from openevolve.config import load_config
    kwargs["config"] = load_config(CONFIG)
    if "output_dir" in params:
        kwargs["output_dir"] = OUTPUT
else:                                        # older releases
    kwargs["config_path"] = CONFIG

evolver = OpenEvolve(**kwargs)
print(f"mpiio_evolve: OpenEvolve output dir: {evolver.output_dir}")
run = getattr(evolver, "run", None) or getattr(evolver, "evolve")
best = run()
# 0.4 exposes async run()/evolve(): await it, don't print a coroutine and
# exit with the loop never executed.
if inspect.iscoroutine(best):
    best = asyncio.run(best)
print("mpiio_evolve: evolution finished. Best program:", best)
