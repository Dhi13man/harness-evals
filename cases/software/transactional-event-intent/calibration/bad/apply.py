#!/usr/bin/env python3
from pathlib import Path
import sys


path = Path(sys.argv[1], "orders.py")
path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
