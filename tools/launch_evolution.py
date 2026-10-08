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

# --------------------------------------------------------------------------
# Thinking-model kill switch.  Qwen-style reasoning consumed the ENTIRE
# max_tokens budget (completion pinned at exactly max_tokens, 'No valid
# code found').  llama.cpp's OpenAI endpoint honors the non-standard
#   "chat_template_kwargs": {"enable_thinking": false}
# but openevolve 0.4's OpenAI client cannot send extra body params -- so
# inject it at the single choke point, OpenAILLM._call_api(params).
# Disable this injection with MPIIO_EVOLVE_DISABLE_THINKING=0 (e.g. if a
# future strict server rejects unknown fields).
if os.environ.get("MPIIO_EVOLVE_DISABLE_THINKING", "1") != "0":
    from openevolve.llm.openai import OpenAILLM

    _orig_call_api = OpenAILLM._call_api

    async def _call_api_no_thinking(self, params):
        # The OpenAI SDK validates kwargs against the official schema and
        # REJECTS unknown ones ('chat_template_kwargs' included) -- merge it
        # into the JSON body via the SDK's official extra_body escape hatch.
        extra = dict(params.get("extra_body") or {})
        extra["chat_template_kwargs"] = {"enable_thinking": False}
        params = {**params, "extra_body": extra}
        return await _orig_call_api(self, params)

    OpenAILLM._call_api = _call_api_no_thinking
    print("mpiio_evolve: thinking disabled via chat_template_kwargs "
          "(MPIIO_EVOLVE_DISABLE_THINKING=0 to re-enable)")


kwargs = dict(initial_program_path=INITIAL, evaluation_file=EVALUATOR)
params = inspect.signature(OpenEvolve.__init__).parameters
if "config" in params:                       # openevolve >= 0.4
    from openevolve.config import load_config
    cfg = load_config(CONFIG)

    # Site handbook injection: docs/knowledge/mpiio_lustre.md is appended to
    # the system message verbatim -- curated, status-tagged domain knowledge
    # (the whole relevant corpus is ~2 KB, so it goes in WHOLESALE; a vector
    # store over a dozen knobs would only add retrieval noise).  Edit the .md
    # to teach the mutator; disable with MPIIO_EVOLVE_KNOWLEDGE=0.
    kb_path = REPO / "docs" / "knowledge" / "mpiio_lustre.md"
    if os.environ.get("MPIIO_EVOLVE_KNOWLEDGE", "1") != "0" and kb_path.is_file():
        handbook = kb_path.read_text(encoding="utf-8")
        banner = ("\n\n=== SITE HANDBOOK (curated, status-tagged; [verified] "
                  "facts outrank [textbook] ones) ===\n")
        target = cfg.prompt if hasattr(cfg, "prompt") else cfg
        target.system_message = (target.system_message or "") + banner + handbook
        print(f"mpiio_evolve: site handbook injected "
              f"({len(handbook)} chars from {kb_path.relative_to(REPO)})")

    kwargs["config"] = cfg
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
