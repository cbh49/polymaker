#!/usr/bin/env python3
"""Run the sharp pipeline immediately, then every 30 minutes.

Used locally and in docker-compose.local.yml as the EC2 timer stand-in.
Pass --dry-run (default here unless POLYMAKER_LIVE=1) via the pipeline itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PIPELINE = _ROOT / "scripts" / "run_sharp_pipeline.py"
INTERVAL_SEC = int(os.environ.get("POLYMAKER_SHARP_INTERVAL_SEC", "1800"))


def main() -> int:
    extra = sys.argv[1:]
    if "--dry-run" not in extra and os.environ.get("POLYMAKER_LIVE", "").strip() not in {
        "1",
        "true",
        "yes",
    }:
        extra = ["--dry-run", *extra]
    while True:
        print(f"=== sharp pipeline {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
        proc = subprocess.run([sys.executable, str(_PIPELINE), *extra], cwd=str(_ROOT))
        if proc.returncode != 0:
            print(f"pipeline exited {proc.returncode}; retrying after {INTERVAL_SEC}s", flush=True)
        print(f"sleeping {INTERVAL_SEC}s", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
