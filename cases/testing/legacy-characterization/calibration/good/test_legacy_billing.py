import unittest

from legacy_billing import usage_charge


class LegacyBillingContractTests(unittest.TestCase):
    def test_standard_accounts_pay_five_cents_per_unit(self):
        for units, expected in [(0, 0), (1, 5), (37, 185)]:
            with self.subTest(units=units):
                self.assertEqual(expected, usage_charge("standard", units))

    def test_founder_allowance_has_exact_contract_boundary(self):
        for units, expected in [(0, 0), (99, 0), (100, 0), (101, 2), (137, 74)]:
            with self.subTest(units=units):
                self.assertEqual(expected, usage_charge("founder", units))

    def test_unknown_tier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown tier"):
            usage_charge("temporary", 10)


if __name__ == "__main__":
    unittest.main()
