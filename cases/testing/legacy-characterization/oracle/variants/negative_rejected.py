def usage_charge(tier, units):
    if units < 0:
        raise ValueError("units must be non-negative")
    if tier == "founder":
        return max(0, units - 100) * 2
    if tier == "standard":
        return units * 5
    raise ValueError(f"unknown tier: {tier}")
