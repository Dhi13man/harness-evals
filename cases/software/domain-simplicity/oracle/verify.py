#!/usr/bin/env python3
"""Hidden return-policy behavior and production-scope oracle."""

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


def assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return assigned_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return set().union(*(assigned_names(item) for item in target.elts))
    return set()


def top_level_lambda_bindings(tree: ast.Module) -> list[str]:
    bindings: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        value = (
            statement.value
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr))
            else None
        )
        if value is None or not any(
            isinstance(node, ast.Lambda) for node in ast.walk(value)
        ):
            continue
        statement_bindings: set[str] = set()
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                statement_bindings.update(assigned_names(target))
        elif isinstance(statement, ast.AnnAssign):
            statement_bindings.update(assigned_names(statement.target))
        bindings.update(statement_bindings or {f"line:{statement.lineno}"})
    return sorted(bindings)


def evaluator_introspection_facts(tree: ast.Module) -> set[str]:
    """Return evaluator-dependent source signals, including import aliases."""

    forbidden_modules = {"builtins", "importlib", "inspect", "os", "sys", "traceback"}
    forbidden_names = {
        "__file__",
        "__builtins__",
        "__loader__",
        "__name__",
        "__package__",
        "__spec__",
    }
    forbidden_calls = {
        "__import__",
        "compile",
        "dir",
        "eval",
        "exec",
        "globals",
        "getattr",
        "hasattr",
        "locals",
        "setattr",
        "delattr",
        "vars",
    }
    origins: dict[str, str] = {}
    facts: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                local = alias.asname or root
                origins[local] = root
                if root in forbidden_modules:
                    facts.add(f"import:{root}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.partition(".")[0]
            for alias in node.names:
                local = alias.asname or alias.name
                origins[local] = f"{root}.{alias.name}"
            if root in forbidden_modules:
                facts.add(f"import:{root}")

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            origin = origins.get(value.id) if isinstance(value, ast.Name) else None
            if origin is None and isinstance(value, ast.Call):
                if (
                    isinstance(value.func, ast.Name)
                    and value.func.id == "__import__"
                    and value.args
                    and isinstance(value.args[0], ast.Constant)
                    and isinstance(value.args[0].value, str)
                ):
                    origin = value.args[0].value.partition(".")[0]
            if origin is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in assigned_names(target):
                    if origins.get(name) != origin:
                        origins[name] = origin
                        changed = True

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in forbidden_names:
                facts.add(f"name:{node.id}")
            if node.id in forbidden_calls:
                facts.add(f"reference:{node.id}")
            origin = origins.get(node.id, "")
            if origin.partition(".")[0] in forbidden_modules:
                facts.add(f"reference:{origin}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                origin = origins.get(node.value.id, node.value.id)
                root = origin.partition(".")[0]
                if root in forbidden_modules:
                    facts.add(f"attribute:{origin}.{node.attr}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                facts.add(f"call:{node.func.id}")
            elif isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                origin = origins.get(node.func.value.id, node.func.value.id)
                if origin.partition(".")[0] in forbidden_modules:
                    facts.add(f"call:{origin}.{node.func.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (".oracle" in node.value or node.value.startswith("EVAL_"))
        ):
            facts.add(f"literal:{node.value!r}")
    return facts


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
results: list[dict[str, object]] = []
loaded_workspace_files: set[str] = set()
worker_source = r"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

driver = Path(__file__).resolve()
workspace = Path.cwd().resolve() / "candidate"
driver.unlink()
os.chdir(workspace)
loaded = set()
active = [True]

def audit(event, args):
    if not active[0] or not args:
        return
    if event == "open":
        raw = args[0]
    elif event == "exec":
        raw = getattr(args[0], "co_filename", None)
    else:
        return
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
    spec = importlib.util.spec_from_file_location("returns", workspace / "returns.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load returns.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(workspace))
    spec.loader.exec_module(module)

protocol = sys.__stdout__
for line in sys.stdin:
    request = json.loads(line)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            value = module.refund_cents(*request["args"], **request.get("kwargs", {}))
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

requests: list[dict[str, object]] = []
legacy_expected: dict[str, tuple[bool, object]] = {}
policy_expected: dict[str, tuple[bool, object]] = {}


def request(
    identifier: str,
    args: list[object],
    kwargs: dict[str, object],
    expected: object,
    *,
    policy: bool,
    succeeds: bool = True,
) -> None:
    requests.append({"id": identifier, "args": args, "kwargs": kwargs})
    target = policy_expected if policy else legacy_expected
    target[identifier] = (succeeds, expected)


for index, (paid_cents, age) in enumerate(
    ((0, 0), (1, 30), (499, 31), (2_500, 7), (999_999, 365))
):
    request(
        f"legacy-{index}",
        [paid_cents, age],
        {},
        paid_cents if age <= 30 else 0,
        policy=False,
    )
request("legacy-negative-paid", [-1, 0], {}, "ValueError", policy=False, succeeds=False)
request(
    "legacy-negative-age", [100, -1], {}, "ValueError", policy=False, succeeds=False
)

case_index = 0
for paid_cents in (0, 1, 499, 2_500, 999_999):
    for age in (0, 29, 30, 31, 365):
        for final_sale in (False, True):
            for defective in (False, True):
                expected = (
                    paid_cents if defective or (not final_sale and age <= 30) else 0
                )
                request(
                    f"policy-{case_index}",
                    [paid_cents, age],
                    {"final_sale": final_sale, "defective": defective},
                    expected,
                    policy=True,
                )
                case_index += 1

for index, invalid in enumerate((1, "yes", None)):
    request(
        f"invalid-final-sale-{index}",
        [100, 2],
        {"final_sale": invalid},
        "TypeError",
        policy=True,
        succeeds=False,
    )
    request(
        f"invalid-defective-{index}",
        [100, 2],
        {"defective": invalid},
        "TypeError",
        policy=True,
        succeeds=False,
    )
request(
    "keyword-only-flags",
    [100, 2, False, False],
    {},
    "TypeError",
    policy=True,
    succeeds=False,
)


def mismatches(
    responses: dict[str, dict[str, object]],
    expected: dict[str, tuple[bool, object]],
) -> list[str]:
    failures: list[str] = []
    for identifier, (should_succeed, expected_value) in expected.items():
        response = responses.get(identifier)
        if response is None:
            failures.append(f"{identifier}: missing response")
        elif should_succeed and not (
            response.get("ok") is True
            and type(response.get("value")) is type(expected_value)
            and response.get("value") == expected_value
        ):
            failures.append(
                f"{identifier}: expected {expected_value!r}, got {response!r}"
            )
        elif not should_succeed and not (
            response.get("ok") is False and response.get("error_type") == expected_value
        ):
            failures.append(
                f"{identifier}: expected {expected_value}, got {response!r}"
            )
    return failures


try:
    with tempfile.TemporaryDirectory(prefix="return-policy-worker-") as raw_worker:
        worker_workspace = Path(raw_worker) / "workspace"
        candidate_workspace = worker_workspace / "candidate"
        shutil.copytree(workspace, candidate_workspace)
        worker_workspace.mkdir(exist_ok=True)
        worker_workspace.joinpath("worker.py").write_text(
            worker_source, encoding="utf-8"
        )
        completed = run_untrusted(
            [sys.executable, "worker.py"],
            worker_workspace,
            8,
            input_text="".join(json.dumps(item) + "\n" for item in requests),
        )
    if not completed.passed:
        raise RuntimeError(
            completed.sandbox_error
            or ("candidate timed out" if completed.timed_out else completed.stderr)
            or f"candidate exited {completed.returncode}"
        )
    protocol = [json.loads(line) for line in completed.stdout.splitlines()]
    meta = protocol.pop()
    if set(meta) != {"meta"} or len(protocol) != len(requests):
        raise ValueError("candidate worker returned an incomplete protocol response")
    by_id = {str(response["id"]): response for response in protocol}
    if len(by_id) != len(requests):
        raise ValueError("candidate worker returned duplicate response IDs")
    loaded = meta["meta"]["loaded_files"]
    if not isinstance(loaded, list) or not all(
        isinstance(item, str) for item in loaded
    ):
        raise TypeError("candidate worker returned invalid loaded-file metadata")
    loaded_workspace_files.update(loaded)
    legacy_failures = mismatches(by_id, legacy_expected)
    policy_failures = mismatches(by_id, policy_expected)
except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
    execution_detail = f"candidate could not execute: {type(error).__name__}: {error}"
    legacy_failures = [execution_detail]
    policy_failures = [execution_detail]

results.append(
    assertion(
        "return-policy-behavior",
        not policy_failures,
        "defective precedence, final-sale denial, window boundaries, and flag types matched"
        if not policy_failures
        else "; ".join(policy_failures[:6]),
    )
)
results.append(
    assertion(
        "legacy-refund-contract",
        not legacy_failures,
        "legacy calls and negative-input errors retained their exact behavior"
        if not legacy_failures
        else "; ".join(legacy_failures[:6]),
    )
)

production_tree: ast.Module | None = None
signature_ok = False
signature_detail = "returns.py could not be inspected"
try:
    production_tree = ast.parse(
        (workspace / "returns.py").read_text(encoding="utf-8"),
        "returns.py",
    )
    definitions = [
        node
        for node in production_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "refund_cents"
    ]
    if len(definitions) == 1:
        arguments = definitions[0].args
        signature_ok = (
            not arguments.posonlyargs
            and [argument.arg for argument in arguments.args]
            == ["paid_cents", "days_since_delivery"]
            and not arguments.defaults
            and arguments.vararg is None
            and [argument.arg for argument in arguments.kwonlyargs]
            == ["final_sale", "defective"]
            and len(arguments.kw_defaults) == 2
            and all(
                isinstance(default, ast.Constant) and default.value is False
                for default in arguments.kw_defaults
            )
            and arguments.kwarg is None
        )
        signature_detail = (
            "refund_cents retained both positional inputs and exact keyword-only flags"
            if signature_ok
            else "refund_cents must use (paid_cents, days_since_delivery, *, final_sale=False, defective=False)"
        )
except (OSError, SyntaxError) as error:
    signature_detail = f"signature inspection failed: {type(error).__name__}: {error}"
results.append(assertion("public-call-contract", signature_ok, signature_detail))


def ignored_cache(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


def allowed_test_artifact(path: Path) -> bool:
    relative = path.relative_to(workspace)
    return (
        len(relative.parts) == 1
        and path.suffix == ".py"
        and path.name.startswith("test_")
    ) or (len(relative.parts) > 1 and relative.parts[0] == "tests")


workspace_entries = [path for path in workspace.rglob("*") if not ignored_cache(path)]
unsupported_entries = sorted(
    path.relative_to(workspace).as_posix()
    for path in workspace_entries
    if path.is_symlink() or (not path.is_dir() and not path.is_file())
)
regular_files = [
    path for path in workspace_entries if not path.is_symlink() and path.is_file()
]
unexpected_files = sorted(
    path.relative_to(workspace).as_posix()
    for path in regular_files
    if path.relative_to(workspace).as_posix() != "returns.py"
    and not allowed_test_artifact(path)
)
reachable_extras = sorted(
    relative
    for relative in loaded_workspace_files - {"returns.py"}
    if not ignored_cache(Path(relative))
)

local_roots = {
    path.stem for path in workspace.iterdir() if path.is_file() and path.suffix == ".py"
}
local_roots.update(path.name for path in workspace.iterdir() if path.is_dir())
local_imports: set[str] = set()
third_party_imports: set[str] = set()
production_classes: list[str] = []
extra_top_level_functions: list[str] = []
top_level_lambdas: list[str] = []
evaluator_introspection: set[str] = set()
if production_tree is None:
    third_party_imports.add("unreadable-source")
else:
    production_classes = sorted(
        node.name
        for node in ast.walk(production_tree)
        if isinstance(node, ast.ClassDef)
    )
    extra_top_level_functions = sorted(
        node.name
        for node in production_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "refund_cents"
    )
    top_level_lambdas = top_level_lambda_bindings(production_tree)
    evaluator_introspection = evaluator_introspection_facts(production_tree)
    for node in ast.walk(production_tree):
        if isinstance(node, ast.Import):
            imports = {alias.name.partition(".")[0] for alias in node.names}
            relative = False
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports = {node.module.partition(".")[0]}
            relative = node.level > 0
        elif isinstance(node, ast.ImportFrom):
            imports = set()
            relative = node.level > 0
        else:
            continue
        if relative:
            local_imports.update(imports or {"relative-import"})
        for imported in imports:
            if imported in local_roots and imported not in {"returns"}:
                local_imports.add(imported)
            elif imported not in sys.stdlib_module_names and imported != "__future__":
                third_party_imports.add(imported)

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
    for path in regular_files
    if path.name in dependency_names or path.name.startswith("requirements")
)
scope_ok = not any(
    (
        unsupported_entries,
        unexpected_files,
        reachable_extras,
        local_imports,
        third_party_imports,
        dependency_files,
        production_classes,
        extra_top_level_functions,
        top_level_lambdas,
        evaluator_introspection,
    )
)
scope_detail = (
    "returns.py remained the only production module and isolated tests stayed unreachable"
    if scope_ok
    else (
        f"unsupported entries={unsupported_entries}, unexpected files={unexpected_files}, "
        f"reachable extras={reachable_extras}, local imports={sorted(local_imports)}, "
        f"third-party imports={sorted(third_party_imports)}, dependency files={dependency_files}, "
        f"production classes={production_classes}, extra functions={extra_top_level_functions}, "
        f"top-level lambdas={top_level_lambdas}, evaluator introspection={sorted(evaluator_introspection)}"
    )
)
results.append(assertion("domain-policy-placement", scope_ok, scope_detail))

print(
    json.dumps(
        {"passed": all(item["passed"] for item in results), "assertions": results}
    )
)
