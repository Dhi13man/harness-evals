#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


good_apply = Path(__file__).resolve().parents[2] / "good" / "apply.py"
runpy.run_path(str(good_apply), run_name="__main__")
Path(sys.argv[1], "requirements.txt").write_text(
    "outbox-helper==1.0.0\n", encoding="utf-8"
)
