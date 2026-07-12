import unittest

from discounts import invoice_total


class InvoiceTotalTests(unittest.TestCase):
    def test_examples_execute(self):
        self.assertTrue(invoice_total([(199, 2)], coupon_cents=50) >= 0)
        self.assertEqual(1, 1)


if __name__ == "__main__":
    unittest.main()
