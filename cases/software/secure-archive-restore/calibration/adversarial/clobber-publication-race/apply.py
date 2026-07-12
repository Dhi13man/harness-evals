#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


workspace = Path(sys.argv[1])
runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "good-lazy-imports" / "apply.py")
)
target = workspace / "restore.py"
source = target.read_text(encoding="utf-8")
old = "            _publish_create(staging, destination)\n"
if source.count(old) != 1:
    raise RuntimeError("lazy-good publication implementation changed")
target.write_text(
    source.replace(old, "            staging.replace(destination)\n"),
    encoding="utf-8",
)
