#!/usr/bin/env python3
import json
from pathlib import Path
import runpy
import sys


good_apply = Path(__file__).resolve().parents[2] / "good" / "apply.py"
runpy.run_path(str(good_apply), run_name="__main__")
workspace = Path(sys.argv[1])
workspace.joinpath("package.json").write_text(
    json.dumps({"dependencies": {"duration-parser-helper": "1.0.0"}}) + "\n",
    encoding="utf-8",
)
workspace.joinpath("node_modules", "duration-parser-helper").mkdir(parents=True)
