#!/usr/bin/env python3
"""Hidden behavioral oracle for local JavaScript symbol verification."""

from __future__ import annotations

import hashlib
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


def check(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
store_path = workspace / "token-store.js"

node_probe = r"""
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const requests = JSON.parse(fs.readFileSync(0, "utf8"));
const candidateOutput = [];
console.log = (...values) => candidateOutput.push(values.join(" "));
console.error = (...values) => candidateOutput.push(values.join(" "));
const output = [];
try {
  const store = require("./token-store.js");
  const originalReadFreshToken = store.readFreshToken;
  let inSessionCall = false;
  let sessionDelegations = 0;
  store.readFreshToken = function instrumentedReadFreshToken(cachePath, now) {
    if (inSessionCall) {
      sessionDelegations += 1;
      if (cachePath.endsWith("session-generic.json")) {
        throw new RangeError("delegation propagation sentinel");
      }
    }
    return originalReadFreshToken(cachePath, now);
  };
  const session = require("./session.js");
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "symbol-worker-"));
  for (const request of requests) {
    const cachePath = path.join(temp, `${request.id}.json`);
    if (request.contents !== null) fs.writeFileSync(cachePath, request.contents);
    try {
      const callable = request.operation === "store" ? originalReadFreshToken : session.getCachedAuthorization;
      inSessionCall = request.operation === "session";
      output.push({id: request.id, ok: true, value: callable(cachePath, request.now)});
    } catch (error) {
      output.push({id: request.id, ok: false, errorType: error.name});
    } finally {
      inSessionCall = false;
    }
  }
  output.push({meta: {storeExports: Object.keys(store).sort(), sessionDelegations}});
} catch (error) {
  output.push({fatal: `${error.name}: ${error.message}`});
}
process.stdout.write(JSON.stringify(output));
"""

good_cache = json.dumps({"token": "abc123", "expiresAt": 2000})
expired_cache = json.dumps({"token": "old", "expiresAt": 999})
requests = [
    {"id": "store-good", "operation": "store", "contents": good_cache, "now": 1000},
    {
        "id": "store-expired",
        "operation": "store",
        "contents": expired_cache,
        "now": 1000,
    },
    {"id": "session-good", "operation": "session", "contents": good_cache, "now": 1000},
    {
        "id": "session-expired",
        "operation": "session",
        "contents": expired_cache,
        "now": 1000,
    },
    {"id": "session-missing", "operation": "session", "contents": None, "now": 1000},
    {
        "id": "session-malformed",
        "operation": "session",
        "contents": "{bad-json",
        "now": 1000,
    },
    {
        "id": "session-malformed-shape",
        "operation": "session",
        "contents": json.dumps({"token": "", "expiresAt": 2000}),
        "now": 1000,
    },
    {
        "id": "session-generic",
        "operation": "session",
        "contents": good_cache,
        "now": 1000,
    },
]
try:
    with tempfile.TemporaryDirectory(prefix="symbol-worker-") as raw_worker:
        worker_workspace = Path(raw_worker) / "workspace"
        shutil.copytree(workspace, worker_workspace)
        completed = run_untrusted(
            ["node", "-e", node_probe],
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
    behavior_ok = (
        by_id["session-good"].get("ok") is True
        and by_id["session-good"].get("value") == "Bearer abc123"
        and by_id["session-expired"].get("ok") is True
        and by_id["session-expired"].get("value") is None
        and by_id["session-missing"].get("ok") is True
        and by_id["session-missing"].get("value") is None
        and by_id["session-malformed"].get("ok") is False
        and by_id["session-malformed"].get("errorType") == "SyntaxError"
        and by_id["session-malformed-shape"]
        == {
            "id": "session-malformed-shape",
            "ok": False,
            "errorType": "TypeError",
        }
        and by_id["session-generic"]
        == {"id": "session-generic", "ok": False, "errorType": "RangeError"}
    )
    expected_store_digest = (
        "701bfb8188fb1429f32cd9d32a80a7001155ab5f126aecbedb84f9bd7943f413"
    )
    actual_store_digest = hashlib.sha256(store_path.read_bytes()).hexdigest()
    contract_ok = (
        meta["meta"] == {"storeExports": ["readFreshToken"], "sessionDelegations": 6}
        and by_id["store-good"].get("ok") is True
        and by_id["store-good"].get("value") == "abc123"
        and by_id["store-expired"].get("ok") is True
        and by_id["store-expired"].get("value") is None
        and actual_store_digest == expected_store_digest
    )
    detail = "candidate protocol completed"
except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
    behavior_ok = False
    contract_ok = False
    detail = f"probe failed: {type(error).__name__}: {error}"

package_json = workspace / "package.json"
dependencies: dict[str, object] = {}
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
external_imports: set[str] = set()
for source_path in workspace.rglob("*.js"):
    for specifier in import_pattern.findall(source_path.read_text(encoding="utf-8")):
        if not specifier.startswith((".", "node:")):
            external_imports.add(specifier)
dependency_ok = (
    not dependencies
    and not lockfiles
    and not external_imports
    and not (workspace / "node_modules").exists()
)
source_ok, source_detail = source_guard(
    workspace,
    ["session.js"],
    "javascript",
    ["token-store.js"],
)
dependency_ok = dependency_ok and source_ok

assertions = [
    check(
        "cached-token-behavior",
        behavior_ok,
        "fresh, expired, missing, and malformed cache cases behaved correctly"
        if behavior_ok
        else str(detail),
    ),
    check(
        "local-api-contract",
        contract_ok,
        "token-store retained and executed its readFreshToken contract"
        if contract_ok
        else str(detail),
    ),
    check(
        "no-runtime-dependency",
        dependency_ok,
        "no runtime package or node_modules tree was introduced"
        if dependency_ok
        else (
            f"dependencies={sorted(dependencies)}, lockfiles={lockfiles}, "
            f"external imports={sorted(external_imports)}, "
            f"node_modules={workspace.joinpath('node_modules').exists()}; "
            f"{source_detail}"
        ),
    ),
]
print(
    json.dumps(
        {"passed": all(item["passed"] for item in assertions), "assertions": assertions}
    )
)
