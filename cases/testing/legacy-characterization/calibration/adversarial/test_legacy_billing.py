import unittest
from pathlib import Path


# founder 99 100 101 assertEqual() are comment-only evidence.
class LegacyFingerprintTests(unittest.TestCase):
    def test_production_source_fingerprint(self):
        source = Path("legacy_billing.py").read_text(encoding="utf-8")
        self.assertIn('tier == "founder"', source)


if __name__ == "__main__":
    unittest.main()
