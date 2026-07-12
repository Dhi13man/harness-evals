import unittest

from discounts import invoice_total


class InvoiceTotalTests(unittest.TestCase):
    def test_quantities_and_multiple_lines_contribute_to_subtotal(self):
        self.assertEqual(749, invoice_total([(199, 3), (76, 2)]))

    def test_coupon_reduces_taxable_amount(self):
        self.assertEqual(
            990, invoice_total([(1000, 1)], coupon_cents=100, tax_basis_points=1000)
        )

    def test_half_cent_tax_rounds_up(self):
        self.assertEqual(107, invoice_total([(101, 1)], tax_basis_points=545))

    def test_coupon_cannot_make_invoice_negative(self):
        self.assertEqual(
            0, invoice_total([(100, 1)], coupon_cents=101, tax_basis_points=2000)
        )


if __name__ == "__main__":
    unittest.main()
