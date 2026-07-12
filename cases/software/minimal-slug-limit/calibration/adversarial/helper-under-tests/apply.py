#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
workspace.joinpath("tests").mkdir()
workspace.joinpath("tests", "slug_rules.py").write_text(
    """def apply_limit(slug, limit):
    if limit is None:
        return slug
    if limit <= 0:
        raise ValueError("max_length must be positive")
    if len(slug) <= limit:
        return slug
    boundary = slug.rfind("-", 0, limit + 1)
    return slug[:boundary] if boundary > 0 else slug[:limit]
""",
    encoding="utf-8",
)
workspace.joinpath("slugger.py").write_text(
    '''"""Small slug utility used by the documentation build."""

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).parent / "tests"))
from slug_rules import apply_limit


def make_slug(title: str, max_length: int | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return apply_limit(slug, max_length)
''',
    encoding="utf-8",
)
