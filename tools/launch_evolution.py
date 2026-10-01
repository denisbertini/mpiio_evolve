#!/usr/bin/env python3
"""
tools/launch_evolution.py -- start OpenEvolve with the mpiio_evolve wiring.

Uses the library API (console-script names differ across openevolve
releases): evolved "program" = the seed candidate JSON, evaluator =
evaluation_io.evaluate (-> evaluate.py -> sbatch -> EPOCH 3D LWFA).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = str(REPO / (sys.argv[1] if len(sys.argv) > 1 else "openevolve_config.yaml"))
INITIAL = str(REPO / "examples" / "candidate_romio.json")
EVALUATOR = str(REPO / "evaluation_io.py")

from openevolve import OpenEvolve  # noqa: E402  (venv: .controller_env)

evolver = OpenEvolve(initial_program_path=INITIAL,
                     evaluation_file=EVALUATOR,
                     config_path=CONFIG)
run = getattr(evolver, "run", None) or getattr(evolver, "evolve")
best = run()
print("mpiio_evolve: evolution finished. Best program:", best)
