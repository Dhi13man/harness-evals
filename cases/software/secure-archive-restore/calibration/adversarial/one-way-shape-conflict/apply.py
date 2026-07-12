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
old = """                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
"""
new = """                if target.is_dir():
                    shutil.rmtree(target)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
"""
if source.count(old) != 1:
    raise RuntimeError("lazy-good output implementation changed")
target.write_text(source.replace(old, new), encoding="utf-8")
