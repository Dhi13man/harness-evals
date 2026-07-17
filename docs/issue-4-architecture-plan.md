# Issue 4 Architecture Plan

**Status:** Ready for implementation **Scope:** Generalize evaluator extension boundaries before 1.0 **Risk:** High because suite input, provider execution, sealed holdouts, and release authority cross trust boundaries

## Intent

Skivolve must allow non-engineering suites, independently calibrated comparators, new reviewed provider adapters, configurable instruction-bundle layouts, and output-oriented artifact evaluation without weakening source binding, blinding, isolation, spend accounting, cleanup, or release authority.

The work ships as six stacked pull requests. Each pull request owns one public or trust contract, contains independently useful commits, and passes every repository gate. Once dependent pull requests merge, rollback proceeds in reverse dependency order unless a forward compatibility change explicitly restores an earlier contract.

## Scope

### In Scope

- Explicit versioned comparator profiles with packaged and suite-local data resources.
- A data-driven comparator engine whose criteria, response contract, calibration corpus, artifact support, and certification belong to the selected profile.
- Objective-verifier-only suites that construct no comparator runtime, certification state, or comparator spend ledger.
- Installed-wheel execution of external suites without checkout-relative resource assumptions.
- Configurable bundle-source and shared-verifier paths with schema-v2 compatibility defaults.
- Suite-owned holdout comparison selection and generic sealed per-variant source bindings.
- Reviewed provider capability declarations and capability-bound runtime/holdout evidence.
- Workspace-diff, final-text, and strict final-JSON artifact contracts.
- Schema, migration, security, package, unit, calibration, and adversarial evidence for every contract.

### Out of Scope

- Automatic provider plugin discovery or arbitrary manifest-supplied Python imports.
- User-declared production authority, release authority, or trust elevation.
- Arbitrary binary/media artifact plugins, multiple artifacts per arm, or suite-supplied executable comparator engines.
- Changing the three-repetition AB/BA protocol, one-shot holdout policy, spend semantics, or Linux isolation model.
- Claiming that the bundled software comparator is valid for another domain without independent calibration and certification.

## Current Constraints

- `skivolve/comparator_runtime.py` resolves one software-engineering calibration bundle from package-directory assumptions.
- `skivolve/runner.py` also resolves comparator resources relative to the suite checkout and binds frozen-original authority to the comparator release.
- `skivolve/manifest.py` and `skivolve/runner.py` branch on `claude`, `codex`, and `fake` provider kinds.
- Source material assumes `skills/<id>/SKILL.md`; shared verifier material assumes `cases/testing/_shared`.
- Holdout plan schema v2 seals candidate/original names instead of generic variant source bindings.
- Comparator inputs and verifier execution primarily assume a workspace-diff artifact.

## Dependency Order

```mermaid
flowchart LR
    P1[PR 1<br/>Profile resources] --> P2[PR 2<br/>Comparator engine]
    P2 --> P3[PR 3<br/>Suite layout]
    P3 --> P4[PR 4<br/>Holdout authority]
    P4 --> P5[PR 5<br/>Provider capabilities]
    P5 --> P6[PR 6<br/>Artifact contracts]
```

PR 1 establishes package-safe profile identity and comparator absence. PR 2 makes trusted comparison semantics profile-owned and proves a non-engineering profile. PR 3 removes layout assumptions before generic source bindings are sealed. PR 4 replaces name-specific holdout authority before provider authority is generalized. PR 5 seals reviewed provider capabilities in holdout schema v4. PR 6 consumes the generic profile and provider contracts for explicit output forms.

## Pull Request 1: Comparator Profile Resources

**Branch:** `feat/issue-4-comparator-profiles` **Base:** `main` **Estimate:** best 8h, likely 14h, worst 24h, PERT 14.7h **Rollback:** before dependent PRs merge, downgrade only representable built-in-software judged suites to schema v2, verify them with the prior release, and preserve but disable schema-v3 objective-only or suite-local-profile suites that v2 cannot represent before reverting. After dependent PRs merge, revert the stack in reverse order.

### Deliverable Commits

1. `docs(architecture): plan issue 4 delivery`
2. `feat(comparator): define versioned profile resources` with focused descriptor, release-binding, package-resource, and drift tests plus regenerated production/test release locks.
3. `feat(manifest): select packaged comparator profiles` introduces suite schema v3 with focused compatibility, suite-local data-only, containment, lifecycle, and objective-only tests plus regenerated release locks.
4. `test(package): execute external suite from wheel` as independent installed-distribution coverage.
5. `docs(comparator): document profile selection and objective-only suites`.

