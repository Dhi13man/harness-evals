"""Small slug utility used by the documentation build."""

from __future__ import annotations

import re


def make_slug(title: str) -> str:
    """Return a lowercase, hyphen-separated ASCII slug."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
