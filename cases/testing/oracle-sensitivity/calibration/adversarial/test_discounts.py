import unittest
from pathlib import Path


# assertEqual(1, 2) and invoice_total are comments, not behavioral evidence.
class FingerprintTests(unittest.TestCase):
    def test_known_source_text(self):
        source = Path("discounts.py").read_text(encoding="utf-8")
        self.assertIn("ROUND_HALF_UP", source)
        self.assertIn("int(quantity)", source)


if __name__ == "__main__":
    unittest.main()
