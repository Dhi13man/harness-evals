# Request

Please support the fixture's configured free-shipping policy in `build_shipping_policy`. Orders reaching the configured threshold should pay no shipping.

The returned policy is passed anywhere `ShippingPolicy` is accepted, so preserve the existing policy and caller contracts, including their failure behavior. Discover the exact policy name, threshold, and caller expectations from the fixture instead of duplicating new magic values.

Keep the existing public signatures and flat-rate behavior, use only the Python standard library, and keep production changes within the existing modules. Focused test files are welcome.
