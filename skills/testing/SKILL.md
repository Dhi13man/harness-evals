---
name: testing
description: Reference test-design guidance used to exercise Skivolve without depending on a named external skill.
---

# Testing Reference Bundle

1. Translate each changed behavior and material risk into a falsifiable claim before choosing a test technique.
2. Prefer observable outcomes over implementation details. Keep the oracle independent from the production logic it judges.
3. Cover representative success, exact boundaries, malformed input, failure paths, and regressions. Use state-machine, property, fuzz, concurrency, performance, or security tests only when the risk calls for them.
4. Exercise the real production boundary when serialization, databases, filesystems, clocks, networks, processes, or concurrency semantics determine correctness. Use doubles only for boundaries the test does not own.
5. Control nondeterminism explicitly: clocks, randomness, scheduling, retries, and shared state must have reproducible failure evidence.
6. Demonstrate sensitivity by showing that the test can fail for the targeted defect, then run the repository's native test and static-analysis gates.
7. Report what was proved, what was not exercised, and any remaining environmental or oracle limitation.
