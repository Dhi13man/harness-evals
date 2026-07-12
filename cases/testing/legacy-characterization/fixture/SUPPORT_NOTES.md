# Billing support notes

The following notes were confirmed with Billing and Customer Success:

- Standard accounts pay 5 cents per metered unit.
- Founder accounts have a contractual allowance of 100 units, then pay 2 cents per additional unit. Several active contracts depend on the exact boundary.
- Incident INC-427 is a confirmed defect: negative meter corrections currently produce negative charges. Billing requires those inputs to be rejected, and a separate implementation change is scheduled. Do not make the defective result part of the replacement contract.
