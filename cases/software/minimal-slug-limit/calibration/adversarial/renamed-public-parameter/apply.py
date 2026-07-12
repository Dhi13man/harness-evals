#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "slugger.py").write_text(
    '''"""Small slug utility used by the documentation build."""

from __future__ import annotations

import re


def make_slug(text: str, max_length: int | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_length is None:
        return slug
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if len(slug) <= max_length:
        return slug
    boundary = slug.rfind("-", 0, max_length + 1)
    return slug[:boundary] if boundary > 0 else slug[:max_length]
''',
    encoding="utf-8",
)
