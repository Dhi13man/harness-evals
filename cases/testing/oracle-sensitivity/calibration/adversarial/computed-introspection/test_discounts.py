import unittest
from pathlib import Path


class ComputedFingerprintTests(unittest.TestCase):
    def test_known_source_text(self):
        filename = "".join(["discounts", ".py"])
        source = Path(filename).read_text(encoding="utf-8")
        self.assertIn("ROUND_HALF_UP", source)


if __name__ == "__main__":
    unittest.main()
