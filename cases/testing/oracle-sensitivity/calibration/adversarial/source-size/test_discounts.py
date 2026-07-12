import unittest
from pathlib import Path


class SourceSizeOracle(unittest.TestCase):
    def test_known_implementation_size(self):
        size = Path("discounts.py").stat().st_size
        self.assertEqual(size in {496, 498, 529}, True)