### Exit Criteria

- Existing schema-v2 manifests preserve exact raw bytes/hash and select the current profile by compatibility default; suite schema v3 explicitly selects `judged` or `objective_only` mode and a profile for judged execution.
- Explicit built-in selection and the compatibility default produce identical runtime evidence.
- Built-in profiles resolve through a code-owned `importlib.resources` registry; suite-local profiles are contained non-symlink data directories and never import suite code. A separate immutable code-owned authority registry binds exact profile, release, and certification digests; unregistered suite-local profiles are diagnostic-only regardless of self-consistent hashes or certification claims.
- A positive suite-local data profile loads with every resource bound by descriptor and release hashes before dispatch.
- Unknown profile IDs, unknown fields, path traversal, symlinks, descriptor drift, release mismatch, and suite shadowing fail before provider construction or dispatch.
- Suite schema v3 requires a comparator and profile for `judged` mode and forbids both for `objective_only` mode; inconsistent selection fails before provider construction, certification validation, output creation, or spend-ledger creation.
- Objective-only routing selects the sole verifier-passing arm and records equal verifier outcomes as ties/inconclusive without constructing or dispatching a comparator; result evidence and, once PR 4 enables authority, release evidence identify that acceptance basis explicitly.
- Until PR 4 moves source authority out of the comparator release, objective-only execution is diagnostic and cannot prepare or consume a production holdout or authorize a release; preflight rejects that path without loading comparator state. A no-comparator diagnostic smoke proves the boundary in PR 1.
- A wheel and sdist contain complete profile resources; a clean venv installs the wheel, creates a suite outside checkout and site-packages, executes the installed CLI, and asserts profile provenance in preflight.
- Comparator, runner, package, calibration, source-binding, spend, blinding, and cleanup tests pass.
- The known-good production-sandbox smoke runs in this PR because comparator loading is a trust-boundary change.

## Pull Request 2: Data-Driven Comparator Engine

**Branch:** `feat/issue-4-comparator-engine` **Base:** `feat/issue-4-comparator-profiles` **Estimate:** best 14h, likely 26h, worst 46h, PERT 27.3h **Rollback:** before later PRs merge, preserve evidence and disable non-engineering suites whose domain the bundled software profile does not cover; migrate only suites whose domain remains covered by that profile, verify them, and revert. After later PRs merge, revert the stack in reverse order.

### Deliverable Commits

1. `refactor(comparator): consume profile-owned contracts` with focused criteria, response-schema, request, artifact-support, acceptance-policy, and release-lock tests plus regenerated production/test locks.
2. `feat(comparator): calibrate non-engineering profile` with a minimal data-only non-engineering rubric, corpus, certification fixture, positive execution, and adversarial profile-substitution tests.
3. `docs(comparator): document profile calibration contracts`.

### Exit Criteria

- A profile descriptor owns its criteria, response schema, request contract, calibration corpus, supported artifact kinds, acceptance policy, certification contract, and versioned digests.
- The trusted packaged engine interprets profile data without importing suite code or accepting a suite-supplied executable engine.
- The bundled software profile preserves the v0.2 rubric, request/response schemas, corpus, acceptance policy, and golden canonical request/evidence bytes while declaring only `workspace_diff` support; engine and release-lock digests version normally when pinned implementation sources change.
- A minimal non-engineering suite runs judged comparison through an author-authored packaged fixture profile registered for test authority without inheriting software-change criteria or making an independent production-calibration claim; an equivalent unregistered suite-local profile remains diagnostic-only.
- Unknown criteria, unsupported artifact kinds, response-schema drift, corpus drift, certification drift, and release/profile substitution fail before comparator dispatch.
- The known-good production-sandbox smoke and profile calibration suite pass in this PR.

## Pull Request 3: Configurable Suite Layout

**Branch:** `feat/issue-4-suite-layout` **Base:** `feat/issue-4-comparator-engine` **Estimate:** best 5h, likely 9h, worst 16h, PERT 9.5h **Rollback:** migrate explicit layout manifests to suite schema v3 default paths and verify them with the prior release before reverting; after dependent PRs merge, revert in reverse order.

### Deliverable Commits

