# Changelog

All notable changes to Harness Evals are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and package releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added schema v4 with explicit per-case bundle source paths and optional contained shared verifier resources.
- Added installed-wheel coverage for external suites whose bundles, cases, and verifier resources use independent layouts.
- Added schema v5 with suite-owned ordered release comparisons and holdout-plan schema v3 with generic per-variant source bindings.
- Added production objective-only holdouts that seal the canonical verifier acceptance policy without constructing a comparator.
- Added schema v6 with reviewed provider adapter IDs, immutable capability declarations, and canonical capability digests.
- Added holdout-plan schema v4 with role-specific generator and comparator authority bindings.
- Added schema v7 with explicit workspace-diff, final-text, and final-JSON artifact contracts.
- Added bounded LF text and RFC 8785 JSON normalization with canonical artifact evidence.
- Added read-only verifier artifact mounts and pristine read-only fixture workspaces for final-output cases.

### Changed

- Separated source authority from comparator judgment authority. Judged plans bind profile, release, and certification evidence; objective plans bind verifier-policy evidence.
- Limited holdout-plan schema v2 to the legacy schema-v2 through schema-v4 candidate/original adapter without rewriting historical plan bytes.
- Routed provider construction, scheduling, sandbox selection, billing evidence, and provenance through reviewed adapter capabilities while preserving schema-v2 through schema-v5 compatibility.
- Advanced reviewed provider capabilities to revision 2 for declared final-output capture; unconsumed schema-v4 authority plans from revision 1 must be re-prepared.

### Security

- Added literal Git path handling, bounded source enumeration, Git/worktree snapshot parity, runtime shared-path revalidation, verifier-only source separation, and read-only configured shared mounts.
- Added domain-separated source fingerprints over bundle locators, paths, bytes, executable modes, and context files; equal evaluated arms, source drift, mode confusion, and authority substitution fail before dispatch.
- Bound production provider authority to reviewed capability, configuration, runtime provenance, executable identity, and non-injected provider instances; drift and instance substitution fail before dispatch or release.
- Reject malformed output before verification or judgment, reject uncalibrated judged artifact kinds before dispatch, and isolate final-output verification from candidate workspace mutations.

## [0.2.0] - 2026-07-12

### Changed (2)

- Repositioned the project around A/B evaluation of user-defined skills and instruction bundles through agent harnesses; engineering and testing remain the first reference corpus rather than the evaluator boundary.
- Derived holdout skill cells and minimum-case gates from each selected suite instead of requiring the built-in `engineering` and `testing` identifiers.
- Generalized the isolated task context so non-engineering skill suites are not instructed as software-engineering evaluations.

## [0.1.0] - 2026-07-12

### Added (2)

- Standalone MIT-licensed `harness-evals` Python package and console commands.
- Seventeen calibrated software engineering and testing cases with objective verifiers and adversarial oracle calibration.
- Strict suite manifests, Git-bound instruction variants, isolated Claude and diagnostic Codex providers, blinded AB/BA comparison, crash-safe spend accounting, and review-sealed holdout plans.
- Repository-local `engineering` and `testing` reference bundles for exercising Git-bound comparison variants.
- CI, CodeQL, OpenSSF Scorecard, Dependabot, issue forms, contribution policy, security reporting, governance, support, and release documentation.

[Unreleased]: https://github.com/Dhi13man/harness-evals/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Dhi13man/harness-evals/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Dhi13man/harness-evals/releases/tag/v0.1.0
