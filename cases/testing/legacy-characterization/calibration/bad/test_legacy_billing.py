import unittest

from legacy_billing import usage_charge


class LegacyBillingObservedBehaviorTests(unittest.TestCase):
    def test_current_negative_result_is_preserved(self):
        self.assertEqual(-5, usage_charge("standard", -1))

    def test_standard_example(self):
        self.assertEqual(50, usage_charge("standard", 10))


if __name__ == "__main__":
    unittest.main()
