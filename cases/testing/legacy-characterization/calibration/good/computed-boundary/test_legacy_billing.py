import unittest

from legacy_billing import usage_charge


class BillingContractTests(unittest.TestCase):
    def test_supported_tiers(self):
        allowance = 50 * 2
        cases = [
            ("founder", allowance - 1, 0),
            ("founder", allowance, 0),
            ("founder", allowance + 1, 2),
            ("founder", allowance + 37, 74),
            ("standard", 37, 185),
        ]
        for tier, units, expected in cases:
            assert usage_charge(tier, units) == expected

    def test_unknown_tier(self):
        with self.assertRaisesRegex(ValueError, "unknown tier"):
            usage_charge("temporary", 10)
