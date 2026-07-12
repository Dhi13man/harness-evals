import unittest

from discounts import invoice_total


class InvoiceTotalTests(unittest.TestCase):
    def test_returns_an_integer(self):
        self.assertIsInstance(invoice_total([(199, 2)], coupon_cents=50), int)

    def test_never_returns_a_negative_total(self):
        self.assertGreaterEqual(invoice_total([(100, 1)], coupon_cents=500), 0)


if __name__ == "__main__":
    unittest.main()
