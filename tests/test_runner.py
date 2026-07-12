from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator


HARNESS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS_ROOT))

from harness_evals.comparator_runtime import canonical_sha256  # noqa: E402
import harness_evals.runner as runner_module  # noqa: E402
from harness_evals import (  # noqa: E402
    EvalRunner,
    FakeProvider,
    ManifestError,
    RunSelection,
    RunnerError,
    SuiteSpec,
    load_suite,
)
from harness_evals.providers import (  # noqa: E402
    AgentHandler,
    AgentRequest,
    ClaudeCliProvider,
    ProviderError,
    ProviderExecutionPolicy,
    ProviderResult,
    SERIALIZED_DIAGNOSTIC,
)
from harness_evals.holdout_plan import HoldoutPlanError, load_holdout_plan  # noqa: E402
from harness_evals.runner import (  # noqa: E402
    MAX_CODEX_EXECUTABLE_BYTES,
    MAX_EXECUTABLE_BYTES,
    _aggregate,
    _assert_file_size_within_limit,
    _attest_executable,
    _build_provider,
    _claim_holdout_consumption,
    _GeneratorDispatchJournalError,
    _GeneratorDispatchLedger,
    _generator_request_sha256,
    _release_case_fingerprint,
    _release_context_content_hashes,
    _snapshot_tree,
    _serialized_arm_order,
    _tree_hash,
)
from harness_evals.holdout_cli import (  # noqa: E402
    build_parser as build_prepare_parser,
    main as prepare_holdout_main,
)
from harness_evals.cli import build_parser, main as run_evals_main  # noqa: E402


_REAL_BARRIER = threading.Barrier


class _SerializedCodexTestProvider(FakeProvider):
    def __init__(
        self,
        protocol_lock: Path,
        *,
        agent_handler: AgentHandler | None = None,
    ) -> None:
        super().__init__(agent_handler=agent_handler)
        lock_bytes = protocol_lock.read_bytes()
        lock = json.loads(lock_bytes)
        self.reported_name = "codex-app-server"
        self.reported_version = lock["codex_cli_version"]
        self.reported_executable_sha256 = lock["executable_sha256"]
        self.reported_lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
        self.reported_runtime_bundle_sha256 = lock["runtime_bundle"]["sha256"]
        self.reported_schema_sha256 = lock["protocol"]["sha256"]
        self.reported_provenance = {
            "codex_cli_version": self.reported_version,
            "executable_sha256": self.reported_executable_sha256,
            "lock_sha256": self.reported_lock_sha256,
            "runtime_bundle_sha256": self.reported_runtime_bundle_sha256,
            "schema_sha256": self.reported_schema_sha256,
        }

    @property
    def name(self) -> str:
        return self.reported_name

    @property
    def version(self) -> str:
        return self.reported_version

    @property
    def execution_policy(self) -> ProviderExecutionPolicy:
        return SERIALIZED_DIAGNOSTIC

    @property
    def executable_sha256(self) -> str:
        return self.reported_executable_sha256

    @property
    def protocol_lock_sha256(self) -> str:
        return self.reported_lock_sha256

    @property
    def runtime_bundle_sha256(self) -> str:
        return self.reported_runtime_bundle_sha256

    @property
    def protocol_schema_sha256(self) -> str:
        return self.reported_schema_sha256

    @property
    def protocol_provenance(self) -> dict[str, str]:
        return dict(self.reported_provenance)

    def run_agent(self, request: AgentRequest) -> ProviderResult:
        return replace(
            super().run_agent(request),
            cost_usd=None,
            billing_basis="chatgpt_subscription",
            quota={"test_double": True},
            protocol_provenance=self.protocol_provenance,
            sandbox={
                "cleanup_confirmed": True,
                "enforced": True,
                "kind": "systemd-run-user+codex-permission-profile",
                "permission_profile": "eval",
            },
        )


class _MutablePolicyFakeProvider(FakeProvider):
    def __init__(self, policy: ProviderExecutionPolicy) -> None:
        super().__init__()
        self.policy = policy

    @property
    def execution_policy(self) -> ProviderExecutionPolicy:
        return self.policy


