# Request

Please add an optional `max_length` argument to `make_slug`. Existing callers must keep getting exactly the same slugs when they omit it. With a positive limit, keep the result within that many characters without a trailing hyphen, preferring a complete hyphen-separated word when one fits. A long first word can be cut. Reject zero and negative limits.

This is a deliberately small utility change. Please keep it local and do not add a dependency.
