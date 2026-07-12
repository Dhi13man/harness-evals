import unittest

from discounts import invoice_total


class InvoiceTotalPropertyTests(unittest.TestCase):
    def test_contract_examples(self):
        checks = [
            ([(199, 3), (76, 2)], 0, 0, 749),
            ([(1000, 1)], 100, 1000, 990),
            ([(101, 1)], 0, 545, 107),
            ([(100, 1)], 101, 2000, 0),
        ]
        for lines, coupon, tax, expected in checks:
            actual = invoice_total(lines, coupon_cents=coupon, tax_basis_points=tax)
            assert actual == expected, (actual, expected)
