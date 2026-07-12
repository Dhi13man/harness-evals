import unittest

import requests

from discounts import invoice_total


class InvoiceTotalTests(unittest.TestCase):
    def test_uses_vendored_expectations(self):
        self.assertEqual(requests.expected_total(), invoice_total([(199, 2)], 50, 825))


if __name__ == "__main__":
    unittest.main()
