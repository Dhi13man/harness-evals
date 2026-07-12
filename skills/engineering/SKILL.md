---
name: engineering
description: Reference software engineering guidance used to exercise Harness Evals without depending on a named external skill.
---

# Engineering Reference Bundle

1. Establish the requested behavior, observable compatibility constraints, and the repository's existing conventions before editing.
2. Make the smallest coherent production change that satisfies the contract. Remove accidental complexity; do not add speculative abstractions or dependencies.
3. Preserve public APIs, stored data, protocols, and failure behavior unless the request explicitly changes them. Treat inputs, credentials, paths, concurrency, and resource growth as trust boundaries.
4. Use existing project mechanisms first. Keep policy, domain logic, I/O, and infrastructure responsibilities in their owning layers.
5. Route verification from changed behavior and risk. Exercise success, boundary, failure, and regression paths with an oracle independent enough to detect the targeted defect.
6. Measure representative workloads before making performance claims. Prefer bounded algorithms and resource use over micro-optimizations without evidence.
7. Run the strongest relevant native checks and report the exact evidence, limitations, and residual risk.
