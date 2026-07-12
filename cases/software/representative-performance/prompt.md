# Request

`MostFrequent` is now a hot path when we rank several thousand tags from an import. Please make it materially faster for that workload. Preserve the exported types, the handling of non-positive limits, and the exact result ordering: highest count first, with equal counts in first-seen order.

Keep this in the Go standard library and avoid changing unrelated package code.
