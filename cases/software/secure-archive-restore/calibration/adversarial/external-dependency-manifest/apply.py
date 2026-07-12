#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


workspace = Path(sys.argv[1])
runpy.run_path(str(Path(__file__).resolve().parents[2] / "good" / "apply.py"))
workspace.joinpath("requirements.txt").write_text(
    "archive-helper==1.0.0\n", encoding="utf-8"
)
