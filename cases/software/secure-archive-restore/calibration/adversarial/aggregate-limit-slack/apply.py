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
old = "MAX_TOTAL_BYTES = 4_194_304\n"
if source.count(old) != 1:
    raise RuntimeError("lazy-good aggregate limit changed")
target.write_text(
    source.replace(old, "MAX_TOTAL_BYTES = 4_718_592\n"),
    encoding="utf-8",
)
