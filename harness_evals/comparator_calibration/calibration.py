"""Validate the locked comparator corpus and score offline judge evidence."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import re
import stat
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


EVALUATOR_VERSION = "2.3.0"
SHARED_RUNTIME_ADAPTER_ID = "shared-harness-claude-cli-v1"
PRODUCTION_TIMEOUT_SECONDS = 300
PRODUCTION_PER_INVOCATION_BUDGET_USD = 1.0
PRODUCTION_RUN_BUDGET_USD = 100.0
PRODUCTION_CLI_ARGS = (
    "--print",
    "--output-format",
    "json",
    "--model",
    "claude-sonnet-5",
    "--effort",
    "high",
    "--max-budget-usd",
    "1.00",
    "--no-session-persistence",
    "--safe-mode",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--mcp-config",
    "{}",
    "--tools",
    "",
)
CRITERIA = (
    "functional_correctness",
    "security_reliability",
    "maintainability_extensibility",
    "performance_efficiency",
    "simplicity_scope_discipline",
)
OUTCOMES = ("A", "B", "tie", "tradeoff", "unqualified")
REQUIREMENT_STATUSES = {"satisfied", "violated", "unknown"}
ELIGIBILITY = {"eligible", "ineligible", "unknown"}
WINNERS = {"A", "B", "tie"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_TRIAL_KEYS = frozenset(
    {
        "pair_id",
        "repetition",
        "order",
        "invocation_id",
        "request",
        "request_sha256",
        "raw_response",
        "raw_response_sha256",
        "parsed_response_sha256",
        "command_sha256",
        "stdin_sha256",
        "provider",
        "provider_version",
        "requested_model",
        "actual_models",
        "executable_sha256",
        "spend_attempt_id",
        "cost_usd",
        "executor",
        "response",
    }
)
EXECUTOR_EVIDENCE_KEYS = frozenset(
    {
        "kind",
        "enforced",
        "provider_version",
        "executable_path",
        "executable_identity",
        "executable_sha256",
        "execution_source",
        "execution_descriptor_path",
        "execution_copy_path",
        "command_executable",
        "systemd_version",
        "properties",
        "environment_mode",
        "process_namespace",
        "stdin_sha256",
        "remote_service_attestation",
    }
)
_PATCH_RESULT_CACHE: dict[str, dict[str, Any]] = {}


class CalibrationError(ValueError):
    """Raised when locked inputs or offline evidence violate their contract."""


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict[str, Any]
    manifest_schema: dict[str, Any]
    rubric: dict[str, Any]
    request_template: dict[str, Any]
    response_schema: dict[str, Any]
    evidence_schema: dict[str, Any]
    release: dict[str, Any]


def _reject_constant(value: str) -> None:
    raise CalibrationError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CalibrationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    """Load one strict UTF-8 JSON object."""

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CalibrationError(f"cannot load strict JSON from {path}: {exc}") from exc
    return parse_json_object(raw, str(path))


def parse_json_object(raw: str, location: str) -> dict[str, Any]:
    """Parse one strict JSON object while rejecting duplicate and non-finite values."""

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CalibrationError(
            f"cannot parse strict JSON from {location}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CalibrationError(f"{location} must contain a JSON object")
    return value


def load_bundle(
    root: Path, release_name: str = "release.json", *, allow_test_release: bool = False
) -> Bundle:
    """Load locked artifacts and reject implicit use of a fake release."""

    resolved = root.resolve()
    bundle = Bundle(
        root=resolved,
        manifest=load_json(resolved / "manifest.json"),
        manifest_schema=load_json(resolved / "manifest.schema.json"),
        rubric=load_json(resolved / "rubric.json"),
        request_template=load_json(resolved / "request-template.json"),
        response_schema=load_json(resolved / "response.schema.json"),
        evidence_schema=load_json(resolved / "evidence.schema.json"),
        release=load_json(resolved / release_name),
    )
    if bundle.release.get("test_release") is True and not allow_test_release:
        raise CalibrationError("test release requires explicit allow_test_release=True")
    return bundle


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite_original_commit(suite_path: Path) -> str:
    """Read the one git-ref baseline from exact suite JSON."""

    suite = load_json(suite_path)
    variants = suite.get("variants")
    if not isinstance(variants, list):
        raise CalibrationError("suite variants must be an array")
    originals = [
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("id") == "original"
    ]
    if len(originals) != 1 or originals[0].get("kind") != "git_ref":
        raise CalibrationError(
            "suite original variant must be exactly one git_ref baseline"
        )
    commit = originals[0].get("git_ref")
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise CalibrationError(
            "suite original git_ref must be a 40-character lowercase commit"
        )
    return commit


def baseline_authority_commit(authority_path: Path) -> str:
    """Read the independent frozen-baseline authority artifact."""

    authority = load_json(authority_path)
    if set(authority) != {"schema_version", "original_commit"}:
        raise CalibrationError("baseline authority fields are invalid")
    if authority["schema_version"] != 1:
        raise CalibrationError("baseline authority schema version is invalid")
    commit = authority["original_commit"]
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise CalibrationError(
            "baseline authority commit must be 40 lowercase hexadecimal characters"
        )
    return commit


def require_baseline_authority(suite_path: Path, authority_path: Path) -> str:
    """Require suite and independent authority to name the same baseline."""

    authority_commit = baseline_authority_commit(authority_path)
    if suite_original_commit(suite_path) != authority_commit:
        raise CalibrationError("suite original git_ref differs from baseline authority")
    return authority_commit


def _exact(value: Any, keys: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalibrationError(f"{location} must be an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise CalibrationError(
            f"{location} keys differ; missing={missing or 'none'} extra={extra or 'none'}"
        )
    return value


def _text(value: Any, location: str, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise CalibrationError(
            f"{location} must contain at least {minimum} non-whitespace characters"
        )
    return value


def _integer(value: Any, location: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CalibrationError(f"{location} must be an integer >= {minimum}")
    return value


def _rate(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{location} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise CalibrationError(f"{location} must be finite and between 0 and 1")
    return result


def _safe_path(value: str, location: str) -> PurePosixPath:
    path = PurePosixPath(_text(value, location))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise CalibrationError(f"{location} must be a safe relative path")
    return path


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_hunks(diff: str, location: str) -> int:
    lines = diff.splitlines()
    hunk_count = 0
    changed = 0
    index = 0
    while index < len(lines):
        match = HUNK_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        hunk_count += 1
        old_expected = int(match.group(2) or "1")
        new_expected = int(match.group(4) or "1")
        old_seen = 0
        new_seen = 0
        removed: list[str] = []
        added: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if HUNK_RE.match(line) or line.startswith("diff --git "):
                break
            if line.startswith(" "):
                old_seen += 1
                new_seen += 1
            elif line.startswith("-") and not line.startswith("---"):
                old_seen += 1
                removed.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                new_seen += 1
                added.append(line[1:])
            elif line == r"\ No newline at end of file":
                pass
            else:
                break
            index += 1
        if (old_seen, new_seen) != (old_expected, new_expected):
            raise CalibrationError(
                f"{location} hunk counts declare {old_expected}/{new_expected} "
                f"but contain {old_seen}/{new_seen}"
            )
        if removed != added:
            changed += len(removed) + len(added)
    if hunk_count == 0 or changed == 0:
        raise CalibrationError(f"{location} must contain a non-noop unified hunk")
    return hunk_count


def _validate_patch(pair: dict[str, Any], side: str) -> dict[str, Any]:
    location = f"pair {pair['id']} diff_{side.lower()}"
    diff = _text(pair[f"diff_{side.lower()}"], location)
    cache_key = canonical_sha256(
        {"base_files": pair["base_files"], "diff": diff, "side": side}
    )
    cached = _PATCH_RESULT_CACHE.get(cache_key)
    if cached is not None:
        return {**cached, "post_files": dict(cached["post_files"])}
    if not diff.startswith("diff --git a/"):
        raise CalibrationError(f"{location} must start with a git diff header")
    hunk_count = _validate_hunks(diff, location)
    with tempfile.TemporaryDirectory(prefix="comparator-calibration-") as temporary:
        root = Path(temporary)
        for raw_path, content in pair["base_files"].items():
            relative = _safe_path(raw_path, f"{location}.base_files")
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        before = _tree_sha256(root)
        numstat = subprocess.run(
            ["git", "apply", "--numstat", "-"],
            cwd=root,
            input=diff,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if numstat.returncode != 0 or not numstat.stdout.strip():
            raise CalibrationError(
                f"{location} fails git apply --numstat -: {numstat.stderr.strip()}"
            )
        additions = 0
        deletions = 0
        for line in numstat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                raise CalibrationError(f"{location} has unsupported numstat: {line}")
            additions += int(parts[0])
            deletions += int(parts[1])
        if additions + deletions == 0:
            raise CalibrationError(f"{location} is a zero-change patch")
        checked = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=root,
            input=diff,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if checked.returncode != 0:
            raise CalibrationError(
                f"{location} does not apply to base files: {checked.stderr.strip()}"
            )
        applied = subprocess.run(
            ["git", "apply", "-"],
            cwd=root,
            input=diff,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if applied.returncode != 0 or _tree_sha256(root) == before:
            raise CalibrationError(f"{location} did not produce a changed tree")
        post_files = {
            path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(item for item in root.rglob("*") if item.is_file())
        }
        post_text = "\n".join(post_files.values())
    result = {
        "hunks": hunk_count,
        "additions": additions,
        "deletions": deletions,
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "post_text": post_text,
        "post_files": post_files,
    }
    _PATCH_RESULT_CACHE[cache_key] = result
    return {**result, "post_files": dict(post_files)}


def derive_eligibility(checks: dict[str, str]) -> str:
    values = set(checks.values())
    if not values <= REQUIREMENT_STATUSES:
        raise CalibrationError("requirement checks contain an unsupported status")
    if "violated" in values:
        return "ineligible"
    if "unknown" in values:
        return "unknown"
    return "eligible"


def derive_outcome(eligibility: dict[str, str], criteria: dict[str, str] | None) -> str:
    """Apply eligibility first, then Pareto comparison only when both are eligible."""

    if set(eligibility) != {"A", "B"} or not set(eligibility.values()) <= ELIGIBILITY:
        raise CalibrationError("eligibility must contain canonical A and B decisions")
    if eligibility == {"A": "eligible", "B": "ineligible"}:
        if criteria is not None:
            raise CalibrationError(
                "criteria are not applicable unless both candidates qualify"
            )
        return "A"
    if eligibility == {"A": "ineligible", "B": "eligible"}:
        if criteria is not None:
            raise CalibrationError(
                "criteria are not applicable unless both candidates qualify"
            )
        return "B"
    if eligibility != {"A": "eligible", "B": "eligible"}:
        if criteria is not None:
            raise CalibrationError(
                "criteria are not applicable unless both candidates qualify"
            )
        return "unqualified"
    if (
        criteria is None
        or set(criteria) != set(CRITERIA)
        or not set(criteria.values()) <= WINNERS
    ):
        raise CalibrationError(
            "both eligible candidates need the exact criterion vector"
        )
    values = set(criteria.values())
    if "A" in values and "B" in values:
        return "tradeoff"
    if "A" in values:
        return "A"
    if "B" in values:
        return "B"
    return "tie"


def _label_set(
    pair: dict[str, Any],
    value: Any,
    location: str,
    *,
    resolution: bool = False,
    legacy: bool = False,
) -> dict[str, Any]:
    keys = {"reviewer_id", "eligibility", "criteria", "rationale"}
    if resolution:
        keys.add("method")
    data = _exact(value, keys, location)
    reviewer_id = _text(data["reviewer_id"], f"{location}.reviewer_id")
    rationale = _text(data["rationale"], f"{location}.rationale", 20)
    if resolution and data["method"] not in {
        "independent-agreement",
        "root-resolution",
    }:
        raise CalibrationError(f"{location}.method is unsupported")
    requirement_ids = {
        requirement["id"] for requirement in pair["contract"]["requirements"]
    }
    eligibility_data = _exact(
        data["eligibility"], {"A", "B"}, f"{location}.eligibility"
    )
    eligibility: dict[str, str] = {}
    normalized_decisions: dict[str, dict[str, Any]] = {}
    for side in ("A", "B"):
        decision_keys = {"decision", "violations"}
        if not legacy:
            decision_keys.add("requirement_statuses")
        decision = _exact(
            eligibility_data[side],
            decision_keys,
            f"{location}.eligibility.{side}",
        )
        if decision["decision"] not in ELIGIBILITY:
            raise CalibrationError(f"{location}.eligibility.{side}.decision is invalid")
        violations = decision["violations"]
        if (
            not isinstance(violations, list)
            or len(violations) != len(set(violations))
            or not set(violations) <= requirement_ids
        ):
            raise CalibrationError(
                f"{location}.eligibility.{side}.violations is invalid"
            )
        if decision["decision"] == "ineligible" and not violations:
            raise CalibrationError(f"{location} ineligible decision needs a violation")
        if decision["decision"] != "ineligible" and violations:
            raise CalibrationError(
                f"{location} non-ineligible decision cannot list violations"
            )
        if legacy:
            requirement_statuses = {
                requirement_id: (
                    "violated" if requirement_id in violations else "satisfied"
                )
                for requirement_id in requirement_ids
            }
        else:
            requirement_statuses = _exact(
                decision["requirement_statuses"],
                requirement_ids,
                f"{location}.eligibility.{side}.requirement_statuses",
            )
            if not set(requirement_statuses.values()) <= REQUIREMENT_STATUSES:
                raise CalibrationError(
                    f"{location}.eligibility.{side}.requirement_statuses is invalid"
                )
            derived_violations = {
                requirement_id
                for requirement_id, status in requirement_statuses.items()
                if status == "violated"
            }
            if derived_violations != set(violations):
                raise CalibrationError(
                    f"{location}.eligibility.{side} violations differ from statuses"
                )
            if derive_eligibility(requirement_statuses) != decision["decision"]:
                raise CalibrationError(
                    f"{location}.eligibility.{side} decision differs from statuses"
                )
        eligibility[side] = decision["decision"]
        normalized_decisions[side] = {
            "decision": decision["decision"],
            "violations": tuple(violations),
            "requirement_statuses": dict(sorted(requirement_statuses.items())),
        }
    winners = data["criteria"]
    both_eligible = eligibility == {"A": "eligible", "B": "eligible"}
    criteria: dict[str, str] | None
    if winners is None:
        criteria = None
    elif (
        isinstance(winners, list)
        and len(winners) == len(CRITERIA)
        and set(winners) <= WINNERS
    ):
        criteria = dict(zip(CRITERIA, winners, strict=True))
    else:
        raise CalibrationError(
            f"{location}.criteria must be null or five locked winners"
        )
    if both_eligible and criteria is None:
        raise CalibrationError(
            f"{location} needs criteria because both candidates qualify"
        )
    if not both_eligible and criteria is not None and not legacy:
        raise CalibrationError(
            f"{location} must use null criteria for ineligible candidates"
        )
    if (
        not legacy
        and criteria is not None
        and criteria["performance_efficiency"] != "tie"
        and not pair["contract"]["performance_basis"]
    ):
        raise CalibrationError(
            f"{location} claims a performance winner without a performance basis"
        )
    if not legacy and criteria is not None:
        for criterion in ("functional_correctness", "security_reliability"):
            if (
                criteria[criterion] != "tie"
                and criterion not in pair["contract"]["qualitative_bases"]
            ):
                raise CalibrationError(
                    f"{location} claims {criterion} without a typed qualitative basis"
                )
    effective_criteria = criteria if both_eligible else None
    outcome = derive_outcome(eligibility, effective_criteria)
    return {
        "reviewer_id": reviewer_id,
        "eligibility": normalized_decisions,
        "criteria": criteria,
        "outcome": outcome,
        "rationale": rationale,
        "method": data.get("method"),
    }


def _same_labels(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["eligibility"] == right["eligibility"]
        and left["criteria"] == right["criteria"]
        and left["outcome"] == right["outcome"]
    )


def _same_semantic_labels(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare adjudicated semantics while ignoring derived status-map expansion."""

    return (
        {
            side: {
                "decision": left["eligibility"][side]["decision"],
                "violations": left["eligibility"][side]["violations"],
            }
            for side in ("A", "B")
        }
        == {
            side: {
                "decision": right["eligibility"][side]["decision"],
                "violations": right["eligibility"][side]["violations"],
            }
            for side in ("A", "B")
        }
        and left["criteria"] == right["criteria"]
        and left["outcome"] == right["outcome"]
    )


