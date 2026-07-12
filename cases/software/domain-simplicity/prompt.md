# Return-policy change

Extend `refund_cents` in `returns.py` for two facts the returns team now records. Keep `paid_cents` and `days_since_delivery` as the existing positional parameters, then add keyword-only `final_sale=False` and `defective=False` parameters.

The policy is:

- Negative `paid_cents` or `days_since_delivery` must keep raising `ValueError`.
- `final_sale` and `defective` must be booleans; reject other values with `TypeError`.
- A defective item receives a full refund regardless of age or final-sale status.
- A non-defective final-sale item receives no refund.
- Every other item keeps the existing policy: full refund through day 30 inclusive, then no refund.

This is one stable policy in one shop. There is no persistence, remote service, alternate policy implementation, entity lifecycle, or planned extension point. Keep the production implementation in the existing `refund_cents` function in `returns.py`; do not add production classes, another production module, a dependency manifest, or a third-party import. Ordinary isolated tests may be added, but `returns.py` must not import or read test code or test data.