1. `feat(suites): configure bundle source paths` introduces suite schema v4 with focused schema, Git-ref/worktree parity, containment, symlink, dirty-source, v2/v3-default, and bidirectional schema/parser mutation tests.
2. `feat(verifiers): configure shared resource paths` with focused snapshot, read-only mount, hash, null-root, and drift tests.
3. `test(suites): exercise external layout compatibility` as independent cross-contract coverage.
4. `docs(suites): document schema v4 layout migration`.

### Exit Criteria

- Suite schema v4 requires `cases[].bundle_source` and suite-level `shared_verifier_dir`; schema v2 and schema v3 remain accepted and derive `skills/<skill>` plus optional `cases/testing/_shared` without changing raw manifest bytes/hash.
- `suite.schema.json` validates every supported version while the parser enforces version-specific required and forbidden fields.
- Git-ref and worktree variants support a canonical contained nonstandard bundle root with a required `SKILL.md` entrypoint.
- Configured or null shared-verifier paths retain immutable snapshotting, read-only mounts, hash binding, and drift rejection.
- Traversal, symlinks, missing entrypoints, dirty sources, and shared-resource drift fail before provider dispatch.
- Existing schema-v2 and schema-v3 suites execute unchanged.

## Pull Request 4: Suite-Owned Holdout Authority

**Branch:** `feat/issue-4-holdout-authority` **Base:** `feat/issue-4-suite-layout` **Estimate:** best 14h, likely 27h, worst 50h, PERT 28.7h **Rollback:** disable only unconsumed schema-v3 plans, migrate the suite to legacy canonical comparison/source shape, verify it, revert this PR, and prepare a new schema-v2 plan. Consumed v3 plans and records remain immutable. Revert dependent PRs first.

### Deliverable Commits

1. `feat(holdout): declare release comparisons` introduces suite schema v5 with focused arbitrary-ID, selection, matrix, legacy-representability, and bidirectional schema/parser mutation tests.
2. `feat(holdout): seal generic source bindings` with focused holdout schema-v3, exact-set, empty-source, ordering, fingerprint-component, drift, plan-byte, one-shot, and bidirectional schema/parser mutation tests.
3. `refactor(comparator): separate source and judgment authority` with focused mode-specific evidence, legacy-adapter, historical-plan-byte, release-lock, calibration, and source-authority regression tests plus regenerated production/test locks.
4. `test(holdout): exercise generic authority adversaries` as independent cross-contract coverage.
5. `docs(holdout): document source authority migration`.

### Exit Criteria

- Suite schema v5 uses `holdout.comparison_ids` to select release comparisons without reserved comparison or variant identifiers; suite schema v2-v4 retain their original release-selection semantics.
- Holdout-plan schema v3 seals deterministic `source_bindings[]` records containing `variant_id`, `kind`, nullable `source_commit`, and `source_sha256_by_case`. Variant bindings and per-binding case keys have exact set equality with the selected variants and cases; duplicate, extra, missing, or noncanonical ordering fails before dispatch.
- `without_skill` uses a null commit and one canonical empty-source digest; every selected control/treatment pair must have unequal per-case source fingerprints.
- Each source fingerprint hashes a versioned, domain-separated canonical preimage containing the normalized bundle locator, sorted relative tree paths, file bytes, executable-mode bits, and every declared context-file path and bytes identically for Git-ref and worktree variants. Different commits with identical evaluated preimages are rejected as non-independent arms; mutation of any preimage component fails closed.
- Schema-v2 plans remain readable only through a code-owned, release-bound legacy authority adapter that preserves their exact candidate/original comparison, variant, provider, frozen-original, and historical plan-byte semantics. They are rejected for generalized manifests that v2 cannot represent. Only new plans emit schema v3.
- Judged schema-v3 plans require and seal comparator profile/release/certification evidence. Objective-only plans forbid every comparator field and instead seal the verifier acceptance-policy version and digest; production objective-only authority becomes available only here. Mode confusion or evidence substitution fails before consumption or dispatch.
- Manifest bytes, plan bytes, external mode-0600 storage, one-shot consumption, applicable judgment evidence, and source-drift gates remain fail-closed.
- Holdout preparation, binding, one-shot consumption, aggregate completeness, the existing judged production-sandbox smoke, and a no-comparator objective-only production-sandbox smoke pass before this PR merges.

## Pull Request 5: Provider Capabilities

