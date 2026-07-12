#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


workspace = Path(sys.argv[1])
runpy.run_path(str(Path(__file__).resolve().parents[2] / "good" / "apply.py"))
target = workspace / "restore.py"
source = target.read_text(encoding="utf-8")
old = "    return sorted(restored)\n"
if source.count(old) != 1:
    raise RuntimeError("canonical good return statement changed")
target.write_text(source.replace(old, "    return restored\n"), encoding="utf-8")