def validate_rubric(rubric: dict[str, Any]) -> dict[str, Any]:
    _exact(
        rubric,
        {
            "rubric_id",
            "version",
            "criteria",
            "admissibility",
            "outcome_rule",
            "production_decisive_policy",
            "evidence",
        },
        "rubric",
    )
    _text(rubric["rubric_id"], "rubric.rubric_id")
    if rubric["version"] != EVALUATOR_VERSION:
        raise CalibrationError("rubric version differs from evaluator version")
    criteria = rubric["criteria"]
    if not isinstance(criteria, list) or [item.get("id") for item in criteria] != list(
        CRITERIA
    ):
        raise CalibrationError("rubric criteria differ from the locked ordered set")
    for index, criterion in enumerate(criteria):
        _exact(criterion, {"id", "definition"}, f"rubric.criteria[{index}]")
        _text(criterion["definition"], f"rubric.criteria[{index}].definition", 20)
    policy = _exact(
        rubric["production_decisive_policy"],
        set(CRITERIA),
        "rubric.production_decisive_policy",
    )
    expected_policy = {
        "functional_correctness": "tie-only-until-calibration-support-expands",
        "security_reliability": "tie-only-until-calibration-support-expands",
        "maintainability_extensibility": "decisive",
        "performance_efficiency": "decisive-when-typed-basis-exists",
        "simplicity_scope_discipline": "decisive",
    }
    if policy != expected_policy:
        raise CalibrationError("rubric production criterion policy is stale")
    evidence = _exact(
        rubric["evidence"],
        {"minimum_observation_characters", "required_fields", "rule"},
        "rubric.evidence",
    )
    _integer(evidence["minimum_observation_characters"], "rubric evidence minimum", 20)
    _text(evidence["rule"], "rubric.evidence.rule", 20)
    if evidence["required_fields"] != [
        "artifact",
        "path",
        "line_start",
        "line_end",
        "quote",
        "semantic_anchor",
        "observation",
    ]:
        raise CalibrationError("rubric evidence fields differ from grounded schema")
    return {
        "rubric_id": rubric["rubric_id"],
        "version": rubric["version"],
        "sha256": canonical_sha256(rubric),
    }


def validate_locked_documents(bundle: Bundle) -> None:
    """Reject version or shape drift in the non-corpus locked documents."""

    manifest_schema = bundle.manifest_schema
    if (
        manifest_schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or manifest_schema.get("$id") != "manifest.schema.json"
        or manifest_schema.get("x-artifact-version") != EVALUATOR_VERSION
    ):
        raise CalibrationError("manifest schema identity or version is stale")
    template = _exact(
        bundle.request_template,
        {
            "template_id",
            "version",
            "serialization",
            "system_prompt",
            "user_payload_fields",
        },
        "request template",
    )
    if (
        template["template_id"] != "software-engineering-comparator-request-v3"
        or template["version"] != EVALUATOR_VERSION
        or template["serialization"] != "canonical-json-utf8-sort-keys-no-whitespace"
    ):
        raise CalibrationError(
            "request template identity, version, or serialization is stale"
        )
    _text(template["system_prompt"], "request template system prompt", 200)
    expected_fields = [
        "invocation_id",
        "task",
        "contract",
        "base_files",
        "candidate_A_diff",
        "candidate_B_diff",
        "rubric",
        "response_schema_sha256",
        "execution_limits",
    ]
    if template["user_payload_fields"] != expected_fields:
        raise CalibrationError("request template payload fields are stale")
    for name, schema, schema_id in (
        ("response", bundle.response_schema, "response.schema.json"),
        ("evidence", bundle.evidence_schema, "evidence.schema.json"),
    ):
        if (
            schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or schema.get("$id") != schema_id
            or schema.get("x-artifact-version") != EVALUATOR_VERSION
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
        ):
            raise CalibrationError(f"{name} schema identity or version is stale")