**Branch:** `feat/issue-4-provider-capabilities` **Base:** `feat/issue-4-holdout-authority` **Estimate:** best 16h, likely 30h, worst 54h, PERT 31.7h **Rollback:** disable only unconsumed schema-v4 plans, migrate to reviewed built-in v3 provider semantics, verify the suite, revert this PR, and prepare a new schema-v3 plan. Consumed v4 plans and records remain immutable. Revert dependent PRs first.

### Deliverable Commits

1. `refactor(providers): add reviewed capability registry` introduces suite schema v6 with focused canonical declaration, registry, role, schema-v2-v5 compatibility, parser/schema parity, and built-in matrix tests while retaining the legacy production gate.
2. `feat(providers): bind capability-driven execution authority` atomically switches runner execution and holdout-plan schema v4 sealing with focused scheduling, billing, provenance, drift, authority, result-binding, and legacy-v3 tests.
3. `test(providers): exercise capability authority adversaries` as independent cross-contract coverage.
4. `docs(providers): document adapter and holdout migration`.

### Exit Criteria

- Suite schema v6 selects an adapter ID through the reviewed registry without a provider-kind enum; suite schema v2-v5 retain their original built-in provider syntax and semantics.
- Adapter-owned reviewed capability declarations include a monotonic contract revision, generation/comparison roles, concurrency, a closed billing contract, provenance contract, artifact-output support, and canonical capability digest without core kind branches.
- Production trust is a separate immutable code-owned authority registry. Eligibility requires the default reviewed registration and digest, registry-built non-injected instance, exact config/capability/revision/runtime binding, sealed schema-v4 binding, and live comparator certification.
- Suite fields, custom registries, injected instances, provider results, and custom adapters are structurally unable to elevate authority; they remain diagnostic or test-only.
- Claude remains production-eligible, Codex remains serialized diagnostic generation-only, and Fake remains test-only.
- Holdout-plan schema v4 seals adapter ID, role-specific authority contract, capability/revision digest, normalized non-secret config digest, runtime provenance digest, and whole provider binding. Runtime provenance includes privacy-safe account, organization/project, endpoint, credential-source revision, and billing/quota identity without secret material. Schema-v3 plans retain their original semantics and must be re-prepared for v4 production authority.
- Binding capture occurs at construction and is revalidated at preflight, before every dispatch, after every result, and before aggregation/release, including changes to account, endpoint, credential source, and billing/quota identity; executable descriptor and cleanup-poison protections remain active.
- Billing basis, required/forbidden cost evidence, budget mechanism, quota evidence, reservation, reconciliation, and unknown-charge behavior form a closed contract that cannot turn provider entry into zero cost.
- Aggregation consumes immutable authority evidence containing bound digests and explicit predicates, never a caller boolean or provider-name inference.
- A new diagnostic adapter executes through the generic registry without changes to core aggregation or holdout logic.
- The provider production-sandbox smoke and adversarial capability/authority matrix pass before this PR merges.

## Pull Request 6: Artifact Contracts

**Branch:** `feat/issue-4-artifact-contracts` **Base:** `feat/issue-4-provider-capabilities` **Estimate:** best 9h, likely 18h, worst 34h, PERT 19.2h **Rollback:** downgrade only cases already using `workspace_diff` to suite schema v6, verify them with the prior release, and preserve but disable unconsumed final-output suites whose contract cannot be represented. Preserve consumed records and require a forward fix for those suites rather than semantic substitution; revert no earlier PR while this PR remains merged.

### Deliverable Commits

1. `feat(artifacts): normalize declared case outputs` introduces suite schema v7, compatibility defaults, bounded LF text and RFC 8785 JSON normalization, capability revision 2, case-fingerprint binding, and focused contract tests.
2. `feat(verifiers): mount normalized artifacts read-only` delivers canonical artifacts through a separate read-only mount and gives final-output cases a pristine read-only fixture workspace.
3. `feat(artifacts): gate declared output compatibility` rejects unsupported profile kinds before dispatch and malformed provider output before verification or judgment.
4. `docs(artifacts): document output evaluation contracts` records the migration and the calibrated-comparator boundary.

### Exit Criteria