class _CloseSpyProvider(FakeProvider):
    def __init__(
        self,
        policy: ProviderExecutionPolicy = ProviderExecutionPolicy("concurrent", True),
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.policy = policy
        self.close_error = close_error
        self.close_calls = 0

    @property
    def execution_policy(self) -> ProviderExecutionPolicy:
        return self.policy

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _StaggeredSecondBarrier:
    """Expose an abort-after-trip race deterministically."""

    def __init__(self, parties: int) -> None:
        self._first = _REAL_BARRIER(parties)
        self._condition = threading.Condition()
        self._calls = threading.local()
        self._second_arrivals = 0
        self._release = False
        self._aborted = False

    def wait(self, timeout: float | None = None) -> int:
        calls = getattr(self._calls, "value", 0) + 1
        self._calls.value = calls
        if calls == 1:
            return self._first.wait(timeout=timeout)
        with self._condition:
            self._second_arrivals += 1
            if self._second_arrivals == 2:
                timer = threading.Timer(0.05, self._release_delayed_waiter)
                timer.daemon = True
                timer.start()
                return 1
            while not self._release and not self._aborted:
                if not self._condition.wait(timeout=timeout):
                    raise threading.BrokenBarrierError
            if self._aborted:
                raise threading.BrokenBarrierError
            return 0

    def _release_delayed_waiter(self) -> None:
        with self._condition:
            self._release = True
            self._condition.notify_all()

    def abort(self) -> None:
        with self._condition:
            self._aborted = True
            self._condition.notify_all()
        self._first.abort()


class SuiteFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repository = root / "repository"
        self.suite_root = self.repository / "eval-suite"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Harness Tests")
        self._write("skills/demo/SKILL.md", "# Demo Skill\n\nOld skill guidance.\n")
        self._write("skills/demo/references/rule.md", "# Rule\n\nOld routed rule.\n")
        for skill in ("engineering", "testing"):
            self._write(
                f"skills/{skill}/SKILL.md", f"# {skill}\n\nOld skill guidance.\n"
            )
            self._write(
                f"skills/{skill}/references/rule.md", "# Rule\n\nOld routed rule.\n"
            )
        self._git("add", "skills")
        self._git("commit", "-q", "-m", "baseline skill")
        self.baseline_commit = self._git("rev-parse", "HEAD").strip()
        self._write("skills/demo/SKILL.md", "# Demo Skill\n\nNew treatment guidance.\n")
        self._write("skills/demo/references/rule.md", "# Rule\n\nNew routed rule.\n")
        for skill in ("engineering", "testing"):
            self._write(
                f"skills/{skill}/SKILL.md",
                f"# {skill}\n\nNew treatment guidance.\n",
            )
            self._write(
                f"skills/{skill}/references/rule.md", "# Rule\n\nNew routed rule.\n"
            )
        self._git("add", "skills")
        self._git("commit", "-q", "-m", "treatment skill")
        self.treatment_commit = self._git("rev-parse", "HEAD").strip()

        self.suite_root.mkdir()
        for name in (
            "baseline-authority.json",
            "holdout-plan.schema.json",
        ):
            shutil.copy2(HARNESS_ROOT / name, self.suite_root / name)
        shutil.copytree(
            HARNESS_ROOT / "harness_evals",
            self.suite_root / "harness_evals",
        )
        test_release_path = (
            self.suite_root
            / "harness_evals/comparator_calibration/tests/test-release.json"
        )
        test_release = json.loads(test_release_path.read_text(encoding="utf-8"))
        authority_path = self.suite_root / "baseline-authority.json"
        authority_path.write_text(
            json.dumps(
                {"schema_version": 1, "original_commit": self.baseline_commit},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        calibration_root = self.suite_root / "harness_evals/comparator_calibration"
        artifacts = test_release["artifacts"]
        artifact_json = {
            "corpus_sha256": "manifest.json",
            "manifest_schema_sha256": "manifest.schema.json",
            "rubric_sha256": "rubric.json",
            "request_template_sha256": "request-template.json",
            "response_schema_sha256": "response.schema.json",
            "evidence_schema_sha256": "evidence.schema.json",
        }
        for field, name in artifact_json.items():
            artifacts[field] = canonical_sha256(
                json.loads((calibration_root / name).read_text(encoding="utf-8"))
            )
        request_template = json.loads(
            (calibration_root / "request-template.json").read_text(encoding="utf-8")
        )
        artifacts["system_prompt_sha256"] = hashlib.sha256(
            request_template["system_prompt"].encode("utf-8")
        ).hexdigest()
        holdout_schema_path = self.suite_root / "holdout-plan.schema.json"
        artifacts["holdout_plan_schema_sha256"] = canonical_sha256(
            json.loads(holdout_schema_path.read_text(encoding="utf-8"))
        )
        artifacts["holdout_plan_schema_bytes_sha256"] = hashlib.sha256(
            holdout_schema_path.read_bytes()
        ).hexdigest()
        evaluator = test_release["evaluator"]
        evaluator["source_sha256"] = hashlib.sha256(
            (calibration_root / "calibration.py").read_bytes()
        ).hexdigest()
        evaluator["collector_source_sha256"] = hashlib.sha256(
            (calibration_root / "collect.py").read_bytes()
        ).hexdigest()
        evaluator["certifier_source_sha256"] = hashlib.sha256(
            (calibration_root / "certify.py").read_bytes()
        ).hexdigest()
        runtime_adapter = test_release["runtime_adapter"]
        runtime_sources = {
            "source_sha256": "harness_evals/comparator_runtime.py",
            "harness_runner_source_sha256": "harness_evals/runner.py",
            "provider_source_sha256": "harness_evals/providers.py",
            "harness_manifest_source_sha256": "harness_evals/manifest.py",
            "harness_package_source_sha256": "harness_evals/__init__.py",
            "run_evals_source_sha256": "harness_evals/cli.py",
            "holdout_plan_source_sha256": "harness_evals/holdout_plan.py",
            "prepare_holdout_plan_source_sha256": "harness_evals/holdout_cli.py",
            "baseline_authority_source_sha256": "baseline-authority.json",
        }
        for field, name in runtime_sources.items():
            runtime_adapter[field] = hashlib.sha256(
                (self.suite_root / name).read_bytes()
            ).hexdigest()
        runtime_adapter["frozen_original_commit"] = self.baseline_commit
        test_release_path.write_text(
            json.dumps(test_release, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._write_suite("prompt.md", "Fix the fixture and verify the result.\n")
        self._write_suite("fixture/input.txt", "original\n")
        self._write_suite("verifier.py", _PASSING_VERIFIER)
        self.fake_codex = self.suite_root / "fake-codex"
        self._write_suite("fake-codex", "#!/bin/sh\nexit 0\n")
        self.fake_codex.chmod(0o755)
        self.codex_protocol_lock = self.suite_root / "codex-protocol-lock.json"
        self.codex_protocol_lock.write_text(
            json.dumps(
                {
                    "codex_cli_version": "codex-cli test-1",
                    "executable_sha256": hashlib.sha256(
                        self.fake_codex.read_bytes()
                    ).hexdigest(),
                    "models": {
                        "gpt-5.6-luna": {
                            "reasoning_efforts": [
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                            ]
                        },
                        "gpt-5.6-terra": {
                            "reasoning_efforts": [
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                                "ultra",
                            ]
                        },
                    },
                    "protocol": {
                        "bundle": "codex_app_server_protocol.v2.schemas.json",
                        "canonical_bytes": 1,
                        "canonicalization": "json-sort-keys-compact-ascii-v1",
                        "generate_argv": [
                            "app-server",
                            "generate-json-schema",
                            "--experimental",
                            "--out",
                            "{output_dir}",
                        ],
                        "sha256": "b" * 64,
                    },
                    "runtime_bundle": {
                        "canonicalization": "json-sort-keys-compact-ascii-v1",
                        "files": {
                            "bin/codex": hashlib.sha256(
                                self.fake_codex.read_bytes()
                            ).hexdigest()
                        },
                        "sha256": "c" * 64,
                    },
                    "schema_version": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.manifest = self._manifest()
        self.manifest_path = self.suite_root / "suite.json"
        self.save_manifest()

    def _write(self, relative: str, content: str) -> None:
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_suite(self, relative: str, content: str) -> None:
        path = self.suite_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                "-C",
                str(self.repository),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout

    def _manifest(self) -> dict[str, object]:
        return {
            "$schema": "../suite.schema.json",
            "schema_version": 2,
            "suite_id": "unit-suite",
            "seed": 7123,
            "repository_root": "..",
            "provider": {
                "kind": "fake",
                "model": "fake-model-v1",
                "timeout_seconds": 10,
            },
            "comparator": {
                "kind": "fake",
                "model": "fake-sonnet-v2",
                "timeout_seconds": 300,
                "max_budget_usd": 1.0,
            },
            "variants": [
                {"id": "without", "kind": "without_skill"},
                {"id": "old", "kind": "git_ref", "git_ref": self.baseline_commit},
                {
                    "id": "current",
                    "kind": "worktree",
                    "root": "..",
                    "source_ref": self.treatment_commit,
                },
                {
                    "id": "original",
                    "kind": "git_ref",
                    "git_ref": self.baseline_commit,
                },
            ],
            "comparisons": [
                {
                    "id": "without-current",
                    "control": "without",
                    "treatment": "current",
                    "repetitions": 3,
                    "comparator_order": "ab_ba",
                },
                {
                    "id": "old-current",
                    "control": "old",
                    "treatment": "current",
                    "repetitions": 3,
                    "comparator_order": "ab_ba",
                },
            ],
            "cases": [
                {
                    "id": "basic",
                    "skill": "demo",
                    "split": "train",
                    "prompt_file": "prompt.md",
                    "fixture_dir": "fixture",
                    "verifier": {
                        "argv": ["python3", "verifier.py"],
                        "timeout_seconds": 5,
                        "required_tools": [],
                    },
                    "context_files": ["skills/demo/SKILL.md"],
                    "timeout_seconds": 5,
                    "critical_expectations": ["answer-present"],
                    "comparator_contract": {
                        "requirements": [
                            {
                                "id": "answer-present",
                                "kind": "required_behavior",
                                "text": "The implementation must create a non-empty answer file in the fixture workspace.",
                            }
                        ],
                        "performance_basis": None,
                        "qualitative_bases": {},
                    },
                }
            ],
        }

    def save_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def use_v3_judged(self, profile: dict[str, str] | None = None) -> None:
        self.manifest["schema_version"] = 3
        self.manifest["evaluation_mode"] = "judged"
        self.manifest["comparator_profile"] = profile or {
            "kind": "builtin",
            "id": "software-engineering-v2.3",
        }
        self.save_manifest()

    def use_v3_objective(self) -> None:
        self.manifest["schema_version"] = 3
        self.manifest["evaluation_mode"] = "objective_only"
        self.manifest.pop("comparator", None)
        self.manifest.pop("comparator_profile", None)
        for case in self.manifest["cases"]:
            case.pop("comparator_contract", None)
        self.save_manifest()

    def use_v4_judged(self, bundle_source: str = "skills/demo") -> None:
        self.use_v3_judged()
        self.manifest["schema_version"] = 4
        self.manifest["shared_verifier_dir"] = None
        for case in self.manifest["cases"]:
            case["bundle_source"] = bundle_source
        self.save_manifest()

    def use_v4_objective(self, bundle_source: str = "skills/demo") -> None:
        self.use_v3_objective()
        self.manifest["schema_version"] = 4
        self.manifest["shared_verifier_dir"] = None
        for case in self.manifest["cases"]:
            case["bundle_source"] = bundle_source
        self.save_manifest()

    def use_v5_judged(
        self,
        comparison_ids: tuple[str, ...] = ("without-current", "old-current"),
        bundle_source: str = "skills/demo",
    ) -> None:
        self.use_v4_judged(bundle_source)
        self.manifest["schema_version"] = 5
        self.manifest["holdout"] = {"comparison_ids": list(comparison_ids)}
        self.save_manifest()

    def use_v5_objective(
        self,
        comparison_ids: tuple[str, ...] = ("without-current", "old-current"),
        bundle_source: str = "skills/demo",
    ) -> None:
        self.use_v4_objective(bundle_source)
        self.manifest["schema_version"] = 5
        self.manifest["holdout"] = {"comparison_ids": list(comparison_ids)}
        self.save_manifest()

    @staticmethod
    def _use_adapter(config: dict[str, object]) -> None:
        adapter_ids = {
            "claude": "claude-cli",
            "codex": "codex-app-server",
            "fake": "deterministic-fake",
        }
        config["adapter"] = adapter_ids[config.pop("kind")]

    def use_v6_judged(
        self,
        comparison_ids: tuple[str, ...] = ("without-current", "old-current"),
        bundle_source: str = "skills/demo",
    ) -> None:
        self.use_v5_judged(comparison_ids, bundle_source)
        self.manifest["schema_version"] = 6
        self._use_adapter(self.manifest["provider"])
        self._use_adapter(self.manifest["comparator"])
        self.save_manifest()

    def use_v6_objective(
        self,
        comparison_ids: tuple[str, ...] = ("without-current", "old-current"),
        bundle_source: str = "skills/demo",
    ) -> None:
        self.use_v5_objective(comparison_ids, bundle_source)
        self.manifest["schema_version"] = 6
        self._use_adapter(self.manifest["provider"])
        self.save_manifest()

    def isolate_basic_case(self) -> None:
        case_root = "cases/basic"
        self._write_suite(f"{case_root}/prompt.md", "Fix and verify the result.\n")
        self._write_suite(f"{case_root}/fixture/input.txt", "original\n")
        self._write_suite(
            f"{case_root}/oracle/verifier.py",
            (self.suite_root / "verifier.py").read_text(encoding="utf-8"),
        )
        case = self.manifest["cases"][0]
        case["prompt_file"] = f"{case_root}/prompt.md"
        case["fixture_dir"] = f"{case_root}/fixture"
        case["verifier"]["argv"] = [
            "python3",
            f"{case_root}/oracle/verifier.py",
        ]
        self.save_manifest()

    def create_data_profile(
        self,
        relative: str,
        *,
        profile_id: str = "suite-local-software-v2.3",
        source_directory: str = "comparator_calibration",
    ) -> Path:
        destination = self.suite_root / relative
        shutil.copytree(
            self.suite_root / "harness_evals" / source_directory,
            destination,
        )
        descriptor_path = destination / "profile.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["id"] = profile_id
        for field in ("calibration_engine", "collector", "certifier"):
            descriptor["resources"].pop(field, None)
        descriptor_bytes = (
            json.dumps(descriptor, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor_path.write_bytes(descriptor_bytes)
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        for release_path in (
            destination / "release.json",
            destination / "tests/test-release.json",
        ):
            release = json.loads(release_path.read_text(encoding="utf-8"))
            release["artifacts"]["profile_descriptor_sha256"] = descriptor_sha256
            release_path.write_text(
                json.dumps(release, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        for name in ("calibration.py", "collect.py", "certify.py"):
            (destination / name).unlink(missing_ok=True)
        return destination

    def align_builtin_profile_authority(self) -> None:
        authority_bytes = (HARNESS_ROOT / "baseline-authority.json").read_bytes()
        authority = json.loads(authority_bytes)
        (self.suite_root / "baseline-authority.json").write_bytes(authority_bytes)
        for variant in self.manifest["variants"]:
            if variant["id"] == "original":
                variant["git_ref"] = authority["original_commit"]
        self.save_manifest()

    def codex_suite(self, suite: SuiteSpec) -> SuiteSpec:
        return replace(
            suite,
            provider=replace(
                suite.provider,
                kind="codex",
                executable=str(self.fake_codex),
                model="gpt-5.6-luna",
                max_budget_usd=None,
                reasoning_effort="max",
                billing_basis="chatgpt_subscription",
                protocol_lock=self.codex_protocol_lock,
            ),
        )

    def set_verifier(self, source: str) -> None:
        self._write_suite("verifier.py", source)

    def configure_holdout(
        self, skills: tuple[str, ...] = ("engineering", "testing")
    ) -> None:
        self.manifest["provider"]["max_budget_usd"] = 1.5
        self.manifest["variants"] = [
            {"id": "no-skill", "kind": "without_skill"},
            {
                "id": "original",
                "kind": "git_ref",
                "git_ref": self.baseline_commit,
            },
            {
                "id": "candidate",
                "kind": "worktree",
                "root": "..",
                "source_ref": "HEAD",
            },
        ]
        self.manifest["comparisons"] = [
            {
                "id": "candidate-vs-original",
                "control": "original",
                "treatment": "candidate",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            },
            {
                "id": "candidate-vs-no-skill",
                "control": "no-skill",
                "treatment": "candidate",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            },
        ]
        base_case = self.manifest["cases"][0]
        cases = []
        for skill in skills:
            for index in range(8):
                case = copy.deepcopy(base_case)
                case_root = f"holdout/{skill}/{index}"
                self._write_suite(
                    f"{case_root}/prompt.md",
                    f"Fix and verify independent {skill} case {index}.\n",
                )
                self._write_suite(
                    f"{case_root}/fixture/input.txt",
                    f"independent-{skill}-{index}\n",
                )
                self._write_suite(f"{case_root}/oracle/verifier.py", _PASSING_VERIFIER)
                case["id"] = f"{skill}-{index}"
                case["skill"] = skill
                case["split"] = "holdout"
                case["prompt_file"] = f"{case_root}/prompt.md"
                case["fixture_dir"] = f"{case_root}/fixture"
                case["verifier"]["argv"] = [
                    "python3",
                    f"{case_root}/oracle/verifier.py",
                ]
                case["context_files"] = [f"skills/{skill}/SKILL.md"]
                cases.append(case)
        self.manifest["cases"] = cases
        self.save_manifest()

    def holdout_plan_payload(self) -> dict[str, object]:
        suite = load_suite(self.manifest_path)
        test_release = json.loads(
            (
                self.suite_root
                / "harness_evals/comparator_calibration/tests/test-release.json"
            ).read_text(encoding="utf-8")
        )
        case_bindings = []
        shared_root = suite.shared_verifier_dir
        shared_snapshot = (
            _snapshot_tree(shared_root, ignore_generated_caches=True)
            if shared_root is not None
            else None
        )
        for case in suite.cases:
            snapshot = _snapshot_tree(
                case.prompt_file.parent, ignore_generated_caches=True
            )
            prompt_sha256 = hashlib.sha256(
                case.prompt_file.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            fixture_sha256 = _tree_hash(case.fixture_dir, ignore_generated_caches=True)
            context_content_sha256s = _release_context_content_hashes(
                suite.repository_root,
                case,
                {
                    "candidate": self.treatment_commit,
                    "original": self.baseline_commit,
                },
            )
            case_bindings.append(
                {
                    "id": case.id,
                    "case_tree_sha256": snapshot.sha256,
                    "shared_tree_sha256": (
                        shared_snapshot.sha256 if shared_snapshot is not None else None
                    ),
                    "release_case_fingerprint": _release_case_fingerprint(
                        case,
                        prompt_sha256=prompt_sha256,
                        fixture_sha256=fixture_sha256,
                        context_content_sha256s=context_content_sha256s,
                    ),
                    "skill": case.skill,
                    "critical_expectations": list(case.critical_expectations),
                }
            )
        return {
            "schema_version": 2,
            "plan_id": "unit-holdout-v1",
            "status": "sealed",
            "manifest_sha256": suite.manifest_hash,
            "comparator_release_sha256": canonical_sha256(test_release),
            "comparator_calibration_evidence_sha256": None,
            "generator_provider": {
                "name": "deterministic-fake",
                "version": "1",
                "requested_model": suite.provider.model,
                "executable_sha256": None,
                "reasoning_effort": None,
                "billing_basis": "metered_api",
                "protocol_lock": None,
                "protocol_lock_sha256": None,
                "execution_policy": {
                    "concurrency": "concurrent",
                    "release_authoritative": True,
                },
            },
            "candidate_commit": self.treatment_commit,
            "original_commit": self.baseline_commit,
            "consumption_record_path": str(
                (self.root / "holdout-plan.json.consumption.json").resolve()
            ),
            "seed": suite.seed,
            "comparison_profile": [
                {
                    "id": comparison.id,
                    "control": comparison.control,
                    "treatment": comparison.treatment,
                    "repetitions": comparison.repetitions,
                    "comparator_order": comparison.comparator_order,
                }
                for comparison in suite.comparisons
            ],
            "cases": case_bindings,
            "provenance": {
                "assurance": "trusted-reviewed-attestation",
                "privacy_claim": "not-a-cryptographic-privacy-proof",
                "frozen_before_candidate_evaluation": True,
                "sealed_after_independent_review": True,
                "reviewed_by": ["independent-reviewer"],
                "freeze_record": "review-record:freeze:unit-holdout-v1",
                "seal_record": "review-record:seal:unit-holdout-v1",
            },
        }

    def save_holdout_plan(
        self,
        payload: dict[str, object] | None = None,
        *,
        name: str = "holdout-plan.json",
    ) -> Path:
        plan_path = self.root / name
        plan_path.write_text(
            json.dumps(payload or self.holdout_plan_payload(), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        plan_path.chmod(0o600)
        return plan_path

    def provider(
        self,
        *,
        barrier: threading.Barrier | None = None,
        comparator_always_a: bool = False,
        actual_model_by_variant: bool = False,
    ) -> FakeProvider:
        def agent(request):
            if barrier is not None:
                barrier.wait(timeout=3)
            if request.skill_snapshot is None:
                flavor = "baseline result"
            else:
                skill = (request.skill_snapshot / "SKILL.md").read_text(
                    encoding="utf-8"
                )
                rule = (request.skill_snapshot / "references/rule.md").read_text(
                    encoding="utf-8"
                )
                flavor = (
                    "improved result"
                    if "New treatment" in skill
                    else "historical result"
                )
                if "New routed rule" in rule:
                    flavor += " with new route"
            (request.workspace / "answer.txt").write_text(
                flavor + "\n", encoding="utf-8"
            )
            models = (
                [request.model, f"model-{request.variant_id}"]
                if actual_model_by_variant
                else [request.model]
            )
            return {
                "final_output": flavor,
                "actual_models": models,
                "cost_usd": 0.25,
                "tokens": {"input_tokens": 3, "output_tokens": 2},
            }

        def compare(request):
            winner = "A" if comparator_always_a or request.order == "BA" else "B"
            requirement = request.pair["contract"]["requirements"][0]

            def evidence(anchor):
                quote = requirement["text"]
                return {
                    "artifact": "contract",
                    "path": "contract/requirements/answer-present",
                    "line_start": 1,
                    "line_end": 1,
                    "quote": quote,
                    "semantic_anchor": anchor,
                    "observation": f"{quote} provides the controlled basis; {anchor} is the typed decision.",
                }

            criteria = {}
            for criterion in (
                "functional_correctness",
                "security_reliability",
                "maintainability_extensibility",
                "performance_efficiency",
                "simplicity_scope_discipline",
            ):
                criterion_winner = (
                    winner if criterion == "maintainability_extensibility" else "tie"
                )
                anchor = f"criterion:{criterion}:{criterion_winner}"
                criteria[criterion] = {
                    "winner": criterion_winner,
                    "evidence": evidence(anchor),
                }
            return {
                "structured_output": {
                    "checks": {
                        side: [
                            {
                                "requirement_id": "answer-present",
                                "status": "satisfied",
                                "evidence": evidence(
                                    "requirement:answer-present:satisfied"
                                ),
                            }
                        ]
                        for side in ("A", "B")
                    },
                    "admissibility": {
                        side: {"decision": "eligible", "violation_ids": []}
                        for side in ("A", "B")
                    },
                    "criteria": criteria,
                },
                "actual_models": ["fake-sonnet-v2.0"],
                "cost_usd": 0.1,
                "tokens": {"input_tokens": 4, "output_tokens": 1},
            }

        return FakeProvider(agent_handler=agent, comparator_handler=compare)


_PASSING_VERIFIER = """import json
import os
from pathlib import Path

workspace = Path(os.environ["EVAL_WORKSPACE"])
answer = workspace / "answer.txt"
passed = answer.is_file() and bool(answer.read_text(encoding="utf-8").strip())
print(json.dumps({
    "passed": passed,
    "assertions": [{
        "id": "answer-present",
        "passed": passed,
        "evidence": "answer.txt exists and is non-empty",
    }],
    "metrics": {"answer_bytes": answer.stat().st_size if answer.exists() else 0},
}))
"""


class GeneratorDispatchLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.provider_binding = {
            "billing_basis": "metered_api",
            "execution_policy": {
                "concurrency": "concurrent",
                "release_authoritative": True,
            },
            "name": "test-provider",
            "protocol_provenance": {"contract_sha256": "b" * 64},
            "requested_model": "test-model",
            "version": "1",
        }

    def root(self, name: str) -> Path:
        root = self.base / name
        root.mkdir(mode=0o700)
        return root

    def ledger(self, root: Path) -> _GeneratorDispatchLedger:
        ledger = _GeneratorDispatchLedger(
            result_root=root,
            suite_id="private-suite-id",
            manifest_sha256="a" * 64,
            provider_binding=self.provider_binding,
        )

        def close_after_test() -> None:
            try:
                ledger.close()
            except _GeneratorDispatchJournalError:
                pass

        self.addCleanup(close_after_test)
        return ledger

    @staticmethod
    def plan(
        ledger: _GeneratorDispatchLedger,
        *,
        case_id: str = "private-case-id",
        role: str = "control",
        request_sha256: str = "c" * 64,
    ) -> str:
        return ledger.plan_attempt(
            comparison_id="private-comparison-id",
            case_id=case_id,
            repetition=0,
            role=role,
            variant_id=f"private-{role}-variant-id",
            request_sha256=request_sha256,
        )

    def test_crash_replay_preserves_planned_and_dispatched_without_retry(self) -> None:
        for phase in ("planned", "dispatched"):
            with self.subTest(phase=phase):
                root = self.root(phase)
                ledger = self.ledger(root)
                attempt_id = self.plan(ledger)
                if phase == "dispatched":
                    ledger.mark_dispatched(attempt_id)
                ledger.close()
                resumed = self.ledger(root)
                self.assertEqual(
                    resumed.audit()["unresolved_attempts"],
                    [
                        {
                            "attempt_id": attempt_id,
                            "phase": phase,
                            "request_sha256": "c" * 64,
                        }
                    ],
                )
                with self.assertRaisesRegex(
                    _GeneratorDispatchJournalError,
                    "already has an accounted attempt",
                ):
                    self.plan(resumed, request_sha256="d" * 64)

    def test_cross_instance_race_has_one_owner_and_never_poisons_journal(self) -> None:
        root = self.root("instance-race")
        barrier = threading.Barrier(2)
        acquired = threading.Event()
        loser_done = threading.Event()
        release = threading.Event()
        outcomes: list[tuple[str, str]] = []
        outcomes_lock = threading.Lock()

        def contend(label: str) -> None:
            barrier.wait(timeout=3)
            try:
                ledger = _GeneratorDispatchLedger(
                    result_root=root,
                    suite_id="private-suite-id",
                    manifest_sha256="a" * 64,
                    provider_binding=self.provider_binding,
                )
            except _GeneratorDispatchJournalError as exc:
                with outcomes_lock:
                    outcomes.append(("loser", str(exc)))
                loser_done.set()
                return
            try:
                self.plan(ledger, case_id=f"winner-{label}")
                with outcomes_lock:
                    outcomes.append(("winner", label))
                acquired.set()
                release.wait(timeout=3)
            finally:
                ledger.close()

        threads = [
            threading.Thread(target=contend, args=(label,)) for label in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(acquired.wait(timeout=3))
        self.assertTrue(loser_done.wait(timeout=3))
        release.set()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual([kind for kind, _ in outcomes].count("winner"), 1)
        self.assertEqual([kind for kind, _ in outcomes].count("loser"), 1)
        loser_error = next(value for kind, value in outcomes if kind == "loser")
        self.assertIn("already active", loser_error)
        replayed = self.ledger(root)
        audit = replayed.audit()
        self.assertEqual(audit["attempts"], 1)
        self.assertEqual(audit["records"], 2)
        self.assertEqual(audit["states"]["planned"], 1)

    def test_replay_validates_completed_and_failed_attempts_without_raw_ids(
        self,
    ) -> None:
        root = self.root("terminals")
        ledger = self.ledger(root)
        completed = self.plan(ledger, case_id="completed-private-id")
        ledger.mark_dispatched(completed)
        ledger.mark_completed(completed, "d" * 64)
        not_observed = self.plan(ledger, case_id="not-observed-private-id")
        ledger.mark_failed(
            not_observed,
            dispatch_observed=False,
            failure_category="provider",
        )
        observed = self.plan(ledger, case_id="observed-private-id")
        ledger.mark_dispatched(observed)
        ledger.mark_failed(
            observed,
            dispatch_observed=True,
            failure_category="result_validation",
        )

        ledger.close()
        audit = self.ledger(root).audit()
        self.assertEqual(audit["states"]["completed"], 1)
        self.assertEqual(audit["states"]["failed"], 2)
        self.assertEqual(audit["unresolved_attempts"], [])
        journal = (root / "generator-dispatch.jsonl").read_text(encoding="ascii")
        for private_value in (
            "private-suite-id",
            "private-comparison-id",
            "completed-private-id",
            "not-observed-private-id",
            "observed-private-id",
        ):
            self.assertNotIn(private_value, journal)

    def test_replay_rejects_suite_manifest_provider_and_transition_tamper(self) -> None:
        root = self.root("binding")
        initial = self.ledger(root)
        initial.close()
        mismatches = (
            {"suite_id": "other-suite"},
            {"manifest_sha256": "e" * 64},
            {"provider_binding": {**self.provider_binding, "version": "2"}},
        )
        baseline = {
            "result_root": root,
            "suite_id": "private-suite-id",
            "manifest_sha256": "a" * 64,
            "provider_binding": self.provider_binding,
        }
        for mismatch in mismatches:
            with self.subTest(mismatch=tuple(mismatch)):
                with self.assertRaisesRegex(
                    _GeneratorDispatchJournalError, "header binding"
                ):
                    _GeneratorDispatchLedger(**(baseline | mismatch))

        transition_root = self.root("transition")
        transition_ledger = self.ledger(transition_root)
        transition_ledger.close()
        invalid = json.dumps(
            {"attempt_id": "f" * 32, "event": "dispatched"},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        with (transition_root / "generator-dispatch.jsonl").open("ab") as stream:
            stream.write(invalid + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        with self.assertRaisesRegex(
            _GeneratorDispatchJournalError, "transition is out of order"
        ):
            self.ledger(transition_root)

    def test_concurrent_attempts_are_lossless_and_replayable(self) -> None:
        root = self.root("concurrent")
        ledger = self.ledger(root)
        parties = 8
        barrier = threading.Barrier(parties)
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=3)
                attempt_id = self.plan(ledger, case_id=f"case-{index}")
                ledger.mark_dispatched(attempt_id)
                digest = hashlib.sha256(str(index).encode()).hexdigest()
                ledger.mark_completed(attempt_id, digest)
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(parties)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        ledger.close()
        audit = self.ledger(root).audit()
        self.assertEqual(audit["states"]["completed"], parties)
        self.assertEqual(audit["records"], 1 + 3 * parties)

    def test_journal_rejects_mode_inode_prefix_and_torn_record_tamper(self) -> None:
        root_mode = self.root("root-mode")
        root_mode_ledger = self.ledger(root_mode)
        root_mode.chmod(0o755)
        with self.assertRaises(_GeneratorDispatchJournalError):
            root_mode_ledger.audit()
        root_mode.chmod(0o700)

        mode_root = self.root("mode")
        mode_ledger = self.ledger(mode_root)
        mode_path = mode_root / "generator-dispatch.jsonl"
        mode_path.chmod(0o644)
        with self.assertRaises(_GeneratorDispatchJournalError):
            mode_ledger.audit()
        mode_path.chmod(0o600)

        inode_root = self.root("inode")
        inode_ledger = self.ledger(inode_root)
        inode_path = inode_root / "generator-dispatch.jsonl"
        content = inode_path.read_bytes()
        inode_path.replace(inode_root / "old.jsonl")
        inode_path.write_bytes(content)
        inode_path.chmod(0o600)
        with self.assertRaises(_GeneratorDispatchJournalError):
            inode_ledger.audit()

        prefix_root = self.root("prefix")
        prefix_ledger = self.ledger(prefix_root)
        prefix_path = prefix_root / "generator-dispatch.jsonl"
        with prefix_path.open("r+b") as stream:
            stream.write(b"[")
            stream.flush()
            os.fsync(stream.fileno())
        with self.assertRaises(_GeneratorDispatchJournalError):
            prefix_ledger.audit()

        torn_root = self.root("torn")
        torn_ledger = self.ledger(torn_root)
        torn_ledger.close()
        with (torn_root / "generator-dispatch.jsonl").open("ab") as stream:
            stream.write(b'{"event":"dispatched"')
            stream.flush()
            os.fsync(stream.fileno())
        with self.assertRaisesRegex(_GeneratorDispatchJournalError, "torn record"):
            self.ledger(torn_root)

    def test_journal_and_result_root_symlink_replacement_are_rejected(self) -> None:
        self.assertTrue(hasattr(os, "symlink"), "runner requires POSIX symlinks")
        journal_root = self.root("journal-symlink")
        journal_ledger = self.ledger(journal_root)
        journal = journal_root / "generator-dispatch.jsonl"
        target = journal_root / "target.jsonl"
        journal.replace(target)
        journal.symlink_to(target)
        with self.assertRaises(_GeneratorDispatchJournalError):
            journal_ledger.audit()

        root = self.root("root-symlink")
        root_ledger = self.ledger(root)
        moved = self.base / "moved-root"
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(_GeneratorDispatchJournalError):
            root_ledger.audit()

    def test_lock_path_replacement_fails_operations_and_close_without_fd_leak(
        self,
    ) -> None:
        root = self.root("lock-replacement")
        ledger = self.ledger(root)
        descriptor = ledger._lock_descriptor
        lock_path = root / "generator-dispatch.lock"
        lock_path.replace(root / "old.lock")
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)

        with self.assertRaisesRegex(
            _GeneratorDispatchJournalError, "lock lost integrity"
        ):
            ledger.audit()
        with self.assertRaisesRegex(
            _GeneratorDispatchJournalError, "lock lost integrity"
        ):
            ledger.close()
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_request_digest_binds_all_agent_request_and_logical_fields(self) -> None:
        request = AgentRequest(
            case_id="case",
            variant_id="variant",
            prompt="prompt",
            model="model",
            workspace=self.base / "workspace",
            skill_snapshot=self.base / "snapshot",
            sandbox_pair_root=self.base / "pair",
            sandbox_repository_root=self.base / "repository",
            system_context="context",
            timeout_seconds=30,
            sandbox_suite_root=self.base / "suite",
            required_tools=(("tool", "/bin/tool"),),
            on_dispatched=lambda: None,
        )
        logical = {
            "comparison_id": "comparison",
            "repetition": 1,
            "role": "control",
            "skill_snapshot_sha256": "a" * 64,
            "context_sha256": "b" * 64,
        }
        baseline = _generator_request_sha256(request, **logical)
        request_changes = {
            "case_id": "other-case",
            "variant_id": "other-variant",
            "prompt": "other-prompt",
            "model": "other-model",
            "workspace": self.base / "other-workspace",
            "skill_snapshot": None,
            "sandbox_pair_root": self.base / "other-pair",
            "sandbox_repository_root": self.base / "other-repository",
            "system_context": "other-context",
            "timeout_seconds": 31,
            "sandbox_suite_root": None,
            "required_tools": (("other-tool", "/bin/other-tool"),),
            "on_dispatched": None,
        }
        for field, value in request_changes.items():
            with self.subTest(request_field=field):
                changed = replace(request, **{field: value})
                self.assertNotEqual(
                    _generator_request_sha256(changed, **logical), baseline
                )
        logical_changes = {
            "comparison_id": "other-comparison",
            "repetition": 2,
            "role": "treatment",
            "skill_snapshot_sha256": None,
            "context_sha256": "c" * 64,
        }
        for field, value in logical_changes.items():
            with self.subTest(logical_field=field):
                changed = dict(logical)
                changed[field] = value
                self.assertNotEqual(
                    _generator_request_sha256(request, **changed), baseline
                )


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path.home() / ".cache" / "skill-eval-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = SuiteFixture(Path(self.temporary.name))

    def load(self):
        return load_suite(self.fixture.manifest_path)

    def output(self, name: str) -> Path:
        return Path(self.temporary.name) / name

    def runner(self, provider: FakeProvider | None = None) -> EvalRunner:
        selected = provider or self.fixture.provider()
        return EvalRunner(self.load(), selected, selected)

    def assert_configured_bundle_rejected_for_git_and_worktree(
        self, git_message_pattern: str, worktree_message_pattern: str
    ) -> None:
        suite = self.load()
        without = next(variant for variant in suite.variants if variant.id == "without")
        source_variants = {
            variant.id: variant
            for variant in suite.variants
            if variant.id in {"old", "current"}
        }
        base_comparison = suite.comparisons[0]
        for variant_id in ("old", "current"):
            comparison_id = f"without-{variant_id}"
            comparison = replace(
                base_comparison,
                id=comparison_id,
                control="without",
                treatment=variant_id,
            )
            selected_suite = replace(
                suite,
                variants=(without, source_variants[variant_id]),
                comparisons=(comparison,),
            )
            provider = self.fixture.provider()
            with self.subTest(variant_id=variant_id):
                expected = (
                    git_message_pattern
                    if variant_id == "old"
                    else worktree_message_pattern
                )
                with self.assertRaisesRegex(RunnerError, expected):
                    EvalRunner(selected_suite, provider).preflight(
                        RunSelection(comparison_ids=(comparison_id,))
                    )
                self.assertEqual(provider.agent_requests, [])

    def test_provider_factory_lazily_builds_codex_app_server(self) -> None:
        config = replace(self.load().provider, kind="codex")
        sentinel = object()
        constructor = Mock(return_value=sentinel)
        module = SimpleNamespace(CodexAppServerProvider=constructor)
        with patch.dict(sys.modules, {"harness_evals.codex_app_server": module}):
            self.assertIs(_build_provider(config), sentinel)
        constructor.assert_called_once_with(config)

    def test_runner_closes_only_owned_providers_once_and_rejects_reuse(self) -> None:
        suite = self.load()
        agent = _CloseSpyProvider()
        comparator = _CloseSpyProvider()
        with patch.object(
            runner_module, "_build_provider", side_effect=(agent, comparator)
        ):
            with EvalRunner(suite) as runner:
                self.assertEqual(agent.close_calls, 0)
                self.assertEqual(comparator.close_calls, 0)

        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)
        runner.close()
        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)
        with self.assertRaisesRegex(RunnerError, "runner is closed"):
            runner.preflight(RunSelection(comparison_ids=("without-current",)))

        injected = _CloseSpyProvider()
        with EvalRunner(suite, injected, injected):
            pass
        self.assertEqual(injected.close_calls, 0)

    def test_runner_closes_owned_providers_on_partial_construction_failure(
        self,
    ) -> None:
        suite = self.load()
        agent = _CloseSpyProvider()
        with (
            patch.object(
                runner_module,
                "_build_provider",
                side_effect=(agent, ProviderError("comparator construction failed")),
            ),
            self.assertRaisesRegex(ProviderError, "comparator construction failed"),
        ):
            EvalRunner(suite)
        self.assertEqual(agent.close_calls, 1)

    def test_runner_closes_both_owned_providers_on_validation_failure(self) -> None:
        suite = self.load()
        agent = _CloseSpyProvider(ProviderExecutionPolicy("serialized", False))
        comparator = _CloseSpyProvider()
        with (
            patch.object(
                runner_module, "_build_provider", side_effect=(agent, comparator)
            ),
            self.assertRaisesRegex(RunnerError, "differs from the manifest"),
        ):
            EvalRunner(suite)
        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)

    def test_runner_preserves_constructor_failure_when_cleanup_also_fails(self) -> None:
        suite = self.load()
        agent = _CloseSpyProvider(ProviderExecutionPolicy("serialized", False))
        comparator = _CloseSpyProvider(close_error=RuntimeError("cleanup failed"))
        with (
            patch.object(
                runner_module, "_build_provider", side_effect=(agent, comparator)
            ),
            self.assertRaisesRegex(RunnerError, "differs from the manifest") as caught,
        ):
            EvalRunner(suite)

        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)
        self.assertIn(
            "owned provider cleanup also failed with RunnerError",
            getattr(caught.exception, "__notes__", ()),
        )

    def test_runner_attempts_every_cleanup_before_reporting_failure(self) -> None:
        suite = self.load()
        agent = _CloseSpyProvider()
        comparator = _CloseSpyProvider(close_error=RuntimeError("cleanup failed"))
        with patch.object(
            runner_module, "_build_provider", side_effect=(agent, comparator)
        ):
            runner = EvalRunner(suite)

        with self.assertRaisesRegex(RunnerError, "failed to close") as caught:
            runner.close()
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)
        runner.close()
        self.assertEqual(agent.close_calls, 1)
        self.assertEqual(comparator.close_calls, 1)

    def test_runner_deduplicates_owned_provider_cleanup(self) -> None:
        suite = self.load()
        shared = _CloseSpyProvider()
        with patch.object(
            runner_module, "_build_provider", side_effect=(shared, shared)
        ):
            runner = EvalRunner(suite)
        runner.close()
        self.assertEqual(shared.close_calls, 1)

    def test_run_cli_closes_manifest_built_runner(self) -> None:
        runner = self.runner()
        with (
            patch("harness_evals.cli.EvalRunner", return_value=runner),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            exit_code = run_evals_main(
                [
                    "--suite",
                    str(self.fixture.manifest_path),
                    "--comparison",
                    "without-current",
                    "--dry-run",
                ]
            )
        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertTrue(runner._closed)

    def test_codex_executable_cap_does_not_expand_verifier_tool_cap(self) -> None:
        self.assertEqual(MAX_EXECUTABLE_BYTES, 256 * 1024 * 1024)
        self.assertEqual(MAX_CODEX_EXECUTABLE_BYTES, 512 * 1024 * 1024)
        _assert_file_size_within_limit(
            MAX_CODEX_EXECUTABLE_BYTES,
            MAX_CODEX_EXECUTABLE_BYTES,
            "Codex executable",
        )
        with self.assertRaisesRegex(RunnerError, "exceeds its size limit"):
            _assert_file_size_within_limit(
                MAX_CODEX_EXECUTABLE_BYTES + 1,
                MAX_CODEX_EXECUTABLE_BYTES,
                "Codex executable",
            )

    def test_generator_policy_must_match_manifest_kind_and_remain_stable(self) -> None:
        suite = self.load()

        class ExtraPolicyProvider(FakeProvider):
            @property
            def execution_policy(self) -> dict[str, object]:
                return {
                    "concurrency": "concurrent",
                    "release_authoritative": True,
                    "unreviewed_override": True,
                }

        with self.assertRaisesRegex(RunnerError, "fields are not exact"):
            EvalRunner(suite, ExtraPolicyProvider(), self.fixture.provider())
        with self.assertRaisesRegex(RunnerError, "differs from the manifest"):
            EvalRunner(
                suite,
                _MutablePolicyFakeProvider(SERIALIZED_DIAGNOSTIC),
                self.fixture.provider(),
            )

        provider = _MutablePolicyFakeProvider(
            ProviderExecutionPolicy("concurrent", True)
        )
        runner = EvalRunner(suite, provider, self.fixture.provider())
        provider.policy = SERIALIZED_DIAGNOSTIC
        with self.assertRaisesRegex(RunnerError, "drifted after initialization"):
            runner.preflight(RunSelection(comparison_ids=("without-current",)))

        replacement_runner = EvalRunner(
            suite,
            self.fixture.provider(),
            self.fixture.provider(),
        )
        replacement_runner.agent_provider = self.fixture.provider()
        with self.assertRaisesRegex(RunnerError, "provider instance drifted"):
            replacement_runner.preflight(
                RunSelection(comparison_ids=("without-current",))
            )

    def test_agent_result_is_bound_to_request_provider_billing_and_sandbox(
        self,
    ) -> None:
        provider = self.fixture.provider()
        runner = self.runner(provider)
        request = AgentRequest(
            case_id="basic",
            variant_id="without",
            prompt="prompt",
            model="fake-model-v1",
            workspace=self.fixture.suite_root / "fixture",
            skill_snapshot=None,
            sandbox_pair_root=self.fixture.suite_root,
            sandbox_repository_root=self.fixture.repository,
            system_context="context",
            timeout_seconds=10,
        )
        valid = ProviderResult(
            final_output="done",
            requested_model=request.model,
            actual_models=(request.model,),
            provider_name=provider.name,
            provider_version=provider.version,
            duration_seconds=0.1,
            cost_usd=0.1,
            tokens={"input_tokens": 1},
            sandbox={"enforced": True, "kind": "fake"},
            raw_response={"result": "done"},
        )
        self.assertEqual(runner._agent_result_json(valid, request)["cost_usd"], 0.1)

        mutations = (
            replace(
                valid,
                requested_model="wrong-model",
                actual_models=("wrong-model",),
            ),
            replace(valid, actual_models=("resolved-model-version",)),
            replace(valid, actual_models=(request.model, "extra-model")),
            replace(valid, provider_name="wrong-provider"),
            replace(valid, provider_version="wrong-version"),
            replace(
                valid,
                billing_basis="chatgpt_subscription",
                cost_usd=None,
                quota={"remaining": 1},
                protocol_provenance={"lock_sha256": "a" * 64},
            ),
            replace(valid, protocol_provenance={"lock_sha256": "b" * 64}),
            replace(
                valid,
                sandbox={"enforced": True, "kind": "systemd-run-user"},
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(RunnerError):
                    runner._agent_result_json(mutation, request)

    def test_codex_result_requires_exposed_protocol_and_cleanup_binding(self) -> None:
        suite = self.fixture.codex_suite(self.load())
        provider = _SerializedCodexTestProvider(self.fixture.codex_protocol_lock)
        runner = EvalRunner(suite, provider, self.fixture.provider())
        request = AgentRequest(
            case_id="basic",
            variant_id="without",
            prompt="prompt",
            model=suite.provider.model,
            workspace=self.fixture.suite_root / "fixture",
            skill_snapshot=None,
            sandbox_pair_root=self.fixture.suite_root,
            sandbox_repository_root=self.fixture.repository,
            system_context="context",
            timeout_seconds=10,
        )
        valid = replace(
            FakeProvider().run_agent(request),
            provider_name=provider.name,
            provider_version=provider.version,
            cost_usd=None,
            billing_basis="chatgpt_subscription",
            quota={"remaining": 1},
            protocol_provenance=provider.protocol_provenance,
            sandbox={
                "cleanup_confirmed": True,
                "enforced": True,
                "kind": "systemd-run-user+codex-permission-profile",
                "permission_profile": "eval",
            },
        )
        runner._agent_result_json(valid, request)
        for sandbox in (
            {**valid.sandbox, "cleanup_confirmed": False},
            {**valid.sandbox, "permission_profile": "other"},
        ):
            with self.subTest(sandbox=sandbox):
                with self.assertRaisesRegex(RunnerError, "cleanup evidence"):
                    runner._agent_result_json(replace(valid, sandbox=sandbox), request)
        with self.assertRaisesRegex(RunnerError, "protocol provenance"):
            runner._agent_result_json(
                replace(valid, protocol_provenance={"test_double": "wrong"}),
                request,
            )

    def test_codex_provenance_is_cross_bound_to_provider_lock_and_config(self) -> None:
        suite = self.fixture.codex_suite(self.load())
        runner = EvalRunner(
            suite,
            _SerializedCodexTestProvider(self.fixture.codex_protocol_lock),
            self.fixture.provider(),
        )
        self.assertEqual(
            runner._agent_protocol_provenance["runtime_bundle_sha256"],
            "c" * 64,
        )

        def add_unknown(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_provenance["unknown"] = True

        def change_name(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_name = "other-provider"

        def change_version(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_version = "codex-cli other"

        def change_executable(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_executable_sha256 = "c" * 64

        def change_lock(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_lock_sha256 = "d" * 64
            provider.reported_provenance["lock_sha256"] = "d" * 64

        def change_schema(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_schema_sha256 = "e" * 64
            provider.reported_provenance["schema_sha256"] = "e" * 64

        def change_runtime_bundle(provider: _SerializedCodexTestProvider) -> None:
            provider.reported_runtime_bundle_sha256 = "f" * 64
            provider.reported_provenance["runtime_bundle_sha256"] = "f" * 64

        mutations = (
            add_unknown,
            change_name,
            change_version,
            change_executable,
            change_lock,
            change_schema,
            change_runtime_bundle,
        )
        for mutate in mutations:
            provider = _SerializedCodexTestProvider(self.fixture.codex_protocol_lock)
            mutate(provider)
            with self.subTest(mutation=mutate.__name__):
                with self.assertRaises(RunnerError):
                    EvalRunner(suite, provider, self.fixture.provider())

        invalid_config = replace(
            suite,
            provider=replace(suite.provider, reasoning_effort="ultra"),
        )
        with self.assertRaisesRegex(RunnerError, "model configuration"):
            EvalRunner(
                invalid_config,
                _SerializedCodexTestProvider(self.fixture.codex_protocol_lock),
                self.fixture.provider(),
            )

    def test_dry_run_validates_everything_without_agents_verifier_or_writes(
        self,
    ) -> None:
        def forbidden_agent(_request):
            raise AssertionError("dry-run invoked agent")

        def forbidden_comparator(_request):
            raise AssertionError("dry-run invoked comparator")

        provider = FakeProvider(
            agent_handler=forbidden_agent, comparator_handler=forbidden_comparator
        )
        output = self.output("must-not-exist")
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=output,
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["planned_pair_runs"], 3)
        self.assertTrue(result["protocol_locks_valid"])
        self.assertFalse(result["live_calibration_valid"])
        self.assertNotIn("profile_locks_valid", result)
        self.assertNotIn("objective_acceptance", result["preflight"])
        self.assertEqual(
            set(result["preflight"]["comparator"]),
            {
                "name",
                "version",
                "requested_model",
                "release_sha256",
                "calibration_evidence_sha256",
                "protocol_locks_valid",
                "live_calibration_valid",
                "certification",
            },
        )
        self.assertFalse(output.exists())
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_arms_are_concurrent_isolated_identical_and_fully_evidenced(self) -> None:
        barrier = threading.Barrier(2)
        observations: list[dict[str, object]] = []
        lock = threading.Lock()

        def agent(request):
            barrier.wait(timeout=3)
            snapshot_mode = None
            snapshot_text = None
            if request.skill_snapshot is not None:
                skill_path = request.skill_snapshot / "SKILL.md"
                snapshot_mode = stat.S_IMODE(skill_path.stat().st_mode)
                snapshot_text = skill_path.read_text(encoding="utf-8")
            with lock:
                observations.append(
                    {
                        "prompt": request.prompt,
                        "model": request.model,
                        "workspace": str(request.workspace),
                        "snapshot": snapshot_text,
                        "snapshot_mode": snapshot_mode,
                        "system": request.system_context,
                    }
                )
            flavor = "improved result" if snapshot_text else "baseline result"
            (request.workspace / "answer.txt").write_text(flavor, encoding="utf-8")
            return {
                "final_output": flavor,
                "cost_usd": 0.25,
                "tokens": {"input_tokens": 3, "output_tokens": 2},
            }

        provider = self.fixture.provider(barrier=None)
        provider._agent_handler = agent
        output = self.output("concurrent-result")
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)), output_dir=output
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(len(observations), 6)
        self.assertEqual(
            {item["prompt"] for item in observations},
            {"Fix the fixture and verify the result.\n"},
        )
        self.assertEqual({item["model"] for item in observations}, {"fake-model-v1"})
        self.assertEqual(len({item["workspace"] for item in observations}), 6)
        for item in observations:
            workspace_path = str(item["workspace"])
            self.assertNotIn("control", workspace_path)
            self.assertNotIn("treatment", workspace_path)
        treatment = next(item for item in observations if item["snapshot"] is not None)
        baseline = next(item for item in observations if item["snapshot"] is None)
        self.assertIn("New treatment guidance", treatment["snapshot"])
        self.assertEqual(treatment["snapshot_mode"] & stat.S_IWUSR, 0)
        self.assertIn("follow its reference routing", treatment["system"])
        self.assertNotIn("<explicit-context>", baseline["system"])
        self.assertFalse((self.fixture.suite_root / "fixture/answer.txt").exists())

        pair = result["pairs"][0]
        self.assertEqual(pair["arm_execution_mode"], "concurrent")
        self.assertIsNone(pair["arm_execution_order"])
        self.assertTrue(pair["arms_started_concurrently"])
        self.assertEqual(len(pair["comparator_trials"]), 2)
        self.assertFalse(pair["position_bias"])
        self.assertEqual(pair["final_winner"], "treatment")
        arm = pair["arms"]["treatment"]
        self.assertTrue(arm["verifier"]["ran"])
        self.assertTrue(arm["verifier"]["valid"])
        self.assertIn("answer.txt", arm["diff"])
        self.assertEqual(len(arm["hashes"]["diff_sha256"]), 64)
        self.assertAlmostEqual(result["aggregate"]["total_cost_usd"], 2.1)
        self.assertEqual(result["aggregate"]["tokens"]["input_tokens"], 42)
        spend = result["aggregate"]["comparator_spend_ledgers"]
        self.assertEqual(set(spend["by_comparison"]), {"without-current"})
        self.assertAlmostEqual(spend["total_charged_usd"], 0.6)
        self.assertEqual(spend["total_maximum_usd"], 100.0)
        self.assertEqual(
            spend["by_comparison"]["without-current"]["maximum_usd"], 100.0
        )
        dispatch = result["aggregate"]["generator_dispatch_ledger"]
        self.assertEqual(dispatch["attempts"], 6)
        self.assertEqual(dispatch["states"]["completed"], 6)
        self.assertEqual(dispatch["unresolved_attempts"], [])
        self.assertEqual(dispatch["records"], 19)
        self.assertTrue((output / "run.json").is_file())
        self.assertTrue((output / "manifest.snapshot.json").is_file())
        self.assertEqual(
            (output / "manifest.snapshot.json").read_bytes(),
            self.fixture.manifest_path.read_bytes(),
        )
        self.assertEqual(
            hashlib.sha256(
                (output / "manifest.snapshot.json").read_bytes()
            ).hexdigest(),
            result["preflight"]["manifest_sha256"],
        )
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        journal_path = output / "generator-dispatch.jsonl"
        journal_records = [
            json.loads(line)
            for line in journal_path.read_text(encoding="ascii").splitlines()
        ]
        self.assertEqual(
            journal_records[0]["provider"], result["preflight"]["provider"]
        )
        self.assertEqual(
            journal_records[0]["manifest_sha256"],
            result["preflight"]["manifest_sha256"],
        )
        self.assertEqual(journal_records[0]["result_root"], str(output.resolve()))
        journal_text = journal_path.read_text(encoding="ascii")
        for private_value in (
            "without-current",
            "basic",
            "Fix the fixture and verify the result.",
        ):
            self.assertNotIn(private_value, journal_text)
        for path in output.rglob("*"):
            expected_mode = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode, path)
        self.assertTrue(
            (output / "pairs/without-current/basic/000/treatment/diff.patch").is_file()
        )
        copied_values = arm["verifier"]["sandbox"]["copied_executables"].values()
        copied = next(
            item
            for item in copied_values
            if item["source"]["logical_name"].startswith("python")
        )
        self.assertTrue(copied["matches_preflight"])
        self.assertEqual(copied["sha256"], copied["source"]["sha256"])
        self.assertEqual(copied["stat"]["mode"], 0o500)

    def test_subscription_arms_are_serialized_counterbalanced_and_unpriced(
        self,
    ) -> None:
        observed_variants: list[str] = []
        observed_threads: list[int] = []
        runner_thread = threading.get_ident()

        def agent(request):
            observed_threads.append(threading.get_ident())
            observed_variants.append(request.variant_id)
            (request.workspace / "answer.txt").write_text(
                request.variant_id, encoding="utf-8"
            )
            return {
                "final_output": request.variant_id,
                "actual_models": [request.model],
                "cost_usd": 0.0,
                "tokens": {"input_tokens": 2, "output_tokens": 1},
            }

        suite = self.fixture.codex_suite(self.load())
        provider = _SerializedCodexTestProvider(
            self.fixture.codex_protocol_lock,
            agent_handler=agent,
        )
        result = EvalRunner(suite, provider, self.fixture.provider()).run(
            RunSelection(
                comparison_ids=("without-current",),
                seed=73,
                verifier_only=True,
            ),
            output_dir=self.output("serialized-subscription"),
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(set(observed_threads), {runner_thread})
        expected_orders = [
            list(_serialized_arm_order(73, "without-current", "basic", repetition))
            for repetition in range(3)
        ]
        self.assertEqual(
            [pair["arm_execution_order"] for pair in result["pairs"]],
            expected_orders,
        )
        self.assertEqual(expected_orders[1], list(reversed(expected_orders[0])))
        self.assertEqual(expected_orders[2], expected_orders[0])
        role_variants = {"control": "without", "treatment": "current"}
        self.assertEqual(
            observed_variants,
            [role_variants[role] for order in expected_orders for role in order],
        )
        self.assertTrue(
            all(pair["arm_execution_mode"] == "serialized" for pair in result["pairs"])
        )
        self.assertTrue(
            all(pair["arms_started_concurrently"] is None for pair in result["pairs"])
        )
        plan = result["preflight"]["plan"]
        binding = result["preflight"]["provider"]
        self.assertEqual(binding["billing_basis"], "chatgpt_subscription")
        self.assertEqual(binding["execution_policy"], SERIALIZED_DIAGNOSTIC.as_json())
        self.assertEqual(plan["planned_agent_calls"], 6)
        self.assertEqual(plan["agent_billing_basis"], "chatgpt_subscription")
        self.assertIsNone(plan["agent_per_invocation_max_usd"])
        self.assertIsNone(plan["maximum_agent_exposure_usd"])
        self.assertIsNone(plan["maximum_combined_exposure_usd"])
        self.assertIsNone(result["aggregate"]["total_cost_usd"])
        self.assertEqual(result["aggregate"]["known_total_cost_usd"], 0.0)
        self.assertEqual(result["aggregate"]["unknown_cost_invocations"], 6)

    def test_comparator_input_keeps_raw_diff_but_never_agent_final_output(self) -> None:
        def agent(request):
            (request.workspace / "answer.txt").write_text(
                "control treatment raw marker\n", encoding="utf-8"
            )
            return {
                "final_output": "DO-NOT-LEAK-FINAL-PROSE",
                "actual_models": [request.model],
            }

        provider = self.fixture.provider()
        provider._agent_handler = agent
        self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("leakage-boundary"),
        )

        self.assertEqual(len(provider.comparator_requests), 6)
        for request in provider.comparator_requests:
            serialized = request.request_bytes.decode("utf-8")
            self.assertIn("control treatment raw marker", serialized)
            self.assertNotIn("DO-NOT-LEAK-FINAL-PROSE", serialized)
            self.assertEqual(
                set(request.pair),
                {"id", "task", "contract", "base_files", "diff_a", "diff_b"},
            )

    def test_comparator_nested_mutation_after_construction_fails_closed(self) -> None:
        provider = self.fixture.provider()
        run_comparator = provider.run_comparator

        def mutate_after_validation(request):
            result = run_comparator(request)
            result.transport["request_sha256"] = "f" * 64
            return result

        provider.run_comparator = mutate_after_validation
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("mutated-comparator-result"),
        )

        self.assertFalse(result["passed"])
        self.assertEqual(len(provider.comparator_requests), 6)
        for pair in result["pairs"]:
            self.assertEqual(pair["comparator_trials"], [])
            self.assertIn("changed after validation", pair["comparator_error"])
            self.assertEqual(pair["winner_basis"], "infrastructure_error")

    def test_objective_routing_skips_comparator_for_one_pass_or_both_fail(self) -> None:
        self.fixture.set_verifier(
            """import json
import os
from pathlib import Path
text = Path(os.environ["EVAL_WORKSPACE"], "answer.txt").read_text()
passed = "improved" in text
print(json.dumps({"passed": passed, "assertions": [{"id": "answer-present", "passed": passed, "evidence": "treatment-only objective"}], "metrics": {}}))
"""
        )
        one_pass = self.fixture.provider()
        result = self.runner(one_pass).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("one-objective-pass"),
        )
        self.assertEqual(one_pass.comparator_requests, [])
        self.assertTrue(
            all(pair["final_winner"] == "treatment" for pair in result["pairs"])
        )
        self.assertTrue(
            all(
                pair["winner_basis"] == "objective_verifier" for pair in result["pairs"]
            )
        )

        self.fixture.set_verifier(
            """import json
print(json.dumps({"passed": False, "assertions": [{"id": "answer-present", "passed": False, "evidence": "neither qualifies"}], "metrics": {}}))
"""
        )
        both_fail = self.fixture.provider()
        result = self.runner(both_fail).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("both-objective-fail"),
        )
        self.assertEqual(both_fail.comparator_requests, [])
        self.assertTrue(
            all(pair["final_winner"] == "unqualified" for pair in result["pairs"])
        )

    def test_ab_ba_requires_full_normalized_decision_not_only_outcome(self) -> None:
        provider = self.fixture.provider()
        original = provider._comparator_handler

        def changed_decision(request):
            response = original(request)
            if request.order == "BA":
                decision = response["structured_output"]["criteria"][
                    "simplicity_scope_discipline"
                ]
                decision["winner"] = "A"
                decision["evidence"]["semantic_anchor"] = (
                    "criterion:simplicity_scope_discipline:A"
                )
                decision["evidence"]["observation"] = decision["evidence"][
                    "observation"
                ].replace(
                    "criterion:simplicity_scope_discipline:tie",
                    "criterion:simplicity_scope_discipline:A",
                )
            return response

        provider._comparator_handler = changed_decision
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("full-decision-order"),
        )
        self.assertFalse(result["passed"])
        self.assertTrue(all(pair["position_bias"] for pair in result["pairs"]))
        self.assertTrue(
            all(
                "full normalized decisions disagree" in pair["comparator_error"]
                for pair in result["pairs"]
            )
        )

    def test_git_ref_and_worktree_materialize_distinct_complete_skill_snapshots(
        self,
    ) -> None:
        observations: dict[str, tuple[str, str, str]] = {}

        def agent(request):
            skill = (request.skill_snapshot / "SKILL.md").read_text(encoding="utf-8")
            rule = (request.skill_snapshot / "references/rule.md").read_text(
                encoding="utf-8"
            )
            observations[request.variant_id] = (skill, rule, request.system_context)
            flavor = (
                "improved result" if "New treatment" in skill else "historical result"
            )
            (request.workspace / "answer.txt").write_text(flavor, encoding="utf-8")
            return flavor

        provider = self.fixture.provider()
        provider._agent_handler = agent
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("old-current",)),
            output_dir=self.output("ref-result"),
        )

        self.assertTrue(result["passed"])
        self.assertIn("Old skill guidance", observations["old"][0])
        self.assertIn("Old routed rule", observations["old"][1])
        self.assertIn("Old skill guidance", observations["old"][2])
        self.assertIn("isolated agent-harness evaluation", observations["old"][2])
        self.assertNotIn("software-engineering evaluation", observations["old"][2])
        self.assertIn("New treatment guidance", observations["current"][0])
        self.assertIn("New routed rule", observations["current"][1])
        pair = result["pairs"][0]
        self.assertEqual(
            pair["arms"]["control"]["source"]["source_commit"],
            self.fixture.baseline_commit,
        )
        self.assertNotEqual(
            pair["arms"]["control"]["source"]["skill_snapshot_sha256"],
            pair["arms"]["treatment"]["source"]["skill_snapshot_sha256"],
        )

    def test_configured_bundle_source_has_git_and_worktree_parity(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md",
            "# Configured Bundle\n\nConfigured guidance.\n",
        )
        self.fixture._write(
            "instruction-bundles/demo/references/rule.md",
            "# Configured Rule\n\nConfigured route.\n",
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")

        observations: dict[str, tuple[str, str]] = {}

        def agent(request):
            observations[request.variant_id] = (
                (request.skill_snapshot / "SKILL.md").read_text(encoding="utf-8"),
                (request.skill_snapshot / "references/rule.md").read_text(
                    encoding="utf-8"
                ),
            )
            (request.workspace / "answer.txt").write_text(
                "configured result", encoding="utf-8"
            )
            return "configured result"

        provider = self.fixture.provider()
        provider._agent_handler = agent
        result = EvalRunner(self.load(), provider).run(
            RunSelection(comparison_ids=("old-current",)),
            output_dir=self.output("configured-bundle"),
        )

        self.assertEqual(observations["old"], observations["current"])
        self.assertIn("Configured guidance", observations["old"][0])
        self.assertIn("Configured route", observations["old"][1])
        pair = result["pairs"][0]
        self.assertTrue(
            all(arm["error"] is None for arm in pair["arms"].values()), result
        )
        self.assertTrue(
            all(arm["verifier"]["passed"] for arm in pair["arms"].values()), result
        )
        self.assertEqual(
            pair["arms"]["control"]["source"]["skill_snapshot_sha256"],
            pair["arms"]["treatment"]["source"]["skill_snapshot_sha256"],
        )

    def test_literal_git_pathspec_bundle_has_parity_and_dirty_detection(self) -> None:
        bundle_source = ":(literal)demo"
        self.fixture._write(f"{bundle_source}/SKILL.md", "# Literal Pathspec Bundle\n")
        self.fixture._git("--literal-pathspecs", "add", "--", bundle_source)
        self.fixture._git("commit", "-q", "-m", "literal pathspec bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective(bundle_source)

        observed: dict[str, str] = {}

        def agent(request):
            observed[request.variant_id] = (
                request.skill_snapshot / "SKILL.md"
            ).read_text(encoding="utf-8")
            (request.workspace / "answer.txt").write_text(
                "literal path result", encoding="utf-8"
            )
            return "literal path result"

        provider = self.fixture.provider()
        provider._agent_handler = agent
        result = EvalRunner(self.load(), provider).run(
            RunSelection(comparison_ids=("old-current",)),
            output_dir=self.output("literal-pathspec-bundle"),
        )
        pair = result["pairs"][0]
        self.assertEqual(observed["old"], observed["current"])
        self.assertEqual(
            pair["arms"]["control"]["source"]["skill_snapshot_sha256"],
            pair["arms"]["treatment"]["source"]["skill_snapshot_sha256"],
        )

        self.fixture._write(
            f"{bundle_source}/SKILL.md",
            "# Literal Pathspec Bundle\n\nUncommitted.\n",
        )
        with self.assertRaisesRegex(RunnerError, "commit it before A/B evaluation"):
            EvalRunner(self.load(), self.fixture.provider()).preflight(
                RunSelection(comparison_ids=("without-current",))
            )

    def test_tracked_generated_caches_have_git_and_worktree_parity(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Cache-Aware Bundle\n"
        )
        cache = self.fixture.repository / "instruction-bundles/demo/__pycache__"
        cache.mkdir(parents=True)
        cache.joinpath("compiled.pyc").write_bytes(b"tracked cache")
        self.fixture._write(
            "instruction-bundles/demo/scripts/generated.pyo", "tracked cache\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle with tracked caches")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        observed: dict[str, list[str]] = {}

        def agent(request):
            observed[request.variant_id] = [
                path.relative_to(request.skill_snapshot).as_posix()
                for path in request.skill_snapshot.rglob("*")
            ]
            (request.workspace / "answer.txt").write_text(
                "cache-free result", encoding="utf-8"
            )
            return "cache-free result"

        provider = self.fixture.provider()
        provider._agent_handler = agent
        result = EvalRunner(self.load(), provider).run(
            RunSelection(comparison_ids=("old-current",)),
            output_dir=self.output("tracked-cache-bundle"),
        )

        pair = result["pairs"][0]
        self.assertEqual(
            pair["arms"]["control"]["source"]["skill_snapshot_sha256"],
            pair["arms"]["treatment"]["source"]["skill_snapshot_sha256"],
        )
        self.assertEqual(observed["old"], observed["current"])
        self.assertTrue(
            all(
                "__pycache__" not in path and not path.endswith((".pyc", ".pyo"))
                for paths in observed.values()
                for path in paths
            )
        )

    def test_legacy_git_ref_preserves_tracked_cache_snapshot_bytes(self) -> None:
        cache = self.fixture.repository / "skills/demo/__pycache__"
        cache.mkdir(parents=True)
        cache.joinpath("legacy.pyc").write_bytes(b"legacy tracked cache")
        self.fixture._git("add", "skills/demo/__pycache__/legacy.pyc")
        self.fixture._git("commit", "-q", "-m", "legacy tracked cache")
        cache_commit = self.fixture._git("rev-parse", "HEAD").strip()

        version_two = self.load()
        version_two_fingerprint = runner_module._git_source_fingerprint(
            self.fixture.repository, cache_commit, version_two.cases[0]
        )
        snapshot = self.output("legacy-cache-snapshot")
        runner_module._materialize_git_bundle(
            self.fixture.repository,
            cache_commit,
            version_two.cases[0].bundle_source,
            snapshot,
        )
        self.assertEqual(
            (snapshot / "__pycache__/legacy.pyc").read_bytes(),
            b"legacy tracked cache",
        )

        self.fixture.use_v3_judged()
        version_three = self.load()
        version_three_fingerprint = runner_module._git_source_fingerprint(
            self.fixture.repository, cache_commit, version_three.cases[0]
        )
        self.assertEqual(version_three_fingerprint, version_two_fingerprint)
        self.assertEqual(
            version_two_fingerprint,
            "bb56f030d871f9cb5af42d1fae523140be90136caba30c335736f9401852e72c",
        )

    def test_v5_source_fingerprint_binds_locator_paths_bytes_and_modes(self) -> None:
        self.fixture.use_v5_judged()
        suite = self.load()
        case = suite.cases[0]
        git_hash = runner_module._git_source_fingerprint(
            self.fixture.repository,
            self.fixture.treatment_commit,
            case,
            ignore_generated_caches=True,
            canonical=True,
        )
        worktree_hash = runner_module._worktree_source_fingerprint(
            self.fixture.repository,
            case,
            ignore_empty_directories=True,
            canonical=True,
        )
        self.assertEqual(git_hash, worktree_hash)

        alternate_bundle = self.fixture.repository / "skills/demo-copy"
        shutil.copytree(self.fixture.repository / "skills/demo", alternate_bundle)
        locator_case = replace(
            case, bundle_source=case.bundle_source.parent / "demo-copy"
        )
        locator_hash = runner_module._worktree_source_fingerprint(
            self.fixture.repository,
            locator_case,
            ignore_empty_directories=True,
            canonical=True,
        )
        self.assertNotEqual(locator_hash, worktree_hash)

        entrypoint = self.fixture.repository / "skills/demo/SKILL.md"
        entrypoint.chmod(0o755)
        try:
            executable_hash = runner_module._worktree_source_fingerprint(
                self.fixture.repository,
                case,
                ignore_empty_directories=True,
                canonical=True,
            )
        finally:
            entrypoint.chmod(0o644)
        self.assertNotEqual(executable_hash, worktree_hash)

        context_copy = self.fixture.repository / "context-copy.md"
        context_copy.write_bytes(entrypoint.read_bytes())
        context_case = replace(
            case,
            context_files=(case.bundle_source.parent.parent / "context-copy.md",),
        )
        context_path_hash = runner_module._worktree_source_fingerprint(
            self.fixture.repository,
            context_case,
            ignore_empty_directories=True,
            canonical=True,
        )
        self.assertNotEqual(context_path_hash, worktree_hash)

        context_copy.write_text("changed context\n", encoding="utf-8")
        context_bytes_hash = runner_module._worktree_source_fingerprint(
            self.fixture.repository,
            context_case,
            ignore_empty_directories=True,
            canonical=True,
        )
        self.assertNotEqual(context_bytes_hash, context_path_hash)

    def test_ab_ba_detects_position_bias_and_seed_reproduces_mapping(self) -> None:
        provider_one = self.fixture.provider(comparator_always_a=True)
        first = self.runner(provider_one).run(
            RunSelection(comparison_ids=("without-current",), seed=99),
            output_dir=self.output("bias-one"),
        )
        provider_two = self.fixture.provider(comparator_always_a=True)
        second = self.runner(provider_two).run(
            RunSelection(comparison_ids=("without-current",), seed=99),
            output_dir=self.output("bias-two"),
        )

        first_pair = first["pairs"][0]
        second_pair = second["pairs"][0]
        self.assertTrue(first_pair["position_bias"])
        self.assertEqual(first_pair["comparator_consensus"], "inconclusive")
        self.assertEqual(first_pair["final_winner"], "inconclusive")
        self.assertEqual(
            [trial["order"] for trial in first_pair["comparator_trials"]],
            [trial["order"] for trial in second_pair["comparator_trials"]],
        )
        self.assertFalse(first["aggregate"]["gates"]["order_integrity"]["passed"])
        self.assertFalse(first["passed"])

    def test_aggregate_uses_distinct_case_sign_test_not_repetitions(self) -> None:
        suite = self.load()
        cases = tuple(replace(suite.cases[0], id=f"case-{index}") for index in range(5))
        comparison = replace(suite.comparisons[0], id="candidate-vs-original")
        gated_suite = replace(suite, cases=cases, comparisons=(comparison,))
        provider = {
            "actual_models": ("fake-model-v1",),
            "cost_usd": 0.0,
            "tokens": {},
        }
        pairs: list[dict[str, object]] = []
        for case in cases:
            for repetition in range(3):
                pair_id = f"{comparison.id}/{case.id}/{repetition:03d}"
                arm = {
                    "pair_id": pair_id,
                    "status": "completed",
                    "passed": True,
                    "critical_results": {"answer-present": True},
                    "provider": provider,
                }
                pairs.append(
                    {
                        "comparison_id": comparison.id,
                        "case_id": case.id,
                        "repetition": repetition,
                        "skill": case.skill,
                        "arms": {"control": arm, "treatment": arm},
                        "final_winner": "treatment",
                        "position_bias": False,
                        "completed": True,
                        "comparator_trials": [],
                    }
                )

        aggregate = _aggregate(
            pairs,  # type: ignore[arg-type]
            gated_suite,
            (comparison,),
            RunSelection(comparison_ids=(comparison.id,)),
        )
        cell = aggregate["by_comparison_skill"][comparison.id]["demo"]
        self.assertEqual(cell["distinct_cases"], 5)
        self.assertEqual(cell["informative_cases"], 5)
        self.assertEqual(cell["one_sided_sign_test_p"], 0.03125)
        self.assertTrue(cell["developmental_signal"])

        mixed_cost_pairs = copy.deepcopy(pairs)
        first_pair = mixed_cost_pairs[0]
        first_control = first_pair["arms"]["control"]
        first_pair["arms"]["control"] = {
            **first_control,
            "provider": {**first_control["provider"], "cost_usd": None},
        }
        mixed_cost = _aggregate(
            mixed_cost_pairs,  # type: ignore[arg-type]
            gated_suite,
            (comparison,),
            RunSelection(comparison_ids=(comparison.id,)),
        )
        self.assertIsNone(mixed_cost["total_cost_usd"])
        self.assertEqual(mixed_cost["known_total_cost_usd"], 0.0)
        self.assertEqual(mixed_cost["unknown_cost_invocations"], 1)

        known_attempt = "1" * 32
        unknown_attempt = "2" * 32
        known_request = "a" * 64
        known_invocation = "b" * 64
        unknown_request = "c" * 64
        unknown_invocation = "d" * 64
        comparator_pairs = copy.deepcopy(pairs)
        comparator_pairs[0]["comparator_trials"] = [
            {
                "invocation_id": known_invocation,
                "provider": {
                    "actual_models": ("fake-sonnet-v2",),
                    "cost_usd": 0.4,
                    "tokens": {"input_tokens": 2},
                },
                "request_sha256": known_request,
                "transport": {
                    "cost_usd": 0.4,
                    "request_sha256": known_request,
                    "spend_attempt_id": known_attempt,
                },
            }
        ]
        comparator_records = {
            comparison.id: [
                {
                    "event": "reserve",
                    "attempt_id": known_attempt,
                    "invocation_id": known_invocation,
                    "request_sha256": known_request,
                    "reserved_usd": 1.0,
                },
                {
                    "event": "reconcile",
                    "attempt_id": known_attempt,
                    "charged_usd": 0.4,
                    "invocation_id": known_invocation,
                    "request_sha256": known_request,
                },
                {
                    "event": "reserve",
                    "attempt_id": unknown_attempt,
                    "invocation_id": unknown_invocation,
                    "request_sha256": unknown_request,
                    "reserved_usd": 1.0,
                },
                {
                    "event": "forfeit",
                    "attempt_id": unknown_attempt,
                    "charged_usd": 1.0,
                    "invocation_id": unknown_invocation,
                    "request_sha256": unknown_request,
                },
            ]
        }
        comparator_cost = _aggregate(
            comparator_pairs,  # type: ignore[arg-type]
            gated_suite,
            (comparison,),
            RunSelection(comparison_ids=(comparison.id,)),
            comparator_spend_records=comparator_records,
        )
        self.assertEqual(comparator_cost["known_total_cost_usd"], 0.4)
        self.assertEqual(comparator_cost["unknown_cost_invocations"], 1)
        self.assertIsNone(comparator_cost["total_cost_usd"])
        self.assertEqual(comparator_cost["tokens"]["input_tokens"], 2)

        late_reconcile = _aggregate(
            pairs,  # type: ignore[arg-type]
            gated_suite,
            (comparison,),
            RunSelection(comparison_ids=(comparison.id,)),
            comparator_spend_records={
                comparison.id: [
                    {
                        "event": "reserve",
                        "attempt_id": "3" * 32,
                        "invocation_id": "e" * 64,
                        "request_sha256": "f" * 64,
                        "reserved_usd": 1.0,
                    },
                    {
                        "event": "reconcile",
                        "attempt_id": "3" * 32,
                        "charged_usd": 0.3,
                        "invocation_id": "e" * 64,
                        "request_sha256": "f" * 64,
                    },
                ]
            },
        )
        self.assertEqual(late_reconcile["known_total_cost_usd"], 0.3)
        self.assertEqual(late_reconcile["unknown_cost_invocations"], 0)
        self.assertEqual(late_reconcile["total_cost_usd"], 0.3)

        with self.assertRaisesRegex(RunnerError, "lacks a reconciled"):
            _aggregate(
                comparator_pairs,  # type: ignore[arg-type]
                gated_suite,
                (comparison,),
                RunSelection(comparison_ids=(comparison.id,)),
                comparator_spend_records={comparison.id: []},
            )

        mismatched_records = copy.deepcopy(comparator_records)
        mismatched_records[comparison.id][1]["charged_usd"] = 0.3
        with self.assertRaisesRegex(RunnerError, "differs from spend journal"):
            _aggregate(
                comparator_pairs,  # type: ignore[arg-type]
                gated_suite,
                (comparison,),
                RunSelection(comparison_ids=(comparison.id,)),
                comparator_spend_records=mismatched_records,
            )

        terminal_binding_drift = copy.deepcopy(comparator_records)
        terminal_binding_drift[comparison.id][1]["invocation_id"] = "e" * 64
        with self.assertRaisesRegex(RunnerError, "terminal binding differs"):
            _aggregate(
                comparator_pairs,  # type: ignore[arg-type]
                gated_suite,
                (comparison,),
                RunSelection(comparison_ids=(comparison.id,)),
                comparator_spend_records=terminal_binding_drift,
            )

        trial_binding_drift = copy.deepcopy(comparator_pairs)
        trial_binding_drift[0]["comparator_trials"][0]["request_sha256"] = "e" * 64  # type: ignore[index]
        with self.assertRaisesRegex(RunnerError, "trial binding differs"):
            _aggregate(
                trial_binding_drift,  # type: ignore[arg-type]
                gated_suite,
                (comparison,),
                RunSelection(comparison_ids=(comparison.id,)),
                comparator_spend_records=comparator_records,
            )

        duplicate_trial_pairs = copy.deepcopy(comparator_pairs)
        duplicate_trial_pairs[0]["comparator_trials"].append(  # type: ignore[union-attr]
            copy.deepcopy(duplicate_trial_pairs[0]["comparator_trials"][0])  # type: ignore[index]
        )
        with self.assertRaisesRegex(RunnerError, "reused by trials"):
            _aggregate(
                duplicate_trial_pairs,  # type: ignore[arg-type]
                gated_suite,
                (comparison,),
                RunSelection(comparison_ids=(comparison.id,)),
                comparator_spend_records=comparator_records,
            )

        incomplete_smoke = _aggregate(
            pairs[:-3],  # type: ignore[arg-type]
            gated_suite,
            (comparison,),
            RunSelection(
                comparison_ids=(comparison.id,),
                verifier_only=True,
            ),
        )
        self.assertFalse(
            incomplete_smoke["gates"]["execution_matrix_integrity"]["passed"]
        )
        self.assertFalse(incomplete_smoke["passed"])

    def test_verifier_only_aggregate_cannot_authorize_release(self) -> None:
        self.fixture.configure_holdout()
        suite = self.load()
        comparisons = suite.comparisons
        cases = suite.cases
        plan_path = self.fixture.save_holdout_plan()
        holdout_plan = load_holdout_plan(plan_path)
        provider = {
            "actual_models": ["stable-model"],
            "cost_usd": 0.0,
            "tokens": {},
        }
        pairs: list[dict[str, object]] = []
        for comparison in comparisons:
            for case in cases:
                for repetition in range(3):
                    arm = {
                        "status": "completed",
                        "passed": True,
                        "critical_results": {"answer-present": True},
                        "provider": provider,
                    }
                    pairs.append(
                        {
                            "comparison_id": comparison.id,
                            "case_id": case.id,
                            "repetition": repetition,
                            "arms": {"control": arm, "treatment": arm},
                            "final_winner": "treatment",
                            "position_bias": False,
                            "completed": True,
                            "comparator_trials": [],
                        }
                    )

        judged = _aggregate(
            pairs,  # type: ignore[arg-type]
            suite,
            comparisons,
            RunSelection(
                split="holdout",
                comparison_ids=tuple(item.id for item in comparisons),
                holdout_plan=plan_path,
            ),
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        verifier_only = _aggregate(
            pairs,  # type: ignore[arg-type]
            suite,
            comparisons,
            RunSelection(
                split="holdout",
                comparison_ids=tuple(item.id for item in comparisons),
                verifier_only=True,
                holdout_plan=plan_path,
            ),
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        non_authoritative = _aggregate(
            pairs,  # type: ignore[arg-type]
            suite,
            comparisons,
            RunSelection(
                split="holdout",
                comparison_ids=tuple(item.id for item in comparisons),
                holdout_plan=plan_path,
            ),
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=False,
        )

        self.assertTrue(judged["final_release_authorized"])
        self.assertEqual(judged["passed"], judged["final_release_authorized"])
        self.assertEqual(verifier_only["execution_mode"], "verifier_only")
        self.assertFalse(verifier_only["final_release_authorized"])
        self.assertFalse(verifier_only["passed"])
        self.assertFalse(non_authoritative["final_release_authorized"])
        self.assertFalse(
            non_authoritative["gates"]["holdout_release_protocol"]["passed"]
        )
        self.assertFalse(
            non_authoritative["gates"]["holdout_release_protocol"][
                "generator_release_authoritative"
            ]
        )

    def test_missing_critical_assertion_fails_closed_after_verifier_ran(self) -> None:
        self.fixture.set_verifier(
            """import json
print(json.dumps({"passed": True, "assertions": [
    {"id": "other", "passed": True, "evidence": "wrong assertion"}
]}))
"""
        )
        result = self.runner().run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("missing-critical"),
        )

        self.assertFalse(result["passed"])
        pair = result["pairs"][0]
        self.assertFalse(pair["completed"])
        self.assertEqual(pair["comparator_trials"], [])
        for arm in pair["arms"].values():
            self.assertTrue(arm["verifier"]["ran"])
            self.assertFalse(arm["verifier"]["valid"])
            self.assertIn("omitted critical assertions", arm["error"])

    def test_timed_out_verifier_is_recorded_and_fails_closed(self) -> None:
        self.fixture.manifest["cases"][0]["verifier"]["timeout_seconds"] = 1
        self.fixture.save_manifest()
        self.fixture.set_verifier("import time\ntime.sleep(2)\n")
        result = self.runner().run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("timeout"),
        )

        self.assertFalse(result["passed"])
        for arm in result["pairs"][0]["arms"].values():
            self.assertTrue(arm["verifier"]["ran"])
            self.assertFalse(arm["verifier"]["valid"])
            self.assertIn("timed out", arm["verifier"]["error"])

    def test_verifier_runs_in_disposable_copy_and_cannot_contaminate_agent_diff(
        self,
    ) -> None:
        self.fixture.set_verifier(
            """import json
import os
from pathlib import Path
Path(os.environ["EVAL_WORKSPACE"], "verifier-created.txt").write_text("bad")
print(json.dumps({"passed": True, "assertions": [
    {"id": "answer-present", "passed": True, "evidence": "claimed"}
]}))
"""
        )
        result = self.runner().run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("mutating-verifier"),
        )

        self.assertTrue(result["passed"], result)
        for arm in result["pairs"][0]["arms"].values():
            self.assertTrue(arm["verifier"]["workspace_mutated"])
            self.assertNotIn("verifier-created.txt", arm["diff"])

    def test_configured_shared_verifier_is_snapshotted_and_mounted_read_only(
        self,
    ) -> None:
        self.fixture.isolate_basic_case()
        self.fixture._write_suite(
            "verifier-resources/shared/helper.txt", "configured shared value\n"
        )
        self.fixture._write_suite(
            "verifier-resources/shared/verifier.py",
            """import json
import os
from pathlib import Path

workspace = Path(os.environ["EVAL_WORKSPACE"])
shared = Path(os.environ["EVAL_SHARED_ROOT"])
helper = shared / "helper.txt"
answer = workspace / "answer.txt"
try:
    helper.write_text("mutated", encoding="utf-8")
    read_only = False
except OSError:
    read_only = True
passed = (
    helper.read_text(encoding="utf-8") == "configured shared value\\n"
    and answer.is_file()
    and read_only
)
print(json.dumps({
    "passed": passed,
    "assertions": [{
        "id": "answer-present",
        "passed": passed,
        "evidence": "configured shared helper was readable and immutable",
    }],
    "metrics": {},
}))
""",
        )
        self.fixture.use_v4_objective()
        self.fixture.manifest["shared_verifier_dir"] = "verifier-resources/shared"
        self.fixture.manifest["cases"][0]["verifier"]["argv"] = [
            "python3",
            "verifier-resources/shared/verifier.py",
        ]
        self.fixture.save_manifest()
        provider = self.fixture.provider()

        result = EvalRunner(self.load(), provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("configured-shared-verifier"),
        )

        self.assertTrue(
            all(
                arm["verifier"]["passed"]
                for pair in result["pairs"]
                for arm in pair["arms"].values()
            ),
            result,
        )
        for pair in result["pairs"]:
            for arm in pair["arms"].values():
                self.assertTrue(
                    arm["verifier"]["executed_argv"][1].endswith("/_shared/verifier.py")
                )
                self.assertTrue(
                    any(
                        value.startswith("BindReadOnlyPaths=")
                        and value.endswith("/_shared")
                        for value in arm["verifier"]["sandbox"]["properties"]
                    )
                )

    def test_null_shared_verifier_omits_environment_and_mount(self) -> None:
        self.fixture.isolate_basic_case()
        self.fixture._write_suite(
            "cases/testing/_shared/must-not-mount.txt", "legacy fallback\n"
        )
        self.fixture._write_suite(
            "cases/basic/oracle/verifier.py",
            """import json
import os
from pathlib import Path

answer = Path(os.environ["EVAL_WORKSPACE"]) / "answer.txt"
passed = answer.is_file() and "EVAL_SHARED_ROOT" not in os.environ
print(json.dumps({
    "passed": passed,
    "assertions": [{
        "id": "answer-present",
        "passed": passed,
        "evidence": "null shared verifier root was omitted",
    }],
    "metrics": {},
}))
""",
        )
        self.fixture.use_v4_objective()
        provider = self.fixture.provider()

        result = EvalRunner(self.load(), provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("null-shared-verifier"),
        )

        self.assertTrue(
            all(
                arm["verifier"]["passed"]
                and all(
                    not (
                        value.startswith("BindReadOnlyPaths=")
                        and value.endswith("/_shared")
                    )
                    for value in arm["verifier"]["sandbox"]["properties"]
                )
                for pair in result["pairs"]
                for arm in pair["arms"].values()
            ),
            result,
        )

    def test_legacy_shared_verifier_environment_is_preserved(self) -> None:
        self.fixture._write_suite(
            "cases/testing/_shared/helper.txt", "legacy shared value\n"
        )
        self.fixture._write_suite(
            "cases/testing/_shared/verifier.py",
            """import json
import os
from pathlib import Path

shared = Path(os.environ["EVAL_SHARED_ROOT"])
answer = Path(os.environ["EVAL_WORKSPACE"]) / "answer.txt"
passed = (
    answer.is_file()
    and (shared / "helper.txt").read_text(encoding="utf-8") == "legacy shared value\\n"
)
print(json.dumps({
    "passed": passed,
    "assertions": [{
        "id": "answer-present",
        "passed": passed,
        "evidence": "legacy shared verifier environment remains available",
    }],
    "metrics": {},
}))
""",
        )
        for version in (2, 3):
            self.fixture.manifest = self.fixture._manifest()
            self.fixture.isolate_basic_case()
            if version == 3:
                self.fixture.use_v3_objective()
            self.fixture.manifest["cases"][0]["verifier"]["argv"] = [
                "python3",
                "cases/testing/_shared/verifier.py",
            ]
            self.fixture.save_manifest()
            provider = self.fixture.provider()
            suite = self.load()
            runner = (
                EvalRunner(suite, provider, provider)
                if version == 2
                else EvalRunner(suite, provider)
            )

            with runner:
                result = runner.run(
                    RunSelection(comparison_ids=("without-current",)),
                    output_dir=self.output(f"legacy-shared-v{version}"),
                )

            with self.subTest(version=version):
                self.assertTrue(
                    all(
                        arm["verifier"]["passed"]
                        and any(
                            value.startswith("BindReadOnlyPaths=")
                            and value.endswith("/_shared")
                            for value in arm["verifier"]["sandbox"]["properties"]
                        )
                        for pair in result["pairs"]
                        for arm in pair["arms"].values()
                    ),
                    result,
                )

    def test_absent_legacy_shared_verifier_path_remains_unmounted(self) -> None:
        verifier = """import json
import os
from pathlib import Path

shared_value = os.environ.get("EVAL_SHARED_ROOT")
answer = Path(os.environ["EVAL_WORKSPACE"]) / "answer.txt"
passed = (
    answer.is_file()
    and shared_value is not None
    and not Path(shared_value).exists()
)
print(json.dumps({
    "passed": passed,
    "assertions": [{
        "id": "answer-present",
        "passed": passed,
        "evidence": "absent legacy shared path remains exported but unmounted",
    }],
    "metrics": {},
}))
"""
        for version in (2, 3):
            self.fixture.manifest = self.fixture._manifest()
            self.fixture.isolate_basic_case()
            self.fixture._write_suite("cases/basic/oracle/verifier.py", verifier)
            if version == 3:
                self.fixture.use_v3_objective()
            provider = self.fixture.provider()
            suite = self.load()
            runner = (
                EvalRunner(suite, provider, provider)
                if version == 2
                else EvalRunner(suite, provider)
            )

            with runner:
                result = runner.run(
                    RunSelection(comparison_ids=("without-current",)),
                    output_dir=self.output(f"absent-legacy-shared-v{version}"),
                )

            with self.subTest(version=version):
                self.assertTrue(
                    all(
                        arm["verifier"]["passed"]
                        and all(
                            not (
                                value.startswith("BindReadOnlyPaths=")
                                and value.endswith("/_shared")
                            )
                            for value in arm["verifier"]["sandbox"]["properties"]
                        )
                        for pair in result["pairs"]
                        for arm in pair["arms"].values()
                    ),
                    result,
                )

    def test_configured_shared_verifier_drift_fails_before_provider_dispatch(
        self,
    ) -> None:
        self.fixture.isolate_basic_case()
        shared_path = "verifier-resources/shared/helper.py"
        self.fixture._write_suite(shared_path, "VALUE = 1\n")
        self.fixture.use_v4_objective()
        self.fixture.manifest["shared_verifier_dir"] = "verifier-resources/shared"
        self.fixture.save_manifest()
        provider = self.fixture.provider()
        runner = EvalRunner(self.load(), provider)
        original_preflight = runner.preflight

        def preflight_then_mutate(selection):
            evidence = original_preflight(selection)
            self.fixture._write_suite(shared_path, "VALUE = 2\n")
            return evidence

        with (
            patch.object(runner, "preflight", side_effect=preflight_then_mutate),
            self.assertRaisesRegex(RunnerError, "source drifted after preflight"),
        ):
            runner.run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.output("shared-verifier-drift"),
            )
        self.assertEqual(provider.agent_requests, [])

    def test_configured_shared_verifier_revalidates_ancestors_after_load(self) -> None:
        self.fixture.isolate_basic_case()
        self.fixture._write_suite("verifier-resources/shared/helper.py", "VALUE = 1\n")
        self.fixture.use_v4_objective()
        self.fixture.manifest["shared_verifier_dir"] = "verifier-resources/shared"
        self.fixture.save_manifest()
        suite = self.load()
        original = self.fixture.suite_root / "verifier-resources"
        original.rename(self.fixture.suite_root / "original-verifier-resources")
        external = self.fixture.root / "external-verifier-resources"
        external.joinpath("shared").mkdir(parents=True)
        external.joinpath("shared/helper.py").write_text(
            "EXTERNAL = True\n", encoding="utf-8"
        )
        original.symlink_to(external, target_is_directory=True)
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "traverses a symlink"):
            EvalRunner(suite, provider).preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_preflight_rejects_unmounted_suite_verifier_scripts(self) -> None:
        self.fixture.isolate_basic_case()
        self.fixture._write_suite("tools/verifier.py", _PASSING_VERIFIER)
        self.fixture._write_suite("verifier-resources/shared/helper.py", "VALUE = 1\n")
        for shared_verifier_dir in (None, "verifier-resources/shared"):
            self.fixture.manifest = self.fixture._manifest()
            self.fixture.isolate_basic_case()
            self.fixture.use_v4_objective()
            self.fixture.manifest["shared_verifier_dir"] = shared_verifier_dir
            self.fixture.manifest["cases"][0]["verifier"]["argv"] = [
                "python3",
                "tools/verifier.py",
            ]
            self.fixture.save_manifest()
            provider = self.fixture.provider()

            with self.subTest(shared_verifier_dir=shared_verifier_dir):
                with self.assertRaisesRegex(
                    RunnerError, "outside case and shared verifier roots"
                ):
                    EvalRunner(self.load(), provider).preflight(
                        RunSelection(comparison_ids=("without-current",))
                    )
                self.assertEqual(provider.agent_requests, [])

    def test_verifier_sandbox_hides_host_secrets_processes_and_network(self) -> None:
        secret = self.fixture.repository / "verifier-secret.txt"
        secret.write_text("must stay hidden", encoding="utf-8")
        runtime_parent = Path(f"/run/user/{os.getuid()}")
        with tempfile.TemporaryDirectory(dir=runtime_parent) as external_temp:
            external_home = Path(external_temp) / "external-user-home"
            config_root = external_home / ".claude"
            config_root.mkdir(parents=True)
            external_secret = external_home / "ssh-sibling-secret.txt"
            external_secret.write_text("must stay hidden", encoding="utf-8")
            self.fixture.set_verifier(
                f"""import json
import os
import socket
from pathlib import Path

def hidden(path):
    try:
        return not Path(path).exists()
    except OSError:
        return True

paths_hidden = all(hidden(path) for path in (
    {str(secret)!r},
    {str(external_secret)!r},
))
host_pid = {os.getpid()}
process_hidden = all(
    not Path("/proc", str(host_pid), name).exists()
    for name in ("cmdline", "environ")
)
try:
    os.kill(host_pid, 0)
    signal_blocked = False
except (ProcessLookupError, PermissionError):
    signal_blocked = True
environment_minimal = (
    "ANTHROPIC_API_KEY" not in os.environ
    and {{name for name in os.environ if name.startswith("EVAL_")}} == {{
        "EVAL_WORKSPACE",
        "EVAL_CASE_ROOT",
        "EVAL_SHARED_ROOT",
        "EVAL_TOOL_BIN",
        "EVAL_RESULT_ROOT",
        "EVAL_HOST_UID",
        "EVAL_UNSHARE",
        "EVAL_MOUNT",
        "EVAL_SETPRIV",
        "EVAL_ENV",
    }}
)
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.2)
    network_hidden = False
except OSError:
    network_hidden = True
answer_present = Path(os.environ["EVAL_WORKSPACE"], "answer.txt").is_file()
passed = all((
    paths_hidden,
    process_hidden,
    signal_blocked,
    os.getpid() == 1,
    environment_minimal,
    network_hidden,
    answer_present,
))
print(json.dumps({{"passed": passed, "assertions": [{{
    "id": "answer-present", "passed": passed,
    "evidence": f"paths={{paths_hidden}} proc={{process_hidden}} signal={{signal_blocked}} "
                f"pid={{os.getpid()}} env={{environment_minimal}} network={{network_hidden}}"
}}]}}))
"""
            )
            with patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "must-not-leak",
                    "CLAUDE_CONFIG_DIR": str(config_root),
                },
            ):
                result = self.runner().run(
                    RunSelection(comparison_ids=("without-current",)),
                    output_dir=self.output("verifier-sandbox"),
                )

        self.assertTrue(result["passed"], result)
        for arm in result["pairs"][0]["arms"].values():
            sandbox = arm["verifier"]["sandbox"]
            self.assertEqual(sandbox["kind"], "systemd-run-user")
            self.assertIn("PrivateNetwork=yes", sandbox["properties"])
            self.assertIn("ProtectProc=invisible", sandbox["properties"])
            self.assertIn("ProcSubset=pid", sandbox["properties"])
            self.assertIn("PrivateUsers=yes", sandbox["properties"])
            self.assertIn("MemoryMax=3G", sandbox["properties"])
            self.assertEqual(sandbox["environment_mode"], "env-i-allowlist")
            self.assertEqual(
                sandbox["process_namespace"], "unshare-user-pid-private-proc"
            )
            self.assertEqual(
                sandbox["candidate_process_namespace"],
                "nested-unshare-user-mount-pid-net-ipc-uts-private-proc",
            )

    def test_result_root_outside_tmp_and_home_is_explicitly_masked(self) -> None:
        runtime_parent = Path(f"/run/user/{os.getuid()}")
        if not runtime_parent.is_dir() or not os.access(runtime_parent, os.W_OK):
            self.skipTest("user runtime directory is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="skill-eval-result-parent-", dir=runtime_parent
        ) as raw_parent:
            output = Path(raw_parent) / "result"
            self.fixture.set_verifier(
                """import json
import os
from pathlib import Path
result_root = Path(os.environ["EVAL_RESULT_ROOT"])
try:
    list(result_root.iterdir())
    hidden = False
except OSError:
    hidden = True
answer = Path(os.environ["EVAL_WORKSPACE"], "answer.txt")
passed = hidden and answer.is_file()
print(json.dumps({"passed": passed, "assertions": [{
    "id": "answer-present", "passed": passed,
    "evidence": f"result root hidden={hidden}",
}]}))
"""
            )
            result = self.runner().run(
                RunSelection(comparison_ids=("without-current",)), output_dir=output
            )
            self.assertTrue(result["passed"], result)
            for arm in result["pairs"][0]["arms"].values():
                sandbox = arm["verifier"]["sandbox"]
                self.assertEqual(sandbox["result_root_masked"], str(output.resolve()))
                self.assertIn(
                    f"InaccessiblePaths=-{output.resolve()}", sandbox["properties"]
                )

    def test_unsafe_preexisting_result_roots_are_rejected(self) -> None:
        unsafe_mode = self.output("unsafe-mode")
        unsafe_mode.mkdir(mode=0o755)
        unsafe_mode.chmod(0o755)
        with self.assertRaisesRegex(RunnerError, "mode 0700"):
            self.runner().run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=unsafe_mode,
            )

        nonempty = self.output("nonempty")
        nonempty.mkdir(mode=0o700)
        nonempty.chmod(0o700)
        nonempty.joinpath("sentinel").write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "must be empty"):
            self.runner().run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=nonempty,
            )

    def test_verifier_uses_preflight_tool_bytes_after_path_disagreement(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        self.fixture.manifest["cases"][0]["verifier"]["required_tools"] = ["node"]
        self.fixture.save_manifest()
        self.fixture.set_verifier(
            """import hashlib
import json
import os
from pathlib import Path
import shutil
tool = Path(shutil.which("node") or "")
inside = tool.parent == Path(os.environ["EVAL_TOOL_BIN"])
answer = Path(os.environ["EVAL_WORKSPACE"], "answer.txt")
passed = inside and answer.is_file()
print(json.dumps({"passed": passed, "assertions": [{
    "id": "answer-present", "passed": passed,
    "evidence": f"private node={tool} sha={hashlib.sha256(tool.read_bytes()).hexdigest() if tool.is_file() else 'missing'}",
}]}))
"""
        )
        fake_bin = self.output("fake-path")
        fake_bin.mkdir()
        fake_node = fake_bin / "node"
        fake_node.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        fake_node.chmod(0o755)
        original_path = os.environ.get("PATH", "")

        provider = self.fixture.provider()
        original_handler = provider._agent_handler

        def agent(request):
            os.environ["PATH"] = f"{fake_bin}:{original_path}"
            return original_handler(request)

        provider._agent_handler = agent
        self.addCleanup(os.environ.__setitem__, "PATH", original_path)
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("path-disagreement"),
        )
        self.assertTrue(result["passed"], result)
        expected = Path(node).resolve()
        for arm in result["pairs"][0]["arms"].values():
            tool = arm["verifier"]["sandbox"]["required_tools"]["node"]
            copied = arm["verifier"]["sandbox"]["copied_executables"]["node"]
            self.assertEqual(Path(tool["source_path"]), expected)
            self.assertEqual(copied["sha256"], tool["sha256"])

    def test_generated_caches_do_not_drift_or_enter_source_snapshot(self) -> None:
        self.fixture.set_verifier(
            """import json
import os
from pathlib import Path
case_root = Path(os.environ["EVAL_CASE_ROOT"])
cache_free = not any(path.name == "__pycache__" for path in case_root.rglob("*"))
answer = Path(os.environ["EVAL_WORKSPACE"], "answer.txt")
passed = cache_free and answer.is_file()
print(json.dumps({"passed": passed, "assertions": [{
    "id": "answer-present", "passed": passed,
    "evidence": f"cache-free snapshot={cache_free}",
}]}))
"""
        )
        provider = self.fixture.provider()
        original_handler = provider._agent_handler
        created = threading.Event()

        def agent(request):
            response = original_handler(request)
            if not created.is_set():
                created.set()
                cache = self.fixture.suite_root / "__pycache__"
                cache.mkdir(exist_ok=True)
                cache.joinpath("late.pyc").write_bytes(b"generated cache")
            return response

        provider._agent_handler = agent
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("cache-snapshot"),
        )
        self.assertTrue(result["passed"], result)

    def test_provider_failure_never_runs_verifier_or_comparator(self) -> None:
        def fail(_request):
            raise ProviderError("synthetic provider failure")

        provider = FakeProvider(agent_handler=fail)
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("provider-failure"),
        )

        self.assertFalse(result["passed"])
        self.assertEqual(provider.comparator_requests, [])
        for arm in result["pairs"][0]["arms"].values():
            self.assertEqual(arm["error_stage"], "agent")
            self.assertFalse(arm["verifier"]["ran"])
        dispatch = result["aggregate"]["generator_dispatch_ledger"]
        self.assertEqual(dispatch["states"]["failed"], 6)
        self.assertEqual(dispatch["unresolved_attempts"], [])
        self.assertEqual(result["aggregate"]["unknown_cost_invocations"], 6)
        self.assertIsNone(result["aggregate"]["total_cost_usd"])
        self.assertTrue(
            all(
                arm["provider_accounting"]["provider_entered"] is True
                and arm["provider_accounting"]["dispatched"] is False
                for pair in result["pairs"]
                for arm in pair["arms"].values()
            )
        )

    def test_post_synchronization_failure_does_not_abort_peer_barrier_exit(
        self,
    ) -> None:
        def fail(_request):
            raise ProviderError("synthetic post-synchronization failure")

        provider = FakeProvider(agent_handler=fail)
        with patch("harness_evals.runner.threading.Barrier", _StaggeredSecondBarrier):
            result = self.runner(provider).run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.output("post-sync-failure"),
            )

        self.assertFalse(result["passed"])
        for pair in result["pairs"]:
            self.assertEqual(
                {arm["error_stage"] for arm in pair["arms"].values()}, {"agent"}
            )
            self.assertTrue(
                all(
                    "post-synchronization failure" in arm["error"]
                    for arm in pair["arms"].values()
                )
            )
        self.assertEqual(result["aggregate"]["unknown_cost_invocations"], 6)
        self.assertIsNone(result["aggregate"]["total_cost_usd"])

    def test_dispatched_agent_failures_are_counted_as_unknown_cost(self) -> None:
        def fail(request):
            self.assertIsNotNone(request.on_dispatched)
            request.on_dispatched()
            raise ProviderError("synthetic failure after dispatch")

        provider = FakeProvider(agent_handler=fail)
        result = self.runner(provider).run(
            RunSelection(
                comparison_ids=("without-current",),
                verifier_only=True,
            ),
            output_dir=self.output("dispatched-agent-failure"),
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["aggregate"]["known_total_cost_usd"], 0.0)
        self.assertEqual(result["aggregate"]["unknown_cost_invocations"], 6)
        self.assertIsNone(result["aggregate"]["total_cost_usd"])
        self.assertTrue(
            all(
                arm["provider_accounting"]["dispatched"] is True
                for pair in result["pairs"]
                for arm in pair["arms"].values()
            )
        )
        dispatch = result["aggregate"]["generator_dispatch_ledger"]
        self.assertEqual(dispatch["states"]["failed"], 6)
        self.assertEqual(dispatch["unresolved_attempts"], [])
        journal = self.output("dispatched-agent-failure") / "generator-dispatch.jsonl"
        self.assertTrue(journal.is_file())
        events = [
            json.loads(line)["event"]
            for line in journal.read_text(encoding="ascii").splitlines()
        ]
        self.assertEqual(events.count("planned"), 6)
        self.assertEqual(events.count("dispatched"), 6)
        self.assertEqual(events.count("failed"), 6)

    def test_injected_fake_generator_is_public_verifier_only(self) -> None:
        self.fixture.manifest["provider"] = {
            "kind": "claude",
            "executable": str(self.fixture.fake_codex),
            "model": "fake-model-v1",
            "max_budget_usd": 1.0,
            "timeout_seconds": 10,
        }
        self.fixture.save_manifest()

        judged_provider = self.fixture.provider()
        judged_output = self.output("injected-fake-judged")
        with self.assertRaisesRegex(
            RunnerError,
            "injected fake generator.*non-holdout verifier-only",
        ):
            self.runner(judged_provider).run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=judged_output,
            )
        self.assertEqual(judged_provider.agent_requests, [])
        self.assertFalse(judged_output.exists())

        class DerivedFakeProvider(FakeProvider):
            pass

        class RenamedDerivedFakeProvider(FakeProvider):
            @property
            def name(self) -> str:
                return "claude-cli"

        class CustomFakeIdentityProvider:
            def __init__(self) -> None:
                self.agent_requests: list[AgentRequest] = []

            @property
            def name(self) -> str:
                return "deterministic-fake"

            @property
            def version(self) -> str:
                return "custom"

            @property
            def execution_policy(self) -> ProviderExecutionPolicy:
                return ProviderExecutionPolicy("concurrent", True)

            def run_agent(self, request: AgentRequest) -> ProviderResult:
                self.agent_requests.append(request)
                raise AssertionError("near-miss fake provider reached agent execution")

            def run_comparator(self, _request):
                raise AssertionError("near-miss fake provider reached comparison")

        near_misses = (
            DerivedFakeProvider(),
            RenamedDerivedFakeProvider(),
            CustomFakeIdentityProvider(),
        )
        for index, near_miss in enumerate(near_misses):
            for verifier_only in (False, True):
                with self.subTest(
                    provider=type(near_miss).__name__,
                    verifier_only=verifier_only,
                ):
                    output = self.output(
                        f"injected-fake-near-miss-{index}-{verifier_only}"
                    )
                    with self.assertRaisesRegex(
                        RunnerError,
                        "injected fake generator.*non-holdout verifier-only",
                    ):
                        EvalRunner(
                            self.load(),
                            near_miss,
                            self.fixture.provider(),
                        ).run(
                            RunSelection(
                                comparison_ids=("without-current",),
                                verifier_only=verifier_only,
                            ),
                            output_dir=output,
                        )
                    self.assertEqual(near_miss.agent_requests, [])
                    self.assertFalse(output.exists())

        poison_provider = FakeProvider()
        poison_runner = EvalRunner(
            self.load(), poison_provider, self.fixture.provider()
        )
        poison_request = AgentRequest(
            case_id=self.load().cases[0].id,
            variant_id="without",
            prompt="prompt",
            model="fake-model-v1",
            workspace=self.fixture.suite_root / "fixture",
            skill_snapshot=None,
            sandbox_pair_root=self.fixture.suite_root,
            sandbox_repository_root=self.fixture.repository,
            system_context="context",
            timeout_seconds=10,
        )
        poison_result = replace(
            poison_provider.run_agent(poison_request),
            sandbox={"enforced": True, "kind": "fake", "extra": True},
        )
        with self.assertRaisesRegex(
            RunnerError, "verifier-only fake agent result sandbox is not exact"
        ):
            poison_runner._agent_result_json(
                poison_result,
                poison_request,
                verifier_only=True,
            )

        provider = self.fixture.provider()
        output = self.output("injected-fake-verifier-only")
        result = self.runner(provider).run(
            RunSelection(
                comparison_ids=("without-current",),
                verifier_only=True,
            ),
            output_dir=output,
        )

        arms = [arm for pair in result["pairs"] for arm in pair["arms"].values()]
        snapshot = json.loads(
            (output / "manifest.snapshot.json").read_text(encoding="utf-8")
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(arms), 6)
        self.assertTrue(
            all(
                arm["status"] == "completed"
                and arm["passed"]
                and arm["verifier"]["ran"]
                and arm["verifier"]["valid"]
                and arm["verifier"]["passed"]
                and arm["provider"]["sandbox"] == {"enforced": True, "kind": "fake"}
                for arm in arms
            )
        )
        self.assertEqual(provider.comparator_requests, [])
        self.assertEqual(result["execution_mode"], "verifier_only")
        self.assertFalse(result["aggregate"]["final_release_authorized"])
        self.assertEqual(result["preflight"]["provider"]["name"], "deterministic-fake")
        self.assertEqual(snapshot["provider"]["kind"], "claude")

    def test_dispatch_callback_is_durable_before_it_returns_to_provider(self) -> None:
        observed_audits: list[dict[str, object]] = []
        runner: EvalRunner

        def agent(request: AgentRequest) -> dict[str, object]:
            self.assertIsNotNone(request.on_dispatched)
            request.on_dispatched()
            self.assertIsNotNone(runner._generator_dispatch_ledger)
            observed_audits.append(runner._generator_dispatch_ledger.audit())
            (request.workspace / "answer.txt").write_text(
                "durably dispatched\n", encoding="utf-8"
            )
            return {
                "final_output": "durably dispatched",
                "cost_usd": 0.25,
                "tokens": {"input_tokens": 1, "output_tokens": 1},
            }

        provider = self.fixture.provider()
        provider._agent_handler = agent
        runner = self.runner(provider)
        result = runner.run(
            RunSelection(
                comparison_ids=("without-current",),
                verifier_only=True,
            ),
            output_dir=self.output("callback-durable"),
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(len(observed_audits), 6)
        self.assertTrue(
            all(audit["states"]["dispatched"] >= 1 for audit in observed_audits)
        )

    def test_dispatch_append_failure_aborts_and_preserves_unresolved_journal(
        self,
    ) -> None:
        output = self.output("dispatch-append-failure")
        callback_returned = threading.Event()

        def agent(request: AgentRequest) -> dict[str, object]:
            journal = output / "generator-dispatch.jsonl"
            journal.chmod(0o400)
            self.assertIsNotNone(request.on_dispatched)
            request.on_dispatched()
            callback_returned.set()
            return {"final_output": "must not return"}

        suite = self.fixture.codex_suite(self.load())
        provider = _SerializedCodexTestProvider(
            self.fixture.codex_protocol_lock,
            agent_handler=agent,
        )
        runner = EvalRunner(suite, provider, self.fixture.provider())
        with self.assertRaises(_GeneratorDispatchJournalError):
            runner.run(
                RunSelection(
                    comparison_ids=("without-current",),
                    verifier_only=True,
                ),
                output_dir=output,
            )

        self.assertFalse(callback_returned.is_set())
        journal = output / "generator-dispatch.jsonl"
        self.assertTrue(journal.is_file())
        self.assertFalse((output / "run.json").exists())
        journal.chmod(0o600)
        recovered = _GeneratorDispatchLedger(
            result_root=output.resolve(),
            suite_id=suite.suite_id,
            manifest_sha256=suite.manifest_hash,
            provider_binding=runner._generator_provider_binding(),
        )
        audit = recovered.audit()
        self.assertEqual(audit["states"]["planned"], 1)
        self.assertEqual(audit["unresolved_attempts"][0]["phase"], "planned")
        recovered.close()

    def test_dispatch_file_fsync_failure_aborts_before_callback_returns(self) -> None:
        output = self.output("dispatch-file-fsync-failure")
        callback_returned = threading.Event()
        real_fsync = os.fsync
        journal_fsyncs = 0

        def fail_dispatch_fsync(descriptor: int) -> None:
            nonlocal journal_fsyncs
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if target.endswith("/generator-dispatch.jsonl"):
                journal_fsyncs += 1
                if journal_fsyncs == 3:
                    raise OSError("synthetic dispatch fsync failure")
            real_fsync(descriptor)

        def agent(request: AgentRequest) -> dict[str, object]:
            self.assertIsNotNone(request.on_dispatched)
            request.on_dispatched()
            callback_returned.set()
            return {"final_output": "must not return"}

        suite = self.fixture.codex_suite(self.load())
        provider = _SerializedCodexTestProvider(
            self.fixture.codex_protocol_lock,
            agent_handler=agent,
        )
        runner = EvalRunner(suite, provider, self.fixture.provider())
        with (
            patch("harness_evals.runner.os.fsync", side_effect=fail_dispatch_fsync),
            self.assertRaises(_GeneratorDispatchJournalError),
        ):
            runner.run(
                RunSelection(
                    comparison_ids=("without-current",),
                    verifier_only=True,
                ),
                output_dir=output,
            )

        self.assertEqual(journal_fsyncs, 3)
        self.assertFalse(callback_returned.is_set())
        self.assertFalse((output / "run.json").exists())
        recovered = _GeneratorDispatchLedger(
            result_root=output.resolve(),
            suite_id=suite.suite_id,
            manifest_sha256=suite.manifest_hash,
            provider_binding=runner._generator_provider_binding(),
        )
        audit = recovered.audit()
        self.assertEqual(audit["states"]["dispatched"], 1)
        self.assertEqual(audit["unresolved_attempts"][0]["phase"], "dispatched")
        recovered.close()

    def test_first_creation_fsyncs_files_and_parent_before_provider_entry(
        self,
    ) -> None:
        output = self.output(
            "dispatch-missing-parent/dispatch-child/dispatch-creation-fsync-order"
        )
        events: list[str] = []
        real_fsync = os.fsync
        real_fsync_directory = runner_module._fsync_directory

        def record_fsync(descriptor: int) -> None:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if target.endswith("/generator-dispatch.lock"):
                events.append("lock-file-fsync")
            elif target.endswith("/generator-dispatch.jsonl"):
                events.append("journal-file-fsync")
            real_fsync(descriptor)

        def record_directory_fsync(path: Path) -> None:
            events.append(f"directory-fsync:{Path(path).resolve()}")
            real_fsync_directory(path)

        provider = self.fixture.provider()
        original_agent = provider._agent_handler

        def agent(request: AgentRequest):
            events.append("provider-entry")
            return original_agent(request)

        provider._agent_handler = agent
        with (
            patch("harness_evals.runner.os.fsync", side_effect=record_fsync),
            patch(
                "harness_evals.runner._fsync_directory",
                side_effect=record_directory_fsync,
            ),
        ):
            result = self.runner(provider).run(
                RunSelection(
                    comparison_ids=("without-current",),
                    verifier_only=True,
                ),
                output_dir=output,
            )

        self.assertTrue(result["passed"], result)
        lock_file = events.index("lock-file-fsync")
        journal_file = events.index("journal-file-fsync")
        result_root_fsyncs = [
            index
            for index, event in enumerate(events)
            if event == f"directory-fsync:{output.resolve()}"
        ]
        creation_parent_fsyncs = [
            events.index(f"directory-fsync:{path.resolve()}")
            for path in (
                Path(self.temporary.name),
                output.parents[1],
                output.parent,
            )
        ]
        provider_entry = events.index("provider-entry")
        self.assertEqual(creation_parent_fsyncs, sorted(creation_parent_fsyncs))
        self.assertLess(creation_parent_fsyncs[-1], lock_file)
        self.assertGreaterEqual(len(result_root_fsyncs), 2)
        self.assertLess(lock_file, result_root_fsyncs[0])
        self.assertLess(result_root_fsyncs[0], journal_file)
        self.assertLess(journal_file, result_root_fsyncs[1])
        self.assertLess(result_root_fsyncs[1], provider_entry)
        replayed = _GeneratorDispatchLedger(
            result_root=output.resolve(),
            suite_id=self.load().suite_id,
            manifest_sha256=self.load().manifest_hash,
            provider_binding=result["preflight"]["provider"],
        )
        self.assertEqual(replayed.audit()["states"]["completed"], 6)
        replayed.close()

    def test_lock_replacement_at_close_prevents_run_publication(self) -> None:
        output = self.output("dispatch-lock-replacement-at-close")
        real_audit = _GeneratorDispatchLedger.audit
        journal_before_close: bytes | None = None
        lock_descriptor: int | None = None
        replaced = False

        def replace_lock_after_final_audit(
            ledger: _GeneratorDispatchLedger,
        ) -> dict[str, object]:
            nonlocal journal_before_close, lock_descriptor, replaced
            audit = real_audit(ledger)
            if not replaced and not audit["unresolved_attempts"]:
                journal_before_close = ledger.journal_path.read_bytes()
                lock_descriptor = ledger._lock_descriptor
                ledger.lock_path.replace(output / "replaced-generator-dispatch.lock")
                ledger.lock_path.write_bytes(b"")
                ledger.lock_path.chmod(0o600)
                replaced = True
            return audit

        suite = self.load()
        runner = self.runner()
        with (
            patch.object(
                _GeneratorDispatchLedger,
                "audit",
                new=replace_lock_after_final_audit,
            ),
            self.assertRaisesRegex(
                _GeneratorDispatchJournalError, "lock lost integrity"
            ),
        ):
            runner.run(
                RunSelection(
                    comparison_ids=("without-current",),
                    verifier_only=True,
                ),
                output_dir=output,
            )

        self.assertTrue(replaced)
        self.assertIsNotNone(journal_before_close)
        self.assertEqual(
            (output / "generator-dispatch.jsonl").read_bytes(),
            journal_before_close,
        )
        self.assertFalse((output / "run.json").exists())
        self.assertIsNone(runner._generator_dispatch_ledger)
        self.assertIsNotNone(lock_descriptor)
        with self.assertRaises(OSError):
            os.fstat(lock_descriptor)  # type: ignore[arg-type]

        recovered = _GeneratorDispatchLedger(
            result_root=output.resolve(),
            suite_id=suite.suite_id,
            manifest_sha256=suite.manifest_hash,
            provider_binding=runner._generator_provider_binding(),
        )
        self.assertEqual(recovered.audit()["states"]["completed"], 6)
        recovered.close()

    def test_actual_model_mismatch_invalidates_pair_before_comparison(self) -> None:
        provider = self.fixture.provider(actual_model_by_variant=True)
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("model-mismatch"),
        )

        self.assertFalse(result["passed"])
        pair = result["pairs"][0]
        self.assertEqual(
            {arm["error_stage"] for arm in pair["arms"].values()}, {"agent"}
        )
        self.assertTrue(
            all(
                "did not use exactly the pinned model" in arm["error"]
                for arm in pair["arms"].values()
            )
        )
        self.assertEqual(provider.comparator_requests, [])
        self.assertEqual(result["aggregate"]["unknown_cost_invocations"], 6)
        self.assertIsNone(result["aggregate"]["total_cost_usd"])

    def test_missing_verifier_script_fails_preflight_before_output_creation(
        self,
    ) -> None:
        self.fixture.manifest["cases"][0]["verifier"]["argv"] = [
            "python3",
            "missing-verifier.py",
        ]
        self.fixture.save_manifest()
        output = self.output("missing-tool")
        with self.assertRaisesRegex(RunnerError, "verifier script is missing"):
            self.runner().run(
                RunSelection(comparison_ids=("without-current",)), output_dir=output
            )
        self.assertFalse(output.exists())

    def test_dirty_worktree_skill_fails_preflight_but_git_ref_stays_pinned(
        self,
    ) -> None:
        self.fixture._write(
            "skills/demo/SKILL.md", "# Demo Skill\n\nUncommitted treatment guidance.\n"
        )
        with self.assertRaisesRegex(RunnerError, "commit it before A/B evaluation"):
            self.runner().preflight(RunSelection(comparison_ids=("without-current",)))

        suite = self.load()
        old_variant = next(variant for variant in suite.variants if variant.id == "old")
        without_variant = next(
            variant for variant in suite.variants if variant.id == "without"
        )
        old_only = replace(
            suite,
            variants=(without_variant, old_variant),
            comparisons=(
                replace(
                    suite.comparisons[0],
                    id="without-old",
                    treatment="old",
                ),
            ),
        )
        provider = self.fixture.provider()
        preflight = EvalRunner(old_only, provider, provider).preflight(
            RunSelection(comparison_ids=("without-old",))
        )
        self.assertEqual(
            preflight["sources"]["old"]["source_commit"], self.fixture.baseline_commit
        )

    def test_configured_bundle_source_must_have_regular_entrypoint(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/references/rule.md", "# Rule\n\nPresent.\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle without entrypoint")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "regular SKILL.md entrypoint"):
            EvalRunner(self.load(), provider).preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_configured_bundle_source_rejects_symlink_traversal(self) -> None:
        self.assertTrue(hasattr(os, "symlink"), "runner requires POSIX symlinks")
        self.fixture._write("instruction-bundles/target/SKILL.md", "# Linked Bundle\n")
        os.symlink(
            self.fixture.repository / "instruction-bundles/target",
            self.fixture.repository / "instruction-bundles/demo",
            target_is_directory=True,
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "linked bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "traverses symlink"):
            EvalRunner(self.load(), provider).preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_git_ref_configured_bundle_rejects_missing_entrypoint(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/references/rule.md", "# Rule\n\nPresent.\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle without entrypoint")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["comparisons"] = [
            {
                "id": "without-old",
                "control": "without",
                "treatment": "old",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            }
        ]
        self.fixture.use_v4_objective("instruction-bundles/demo")
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "no complete bundle directory"):
            EvalRunner(self.load(), provider).preflight(
                RunSelection(comparison_ids=("without-old",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_git_ref_configured_bundle_rejects_special_entries(self) -> None:
        self.assertTrue(hasattr(os, "symlink"), "runner requires POSIX symlinks")
        self.fixture._write("instruction-bundles/target.md", "# Target\n")
        os.symlink(
            self.fixture.repository / "instruction-bundles/target.md",
            self.fixture.repository / "instruction-bundles/SKILL.md",
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle with special entry")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["comparisons"] = [
            {
                "id": "without-old",
                "control": "without",
                "treatment": "old",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            }
        ]
        self.fixture.use_v4_objective("instruction-bundles")
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "unsupported git entry"):
            EvalRunner(self.load(), provider).preflight(
                RunSelection(comparison_ids=("without-old",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_dirty_configured_bundle_source_fails_preflight(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md",
            "# Configured Bundle\n\nUncommitted change.\n",
        )
        provider = self.fixture.provider()

        with self.assertRaisesRegex(RunnerError, "commit it before A/B evaluation"):
            EvalRunner(self.load(), provider).preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        self.assertEqual(provider.agent_requests, [])

    def test_configured_bundle_source_drift_after_preflight_fails_closed(self) -> None:
        bundle_path = "instruction-bundles/demo/SKILL.md"
        self.fixture._write(bundle_path, "# Configured Bundle\n")
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        provider = self.fixture.provider()
        runner = EvalRunner(self.load(), provider)
        original_preflight = runner.preflight

        def preflight_then_mutate(selection):
            evidence = original_preflight(selection)
            self.fixture._write(
                bundle_path, "# Configured Bundle\n\nChanged after preflight.\n"
            )
            return evidence

        with patch.object(runner, "preflight", side_effect=preflight_then_mutate):
            result = runner.run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.output("configured-bundle-drift"),
            )
        self.assertFalse(result["passed"])
        self.assertTrue(
            all(
                "source drifted after preflight" in pair["arms"]["treatment"]["error"]
                for pair in result["pairs"]
            )
        )
        self.assertTrue(
            all(request.variant_id != "current" for request in provider.agent_requests)
        )

    def test_unrelated_uncommitted_path_does_not_dirty_configured_bundle(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        self.fixture._write("unrelated.txt", "uncommitted but unrelated\n")
        provider = self.fixture.provider()

        preflight = EvalRunner(self.load(), provider).preflight(
            RunSelection(comparison_ids=("without-current",))
        )
        self.assertEqual(
            preflight["sources"]["current"]["expected_source_commit"], bundle_commit
        )
        self.assertEqual(provider.agent_requests, [])

    def test_v4_ignores_untracked_empty_bundle_directories(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        empty = self.fixture.repository / "instruction-bundles/demo"
        for index in range(runner_module.MAX_TREE_DEPTH + 1):
            empty /= f"empty-{index}"
        empty.mkdir(parents=True)

        provider = self.fixture.provider()
        preflight = EvalRunner(self.load(), provider).preflight(
            RunSelection(comparison_ids=("old-current",))
        )

        self.assertFalse(preflight["sources"]["current"]["source_dirty"])
        self.assertEqual(provider.agent_requests, [])

    def test_v4_bounds_empty_directory_traversal_entries(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        bundle = self.fixture.repository / "instruction-bundles/demo"
        for index in range(3):
            bundle.joinpath(f"empty-{index}").mkdir()

        with (
            patch.object(runner_module, "MAX_WORKTREE_SCAN_ENTRIES", 2),
            patch.object(
                runner_module.os,
                "walk",
                side_effect=AssertionError("normalized traversal must stream entries"),
            ),
            self.assertRaisesRegex(
                RunnerError, "worktree traversal exceeds maximum entries 2"
            ),
        ):
            runner_module._scan_tree(
                bundle,
                ignore_generated_caches=True,
                ignore_empty_directories=True,
            )

    def test_v4_bounds_empty_directory_traversal_depth(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "configured bundle")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        empty = self.fixture.repository / "instruction-bundles/demo"
        for index in range(3):
            empty /= f"empty-{index}"
        empty.mkdir(parents=True)

        with (
            patch.object(runner_module, "MAX_WORKTREE_SCAN_DEPTH", 2),
            self.assertRaisesRegex(
                RunnerError, "worktree traversal exceeds maximum depth 2"
            ),
        ):
            EvalRunner(self.load(), self.fixture.provider()).preflight(
                RunSelection(comparison_ids=("without-current",))
            )

    def test_configured_bundle_file_limit_matches_git_and_worktree(self) -> None:
        self.fixture._write("instruction-bundles/demo/SKILL.md", "12345")
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "oversized bundle file")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")

        with patch.object(runner_module, "MAX_FILE_BYTES", 4):
            self.assert_configured_bundle_rejected_for_git_and_worktree(
                "bundle snapshot file exceeds 4 bytes",
                "tree file exceeds 4 bytes",
            )

    def test_configured_bundle_total_limit_matches_git_and_worktree(self) -> None:
        self.fixture._write("instruction-bundles/demo/SKILL.md", "1234")
        self.fixture._write("instruction-bundles/demo/rule.md", "5678")
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "oversized bundle tree")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")

        with (
            patch.object(runner_module, "MAX_FILE_BYTES", 4),
            patch.object(runner_module, "MAX_TREE_BYTES", 7),
        ):
            self.assert_configured_bundle_rejected_for_git_and_worktree(
                "bundle snapshot exceeds 7 bytes",
                "tree exceeds 7 bytes",
            )

    def test_configured_bundle_entry_limit_matches_git_and_worktree(self) -> None:
        self.fixture._write("instruction-bundles/demo/SKILL.md", "# Bundle\n")
        self.fixture._write("instruction-bundles/demo/references/rule.md", "rule\n")
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "too many bundle entries")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")

        with patch.object(runner_module, "MAX_TREE_ENTRIES", 2):
            self.assert_configured_bundle_rejected_for_git_and_worktree(
                "bundle snapshot exceeds maximum entries 2",
                "tree exceeds maximum entries 2",
            )

    def test_configured_bundle_depth_limit_matches_git_and_worktree(self) -> None:
        self.fixture._write("instruction-bundles/demo/SKILL.md", "# Bundle\n")
        self.fixture._write(
            "instruction-bundles/demo/references/nested/rule.md", "rule\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle too deep")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.manifest["variants"][2]["source_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")

        with patch.object(runner_module, "MAX_TREE_DEPTH", 1):
            self.assert_configured_bundle_rejected_for_git_and_worktree(
                "bundle snapshot exceeds maximum depth 1",
                "tree exceeds maximum depth 1",
            )

    def test_git_bundle_metadata_is_bounded_while_streaming(self) -> None:
        self.fixture._write(
            "instruction-bundles/demo/SKILL.md", "# Configured Bundle\n"
        )
        self.fixture._write(
            "instruction-bundles/demo/references/long-file-name.md", "rule\n"
        )
        self.fixture._git("add", "instruction-bundles")
        self.fixture._git("commit", "-q", "-m", "bundle metadata")
        bundle_commit = self.fixture._git("rev-parse", "HEAD").strip()
        self.fixture.manifest["variants"][1]["git_ref"] = bundle_commit
        self.fixture.use_v4_objective("instruction-bundles/demo")
        suite = self.load()
        without = next(variant for variant in suite.variants if variant.id == "without")
        old = next(variant for variant in suite.variants if variant.id == "old")
        comparison = replace(
            suite.comparisons[0],
            id="without-old",
            control="without",
            treatment="old",
        )
        git_suite = replace(suite, variants=(without, old), comparisons=(comparison,))

        with (
            patch.object(runner_module, "MAX_GIT_TREE_METADATA_BYTES", 32),
            self.assertRaisesRegex(RunnerError, "git tree metadata exceeds 32 bytes"),
        ):
            EvalRunner(git_suite, self.fixture.provider()).preflight(
                RunSelection(comparison_ids=("without-old",))
            )

    def test_worktree_source_ref_pins_bytes_not_later_unrelated_head(self) -> None:
        self.fixture._write("notes.txt", "later non-skill commit\n")
        self.fixture._git("add", "notes.txt")
        self.fixture._git("commit", "-q", "-m", "unrelated metadata")
        preflight = self.runner().preflight(
            RunSelection(comparison_ids=("without-current",))
        )

        source = preflight["sources"]["current"]
        self.assertEqual(
            source["expected_source_commit"], self.fixture.treatment_commit
        )
        self.assertNotEqual(
            source["worktree_head_commit"], self.fixture.treatment_commit
        )
        self.assertEqual(len(source["expected_source_sha256_by_case"]["basic"]), 64)

    def test_generated_skill_caches_do_not_affect_or_enter_source(self) -> None:
        exclude = self.fixture.repository / ".git/info/exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n__pycache__/\n*.pyc\n")
        cache = self.fixture.repository / "skills/demo/scripts/__pycache__"
        cache.mkdir(parents=True)
        cache.joinpath("compiled.pyc").write_bytes(b"generated cache")

        observed_cache_paths: list[list[str]] = []
        provider = self.fixture.provider()
        original_handler = provider._agent_handler

        def agent(request):
            if request.skill_snapshot is not None:
                observed_cache_paths.append(
                    [
                        path.relative_to(request.skill_snapshot).as_posix()
                        for path in request.skill_snapshot.rglob("*")
                        if path.name == "__pycache__" or path.suffix == ".pyc"
                    ]
                )
            return original_handler(request)

        provider._agent_handler = agent
        result = self.runner(provider).run(
            RunSelection(comparison_ids=("without-current",)),
            output_dir=self.output("ignored-skill-cache"),
        )

        self.assertTrue(result["passed"], result)
        self.assertTrue(observed_cache_paths)
        self.assertTrue(all(not paths for paths in observed_cache_paths))

    def test_worktree_source_ref_mismatch_fails_preflight(self) -> None:
        self.fixture.manifest["variants"][2]["source_ref"] = (
            self.fixture.baseline_commit
        )
        self.fixture.save_manifest()
        with self.assertRaisesRegex(RunnerError, "bytes do not match source_ref"):
            self.runner().preflight(RunSelection(comparison_ids=("without-current",)))

    def test_case_source_drift_after_preflight_fails_closed(self) -> None:
        changed = threading.Event()

        def agent(request):
            (request.workspace / "answer.txt").write_text("candidate", encoding="utf-8")
            if not changed.is_set():
                changed.set()
                self.fixture._write_suite("prompt.md", "mutated during run\n")
            return "candidate"

        provider = self.fixture.provider()
        provider._agent_handler = agent
        with self.assertRaisesRegex(RunnerError, "source drifted"):
            self.runner(provider).run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.output("source-drift"),
            )

    def test_fixture_symlink_is_rejected_before_agent_execution(self) -> None:
        self.assertTrue(hasattr(os, "symlink"), "runner requires POSIX symlinks")
        target = self.fixture.suite_root / "fixture/input.txt"
        os.symlink(target, self.fixture.suite_root / "fixture/link.txt")
        provider = self.fixture.provider()
        with self.assertRaisesRegex(RunnerError, "symlink"):
            self.runner(provider).run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.output("symlink"),
            )
        self.assertEqual(provider.agent_requests, [])


class HoldoutReleaseProtocolTests(unittest.TestCase):
    comparison_ids = ("candidate-vs-original", "candidate-vs-no-skill")

    def setUp(self) -> None:
        test_root = Path.home() / ".cache" / "skill-eval-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = SuiteFixture(Path(self.temporary.name))
        self.fixture.configure_holdout()
        self.suite = load_suite(self.fixture.manifest_path)
        self.provider = self.fixture.provider()
        seed_runner = self.production_runner(self.runner(production=False))
        runtime = seed_runner._load_comparator_runtime()
        self.payload = self.fixture.holdout_plan_payload()
        self.payload["comparator_release_sha256"] = runtime.release_summary[
            "release_sha256"
        ]
        self.payload["comparator_calibration_evidence_sha256"] = (
            runtime.certification.evidence_sha256
        )
        self.plan_path = self.fixture.save_holdout_plan(self.payload)
        seed_runner.close()

    def runner(self, suite=None, *, production: bool = True) -> EvalRunner:
        runner = EvalRunner(suite or self.suite, self.provider, self.provider)
        return self.production_runner(runner) if production else runner

    def production_runner(
        self,
        runner: EvalRunner | None = None,
        *,
        bypass_generator_authority: bool = True,
    ) -> EvalRunner:
        runner = runner or self.runner(production=False)
        runtime = runner._load_comparator_runtime()
        release = copy.deepcopy(runtime.bundle.release)
        release["test_release"] = False
        certification = replace(
            runtime.certification,
            valid=True,
            evidence_path=self.fixture.root / "live-evidence.json",
            evidence_sha256="c" * 64,
            result_sha256="d" * 64,
            actual_models=("fake-sonnet-v2.0",),
            executable_sha256="e" * 64,
            error=None,
        )
        runner._comparator_runtime = replace(
            runtime,
            bundle=replace(runtime.bundle, release=release),
            release_summary={
                **runtime.release_summary,
                "release_sha256": canonical_sha256(release),
            },
            certification=certification,
        )
        if bypass_generator_authority:
            patcher = patch.object(
                runner,
                "_assert_generator_release_authority",
                return_value=None,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        return runner

    def selection(self, **changes) -> RunSelection:
        values = {
            "split": "holdout",
            "comparison_ids": self.comparison_ids,
            "holdout_plan": self.plan_path,
        }
        values.update(changes)
        return RunSelection(**values)

    def binding_inputs(self):
        cases = self.suite.cases
        comparisons = self.suite.comparisons
        source_records = {
            "candidate": {"source_commit": self.fixture.treatment_commit},
            "original": {"source_commit": self.fixture.baseline_commit},
        }
        case_records = copy.deepcopy(self.payload["cases"])
        return cases, comparisons, source_records, case_records

    def objective_v5_suite(self) -> SuiteSpec:
        self.fixture.manifest["schema_version"] = 5
        self.fixture.manifest["evaluation_mode"] = "objective_only"
        self.fixture.manifest.pop("comparator")
        self.fixture.manifest.pop("comparator_profile", None)
        self.fixture.manifest["shared_verifier_dir"] = None
        self.fixture.manifest["holdout"] = {"comparison_ids": list(self.comparison_ids)}
        self.fixture.manifest["cases"] = [
            case
            for case in self.fixture.manifest["cases"]
            if case["skill"] == "engineering"
        ]
        for case in self.fixture.manifest["cases"]:
            case["bundle_source"] = "skills/engineering"
            case.pop("comparator_contract")
        self.fixture.save_manifest()
        return load_suite(self.fixture.manifest_path)

    def test_non_authoritative_generator_rejects_holdout_before_agent_turns(
        self,
    ) -> None:
        def forbidden_agent(_request):
            raise AssertionError("non-authoritative holdout reached an agent turn")

        suite = self.fixture.codex_suite(self.suite)
        provider = _SerializedCodexTestProvider(
            self.fixture.codex_protocol_lock,
            agent_handler=forbidden_agent,
        )
        runner = self.production_runner(
            EvalRunner(suite, provider, self.provider),
            bypass_generator_authority=False,
        )
        output = Path(self.temporary.name) / "must-not-exist"

        with self.assertRaisesRegex(
            RunnerError, "not authoritative for holdout release"
        ):
            runner.run(self.selection(), output_dir=output)

        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])
        self.assertFalse(output.exists())

    def test_injected_fake_generator_cannot_run_holdout_verifier_only(self) -> None:
        self.fixture.manifest["provider"] = {
            "kind": "claude",
            "executable": "claude",
            "model": "fake-model-v1",
            "max_budget_usd": 1.0,
            "timeout_seconds": 10,
        }
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = self.production_runner(EvalRunner(suite, provider, provider))
        output = Path(self.temporary.name) / "fake-holdout-must-not-exist"

        with self.assertRaisesRegex(
            RunnerError,
            "injected fake generator.*non-holdout verifier-only",
        ):
            runner.run(self.selection(verifier_only=True), output_dir=output)

        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])
        self.assertFalse(output.exists())

    def test_production_holdout_rejects_fake_and_injected_authority(self) -> None:
        fake_runner = self.production_runner(bypass_generator_authority=False)
        fake_output = self.fixture.root / "fake-production-plan.json"
        with self.assertRaisesRegex(RunnerError, "exact built-in Claude CLI generator"):
            fake_runner.prepare_holdout_plan(
                output_path=fake_output,
                plan_id="fake-production-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:fake-production-v1",
                seal_record="review:seal:fake-production-v1",
            )
        self.assertFalse(fake_runner._production_generator_release_authoritative())
        self.assertFalse(fake_output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

        class MasqueradingProvider:
            def __init__(self) -> None:
                self.agent_requests: list[AgentRequest] = []

            @property
            def name(self) -> str:
                return "claude-cli"

            @property
            def version(self) -> str:
                return "forged"

            @property
            def execution_policy(self) -> ProviderExecutionPolicy:
                return ProviderExecutionPolicy("concurrent", True)

            @property
            def executable_sha256(self) -> str:
                return "f" * 64

            def run_agent(self, request: AgentRequest) -> ProviderResult:
                self.agent_requests.append(request)
                raise AssertionError("masquerading provider reached agent execution")

            def run_comparator(self, _request):
                raise AssertionError("masquerading provider reached comparison")

        self.fixture.manifest["provider"] = {
            "kind": "claude",
            "executable": str(self.fixture.fake_codex),
            "model": "fake-model-v1",
            "max_budget_usd": 1.0,
            "timeout_seconds": 10,
        }
        self.fixture.save_manifest()
        claude_suite = load_suite(self.fixture.manifest_path)
        masquerader = MasqueradingProvider()
        injected_runner = EvalRunner(
            claude_suite,
            masquerader,
            self.provider,
        )
        self.production_runner(
            injected_runner,
            bypass_generator_authority=False,
        )
        injected_output = self.fixture.root / "injected-production-plan.json"
        with self.assertRaisesRegex(RunnerError, "exact built-in Claude CLI generator"):
            injected_runner.prepare_holdout_plan(
                output_path=injected_output,
                plan_id="injected-production-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:injected-production-v1",
                seal_record="review:seal:injected-production-v1",
            )
        self.assertFalse(injected_runner._production_generator_release_authoritative())
        self.assertEqual(masquerader.agent_requests, [])
        self.assertFalse(injected_output.exists())

        exact_injected = object.__new__(ClaudeCliProvider)
        exact_injected._config = claude_suite.provider
        exact_injected._version = "injected-exact-class"
        exact_injected._verified_executable = SimpleNamespace(sha256="f" * 64)
        exact_runner = EvalRunner(
            claude_suite,
            exact_injected,
            self.provider,
        )
        self.production_runner(
            exact_runner,
            bypass_generator_authority=False,
        )
        exact_output = self.fixture.root / "exact-injected-production-plan.json"
        with self.assertRaisesRegex(RunnerError, "exact built-in Claude CLI generator"):
            exact_runner.prepare_holdout_plan(
                output_path=exact_output,
                plan_id="exact-injected-production-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:exact-injected-production-v1",
                seal_record="review:seal:exact-injected-production-v1",
            )
        self.assertFalse(exact_runner._production_generator_release_authoritative())
        self.assertFalse(exact_output.exists())

    def test_manifest_built_claude_authority_is_reachable_without_transport(
        self,
    ) -> None:
        self.fixture.manifest["provider"] = {
            "kind": "claude",
            "executable": str(self.fixture.fake_codex),
            "model": "fake-model-v1",
            "max_budget_usd": 1.0,
            "timeout_seconds": 10,
        }
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)

        with (
            patch.object(
                ClaudeCliProvider,
                "_capture_version",
                return_value="claude-fixture-1",
            ) as capture_version,
            patch.object(
                ClaudeCliProvider,
                "_probe_sandbox",
                return_value="systemd-fixture-1",
            ) as probe_sandbox,
        ):
            runner = EvalRunner(suite, comparator_provider=self.provider)
        capture_version.assert_called_once_with()
        probe_sandbox.assert_called_once_with()
        self.addCleanup(runner.close)
        self.production_runner(runner, bypass_generator_authority=False)

        output = self.fixture.root / "manifest-built-production-plan.json"
        result = runner.prepare_holdout_plan(
            output_path=output,
            plan_id="manifest-built-production-v1",
            reviewers=("reviewer-a",),
            freeze_record="review:freeze:manifest-built-production-v1",
            seal_record="review:seal:manifest-built-production-v1",
        )
        expected_digest = hashlib.sha256(
            self.fixture.fake_codex.read_bytes()
        ).hexdigest()
        plan = load_holdout_plan(output)

        self.assertIs(type(runner.agent_provider), ClaudeCliProvider)
        self.assertFalse(runner._agent_provider_injected)
        self.assertIs(runner.agent_provider, runner._agent_provider_instance)
        self.assertIs(runner.agent_provider._config, suite.provider)
        self.assertTrue(runner._production_generator_release_authoritative())
        self.assertTrue(
            result["preflight"]["holdout_plan"]["production_release_authority_eligible"]
        )
        self.assertEqual(
            result["preflight"]["provider"]["executable_sha256"],
            expected_digest,
        )
        self.assertEqual(plan.generator_provider.executable_sha256, expected_digest)
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def release_pairs(self) -> list[dict[str, object]]:
        provider = {
            "actual_models": ["stable-model"],
            "cost_usd": 0.0,
            "tokens": {},
        }
        pairs: list[dict[str, object]] = []
        for comparison in self.suite.comparisons:
            for case in self.suite.cases:
                for repetition in range(3):
                    arm = {
                        "status": "completed",
                        "passed": True,
                        "critical_results": {
                            expectation: True
                            for expectation in case.critical_expectations
                        },
                        "provider": provider,
                    }
                    pairs.append(
                        {
                            "comparison_id": comparison.id,
                            "case_id": case.id,
                            "repetition": repetition,
                            "arms": {"control": arm, "treatment": arm},
                            "final_winner": "treatment",
                            "position_bias": False,
                            "completed": True,
                            "comparator_trials": [],
                        }
                    )
        return pairs

    def test_holdout_selection_rejects_every_filter_and_profile_bypass(self) -> None:
        runner = self.runner()
        runner._selected(self.selection())
        invalid = (
            (
                "explicit holdout plan",
                RunSelection(split="holdout", comparison_ids=self.comparison_ids),
            ),
            (
                "forbids case filters",
                self.selection(case_ids=(self.suite.cases[0].id,)),
            ),
            ("forbids seed overrides", self.selection(seed=self.suite.seed)),
            (
                "exactly the explicit comparisons",
                self.selection(comparison_ids=("candidate-vs-original",)),
            ),
            (
                "exactly the explicit comparisons",
                self.selection(comparison_ids=tuple(reversed(self.comparison_ids))),
            ),
            (
                "exactly the explicit comparisons",
                self.selection(
                    comparison_ids=("candidate-vs-original",), verifier_only=True
                ),
            ),
        )
        for message, selection in invalid:
            with self.subTest(message=message, selection=selection):
                with self.assertRaisesRegex(RunnerError, message):
                    runner._selected(selection)

        wrong_semantics = replace(
            self.suite,
            comparisons=(
                replace(self.suite.comparisons[0], control="no-skill"),
                self.suite.comparisons[1],
            ),
        )
        with self.assertRaisesRegex(RunnerError, "semantics differ"):
            self.runner(wrong_semantics)._selected(self.selection())

        wrong_variants = replace(
            self.suite,
            variants=tuple(
                replace(variant, kind="git_ref")
                if variant.id == "candidate"
                else variant
                for variant in self.suite.variants
            ),
        )
        with self.assertRaisesRegex(RunnerError, "variant kinds differ"):
            self.runner(wrong_variants)._selected(self.selection())

        stale_candidate = replace(
            self.suite,
            variants=tuple(
                replace(variant, source_ref=self.fixture.treatment_commit)
                if variant.id == "candidate"
                else variant
                for variant in self.suite.variants
            ),
        )
        with self.assertRaisesRegex(RunnerError, "dynamically.*HEAD"):
            self.runner(stale_candidate)._selected(self.selection())

        too_few = replace(
            self.suite,
            cases=tuple(
                case for case in self.suite.cases if case.id != "engineering-0"
            ),
        )
        with self.assertRaisesRegex(RunnerError, "at least 8 cases"):
            self.runner(too_few)._selected(self.selection())

        wrong_skill = replace(
            self.suite,
            cases=(replace(self.suite.cases[0], skill="demo"), *self.suite.cases[1:]),
        )
        with self.assertRaisesRegex(RunnerError, "at least 8 cases"):
            self.runner(wrong_skill)._selected(self.selection())

    def test_holdout_release_scope_is_derived_from_selected_skills(self) -> None:
        self.fixture.configure_holdout(("demo",))
        self.suite = load_suite(self.fixture.manifest_path)
        self.payload = self.fixture.holdout_plan_payload()
        self.plan_path = self.fixture.save_holdout_plan(
            self.payload, name="demo-holdout-plan.json"
        )
        selection = self.selection()
        runner = self.runner()

        cases, _comparisons = runner._selected(selection)
        self.assertEqual({case.skill for case in cases}, {"demo"})

        holdout_plan = load_holdout_plan(self.plan_path)
        aggregate = _aggregate(
            self.release_pairs(),  # type: ignore[arg-type]
            self.suite,
            self.suite.comparisons,
            selection,
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        self.assertEqual(
            set(aggregate["by_comparison_skill"]["candidate-vs-original"]),
            {"demo"},
        )
        self.assertTrue(aggregate["final_release_authorized"])

    def test_holdout_preflight_rejects_release_baseline_mismatch_and_same_candidate(
        self,
    ) -> None:
        mismatched_runner = self.runner()
        runtime = mismatched_runner._load_comparator_runtime()
        mismatched_release = copy.deepcopy(runtime.bundle.release)
        mismatched_release["runtime_adapter"]["frozen_original_commit"] = "1" * 40
        mismatched_runner._comparator_runtime = replace(
            runtime,
            bundle=replace(runtime.bundle, release=mismatched_release),
        )
        with self.assertRaisesRegex(RunnerError, "release-owned frozen original"):
            mismatched_runner.preflight(self.selection())

        original_variant = next(
            variant
            for variant in self.fixture.manifest["variants"]
            if variant["id"] == "original"
        )
        original_variant["git_ref"] = self.fixture.treatment_commit
        self.fixture.save_manifest()
        same_suite = load_suite(self.fixture.manifest_path)
        same_runner = self.runner(same_suite, production=False)
        same_release = copy.deepcopy(runtime.bundle.release)
        same_release["runtime_adapter"]["frozen_original_commit"] = (
            self.fixture.treatment_commit
        )
        same_runner._comparator_runtime = replace(
            runtime,
            bundle=replace(runtime.bundle, release=same_release),
        )
        with (
            patch.object(
                same_runner, "_assert_generator_release_authority", return_value=None
            ),
            self.assertRaisesRegex(RunnerError, "must differ"),
        ):
            same_runner.preflight(self.selection())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_suite_only_alternate_baseline_fails_before_plan_write(self) -> None:
        original = next(
            variant
            for variant in self.fixture.manifest["variants"]
            if variant["id"] == "original"
        )
        original["git_ref"] = self.fixture.treatment_commit
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        output = self.fixture.root / "alternate-baseline-plan.json"
        with self.assertRaisesRegex(RunnerError, "baseline authority"):
            self.runner(suite).prepare_holdout_plan(
                output_path=output,
                plan_id="alternate-baseline-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:alternate-baseline-v1",
                seal_record="review:seal:alternate-baseline-v1",
            )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_holdout_rejects_pseudoreplication_by_tree_and_fingerprint(self) -> None:
        duplicate_tree = copy.deepcopy(self.payload)
        duplicate_tree["cases"][1]["case_tree_sha256"] = duplicate_tree["cases"][0][
            "case_tree_sha256"
        ]
        duplicate_tree_plan = self.fixture.save_holdout_plan(
            duplicate_tree, name="duplicate-tree-plan.json"
        )
        with self.assertRaisesRegex(HoldoutPlanError, "globally unique"):
            load_holdout_plan(duplicate_tree_plan)

        duplicate_fingerprint = copy.deepcopy(self.payload)
        duplicate_fingerprint["cases"][1]["release_case_fingerprint"] = (
            duplicate_fingerprint["cases"][0]["release_case_fingerprint"]
        )
        duplicate_fingerprint_plan = self.fixture.save_holdout_plan(
            duplicate_fingerprint, name="duplicate-fingerprint-plan.json"
        )
        with self.assertRaisesRegex(HoldoutPlanError, "globally unique"):
            load_holdout_plan(duplicate_fingerprint_plan)

        first = self.fixture.manifest["cases"][0]
        second = self.fixture.manifest["cases"][1]
        first_root = self.fixture.suite_root / Path(first["prompt_file"]).parent
        second_root = self.fixture.suite_root / Path(second["prompt_file"]).parent
        (second_root / "prompt.md").write_bytes((first_root / "prompt.md").read_bytes())
        shutil.rmtree(second_root / "fixture")
        shutil.copytree(first_root / "fixture", second_root / "fixture")
        shutil.rmtree(second_root / "oracle")
        shutil.copytree(first_root / "oracle", second_root / "oracle")
        nonce = second_root / "calibration" / "irrelevant-review-nonce.txt"
        nonce.parent.mkdir()
        nonce.write_text("renamed-copy-nonce\n", encoding="utf-8")
        self.fixture.save_manifest()
        duplicated_bindings = self.fixture.holdout_plan_payload()["cases"]
        self.assertNotEqual(
            duplicated_bindings[0]["case_tree_sha256"],
            duplicated_bindings[1]["case_tree_sha256"],
        )
        self.assertEqual(
            duplicated_bindings[0]["release_case_fingerprint"],
            duplicated_bindings[1]["release_case_fingerprint"],
        )
        duplicated_suite = load_suite(self.fixture.manifest_path)
        with self.assertRaisesRegex(RunnerError, "task-content.*pseudoreplication"):
            self.runner(duplicated_suite).preflight(self.selection())
        output = self.fixture.root / "duplicate-task-plan.json"
        with self.assertRaisesRegex(RunnerError, "task-content.*pseudoreplication"):
            self.production_runner().prepare_holdout_plan(
                output_path=output,
                plan_id="duplicate-task-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:duplicate-task-v1",
                seal_record="review:seal:duplicate-task-v1",
            )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_task_fingerprint_includes_only_canonical_task_content(self) -> None:
        case = self.suite.cases[0]
        prompt_sha256 = hashlib.sha256(
            case.prompt_file.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        fixture_sha256 = _tree_hash(case.fixture_dir, ignore_generated_caches=True)
        context_hashes = _release_context_content_hashes(
            self.suite.repository_root,
            case,
            {
                "candidate": self.fixture.treatment_commit,
                "original": self.fixture.baseline_commit,
            },
        )

        def fingerprint(
            candidate=case,
            *,
            prompt=prompt_sha256,
            fixture=fixture_sha256,
            contexts=context_hashes,
        ):
            return _release_case_fingerprint(
                candidate,
                prompt_sha256=prompt,
                fixture_sha256=fixture,
                context_content_sha256s=contexts,
            )

        baseline = fingerprint()
        reordered_contract = copy.deepcopy(case.comparator_contract)
        reordered_contract["requirements"].reverse()
        excluded = (
            replace(case, id="renamed-case"),
            replace(
                case,
                prompt_file=case.prompt_file.with_name("renamed-prompt.md"),
                fixture_dir=case.fixture_dir.with_name("renamed-fixture"),
                context_files=(type(case.context_files[0])("renamed/context.md"),),
            ),
            replace(case, timeout_seconds=case.timeout_seconds + 1),
            replace(
                case,
                verifier=replace(
                    case.verifier,
                    argv=("python3", "different/oracle.py"),
                    timeout_seconds=case.verifier.timeout_seconds + 1,
                    required_tools=("different-tool",),
                ),
            ),
            replace(
                case, critical_expectations=tuple(reversed(case.critical_expectations))
            ),
            replace(case, comparator_contract=reordered_contract),
        )
        for mutated in excluded:
            with self.subTest(excluded=mutated):
                self.assertEqual(fingerprint(mutated), baseline)
        reversed_contexts = {
            role: list(reversed(hashes))
            for role, hashes in reversed(tuple(context_hashes.items()))
        }
        self.assertEqual(fingerprint(contexts=reversed_contexts), baseline)

        two_requirements = copy.deepcopy(case.comparator_contract)
        two_requirements["requirements"].append(
            {
                "id": "second-requirement",
                "kind": "required_behavior",
                "text": "A second semantic requirement used only to test ordering.",
            }
        )
        ordered = replace(
            case,
            comparator_contract=two_requirements,
            critical_expectations=("answer-present", "second-requirement"),
        )
        reversed_requirements = copy.deepcopy(two_requirements)
        reversed_requirements["requirements"].reverse()
        reordered = replace(
            ordered,
            comparator_contract=reversed_requirements,
            critical_expectations=tuple(reversed(ordered.critical_expectations)),
        )
        self.assertEqual(fingerprint(ordered), fingerprint(reordered))

        changed_contract = copy.deepcopy(case.comparator_contract)
        changed_contract["requirements"][0]["text"] += " Changed semantics."
        included = (
            fingerprint(replace(case, skill="different-skill")),
            fingerprint(prompt="a" * 64),
            fingerprint(fixture="b" * 64),
            fingerprint(
                contexts={
                    **context_hashes,
                    "candidate": ["c" * 64],
                }
            ),
            fingerprint(replace(case, comparator_contract=changed_contract)),
            fingerprint(
                replace(
                    case,
                    critical_expectations=(
                        *case.critical_expectations,
                        "new-critical-expectation",
                    ),
                )
            ),
        )
        self.assertTrue(all(value != baseline for value in included))

    def test_oracle_drift_keeps_task_key_but_breaks_sealed_tree_integrity(self) -> None:
        before = self.fixture.holdout_plan_payload()
        oracle = (
            self.fixture.suite_root
            / self.fixture.manifest["cases"][0]["verifier"]["argv"][1]
        )
        oracle.write_text(
            oracle.read_text(encoding="utf-8") + "\n# drift\n",
            encoding="utf-8",
        )
        after = self.fixture.holdout_plan_payload()
        self.assertEqual(
            before["cases"][0]["release_case_fingerprint"],
            after["cases"][0]["release_case_fingerprint"],
        )
        self.assertNotEqual(
            before["cases"][0]["case_tree_sha256"],
            after["cases"][0]["case_tree_sha256"],
        )
        self.assertEqual(
            before["cases"][0]["shared_tree_sha256"],
            after["cases"][0]["shared_tree_sha256"],
        )
        with self.assertRaisesRegex(RunnerError, "cases do not exactly match"):
            self.runner().preflight(self.selection())

    def test_shared_drift_keeps_task_key_but_breaks_sealed_tree_integrity(self) -> None:
        before = self.fixture.holdout_plan_payload()
        self.fixture._write_suite(
            "cases/testing/_shared/helper.py", "SHARED_SENTINEL = 1\n"
        )
        after = self.fixture.holdout_plan_payload()
        self.assertEqual(
            [case["release_case_fingerprint"] for case in before["cases"]],
            [case["release_case_fingerprint"] for case in after["cases"]],
        )
        self.assertEqual(
            [case["case_tree_sha256"] for case in before["cases"]],
            [case["case_tree_sha256"] for case in after["cases"]],
        )
        self.assertNotEqual(
            [case["shared_tree_sha256"] for case in before["cases"]],
            [case["shared_tree_sha256"] for case in after["cases"]],
        )
        with self.assertRaisesRegex(RunnerError, "cases do not exactly match"):
            self.runner().preflight(self.selection())

    def test_prepare_holdout_plan_writes_private_external_proved_plan_without_models(
        self,
    ) -> None:
        self.provider.executable_sha256 = "f" * 64
        runner = self.production_runner()
        output = self.fixture.root / "prepared-holdout.json"
        result = runner.prepare_holdout_plan(
            output_path=output,
            plan_id="prepared-holdout-v1",
            reviewers=("reviewer-a", "reviewer-b"),
            freeze_record="review:freeze:prepared-holdout-v1",
            seal_record="review:seal:prepared-holdout-v1",
        )

        self.assertTrue(result["binding_verified"])
        self.assertEqual(result["plan_path"], str(output.resolve()))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(
            hashlib.sha256(output.read_bytes()).hexdigest(), result["plan_sha256"]
        )
        plan = load_holdout_plan(output)
        self.assertEqual(plan.schema_version, 3)
        bindings = {binding.variant_id: binding for binding in plan.source_bindings}
        self.assertEqual(
            bindings["candidate"].source_commit, self.fixture.treatment_commit
        )
        self.assertEqual(
            bindings["original"].source_commit, self.fixture.baseline_commit
        )
        self.assertIsNone(bindings["no-skill"].source_commit)
        self.assertEqual(
            {binding.kind for binding in plan.source_bindings},
            {"git_ref", "without_skill", "worktree"},
        )
        self.assertTrue(
            all(
                tuple(case_id for case_id, _digest in binding.source_sha256_by_case)
                == tuple(sorted(case.id for case in plan.cases))
                for binding in plan.source_bindings
            )
        )
        self.assertEqual(
            plan.consumption_record_path,
            output.with_name(f"{output.name}.consumption.json"),
        )
        self.assertFalse(plan.consumption_record_path.exists())
        self.assertEqual(len({case.case_tree_sha256 for case in plan.cases}), 16)
        self.assertEqual(
            len({case.release_case_fingerprint for case in plan.cases}), 16
        )
        self.assertEqual(plan.comparator_calibration_evidence_sha256, "c" * 64)
        self.assertEqual(plan.generator_provider.version, self.provider.version)
        self.assertEqual(plan.generator_provider.requested_model, "fake-model-v1")
        self.assertEqual(plan.generator_provider.executable_sha256, "f" * 64)
        self.assertIsNone(plan.generator_provider.reasoning_effort)
        self.assertEqual(plan.generator_provider.billing_basis, "metered_api")
        self.assertIsNone(plan.generator_provider.protocol_lock)
        self.assertIsNone(plan.generator_provider.protocol_lock_sha256)
        self.assertEqual(
            plan.generator_provider.execution_policy,
            {"concurrency": "concurrent", "release_authoritative": True},
        )
        self.assertEqual(
            result["preflight"]["holdout_plan"]["sha256"], result["plan_sha256"]
        )
        self.assertFalse(
            result["preflight"]["holdout_plan"]["production_release_authority_eligible"]
        )
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

        self.provider.executable_sha256 = "a" * 64
        with self.assertRaisesRegex(RunnerError, "provider binding"):
            runner.preflight(self.selection(holdout_plan=output))

    def test_prepare_holdout_plan_rejects_unsafe_outputs_before_model_calls(
        self,
    ) -> None:
        runner = self.production_runner()
        existing = self.fixture.root / "existing-plan.json"
        existing.write_text("occupied", encoding="utf-8")
        existing.chmod(0o600)
        inside_suite = self.fixture.suite_root / "new-plan.json"
        for output, message in (
            (existing, "must not already exist"),
            (inside_suite, "external to the evaluation suite"),
        ):
            with self.subTest(output=output):
                with self.assertRaisesRegex(RunnerError, message):
                    runner.prepare_holdout_plan(
                        output_path=output,
                        plan_id="unsafe-output-v1",
                        reviewers=("reviewer-a",),
                        freeze_record="review:freeze:unsafe-output-v1",
                        seal_record="review:seal:unsafe-output-v1",
                    )
        self.assertFalse(inside_suite.exists())

        consumed_output = self.fixture.root / "already-consumed-plan.json"
        consumed_record = consumed_output.with_name(
            f"{consumed_output.name}.consumption.json"
        )
        consumed_record.write_text("claimed\n", encoding="utf-8")
        consumed_record.chmod(0o600)
        with self.assertRaisesRegex(RunnerError, "already been consumed"):
            runner.prepare_holdout_plan(
                output_path=consumed_output,
                plan_id="consumed-output-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:consumed-output-v1",
                seal_record="review:seal:consumed-output-v1",
            )
        self.assertFalse(consumed_output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_schema_v5_prepares_generic_source_bindings_without_reserved_ids(
        self,
    ) -> None:
        external_worktree = self.fixture.root / "external-treatment"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                str(self.fixture.repository),
                str(external_worktree),
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(external_worktree),
                "config",
                "user.email",
                "external@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(external_worktree),
                "config",
                "user.name",
                "External Tests",
            ],
            check=True,
        )
        for skill in ("engineering", "testing"):
            path = external_worktree / f"skills/{skill}/SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8") + "\nExternal treatment.\n",
                encoding="utf-8",
            )
        subprocess.run(
            ["git", "-C", str(external_worktree), "add", "skills"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(external_worktree),
                "commit",
                "-q",
                "-m",
                "external treatment",
            ],
            check=True,
        )
        self.fixture.manifest["schema_version"] = 5
        self.fixture.manifest["evaluation_mode"] = "judged"
        self.fixture.manifest["comparator_profile"] = {
            "kind": "builtin",
            "id": "software-engineering-v2.3",
        }
        self.fixture.manifest["shared_verifier_dir"] = None
        variant_ids = {
            "no-skill": "blank",
            "original": "reference",
            "candidate": "treatment",
        }
        for variant in self.fixture.manifest["variants"]:
            variant["id"] = variant_ids[variant["id"]]
            if variant["id"] == "treatment":
                variant["root"] = Path(
                    os.path.relpath(external_worktree, self.fixture.suite_root)
                ).as_posix()
        comparison_ids = ("reference-treatment", "blank-treatment")
        for comparison, comparison_id in zip(
            self.fixture.manifest["comparisons"], comparison_ids, strict=True
        ):
            comparison["id"] = comparison_id
            comparison["control"] = variant_ids[comparison["control"]]
            comparison["treatment"] = variant_ids[comparison["treatment"]]
        self.fixture.manifest["holdout"] = {"comparison_ids": list(comparison_ids)}
        for case in self.fixture.manifest["cases"]:
            case["bundle_source"] = f"skills/{case['skill']}"
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        runner = self.production_runner(self.runner(suite, production=False))
        output = self.fixture.root / "generic-source-plan.json"

        result = runner.prepare_holdout_plan(
            output_path=output,
            plan_id="generic-source-v1",
            reviewers=("reviewer-a",),
            freeze_record="review:freeze:generic-source-v1",
            seal_record="review:seal:generic-source-v1",
        )
        plan = load_holdout_plan(output)
        schema = json.loads(
            (HARNESS_ROOT / "holdout-plan.schema.json").read_text(encoding="utf-8")
        )

        self.assertEqual(plan.schema_version, 3)
        self.assertFalse(
            list(
                Draft202012Validator(schema).iter_errors(
                    json.loads(output.read_text(encoding="utf-8"))
                )
            )
        )
        self.assertEqual(
            tuple(binding.variant_id for binding in plan.source_bindings),
            ("blank", "reference", "treatment"),
        )
        bindings = {
            binding.variant_id: binding.as_json() for binding in plan.source_bindings
        }
        self.assertIsNone(bindings["blank"]["source_commit"])
        self.assertEqual(
            set(bindings["blank"]["source_sha256_by_case"].values()),
            {runner_module.EMPTY_SOURCE_SHA256},
        )
        self.assertEqual(
            result["preflight"]["selection"]["comparison_ids"],
            list(comparison_ids),
        )
        for case in suite.cases:
            self.assertNotEqual(
                bindings["reference"]["source_sha256_by_case"][case.id],
                bindings["treatment"]["source_sha256_by_case"][case.id],
            )

    def test_schema_v5_objective_holdout_seals_policy_without_comparator(self) -> None:
        suite = self.objective_v5_suite()
        runner = EvalRunner(suite)
        self.addCleanup(runner.close)

        def treatment_only(request):
            if request.variant_id == "candidate":
                (request.workspace / "answer.txt").write_text(
                    "accepted treatment", encoding="utf-8"
                )
            return "objective result"

        runner.agent_provider._agent_handler = treatment_only
        output = self.fixture.root / "objective-holdout-plan.json"
        result_dir = self.fixture.root / "objective-holdout-result"
        with (
            patch.object(
                runner, "_assert_generator_release_authority", return_value=None
            ),
            patch.object(
                runner,
                "_production_generator_release_authoritative",
                return_value=True,
            ),
        ):
            prepared = runner.prepare_holdout_plan(
                output_path=output,
                plan_id="objective-holdout-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:objective-holdout-v1",
                seal_record="review:seal:objective-holdout-v1",
            )
            result = runner.run(
                RunSelection(
                    split="holdout",
                    comparison_ids=self.comparison_ids,
                    holdout_plan=output,
                ),
                output_dir=result_dir,
            )

        plan = load_holdout_plan(output)
        self.assertEqual(plan.evaluation_mode, "objective_only")
        self.assertIsNone(plan.comparator_release_sha256)
        self.assertEqual(
            plan.objective_acceptance_policy_id,
            runner_module._OBJECTIVE_ACCEPTANCE_POLICY["policy_id"],
        )
        self.assertEqual(
            plan.objective_acceptance_policy_sha256,
            runner_module._OBJECTIVE_ACCEPTANCE_POLICY_SHA256,
        )
        self.assertIsNone(prepared["preflight"]["comparator"])
        self.assertNotIn(
            "comparator_release_sha256", prepared["preflight"]["holdout_plan"]
        )
        self.assertTrue(result["aggregate"]["final_release_authorized"])
        self.assertTrue(result["passed"])
        self.assertNotIn(
            "comparator-spend", {path.name for path in result_dir.iterdir()}
        )

    def test_generic_holdout_rejects_cross_authority_substitution(self) -> None:
        suite = self.objective_v5_suite()
        runner = EvalRunner(suite)
        self.addCleanup(runner.close)
        output = self.fixture.root / "objective-authority-plan.json"
        with (
            patch.object(
                runner, "_assert_generator_release_authority", return_value=None
            ),
            patch.object(
                runner,
                "_production_generator_release_authoritative",
                return_value=True,
            ),
        ):
            runner.prepare_holdout_plan(
                output_path=output,
                plan_id="objective-authority-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:objective-authority-v1",
                seal_record="review:seal:objective-authority-v1",
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            first_case = sorted(payload["source_bindings"][0]["source_sha256_by_case"])[
                0
            ]
            mutations = (
                (
                    "objective acceptance authority",
                    lambda value: value.__setitem__(
                        "objective_acceptance_policy_sha256", "f" * 64
                    ),
                ),
                (
                    "source bindings",
                    lambda value: value["source_bindings"][0][
                        "source_sha256_by_case"
                    ].__setitem__(first_case, "e" * 64),
                ),
                (
                    "invalid holdout plan",
                    lambda value: value.__setitem__(
                        "comparator_release_sha256", "d" * 64
                    ),
                ),
                (
                    "invalid holdout plan",
                    lambda value: value["source_bindings"].reverse(),
                ),
            )
            for index, (message, mutate) in enumerate(mutations):
                with self.subTest(mutation=message):
                    invalid = copy.deepcopy(payload)
                    mutate(invalid)
                    plan_path = self.fixture.save_holdout_plan(
                        invalid, name=f"objective-authority-invalid-{index}.json"
                    )
                    with self.assertRaisesRegex(RunnerError, message):
                        runner.preflight(
                            RunSelection(
                                split="holdout",
                                comparison_ids=self.comparison_ids,
                                holdout_plan=plan_path,
                            )
                        )
        self.assertEqual(runner.agent_provider.agent_requests, [])

    def test_holdout_consumption_is_one_shot_across_roots_copies_and_crashes(
        self,
    ) -> None:
        runner = self.runner()
        first_output = self.fixture.root / "consumed-result-a"
        with patch.object(
            runner,
            "_run_pair",
            side_effect=RuntimeError("simulated post-claim crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "post-claim crash"):
                runner.run(
                    self.selection(verifier_only=True),
                    output_dir=first_output,
                )

        record_path = Path(self.payload["consumption_record_path"])
        self.assertTrue(record_path.is_file())
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        record = json.loads(record_path.read_text(encoding="ascii"))
        self.assertEqual(
            record["plan_sha256"], load_holdout_plan(self.plan_path).sha256
        )
        self.assertEqual(record["manifest_sha256"], self.suite.manifest_hash)
        self.assertEqual(record["candidate_commit"], self.fixture.treatment_commit)
        self.assertEqual(record["original_commit"], self.fixture.baseline_commit)
        self.assertEqual(record["result_root"], str(first_output.resolve()))

        second_output = self.fixture.root / "consumed-result-b"
        with self.assertRaisesRegex(RunnerError, "already been consumed"):
            self.runner().run(
                self.selection(verifier_only=True),
                output_dir=second_output,
            )
        self.assertFalse(second_output.exists())

        copied_plan = self.fixture.root / "renamed-plan-copy.json"
        shutil.copyfile(self.plan_path, copied_plan)
        copied_plan.chmod(0o600)
        with self.assertRaisesRegex(RunnerError, "already been consumed"):
            self.runner().run(
                self.selection(
                    holdout_plan=copied_plan,
                    verifier_only=True,
                ),
                output_dir=self.fixture.root / "consumed-result-copy",
            )
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_holdout_consumption_claim_is_atomic_under_concurrency(self) -> None:
        plan = load_holdout_plan(self.plan_path)
        roots = []
        for name in ("concurrent-a", "concurrent-b"):
            root = self.fixture.root / name
            root.mkdir(mode=0o700)
            roots.append(root.resolve())
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        outcome_lock = threading.Lock()

        def claim(result_root: Path) -> None:
            barrier.wait(timeout=3)
            try:
                _claim_holdout_consumption(plan, self.suite, result_root)
            except RunnerError:
                outcome = "rejected"
            else:
                outcome = "claimed"
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=claim, args=(root,)) for root in roots]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["claimed", "rejected"])
        record_path = Path(self.payload["consumption_record_path"])
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        self.assertIn(
            json.loads(record_path.read_text(encoding="ascii"))["result_root"],
            {str(root) for root in roots},
        )

    def test_partial_consumption_record_write_still_burns_attempt(self) -> None:
        plan = load_holdout_plan(self.plan_path)
        result_root = self.fixture.root / "partial-claim-result"
        result_root.mkdir(mode=0o700)
        with patch(
            "harness_evals.runner.os.write", side_effect=OSError("simulated crash")
        ):
            with self.assertRaisesRegex(RunnerError, "persist.*claim"):
                _claim_holdout_consumption(plan, self.suite, result_root.resolve())
        record_path = Path(self.payload["consumption_record_path"])
        self.assertTrue(record_path.exists())
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(RunnerError, "already been consumed"):
            _claim_holdout_consumption(plan, self.suite, result_root.resolve())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_consumption_registry_rejects_symlinks_unsafe_parents_and_cleans_root(
        self,
    ) -> None:
        record_path = Path(self.payload["consumption_record_path"])
        victim = self.fixture.root / "consumption-victim.json"
        victim.write_text("unclaimed\n", encoding="utf-8")
        victim.chmod(0o600)
        record_path.symlink_to(victim)
        with self.assertRaisesRegex(RunnerError, "already been consumed"):
            self.runner().preflight(self.selection())
        record_path.unlink()

        unsafe_parent = self.fixture.root / "unsafe-registry"
        unsafe_parent.mkdir(mode=0o755)
        unsafe_parent.chmod(0o755)
        unsafe_payload = copy.deepcopy(self.payload)
        unsafe_payload["consumption_record_path"] = str(
            (unsafe_parent / "claim.json").resolve()
        )
        unsafe_plan = self.fixture.save_holdout_plan(
            unsafe_payload, name="unsafe-registry-plan.json"
        )
        with self.assertRaisesRegex(RunnerError, "mode-0700"):
            self.runner().preflight(self.selection(holdout_plan=unsafe_plan))

        external = self.fixture.root / "real-registry"
        external.mkdir(mode=0o700)
        linked_parent = self.fixture.root / "linked-registry"
        linked_parent.symlink_to(external, target_is_directory=True)
        linked_payload = copy.deepcopy(self.payload)
        linked_payload["consumption_record_path"] = str(linked_parent / "claim.json")
        linked_plan = self.fixture.save_holdout_plan(
            linked_payload, name="linked-registry-plan.json"
        )
        with self.assertRaisesRegex(RunnerError, "non-symlink"):
            self.runner().preflight(self.selection(holdout_plan=linked_plan))
        self.assertEqual(list(external.iterdir()), [])

        runner = self.runner()
        output = self.fixture.root / "claim-race-result"
        real_preflight = runner.preflight

        def consume_after_preflight(selection: RunSelection) -> dict[str, object]:
            proof = real_preflight(selection)
            record_path = Path(self.payload["consumption_record_path"])
            record_path.write_text("raced\n", encoding="utf-8")
            record_path.chmod(0o600)
            return proof

        with patch.object(runner, "preflight", side_effect=consume_after_preflight):
            with self.assertRaisesRegex(RunnerError, "already been consumed"):
                runner.run(
                    self.selection(verifier_only=True),
                    output_dir=output,
                )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_prepare_holdout_plan_requires_valid_live_calibration(self) -> None:
        output = self.fixture.root / "uncalibrated-plan.json"
        with self.assertRaisesRegex(RunnerError, "test comparator release"):
            self.runner(production=False).prepare_holdout_plan(
                output_path=output,
                plan_id="uncalibrated-plan-v1",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:uncalibrated-plan-v1",
                seal_record="review:seal:uncalibrated-plan-v1",
            )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_prepare_holdout_plan_rejects_invalid_provenance_cleanly(self) -> None:
        runner = self.production_runner()
        output = self.fixture.root / "invalid-provenance-plan.json"
        with self.assertRaisesRegex(RunnerError, "prepared holdout plan is invalid"):
            runner.prepare_holdout_plan(
                output_path=output,
                plan_id="INVALID-PLAN-ID",
                reviewers=("reviewer-a",),
                freeze_record="review:freeze:invalid-plan-v1",
                seal_record="review:seal:invalid-plan-v1",
            )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_prepare_holdout_plan_removes_output_when_normal_preflight_disagrees(
        self,
    ) -> None:
        runner = self.production_runner()
        output = self.fixture.root / "drifted-plan.json"
        normal_preflight = runner.preflight

        def drift_then_preflight(selection):
            output.write_bytes(output.read_bytes() + b"\n")
            return normal_preflight(selection)

        with patch.object(runner, "preflight", side_effect=drift_then_preflight):
            with self.assertRaisesRegex(RunnerError, "changed before normal preflight"):
                runner.prepare_holdout_plan(
                    output_path=output,
                    plan_id="drift-proof-v1",
                    reviewers=("reviewer-a",),
                    freeze_record="review:freeze:drift-proof-v1",
                    seal_record="review:seal:drift-proof-v1",
                )
        self.assertFalse(output.exists())
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_prepare_cli_has_no_selection_or_normal_run_draft_escape(self) -> None:
        parser = build_prepare_parser()
        required = [
            "--output",
            str(self.fixture.root / "cli-plan.json"),
            "--plan-id",
            "cli-plan-v1",
            "--reviewer",
            "reviewer-a",
            "--freeze-record",
            "review:freeze:cli-plan-v1",
            "--seal-record",
            "review:seal:cli-plan-v1",
        ]
        for forbidden in ("--case", "--seed", "--comparison"):
            with self.subTest(forbidden=forbidden):
                with patch("sys.stderr", new=io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args([*required, forbidden, "value"])

        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--prepare-holdout"])

        runner = self.production_runner()
        output = self.fixture.root / "cli-plan.json"
        with (
            patch("harness_evals.holdout_cli.EvalRunner", return_value=runner),
            patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            exit_code = prepare_holdout_main(
                [
                    "--suite",
                    str(self.fixture.manifest_path),
                    *required,
                ]
            )
        self.assertEqual(exit_code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertTrue(summary["binding_verified"])
        self.assertEqual(summary["file_mode"], "0600")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertTrue(runner._closed)
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_valid_holdout_dry_run_binds_plan_and_has_reachable_budgets(self) -> None:
        output = Path(self.temporary.name) / "must-not-exist"
        result = self.runner().run(
            self.selection(),
            output_dir=output,
            dry_run=True,
        )

        self.assertFalse(output.exists())
        self.assertEqual(result["planned_pair_runs"], 96)
        preflight = result["preflight"]
        self.assertEqual(
            preflight["holdout_plan"]["sha256"],
            hashlib.sha256(self.plan_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            preflight["holdout_plan"]["provenance"]["privacy_claim"],
            "not-a-cryptographic-privacy-proof",
        )
        self.assertEqual(
            preflight["holdout_plan"]["comparator_release_sha256"],
            preflight["comparator"]["release_sha256"],
        )
        self.assertEqual(
            preflight["holdout_plan"]["comparator_calibration_evidence_sha256"],
            "c" * 64,
        )
        self.assertEqual(
            preflight["holdout_plan"]["manifest_bound_models"],
            {"generator": "fake-model-v1", "comparator": "fake-sonnet-v2"},
        )
        self.assertFalse(
            preflight["holdout_plan"]["test_release_without_live_certification"]
        )
        self.assertFalse(
            preflight["holdout_plan"]["production_release_authority_eligible"]
        )
        plan = preflight["plan"]
        self.assertEqual(plan["maximum_agent_exposure_usd"], 288.0)
        self.assertEqual(plan["maximum_comparator_exposure_usd"], 192.0)
        self.assertEqual(plan["maximum_combined_exposure_usd"], 480.0)
        self.assertEqual(plan["total_comparator_run_cap_usd"], 200.0)
        self.assertEqual(
            [item["maximum_comparator_exposure_usd"] for item in plan["by_comparison"]],
            [96.0, 96.0],
        )
        self.assertTrue(
            all(
                item["maximum_comparator_exposure_usd"]
                <= item["comparator_run_cap_usd"]
                for item in plan["by_comparison"]
            )
        )
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_holdout_dry_run_rejects_first_unreachable_case_count(self) -> None:
        extra_case = copy.deepcopy(self.fixture.manifest["cases"][0])
        case_root = "holdout/engineering/8"
        self.fixture._write_suite(
            f"{case_root}/prompt.md", "Fix independent software case 8.\n"
        )
        self.fixture._write_suite(
            f"{case_root}/fixture/input.txt", "independent-software-8\n"
        )
        self.fixture._write_suite(f"{case_root}/oracle/verifier.py", _PASSING_VERIFIER)
        extra_case["id"] = "engineering-8"
        extra_case["prompt_file"] = f"{case_root}/prompt.md"
        extra_case["fixture_dir"] = f"{case_root}/fixture"
        extra_case["verifier"]["argv"] = [
            "python3",
            f"{case_root}/oracle/verifier.py",
        ]
        self.fixture.manifest["cases"].append(extra_case)
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        plan_payload = self.fixture.holdout_plan_payload()
        plan_payload["comparator_release_sha256"] = self.payload[
            "comparator_release_sha256"
        ]
        plan_payload["comparator_calibration_evidence_sha256"] = self.payload[
            "comparator_calibration_evidence_sha256"
        ]
        plan_path = self.fixture.save_holdout_plan(
            plan_payload, name="seventeen-case-plan.json"
        )
        runner = self.runner(suite)

        with self.assertRaisesRegex(
            RunnerError, "exceeds the per-comparison release cap"
        ):
            runner.run(
                self.selection(holdout_plan=plan_path),
                output_dir=None,
                dry_run=True,
            )
        self.assertEqual(self.provider.agent_requests, [])
        self.assertEqual(self.provider.comparator_requests, [])

    def test_holdout_plan_binding_rejects_each_bound_field(self) -> None:
        cases, comparisons, sources, case_records = self.binding_inputs()
        mutations = {
            "manifest hash": lambda payload: payload.__setitem__(
                "manifest_sha256", "0" * 64
            ),
            "comparator release hash": lambda payload: payload.__setitem__(
                "comparator_release_sha256", "a" * 64
            ),
            "comparator calibration evidence hash": lambda payload: payload.__setitem__(
                "comparator_calibration_evidence_sha256", "b" * 64
            ),
            "generator provider binding": lambda payload: payload[
                "generator_provider"
            ].__setitem__("version", "stale-provider-version"),
            "candidate commit": lambda payload: payload.__setitem__(
                "candidate_commit", "1" * 40
            ),
            "original commit": lambda payload: payload.__setitem__(
                "original_commit", "2" * 40
            ),
            "seed": lambda payload: payload.__setitem__("seed", self.suite.seed + 1),
            "comparison profile": lambda payload: payload[
                "comparison_profile"
            ].reverse(),
            "at least 8 unique": lambda payload: payload["cases"].pop(),
            "ids, hashes, fingerprints, skills, and critical expectations": lambda payload: (
                payload["cases"][0].__setitem__("case_tree_sha256", "f" * 64)
            ),
        }
        for message, mutate in mutations.items():
            with self.subTest(binding=message):
                payload = copy.deepcopy(self.payload)
                mutate(payload)
                plan_path = self.fixture.save_holdout_plan(
                    payload, name=f"invalid-{message.replace(' ', '-')}.json"
                )
                selection = self.selection(holdout_plan=plan_path)
                with self.assertRaisesRegex(RunnerError, message):
                    self.runner()._bind_holdout_plan(
                        selection,
                        cases,
                        comparisons,
                        sources,
                        case_records,
                    )

    def test_production_holdout_requires_live_certification_evidence(self) -> None:
        cases, comparisons, sources, case_records = self.binding_inputs()
        runner = self.runner()
        runtime = runner._load_comparator_runtime()
        invalid_certification = replace(
            runtime.certification,
            valid=False,
            evidence_sha256=None,
        )
        invalid_runtime = replace(runtime, certification=invalid_certification)
        with patch.object(
            runner, "_load_comparator_runtime", return_value=invalid_runtime
        ):
            with self.assertRaisesRegex(RunnerError, "valid live comparator"):
                runner._bind_holdout_plan(
                    self.selection(),
                    cases,
                    comparisons,
                    sources,
                    case_records,
                )

        invalid_runtime = replace(runtime, external_bindings_validated=False)
        with patch.object(
            runner, "_load_comparator_runtime", return_value=invalid_runtime
        ):
            with self.assertRaisesRegex(RunnerError, "external release bindings"):
                runner._bind_holdout_plan(
                    self.selection(),
                    cases,
                    comparisons,
                    sources,
                    case_records,
                )

    def test_holdout_plan_requires_external_trusted_sealed_provenance(self) -> None:
        provenance_mutations = (
            (
                "assurance",
                "self-certified",
                "trusted-reviewed-attestation",
            ),
            (
                "privacy_claim",
                "cryptographically-private",
                "disclaim cryptographic privacy proof",
            ),
            ("frozen_before_candidate_evaluation", False, "must be true"),
            ("sealed_after_independent_review", False, "must be true"),
            ("reviewed_by", [], "at least 1 item"),
            ("freeze_record", "", "non-empty string"),
            ("seal_record", "", "non-empty string"),
        )
        for field, value, message in provenance_mutations:
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["provenance"][field] = value
                invalid = self.fixture.save_holdout_plan(
                    payload, name=f"bad-provenance-{field}.json"
                )
                with self.assertRaisesRegex(HoldoutPlanError, message):
                    load_holdout_plan(invalid)

        payload = copy.deepcopy(self.payload)
        payload["status"] = "draft"
        invalid = self.fixture.save_holdout_plan(payload, name="draft.json")
        with self.assertRaisesRegex(HoldoutPlanError, "status must be 'sealed'"):
            load_holdout_plan(invalid)

        duplicate = self.fixture.root / "duplicate.json"
        duplicate.write_text(
            '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
        )
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(HoldoutPlanError, "duplicate JSON key"):
            load_holdout_plan(duplicate)

        symlink = self.fixture.root / "plan-link.json"
        symlink.symlink_to(self.plan_path)
        with self.assertRaisesRegex(HoldoutPlanError, "non-symlink"):
            load_holdout_plan(symlink)

        cases, comparisons, sources, case_records = self.binding_inputs()
        with self.assertRaisesRegex(RunnerError, "non-symlink"):
            self.runner()._bind_holdout_plan(
                self.selection(holdout_plan=symlink),
                cases,
                comparisons,
                sources,
                case_records,
            )

        hardlink = self.fixture.root / "plan-hardlink.json"
        os.link(self.plan_path, hardlink)
        try:
            with self.assertRaisesRegex(HoldoutPlanError, "exactly one hard link"):
                load_holdout_plan(self.plan_path)
        finally:
            hardlink.unlink()

        real_read = os.read
        changed = False

        def mutate_after_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            data = real_read(descriptor, size)
            if data and not changed:
                changed = True
                with self.plan_path.open("ab") as stream:
                    stream.write(b" ")
            return data

        with (
            patch("harness_evals.holdout_plan.os.read", side_effect=mutate_after_read),
            self.assertRaisesRegex(HoldoutPlanError, "changed while it was read"),
        ):
            load_holdout_plan(self.plan_path)
        self.plan_path.write_bytes(self.plan_path.read_bytes().rstrip() + b"\n")
        self.plan_path.chmod(0o600)

        self.plan_path.chmod(0o640)
        with self.assertRaisesRegex(HoldoutPlanError, "group or other permissions"):
            load_holdout_plan(self.plan_path)
        with self.assertRaisesRegex(RunnerError, "group or other permissions"):
            self.runner()._bind_holdout_plan(
                self.selection(),
                cases,
                comparisons,
                sources,
                case_records,
            )
        self.plan_path.chmod(0o600)

        ownership_runner = self.runner()
        with patch(
            "harness_evals.holdout_plan.os.getuid", return_value=os.getuid() + 1
        ):
            with self.assertRaisesRegex(HoldoutPlanError, "current uid"):
                load_holdout_plan(self.plan_path)
            with self.assertRaisesRegex(RunnerError, "current uid"):
                ownership_runner._bind_holdout_plan(
                    self.selection(),
                    cases,
                    comparisons,
                    sources,
                    case_records,
                )

        internal = self.fixture.suite_root / "holdout-plan.json"
        internal.write_bytes(self.plan_path.read_bytes())
        internal.chmod(0o600)
        with self.assertRaisesRegex(RunnerError, "external to the evaluation suite"):
            self.runner()._bind_holdout_plan(
                self.selection(holdout_plan=internal),
                cases,
                comparisons,
                sources,
                case_records,
            )

    def test_holdout_plan_byte_drift_after_validation_fails_closed(self) -> None:
        cases, comparisons, sources, case_records = self.binding_inputs()
        runner = self.runner()
        runner._bind_holdout_plan(
            self.selection(), cases, comparisons, sources, case_records
        )
        self.plan_path.write_bytes(self.plan_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(RunnerError, "bytes drifted"):
            runner._assert_holdout_plan_integrity()

    def test_holdout_aggregate_rejects_missing_duplicate_and_partial_cells(
        self,
    ) -> None:
        holdout_plan = load_holdout_plan(self.plan_path)
        selection = self.selection()
        valid_pairs = self.release_pairs()
        valid = _aggregate(
            valid_pairs,  # type: ignore[arg-type]
            self.suite,
            self.suite.comparisons,
            selection,
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        self.assertTrue(valid["final_release_authorized"])
        self.assertTrue(valid["passed"])

        duplicated_cases = list(holdout_plan.cases)
        duplicated_cases[1] = replace(
            duplicated_cases[1],
            release_case_fingerprint=duplicated_cases[0].release_case_fingerprint,
        )
        pseudoreplicated = _aggregate(
            valid_pairs,  # type: ignore[arg-type]
            self.suite,
            self.suite.comparisons,
            selection,
            holdout_plan=replace(holdout_plan, cases=tuple(duplicated_cases)),
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        pseudoreplication_cell = pseudoreplicated["by_comparison_skill"][
            "candidate-vs-original"
        ]["engineering"]
        self.assertEqual(
            pseudoreplication_cell["distinct_release_case_fingerprints"], 7
        )
        self.assertTrue(pseudoreplication_cell["duplicate_release_case_fingerprints"])
        self.assertFalse(
            pseudoreplicated["gates"]["holdout_release_protocol"][
                "exact_task_content_uniqueness"
            ]
        )
        self.assertFalse(pseudoreplicated["final_release_authorized"])

        test_release = _aggregate(
            valid_pairs,  # type: ignore[arg-type]
            self.suite,
            self.suite.comparisons,
            selection,
            holdout_plan=holdout_plan,
            generator_release_authoritative=True,
        )
        self.assertFalse(
            test_release["gates"]["holdout_release_protocol"][
                "production_comparator_release_validated"
            ]
        )
        self.assertFalse(test_release["final_release_authorized"])
        self.assertFalse(test_release["passed"])

        for label, pairs in (
            ("missing", valid_pairs[:-1]),
            ("duplicate", [*valid_pairs, valid_pairs[0]]),
        ):
            with self.subTest(matrix=label):
                aggregate = _aggregate(
                    pairs,  # type: ignore[arg-type]
                    self.suite,
                    self.suite.comparisons,
                    selection,
                    holdout_plan=holdout_plan,
                    release_authority_validated=True,
                    generator_release_authoritative=True,
                )
                self.assertFalse(
                    aggregate["gates"]["execution_matrix_integrity"]["passed"]
                )
                self.assertFalse(aggregate["final_release_authorized"])
                self.assertEqual(
                    aggregate["passed"], aggregate["final_release_authorized"]
                )

        partial = copy.deepcopy(valid_pairs)
        losing_cases = {
            "engineering-0",
            "engineering-1",
        }
        for pair in partial:
            if (
                pair["comparison_id"] == "candidate-vs-original"
                and pair["case_id"] in losing_cases
                and pair["repetition"] in {0, 1}
            ):
                pair["final_winner"] = "control"
        aggregate = _aggregate(
            partial,  # type: ignore[arg-type]
            self.suite,
            self.suite.comparisons,
            selection,
            holdout_plan=holdout_plan,
            release_authority_validated=True,
            generator_release_authoritative=True,
        )
        self.assertTrue(aggregate["gates"]["execution_matrix_integrity"]["passed"])
        self.assertFalse(
            aggregate["by_comparison_skill"]["candidate-vs-original"]["engineering"][
                "holdout_authorized"
            ]
        )
        self.assertFalse(aggregate["final_release_authorized"])
        self.assertEqual(aggregate["passed"], aggregate["final_release_authorized"])


class ManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path.home() / ".cache" / "skill-eval-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = SuiteFixture(Path(self.temporary.name))

    def test_case_path_escape_is_rejected(self) -> None:
        self.fixture.manifest["cases"][0]["prompt_file"] = "../outside.md"
        (self.fixture.repository / "outside.md").write_text("outside", encoding="utf-8")
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "escapes the suite root"):
            load_suite(self.fixture.manifest_path)

    def test_unknown_keys_are_rejected(self) -> None:
        self.fixture.manifest["provider"]["surprise"] = True
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "unknown keys"):
            load_suite(self.fixture.manifest_path)

    def test_v3_judged_requires_explicit_profile_and_contract(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v3_judged()
        payload = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(payload)), [])
        suite = load_suite(self.fixture.manifest_path)
        self.assertEqual(suite.evaluation_mode, "judged")
        self.assertEqual(suite.comparator_profile.kind, "builtin")
        self.assertEqual(suite.comparator_profile.id, "software-engineering-v2.3")

        mutations = []
        missing_profile = copy.deepcopy(payload)
        del missing_profile["comparator_profile"]
        mutations.append(missing_profile)
        missing_contract = copy.deepcopy(payload)
        del missing_contract["cases"][0]["comparator_contract"]
        mutations.append(missing_contract)
        unknown_profile = copy.deepcopy(payload)
        unknown_profile["comparator_profile"]["id"] = "unknown-profile"
        mutations.append(unknown_profile)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

    def test_v3_objective_forbids_comparator_fields_and_contract(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v3_objective()
        payload = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(payload)), [])
        suite = load_suite(self.fixture.manifest_path)
        self.assertEqual(suite.evaluation_mode, "objective_only")
        self.assertIsNone(suite.comparator)
        self.assertIsNone(suite.comparator_profile)
        self.assertIsNone(suite.cases[0].comparator_contract)

        mutations = []
        comparator = copy.deepcopy(payload)
        comparator["comparator"] = self.fixture._manifest()["comparator"]
        mutations.append(comparator)
        profile = copy.deepcopy(payload)
        profile["comparator_profile"] = {
            "kind": "builtin",
            "id": "software-engineering-v2.3",
        }
        mutations.append(profile)
        contract = copy.deepcopy(payload)
        contract["cases"][0]["comparator_contract"] = self.fixture._manifest()["cases"][
            0
        ]["comparator_contract"]
        mutations.append(contract)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

    def test_v6_selects_reviewed_provider_adapters_without_kind_fields(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v6_judged()
        payload = copy.deepcopy(self.fixture.manifest)
        self.assertFalse(list(Draft202012Validator(schema).iter_errors(payload)))
        suite = load_suite(self.fixture.manifest_path)
        self.assertEqual(suite.provider.adapter_id, "deterministic-fake")
        self.assertEqual(suite.provider.kind, "fake")
        self.assertEqual(suite.comparator.adapter_id, "deterministic-fake")

        generation_configs = (
            {
                "adapter": "claude-cli",
                "executable": str(self.fixture.fake_codex),
                "max_budget_usd": 1.0,
                "model": "fake-model-v1",
                "timeout_seconds": 10,
            },
            {
                "adapter": "codex-app-server",
                "billing_basis": "chatgpt_subscription",
                "executable": str(self.fixture.fake_codex),
                "model": "gpt-5.6-terra",
                "protocol_lock": self.fixture.codex_protocol_lock.name,
                "reasoning_effort": "ultra",
                "timeout_seconds": 10,
            },
            payload["provider"],
        )
        for config in generation_configs:
            with self.subTest(adapter=config["adapter"]):
                candidate = copy.deepcopy(payload)
                candidate["provider"] = copy.deepcopy(config)
                self.assertFalse(
                    list(Draft202012Validator(schema).iter_errors(candidate))
                )
                self.fixture.manifest = candidate
                self.fixture.save_manifest()
                parsed = load_suite(self.fixture.manifest_path)
                self.assertEqual(parsed.provider.adapter_id, config["adapter"])

        mutations: list[dict[str, object]] = []
        unknown = copy.deepcopy(payload)
        unknown["provider"]["adapter"] = "suite-claimed-production"
        mutations.append(unknown)
        legacy_kind = copy.deepcopy(payload)
        legacy_kind["provider"]["kind"] = legacy_kind["provider"].pop("adapter")
        mutations.append(legacy_kind)
        codex_comparator = copy.deepcopy(payload)
        codex_comparator["comparator"] = {
            "adapter": "codex-app-server",
            "billing_basis": "chatgpt_subscription",
            "executable": str(self.fixture.fake_codex),
            "model": "gpt-5.6-luna",
            "protocol_lock": self.fixture.codex_protocol_lock.name,
            "reasoning_effort": "max",
            "timeout_seconds": 10,
        }
        mutations.append(codex_comparator)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

        legacy_adapter = copy.deepcopy(payload)
        legacy_adapter["schema_version"] = 5
        with self.subTest(mutation="adapter in schema v5"):
            self.assertTrue(
                list(Draft202012Validator(schema).iter_errors(legacy_adapter))
            )
            self.fixture.manifest = legacy_adapter
            self.fixture.save_manifest()
            with self.assertRaises(ManifestError):
                load_suite(self.fixture.manifest_path)

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.save_manifest()
        self.fixture.use_v6_objective()
        objective = load_suite(self.fixture.manifest_path)
        self.assertEqual(objective.provider.adapter_id, "deterministic-fake")
        self.assertIsNone(objective.comparator)

    def test_v4_requires_bundle_source_and_legacy_versions_derive_it(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        original_bytes = self.fixture.manifest_path.read_bytes()
        legacy = load_suite(self.fixture.manifest_path)
        self.assertEqual(legacy.raw_bytes, original_bytes)
        self.assertEqual(
            legacy.manifest_hash, hashlib.sha256(original_bytes).hexdigest()
        )
        self.assertEqual(legacy.cases[0].bundle_source.as_posix(), "skills/demo")

        self.fixture.use_v3_judged()
        version_three_bytes = self.fixture.manifest_path.read_bytes()
        version_three = load_suite(self.fixture.manifest_path)
        self.assertEqual(version_three.raw_bytes, version_three_bytes)
        self.assertEqual(
            version_three.manifest_hash,
            hashlib.sha256(version_three_bytes).hexdigest(),
        )
        self.assertEqual(version_three.cases[0].bundle_source.as_posix(), "skills/demo")
        self.assertNotIn("bundle_source", version_three.raw["cases"][0])

        self.fixture.use_v4_judged("instruction-bundles/demo")
        version_four = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(version_four)), []
        )
        parsed = load_suite(self.fixture.manifest_path)
        self.assertEqual(
            parsed.cases[0].bundle_source.as_posix(), "instruction-bundles/demo"
        )

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.use_v4_objective("instruction-bundles/demo")
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(self.fixture.manifest)), []
        )
        self.assertEqual(
            load_suite(self.fixture.manifest_path).cases[0].bundle_source.as_posix(),
            "instruction-bundles/demo",
        )

    def test_bundle_source_schema_and_parser_reject_version_and_path_mutations(
        self,
    ) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v4_judged("instruction-bundles/demo")
        valid = copy.deepcopy(self.fixture.manifest)
        mutations: list[dict[str, object]] = []

        missing = copy.deepcopy(valid)
        del missing["cases"][0]["bundle_source"]
        mutations.append(missing)

        for version in (2, 3):
            legacy = copy.deepcopy(valid)
            legacy["schema_version"] = version
            if version == 2:
                legacy.pop("evaluation_mode", None)
                legacy.pop("comparator_profile", None)
            mutations.append(legacy)

        for value in (
            "/absolute",
            ".",
            "../escape",
            "./prefixed",
            "double//separator",
            "trailing/",
            "windows\\separator",
            "control\u0001character",
            "trailing-control\n",
            "unpaired-surrogate\ud800",
        ):
            mutation = copy.deepcopy(valid)
            mutation["cases"][0]["bundle_source"] = value
            mutations.append(mutation)

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

    def test_v4_preserves_judged_and_objective_mode_exclusivity(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v4_judged()
        judged = copy.deepcopy(self.fixture.manifest)
        judged_mutations: list[dict[str, object]] = []
        for field in ("comparator", "comparator_profile"):
            mutation = copy.deepcopy(judged)
            del mutation[field]
            judged_mutations.append(mutation)
        missing_contract = copy.deepcopy(judged)
        del missing_contract["cases"][0]["comparator_contract"]
        judged_mutations.append(missing_contract)

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.use_v4_objective()
        objective = copy.deepcopy(self.fixture.manifest)
        objective_mutations: list[dict[str, object]] = []
        for field in ("comparator", "comparator_profile"):
            mutation = copy.deepcopy(objective)
            if field == "comparator":
                mutation[field] = self.fixture._manifest()[field]
            else:
                mutation[field] = {
                    "kind": "builtin",
                    "id": "software-engineering-v2.3",
                }
            objective_mutations.append(mutation)
        unexpected_contract = copy.deepcopy(objective)
        unexpected_contract["cases"][0]["comparator_contract"] = (
            self.fixture._manifest()["cases"][0]["comparator_contract"]
        )
        objective_mutations.append(unexpected_contract)

        for mutation in (*judged_mutations, *objective_mutations):
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

    def test_v5_declares_canonical_release_comparison_ids(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.use_v5_objective(("release-alpha", "release-beta"))
        self.fixture.manifest["variants"] = [
            {"id": "empty-arm", "kind": "without_skill"},
            {
                "id": "archive-arm",
                "kind": "git_ref",
                "git_ref": self.fixture.baseline_commit,
            },
            {
                "id": "trial-arm",
                "kind": "worktree",
                "root": "..",
                "source_ref": self.fixture.treatment_commit,
            },
            {"id": "unused-arm", "kind": "without_skill"},
        ]
        self.fixture.manifest["comparisons"] = [
            {
                "id": "release-alpha",
                "control": "empty-arm",
                "treatment": "trial-arm",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            },
            {
                "id": "release-beta",
                "control": "archive-arm",
                "treatment": "trial-arm",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            },
            {
                "id": "diagnostic-only",
                "control": "archive-arm",
                "treatment": "empty-arm",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            },
        ]
        self.fixture.save_manifest()
        valid = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(valid)), [])
        suite = load_suite(self.fixture.manifest_path)
        self.assertEqual(
            suite.holdout_comparison_ids, ("release-alpha", "release-beta")
        )
        self.assertNotIn("unused-arm", suite.holdout_comparison_ids)
        self.assertNotIn("diagnostic-only", suite.holdout_comparison_ids)

        structural_mutations: list[dict[str, object]] = []
        missing = copy.deepcopy(valid)
        del missing["holdout"]
        structural_mutations.append(missing)
        empty = copy.deepcopy(valid)
        empty["holdout"]["comparison_ids"] = []
        structural_mutations.append(empty)
        duplicate = copy.deepcopy(valid)
        duplicate["holdout"]["comparison_ids"] = ["release-alpha", "release-alpha"]
        structural_mutations.append(duplicate)
        invalid_id = copy.deepcopy(valid)
        invalid_id["holdout"]["comparison_ids"] = ["Invalid ID"]
        structural_mutations.append(invalid_id)
        extra = copy.deepcopy(valid)
        extra["holdout"]["unexpected"] = True
        structural_mutations.append(extra)
        for mutation in structural_mutations:
            with self.subTest(structural=mutation):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

        semantic_mutations = []
        unknown = copy.deepcopy(valid)
        unknown["holdout"]["comparison_ids"] = ["missing-comparison"]
        semantic_mutations.append(unknown)
        reordered = copy.deepcopy(valid)
        reordered["holdout"]["comparison_ids"] = ["release-beta", "release-alpha"]
        semantic_mutations.append(reordered)
        for mutation in semantic_mutations:
            with self.subTest(semantic=mutation):
                self.assertEqual(
                    list(Draft202012Validator(schema).iter_errors(mutation)), []
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

        for version in (2, 3, 4):
            self.fixture.manifest = self.fixture._manifest()
            if version == 3:
                self.fixture.use_v3_judged()
            elif version == 4:
                self.fixture.use_v4_judged()
            self.fixture.manifest["holdout"] = {"comparison_ids": ["without-current"]}
            self.fixture.save_manifest()
            with self.subTest(legacy_version=version):
                self.assertTrue(
                    list(
                        Draft202012Validator(schema).iter_errors(self.fixture.manifest)
                    )
                )
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.use_v5_judged(("without-current",))
        self.assertEqual(
            load_suite(self.fixture.manifest_path).holdout_comparison_ids,
            ("without-current",),
        )

    def test_shared_verifier_dir_versions_null_and_configured(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.assertIsNone(load_suite(self.fixture.manifest_path).shared_verifier_dir)

        self.fixture.use_v3_judged()
        version_three_bytes = self.fixture.manifest_path.read_bytes()
        version_three = load_suite(self.fixture.manifest_path)
        self.assertIsNone(version_three.shared_verifier_dir)
        self.assertEqual(version_three.raw_bytes, version_three_bytes)

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.isolate_basic_case()
        self.fixture._write_suite(
            "cases/testing/_shared/helper.py", "LEGACY_SHARED = 1\n"
        )
        legacy_bytes = self.fixture.manifest_path.read_bytes()
        legacy_shared = load_suite(self.fixture.manifest_path)
        self.assertEqual(legacy_shared.raw_bytes, legacy_bytes)
        self.assertEqual(
            legacy_shared.shared_verifier_dir,
            self.fixture.suite_root / "cases/testing/_shared",
        )

        self.fixture.use_v4_objective()
        null_payload = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(null_payload)), []
        )
        self.assertIsNone(load_suite(self.fixture.manifest_path).shared_verifier_dir)

        missing = copy.deepcopy(null_payload)
        del missing["shared_verifier_dir"]
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(missing)))
        self.fixture.manifest = missing
        self.fixture.save_manifest()
        with self.assertRaises(ManifestError):
            load_suite(self.fixture.manifest_path)

        for version in (2, 3):
            legacy = self.fixture._manifest()
            if version == 3:
                self.fixture.manifest = legacy
                self.fixture.use_v3_judged()
                legacy = copy.deepcopy(self.fixture.manifest)
            legacy["shared_verifier_dir"] = None
            self.assertTrue(list(Draft202012Validator(schema).iter_errors(legacy)))
            self.fixture.manifest = legacy
            self.fixture.save_manifest()
            with self.assertRaises(ManifestError):
                load_suite(self.fixture.manifest_path)

        self.fixture.manifest = self.fixture._manifest()
        self.fixture.isolate_basic_case()
        self.fixture._write_suite(
            "verifier-resources/shared/helper.py", "SHARED_VALUE = 1\n"
        )
        self.fixture.use_v4_objective()
        self.fixture.manifest["shared_verifier_dir"] = "verifier-resources/shared"
        self.fixture.save_manifest()
        configured_payload = copy.deepcopy(self.fixture.manifest)
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(configured_payload)), []
        )
        self.assertEqual(
            load_suite(self.fixture.manifest_path).shared_verifier_dir,
            self.fixture.suite_root / "verifier-resources/shared",
        )

    def test_shared_verifier_dir_rejects_invalid_symlink_and_overlap(self) -> None:
        schema = json.loads((HARNESS_ROOT / "suite.schema.json").read_text())
        self.fixture.isolate_basic_case()
        self.fixture._write_suite("verifier-resources/shared/helper.py", "value = 1\n")
        self.fixture.use_v4_objective()
        valid = copy.deepcopy(self.fixture.manifest)
        invalid_values = (
            "",
            ".",
            "/absolute",
            "../escape",
            "./prefixed",
            "double//separator",
            "trailing/",
            "windows\\separator",
            "control\n",
            "surrogate\ud800",
        )
        for value in invalid_values:
            mutation = copy.deepcopy(valid)
            mutation["shared_verifier_dir"] = value
            with self.subTest(value=value):
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(mutation))
                )
                self.fixture.manifest = mutation
                self.fixture.save_manifest()
                with self.assertRaises(ManifestError):
                    load_suite(self.fixture.manifest_path)

        missing = copy.deepcopy(valid)
        missing["shared_verifier_dir"] = "verifier-resources/missing"
        self.fixture.manifest = missing
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "cannot resolve"):
            load_suite(self.fixture.manifest_path)

        linked = self.fixture.suite_root / "verifier-resources/linked"
        linked.symlink_to(
            self.fixture.suite_root / "verifier-resources/shared",
            target_is_directory=True,
        )
        symlinked = copy.deepcopy(valid)
        symlinked["shared_verifier_dir"] = "verifier-resources/linked"
        self.fixture.manifest = symlinked
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "must not traverse a symlink"):
            load_suite(self.fixture.manifest_path)

        overlap = copy.deepcopy(valid)
        overlap["shared_verifier_dir"] = "cases/basic/fixture"
        self.fixture.manifest = overlap
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "overlaps case basic"):
            load_suite(self.fixture.manifest_path)

    def test_legacy_shared_verifier_dir_fails_closed_on_symlinks(self) -> None:
        external = self.fixture.root / "external-legacy-shared"
        external.mkdir()
        shared = self.fixture.suite_root / "cases/testing/_shared"
        shared.parent.mkdir(parents=True)
        shared.symlink_to(external, target_is_directory=True)

        for version in (2, 3):
            self.fixture.manifest = self.fixture._manifest()
            if version == 3:
                self.fixture.use_v3_judged()
            else:
                self.fixture.save_manifest()
            with self.subTest(version=version, link="leaf"):
                with self.assertRaisesRegex(
                    ManifestError, "must not traverse a symlink"
                ):
                    load_suite(self.fixture.manifest_path)

        shared.unlink()
        shutil.rmtree(self.fixture.suite_root / "cases")
        external_cases = self.fixture.root / "external-cases"
        external_cases.joinpath("testing/_shared").mkdir(parents=True)
        (self.fixture.suite_root / "cases").symlink_to(
            external_cases, target_is_directory=True
        )
        for version in (2, 3):
            self.fixture.manifest = self.fixture._manifest()
            if version == 3:
                self.fixture.use_v3_judged()
            else:
                self.fixture.save_manifest()
            with self.subTest(version=version, link="ancestor"):
                with self.assertRaisesRegex(
                    ManifestError, "must not traverse a symlink"
                ):
                    load_suite(self.fixture.manifest_path)

    def test_shared_verifier_dir_rejects_bundle_and_context_overlap(self) -> None:
        self.fixture.isolate_basic_case()
        self.fixture._write_suite("verifier-resources/shared/helper.py", "VALUE = 1\n")
        self.fixture._write_suite(
            "verifier-resources/SKILL.md", "# Verifier resources must stay private\n"
        )
        self.fixture.use_v4_objective("eval-suite/verifier-resources")
        self.fixture.manifest["shared_verifier_dir"] = "verifier-resources/shared"
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "overlaps case basic"):
            load_suite(self.fixture.manifest_path)

        self.fixture.manifest["cases"][0]["bundle_source"] = "skills/demo"
        self.fixture.manifest["cases"][0]["context_files"] = [
            "eval-suite/verifier-resources/shared/helper.py"
        ]
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "overlaps case basic"):
            load_suite(self.fixture.manifest_path)

    def test_suite_local_profile_is_contained_and_never_authoritative(self) -> None:
        self.fixture.create_data_profile("profiles/local")
        self.fixture.use_v3_judged({"kind": "suite_local", "path": "profiles/local"})
        suite = load_suite(self.fixture.manifest_path)
        self.assertEqual(suite.comparator_profile.kind, "suite_local")
        self.assertIsNone(suite.comparator_profile.resources.authority_binding)
        self.assertNotIn(
            "calibration_engine",
            suite.comparator_profile.resources.descriptor.resources_by_name,
        )
        self.assertFalse(
            (self.fixture.suite_root / "profiles/local/calibration.py").exists()
        )
        provider = self.fixture.provider()
        with EvalRunner(suite, provider, provider) as runner:
            preflight = runner.preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        self.assertEqual(
            preflight["comparator"]["profile_id"],
            "suite-local-software-v2.3",
        )
        self.assertEqual(preflight["comparator"]["profile_kind"], "suite_local")
        self.assertIsNone(preflight["comparator"]["profile_authority_registry_sha256"])
        self.assertTrue(preflight["comparator"]["profile_locks_valid"])
        self.assertFalse(preflight["comparator"]["protocol_locks_valid"])

    def test_suite_local_profile_rejects_escape_symlink_and_drift(self) -> None:
        outside = self.fixture.create_data_profile("../outside-profile")
        link = self.fixture.suite_root / "linked-profile"
        link.symlink_to(outside, target_is_directory=True)
        for path, message in (
            ("../outside-profile", "canonical suite-relative"),
            ("linked-profile", "must not traverse a symlink"),
        ):
            with self.subTest(path=path):
                self.fixture.manifest = self.fixture._manifest()
                self.fixture.use_v3_judged({"kind": "suite_local", "path": path})
                with self.assertRaisesRegex(ManifestError, message):
                    load_suite(self.fixture.manifest_path)

        local_profile = self.fixture.create_data_profile("profiles/drifted")
        manifest = local_profile / "manifest.json"
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["_drift"] = True
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        self.fixture.manifest = self.fixture._manifest()
        self.fixture.use_v3_judged({"kind": "suite_local", "path": "profiles/drifted"})
        with self.assertRaisesRegex(ManifestError, "invalid comparator profile"):
            load_suite(self.fixture.manifest_path)

    def test_suite_local_profile_cannot_shadow_builtin_identity(self) -> None:
        self.fixture.create_data_profile(
            "profiles/shadow", profile_id="software-engineering-v2.3"
        )
        self.fixture.use_v3_judged({"kind": "suite_local", "path": "profiles/shadow"})
        with self.assertRaisesRegex(ManifestError, "must not shadow a built-in id"):
            load_suite(self.fixture.manifest_path)

    def test_suite_local_profile_cannot_prepare_holdout(self) -> None:
        self.fixture.create_data_profile("profiles/local")
        self.fixture.use_v3_judged({"kind": "suite_local", "path": "profiles/local"})
        self.fixture.configure_holdout(skills=("demo",))
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        with EvalRunner(suite, provider, provider) as runner:
            with self.assertRaisesRegex(
                RunnerError, "authority-bound comparator profile"
            ):
                runner.prepare_holdout_plan(
                    output_path=self.fixture.root / "local-holdout.json",
                    plan_id="local-holdout",
                    reviewers=("reviewer-a", "reviewer-b"),
                    freeze_record="freeze-record",
                    seal_record="seal-record",
                )
        self.assertFalse((self.fixture.root / "local-holdout.json").exists())

    def test_test_authority_profile_cannot_access_holdout(self) -> None:
        self.fixture.use_v3_judged(
            {"kind": "builtin", "id": "plain-language-revision-v1"}
        )
        self.fixture.configure_holdout(skills=("demo",))
        self.fixture.align_builtin_profile_authority()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        output = self.fixture.root / "test-authority-holdout.json"
        with EvalRunner(suite, provider, provider) as runner:
            with self.assertRaisesRegex(
                RunnerError, "not authorized for production holdouts"
            ):
                runner.prepare_holdout_plan(
                    output_path=output,
                    plan_id="test-authority-holdout",
                    reviewers=("reviewer-a", "reviewer-b"),
                    freeze_record="freeze-record",
                    seal_record="seal-record",
                )
        self.assertFalse(output.exists())
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_required_tools_and_worktree_source_ref_are_mandatory(self) -> None:
        del self.fixture.manifest["cases"][0]["verifier"]["required_tools"]
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "missing required keys"):
            load_suite(self.fixture.manifest_path)

        self.fixture.manifest = self.fixture._manifest()
        del self.fixture.manifest["variants"][2]["source_ref"]
        self.fixture.save_manifest()
        with self.assertRaisesRegex(ManifestError, "missing required keys"):
            load_suite(self.fixture.manifest_path)

    def test_missing_declared_tool_fails_preflight(self) -> None:
        self.fixture.manifest["cases"][0]["verifier"]["required_tools"] = [
            "definitely-not-a-real-eval-tool"
        ]
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        with self.assertRaisesRegex(RunnerError, "required tool is not on PATH"):
            EvalRunner(suite, provider, provider).preflight(
                RunSelection(comparison_ids=("without-current",))
            )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        self.fixture.manifest_path.write_text(
            '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ManifestError, "duplicate JSON key"):
            load_suite(self.fixture.manifest_path)

    def test_preflight_rejects_manifest_byte_drift_after_load(self) -> None:
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)

        self.fixture.manifest_path.write_bytes(suite.raw_bytes + b" ")

        with self.assertRaisesRegex(
            RunnerError, "suite manifest integrity check failed.*bytes drifted"
        ):
            runner.preflight(RunSelection(comparison_ids=("without-current",)))
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_preflight_rejects_manifest_leaf_replaced_by_symlink(self) -> None:
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)
        replacement = self.fixture.root / "replacement-suite.json"
        replacement.write_bytes(suite.raw_bytes)
        self.fixture.manifest_path.unlink()
        self.fixture.manifest_path.symlink_to(replacement)

        with self.assertRaisesRegex(
            RunnerError, "suite manifest integrity check failed.*non-symlink"
        ):
            runner.preflight(RunSelection(comparison_ids=("without-current",)))
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_selection_defaults_to_train_and_requires_nonempty_match(self) -> None:
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)
        preflight = runner.preflight(RunSelection(comparison_ids=("without-current",)))
        self.assertEqual(preflight["selection"]["split"], "train")
        with self.assertRaisesRegex(RunnerError, "unknown case ids"):
            runner.preflight(
                RunSelection(case_ids=("missing",), comparison_ids=("without-current",))
            )

    def test_judged_diagnostic_dry_run_requires_one_explicit_comparison(self) -> None:
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)
        for selection in (
            RunSelection(),
            RunSelection(comparison_ids=("without-current", "old-current")),
        ):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(
                    RunnerError, "exactly one explicit comparison"
                ):
                    runner.run(selection, output_dir=None, dry_run=True)
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_validation_split_requires_explicit_selection(self) -> None:
        self.fixture.manifest["cases"][0]["split"] = "validation"  # type: ignore[index]
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)
        with self.assertRaisesRegex(RunnerError, "selection matched no cases"):
            runner.preflight(RunSelection(comparison_ids=("without-current",)))
        preflight = runner.preflight(
            RunSelection(split="validation", comparison_ids=("without-current",))
        )
        self.assertEqual(preflight["selection"]["case_ids"], ["basic"])

    def test_public_split_selects_train_and_validation_but_never_holdout(self) -> None:
        train = self.fixture.manifest["cases"][0]
        validation = dict(train)
        validation["id"] = "validation-case"
        validation["split"] = "validation"
        holdout = dict(train)
        holdout["id"] = "holdout-case"
        holdout["split"] = "holdout"
        self.fixture.manifest["cases"] = [train, validation, holdout]
        self.fixture.save_manifest()

        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        runner = EvalRunner(suite, provider, provider)
        public = runner.preflight(
            RunSelection(split="public", comparison_ids=("without-current",))
        )
        self.assertEqual(public["selection"]["case_ids"], ["basic", "validation-case"])
        with self.assertRaisesRegex(RunnerError, "explicit holdout plan"):
            runner.preflight(
                RunSelection(split="holdout", comparison_ids=("without-current",))
            )

    def test_cli_defaults_to_train_and_exposes_bounded_filters(self) -> None:
        default = build_parser().parse_args([])
        selected = build_parser().parse_args(
            [
                "--split",
                "validation",
                "--case",
                "case-a",
                "--comparison",
                "comparison-a",
            ]
        )
        self.assertEqual(default.split, "train")
        self.assertEqual(selected.split, "validation")
        self.assertEqual(selected.case, ["case-a"])
        self.assertEqual(selected.comparison, ["comparison-a"])
        holdout = build_parser().parse_args(
            ["--split", "holdout", "--holdout-plan", "/tmp/holdout-plan.json"]
        )
        self.assertEqual(holdout.holdout_plan, Path("/tmp/holdout-plan.json"))
        public = build_parser().parse_args(["--split", "public"])
        self.assertEqual(public.split, "public")