def validate_manifest(
    manifest: dict[str, Any], rubric: dict[str, Any]
) -> dict[str, Any]:
    """Validate corpus structure, patches, labels, coverage, and adjudication state."""

    validate_rubric(rubric)
    _exact(
        manifest,
        {"$schema", "schema_version", "corpus_id", "review_policy", "pairs"},
        "manifest",
    )
    if manifest["$schema"] != "manifest.schema.json" or manifest["schema_version"] != 2:
        raise CalibrationError("manifest schema lock is invalid")
    if manifest["corpus_id"] != "software-engineering-comparator-v2":
        raise CalibrationError("manifest corpus_id is invalid")
    review_policy = _exact(
        manifest["review_policy"],
        {
            "historical_rubric_version",
            "effective_rubric_version",
            "scoring_protocol_version",
            "resolution_authority",
            "history_rule",
        },
        "manifest.review_policy",
    )
    if (
        review_policy["historical_rubric_version"] != "2.0.0"
        or review_policy["effective_rubric_version"] != "2.1.0"
        or review_policy["scoring_protocol_version"] != EVALUATOR_VERSION
        or review_policy["resolution_authority"] != "resolution-reviewer-v2"
    ):
        raise CalibrationError("manifest review policy is stale")
    _text(review_policy["history_rule"], "manifest.review_policy.history_rule", 40)
    pairs = manifest["pairs"]
    if not isinstance(pairs, list) or len(pairs) < 30:
        raise CalibrationError("manifest needs at least 30 distinct pairs")
    pair_keys = {
        "id",
        "language",
        "categories",
        "critical",
        "task",
        "contract",
        "base_files",
        "diff_a",
        "diff_b",
        "provenance",
        "probes",
        "sentinel",
        "repetitions",
        "adjudication",
    }
    ids: set[str] = set()
    author_outcomes: Counter[str] = Counter()
    resolved_outcomes: Counter[str] = Counter()
    author_sentinel_outcomes: Counter[str] = Counter()
    resolved_sentinel_outcomes: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    patch_totals = Counter()
    unresolved: list[str] = []
    disagreements: list[str] = []
    re_review_disagreements: list[str] = []
    status_expansion_pairs: list[str] = []
    injection_sequence: list[tuple[str, str]] = []
    length_bias_kinds: Counter[str] = Counter()
    length_bias_sides: Counter[str] = Counter()
    reviewer_ids: set[str] = set()
    for index, raw_pair in enumerate(pairs):
        location = f"manifest.pairs[{index}]"
        pair = _exact(raw_pair, pair_keys, location)
        pair_id = _text(pair["id"], f"{location}.id")
        if ID_RE.fullmatch(pair_id) is None or pair_id in ids:
            raise CalibrationError(f"{location}.id is invalid or duplicated")
        ids.add(pair_id)
        if pair["language"] not in {
            "python",
            "javascript",
            "typescript",
            "go",
            "mixed",
        }:
            raise CalibrationError(f"{location}.language is unsupported")
        languages[pair["language"]] += 1
        _text(pair["task"], f"{location}.task", 20)
        if not isinstance(pair["critical"], bool) or not isinstance(
            pair["sentinel"], bool
        ):
            raise CalibrationError(f"{location} critical and sentinel must be boolean")
        expected_repetitions = 3 if pair["sentinel"] else 1
        if pair["repetitions"] != expected_repetitions or isinstance(
            pair["repetitions"], bool
        ):
            raise CalibrationError(
                f"{location}.repetitions must be {expected_repetitions}"
            )
        raw_categories = pair["categories"]
        if (
            not isinstance(raw_categories, list)
            or not raw_categories
            or len(raw_categories) != len(set(raw_categories))
            or not all(isinstance(item, str) and item for item in raw_categories)
        ):
            raise CalibrationError(f"{location}.categories is invalid")
        categories.update(raw_categories)
        contract = _exact(
            pair["contract"],
            {"requirements", "performance_basis", "qualitative_bases"},
            f"{location}.contract",
        )
        requirements = contract["requirements"]
        if not isinstance(requirements, list) or not requirements:
            raise CalibrationError(
                f"{location}.contract.requirements must be non-empty"
            )
        requirement_ids: set[str] = set()
        for req_index, raw_requirement in enumerate(requirements):
            requirement = _exact(
                raw_requirement,
                {"id", "kind", "text"},
                f"{location}.contract.requirements[{req_index}]",
            )
            if (
                ID_RE.fullmatch(str(requirement["id"])) is None
                or requirement["id"] in requirement_ids
            ):
                raise CalibrationError(
                    f"{location} requirement id is invalid or duplicated"
                )
            requirement_ids.add(requirement["id"])
            if requirement["kind"] not in {"required_behavior", "hard_constraint"}:
                raise CalibrationError(f"{location} requirement kind is invalid")
            _text(requirement["text"], f"{location} requirement text", 20)
        performance_basis = contract["performance_basis"]
        if performance_basis is not None:
            basis = _exact(
                performance_basis,
                {"kind", "detail"},
                f"{location}.performance_basis",
            )
            if basis["kind"] not in {"workload", "asymptotic", "measurement"}:
                raise CalibrationError(
                    f"{location}.performance_basis.kind is unsupported"
                )
            _text(basis["detail"], f"{location}.performance_basis.detail", 20)
        qualitative_bases = contract["qualitative_bases"]
        if not isinstance(qualitative_bases, dict) or not set(qualitative_bases) <= {
            "functional_correctness",
            "security_reliability",
        }:
            raise CalibrationError(f"{location}.qualitative_bases is invalid")
        for criterion, raw_basis in qualitative_bases.items():
            basis = _exact(
                raw_basis,
                {"kind", "detail"},
                f"{location}.qualitative_bases.{criterion}",
            )
            if basis["kind"] not in {
                "test-fault-sensitivity",
                "behavioral-quality",
                "defense-in-depth",
                "failure-determinism",
                "concurrency-margin",
            }:
                raise CalibrationError(
                    f"{location}.qualitative_bases.{criterion}.kind is unsupported"
                )
            _text(
                basis["detail"],
                f"{location}.qualitative_bases.{criterion}.detail",
                20,
            )
        base_files = pair["base_files"]
        if not isinstance(base_files, dict) or not base_files:
            raise CalibrationError(f"{location}.base_files must be non-empty")
        for raw_path, content in base_files.items():
            _safe_path(raw_path, f"{location}.base_files")
            if not isinstance(content, str):
                raise CalibrationError(f"{location}.base_files values must be strings")
        patch_results: dict[str, dict[str, Any]] = {}
        for side in ("A", "B"):
            patch = _validate_patch(pair, side)
            patch_results[side] = patch
            patch_totals["patches"] += 1
            patch_totals["hunks"] += patch["hunks"]
            patch_totals["additions"] += patch["additions"]
            patch_totals["deletions"] += patch["deletions"]
        provenance = _exact(
            pair["provenance"],
            {"kind", "reference", "machine_check"},
            f"{location}.provenance",
        )
        _text(provenance["reference"], f"{location}.provenance.reference", 20)
        if provenance["kind"] == "machine":
            if (
                provenance["machine_check"] != "identical-patches"
                or pair["diff_a"] != pair["diff_b"]
            ):
                raise CalibrationError(f"{location} has unsupported machine provenance")
        elif provenance["kind"] != "expert" or provenance["machine_check"] is not None:
            raise CalibrationError(f"{location} provenance is invalid")
        probes = _exact(
            pair["probes"],
            {"injection", "length_bias", "preservation_tokens"},
            f"{location}.probes",
        )
        injection = probes["injection"]
        if injection is not None:
            data = _exact(
                injection, {"side", "location", "token"}, f"{location}.probes.injection"
            )
            if data["side"] not in {"A", "B"} or data["location"] not in {
                "comment",
                "string",
                "path",
            }:
                raise CalibrationError(f"{location} injection metadata is invalid")
            token = _text(data["token"], f"{location} injection token", 4)
            if token not in pair[f"diff_{data['side'].lower()}"]:
                raise CalibrationError(
                    f"{location} injection token is not byte-preserved"
                )
            injection_sequence.append((data["side"], data["location"]))
        length_bias = probes["length_bias"]
        if length_bias is not None:
            data = _exact(
                length_bias,
                {"longer_side", "kind"},
                f"{location}.probes.length_bias",
            )
            if data["longer_side"] not in {"A", "B"} or data["kind"] not in {
                "necessary",
                "harmful",
            }:
                raise CalibrationError(f"{location} length-bias metadata is invalid")
            longer = pair[f"diff_{data['longer_side'].lower()}"]
            other = pair["diff_b" if data["longer_side"] == "A" else "diff_a"]
            if len(longer.encode("utf-8")) <= len(other.encode("utf-8")):
                raise CalibrationError(f"{location} declared longer side is not longer")
            length_bias_kinds[data["kind"]] += 1
            length_bias_sides[data["longer_side"]] += 1
        tokens = probes["preservation_tokens"]
        if (
            not isinstance(tokens, list)
            or len(tokens) != len(set(tokens))
            or not all(isinstance(token, str) and token for token in tokens)
        ):
            raise CalibrationError(f"{location} preservation tokens are invalid")
        if any(
            token not in patch_results[side]["post_text"]
            for token in tokens
            for side in ("A", "B")
        ):
            raise CalibrationError(
                f"{location} did not preserve every declared token in both candidates"
            )
        adjudication = _exact(
            pair["adjudication"],
            {
                "reviewer_a",
                "reviewer_b",
                "re_review",
                "resolution",
                "scoring_gold",
            },
            f"{location}.adjudication",
        )
        reviewer_a = _label_set(
            pair,
            adjudication["reviewer_a"],
            f"{location}.reviewer_a",
            legacy=True,
        )
        reviewer_ids.add(reviewer_a["reviewer_id"])
        author_outcomes[reviewer_a["outcome"]] += 1
        if pair["sentinel"]:
            author_sentinel_outcomes[reviewer_a["outcome"]] += 1
        expected_category = {
            "tie": "tie",
            "tradeoff": "tradeoff",
            "unqualified": "unqualified",
        }.get(reviewer_a["outcome"])
        if expected_category and expected_category not in raw_categories:
            raise CalibrationError(
                f"{location} outcome {reviewer_a['outcome']} lacks its category"
            )
        reviewer_b_raw = adjudication["reviewer_b"]
        re_review_raw = adjudication["re_review"]
        resolution_raw = adjudication["resolution"]
        scoring_gold_raw = adjudication["scoring_gold"]
        if reviewer_b_raw is None or re_review_raw is None or scoring_gold_raw is None:
            if resolution_raw is not None:
                raise CalibrationError(
                    f"{location} resolves without complete review history"
                )
            unresolved.append(pair_id)
            continue
        reviewer_b = _label_set(
            pair, reviewer_b_raw, f"{location}.reviewer_b", legacy=True
        )
        if reviewer_b["reviewer_id"] == reviewer_a["reviewer_id"]:
            raise CalibrationError(
                f"{location} reviewers must be independently identified"
            )
        reviewer_ids.add(reviewer_b["reviewer_id"])
        re_review = _label_set(
            pair, re_review_raw, f"{location}.re_review", legacy=True
        )
        if re_review["reviewer_id"] == reviewer_a["reviewer_id"]:
            raise CalibrationError(
                f"{location} re-review is not independently identified"
            )
        reviewer_ids.add(re_review["reviewer_id"])
        if resolution_raw is None:
            unresolved.append(pair_id)
            if not _same_labels(reviewer_a, reviewer_b):
                disagreements.append(pair_id)
            continue
        resolution = _label_set(
            pair,
            resolution_raw,
            f"{location}.resolution",
            resolution=True,
            legacy=True,
        )
        scoring_gold = _label_set(
            pair,
            scoring_gold_raw,
            f"{location}.scoring_gold",
            resolution=True,
        )
        if not _same_labels(reviewer_a, reviewer_b):
            disagreements.append(pair_id)
        if (
            resolution["method"] != "root-resolution"
            or resolution["reviewer_id"] != review_policy["resolution_authority"]
        ):
            raise CalibrationError(f"{location} needs the declared root resolution")
        if (
            scoring_gold["method"] != "root-resolution"
            or scoring_gold["reviewer_id"] != review_policy["resolution_authority"]
            or not _same_semantic_labels(resolution, scoring_gold)
        ):
            raise CalibrationError(
                f"{location} scoring gold changes root-resolved semantics"
            )
        if any(
            resolution["eligibility"][side]["requirement_statuses"]
            != scoring_gold["eligibility"][side]["requirement_statuses"]
            for side in ("A", "B")
        ):
            status_expansion_pairs.append(pair_id)
        if not _same_labels(re_review, resolution):
            re_review_disagreements.append(pair_id)
        resolved_outcomes[scoring_gold["outcome"]] += 1
        if pair["sentinel"]:
            resolved_sentinel_outcomes[scoring_gold["outcome"]] += 1
    for previous, current in zip(
        injection_sequence, injection_sequence[1:], strict=False
    ):
        if previous[0] == current[0] or previous[1] == current[1]:
            raise CalibrationError(
                "injection probes must alternate sides and locations"
            )
    if (
        len(injection_sequence) < 6
        or categories["length-bias"] < 5
        or length_bias_kinds != {"necessary": 3, "harmful": 2}
        or not {"A", "B"} <= set(length_bias_sides)
    ):
        raise CalibrationError("corpus lacks balanced injection or length-bias probes")
    if categories["identifier-preservation"] < 4 or categories["multi-file"] < 3:
        raise CalibrationError("corpus lacks identifier or multi-file coverage")
    if any(author_outcomes[outcome] != 6 for outcome in OUTCOMES):
        raise CalibrationError("author outcomes must be balanced six per class")
    if any(author_sentinel_outcomes[outcome] != 2 for outcome in OUTCOMES):
        raise CalibrationError("historical sentinels must be balanced two per class")
    if languages["go"] < 5 or languages["python"] < 5:
        raise CalibrationError("corpus lacks required Go or Python breadth")
    if languages["javascript"] + languages["typescript"] < 10:
        raise CalibrationError("corpus lacks required JavaScript/TypeScript breadth")
    adjudication_complete = not unresolved
    if adjudication_complete and any(
        resolved_outcomes[outcome] != 6 for outcome in OUTCOMES
    ):
        raise CalibrationError("resolved outcomes must remain balanced six per class")
    if adjudication_complete and any(
        resolved_sentinel_outcomes[outcome] != 2 for outcome in OUTCOMES
    ):
        raise CalibrationError("resolved sentinels must remain balanced two per class")
    return {
        "corpus_sha256": canonical_sha256(manifest),
        "pair_count": len(pairs),
        "raw_trial_count": sum(pair["repetitions"] * 2 for pair in pairs),
        "author_outcomes": {outcome: author_outcomes[outcome] for outcome in OUTCOMES},
        "resolved_outcomes": {
            outcome: resolved_outcomes[outcome] for outcome in OUTCOMES
        },
        "author_sentinel_outcomes": {
            outcome: author_sentinel_outcomes[outcome] for outcome in OUTCOMES
        },
        "sentinel_outcomes": {
            outcome: (
                resolved_sentinel_outcomes[outcome]
                if adjudication_complete
                else author_sentinel_outcomes[outcome]
            )
            for outcome in OUTCOMES
        },
        "categories": dict(sorted(categories.items())),
        "languages": dict(sorted(languages.items())),
        "patch_totals": dict(patch_totals),
        "adjudication_complete": adjudication_complete,
        "unresolved_pairs": unresolved,
        "disagreements": disagreements,
        "re_review_disagreements": re_review_disagreements,
        "status_expansion_pairs": status_expansion_pairs,
        "reviewer_ids": sorted(reviewer_ids),
        "criterion_support": criterion_support(manifest),
    }


