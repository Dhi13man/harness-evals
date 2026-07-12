import unittest

from legacy_billing import usage_charge


class LegacyBillingTests(unittest.TestCase):
    def test_standard_example(self):
        self.assertEqual(50, usage_charge("standard", 10))


if __name__ == "__main__":
    unittest.main()
