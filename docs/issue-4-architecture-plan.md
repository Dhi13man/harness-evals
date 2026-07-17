# Issue 4 Architecture Record

**Status:** Completed on 2026-07-13. [Issue #4](https://github.com/Dhi13man/skivolve/issues/4) closed after the six dependent pull requests merged in order.

**Scope:** Generalize evaluator extension boundaries before 1.0.

**Risk:** High because suite input, provider execution, sealed holdouts, and release authority cross trust boundaries.

## Outcome

Skivolve now supports non-engineering suites, profile-owned comparison semantics, objective-verifier-only evaluation, configurable instruction-bundle and verifier layouts, suite-owned source authority, reviewed provider adapters, and declared output artifacts. These extensions preserve source binding, blinding, isolation, spend accounting, cleanup, and fail-closed release authority.

The work remained a sequence because each contract depends on the one before it: profiles define comparison semantics; configurable layouts define what is evaluated; holdout plans bind those sources; provider capabilities bind who executes them; artifact contracts define what execution produces.

## Delivered Contracts

| Step | Pull request | Contract |
| --- | --- | --- |
| 1 | [#5](https://github.com/Dhi13man/skivolve/pull/5) | Added packaged and suite-local comparator profiles, explicit `judged` and `objective_only` modes, and installed-wheel external-suite coverage in schema v3. |
| 2 | [#6](https://github.com/Dhi13man/skivolve/pull/6) | Moved criteria, response contracts, corpus identity, calibration policy, and artifact support into data-only profiles interpreted by a closed package engine. |
| 3 | [#7](https://github.com/Dhi13man/skivolve/pull/7) | Added canonical `bundle_source` and `shared_verifier_dir` paths with contained, hash-bound, read-only snapshots in schema v4. |
| 4 | [#8](https://github.com/Dhi13man/skivolve/pull/8) | Added ordered `holdout.comparison_ids` in suite schema v5 and exact per-variant source bindings in holdout-plan schema v3. |
| 5 | [#9](https://github.com/Dhi13man/skivolve/pull/9) | Added reviewed adapter selection in suite schema v6 and sealed provider authority bindings in holdout-plan schema v4. |
| 6 | [#10](https://github.com/Dhi13man/skivolve/pull/10) | Added bounded `workspace_diff`, `final_output_text`, and RFC 8785 `final_output_json` contracts with separate read-only artifact delivery in schema v7. |

## Durable Decisions

- Comparator profiles are data-only. Built-in profiles load through `importlib.resources`; profiles select allowlisted engine strategies and adapters but cannot introduce imports, expression languages, parsers, or production authority. Built-in production authority remains code-owned; suite-local profiles remain diagnostic. `plain-language-revision-v1` demonstrates non-engineering semantics under test authority, but its author-authored corpus is not independent production calibration and cannot authorize a production claim.
- Objective-only evaluation constructs no comparator runtime, certification state, or comparator spend ledger. Production holdouts became available only after schema v5 could seal suite-owned source authority and the `verifier-pass-v1` acceptance policy.
- Bundle and shared-verifier paths are canonical and contained. Schema v4 `cases[].bundle_source` roots contain `SKILL.md`; the v2 and v3 compatibility paths retain `skills/<skill>` and legacy `cases/testing/_shared`. Symlinks, special entries, traversal, dirty source drift, and snapshot drift fail before dispatch; verifier resources are mounted read-only.
- Holdout plans bind the exact selected variants, cases, source commits, source bytes, executable modes, and declared context. Equal evaluated sources are rejected even when commit IDs differ. Plans and evidence remain owner-only mode `0600`; reviewed and consumed plans or records are immutable.
- Provider capabilities and production eligibility are separate code-owned checks. Migrating to schema v6 replaces legacy `kind` values `claude`, `codex`, and `fake` with reviewed adapter IDs `claude-cli`, `codex-app-server`, and `deterministic-fake`. Reviewed capabilities bind roles, revision, concurrency, billing or quota evidence, provenance, artifact support, and a canonical digest. Plan v4 seals non-secret configuration and runtime provenance and revalidates them before dispatch and release. The first adapter may hold production generation and comparison authority, the second remains serialized diagnostic generation, and the third remains test-only. Names, configuration, injected instances, and provider results cannot self-authorize.
- Schema v7 cases declare one `artifact_contract`: `workspace_diff`, `final_output_text`, or `final_output_json`. Schema-v2 through schema-v6 cases retain the `workspace_diff` default without changing manifest bytes, hashes, or historical case fingerprints. Raw semantic output and normalized content are each capped at 1 MiB. Text converts CRLF and CR to LF while preserving Unicode, other whitespace, and terminal-newline state. JSON depth is capped at 64, aggregate members at 10,000, strings at 256 KiB, and number tokens at 128 bytes. Canonical `artifact.txt` or `artifact.json` bytes are mounted separately and exposed through `EVAL_ARTIFACT_PATH`, `EVAL_ARTIFACT_KIND`, and `EVAL_ARTIFACT_SHA256`. Final-output verifiers receive a pristine read-only fixture workspace, so candidate workspace mutations cannot substitute for or influence the declared artifact. Judged final output remains unavailable until a comparator profile is calibrated for that artifact kind.

## Compatibility And Scope Boundaries

Schema v2 manifests retain their original bytes, hashes, field shapes, and software-profile compatibility behavior. Schemas v3 through v7 add explicit contracts rather than silently reinterpreting earlier files. Legacy holdout plans remain readable only where their source, provider, and artifact semantics are representable; new authority requires a newly reviewed plan. Release and certification identities change when locked code or resources change.

The sequence deliberately did not add automatic provider discovery, arbitrary manifest-supplied Python, suite-supplied evaluator engines, binary or multi-artifact plugins, or user-declared production authority. It also left the three-repetition AB/BA protocol, one-shot holdout consumption, spend semantics, and Linux isolation model unchanged. A comparator calibrated for one domain is not authority for another domain without separate calibration and certification.

Rollback follows the dependency order in reverse:

| From | Required rollback |
| --- | --- |
| Schema v7 | Downgrade only `workspace_diff` cases, disable unrepresentable final-output suites, and replace each unconsumed revision-2 plan v4 with a newly prepared revision-1 plan v4. |
| Schema v6 | Migrate adapter IDs to legacy provider kinds and replace each unconsumed plan-v4 authority record with a newly prepared plan v3. |
| Schema v5 | Restore the legacy comparison and source shape, then prepare a new plan v2. |
| Schema v4 | Migrate explicit layouts to schema-v3 defaults. |
| Schema v3 | Disable objective-only, suite-local-profile, or non-software-profile suites that schema v2 cannot represent. |

Reviewed or consumed plans and result records are never rewritten. Consumed final-output records require a forward fix rather than semantic substitution.

## Verification And History

Each pull request carried focused parser/schema parity, package, unit, calibration, drift, adversarial, and sandbox evidence appropriate to its trust boundary. The pull requests, commits, and CI runs retain the implementation-specific branches, command logs, release-lock changes, and review history removed from this completed record.

Current contracts and migration guidance live in the root [README](../README.md). Release history is recorded in the [changelog](../CHANGELOG.md).
