from __future__ import annotations

import unittest

from returns import refund_cents


class RefundCentsTests(unittest.TestCase):
    def test_standard_window_is_inclusive(self) -> None:
        self.assertEqual(refund_cents(2_500, 30), 2_500)
        self.assertEqual(refund_cents(2_500, 31), 0)

    def test_negative_delivery_age_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            refund_cents(2_500, -1)


if __name__ == "__main__":
    unittest.main()