class SchemaV3RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path.home() / ".cache" / "skill-eval-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = SuiteFixture(Path(self.temporary.name))

    def test_builtin_profile_runtime_is_selected_by_id(self) -> None:
        self.fixture.use_v3_judged()
        self.fixture.align_builtin_profile_authority()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        with EvalRunner(suite, provider, provider) as runner:
            preflight = runner.preflight(
                RunSelection(comparison_ids=("without-current",))
            )
        comparator = preflight["comparator"]
        self.assertEqual(comparator["profile_kind"], "builtin")
        self.assertEqual(comparator["profile_id"], "software-engineering-v2.3")
        self.assertRegex(comparator["profile_descriptor_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            comparator["profile_authority_registry_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertTrue(comparator["profile_locks_valid"])
        self.assertTrue(comparator["protocol_locks_valid"])

    def test_plain_language_profile_executes_non_engineering_judgment(self) -> None:
        self.fixture.create_data_profile(
            "profiles/plain-language",
            profile_id="suite-local-plain-language-v1",
            source_directory="plain_language_calibration",
        )
        self.fixture.use_v3_judged(
            {"kind": "suite_local", "path": "profiles/plain-language"}
        )
        case = self.fixture.manifest["cases"][0]
        case["prompt_file"] = "editorial-prompt.md"
        (self.fixture.suite_root / "editorial-prompt.md").write_text(
            "Rewrite the public notice in plain language without changing its facts.\n",
            encoding="utf-8",
        )
        case["comparator_contract"]["requirements"][0]["text"] = (
            "The revision must create a non-empty plain-language notice in the evaluated workspace."
        )
        case["comparator_contract"]["qualitative_bases"] = {
            "reader_clarity": {
                "kind": "reader-comprehension",
                "detail": "Compare whether a general reader can identify the closure and reopening directly from the revised notice.",
            }
        }
        self.fixture.save_manifest()
        suite = load_suite(self.fixture.manifest_path)

        clear_notice = "The office is closed Friday and reopens Monday."

        def agent(request):
            treatment = request.skill_snapshot is not None
            notice = (
                clear_notice
                if treatment
                else "Friday has the office in a closed state, with reopening occurring Monday."
            )
            (request.workspace / "answer.txt").write_text(
                "verified\n", encoding="utf-8"
            )
            (request.workspace / "notice.txt").write_text(
                notice + "\n", encoding="utf-8"
            )
            return {
                "final_output": "revision complete",
                "actual_models": [request.model],
                "cost_usd": 0.25,
                "tokens": {"input_tokens": 3, "output_tokens": 2},
            }

        def compare(request):
            requirement = request.pair["contract"]["requirements"][0]
            requirement_id = requirement["id"]

            def evidence(anchor):
                quote = requirement["text"]
                return {
                    "artifact": "contract",
                    "path": f"contract/requirements/{requirement_id}",
                    "line_start": 1,
                    "line_end": 1,
                    "quote": quote,
                    "semantic_anchor": anchor,
                    "observation": f"{quote} provides the controlled basis; {anchor} is the typed decision.",
                }

            treatment_side = "A" if request.order == "BA" else "B"
            criteria = {}
            for criterion in request.runtime.bundle.semantic_contract["criterion_ids"]:
                winner = treatment_side if criterion == "reader_clarity" else "tie"
                criterion_evidence = evidence(f"criterion:{criterion}:{winner}")
                if criterion == "reader_clarity":
                    criterion_evidence = {
                        "artifact": treatment_side,
                        "path": "notice.txt",
                        "line_start": 1,
                        "line_end": 1,
                        "quote": clear_notice,
                        "semantic_anchor": f"criterion:{criterion}:{winner}",
                        "observation": f"{clear_notice} directly names the closure and reopening; criterion:{criterion}:{winner} is the typed decision.",
                    }
                criteria[criterion] = {
                    "winner": winner,
                    "evidence": criterion_evidence,
                }
            return {
                "structured_output": {
                    "checks": {
                        side: [
                            {
                                "requirement_id": requirement_id,
                                "status": "satisfied",
                                "evidence": evidence(
                                    f"requirement:{requirement_id}:satisfied"
                                ),
                            }
                        ]
                        for side in ("A", "B")
                    },
                    "admissibility": {
                        side: {"decision": "eligible", "violation_ids": []}
                        for side in ("A", "B")
                    },
                    "criteria": criteria,
                },
                "actual_models": ["fake-sonnet-v2.0"],
                "cost_usd": 0.1,
                "tokens": {"input_tokens": 4, "output_tokens": 1},
            }

        provider = FakeProvider(
            agent_handler=agent,
            comparator_handler=compare,
        )
        output = self.fixture.root / "plain-language-result"
        with EvalRunner(suite, provider, provider) as runner:
            result = runner.run(
                RunSelection(comparison_ids=("without-current",)), output_dir=output
            )

        self.assertTrue(result["passed"], result)
        self.assertEqual(suite.comparator_profile.kind, "suite_local")
        self.assertIsNone(suite.comparator_profile.resources.authority_binding)
        self.assertEqual(len(provider.comparator_requests), 6)
        self.assertEqual(
            {
                tuple(request.runtime.bundle.semantic_contract["criterion_ids"])
                for request in provider.comparator_requests
            },
            {
                (
                    "factual_fidelity",
                    "reader_clarity",
                    "audience_fit",
                    "concision",
                )
            },
        )
        self.assertEqual(
            {pair["final_winner"] for pair in result["pairs"]}, {"treatment"}
        )

    def test_builtin_certification_root_rejects_symlink_before_dispatch(self) -> None:
        self.fixture.use_v3_judged()
        self.fixture.align_builtin_profile_authority()
        evidence = (
            self.fixture.suite_root / "harness_evals/comparator_calibration/evidence"
        )
        evidence.symlink_to(self.fixture.root, target_is_directory=True)
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        with EvalRunner(suite, provider, provider) as runner:
            with self.assertRaisesRegex(
                RunnerError, "certification root traverses a symlink"
            ):
                runner.preflight(RunSelection(comparison_ids=("without-current",)))
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])

    def test_objective_only_constructs_no_comparator_and_rejects_injection(
        self,
    ) -> None:
        self.fixture.use_v3_objective()
        suite = load_suite(self.fixture.manifest_path)
        injected = self.fixture.provider()
        with self.assertRaisesRegex(RunnerError, "reject injected comparator"):
            EvalRunner(suite, injected, injected)

        built = self.fixture.provider()
        with patch("harness_evals.runner._build_provider", return_value=built) as build:
            with EvalRunner(suite) as runner:
                self.assertIsNone(runner.comparator_provider)
        build.assert_called_once_with(suite.provider)

    def test_objective_only_run_uses_verifiers_without_comparator_spend(self) -> None:
        self.fixture.use_v3_objective()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        output = self.fixture.root / "objective-result"
        with EvalRunner(suite, provider) as runner:
            result = runner.run(RunSelection(), output_dir=output)

        self.assertEqual(result["execution_mode"], "objective_only")
        self.assertEqual(result["preflight"]["execution_mode"], "objective_only")
        self.assertIsNone(result["preflight"]["comparator"])
        self.assertEqual(result["aggregate"]["execution_mode"], "objective_only")
        self.assertFalse(result["aggregate"]["final_release_authorized"])
        self.assertEqual(provider.comparator_requests, [])
        self.assertEqual(len(provider.agent_requests), 12)
        self.assertFalse((output / "comparator-spend").exists())
        self.assertEqual(
            result["aggregate"]["comparator_spend_ledgers"],
            {
                "by_comparison": {},
                "total_charged_usd": 0,
                "total_maximum_usd": 0,
            },
        )
        self.assertTrue(
            all(
                pair["winner_basis"] == "verifier-pass-v1"
                and pair["final_winner"] == "tie"
                and pair["comparator_trials"] == []
                for pair in result["pairs"]
            )
        )

    def test_objective_only_equal_failures_tie_and_sole_pass_wins(self) -> None:
        self.fixture.use_v3_objective()

        self.fixture.set_verifier(
            _PASSING_VERIFIER.replace(
                'passed = answer.is_file() and bool(answer.read_text(encoding="utf-8").strip())',
                "passed = False",
            )
        )
        suite = load_suite(self.fixture.manifest_path)
        both_fail_provider = self.fixture.provider()
        with EvalRunner(suite, both_fail_provider) as runner:
            both_fail = runner.run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.fixture.root / "objective-both-fail",
            )
        self.assertTrue(
            all(
                pair["final_winner"] == "tie"
                and pair["winner_basis"] == "verifier-pass-v1"
                for pair in both_fail["pairs"]
            )
        )
        self.assertFalse(both_fail["passed"])

        self.fixture.set_verifier(
            _PASSING_VERIFIER.replace(
                'passed = answer.is_file() and bool(answer.read_text(encoding="utf-8").strip())',
                'passed = answer.is_file() and "improved" in answer.read_text(encoding="utf-8")',
            )
        )
        suite = load_suite(self.fixture.manifest_path)
        sole_pass_provider = self.fixture.provider()
        with EvalRunner(suite, sole_pass_provider) as runner:
            sole_pass = runner.run(
                RunSelection(comparison_ids=("without-current",)),
                output_dir=self.fixture.root / "objective-sole-pass",
            )
        self.assertTrue(
            all(
                pair["final_winner"] == "treatment"
                and pair["winner_basis"] == "verifier-pass-v1"
                for pair in sole_pass["pairs"]
            )
        )

    def test_objective_only_dry_run_is_write_free_and_holdout_is_denied(self) -> None:
        self.fixture.use_v3_objective()
        suite = load_suite(self.fixture.manifest_path)
        provider = self.fixture.provider()
        output = self.fixture.root / "unused-output"
        with EvalRunner(suite, provider) as runner:
            dry_run = runner.run(RunSelection(), output_dir=output, dry_run=True)
            self.assertEqual(dry_run["execution_mode"], "objective_only")
            self.assertIsNone(dry_run["profile_locks_valid"])
            self.assertIsNone(dry_run["protocol_locks_valid"])
            self.assertIsNone(dry_run["live_calibration_valid"])
            self.assertEqual(
                dry_run["preflight"]["plan"]["maximum_comparator_calls"], 0
            )
            self.assertEqual(
                dry_run["preflight"]["plan"]["maximum_comparator_exposure_usd"],
                0,
            )
            with self.assertRaisesRegex(
                RunnerError, "objective-only holdout authority"
            ):
                runner.preflight(RunSelection(split="holdout"))
            with self.assertRaisesRegex(
                RunnerError, "objective-only holdout authority"
            ):
                runner.prepare_holdout_plan(
                    output_path=self.fixture.root / "holdout.json",
                    plan_id="objective-holdout",
                    reviewers=("reviewer-a", "reviewer-b"),
                    freeze_record="freeze-record",
                    seal_record="seal-record",
                )
        self.assertFalse(output.exists())
        self.assertEqual(provider.agent_requests, [])
        self.assertEqual(provider.comparator_requests, [])


