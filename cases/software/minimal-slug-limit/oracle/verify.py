#!/usr/bin/env python3
"""Hidden behavioral oracle for the minimal slug change."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from untrusted_child import run_untrusted  # noqa: E402


def assertion(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
results: list[dict[str, object]] = []
loaded_workspace_files: set[str] = set()
worker_source = r"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys

workspace = Path.cwd().resolve()
loaded = set()
active = [True]

def audit(event, args):
    if not active[0] or not args:
        return
    raw = args[0] if event == "open" else getattr(args[0], "co_filename", None) if event == "exec" else None
    if not isinstance(raw, (str, bytes)):
        return
    try:
        path = Path(raw).resolve()
    except (OSError, TypeError, ValueError):
        return
    if path.is_relative_to(workspace) and path.is_file():
        loaded.add(path.relative_to(workspace).as_posix())

sys.addaudithook(audit)
captured = io.StringIO()
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    spec = importlib.util.spec_from_file_location("candidate_slugger", workspace / "slugger.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load slugger.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(workspace))
    spec.loader.exec_module(module)

protocol = sys.__stdout__
for line in sys.stdin:
    request = json.loads(line)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            value = module.make_slug(*request["args"], **request.get("kwargs", {}))
        response = {"id": request["id"], "ok": True, "value": value}
    except Exception as error:
        response = {"id": request["id"], "ok": False, "error_type": type(error).__name__}
    protocol.write(json.dumps(response, sort_keys=True) + "\n")
active[0] = False
for loaded_module in tuple(sys.modules.values()):
    raw_file = getattr(loaded_module, "__file__", None)
    if raw_file:
        path = Path(raw_file).resolve()
        if path.is_relative_to(workspace):
            loaded.add(path.relative_to(workspace).as_posix())
protocol.write(json.dumps({"meta": {"loaded_files": sorted(loaded)}}, sort_keys=True) + "\n")
"""

limited_cases = {
    ("Hello brave new world", 11): "hello-brave",
    ("Supercalifragilistic", 7): "superca",
    ("alpha beta", 6): "alpha",
    ("alpha beta", 7): "alpha",
    ("alpha beta", 8): "alpha",
    ("alpha beta", 9): "alpha",
    ("alpha beta", 10): "alpha-beta",
    ("---", 3): "",
}
legacy_cases = {
    "Hello, World!": "hello-world",
    "  Already---spaced  ": "already-spaced",
    "HTTP 204 / No Content": "http-204-no-content",
    "": "",
}
requests: list[dict[str, object]] = []
for index, (arguments, _expected) in enumerate(limited_cases.items()):
    requests.append({"id": f"limited-{index}", "args": list(arguments)})
requests.append(
    {
        "id": "keyword-limit",
        "args": ["Hello brave new world"],
        "kwargs": {"max_length": 11},
    }
)
for index, limit in enumerate((0, -1)):
    requests.append({"id": f"invalid-{index}", "args": ["alpha", limit]})
for index, title in enumerate(legacy_cases):
    requests.append({"id": f"legacy-{index}", "args": [title]})

with tempfile.TemporaryDirectory(prefix="slug-worker-") as raw_worker:
    worker_workspace = Path(raw_worker) / "workspace"
    shutil.copytree(workspace, worker_workspace)
    completed = run_untrusted(
        [sys.executable, "-c", worker_source],
        worker_workspace,
        8,
        input_text="".join(json.dumps(item) + "\n" for item in requests),
    )
