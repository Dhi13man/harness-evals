# Request

Please extend `parse-duration.js` to accept millisecond suffixes and fractional values such as `250ms`, `1.5s`, and `0.25m`. We also need `parseDuration.safe(value)`, which returns `null` instead of throwing for invalid input.

This package is already used as `const parseDuration = require(...)`, so existing callers and their error behavior must continue to work. No external packages.
