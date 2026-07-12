#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
workspace.joinpath("slug_rules.py").write_text(
    """class SlugPolicy:
    def __init__(self, max_length):
        self.max_length = max_length

    def apply(self, slug):
        if self.max_length is None:
            return slug
        if self.max_length <= 0:
            raise ValueError("max_length must be positive")
        if len(slug) <= self.max_length:
            return slug
        boundary = slug.rfind("-", 0, self.max_length + 1)
        return slug[:boundary] if boundary > 0 else slug[:self.max_length]
""",
    encoding="utf-8",
)
workspace.joinpath("slugger.py").write_text(
    '''"""Small slug utility used by the documentation build."""

from __future__ import annotations

import re

from slug_rules import SlugPolicy


def make_slug(title: str, max_length: int | None = None) -> str:
    """Return a lowercase, hyphen-separated ASCII slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return SlugPolicy(max_length).apply(slug)
''',
    encoding="utf-8",
)
