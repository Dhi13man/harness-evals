"""Deterministic helpers for hidden behavioral eval verifiers."""

from __future__ import annotations

import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

from untrusted_child import run_untrusted


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    sandbox_error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.output_limited
            and self.sandbox_error is None
        )

    def summary(self) -> str:
        if self.sandbox_error is not None:
            status = f"sandbox failure: {self.sandbox_error}"
        elif self.timed_out:
            status = "timed out"
        elif self.output_limited:
            status = "output limit exceeded"
        else:
            status = f"exit {self.returncode}"
        output = (self.stdout + "\n" + self.stderr).strip().replace("\x00", "")
        if len(output) > 900:
            output = output[-900:]
        return f"{status}: {output}" if output else status


def precheck_failure(detail: str) -> CommandResult:
    return CommandResult(125, "", f"candidate test execution blocked: {detail}")


def workspace_from_env() -> Path:
    raw = os.environ.get("EVAL_WORKSPACE")
    if not raw:
        raise RuntimeError("EVAL_WORKSPACE is not set")
    workspace = Path(raw).resolve()
    if not workspace.is_dir():
        raise RuntimeError(f"EVAL_WORKSPACE is not a directory: {workspace}")
    return workspace


def run(command: Sequence[str], cwd: Path, timeout_seconds: int = 30) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="candidate-execution-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(cwd, clone)
        completed = run_untrusted(command, clone, timeout_seconds)
    return CommandResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
        completed.timed_out,
        completed.output_limited,
        completed.sandbox_error,
    )