try:
    if not completed.passed:
        raise RuntimeError(
            completed.sandbox_error
            or ("candidate timed out" if completed.timed_out else completed.stderr)
            or f"candidate exited {completed.returncode}"
        )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    meta = responses.pop()
    if set(meta) != {"meta"} or len(responses) != len(requests):
        raise ValueError("candidate worker returned an incomplete protocol response")
    by_id = {response["id"]: response for response in responses}
    if len(by_id) != len(requests):
        raise ValueError("candidate worker returned duplicate response IDs")
    loaded_workspace_files.update(meta["meta"]["loaded_files"])

    limited_failures = []
    for index, ((title, limit), expected) in enumerate(limited_cases.items()):
        response = by_id[f"limited-{index}"]
        if not response.get("ok") or response.get("value") != expected:
            limited_failures.append(
                f"{title!r}/{limit}: expected {expected!r}, got {response!r}"
            )
    invalid_rejected = all(
        not by_id[f"invalid-{index}"].get("ok")
        and by_id[f"invalid-{index}"].get("error_type") in {"TypeError", "ValueError"}
        for index in range(2)
    )
    keyword_response = by_id["keyword-limit"]
    keyword_ok = (
        keyword_response.get("ok") is True
        and keyword_response.get("value") == "hello-brave"
    )
    limited_ok = not limited_failures and invalid_rejected and keyword_ok
    evidence = "limited slugs and invalid limits matched the contract"
    if limited_failures or not invalid_rejected or not keyword_ok:
        evidence = "; ".join(
            limited_failures
            or (
                ["zero/negative limit was accepted"]
                if not invalid_rejected
                else [f"max_length keyword failed: {keyword_response!r}"]
            )
        )
    results.append(assertion("slug-limit-behavior", limited_ok, evidence))

    legacy_failures = []
    for index, (title, expected) in enumerate(legacy_cases.items()):
        response = by_id[f"legacy-{index}"]
        if not response.get("ok") or response.get("value") != expected:
            legacy_failures.append(
                f"{title!r}: expected {expected!r}, got {response!r}"
            )
    results.append(
        assertion(
            "legacy-slug-behavior",
            not legacy_failures,
            "legacy calls retained their outputs"
            if not legacy_failures
            else "; ".join(legacy_failures),
        )
    )
except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
    message = f"candidate could not execute: {type(error).__name__}: {error}"
    results.extend(
        [
            assertion("slug-limit-behavior", False, message),
            assertion("legacy-slug-behavior", False, message),
        ]
    )

signature_ok = False
signature_detail = "slugger.py could not be inspected"
try:
    syntax_tree = ast.parse((workspace / "slugger.py").read_text(encoding="utf-8"))
    definitions = [
        node
        for node in syntax_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "make_slug"
    ]
    if len(definitions) == 1:
        arguments = definitions[0].args
        signature_ok = (
            not arguments.posonlyargs
            and [argument.arg for argument in arguments.args] == ["title", "max_length"]
            and len(arguments.defaults) == 1
            and isinstance(arguments.defaults[0], ast.Constant)
            and arguments.defaults[0].value is None
            and arguments.vararg is None
            and not arguments.kwonlyargs
            and arguments.kwarg is None
        )
        signature_detail = (
            "make_slug keeps title and optional max_length as keyword-compatible parameters"
            if signature_ok
            else "make_slug must have signature (title, max_length=None)"
        )
except (OSError, SyntaxError) as error:
    signature_detail = f"signature inspection failed: {type(error).__name__}: {error}"
results.append(assertion("public-call-contract", signature_ok, signature_detail))


def allowed_test_artifact(path: Path) -> bool:
    relative = path.relative_to(workspace)
    if path.name == "__init__.py" and relative.parts[:-1] == ("tests",):
        return not path.read_text(encoding="utf-8").strip()
    return (
        path.suffix == ".py"
        and path.name.startswith("test_")
        and (len(relative.parts) == 1 or relative.parts[0] == "tests")
    )


all_files = sorted(
    path.relative_to(workspace).as_posix()
    for path in workspace.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
)
unexpected_files = sorted(
    relative
    for relative in all_files
    if relative != "slugger.py" and not allowed_test_artifact(workspace / relative)
)
reachable_extra = sorted(
    relative
    for relative in loaded_workspace_files - {"slugger.py"}
    if "__pycache__" not in Path(relative).parts and not relative.endswith(".pyc")
)

third_party_imports: set[str] = set()
try:
    syntax_tree = ast.parse((workspace / "slugger.py").read_text(encoding="utf-8"))
    for node in ast.walk(syntax_tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported = {node.module.partition(".")[0]}
        else:
            continue
        third_party_imports.update(imported - sys.stdlib_module_names)
except (OSError, SyntaxError) as error:
    third_party_imports.add(f"unreadable-source:{type(error).__name__}")

dependency_names = {
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "uv.lock",
}
dependency_files = sorted(
    path.relative_to(workspace).as_posix()
    for path in workspace.rglob("*")
    if path.is_file()
    and (path.name in dependency_names or path.name.startswith("requirements"))
)
scope_ok = (
    not unexpected_files
    and not reachable_extra
    and not third_party_imports
    and not dependency_files
)
results.append(
    assertion(
        "minimal-production-scope",
        scope_ok,
        "only slugger.py was production-reachable; test artifacts and imports were isolated"
        if scope_ok
        else (
            f"unexpected files={unexpected_files}, reachable extras={reachable_extra}, "
            f"third-party imports={sorted(third_party_imports)}, dependency files={dependency_files}"
        ),
    )
)

print(
    json.dumps(
        {"passed": all(item["passed"] for item in results), "assertions": results}
    )
)
