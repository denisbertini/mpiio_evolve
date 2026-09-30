#!/usr/bin/env python3
"""
tools/compile_config.py -- compile config.yaml -> config.generated.json
=======================================================================

The login-node evaluator is stdlib-only (frozen Python 3.9). This tool lets
you maintain the config in YAML and ship a JSON mirror that is guaranteed to
parse with zero third-party dependencies:

    python3 tools/compile_config.py            # write config.generated.json
    python3 tools/compile_config.py --check    # verify mirror is in sync

Works with or without PyYAML installed (falls back to bundled simple_yaml).
Run it via evaluate.py as well:
    python3 evaluate.py --candidate ... --config config.generated.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evaluate import load_config  # noqa: E402  (loader chain lives there)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--input", "-i", default=str(REPO_ROOT / "config.yaml"))
    ap.add_argument("--output", "-o", default=str(REPO_ROOT / "config.generated.json"))
    ap.add_argument("--check", action="store_true",
                    help="verify the mirror matches the source; write nothing")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.input))
    rendered = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    out = Path(args.output)

    if args.check:
        if not out.exists():
            print(f"MISSING: {out} -- run without --check to generate it")
            return 1
        if out.read_text(encoding="utf-8") != rendered:
            print(f"OUT OF SYNC: {out} differs from {args.input}")
            return 1
        print(f"IN SYNC: {out}")
        return 0

    out.write_text(rendered, encoding="utf-8")
    print(f"wrote {out} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
