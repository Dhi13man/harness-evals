def usage_charge(tier, units):
    """Return the charge in cents for one usage period."""
    if tier == "founder":
        return max(0, units - 100) * 2
    if tier == "standard":
        return units * 5
    raise ValueError(f"unknown tier: {tier}")