class CheckedInSuiteTests(unittest.TestCase):
    def test_holdout_plan_schema_is_strict_and_documents_trust_boundary(self) -> None:
        schema = json.loads(
            (HARNESS_ROOT / "holdout-plan.schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["enum"], [2, 3])
        self.assertEqual(len(schema["oneOf"]), 3)
        self.assertIn("source_bindings", schema["properties"])
        self.assertIn("evaluation_mode", schema["properties"])
        provenance = schema["properties"]["provenance"]
        self.assertEqual(
            provenance["properties"]["assurance"]["const"],
            "trusted-reviewed-attestation",
        )
        self.assertEqual(
            provenance["properties"]["privacy_claim"]["const"],
            "not-a-cryptographic-privacy-proof",
        )
        self.assertNotIn("comparator_release_sha256", schema["required"])
        self.assertNotIn("comparator_calibration_evidence_sha256", schema["required"])
        self.assertIn("generator_provider", schema["required"])
        self.assertIn("consumption_record_path", schema["required"])
        self.assertIn(
            "release_case_fingerprint",
            schema["properties"]["cases"]["items"]["required"],
        )
        self.assertIn(
            "shared_tree_sha256",
            schema["properties"]["cases"]["items"]["required"],
        )

    def test_suite_schema_supports_strict_v2_through_v6_modes(self) -> None:
        schema = json.loads(
            (HARNESS_ROOT / "suite.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [branch["$ref"] for branch in schema["oneOf"]],
            [
                "#/$defs/suiteV2",
                "#/$defs/suiteV3",
                "#/$defs/suiteV4",
                "#/$defs/suiteV5",
                "#/$defs/suiteV6",
            ],
        )
        self.assertEqual(
            schema["$defs"]["suiteV2"]["properties"]["schema_version"]["const"], 2
        )
        self.assertEqual(
            schema["$defs"]["suiteV3"]["properties"]["schema_version"]["const"], 3
        )
        self.assertEqual(
            schema["$defs"]["suiteV3"]["properties"]["evaluation_mode"]["enum"],
            ["judged", "objective_only"],
        )
        self.assertEqual(
            schema["$defs"]["suiteV4"]["properties"]["schema_version"]["const"], 4
        )
        self.assertIn("shared_verifier_dir", schema["$defs"]["suiteV4"]["required"])
        self.assertEqual(
            schema["$defs"]["suiteV5"]["properties"]["schema_version"]["const"], 5
        )
        self.assertIn("holdout", schema["$defs"]["suiteV5"]["required"])
        self.assertEqual(
            schema["$defs"]["suiteV6"]["properties"]["schema_version"]["const"], 6
        )
        self.assertIn("providerV6", schema["$defs"])

    def test_gcc_attestation_includes_derived_driver_closure(self) -> None:
        gcc = shutil.which("gcc")
        if gcc is None:
            self.skipTest("GCC is unavailable")
        attestation = _attest_executable(Path(gcc), "gcc")
        self.assertIsNotNone(attestation.gcc_exec_prefix)
        self.assertEqual(
            {item.logical_name for item in attestation.derived_executables},
            {"cc1", "collect2", "lto-wrapper"},
        )
        for item in attestation.derived_executables:
            self.assertEqual(len(item.sha256), 64)
            self.assertGreater(item.size, 0)
            self.assertIn("derived GCC component", item.version)

    def test_manifest_is_loadable_and_public_cases_are_not_holdouts(self) -> None:
        suite = load_suite(HARNESS_ROOT / "suite.json")
        splits = [case.split for case in suite.cases]
        self.assertEqual(len(suite.cases), 17)
        self.assertEqual(splits.count("train"), 10)
        self.assertEqual(splits.count("validation"), 7)
        self.assertNotIn("holdout", splits)

    def test_models_and_frozen_original_are_pinned(self) -> None:
        suite = load_suite(HARNESS_ROOT / "suite.json")
        authority = json.loads(
            (HARNESS_ROOT / "baseline-authority.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            authority,
            {
                "schema_version": 1,
                "original_commit": "21db6fdad124c2b0769dee6466a23ebddc0264bd",
            },
        )
        self.assertEqual(suite.provider.model, "claude-haiku-4-5-20251001")
        self.assertEqual(suite.comparator.model, "claude-sonnet-5")
        historical_refs = {
            variant.id: variant.git_ref
            for variant in suite.variants
            if variant.id == "original"
        }
        self.assertEqual(
            historical_refs,
            {"original": authority["original_commit"]},
        )
        for git_ref in historical_refs.values():
            self.assertRegex(git_ref or "", r"^[0-9a-f]{40}$")
        candidate = next(
            variant for variant in suite.variants if variant.id == "candidate"
        )
        self.assertEqual(candidate.source_ref, "HEAD")

    def test_every_case_declares_only_its_exact_external_tools(self) -> None:
        suite = load_suite(HARNESS_ROOT / "suite.json")
        expected = {
            "software-symbol-verification": ("node",),
            "software-representative-performance": ("go",),
            "software-public-api-compatibility": ("node",),
            "software-concurrent-store": ("as", "gcc", "go", "ld"),
            "testing-parser-boundaries": ("go",),
            "testing-state-machine-sequences": ("node",),
            "testing-concurrency-flake": ("as", "gcc", "go", "ld"),
            "testing-event-idempotency": ("node",),
        }
        for case in suite.cases:
            self.assertEqual(case.verifier.required_tools, expected.get(case.id, ()))


if __name__ == "__main__":
    unittest.main()
