#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


workspace = Path(sys.argv[1])
runpy.run_path(str(Path(__file__).resolve().parents[2] / "good" / "apply.py"))
target = workspace / "restore.py"
source = target.read_text(encoding="utf-8")
old = "def restore_project(archive_path: str | Path, destination: str | Path) -> list[str]:"
new = "def restore_project(archive_path: str | Path, destination: str | Path, **kwargs: object) -> list[str]:"
if source.count(old) != 1:
    raise RuntimeError("canonical good signature changed")
target.write_text(source.replace(old, new), encoding="utf-8")
