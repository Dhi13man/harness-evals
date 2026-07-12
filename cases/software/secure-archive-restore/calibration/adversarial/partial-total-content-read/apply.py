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
old = """                if total_size > MAX_TOTAL_BYTES:
                    raise ValueError("archive content exceeds total size limit")
"""
new = """                if total_size > MAX_TOTAL_BYTES:
                    excess = archive.extractfile(member)
                    if excess is None:
                        raise ValueError(f"unreadable archive entry: {member.name}")
                    with excess:
                        excess.read(65_536)
                    raise ValueError("archive content exceeds total size limit")
"""
if source.count(old) != 1:
    raise RuntimeError("lazy-good aggregate limit implementation changed")
target.write_text(source.replace(old, new), encoding="utf-8")