def review_artifact_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """Hash each preserved adjudication stream in stable pair order."""

    return {
        field: canonical_sha256(
            [
                {"pair_id": pair["id"], "record": pair["adjudication"][field]}
                for pair in manifest["pairs"]
            ]
        )
        for field in (
            "reviewer_a",
            "reviewer_b",
            "re_review",
            "resolution",
            "scoring_gold",
        )
    }


def criterion_support(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Describe canonical semantic support separately from AB/BA presentation."""

    counts = {criterion: Counter() for criterion in CRITERIA}
    sample_size = 0
    for pair in manifest["pairs"]:
        resolution = pair["adjudication"].get("resolution")
        if not isinstance(resolution, dict) or resolution.get("criteria") is None:
            continue
        sample_size += 1
        for criterion, winner in zip(CRITERIA, resolution["criteria"], strict=True):
            counts[criterion][winner] += 1
    support: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        canonical_counts = {
            winner: counts[criterion][winner] for winner in ("A", "B", "tie")
        }
        semantic_winners = {winner for winner in ("A", "B") if canonical_counts[winner]}
        status = (
            "not-calibrated"
            if not semantic_winners
            else "bidirectional"
            if semantic_winners == {"A", "B"}
            else "one-sided"
        )
        decisive_samples = canonical_counts["A"] + canonical_counts["B"]
        production_decisive = status == "bidirectional" or (
            status == "one-sided" and decisive_samples >= 5
        )
        support[criterion] = {
            "sample_size": sample_size,
            "status": status,
            "calibration_claim": {
                "not-calibrated": "tie-discipline-only",
                "one-sided": "one-sided-detection",
                "bidirectional": "bidirectional-discrimination",
            }[status],
            "decisive_sample_size": decisive_samples,
            "production_decisive": production_decisive,
            "production_policy": (
                "bidirectional-gold-support"
                if status == "bidirectional"
                else "ab-ba-balanced-one-sided-support-at-least-five"
                if production_decisive
                else "tie-only-until-calibration-support-expands"
            ),
            "canonical_counts": canonical_counts,
            "presented_counts": {
                "A": canonical_counts["A"] + canonical_counts["B"],
                "B": canonical_counts["A"] + canonical_counts["B"],
                "tie": canonical_counts["tie"] * 2,
            },
        }
    return support


def validate_release(bundle: Bundle) -> dict[str, Any]:
    """Verify the trusted release lock against current canonical artifacts."""

    validate_rubric(bundle.rubric)
    validate_locked_documents(bundle)
    release = _exact(
        bundle.release,
        {
            "schema_version",
            "release_id",
            "test_release",
            "artifacts",
            "evaluator",
            "judge",
            "sampling",
            "execution_limits",
            "acceptance",
            "criterion_support",
            "invocation_namespace_sha256",
            "runtime_adapter",
            "gold_source",
            "trust_boundary_note",
        },
        "release",
    )
    if release["schema_version"] != 2 or not isinstance(release["test_release"], bool):
        raise CalibrationError("release schema lock is invalid")
    _text(release["release_id"], "release.release_id")
    _text(release["trust_boundary_note"], "release.trust_boundary_note", 40)
    artifacts = _exact(
        release["artifacts"],
        {
            "corpus_sha256",
            "manifest_schema_sha256",
            "rubric_sha256",
            "request_template_sha256",
            "system_prompt_sha256",
            "response_schema_sha256",
            "evidence_schema_sha256",
            "holdout_plan_schema_sha256",
            "holdout_plan_schema_bytes_sha256",
            "reviewer_a_sha256",
            "reviewer_b_sha256",
            "re_review_sha256",
            "resolution_sha256",
            "scoring_gold_sha256",
        },
        "release.artifacts",
    )
    reviews = review_artifact_hashes(bundle.manifest)
    project_root = bundle.root.parents[1]
    holdout_plan_schema_path = project_root / "holdout-plan.schema.json"
    holdout_plan_schema = load_json(holdout_plan_schema_path)
    actual_hashes = {
        "corpus_sha256": canonical_sha256(bundle.manifest),
        "manifest_schema_sha256": canonical_sha256(bundle.manifest_schema),
        "rubric_sha256": canonical_sha256(bundle.rubric),
        "request_template_sha256": canonical_sha256(bundle.request_template),
        "system_prompt_sha256": hashlib.sha256(
            bundle.request_template["system_prompt"].encode("utf-8")
        ).hexdigest(),
        "response_schema_sha256": canonical_sha256(bundle.response_schema),
        "evidence_schema_sha256": canonical_sha256(bundle.evidence_schema),
        "holdout_plan_schema_sha256": canonical_sha256(holdout_plan_schema),
        "holdout_plan_schema_bytes_sha256": file_sha256(holdout_plan_schema_path),
        "reviewer_a_sha256": reviews["reviewer_a"],
        "reviewer_b_sha256": reviews["reviewer_b"],
        "re_review_sha256": reviews["re_review"],
        "resolution_sha256": reviews["resolution"],
        "scoring_gold_sha256": reviews["scoring_gold"],
    }
    if artifacts != actual_hashes:
        raise CalibrationError("release artifact lock is stale or mismatched")
    evaluator = _exact(
        release["evaluator"],
        {
            "version",
            "source_sha256",
            "collector_source_sha256",
            "certifier_source_sha256",
        },
        "release.evaluator",
    )
    if evaluator["version"] != EVALUATOR_VERSION:
        raise CalibrationError("release evaluator version is stale")
    if evaluator["source_sha256"] != file_sha256(bundle.root / "calibration.py"):
        raise CalibrationError("release evaluator source hash is stale")
    if evaluator["collector_source_sha256"] != file_sha256(bundle.root / "collect.py"):
        raise CalibrationError("release collector source hash is stale")
    if evaluator["certifier_source_sha256"] != file_sha256(bundle.root / "certify.py"):
        raise CalibrationError("release certifier source hash is stale")
    judge = _exact(
        release["judge"],
        {
            "provider",
            "provider_version",
            "requested_model",
            "required_primary_model_prefix",
            "allowed_auxiliary_model_prefixes",
        },
        "release.judge",
    )
    for field in (
        "provider",
        "provider_version",
        "requested_model",
        "required_primary_model_prefix",
    ):
        _text(judge[field], f"release.judge.{field}")
    prefixes = judge["allowed_auxiliary_model_prefixes"]
    if not isinstance(prefixes, list) or not all(
        isinstance(prefix, str) and prefix for prefix in prefixes
    ):
        raise CalibrationError("release auxiliary model prefixes are invalid")
    if not release["test_release"]:
        if judge != {
            "provider": "claude-cli",
            "provider_version": "2.1.198 (Claude Code)",
            "requested_model": "claude-sonnet-5",
            "required_primary_model_prefix": "claude-sonnet-5",
            "allowed_auxiliary_model_prefixes": ["claude-haiku"],
        }:
            raise CalibrationError("production release judge is not fully pinned")
        if release["gold_source"] != "scoring_gold":
            raise CalibrationError("production release must use expanded scoring gold")
    elif release["gold_source"] != "scoring_gold":
        raise CalibrationError("test release must exercise expanded scoring gold")
    sampling = _exact(
        release["sampling"],
        {"sentinel_repetitions", "ordinary_repetitions", "cli_args"},
        "release.sampling",
    )
    if sampling["sentinel_repetitions"] != 3 or sampling["ordinary_repetitions"] != 1:
        raise CalibrationError("release sampling repetitions are stale")
    if not isinstance(sampling["cli_args"], list) or not all(
        isinstance(value, str) for value in sampling["cli_args"]
    ):
        raise CalibrationError("release sampling cli_args are invalid")
    if not release["test_release"] and sampling["cli_args"] != list(
        PRODUCTION_CLI_ARGS
    ):
        raise CalibrationError("production Claude CLI arguments are not fully pinned")
    execution_limits = _exact(
        release["execution_limits"],
        {
            "timeout_seconds",
            "per_invocation_max_usd",
            "run_max_usd",
            "expected_call_count",
        },
        "release.execution_limits",
    )
    expected_limits = {
        "timeout_seconds": PRODUCTION_TIMEOUT_SECONDS,
        "per_invocation_max_usd": PRODUCTION_PER_INVOCATION_BUDGET_USD,
        "run_max_usd": PRODUCTION_RUN_BUDGET_USD,
        "expected_call_count": sum(
            pair["repetitions"] * 2 for pair in bundle.manifest["pairs"]
        ),
    }
    if execution_limits != expected_limits:
        raise CalibrationError("release timeout or spend limits are stale")
    namespace = release["invocation_namespace_sha256"]
    if not isinstance(namespace, str) or HASH_RE.fullmatch(namespace) is None:
        raise CalibrationError("release invocation namespace is invalid")
    support = criterion_support(bundle.manifest)
    if release["criterion_support"] != support:
        raise CalibrationError("release criterion support declaration is stale")
    rubric_policy = bundle.rubric["production_decisive_policy"]
    for criterion, record in support.items():
        policy_allows = not rubric_policy[criterion].startswith("tie-only")
        if policy_allows != record["production_decisive"]:
            raise CalibrationError(
                f"rubric and release criterion policy differ for {criterion}"
            )
    acceptance = _exact(
        release["acceptance"],
        {
            "minimum_outcome_balanced_accuracy",
            "minimum_outcome_cohen_kappa",
            "minimum_eligibility_accuracy",
            "minimum_requirement_status_accuracy",
            "minimum_violation_set_accuracy",
            "minimum_per_criterion_balanced_accuracy",
            "require_zero_order_disagreements",
            "require_zero_sentinel_instability",
            "require_zero_critical_hard_outcome_failures",
            "require_zero_critical_hard_admissibility_errors",
            "require_zero_length_bias_failures",
            "require_zero_unsupported_performance",
            "require_zero_unsupported_qualitative",
            "require_zero_spend_limit_failures",
            "require_stable_model_set",
            "require_stable_executable",
        },
        "release.acceptance",
    )
    for field in (
        "minimum_outcome_balanced_accuracy",
        "minimum_outcome_cohen_kappa",
        "minimum_eligibility_accuracy",
        "minimum_requirement_status_accuracy",
        "minimum_violation_set_accuracy",
        "minimum_per_criterion_balanced_accuracy",
    ):
        _rate(acceptance[field], f"release.acceptance.{field}")
    for field in (
        "require_zero_order_disagreements",
        "require_zero_sentinel_instability",
        "require_zero_critical_hard_outcome_failures",
        "require_zero_critical_hard_admissibility_errors",
        "require_zero_length_bias_failures",
        "require_zero_unsupported_performance",
        "require_zero_unsupported_qualitative",
        "require_zero_spend_limit_failures",
        "require_stable_model_set",
        "require_stable_executable",
    ):
        if not isinstance(acceptance[field], bool):
            raise CalibrationError(f"release.acceptance.{field} must be boolean")
    runtime_adapter = _exact(
        release["runtime_adapter"],
        {
            "id",
            "source_sha256",
            "harness_runner_source_sha256",
            "provider_source_sha256",
            "harness_manifest_source_sha256",
            "harness_package_source_sha256",
            "run_evals_source_sha256",
            "holdout_plan_source_sha256",
            "prepare_holdout_plan_source_sha256",
            "baseline_authority_source_sha256",
            "frozen_original_commit",
            "shared_harness_compatible",
            "blocker",
        },
        "release.runtime_adapter",
    )
    _text(runtime_adapter["id"], "release.runtime_adapter.id")
    frozen_original = runtime_adapter["frozen_original_commit"]
    if (
        not isinstance(frozen_original, str)
        or COMMIT_RE.fullmatch(frozen_original) is None
    ):
        raise CalibrationError(
            "release frozen original commit must be 40 lowercase hexadecimal characters"
        )
    project_root = bundle.root.parents[1]
    authority_path = project_root / "baseline-authority.json"
    authority_commit = require_baseline_authority(
        project_root / "suite.json",
        authority_path,
    )
    if frozen_original != authority_commit:
        raise CalibrationError(
            "release frozen original commit differs from baseline authority"
        )
    runtime_sources = {
        "source_sha256": project_root / "harness_evals" / "comparator_runtime.py",
        "harness_runner_source_sha256": project_root / "harness_evals" / "runner.py",
        "provider_source_sha256": project_root / "harness_evals" / "providers.py",
        "harness_manifest_source_sha256": project_root
        / "harness_evals"
        / "manifest.py",
        "harness_package_source_sha256": project_root / "harness_evals" / "__init__.py",
        "run_evals_source_sha256": project_root / "harness_evals" / "cli.py",
        "holdout_plan_source_sha256": project_root
        / "harness_evals"
        / "holdout_plan.py",
        "prepare_holdout_plan_source_sha256": project_root
        / "harness_evals"
        / "holdout_cli.py",
        "baseline_authority_source_sha256": authority_path,
    }
    for field, source_path in runtime_sources.items():
        source_sha256 = runtime_adapter[field]
        if (
            not isinstance(source_sha256, str)
            or HASH_RE.fullmatch(source_sha256) is None
        ):
            raise CalibrationError(f"release {field} source hash is invalid")
        if source_sha256 != file_sha256(source_path):
            raise CalibrationError(f"release {field} source hash is stale")
    if not isinstance(runtime_adapter["shared_harness_compatible"], bool):
        raise CalibrationError("release runtime compatibility must be boolean")
    if runtime_adapter["id"] != SHARED_RUNTIME_ADAPTER_ID:
        raise CalibrationError("release runtime adapter identity is stale")
    if not runtime_adapter["shared_harness_compatible"]:
        _text(runtime_adapter["blocker"], "release.runtime_adapter.blocker", 40)
    elif runtime_adapter["blocker"] is not None:
        raise CalibrationError("compatible runtime adapter cannot retain a blocker")
    return {
        "release_sha256": canonical_sha256(release),
        "release_id": release["release_id"],
        "test_release": release["test_release"],
        "runtime_adapter": runtime_adapter,
        "criterion_support": support,
        "execution_limits": execution_limits,
        "artifacts": actual_hashes,
    }


def invocation_id(
    release: dict[str, Any], pair_id: str, repetition: int, order: str
) -> str:
    namespace = release.get("invocation_namespace_sha256")
    if not isinstance(namespace, str) or HASH_RE.fullmatch(namespace) is None:
        raise CalibrationError("release invocation namespace is not a locked hash")
    material = canonical_bytes(
        {
            "pair_id": pair_id,
            "repetition": repetition,
            "order": order,
            "corpus_sha256": release["artifacts"]["corpus_sha256"],
        }
    )
    return hmac.new(bytes.fromhex(namespace), material, hashlib.sha256).hexdigest()


def build_request_bytes(
    bundle: Bundle, pair: dict[str, Any], repetition: int, order: str
) -> bytes:
    """Serialize the exact provider request envelope locked by offline evidence."""

    if order not in {"AB", "BA"}:
        raise CalibrationError(f"invalid request order: {order}")
    diff_a, diff_b = (
        (pair["diff_a"], pair["diff_b"])
        if order == "AB"
        else (pair["diff_b"], pair["diff_a"])
    )
    payload = {
        "invocation_id": invocation_id(bundle.release, pair["id"], repetition, order),
        "task": pair["task"],
        "contract": pair["contract"],
        "base_files": pair["base_files"],
        "candidate_A_diff": diff_a,
        "candidate_B_diff": diff_b,
        "rubric": bundle.rubric,
        "response_schema_sha256": canonical_sha256(bundle.response_schema),
        "execution_limits": bundle.release["execution_limits"],
    }
    if list(payload) != bundle.request_template["user_payload_fields"]:
        raise CalibrationError("request template fields differ from serialized payload")
    envelope = {
        "system_prompt": bundle.request_template["system_prompt"],
        "user_payload": payload,
        "requested_model": bundle.release["judge"]["requested_model"],
        "runtime_adapter": bundle.release["runtime_adapter"]["id"],
        "sampling": bundle.release["sampling"],
        "execution_limits": bundle.release["execution_limits"],
    }
    return canonical_bytes(envelope)


def expected_transport_hashes(
    bundle: Bundle,
    request_bytes: bytes,
    command_executable: str,
) -> dict[str, str]:
    """Reconstruct the exact shared-runtime stdin and command digests."""

    try:
        envelope = json.loads(request_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalibrationError("evidence request bytes are invalid JSON") from exc
    if canonical_bytes(envelope) != request_bytes:
        raise CalibrationError("evidence request bytes are not canonical JSON")
    if envelope.get("runtime_adapter") != bundle.release["runtime_adapter"]["id"]:
        raise CalibrationError("evidence request runtime adapter is stale")
    stdin_bytes = canonical_bytes(envelope["user_payload"])
    command = (
        command_executable,
        *bundle.release["sampling"]["cli_args"],
        "--system-prompt",
        envelope["system_prompt"],
        "--json-schema",
        canonical_bytes(bundle.response_schema).decode("ascii"),
    )
    return {
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "stdin_sha256": hashlib.sha256(stdin_bytes).hexdigest(),
        "command_sha256": canonical_sha256(list(command)),
    }


def validate_executor_evidence(
    bundle: Bundle,
    value: Any,
    *,
    executable_sha256: str,
    stdin_sha256: str,
    location: str,
) -> dict[str, Any]:
    """Validate one exact systemd executor record preserved by the collector."""

    executor = _exact(value, set(EXECUTOR_EVIDENCE_KEYS), location)
    if executor["kind"] != "shared-systemd-claude-executor":
        raise CalibrationError(f"{location}.kind is not the shared executor")
    if executor["enforced"] is not True:
        raise CalibrationError(f"{location}.enforced must be true")
    if executor["provider_version"] != bundle.release["judge"]["provider_version"]:
        raise CalibrationError(f"{location}.provider_version differs from release")
    executable_path = Path(
        _text(executor["executable_path"], f"{location}.executable_path")
    )
    if not executable_path.is_absolute():
        raise CalibrationError(f"{location}.executable_path must be absolute")
    identity = _exact(
        executor["executable_identity"],
        {"device", "inode", "size", "mode", "mtime_ns", "ctime_ns"},
        f"{location}.executable_identity",
    )
    for field, raw_value in identity.items():
        minimum = 1 if field in {"inode", "size"} else 0
        _integer(raw_value, f"{location}.executable_identity.{field}", minimum)
    if not stat.S_ISREG(identity["mode"]) or identity["mode"] & 0o111 == 0:
        raise CalibrationError(f"{location}.executable_identity is not executable")
    if (
        executor["executable_sha256"] != executable_sha256
        or not isinstance(executable_sha256, str)
        or HASH_RE.fullmatch(executable_sha256) is None
    ):
        raise CalibrationError(f"{location}.executable_sha256 differs from trial")
    if executor["execution_source"] != "descriptor-verified-private-copy":
        raise CalibrationError(
            f"{location}.execution_source has unexpected private-copy provenance"
        )
    descriptor_path = _text(
        executor["execution_descriptor_path"],
        f"{location}.execution_descriptor_path",
    )
    if re.fullmatch(r"/proc/[1-9][0-9]*/fd/[0-9]+", descriptor_path) is None:
        raise CalibrationError(f"{location}.execution_descriptor_path is invalid")
    execution_copy_path = Path(
        _text(executor["execution_copy_path"], f"{location}.execution_copy_path")
    )
    if (
        not execution_copy_path.is_absolute()
        or re.fullmatch(
            r"/run/user/[0-9]+/skill-executable-[A-Za-z0-9_-]+/claude",
            str(execution_copy_path),
        )
        is None
    ):
        raise CalibrationError(f"{location}.execution_copy_path is invalid")
    command_executable = _text(
        executor["command_executable"], f"{location}.command_executable"
    )
    if (
        re.fullmatch(
            r"/run/user/[0-9]+/skill-eval-comparator-runtime/bin/claude",
            command_executable,
        )
        is None
    ):
        raise CalibrationError(f"{location}.command_executable is invalid")
    _text(executor["systemd_version"], f"{location}.systemd_version")
    properties = executor["properties"]
    if (
        not isinstance(properties, list)
        or not properties
        or len(properties) != len(set(properties))
        or not all(isinstance(item, str) and item for item in properties)
    ):
        raise CalibrationError(f"{location}.properties is invalid")
    required_properties = {
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "PrivateUsers=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "MemoryMax=4G",
        "TasksMax=512",
        "LimitNOFILE=4096",
        "LimitFSIZE=512M",
        "KillMode=control-group",
        "UMask=0077",
        f"RuntimeMaxSec={bundle.release['execution_limits']['timeout_seconds']}s",
        f"BindReadOnlyPaths={execution_copy_path}:{command_executable}",
        f"ReadWritePaths={Path(command_executable).parents[1]}",
    }
    if not required_properties.issubset(properties):
        raise CalibrationError(f"{location}.properties omit required isolation")
    dynamic_prefixes = (
        "ReadWritePaths=",
        "BindPaths=",
        "BindReadOnlyPaths=",
        "InaccessiblePaths=",
    )
    if any(
        item not in required_properties and not item.startswith(dynamic_prefixes)
        for item in properties
    ):
        raise CalibrationError(f"{location}.properties contain an unknown control")
    for prefix in ("ReadWritePaths=", "BindPaths=", "BindReadOnlyPaths="):
        if sum(item.startswith(prefix) for item in properties) != 1:
            raise CalibrationError(f"{location}.properties have invalid {prefix} count")
    runtime_mount = str(Path(command_executable).parents[1])
    bind_path = next(item for item in properties if item.startswith("BindPaths="))
    if not bind_path.endswith(f":{runtime_mount}"):
        raise CalibrationError(f"{location}.properties bind the wrong runtime root")
    inaccessible = [
        item.removeprefix("InaccessiblePaths=")
        for item in properties
        if item.startswith("InaccessiblePaths=")
    ]
    if not inaccessible or any(not Path(path).is_absolute() for path in inaccessible):
        raise CalibrationError(f"{location}.properties lack inaccessible host roots")
    if executor["environment_mode"] != "env-i-allowlist":
        raise CalibrationError(f"{location}.environment_mode is invalid")
    if executor["process_namespace"] != "unshare-user-pid-private-proc":
        raise CalibrationError(f"{location}.process_namespace is invalid")
    if executor["stdin_sha256"] != stdin_sha256:
        raise CalibrationError(f"{location}.stdin_sha256 differs from trial")
    if executor["remote_service_attestation"] != "not-cryptographically-attested":
        raise CalibrationError(f"{location}.remote_service_attestation is invalid")
    return executor


def _changed_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[6:])
    return paths


def _contract_artifacts(pair: dict[str, Any]) -> dict[str, str]:
    artifacts = {"contract/task": pair["task"]}
    for requirement in pair["contract"]["requirements"]:
        artifacts[f"contract/requirements/{requirement['id']}"] = requirement["text"]
    if pair["contract"]["performance_basis"] is not None:
        artifacts["contract/performance_basis"] = canonical_bytes(
            pair["contract"]["performance_basis"]
        ).decode("ascii")
    for criterion, basis in pair["contract"]["qualitative_bases"].items():
        artifacts[f"contract/qualitative_bases/{criterion}"] = canonical_bytes(
            basis
        ).decode("ascii")
    return artifacts


def _cited_text(content: str, start: int, end: int, location: str) -> str:
    lines = content.splitlines() or [""]
    if end > len(lines):
        raise CalibrationError(
            f"{location} line range {start}-{end} exceeds {len(lines)} supplied lines"
        )
    return "\n".join(lines[start - 1 : end])


def _evidence_item(
    value: Any,
    location: str,
    *,
    minimum_characters: int,
    artifact_files: dict[str, dict[str, str]],
    contract_files: dict[str, str],
) -> dict[str, Any]:
    data = _exact(
        value,
        {
            "artifact",
            "path",
            "line_start",
            "line_end",
            "quote",
            "semantic_anchor",
            "observation",
        },
        location,
    )
    if data["artifact"] not in {"A", "B", "both", "contract"}:
        raise CalibrationError(f"{location}.artifact is invalid")
    path = _text(data["path"], f"{location}.path")
    start = _integer(data["line_start"], f"{location}.line_start", 1)
    end = _integer(data["line_end"], f"{location}.line_end", 1)
    if end < start:
        raise CalibrationError(f"{location} line range is reversed")
    artifact = data["artifact"]
    if artifact == "contract":
        if path not in contract_files:
            raise CalibrationError(f"{location} contract evidence path is invalid")
        cited_versions = [_cited_text(contract_files[path], start, end, location)]
    else:
        sides = ("A", "B") if artifact == "both" else (artifact,)
        cited_versions = []
        for side in sides:
            files = artifact_files[side]
            if path not in files:
                raise CalibrationError(
                    f"{location} evidence path is not in candidate {side} bytes"
                )
            cited_versions.append(_cited_text(files[path], start, end, location))
    quote = _text(data["quote"], f"{location}.quote", 3)
    if not re.search(r"[A-Za-z0-9_]", quote):
        raise CalibrationError(f"{location}.quote needs concrete lexical content")
    if any(quote not in cited for cited in cited_versions):
        raise CalibrationError(f"{location}.quote is absent from the cited line range")
    semantic_anchor = _text(data["semantic_anchor"], f"{location}.semantic_anchor", 8)
    observation = _text(
        data["observation"], f"{location}.observation", minimum_characters
    ).strip()
    words = re.findall(r"[A-Za-z0-9_]+", observation)
    if len(words) < 4 or len(set(observation.lower())) < 8:
        raise CalibrationError(f"{location}.observation is trivial or fabricated")
    if quote not in observation:
        raise CalibrationError(f"{location}.observation must repeat the exact quote")
    if semantic_anchor not in observation:
        raise CalibrationError(
            f"{location}.observation must repeat the semantic anchor"
        )
    return {
        "artifact": data["artifact"],
        "path": path,
        "line_start": start,
        "line_end": end,
        "quote": quote,
        "semantic_anchor": semantic_anchor,
        "observation": observation,
    }


def _swap_winner(winner: str) -> str:
    return {"A": "B", "B": "A", "tie": "tie"}[winner]


def validate_response(
    bundle: Bundle, pair: dict[str, Any], response: Any, order: str
) -> dict[str, Any]:
    """Validate structured evidence and normalize presented A/B labels to corpus sides."""

    if order not in {"AB", "BA"}:
        raise CalibrationError("response order is invalid")
    data = _exact(response, {"checks", "admissibility", "criteria"}, "response")
    minimum = bundle.rubric["evidence"]["minimum_observation_characters"]
    canonical_artifacts = {
        side: _validate_patch(pair, side)["post_files"] for side in ("A", "B")
    }
    artifacts_by_presented = {
        "A": canonical_artifacts["A" if order == "AB" else "B"],
        "B": canonical_artifacts["B" if order == "AB" else "A"],
    }
    contract_artifacts = _contract_artifacts(pair)
    requirement_ids = [item["id"] for item in pair["contract"]["requirements"]]
    checks_data = _exact(data["checks"], {"A", "B"}, "response.checks")
    presented_eligibility: dict[str, str] = {}
    presented_checks: dict[str, dict[str, str]] = {}
    for side in ("A", "B"):
        checks = checks_data[side]
        if not isinstance(checks, list) or len(checks) != len(requirement_ids):
            raise CalibrationError(
                f"response.checks.{side} must cover every requirement"
            )
        statuses: dict[str, str] = {}
        for index, raw_check in enumerate(checks):
            check = _exact(
                raw_check,
                {"requirement_id", "status", "evidence"},
                f"response.checks.{side}[{index}]",
            )
            requirement_id = check["requirement_id"]
            if requirement_id not in requirement_ids or requirement_id in statuses:
                raise CalibrationError(
                    f"response.checks.{side} requirement ids are invalid"
                )
            if check["status"] not in REQUIREMENT_STATUSES:
                raise CalibrationError(f"response.checks.{side} status is invalid")
            evidence = _evidence_item(
                check["evidence"],
                f"response.checks.{side}[{index}].evidence",
                minimum_characters=minimum,
                artifact_files=artifacts_by_presented,
                contract_files=contract_artifacts,
            )
            if evidence["artifact"] not in {side, "contract"}:
                raise CalibrationError(
                    f"response.checks.{side} cites the wrong candidate"
                )
            if (
                requirement_id not in evidence["observation"]
                or check["status"] not in evidence["observation"]
            ):
                raise CalibrationError(
                    f"response.checks.{side} evidence does not name its requirement and status"
                )
            expected_anchor = f"requirement:{requirement_id}:{check['status']}"
            if evidence["semantic_anchor"] != expected_anchor:
                raise CalibrationError(
                    f"response.checks.{side} semantic anchor is invalid"
                )
            statuses[requirement_id] = check["status"]
        if set(statuses) != set(requirement_ids):
            raise CalibrationError(f"response.checks.{side} omitted a requirement")
        presented_checks[side] = statuses
        presented_eligibility[side] = derive_eligibility(statuses)
    admissibility_data = _exact(
        data["admissibility"], {"A", "B"}, "response.admissibility"
    )
    presented_violations: dict[str, tuple[str, ...]] = {}
    for side in ("A", "B"):
        decision = _exact(
            admissibility_data[side],
            {"decision", "violation_ids"},
            f"response.admissibility.{side}",
        )
        violation_ids = decision["violation_ids"]
        if (
            decision["decision"] not in ELIGIBILITY
            or not isinstance(violation_ids, list)
            or len(violation_ids) != len(set(violation_ids))
            or not set(violation_ids) <= set(requirement_ids)
        ):
            raise CalibrationError(f"response.admissibility.{side} is invalid")
        derived_violations = tuple(
            requirement_id
            for requirement_id in requirement_ids
            if presented_checks[side][requirement_id] == "violated"
        )
        if (
            decision["decision"] != presented_eligibility[side]
            or tuple(violation_ids) != derived_violations
        ):
            raise CalibrationError(
                f"response.admissibility.{side} differs from requirement checks"
            )
        presented_violations[side] = derived_violations
    presented_criteria: dict[str, str] | None = None
    if presented_eligibility == {"A": "eligible", "B": "eligible"}:
        criteria_data = _exact(data["criteria"], set(CRITERIA), "response.criteria")
        presented_criteria = {}
        for criterion in CRITERIA:
            decision = _exact(
                criteria_data[criterion],
                {"winner", "evidence"},
                f"response.criteria.{criterion}",
            )
            if decision["winner"] not in WINNERS:
                raise CalibrationError(
                    f"response.criteria.{criterion}.winner is invalid"
                )
            evidence = _evidence_item(
                decision["evidence"],
                f"response.criteria.{criterion}.evidence",
                minimum_characters=minimum,
                artifact_files=artifacts_by_presented,
                contract_files=contract_artifacts,
            )
            if (
                criterion not in evidence["observation"]
                or decision["winner"] not in evidence["observation"]
            ):
                raise CalibrationError(
                    f"response.criteria.{criterion} evidence does not name its criterion and winner"
                )
            expected_anchor = f"criterion:{criterion}:{decision['winner']}"
            if evidence["semantic_anchor"] != expected_anchor:
                raise CalibrationError(
                    f"response.criteria.{criterion} semantic anchor is invalid"
                )
            presented_criteria[criterion] = decision["winner"]
    elif data["criteria"] is not None:
        raise CalibrationError(
            "response.criteria must be null unless both candidates qualify"
        )
    if order == "AB":
        eligibility = presented_eligibility
        violations = presented_violations
        checks = presented_checks
        criteria = presented_criteria
    else:
        eligibility = {
            "A": presented_eligibility["B"],
            "B": presented_eligibility["A"],
        }
        violations = {
            "A": presented_violations["B"],
            "B": presented_violations["A"],
        }
        checks = {"A": presented_checks["B"], "B": presented_checks["A"]}
        criteria = (
            {
                criterion: _swap_winner(winner)
                for criterion, winner in presented_criteria.items()
            }
            if presented_criteria is not None
            else None
        )
    unsupported_performance = bool(
        criteria is not None
        and criteria["performance_efficiency"] != "tie"
        and pair["contract"]["performance_basis"] is None
    )
    unsupported_qualitative = tuple(
        criterion
        for criterion in ("functional_correctness", "security_reliability")
        if criteria is not None
        and criteria[criterion] != "tie"
        and criterion not in pair["contract"]["qualitative_bases"]
    )
    return {
        "eligibility": eligibility,
        "violations": violations,
        "checks": checks,
        "criteria": criteria,
        "unsupported_performance": unsupported_performance,
        "unsupported_qualitative": unsupported_qualitative,
        "outcome": derive_outcome(eligibility, criteria),
    }


def parse_raw_provider_response(raw: str) -> tuple[dict[str, Any], list[str], float]:
    """Bind an exact provider envelope to structured output and actual model IDs."""

    payload = parse_json_object(raw, "raw provider response")
    if "is_error" not in payload or payload["is_error"] is not False:
        raise CalibrationError(
            "raw provider response is_error must be present and false"
        )
    response = payload.get("structured_output")
    model_usage = payload.get("modelUsage")
    if not isinstance(response, dict):
        raise CalibrationError("raw provider response omitted structured_output")
    if not isinstance(model_usage, dict) or not model_usage:
        raise CalibrationError("raw provider response omitted modelUsage provenance")
    actual_models = sorted(model_usage)
    if not all(isinstance(model, str) and model for model in actual_models):
        raise CalibrationError("raw provider response has invalid modelUsage keys")
    cost = payload.get("total_cost_usd")
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or float(cost) < 0
    ):
        raise CalibrationError(
            "raw provider response total_cost_usd must be finite and non-negative"
        )
    return response, actual_models, float(cost)


def _cohen_kappa(expected: list[str], observed: list[str]) -> float:
    if len(expected) != len(observed) or not expected:
        raise CalibrationError("kappa requires equal non-empty observations")
    total = len(expected)
    expected_counts = Counter(expected)
    observed_counts = Counter(observed)
    labels = set(expected_counts) | set(observed_counts)
    agreement = (
        sum(left == right for left, right in zip(expected, observed, strict=True))
        / total
    )
    chance = sum(
        expected_counts[label] * observed_counts[label] for label in labels
    ) / (total * total)
    if chance == 1:
        return 1.0 if agreement == 1 else 0.0
    return (agreement - chance) / (1 - chance)


def _balanced_accuracy(expected: list[str], observed: list[str]) -> float:
    classes = sorted(set(expected))
    recalls = []
    for label in classes:
        indices = [index for index, value in enumerate(expected) if value == label]
        recalls.append(
            sum(observed[index] == label for index in indices) / len(indices)
        )
    return sum(recalls) / len(recalls)


def expected_trial_keys(manifest: dict[str, Any]) -> set[tuple[str, int, str]]:
    return {
        (pair["id"], repetition, order)
        for pair in manifest["pairs"]
        for repetition in range(pair["repetitions"])
        for order in ("AB", "BA")
    }


def evaluate_evidence(bundle: Bundle, evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate complete offline calls; live invocation is deliberately out of scope."""

    release_summary = validate_release(bundle)
    manifest_summary = validate_manifest(bundle.manifest, bundle.rubric)
    _exact(
        evidence,
        {
            "schema_version",
            "release_sha256",
            "corpus_sha256",
            "rubric_sha256",
            "request_template_sha256",
            "response_schema_sha256",
            "judge",
            "spend_ledger",
            "trials",
        },
        "evidence",
    )
    if evidence["schema_version"] != 2 or isinstance(evidence["schema_version"], bool):
        raise CalibrationError("evidence schema version is invalid")
    expected_hashes = {
        "release_sha256": release_summary["release_sha256"],
        "corpus_sha256": release_summary["artifacts"]["corpus_sha256"],
        "rubric_sha256": release_summary["artifacts"]["rubric_sha256"],
        "request_template_sha256": release_summary["artifacts"][
            "request_template_sha256"
        ],
        "response_schema_sha256": release_summary["artifacts"][
            "response_schema_sha256"
        ],
    }
    for field, expected in expected_hashes.items():
        if evidence[field] != expected:
            raise CalibrationError(f"evidence {field} is stale or mismatched")
    judge = _exact(
        evidence["judge"],
        {"provider", "provider_version", "requested_model"},
        "evidence.judge",
    )
    release_judge = bundle.release["judge"]
    for field in ("provider", "provider_version", "requested_model"):
        if judge[field] != release_judge[field]:
            raise CalibrationError(f"evidence judge {field} differs from release lock")
    trials = evidence["trials"]
    if not isinstance(trials, list):
        raise CalibrationError("evidence.trials must be an array")
    expected_keys = expected_trial_keys(bundle.manifest)
    pair_by_id = {pair["id"]: pair for pair in bundle.manifest["pairs"]}
    observed_keys: set[tuple[str, int, str]] = set()
    normalized: dict[tuple[str, int, str], dict[str, Any]] = {}
    model_sets: set[tuple[str, ...]] = set()
    executable_sha256s: set[str] = set()
    systemd_versions: set[str] = set()
    spend_attempt_ids: set[str] = set()
    model_call_failures: list[tuple[str, int, str]] = []
    trial_costs: list[tuple[tuple[str, int, str], float]] = []
    for index, raw_trial in enumerate(trials):
        location = f"evidence.trials[{index}]"
        trial = _exact(raw_trial, set(EVIDENCE_TRIAL_KEYS), location)
        pair_id = _text(trial["pair_id"], f"{location}.pair_id")
        repetition = _integer(trial["repetition"], f"{location}.repetition")
        order = trial["order"]
        if order not in {"AB", "BA"}:
            raise CalibrationError(f"{location}.order is invalid")
        key = (pair_id, repetition, order)
        if key not in expected_keys or key in observed_keys:
            raise CalibrationError(f"{location} has unexpected or duplicate trial key")
        observed_keys.add(key)
        expected_invocation = invocation_id(bundle.release, pair_id, repetition, order)
        if trial["invocation_id"] != expected_invocation:
            raise CalibrationError(f"{location}.invocation_id is stale or forged")
        expected_request = build_request_bytes(
            bundle, pair_by_id[pair_id], repetition, order
        )
        request_text = _text(trial["request"], f"{location}.request", 2)
        try:
            preserved_request = request_text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise CalibrationError(
                f"{location}.request is not canonical ASCII"
            ) from exc
        if preserved_request != expected_request:
            raise CalibrationError(f"{location}.request bytes are stale or forged")
        executable_sha256 = trial["executable_sha256"]
        spend_attempt_id = trial["spend_attempt_id"]
        if (
            not isinstance(executable_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", executable_sha256) is None
            or not isinstance(spend_attempt_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", spend_attempt_id) is None
        ):
            raise CalibrationError(
                f"{location} executable or spend provenance is invalid"
            )
        trial_stdin_sha256 = trial["stdin_sha256"]
        if (
            not isinstance(trial_stdin_sha256, str)
            or HASH_RE.fullmatch(trial_stdin_sha256) is None
        ):
            raise CalibrationError(f"{location}.stdin_sha256 is invalid")
        executor = validate_executor_evidence(
            bundle,
            trial["executor"],
            executable_sha256=executable_sha256,
            stdin_sha256=trial_stdin_sha256,
            location=f"{location}.executor",
        )
        systemd_versions.add(executor["systemd_version"])
        expected_transport = expected_transport_hashes(
            bundle, expected_request, executor["command_executable"]
        )
        for field, expected_hash in expected_transport.items():
            if trial[field] != expected_hash:
                raise CalibrationError(f"{location}.{field} is stale or forged")
        raw_response = _text(trial["raw_response"], f"{location}.raw_response", 2)
        raw_hash = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
        if trial["raw_response_sha256"] != raw_hash:
            raise CalibrationError(f"{location}.raw_response_sha256 is stale or forged")
        if trial["parsed_response_sha256"] != canonical_sha256(trial["response"]):
            raise CalibrationError(
                f"{location}.parsed_response_sha256 is stale or forged"
            )
        raw_parsed_response, raw_models, raw_cost = parse_raw_provider_response(
            raw_response
        )
        if canonical_sha256(raw_parsed_response) != canonical_sha256(trial["response"]):
            raise CalibrationError(
                f"{location} parsed response differs from raw structured_output"
            )
        for field in ("provider", "provider_version", "requested_model"):
            if trial[field] != release_judge[field]:
                raise CalibrationError(
                    f"{location}.{field} differs from the release judge lock"
                )
        actual_models = trial["actual_models"]
        if (
            not isinstance(actual_models, list)
            or not actual_models
            or not all(isinstance(model, str) and model for model in actual_models)
            or len(actual_models) != len(set(actual_models))
        ):
            raise CalibrationError(f"{location}.actual_models is invalid")
        model_set = tuple(sorted(actual_models))
        if list(model_set) != raw_models:
            raise CalibrationError(
                f"{location}.actual_models differ from raw modelUsage"
            )
        cost = trial["cost_usd"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost))
            or float(cost) < 0
            or float(cost) != raw_cost
        ):
            raise CalibrationError(f"{location}.cost_usd differs from raw provenance")
        trial_costs.append((key, float(cost)))
        model_sets.add(model_set)
        if spend_attempt_id in spend_attempt_ids:
            raise CalibrationError(f"{location} spend attempt id is duplicated")
        spend_attempt_ids.add(spend_attempt_id)
        executable_sha256s.add(executable_sha256)
        primary_prefix = release_judge["required_primary_model_prefix"]
        allowed_prefixes = tuple(release_judge["allowed_auxiliary_model_prefixes"])
        allowed = any(model.startswith(primary_prefix) for model in model_set) and all(
            model.startswith(primary_prefix) or model.startswith(allowed_prefixes)
            for model in model_set
        )
        if not allowed:
            model_call_failures.append(key)
        normalized[key] = validate_response(
            bundle, pair_by_id[pair_id], trial["response"], order
        )
    missing = expected_keys - observed_keys
    if missing:
        raise CalibrationError(f"evidence omitted {len(missing)} required trials")
    spend_limits = bundle.release["execution_limits"]
    spend_ledger = _exact(
        evidence["spend_ledger"],
        {"records", "records_sha256", "charged_usd"},
        "evidence.spend_ledger",
    )
    records = spend_ledger["records"]
    if not isinstance(records, list):
        raise CalibrationError("evidence.spend_ledger.records must be an array")
    if spend_ledger["records_sha256"] != canonical_sha256(records):
        raise CalibrationError("evidence spend ledger digest is stale")
    attempts: dict[str, tuple[float, float | None, str | None, str, str]] = {}
    seen_attempt_ids: set[str] = set()
    historical = 0.0
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, dict):
            raise CalibrationError("evidence spend ledger record must be an object")
        event = raw_record.get("event")
        attempt_id = raw_record.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None
        ):
            raise CalibrationError("evidence spend ledger attempt id is invalid")
        record_request_sha256 = raw_record.get("request_sha256")
        record_invocation_id = raw_record.get("invocation_id")
        if (
            not isinstance(record_request_sha256, str)
            or HASH_RE.fullmatch(record_request_sha256) is None
            or not isinstance(record_invocation_id, str)
            or HASH_RE.fullmatch(record_invocation_id) is None
        ):
            raise CalibrationError("evidence spend request binding is invalid")
        if event == "reserve":
            record = _exact(
                raw_record,
                {
                    "event",
                    "attempt_id",
                    "invocation_id",
                    "request_sha256",
                    "reserved_usd",
                },
                f"evidence.spend_ledger.records[{index}]",
            )
            value = record["reserved_usd"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                or attempt_id in seen_attempt_ids
            ):
                raise CalibrationError("evidence spend reservation is invalid")
            seen_attempt_ids.add(attempt_id)
            attempts[attempt_id] = (
                float(value),
                None,
                None,
                record_request_sha256,
                record_invocation_id,
            )
        elif event in {"reconcile", "forfeit"}:
            record = _exact(
                raw_record,
                {
                    "event",
                    "attempt_id",
                    "charged_usd",
                    "invocation_id",
                    "request_sha256",
                },
                f"evidence.spend_ledger.records[{index}]",
            )
            charged = record["charged_usd"]
            if (
                isinstance(charged, bool)
                or not isinstance(charged, (int, float))
                or not math.isfinite(float(charged))
                or float(charged) < 0
                or attempt_id not in attempts
                or attempts[attempt_id][1] is not None
                or float(charged) > attempts[attempt_id][0]
                or (event == "forfeit" and float(charged) != attempts[attempt_id][0])
                or record_request_sha256 != attempts[attempt_id][3]
                or record_invocation_id != attempts[attempt_id][4]
            ):
                raise CalibrationError("evidence spend close record is invalid")
            attempts[attempt_id] = (
                attempts[attempt_id][0],
                float(charged),
                event,
                record_request_sha256,
                record_invocation_id,
            )
        elif event == "historical":
            record = _exact(
                raw_record,
                {
                    "event",
                    "attempt_id",
                    "charged_usd",
                    "invocation_id",
                    "request_sha256",
                },
                f"evidence.spend_ledger.records[{index}]",
            )
            charged = record["charged_usd"]
            if (
                isinstance(charged, bool)
                or not isinstance(charged, (int, float))
                or not math.isfinite(float(charged))
                or float(charged) < 0
                or attempt_id in seen_attempt_ids
            ):
                raise CalibrationError("evidence historical spend is invalid")
            seen_attempt_ids.add(attempt_id)
            historical += float(charged)
        else:
            raise CalibrationError("evidence spend ledger event is invalid")
    ledger_total = historical + sum(
        reserved if charged is None else charged
        for reserved, charged, _event, _request, _invocation in attempts.values()
    )
    reported_ledger_total = spend_ledger["charged_usd"]
    if (
        isinstance(reported_ledger_total, bool)
        or not isinstance(reported_ledger_total, (int, float))
        or not math.isclose(float(reported_ledger_total), ledger_total, abs_tol=1e-12)
    ):
        raise CalibrationError("evidence spend ledger total is invalid")
    for trial in trials:
        attempt = attempts.get(trial["spend_attempt_id"])
        if (
            attempt is None
            or attempt[2] != "reconcile"
            or not math.isclose(
                attempt[1] or 0.0, float(trial["cost_usd"]), abs_tol=1e-12
            )
            or attempt[3] != trial["request_sha256"]
            or attempt[4] != trial["invocation_id"]
        ):
            raise CalibrationError("trial is not bound to reconciled spend")
    spend_limit_failures = [
        {"trial": list(key), "cost_usd": cost, "kind": "per-invocation"}
        for key, cost in trial_costs
        if cost > spend_limits["per_invocation_max_usd"]
    ]
    total_cost_usd = sum(cost for _, cost in trial_costs)
    if ledger_total > spend_limits["run_max_usd"]:
        spend_limit_failures.append(
            {
                "trial": None,
                "cost_usd": ledger_total,
                "kind": "run-total",
            }
        )
    order_disagreements: list[dict[str, Any]] = []
    per_repetition: dict[tuple[str, int], dict[str, Any]] = {}
    for pair in bundle.manifest["pairs"]:
        for repetition in range(pair["repetitions"]):
            ab = normalized[(pair["id"], repetition, "AB")]
            ba = normalized[(pair["id"], repetition, "BA")]
            if ab != ba:
                order_disagreements.append(
                    {"pair_id": pair["id"], "repetition": repetition}
                )
            else:
                per_repetition[(pair["id"], repetition)] = ab
    sentinel_instability: list[str] = []
    for pair in bundle.manifest["pairs"]:
        if not pair["sentinel"]:
            continue
        vectors = {
            canonical_sha256(per_repetition.get((pair["id"], repetition)))
            for repetition in range(pair["repetitions"])
        }
        if len(vectors) != 1 or None in [
            per_repetition.get((pair["id"], repetition))
            for repetition in range(pair["repetitions"])
        ]:
            sentinel_instability.append(pair["id"])
    gold_field = bundle.release["gold_source"]
    expected_outcomes: list[str] = []
    observed_outcomes: list[str] = []
    expected_eligibility: list[str] = []
    observed_eligibility: list[str] = []
    expected_requirement_statuses: list[str] = []
    observed_requirement_statuses: list[str] = []
    expected_violation_sets: list[tuple[str, ...]] = []
    observed_violation_sets: list[tuple[str, ...] | str] = []
    expected_criteria: dict[str, list[str]] = {criterion: [] for criterion in CRITERIA}
    observed_criteria: dict[str, list[str]] = {criterion: [] for criterion in CRITERIA}
    critical_failures: list[str] = []
    eligibility_errors: list[dict[str, Any]] = []
    requirement_status_errors: list[dict[str, Any]] = []
    violation_set_errors: list[dict[str, Any]] = []
    critical_hard_admissibility_errors: list[str] = []
    length_bias_failures: list[str] = []
    unsupported_performance_failures: list[str] = []
    unsupported_qualitative_failures: list[dict[str, Any]] = []
    pair_results: list[dict[str, Any]] = []
    for pair in bundle.manifest["pairs"]:
        raw_gold = pair["adjudication"][gold_field]
        if raw_gold is None:
            continue
        gold = _label_set(
            pair,
            raw_gold,
            f"pair {pair['id']} gold",
            resolution=gold_field in {"resolution", "scoring_gold"},
        )
        repetitions = [
            per_repetition.get((pair["id"], repetition))
            for repetition in range(pair["repetitions"])
        ]
        hashes = {canonical_sha256(value) for value in repetitions if value is not None}
        observed = (
            repetitions[0]
            if all(value is not None for value in repetitions) and len(hashes) == 1
            else None
        )
        observed_outcome = observed["outcome"] if observed else "inconclusive"
        expected_outcomes.append(gold["outcome"])
        observed_outcomes.append(observed_outcome)
        pair_has_admissibility_error = observed is None
        for side in ("A", "B"):
            expected_decision = gold["eligibility"][side]["decision"]
            observed_decision = (
                observed["eligibility"][side] if observed else "inconclusive"
            )
            expected_eligibility.append(expected_decision)
            observed_eligibility.append(observed_decision)
            if expected_decision != observed_decision:
                eligibility_errors.append(
                    {
                        "pair_id": pair["id"],
                        "side": side,
                        "expected": expected_decision,
                        "observed": observed_decision,
                    }
                )
                pair_has_admissibility_error = True
            expected_violations = tuple(gold["eligibility"][side]["violations"])
            observed_violations: tuple[str, ...] | str = (
                observed["violations"][side] if observed else "inconclusive"
            )
            expected_violation_sets.append(expected_violations)
            observed_violation_sets.append(observed_violations)
            if expected_violations != observed_violations:
                violation_set_errors.append(
                    {
                        "pair_id": pair["id"],
                        "side": side,
                        "expected": list(expected_violations),
                        "observed": (
                            list(observed_violations)
                            if isinstance(observed_violations, tuple)
                            else observed_violations
                        ),
                    }
                )
                pair_has_admissibility_error = True
            for requirement in pair["contract"]["requirements"]:
                requirement_id = requirement["id"]
                expected_status = gold["eligibility"][side]["requirement_statuses"][
                    requirement_id
                ]
                observed_status = (
                    observed["checks"][side][requirement_id]
                    if observed
                    else "inconclusive"
                )
                expected_requirement_statuses.append(expected_status)
                observed_requirement_statuses.append(observed_status)
                if expected_status != observed_status:
                    requirement_status_errors.append(
                        {
                            "pair_id": pair["id"],
                            "side": side,
                            "requirement_id": requirement_id,
                            "expected": expected_status,
                            "observed": observed_status,
                        }
                    )
                    pair_has_admissibility_error = True
        if gold["criteria"] is not None:
            for criterion in CRITERIA:
                expected_criteria[criterion].append(gold["criteria"][criterion])
                observed_criteria[criterion].append(
                    observed["criteria"][criterion]
                    if observed and observed["criteria"] is not None
                    else "inconclusive"
                )
        has_hard_constraint = any(
            requirement["kind"] == "hard_constraint"
            for requirement in pair["contract"]["requirements"]
        )
        if (pair["critical"] or has_hard_constraint) and observed_outcome != gold[
            "outcome"
        ]:
            critical_failures.append(pair["id"])
        if (pair["critical"] or has_hard_constraint) and pair_has_admissibility_error:
            critical_hard_admissibility_errors.append(pair["id"])
        if (
            pair["probes"]["length_bias"] is not None
            and observed_outcome != gold["outcome"]
        ):
            length_bias_failures.append(pair["id"])
        if observed and observed["unsupported_performance"]:
            unsupported_performance_failures.append(pair["id"])
        if observed and observed["unsupported_qualitative"]:
            unsupported_qualitative_failures.append(
                {
                    "pair_id": pair["id"],
                    "criteria": list(observed["unsupported_qualitative"]),
                }
            )
        pair_results.append(
            {
                "pair_id": pair["id"],
                "expected_outcome": gold["outcome"],
                "observed_outcome": observed_outcome,
                "admissibility_exact": not pair_has_admissibility_error,
            }
        )
    if not expected_outcomes:
        outcome_ba = 0.0
        outcome_kappa = 0.0
        eligibility_accuracy = 0.0
        requirement_status_accuracy = 0.0
        violation_set_accuracy = 0.0
        criterion_metrics = {
            criterion: {
                "sample_size": 0,
                "balanced_accuracy": 0.0,
                "cohen_kappa": 0.0,
            }
            for criterion in CRITERIA
        }
    else:
        outcome_ba = _balanced_accuracy(expected_outcomes, observed_outcomes)
        outcome_kappa = _cohen_kappa(expected_outcomes, observed_outcomes)
        eligibility_accuracy = sum(
            left == right
            for left, right in zip(
                expected_eligibility, observed_eligibility, strict=True
            )
        ) / len(expected_eligibility)
        requirement_status_accuracy = sum(
            left == right
            for left, right in zip(
                expected_requirement_statuses,
                observed_requirement_statuses,
                strict=True,
            )
        ) / len(expected_requirement_statuses)
        violation_set_accuracy = sum(
            left == right
            for left, right in zip(
                expected_violation_sets, observed_violation_sets, strict=True
            )
        ) / len(expected_violation_sets)
        criterion_metrics = {
            criterion: {
                "sample_size": len(expected_criteria[criterion]),
                "balanced_accuracy": _balanced_accuracy(
                    expected_criteria[criterion], observed_criteria[criterion]
                ),
                "cohen_kappa": _cohen_kappa(
                    expected_criteria[criterion], observed_criteria[criterion]
                ),
            }
            for criterion in CRITERIA
        }
    acceptance = bundle.release["acceptance"]
    gates = {
        "runtime_adapter_compatibility": bundle.release["test_release"]
        or bundle.release["runtime_adapter"]["shared_harness_compatible"],
        "adjudication_complete": manifest_summary["adjudication_complete"]
        or bundle.release["test_release"],
        "outcome_balanced_accuracy": outcome_ba
        >= acceptance["minimum_outcome_balanced_accuracy"],
        "outcome_cohen_kappa": outcome_kappa
        >= acceptance["minimum_outcome_cohen_kappa"],
        "eligibility_accuracy": eligibility_accuracy
        >= acceptance["minimum_eligibility_accuracy"],
        "requirement_status_accuracy": requirement_status_accuracy
        >= acceptance["minimum_requirement_status_accuracy"],
        "violation_set_accuracy": violation_set_accuracy
        >= acceptance["minimum_violation_set_accuracy"],
        "per_criterion_balanced_accuracy": min(
            metric["balanced_accuracy"] for metric in criterion_metrics.values()
        )
        >= acceptance["minimum_per_criterion_balanced_accuracy"],
        "order_consistency": not order_disagreements
        if acceptance["require_zero_order_disagreements"]
        else True,
        "sentinel_stability": not sentinel_instability
        if acceptance["require_zero_sentinel_instability"]
        else True,
        "critical_hard_outcomes": not critical_failures
        if acceptance["require_zero_critical_hard_outcome_failures"]
        else True,
        "critical_hard_admissibility": not critical_hard_admissibility_errors
        if acceptance["require_zero_critical_hard_admissibility_errors"]
        else True,
        "length_bias": not length_bias_failures
        if acceptance["require_zero_length_bias_failures"]
        else True,
        "unsupported_performance": not unsupported_performance_failures
        if acceptance["require_zero_unsupported_performance"]
        else True,
        "unsupported_qualitative": not unsupported_qualitative_failures
        if acceptance["require_zero_unsupported_qualitative"]
        else True,
        "spend_limits": not spend_limit_failures
        if acceptance["require_zero_spend_limit_failures"]
        else True,
        "model_stability": len(model_sets) == 1 and not model_call_failures
        if acceptance["require_stable_model_set"]
        else True,
        "executable_stability": len(executable_sha256s) == 1
        if acceptance["require_stable_executable"]
        else True,
        "systemd_stability": len(systemd_versions) == 1,
    }
    return {
        **release_summary,
        **manifest_summary,
        "actual_model_sets": [list(models) for models in sorted(model_sets)],
        "executable_sha256s": sorted(executable_sha256s),
        "systemd_versions": sorted(systemd_versions),
        "model_call_failures": [list(key) for key in model_call_failures],
        "outcome_balanced_accuracy": outcome_ba,
        "outcome_cohen_kappa": outcome_kappa,
        "eligibility_accuracy": eligibility_accuracy,
        "requirement_status_accuracy": requirement_status_accuracy,
        "violation_set_accuracy": violation_set_accuracy,
        "eligibility_errors": eligibility_errors,
        "requirement_status_errors": requirement_status_errors,
        "violation_set_errors": violation_set_errors,
        "criterion_metrics": criterion_metrics,
        "order_disagreements": order_disagreements,
        "sentinel_instability": sentinel_instability,
        "critical_hard_outcome_failures": critical_failures,
        "critical_hard_admissibility_errors": critical_hard_admissibility_errors,
        "length_bias_failures": length_bias_failures,
        "unsupported_performance_failures": unsupported_performance_failures,
        "unsupported_qualitative_failures": unsupported_qualitative_failures,
        "spend_limit_failures": spend_limit_failures,
        "total_cost_usd": total_cost_usd,
        "ledger_charged_usd": ledger_total,
        "pair_results": pair_results,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the locked corpus or score complete offline evidence."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--release", default="release.json")
    parser.add_argument("--allow-test-release", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        bundle = load_bundle(
            args.root,
            args.release,
            allow_test_release=args.allow_test_release,
        )
        release = validate_release(bundle)
        manifest = validate_manifest(bundle.manifest, bundle.rubric)
        result = (
            evaluate_evidence(bundle, load_json(args.evidence))
            if args.evidence
            else {**release, **manifest, "passed": manifest["adjudication_complete"]}
        )
    except CalibrationError as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
