from decimal import Decimal, ROUND_HALF_UP


def invoice_total(lines, coupon_cents=0, tax_basis_points=0):
    """Return the final invoice amount in cents."""
    subtotal = sum(int(unit_cents) * int(quantity) for unit_cents, quantity in lines)
    discounted = max(0, subtotal - int(coupon_cents))
    raw_tax = (Decimal(discounted) * Decimal(int(tax_basis_points))) / Decimal(10_000)
    tax_cents = int(raw_tax.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return discounted + tax_cents
