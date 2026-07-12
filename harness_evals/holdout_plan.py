"""Strict loading for externally reviewed holdout release plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .manifest import CODEX_REASONING_EFFORTS


MAX_PLAN_BYTES = 4 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SKILL = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MIN_CASES_PER_SKILL = 8


class HoldoutPlanError(ValueError):
    """Raised when a holdout plan is malformed or its attestation is incomplete."""


@dataclass(frozen=True)
class HoldoutComparisonBinding:
    id: str
    control: str
    treatment: str
    repetitions: int
    comparator_order: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "control": self.control,
            "treatment": self.treatment,
            "repetitions": self.repetitions,
            "comparator_order": self.comparator_order,
        }


@dataclass(frozen=True)
class HoldoutCaseBinding:
    id: str
    case_tree_sha256: str
    shared_tree_sha256: str | None
    release_case_fingerprint: str
    skill: str
    critical_expectations: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_tree_sha256": self.case_tree_sha256,
            "shared_tree_sha256": self.shared_tree_sha256,
            "release_case_fingerprint": self.release_case_fingerprint,
            "skill": self.skill,
            "critical_expectations": list(self.critical_expectations),
        }


@dataclass(frozen=True)
class HoldoutProviderBinding:
    name: str
    version: str
    requested_model: str
    executable_sha256: str | None
    reasoning_effort: str | None
    billing_basis: str
    protocol_lock: str | None
    protocol_lock_sha256: str | None
    execution_policy: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "requested_model": self.requested_model,
            "executable_sha256": self.executable_sha256,
            "reasoning_effort": self.reasoning_effort,
            "billing_basis": self.billing_basis,
            "protocol_lock": self.protocol_lock,
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "execution_policy": dict(self.execution_policy),
        }


@dataclass(frozen=True)
class HoldoutPlan:
    path: Path
    raw_bytes: bytes
    sha256: str
    plan_id: str
    manifest_sha256: str
    comparator_release_sha256: str
    comparator_calibration_evidence_sha256: str | None
    generator_provider: HoldoutProviderBinding
    candidate_commit: str
    original_commit: str
    consumption_record_path: Path
    seed: int
    comparison_profile: tuple[HoldoutComparisonBinding, ...]
    cases: tuple[HoldoutCaseBinding, ...]
    reviewed_by: tuple[str, ...]
    freeze_record: str
    seal_record: str

    def assert_unchanged(self) -> None:
        """Fail if the reviewed plan bytes drift after validation."""

        observed = _read_plan_bytes(self.path, action="reread")
        if observed != self.raw_bytes:
            raise HoldoutPlanError("holdout plan bytes drifted after validation")

    def as_evidence(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "path": str(self.path),
            "sha256": self.sha256,
            "manifest_sha256": self.manifest_sha256,
            "comparator_release_sha256": self.comparator_release_sha256,
            "comparator_calibration_evidence_sha256": (
                self.comparator_calibration_evidence_sha256
            ),
            "generator_provider": self.generator_provider.as_json(),
            "candidate_commit": self.candidate_commit,
            "original_commit": self.original_commit,
            "consumption_record_path": str(self.consumption_record_path),
            "seed": self.seed,
            "comparison_profile": [item.as_json() for item in self.comparison_profile],
            "cases": [item.as_json() for item in self.cases],
            "provenance": {
                "assurance": "trusted-reviewed-attestation",
                "privacy_claim": "not-a-cryptographic-privacy-proof",
                "frozen_before_candidate_evaluation": True,
                "sealed_after_independent_review": True,
                "reviewed_by": list(self.reviewed_by),
                "freeze_record": self.freeze_record,
                "seal_record": self.seal_record,
            },
        }


def _reject_constant(value: str) -> None:
    raise HoldoutPlanError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HoldoutPlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _read_plan_bytes(path: Path, *, action: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HoldoutPlanError(f"cannot {action} holdout plan: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HoldoutPlanError("holdout plan must be a regular, non-symlink file")
    if metadata.st_uid != os.getuid():
        raise HoldoutPlanError("holdout plan must be owned by the current uid")
    if metadata.st_nlink != 1:
        raise HoldoutPlanError("holdout plan must have exactly one hard link")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HoldoutPlanError("holdout plan must not grant group or other permissions")
    if metadata.st_size > MAX_PLAN_BYTES:
        raise HoldoutPlanError("holdout plan exceeds the 4 MiB size limit")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HoldoutPlanError(f"cannot {action} holdout plan: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise HoldoutPlanError("holdout plan must remain a regular file")
        if _file_fingerprint(opened) != _file_fingerprint(metadata):
            raise HoldoutPlanError("holdout plan changed while it was opened")
        if opened.st_uid != os.getuid():
            raise HoldoutPlanError("holdout plan owner changed during validation")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise HoldoutPlanError("holdout plan permissions changed during validation")
        chunks: list[bytes] = []
        remaining = MAX_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        if len(raw_bytes) > MAX_PLAN_BYTES:
            raise HoldoutPlanError("holdout plan exceeds the 4 MiB size limit")
        if _file_fingerprint(os.fstat(descriptor)) != _file_fingerprint(opened):
            raise HoldoutPlanError("holdout plan changed while it was read")
        return raw_bytes
    finally:
        os.close(descriptor)


def _object(
    value: Any,
    location: str,
    *,
    required: set[str],
    allowed: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HoldoutPlanError(f"{location} must be an object")
    missing = sorted(required - value.keys())
    if missing:
        raise HoldoutPlanError(
            f"{location} is missing required keys: {', '.join(missing)}"
        )
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise HoldoutPlanError(f"{location} has unknown keys: {', '.join(unknown)}")
    return value


def _string(
    value: Any, location: str, *, pattern: re.Pattern[str] | None = None
) -> str:
    if not isinstance(value, str) or not value:
        raise HoldoutPlanError(f"{location} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise HoldoutPlanError(f"{location} has an invalid value: {value!r}")
    return value


def _integer(value: Any, location: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HoldoutPlanError(f"{location} must be an integer >= {minimum}")
    return value


def _optional_sha256(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _string(value, location, pattern=_SHA256)


def _optional_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _string(value, location)


def _optional_relative_path(value: Any, location: str) -> str | None:
    raw = _optional_string(value, location)
    if raw is None:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise HoldoutPlanError(
            f"{location} must be a suite-relative path without parent traversal"
        )
    normalized = path.as_posix()
    if normalized != raw:
        raise HoldoutPlanError(f"{location} must use canonical POSIX path syntax")
    return normalized


def _absolute_path(value: Any, location: str) -> Path:
    raw = _string(value, location)
    path = Path(raw)
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise HoldoutPlanError(f"{location} must be a canonical absolute path")
    return path


def _strings(
    value: Any,
    location: str,
    *,
    minimum: int,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise HoldoutPlanError(f"{location} must contain at least {minimum} item(s)")
    result = tuple(
        _string(item, f"{location}[{index}]", pattern=pattern)
        for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise HoldoutPlanError(f"{location} must not contain duplicates")
    return result


def _parse_comparison(value: Any, index: int) -> HoldoutComparisonBinding:
    location = f"comparison_profile[{index}]"
    fields = {"id", "control", "treatment", "repetitions", "comparator_order"}
    item = _object(value, location, required=fields, allowed=fields)
    return HoldoutComparisonBinding(
        id=_string(item["id"], f"{location}.id", pattern=_IDENTIFIER),
        control=_string(item["control"], f"{location}.control", pattern=_IDENTIFIER),
        treatment=_string(
            item["treatment"], f"{location}.treatment", pattern=_IDENTIFIER
        ),
        repetitions=_integer(item["repetitions"], f"{location}.repetitions", minimum=1),
        comparator_order=_string(
            item["comparator_order"], f"{location}.comparator_order"
        ),
    )


def _parse_case(value: Any, index: int) -> HoldoutCaseBinding:
    location = f"cases[{index}]"
    fields = {
        "id",
        "case_tree_sha256",
        "shared_tree_sha256",
        "release_case_fingerprint",
        "skill",
        "critical_expectations",
    }
    item = _object(value, location, required=fields, allowed=fields)
    return HoldoutCaseBinding(
        id=_string(item["id"], f"{location}.id", pattern=_IDENTIFIER),
        case_tree_sha256=_string(
            item["case_tree_sha256"],
            f"{location}.case_tree_sha256",
            pattern=_SHA256,
        ),
        shared_tree_sha256=_optional_sha256(
            item["shared_tree_sha256"],
            f"{location}.shared_tree_sha256",
        ),
        release_case_fingerprint=_string(
            item["release_case_fingerprint"],
            f"{location}.release_case_fingerprint",
            pattern=_SHA256,
        ),
        skill=_string(item["skill"], f"{location}.skill", pattern=_SKILL),
        critical_expectations=_strings(
            item["critical_expectations"],
            f"{location}.critical_expectations",
            minimum=1,
            pattern=_IDENTIFIER,
        ),
    )


def _parse_provider(value: Any) -> HoldoutProviderBinding:
    fields = {
        "name",
        "version",
        "requested_model",
        "executable_sha256",
        "reasoning_effort",
        "billing_basis",
        "protocol_lock",
        "protocol_lock_sha256",
        "execution_policy",
    }
    item = _object(value, "generator_provider", required=fields, allowed=fields)
    requested_model = _string(
        item["requested_model"], "generator_provider.requested_model"
    )
    executable_sha256 = _optional_sha256(
        item["executable_sha256"], "generator_provider.executable_sha256"
    )
    billing_basis = _string(item["billing_basis"], "generator_provider.billing_basis")
    if billing_basis not in {"metered_api", "chatgpt_subscription"}:
        raise HoldoutPlanError(
            "generator_provider.billing_basis must be 'metered_api' or "
            "'chatgpt_subscription'"
        )
    protocol_lock = _optional_relative_path(
        item["protocol_lock"], "generator_provider.protocol_lock"
    )
    protocol_lock_sha256 = _optional_sha256(
        item["protocol_lock_sha256"], "generator_provider.protocol_lock_sha256"
    )
    reasoning_effort = _optional_string(
        item["reasoning_effort"], "generator_provider.reasoning_effort"
    )
    if (protocol_lock is None) != (protocol_lock_sha256 is None):
        raise HoldoutPlanError(
            "generator_provider protocol_lock and protocol_lock_sha256 must both be "
            "null or both be set"
        )
    policy = _object(
        item["execution_policy"],
        "generator_provider.execution_policy",
        required={"concurrency", "release_authoritative"},
        allowed={"concurrency", "release_authoritative"},
    )
    concurrency = _string(
        policy["concurrency"], "generator_provider.execution_policy.concurrency"
    )
    release_authoritative = policy["release_authoritative"]
    if type(release_authoritative) is not bool or (
        concurrency,
        release_authoritative,
    ) not in {
        ("concurrent", True),
        ("serialized", False),
    }:
        raise HoldoutPlanError(
            "generator_provider.execution_policy is not a supported policy"
        )
    if billing_basis == "metered_api" and (
        reasoning_effort is not None
        or protocol_lock is not None
        or (concurrency, release_authoritative) != ("concurrent", True)
    ):
        raise HoldoutPlanError(
            "metered generator binding requires null Codex fields and the "
            "concurrent authoritative policy"
        )
    if billing_basis == "chatgpt_subscription" and (
        reasoning_effort is None
        or protocol_lock is None
        or executable_sha256 is None
        or (concurrency, release_authoritative) != ("serialized", False)
    ):
        raise HoldoutPlanError(
            "ChatGPT subscription generator binding requires Codex provenance and "
            "the serialized non-authoritative policy"
        )
    if billing_basis == "chatgpt_subscription":
        supported_efforts = CODEX_REASONING_EFFORTS.get(requested_model)
        if supported_efforts is None or reasoning_effort not in supported_efforts:
            raise HoldoutPlanError(
                "ChatGPT subscription generator binding has an unsupported Codex "
                "model or reasoning effort"
            )
    return HoldoutProviderBinding(
        name=_string(item["name"], "generator_provider.name"),
        version=_string(item["version"], "generator_provider.version"),
        requested_model=requested_model,
        executable_sha256=executable_sha256,
        reasoning_effort=reasoning_effort,
        billing_basis=billing_basis,
        protocol_lock=protocol_lock,
        protocol_lock_sha256=protocol_lock_sha256,
        execution_policy={
            "concurrency": concurrency,
            "release_authoritative": release_authoritative,
        },
    )


def load_holdout_plan(path: Path) -> HoldoutPlan:
    """Load a sealed, externally supplied trusted-review attestation."""

    supplied = Path(path).expanduser()
    raw_bytes = _read_plan_bytes(supplied, action="read")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise HoldoutPlanError(f"cannot resolve holdout plan: {exc}") from exc
    try:
        text = raw_bytes.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise HoldoutPlanError("holdout plan must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise HoldoutPlanError(f"holdout plan is invalid JSON: {exc}") from exc

    fields = {
        "schema_version",
        "plan_id",
        "status",
        "manifest_sha256",
        "comparator_release_sha256",
        "comparator_calibration_evidence_sha256",
        "generator_provider",
        "candidate_commit",
        "original_commit",
        "consumption_record_path",
        "seed",
        "comparison_profile",
        "cases",
        "provenance",
    }
    data = _object(raw, "holdout plan", required=fields, allowed=fields)
    if _integer(data["schema_version"], "schema_version", minimum=2) != 2:
        raise HoldoutPlanError("schema_version must be 2")
    if _string(data["status"], "status") != "sealed":
        raise HoldoutPlanError("status must be 'sealed'")

    comparison_values = data["comparison_profile"]
    if not isinstance(comparison_values, list) or not comparison_values:
        raise HoldoutPlanError("comparison_profile must be a non-empty array")
    comparison_profile = tuple(
        _parse_comparison(item, index) for index, item in enumerate(comparison_values)
    )
    if len({item.id for item in comparison_profile}) != len(comparison_profile):
        raise HoldoutPlanError("comparison_profile ids must be unique")

    case_values = data["cases"]
    if not isinstance(case_values, list) or not case_values:
        raise HoldoutPlanError("cases must be a non-empty array")
    cases = tuple(_parse_case(item, index) for index, item in enumerate(case_values))
    if len({item.id for item in cases}) != len(cases):
        raise HoldoutPlanError("case ids must be unique")
    if len({item.case_tree_sha256 for item in cases}) != len(cases):
        raise HoldoutPlanError("case_tree_sha256 values must be globally unique")
    if len({item.release_case_fingerprint for item in cases}) != len(cases):
        raise HoldoutPlanError(
            "release_case_fingerprint values must be globally unique"
        )
    skill_counts = {
        skill: sum(item.skill == skill for item in cases)
        for skill in {item.skill for item in cases}
    }
    if any(count < _MIN_CASES_PER_SKILL for count in skill_counts.values()):
        raise HoldoutPlanError(
            "each holdout skill needs at least 8 unique task-content fingerprints"
        )

    provenance_fields = {
        "assurance",
        "privacy_claim",
        "frozen_before_candidate_evaluation",
        "sealed_after_independent_review",
        "reviewed_by",
        "freeze_record",
        "seal_record",
    }
    provenance = _object(
        data["provenance"],
        "provenance",
        required=provenance_fields,
        allowed=provenance_fields,
    )
    if provenance["assurance"] != "trusted-reviewed-attestation":
        raise HoldoutPlanError(
            "provenance.assurance must be 'trusted-reviewed-attestation'"
        )
    if provenance["privacy_claim"] != "not-a-cryptographic-privacy-proof":
        raise HoldoutPlanError(
            "provenance.privacy_claim must disclaim cryptographic privacy proof"
        )
    for field in (
        "frozen_before_candidate_evaluation",
        "sealed_after_independent_review",
    ):
        if provenance[field] is not True:
            raise HoldoutPlanError(f"provenance.{field} must be true")

    return HoldoutPlan(
        path=resolved,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        plan_id=_string(data["plan_id"], "plan_id", pattern=_IDENTIFIER),
        manifest_sha256=_string(
            data["manifest_sha256"], "manifest_sha256", pattern=_SHA256
        ),
        comparator_release_sha256=_string(
            data["comparator_release_sha256"],
            "comparator_release_sha256",
            pattern=_SHA256,
        ),
        comparator_calibration_evidence_sha256=_optional_sha256(
            data["comparator_calibration_evidence_sha256"],
            "comparator_calibration_evidence_sha256",
        ),
        generator_provider=_parse_provider(data["generator_provider"]),
        candidate_commit=_string(
            data["candidate_commit"], "candidate_commit", pattern=_COMMIT
        ),
        original_commit=_string(
            data["original_commit"], "original_commit", pattern=_COMMIT
        ),
        consumption_record_path=_absolute_path(
            data["consumption_record_path"], "consumption_record_path"
        ),
        seed=_integer(data["seed"], "seed", minimum=0),
        comparison_profile=comparison_profile,
        cases=cases,
        reviewed_by=_strings(
            provenance["reviewed_by"], "provenance.reviewed_by", minimum=1
        ),
        freeze_record=_string(provenance["freeze_record"], "provenance.freeze_record"),
        seal_record=_string(provenance["seal_record"], "provenance.seal_record"),
    )