def run_python_tests(
    workspace: Path,
    protected_paths: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    """Preload production modules, remove their source, then discover tests.

    The preloaded Python objects and interpreter remain in the same process. Source
    removal is therefore a defense-in-depth boundary, not a claim that runtime
    introspection is impossible.
    """

    configuration = json.dumps(
        {"protected": list(protected_paths)}, sort_keys=True, separators=(",", ":")
    )
    wrapper = f"""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

configuration = json.loads({configuration!r})
root = Path.cwd()
for relative in configuration["protected"]:
    source = root / relative
    name = source.stem
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot preload protected module {{relative}}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
for relative in configuration["protected"]:
    (root / relative).unlink()
suite = unittest.defaultTestLoader.discover(str(root), pattern="test*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""
    return run([sys.executable, "-c", wrapper], workspace, timeout_seconds)


def run_go_tests(
    workspace: Path,
    *,
    race: bool = False,
    count: int = 1,
    timeout_seconds: int = 30,
) -> CommandResult:
    """Compile candidate tests, remove module/source files, then run the binary."""

    if count < 1:
        raise ValueError("Go test count must be positive")
    with tempfile.TemporaryDirectory(prefix="compiled-go-tests-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        binary = clone / "zz_eval_candidate.test"
        go, environment = _private_go_environment(Path(raw_tmp), race=race)
        command = [
            go,
            "test",
            "-mod=readonly",
            "-trimpath",
            "-buildvcs=false",
        ]
        if race:
            command.append("-race")
        command.extend(["-c", "-o", str(binary), "."])
        try:
            compiled = subprocess.run(
                command,
                cwd=clone,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                124,
                error.stdout or "",
                error.stderr or "",
                timed_out=True,
            )
        if compiled.returncode != 0 or not binary.is_file():
            return CommandResult(
                compiled.returncode,
                compiled.stdout,
                compiled.stderr,
            )
        binary.chmod(0o500)
        for path in sorted(clone.rglob("*"), reverse=True):
            if path == binary:
                continue
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        return run(
            [f"./{binary.name}", "-test.v", f"-test.count={count}"],
            clone,
            timeout_seconds,
        )


def _private_go_environment(root: Path, *, race: bool) -> tuple[str, dict[str, str]]:
    declared_tool_bin = os.environ.get("EVAL_TOOL_BIN")
    if declared_tool_bin:
        tool_bin = Path(declared_tool_bin).resolve(strict=True)
        go_path = tool_bin / "go"
        if not tool_bin.is_dir() or not go_path.is_file():
            raise RuntimeError("declared Go toolchain is unavailable")
        go = str(go_path)
        path = str(tool_bin)
    else:
        raw_go = shutil.which("go")
        if raw_go is None:
            raise RuntimeError("Go toolchain is unavailable")
        go = str(Path(raw_go).resolve(strict=True))
        path = ":".join(
            dict.fromkeys(
                (
                    str(Path(go).parent),
                    "/usr/local/sbin",
                    "/usr/local/bin",
                    "/usr/sbin",
                    "/usr/bin",
                    "/sbin",
                    "/bin",
                )
            )
        )
    directories = {
        "HOME": root / "go-home",
        "TMPDIR": root / "go-tmp",
        "GOCACHE": root / "go-cache",
        "GOMODCACHE": root / "go-mod-cache",
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    go_root = os.environ.get("EVAL_GO_ROOT")
    if not go_root:
        resolved = subprocess.run(
            [go, "env", "GOROOT"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
            env={
                "GOENV": "off",
                "GOTOOLCHAIN": "local",
                "HOME": str(directories["HOME"]),
                "PATH": path,
            },
        )
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise RuntimeError("could not resolve the selected Go toolchain root")
        go_root = resolved.stdout.strip()
    resolved_go_root = Path(go_root).resolve(strict=True)
    if not resolved_go_root.is_dir():
        raise RuntimeError("selected Go toolchain root is not a directory")
    environment = {key: str(value) for key, value in directories.items()}
    environment.update(
        {
            "PATH": path,
            "GOROOT": str(resolved_go_root),
            "GOENV": "off",
            "GOWORK": "off",
            "GOTOOLCHAIN": "local",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "1" if race else "0",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
    )
    gcc_prefix = os.environ.get("EVAL_GCC_EXEC_PREFIX")
    if race and declared_tool_bin:
        if not gcc_prefix:
            raise RuntimeError("race build requires EVAL_GCC_EXEC_PREFIX")
        prefix = Path(gcc_prefix).resolve(strict=True)
        if not prefix.is_dir():
            raise RuntimeError("GCC runtime prefix is not a directory")
        environment["COMPILER_PATH"] = declared_tool_bin
        environment["GCC_EXEC_PREFIX"] = str(prefix) + os.sep
    return go, environment


def run_node_tests(
    workspace: Path,
    protected_paths: Sequence[str],
    test_paths: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    """Preload CommonJS exports, unlink source, restrict loaders, and load tests.

    Preloaded functions still execute in the Node runtime. The loader and source
    coercion restrictions are defense in depth around that trusted runtime.
    """

    configuration = json.dumps(
        {"protected": list(protected_paths), "tests": list(test_paths)},
        sort_keys=True,
        separators=(",", ":"),
    )
    wrapper = f"""
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
require("node:test");
require("node:assert/strict");
const configuration = JSON.parse({json.dumps(configuration)});
const root = process.cwd();
const originalLoad = Module._load;
const protectedExports = new Map();
const allowedBuiltins = new Set(["node:test", "node:assert/strict"]);
for (const relative of configuration.protected) {{
  const absolute = path.resolve(root, relative);
  const exported = originalLoad(absolute, module, false);
  protectedExports.set(absolute, exported);
  if (absolute.endsWith(".js")) protectedExports.set(absolute.slice(0, -3), exported);
}}
Module._load = function guardedLoad(request, parent, isMain) {{
  if (typeof request === "string" && (request.startsWith(".") || path.isAbsolute(request))) {{
    const parentDirectory = parent && parent.filename ? path.dirname(parent.filename) : root;
    const absolute = path.resolve(parentDirectory, request);
    if (protectedExports.has(absolute)) return protectedExports.get(absolute);
    if (protectedExports.has(absolute + ".js")) return protectedExports.get(absolute + ".js");
  }}
  if (allowedBuiltins.has(request)) return originalLoad(request, parent, isMain);
  throw new Error(`test dependency is not allowed: ${{String(request)}}`);
}};
for (const relative of configuration.protected) fs.unlinkSync(path.resolve(root, relative));
Object.defineProperty(Function.prototype, "toString", {{
  value() {{ return "function () {{ [source unavailable] }}"; }},
  writable: false,
  configurable: false,
}});
for (const property of ["getBuiltinModule", "mainModule"]) {{
  Object.defineProperty(process, property, {{
    value: undefined,
    writable: false,
    configurable: false,
  }});
}}
Object.defineProperty(globalThis, "process", {{
  value: undefined,
  writable: false,
  configurable: false,
}});
for (const relative of configuration.tests) originalLoad(path.resolve(root, relative), module, false);
"""
    loader = (
        "export async function resolve(specifier) {"
        "throw new Error(`ESM module loading is disabled: ${specifier}`);"
        "}"
    )
    return run(
        [
            "node",
            "--disable-sigusr1",
            "--disallow-code-generation-from-strings",
            "--no-addons",
            "--no-warnings",
            f"--experimental-loader=data:text/javascript,{quote(loader)}",
            "-e",
            wrapper,
        ],
        workspace,
        timeout_seconds,
    )


def tests_only_changes(
    workspace: Path, fixture: Path, language: str
) -> tuple[bool, str]:
    """Require every workspace difference to be a discoverable test source."""

    if language not in {"python", "go", "javascript"}:
        raise ValueError(f"unsupported source language: {language}")

    fixture_files, fixture_errors = _regular_workspace_files(fixture)
    candidate_files, candidate_errors = _regular_workspace_files(workspace)
    violations = [*fixture_errors, *candidate_errors]
    for relative in sorted(set(fixture_files) | set(candidate_files)):
        path = Path(relative)
        if _is_test_source(path, language):
            continue
        if relative not in fixture_files:
            violations.append(f"added non-test file {relative}")
        elif relative not in candidate_files:
            violations.append(f"removed non-test file {relative}")
        elif candidate_files[relative] != fixture_files[relative]:
            violations.append(f"changed non-test file {relative}")
    if violations:
        return False, "; ".join(violations)
    return True, f"only discoverable {language} test sources differ from the fixture"


def _is_test_source(path: Path, language: str) -> bool:
    forbidden_directories = {
        ".venv",
        "node_modules",
        "site-packages",
        "vendor",
        "venv",
    }
    if any(part in forbidden_directories for part in path.parts[:-1]):
        return False
    if language == "python":
        return path.suffix == ".py" and path.name.startswith("test")
    if language == "go":
        return path.name.endswith("_test.go")
    return path.name.endswith(".test.js")


def _regular_workspace_files(root: Path) -> tuple[dict[str, bytes], list[str]]:
    files: dict[str, bytes] = {}
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if "__pycache__" in relative_path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = relative_path.as_posix()
        if path.is_symlink():
            errors.append(f"unsupported symbolic link {relative}")
        elif path.is_dir():
            continue
        elif not path.is_file():
            errors.append(f"unsupported workspace entry {relative}")
        else:
            files[relative] = path.read_bytes()
    return files, errors


def source_guard(
    workspace: Path,
    relative_paths: Iterable[str],
    language: str,
    protected_paths: Iterable[str],
) -> tuple[bool, str]:
    sources: list[tuple[str, str]] = []
    missing: list[str] = []
    for relative in relative_paths:
        path = workspace / relative
        if not path.is_file():
            missing.append(relative)
            continue
        sources.append((relative, path.read_text(encoding="utf-8")))
    if missing:
        return False, "missing test files: " + ", ".join(missing)
    protected = {Path(relative).name for relative in protected_paths}
    if language == "python":
        violations = _python_source_violations(sources, protected)
        violations.extend(_python_dependency_violations(sources, protected))
    elif language == "go":
        violations = _go_source_violations(workspace, sources, protected)
    elif language == "javascript":
        violations = _javascript_source_violations(sources, protected)
    else:
        raise ValueError(f"unsupported source language: {language}")
    if violations:
        return False, "test source violates isolation rules: " + "; ".join(violations)
    return (
        True,
        "test source uses only allowed dependencies and no detected prohibited "
        "introspection; preloaded or compiled runtime objects remain trusted",
    )


def candidate_sources(
    workspace: Path, language: str, protected_paths: Iterable[str]
) -> list[str]:
    suffix = {"python": ".py", "go": ".go", "javascript": ".js"}[language]
    protected = {Path(relative).as_posix() for relative in protected_paths}
    discovered = [
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob(f"*{suffix}")
        if path.is_file()
        and path.relative_to(workspace).as_posix() not in protected
        and "__pycache__" not in path.parts
    ]
    return sorted(discovered)


class _PythonSourceVisitor(ast.NodeVisitor):
    _FORBIDDEN_IMPORT_ROOTS = {
        "ast",
        "builtins",
        "ctypes",
        "dis",
        "fileinput",
        "gc",
        "importlib",
        "inspect",
        "io",
        "linecache",
        "marshal",
        "operator",
        "os",
        "pickle",
        "runpy",
        "subprocess",
        "sys",
        "zipimport",
    }
    _FORBIDDEN_ATTRIBUTES = {
        "__bases__",
        "__class__",
        "__closure__",
        "__code__",
        "__dict__",
        "__file__",
        "__func__",
        "__getattribute__",
        "__globals__",
        "__loader__",
        "__mro__",
        "__spec__",
        "__subclasses__",
        "__traceback__",
        "_getframe",
        "co_argcount",
        "co_cellvars",
        "co_code",
        "co_consts",
        "co_exceptiontable",
        "co_filename",
        "co_firstlineno",
        "co_flags",
        "co_freevars",
        "co_kwonlyargcount",
        "co_lines",
        "co_linetable",
        "co_name",
        "co_names",
        "co_nlocals",
        "co_positions",
        "co_posonlyargcount",
        "co_qualname",
        "co_stacksize",
        "co_varnames",
        "cr_code",
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_lasti",
        "f_lineno",
        "f_locals",
        "f_trace",
        "gi_code",
        "modules",
        "orig_argv",
        "tb_frame",
        "tb_lasti",
        "tb_lineno",
        "tb_next",
    }
    _FILE_CALLS = {
        "builtins.open",
        "io.FileIO",
        "io.open",
        "os.fdopen",
        "os.lstat",
        "os.open",
        "os.stat",
    }
    _PATH_METHODS = {"lstat", "open", "read_bytes", "read_text", "stat"}
    _INTROSPECTION_CALLS = {
        "builtins.compile",
        "builtins.eval",
        "builtins.exec",
        "builtins.getattr",
        "builtins.globals",
        "builtins.hasattr",
        "builtins.locals",
        "builtins.setattr",
        "builtins.vars",
        "builtins.__import__",
        "dis.dis",
        "importlib.import_module",
        "inspect.getfile",
        "inspect.getsource",
        "inspect.getsourcefile",
        "linecache.getline",
        "os.popen",
        "os.system",
        "runpy.run_module",
        "runpy.run_path",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.run",
    }

    def __init__(self, filename: str, protected: set[str]) -> None:
        self.filename = filename
        self.protected = protected
        self.protected_stems = {Path(name).stem for name in protected}
        self.imports: dict[str, str] = {
            "compile": "builtins.compile",
            "eval": "builtins.eval",
            "exec": "builtins.exec",
            "getattr": "builtins.getattr",
            "globals": "builtins.globals",
            "hasattr": "builtins.hasattr",
            "locals": "builtins.locals",
            "setattr": "builtins.setattr",
            "vars": "builtins.vars",
            "open": "builtins.open",
            "__import__": "builtins.__import__",
        }
        self.strings: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        self.callables: dict[str, tuple[str, str | None]] = {}
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.partition(".")[0] in self._FORBIDDEN_IMPORT_ROOTS:
                self._record(node, f"imports unsafe introspection module {alias.name}")
            if alias.asname and alias.asname.startswith("_"):
                self._record(
                    node, f"imports module through private alias {alias.asname}"
                )
            self.imports[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            if node.module.partition(".")[0] in self._FORBIDDEN_IMPORT_ROOTS:
                self._record(node, f"imports unsafe introspection module {node.module}")
            for alias in node.names:
                if (
                    alias.name.startswith("_")
                    or (alias.asname is not None and alias.asname.startswith("_"))
                    or (node.module == "unittest" and alias.name == "mock")
                ):
                    self._record(
                        node,
                        f"imports unsafe re-export {node.module}.{alias.name}",
                    )
                self.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            string = self._string_value(node.value)
            if string is not None:
                self.strings[target.id] = string
            path = self._path_value(node.value)
            if path is not None:
                self.paths[target.id] = path
            callable_target = self._callable_target(node.value)
            if callable_target is not None:
                self.callables[target.id] = callable_target
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            string = self._string_value(node.value)
            if string is not None:
                self.strings[node.target.id] = string
            path = self._path_value(node.value)
            if path is not None:
                self.paths[node.target.id] = path
            callable_target = self._callable_target(node.value)
            if callable_target is not None:
                self.callables[node.target.id] = callable_target
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        private_or_runtime = node.attr.startswith("_") or node.attr.startswith(
            ("ag_", "co_", "cr_", "f_", "gi_", "tb_")
        )
        if node.attr in self._FORBIDDEN_ATTRIBUTES or private_or_runtime:
            self._record(node, f"reads forbidden reflection attribute {node.attr}")
        self.generic_visit(node)

    def visit_MatchClass(self, node: ast.MatchClass) -> None:
        self._record(node, "uses implicit attribute access in a class pattern")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        attribute = self._string_value(node.slice)
        if attribute and self._forbidden_reflection_string(attribute):
            self._record(node, f"uses computed reflection key {attribute}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified(node.func)
        operation = qualified or "call"
        if qualified == "builtins.getattr":
            self._inspect_getattr(node)
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in self._PATH_METHODS
        ):
            operation = f"Path.{node.func.attr}"
            self._record(node, f"calls {operation}")
        elif qualified in self._FILE_CALLS:
            self._record(node, f"calls {qualified}")
        elif qualified in self._INTROSPECTION_CALLS:
            self._record(node, f"calls {qualified}")
        elif isinstance(node.func, ast.Name) and node.func.id in self.callables:
            operation, _target = self.callables[node.func.id]
            if operation in self._FILE_CALLS or operation in self._INTROSPECTION_CALLS:
                self._record(node, f"calls {operation} through an alias")
            elif operation.startswith("Path."):
                self._record(node, f"calls {operation} through an alias")
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            value = self._string_value(argument)
            if value and self._forbidden_reflection_string(value):
                self._record(node, f"passes computed reflection string {value}")
        self.generic_visit(node)

    def _forbidden_reflection_string(self, value: str) -> bool:
        return (
            value in self._FORBIDDEN_ATTRIBUTES
            or value.startswith("_")
            or value.startswith(("ag_", "co_", "cr_", "f_", "gi_", "tb_"))
            or value in {"builtins.getattr", "unittest.mock"}
        )

    def _inspect_getattr(self, node: ast.Call) -> None:
        if len(node.args) < 2:
            self._record(node, "calls builtins.getattr")
            return
        attribute = self._string_value(node.args[1])
        if attribute in {"__code__", "__file__"}:
            qualified = self._qualified(node.args[0]) or ""
            if qualified.split(".")[0] in self.protected_stems:
                self._record(
                    node,
                    f"gets {attribute} from production object {qualified}",
                )
        if attribute in self._PATH_METHODS:
            target = self._path_value(node.args[0])
            if target is not None and Path(target).name in self.protected:
                self._record(
                    node,
                    f"gets Path.{attribute} for {Path(target).name}",
                )
        self._record(node, "calls builtins.getattr")

    def _callable_target(self, node: ast.AST) -> tuple[str, str | None] | None:
        qualified = self._qualified(node)
        if qualified in self._FILE_CALLS or qualified in self._INTROSPECTION_CALLS:
            return qualified, None
        if isinstance(node, ast.Attribute) and node.attr in self._PATH_METHODS:
            return f"Path.{node.attr}", self._path_value(node.value)
        return None

    def _path_value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.paths.get(node.id) or self.strings.get(node.id)
        if isinstance(node, ast.Call) and self._qualified(node.func) in {
            "pathlib.Path",
            "pathlib.PurePath",
        }:
            return self._string_value(node.args[0]) if node.args else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self._path_value(node.left)
            right = self._string_value(node.right)
            if left is not None and right is not None:
                return str(Path(left) / right)
        return self._string_value(node)

    def _string_value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.strings.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._string_value(node.left)
            right = self._string_value(node.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(node, ast.Call) and self._qualified(node.func) in {
            "os.path.join",
            "posixpath.join",
        }:
            parts = [self._string_value(argument) for argument in node.args]
            if all(part is not None for part in parts):
                return os.path.join(*(part for part in parts if part is not None))
        return None

    def _qualified(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._qualified(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def _record(self, node: ast.AST, detail: str) -> None:
        self.violations.append(f"{self.filename}:{node.lineno}: {detail}")


def _python_source_violations(
    sources: Sequence[tuple[str, str]], protected: set[str]
) -> list[str]:
    violations: list[str] = []
    for filename, source in sources:
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as error:
            violations.append(f"{filename}:{error.lineno or 1}: invalid Python syntax")
            continue
        visitor = _PythonSourceVisitor(filename, protected)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


def _python_dependency_violations(
    sources: Sequence[tuple[str, str]], protected: set[str]
) -> list[str]:
    allowed = {"pathlib", "sqlite3", "tempfile", "unittest"}
    allowed.update(Path(name).stem for name in protected)
    allowed.update(Path(filename).stem for filename, _source in sources)
    violations: list[str] = []
    for filename, source in sources:
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError:
            continue
        imports: dict[str, str] = {"__import__": "builtins.__import__"}
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name for alias in node.names]
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".", 1)[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module]
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            for root in roots:
                if root not in allowed:
                    violations.append(
                        f"{filename}:{node.lineno}: imports non-stdlib module {root}"
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified_python_name(node.func, imports)
            if qualified not in {"builtins.__import__", "importlib.import_module"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                violations.append(
                    f"{filename}:{node.lineno}: uses non-static dynamic import"
                )
                continue
            module = node.args[0].value
            root = module if isinstance(module, str) else ""
            if root not in allowed:
                violations.append(
                    f"{filename}:{node.lineno}: dynamically imports non-stdlib module {root or '<unknown>'}"
                )
    return violations


def python_uses_sqlite(
    workspace: Path, relative_paths: Iterable[str]
) -> tuple[bool, str]:
    uses: list[str] = []
    for relative in relative_paths:
        path = workspace / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError:
            continue
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        for node in _reachable_python_calls(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified_python_name(node.func, imports)
            if qualified in {"sqlite3.Connection", "sqlite3.connect"}:
                uses.append(f"{relative}:{node.lineno}: {qualified}")
    if not uses:
        return False, "no sqlite3.Connection or sqlite3.connect call found"
    return True, "real SQLite connection constructed at " + ", ".join(uses)


class _ReachableCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        condition = _constant_truth(node.test)
        if condition is True:
            for statement in node.body:
                self.visit(statement)
            return
        if condition is False:
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        condition = _constant_truth(node.test)
        if condition is False:
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)


def _constant_truth(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _constant_truth(node.operand)
        return None if value is None else not value
    return None


def _reachable_python_calls(tree: ast.AST) -> list[ast.Call]:
    visitor = _ReachableCallVisitor()
    visitor.visit(tree)
    return visitor.calls


def _qualified_python_name(node: ast.AST, imports: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_python_name(node.value, imports)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _go_source_violations(
    workspace: Path,
    sources: Sequence[tuple[str, str]],
    protected: set[str],
) -> list[str]:
    payload = {
        "files": [str((workspace / relative).resolve()) for relative, _ in sources],
        "protected": sorted(protected),
    }
    try:
        analyzer = _compiled_go_source_guard()
        completed = subprocess.run(
            [analyzer],
            env={},
            input=json.dumps(payload),
            capture_output=True,
            check=False,
            text=True,
            timeout=GO_SOURCE_GUARD_EXECUTION_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return [f"Go source analysis failed: {error}"]
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return [f"Go source analysis failed: {detail or completed.returncode}"]
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ["Go source analysis returned invalid JSON"]
    return [str(item) for item in result.get("violations", [])]


GO_SOURCE_GUARD_BUILD_TIMEOUT_SECONDS = 90
GO_SOURCE_GUARD_EXECUTION_TIMEOUT_SECONDS = 20
_GO_SOURCE_GUARD_BINARY: str | None = None


def _compiled_go_source_guard() -> str:
    global _GO_SOURCE_GUARD_BINARY
    if _GO_SOURCE_GUARD_BINARY is not None:
        return _GO_SOURCE_GUARD_BINARY

    root = Path(tempfile.mkdtemp(prefix="go-source-guard-"))
    try:
        go, environment = _private_go_environment(root, race=False)
        binary = root / "source-guard"
        analyzer = Path(__file__).with_name("go_source_guard.go")
        completed = subprocess.run(
            [
                go,
                "build",
                "-trimpath",
                "-buildvcs=false",
                "-o",
                str(binary),
                str(analyzer),
            ],
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=GO_SOURCE_GUARD_BUILD_TIMEOUT_SECONDS,
            shell=False,
        )
        if completed.returncode != 0 or not binary.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"could not compile the pinned Go source guard: {detail}"
            )
        binary.chmod(0o500)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    _GO_SOURCE_GUARD_BINARY = str(binary)
    return _GO_SOURCE_GUARD_BINARY


@dataclass(frozen=True)
class _JavaScriptToken:
    kind: str
    value: str
    line: int


_FORBIDDEN_JAVASCRIPT_MODULES = {
    "child_process",
    "fs",
    "fs/promises",
    "inspector",
    "module",
    "node:child_process",
    "node:fs",
    "node:fs/promises",
    "node:inspector",
    "node:module",
    "node:vm",
    "vm",
}

_FORBIDDEN_JAVASCRIPT_IDENTIFIERS = {
    "Function",
    "Proxy",
    "Reflect",
    "String",
    "_load",
    "constructor",
    "createRequire",
    "eval",
    "execArgv",
    "getBuiltinModule",
    "global",
    "globalThis",
    "import",
    "mainModule",
    "module",
    "process",
    "readFile",
    "readFileSync",
    "toString",
}


def _javascript_source_violations(
    sources: Sequence[tuple[str, str]], protected: set[str]
) -> list[str]:
    violations: list[str] = []
    allowed_modules = {"node:assert/strict", "node:test"}
    allowed_modules.update(
        f"./{Path(name).stem}" for name in protected if name.endswith(".js")
    )
    allowed_modules.update(f"./{name}" for name in protected if name.endswith(".js"))
    for filename, source in sources:
        tokens = _javascript_tokens(source)
        for index, token in enumerate(tokens):
            if (
                token.kind == "punctuation"
                and token.value == "\\"
                and index + 1 < len(tokens)
                and tokens[index + 1].kind == "identifier"
                and tokens[index + 1].value.startswith("u")
            ):
                violations.append(
                    f"{filename}:{token.line}: uses escaped identifier syntax"
                )
            commonjs_export = (
                token.value == "module"
                and index + 2 < len(tokens)
                and tokens[index + 1].value == "."
                and tokens[index + 2].value == "exports"
            )
            if (
                token.kind == "identifier"
                and token.value in _FORBIDDEN_JAVASCRIPT_IDENTIFIERS
                and not commonjs_export
            ):
                violations.append(
                    f"{filename}:{token.line}: uses forbidden runtime introspection "
                    f"primitive {token.value}"
                )
            if (
                token.kind == "identifier"
                and token.value == "cache"
                and index >= 2
                and tokens[index - 1].value == "."
                and tokens[index - 2].value == "require"
            ):
                violations.append(
                    f"{filename}:{token.line}: inspects the CommonJS module cache"
                )
        constants: dict[str, str] = {}
        loaders = {"require"}
        assignments = _javascript_assignments(tokens)
        for _ in range(len(assignments) + 1):
            changed = False
            for name, start in assignments:
                value, _ = _javascript_constant(tokens, start, constants)
                if value is not None and constants.get(name) != value:
                    constants[name] = value
                    changed = True
                if _javascript_loader_expression(tokens, start, loaders):
                    if name not in loaders:
                        loaders.add(name)
                        changed = True
            if not changed:
                break

        literal_findings: set[tuple[int, str]] = set()
        for index, token in enumerate(tokens):
            if token.kind != "string" and token.value not in constants:
                continue
            module, _ = _javascript_constant(tokens, index, constants)
            if module in _FORBIDDEN_JAVASCRIPT_MODULES:
                literal_findings.add((token.line, module))
        for line, module in sorted(literal_findings):
            violations.append(
                f"{filename}:{line}: contains source-inspection module {module}"
            )

        for index, token in enumerate(tokens):
            if token.kind != "identifier":
                continue
            is_loader = token.value in loaders or (
                token.value == "getBuiltinModule"
                and index > 1
                and tokens[index - 1].value == "."
                and tokens[index - 2].value == "process"
            )
            if not is_loader:
                continue
            module = _javascript_call_constant(tokens, index + 1, constants)
            if module in _FORBIDDEN_JAVASCRIPT_MODULES:
                violations.append(
                    f"{filename}:{token.line}: loads source-inspection module {module}"
                )
            elif module is None:
                violations.append(
                    f"{filename}:{token.line}: uses a non-static module dependency"
                )
            elif module not in allowed_modules:
                violations.append(
                    f"{filename}:{token.line}: loads dependency outside the allowlist {module}"
                )
    return violations


def _javascript_assignments(
    tokens: Sequence[_JavaScriptToken],
) -> list[tuple[str, int]]:
    assignments: list[tuple[str, int]] = []
    for index, token in enumerate(tokens):
        if token.value in {"const", "let", "var"}:
            name_index = index + 1
        elif token.kind == "identifier" and (
            index == 0 or tokens[index - 1].value != "."
        ):
            name_index = index
        else:
            continue
        if (
            name_index + 1 < len(tokens)
            and tokens[name_index].kind == "identifier"
            and tokens[name_index + 1].value == "="
        ):
            assignments.append((tokens[name_index].value, name_index + 2))
    return assignments


def _javascript_loader_expression(
    tokens: Sequence[_JavaScriptToken], start: int, loaders: set[str]
) -> bool:
    if start >= len(tokens):
        return False
    if (
        tokens[start].kind == "identifier"
        and tokens[start].value in loaders
        and (start + 1 >= len(tokens) or tokens[start + 1].value != "(")
    ):
        return True
    return (
        start + 2 < len(tokens)
        and tokens[start].value == "process"
        and tokens[start + 1].value == "."
        and tokens[start + 2].value == "getBuiltinModule"
    )


def _javascript_call_constant(
    tokens: Sequence[_JavaScriptToken],
    start: int,
    constants: dict[str, str],
) -> str | None:
    if start >= len(tokens) or tokens[start].value != "(":
        return None
    value, end = _javascript_constant(tokens, start + 1, constants)
    if value is None or end >= len(tokens) or tokens[end].value not in {",", ")"}:
        return None
    return value


def _javascript_constant(
    tokens: Sequence[_JavaScriptToken],
    start: int,
    constants: dict[str, str],
) -> tuple[str | None, int]:
    parts: list[str] = []
    index = start
    expect_value = True
    while index < len(tokens):
        token = tokens[index]
        if expect_value:
            if token.kind == "string":
                parts.append(token.value)
            elif token.kind == "identifier" and token.value in constants:
                parts.append(constants[token.value])
            else:
                break
            expect_value = False
        elif token.value == "+":
            expect_value = True
        else:
            break
        index += 1
    if not parts or expect_value:
        return None, start
    return "".join(parts), index


def _javascript_tokens(source: str) -> list[_JavaScriptToken]:
    tokens: list[_JavaScriptToken] = []
    index = 0
    line = 1
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if current.isspace():
            line += current == "\n"
            index += 1
            continue
        if current == "/" and following == "/":
            newline = source.find("\n", index + 2)
            if newline < 0:
                break
            index = newline
            continue
        if current == "/" and following == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                break
            line += source.count("\n", index, end + 2)
            index = end + 2
            continue
        if current in {'"', "'", "`"}:
            quote = current
            start_line = line
            index += 1
            value: list[str] = []
            while index < len(source):
                current = source[index]
                if current == "\\" and index + 1 < len(source):
                    escaped = source[index + 1]
                    value.append(
                        {"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped)
                    )
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                line += current == "\n"
                value.append(current)
                index += 1
            tokens.append(_JavaScriptToken("string", "".join(value), start_line))
            continue
        if current.isalpha() or current in {"_", "$"}:
            start = index
            while index < len(source) and (
                source[index].isalnum() or source[index] in {"_", "$"}
            ):
                index += 1
            tokens.append(_JavaScriptToken("identifier", source[start:index], line))
            continue
        tokens.append(_JavaScriptToken("punctuation", current, line))
        index += 1
    return tokens


def run_variant(
    workspace: Path,
    target_relative: str,
    replacement: Path,
    command: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        target = clone / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(replacement, target)
        return run(command, clone, timeout_seconds)


def run_transform(
    workspace: Path,
    target_relative: str,
    replacements: Sequence[tuple[str, str]],
    command: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        target = clone / target_relative
        source = target.read_text(encoding="utf-8")
        for old, new in replacements:
            count = source.count(old)
            if count != 1:
                return CommandResult(
                    125,
                    "",
                    f"transform expected one occurrence, found {count}: {old!r}",
                )
            source = source.replace(old, new, 1)
        target.write_text(source, encoding="utf-8")
        return run(command, clone, timeout_seconds)


def run_python_variant(
    workspace: Path,
    target_relative: str,
    replacement: Path,
    protected_paths: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-python-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        shutil.copyfile(replacement, clone / target_relative)
        return run_python_tests(clone, protected_paths, timeout_seconds)


def run_python_transform(
    workspace: Path,
    target_relative: str,
    replacements: Sequence[tuple[str, str]],
    protected_paths: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-python-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        error = _apply_text_replacements(clone / target_relative, replacements)
        if error is not None:
            return error
        return run_python_tests(clone, protected_paths, timeout_seconds)


def run_go_variant(
    workspace: Path,
    target_relative: str,
    replacement: Path,
    *,
    race: bool = False,
    count: int = 1,
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-go-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        shutil.copyfile(replacement, clone / target_relative)
        return run_go_tests(
            clone, race=race, count=count, timeout_seconds=timeout_seconds
        )


def run_go_transform(
    workspace: Path,
    target_relative: str,
    replacements: Sequence[tuple[str, str]],
    *,
    race: bool = False,
    count: int = 1,
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-go-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        error = _apply_text_replacements(clone / target_relative, replacements)
        if error is not None:
            return error
        return run_go_tests(
            clone, race=race, count=count, timeout_seconds=timeout_seconds
        )


def run_node_transform(
    workspace: Path,
    target_relative: str,
    replacements: Sequence[tuple[str, str]],
    protected_paths: Sequence[str],
    test_paths: Sequence[str],
    timeout_seconds: int = 30,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="skill-eval-node-") as raw_tmp:
        clone = Path(raw_tmp) / "workspace"
        shutil.copytree(workspace, clone)
        error = _apply_text_replacements(clone / target_relative, replacements)
        if error is not None:
            return error
        return run_node_tests(clone, protected_paths, test_paths, timeout_seconds)


def _apply_text_replacements(
    target: Path, replacements: Sequence[tuple[str, str]]
) -> CommandResult | None:
    source = target.read_text(encoding="utf-8")
    for old, new in replacements:
        count = source.count(old)
        if count != 1:
            return CommandResult(
                125,
                "",
                f"transform expected one occurrence, found {count}: {old!r}",
            )
        source = source.replace(old, new, 1)
    target.write_text(source, encoding="utf-8")
    return None


def assertion_failure(language: str, result: CommandResult) -> bool:
    if (
        result.passed
        or result.timed_out
        or result.output_limited
        or result.sandbox_error is not None
    ):
        return False
    output = result.stdout + "\n" + result.stderr
    if language == "python":
        return (
            "Ran " in output
            and "FAILED (" in output
            and "SyntaxError" not in output
            and "ImportError" not in output
            and "ModuleNotFoundError" not in output
        )
    if language == "go":
        return (
            "--- FAIL:" in output
            and "[build failed]" not in output
            and "undefined:" not in output
        )
    if language == "javascript":
        return (
            "not ok" in output
            and "# fail" in output
            and "SyntaxError" not in output
            and "MODULE_NOT_FOUND" not in output
        )
    raise ValueError(f"unsupported result language: {language}")


def check(assertion_id: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": assertion_id, "passed": bool(passed), "evidence": evidence}


def emit(
    assertions: list[dict[str, object]], metrics: dict[str, object] | None = None
) -> None:
    payload: dict[str, object] = {
        "passed": all(bool(item["passed"]) for item in assertions),
        "assertions": assertions,
    }
    if metrics is not None:
        payload["metrics"] = metrics
    print(json.dumps(payload, sort_keys=True))
