#!/usr/bin/env python3
"""Hidden CommonJS compatibility and feature oracle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from untrusted_child import run_untrusted  # noqa: E402
from verifier_lib import source_guard  # noqa: E402


def assertion(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
probe_source = r"""
const fs = require("node:fs");
const requests = JSON.parse(fs.readFileSync(0, "utf8"));
Object.defineProperty(process, "execArgv", {value: Object.freeze([]), writable: false, configurable: false});
Object.defineProperty(process, "argv", {value: Object.freeze(["node", "oracle-probe"]), writable: false, configurable: false});
const candidateOutput = [];
console.log = (...values) => candidateOutput.push(values.join(" "));
console.error = (...values) => candidateOutput.push(values.join(" "));
const output = [];
try {
  const parseDuration = require("./parse-duration.js");
  for (const request of requests) {
    try {
      const callable = request.operation === "safe" ? parseDuration.safe : parseDuration;
      output.push({id: request.id, ok: true, value: callable(request.value)});
    } catch (error) {
      output.push({id: request.id, ok: false, errorType: error.name});
    }
  }
  output.push({meta: {exportType: typeof parseDuration, safeType: typeof parseDuration.safe}});
} catch (error) {
  output.push({fatal: `${error.name}: ${error.message}`});
}
process.stdout.write(JSON.stringify(output));
"""

requests = [
    {"id": "legacy-minutes", "operation": "call", "value": "2m"},
    {"id": "legacy-seconds", "operation": "call", "value": "03s"},
    {"id": "legacy-hours", "operation": "call", "value": "1h"},
    {"id": "invalid-type", "operation": "call", "value": 12},
    {"id": "invalid-unit", "operation": "call", "value": "2days"},
    {"id": "milliseconds", "operation": "call", "value": "250ms"},
    {"id": "fractional-seconds", "operation": "call", "value": "1.5s"},
    {"id": "fractional-minutes", "operation": "call", "value": "0.25m"},
    {"id": "integer-milliseconds", "operation": "call", "value": "2ms"},
    {"id": "safe-valid", "operation": "safe", "value": "1.5s"},
    {"id": "safe-invalid", "operation": "safe", "value": "not-a-duration"},
    {"id": "safe-null", "operation": "safe", "value": None},
]
legacy_invalid = {
    "legacy-leading-space": " 2m",
    "legacy-trailing-space": "2m ",
    "legacy-plus-sign": "+2m",
    "legacy-minus-sign": "-2m",
    "legacy-empty": "",
    "legacy-decimal-leading-dot": ".5s",
    "legacy-decimal-trailing-dot": "1.s",
    "legacy-decimal-multiple-dots": "1.5.0s",
    "legacy-uppercase-unit": "2M",
    "legacy-separated-unit": "2 m",
}
requests.extend(
    {"id": identifier, "operation": "call", "value": value}
    for identifier, value in legacy_invalid.items()
)
try:
    with tempfile.TemporaryDirectory(prefix="duration-worker-") as raw_worker:
        worker_workspace = Path(raw_worker) / "workspace"
        shutil.copytree(workspace, worker_workspace)
        completed = run_untrusted(
            ["node", "-e", probe_source],
            worker_workspace,
            8,
            input_text=json.dumps(requests),
        )
    if not completed.passed:
        raise RuntimeError(
            completed.sandbox_error
            or ("candidate timed out" if completed.timed_out else completed.stderr)
            or f"candidate exited {completed.returncode}"
        )
    responses = json.loads(completed.stdout)
    meta = responses.pop()
    if set(meta) != {"meta"} or len(responses) != len(requests):
        raise ValueError("candidate worker returned an incomplete protocol response")
    by_id = {response["id"]: response for response in responses}
    if len(by_id) != len(requests):
        raise ValueError("candidate worker returned duplicate response IDs")
    legacy_ok = (
        meta["meta"] == {"exportType": "function", "safeType": "function"}
        and by_id["legacy-minutes"]
        == {"id": "legacy-minutes", "ok": True, "value": 120000}
        and by_id["legacy-seconds"]
        == {"id": "legacy-seconds", "ok": True, "value": 3000}
        and by_id["legacy-hours"]
        == {"id": "legacy-hours", "ok": True, "value": 3600000}
        and by_id["invalid-type"].get("errorType") == "TypeError"
        and by_id["invalid-unit"].get("errorType") == "RangeError"
        and all(
            by_id[identifier]
            == {"id": identifier, "ok": False, "errorType": "RangeError"}
            for identifier in legacy_invalid
        )
    )
    extension_ok = (
        by_id["milliseconds"].get("value") == 250
        and by_id["fractional-seconds"].get("value") == 1500
        and by_id["fractional-minutes"].get("value") == 15000
        and by_id["integer-milliseconds"].get("value") == 2
        and all(
            by_id[identifier].get("ok") is True
            for identifier in (
                "milliseconds",
                "fractional-seconds",
                "fractional-minutes",
                "integer-milliseconds",
            )
        )
    )
    safe_ok = (
        by_id["safe-valid"].get("ok") is True
        and by_id["safe-valid"].get("value") == 1500
        and by_id["safe-invalid"].get("ok") is True
        and by_id["safe-invalid"].get("value") is None
        and by_id["safe-null"].get("ok") is True
        and by_id["safe-null"].get("value") is None
    )
    detail = "candidate protocol completed"
except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
    legacy_ok = False
    extension_ok = False
    safe_ok = False
    detail = f"probe failed: {type(error).__name__}: {error}"

dependencies: dict[str, object] = {}
package_json = workspace / "package.json"
if package_json.exists():
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
        for field in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            dependencies.update(package.get(field) or {})
    except (OSError, json.JSONDecodeError):
        dependencies = {"invalid-package-json": True}
lockfiles = sorted(
    name
    for name in (
        "bun.lock",
        "bun.lockb",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    )
    if workspace.joinpath(name).exists()
)
import_pattern = re.compile(
    r"(?:require\s*\(|import\s*\(|from\s+)[ \t]*['\"]([^'\"]+)['\"]"
)
external_imports = {
    specifier
    for source in workspace.rglob("*.js")
    for specifier in import_pattern.findall(source.read_text(encoding="utf-8"))
    if not specifier.startswith((".", "node:"))
}
forbidden_directories = sorted(
    path.relative_to(workspace).as_posix()
    for path in workspace.rglob("*")
    if path.is_dir()
    and path.name in {"node_modules", "third_party", "vendor", "vendors"}
)
source_ok, source_detail = source_guard(
    workspace,
    ["parse-duration.js"],
    "javascript",
    [],
)
dependency_ok = (
    not dependencies
    and not lockfiles
    and not external_imports
    and not forbidden_directories
    and source_ok
)
extension_ok = extension_ok and dependency_ok
assertions = [
    assertion(
        "legacy-require-contract",
        legacy_ok,
        "direct CommonJS function export and legacy errors were preserved"
        if legacy_ok
        else str(detail),
    ),
    assertion(
        "duration-extension-behavior",
        extension_ok,
        "millisecond and fractional forms used exact values without external packages"
        if extension_ok
        else (
            f"{detail}; dependencies={sorted(dependencies)}, lockfiles={lockfiles}, "
            f"external imports={sorted(external_imports)}, "
            f"forbidden directories={forbidden_directories}; {source_detail}"
        ),
    ),
    assertion(
        "safe-api-behavior",
        safe_ok,
        "safe returned parsed values or null without throwing"
        if safe_ok
        else str(detail),
    ),
]
print(
    json.dumps(
        {"passed": all(item["passed"] for item in assertions), "assertions": assertions}
    )
)
