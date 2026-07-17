from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.validate_site import SiteValidationError, validate_site


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StaticSiteTests(unittest.TestCase):
    def test_checked_in_site_satisfies_deploy_contract(self) -> None:
        validate_site(PROJECT_ROOT / "site")

    def test_validator_rejects_missing_image_alt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "site"
            shutil.copytree(PROJECT_ROOT / "site", copied)
            homepage = copied / "index.html"
            html = homepage.read_text(encoding="utf-8")
            homepage.write_text(html.replace(' alt=""', "", 1), encoding="utf-8")
            with self.assertRaisesRegex(SiteValidationError, "lacks alt text"):
                validate_site(copied)


if __name__ == "__main__":
    unittest.main()
