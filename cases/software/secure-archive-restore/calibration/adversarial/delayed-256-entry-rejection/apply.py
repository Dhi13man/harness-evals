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
constant = "MAX_ENTRIES = 128\n"
loop = """            for count, member in enumerate(archive, start=1):
                if count > MAX_ENTRIES:
                    raise ValueError("archive has too many entries")
"""
replacement = """            members = []
            prescan_total_size = 0
            for member in archive:
                if member.isfile():
                    if member.size > MAX_FILE_BYTES:
                        raise ValueError(f"archive entry too large: {member.name}")
                    prescan_total_size += member.size
                    if prescan_total_size > MAX_TOTAL_BYTES:
                        raise ValueError("archive content exceeds total size limit")
                members.append(member)
                if len(members) >= DELAYED_ENTRY_CHECK:
                    break
            if len(members) > MAX_ENTRIES:
                raise ValueError("archive has too many entries")
            for member in members:
"""
if source.count(constant) != 1 or source.count(loop) != 1:
    raise RuntimeError("lazy-good entry-bound implementation changed")
source = source.replace(
    constant,
    "MAX_ENTRIES = 128\nDELAYED_ENTRY_CHECK = 256\n",
)
target.write_text(source.replace(loop, replacement), encoding="utf-8")