- Suite schema v7 requires one `artifact_contract` per case; suite schema v2-v6 cases retain the `workspace_diff` compatibility default without changing raw bytes, manifest hashes, or historical case fingerprints.
- `workspace_diff`, `final_output_text`, and `final_output_json` are the only accepted kinds, one selected artifact exists per successful arm, and fixtures remain required. The provider's extracted semantic string and normalized content are each capped at 1 MiB; transport-envelope limits remain adapter-owned.
- Text requires strict UTF-8 without BOM, normalizes CRLF/CR to LF, performs no Unicode normalization, and preserves trailing whitespace and terminal-newline presence. JSON requires strict UTF-8 without BOM and RFC 8785 canonicalization; parsing rejects duplicate keys, non-finite numbers, trailing prose, fenced extraction, depth over 64, more than 10,000 aggregate members, strings over 256 KiB, and number tokens over 128 bytes before canonical serialization.
- Normalized artifacts use fixed media types, canonical content bytes, `artifact.txt` or `artifact.json`, byte count, canonicalization contract/version, and SHA-256.
- Verifiers receive the artifact through a read-only mount with `EVAL_ARTIFACT_PATH`, `EVAL_ARTIFACT_KIND`, and `EVAL_ARTIFACT_SHA256`; the candidate cannot precreate, replace, or mutate it. `workspace_diff` verifiers retain a disposable candidate-workspace copy, while final-output verifiers receive only a pristine read-only fixture workspace and cannot address or observe the candidate-mutated tree.
- Final output becomes the selected artifact only when the case explicitly opts into a final-output contract; it cannot enter judged comparison until a calibrated profile supports that kind.
- Profiles declare `supported_artifact_kinds`; unsupported profile/artifact combinations fail before provider or comparator dispatch. Both bundled calibrated profiles remain `workspace_diff`-only. Objective suites support final text and JSON now; judged final outputs require a future separately calibrated profile and request adapter.
- Profile incompatibility, artifact mutation, oversized output, canonical-content drift, and holdout fingerprint drift fail before judgment or release authorization.
- Artifact-specific verifier isolation, malformed-output failure timing, both calibration tracks, and the complete stack pass before this PR merges.

## Top Risks

1. **Authority confusion:** Moving source or provider policy into suite-controlled fields could let untrusted input self-authorize. Mitigation: suite fields select reviewed IDs and contained data only; production authority remains code-owned, runtime-bound, and sealed.
2. **Compatibility ambiguity:** Silent migration could reinterpret schema-v2 manifests or consumed plans. Mitigation: preserve manifest bytes and explicit v2 defaults, version new sealed-plan contracts, and never rewrite consumed artifacts.
3. **Correlated evidence:** New abstraction tests could validate only their own model. Mitigation: retain existing production smoke paths, add adversarial mutation cases, install-wheel execution, source-drift checks, and independent post-implementation review.

## Global Verification Gate

Every commit must leave its branch runnable. Every PR must pass:

- Behavior and contract tests ship in the same commit as the behavior they prove; later `test(...)` commits add only independent cross-contract or distribution evidence.
- Every commit that changes release-pinned code or data enumerates every affected registered profile, regenerates its production/test lock pair, reproduces each lock independently, verifies the changed source or resource is covered, and rejects missing, orphaned, or duplicate profile locks. The v0.2 compatibility profile retains `skivolve/comparator_calibration/release.json` and `skivolve/comparator_calibration/tests/test-release.json` as migration inputs until their documented successor paths ship.

- `ruff check .`
- `ruff format --check .`
- `python -m compileall -q skivolve cases tests`
- `python -m unittest discover -s tests -v`
- `python -m unittest discover -s skivolve/comparator_calibration/tests -v`
- `python -m unittest discover -s cases/testing/tests -v`
- JSON duplicate-key and schema validation.
- Prettier with `--prose-wrap never` and markdownlint on every changed Markdown file.
- Wheel and sdist content inspection plus a clean-venv installed-wheel smoke that builds and executes a suite outside the checkout and site-packages whenever package resources or discovery change.
- The known-good production-sandbox smoke in every PR that changes comparator loading, comparison semantics, source authority, provider authority, artifact isolation, or release authorization; both case calibration tracks whenever their contract changes; and the complete-stack smoke in PR 6.
- Independent code, test, architecture, and security review with every Blocker/High finding resolved.
- Reverse-order rollback rehearsal for any stacked contract that cannot be reverted independently, with immutable consumed plans and result records preserved.

## Completion

Issue #4 closes only after all six PRs are merged in order, their workflows pass, the compatibility documentation is published, and the issue links each accepted PR. An open or failing stacked PR is incomplete work, not a partial close.
