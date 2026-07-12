# Changelog

All notable changes to Harness Evals are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and package releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-12

### Changed

- Repositioned the project around A/B evaluation of user-defined skills and instruction bundles through agent harnesses; engineering and testing remain the first reference corpus rather than the evaluator boundary.
- Derived holdout skill cells and minimum-case gates from each selected suite instead of requiring the built-in `engineering` and `testing` identifiers.
- Generalized the isolated task context so non-engineering skill suites are not instructed as software-engineering evaluations.

## [0.1.0] - 2026-07-12

### Added

- Standalone MIT-licensed `harness-evals` Python package and console commands.
- Seventeen calibrated software engineering and testing cases with objective verifiers and adversarial oracle calibration.
- Strict suite manifests, Git-bound instruction variants, isolated Claude and diagnostic Codex providers, blinded AB/BA comparison, crash-safe spend accounting, and review-sealed holdout plans.
- Repository-local `engineering` and `testing` reference bundles for exercising Git-bound comparison variants.
- CI, CodeQL, OpenSSF Scorecard, Dependabot, issue forms, contribution policy, security reporting, governance, support, and release documentation.

[Unreleased]: https://github.com/Dhi13man/harness-evals/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Dhi13man/harness-evals/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Dhi13man/harness-evals/releases/tag/v0.1.0
