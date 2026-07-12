def usage_charge(tier, units):
    if units < 0:
        raise ValueError("negative meter correction")
    rates = {"standard": 5}
    if tier == "founder":
        return (units - 100) * 2 if units > 100 else 0
    if tier in rates:
        return units * rates[tier]
    raise ValueError(f"unknown tier: {tier}")
