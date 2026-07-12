import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SHARED = Path(__file__).resolve().parents[1] / "_shared"
SUITE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SHARED))

import verifier_lib  # noqa: E402
from verifier_lib import (  # noqa: E402
    CommandResult,
    _private_go_environment,
    assertion_failure,
    python_uses_sqlite,
    run_go_tests,
    run_node_tests,
    run_python_tests,
    source_guard,
    tests_only_changes,
)


class SourceGuardTests(unittest.TestCase):
    @staticmethod
    def run_locally(
        command: list[str] | tuple[str, ...], cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def check_source(
        self,
        filename: str,
        source: str,
        language: str,
        protected: list[str],
    ) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / filename).write_text(source, encoding="utf-8")
            return source_guard(workspace, [filename], language, protected)

    def test_go_source_guard_build_timeout_has_cold_start_margin(self):
        self.assertEqual(verifier_lib.GO_SOURCE_GUARD_BUILD_TIMEOUT_SECONDS, 90)

    def test_go_oracle_child_timeouts_have_cold_compile_margin(self):
        expected = {
            "parser-boundaries": [90, 90, 90],
            "concurrency-flake": [90, 90, 90],
        }
        for case, expected_timeouts in expected.items():
            with self.subTest(case=case):
                source = (
                    SUITE_ROOT / f"cases/testing/{case}/oracle/verify.py"
                ).read_text(encoding="utf-8")
                tree = ast.parse(source)
                observed = sorted(
                    keyword.value.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id
                    in {"run_go_tests", "run_go_transform", "run_go_variant"}
                    for keyword in node.keywords
                    if keyword.arg == "timeout_seconds"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, int)
                )
                self.assertEqual(observed, expected_timeouts)

    def test_testing_suite_outer_timeouts_cover_sequential_children(self):
        suite = json.loads((SUITE_ROOT / "suite.json").read_text(encoding="utf-8"))
        actual = {
            case["id"]: case["verifier"]["timeout_seconds"]
            for case in suite["cases"]
            if case["skill"] == "testing"
        }
        sequential_runs = {}
        for case_id in actual:
            case = case_id.removeprefix("testing-")
            source = (SUITE_ROOT / f"cases/testing/{case}/oracle/verify.py").read_text(
                encoding="utf-8"
            )
            tree = ast.parse(source)
            list_lengths = {}
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in {"valid_variants", "mutations"}
                    and isinstance(node.value, ast.List)
                ):
                    list_lengths[node.targets[0].id] = len(node.value.elts)
            self.assertEqual(set(list_lengths), {"valid_variants", "mutations"})
            sequential_runs[case_id] = 1 + sum(list_lengths.values())
        source_guard_budget = (
            verifier_lib.GO_SOURCE_GUARD_BUILD_TIMEOUT_SECONDS
            + verifier_lib.GO_SOURCE_GUARD_EXECUTION_TIMEOUT_SECONDS
        )
        expected = {
            "testing-oracle-sensitivity": sequential_runs["testing-oracle-sensitivity"]
            * 20
            + 40,
            "testing-parser-boundaries": source_guard_budget
            + sequential_runs["testing-parser-boundaries"] * (90 + 90)
            + 130,
            "testing-state-machine-sequences": sequential_runs[
                "testing-state-machine-sequences"
            ]
            * 20
            + 40,
            "testing-real-boundary-fidelity": sequential_runs[
                "testing-real-boundary-fidelity"
            ]
            * 25
            + 45,
            "testing-concurrency-flake": source_guard_budget
            + sequential_runs["testing-concurrency-flake"] * (90 + 90)
            + 130,
            "testing-legacy-characterization": sequential_runs[
                "testing-legacy-characterization"
            ]
            * 20
            + 40,
            "testing-event-idempotency": sequential_runs["testing-event-idempotency"]
            * 25
            + 45,
        }
        self.assertEqual(actual, expected)

    def test_python_rejects_production_stat_but_allows_plain_assertions(self):
        rejected, detail = self.check_source(
            "test_total.py",
            'from pathlib import Path\nPath("total.py").stat()\n',
            "python",
            ["total.py"],
        )
        self.assertFalse(rejected)
        self.assertIn("Path.stat", detail)

        accepted, detail = self.check_source(
            "test_total.py",
            "def test_total():\n    assert total() == 4\n",
            "python",
            ["total.py"],
        )
        self.assertTrue(accepted, detail)

    def test_python_rejects_getattr_on_production_metadata_and_paths(self):
        for source, expected in [
            (
                "import total\ngetattr(total, '__file__')\n",
                "gets __file__",
            ),
            (
                "from total import calculate\ngetattr(calculate, '__code__')\n",
                "gets __code__",
            ),
            (
                "from pathlib import Path\ngetattr(Path('total.py'), 'stat')()\n",
                "gets Path.stat",
            ),
            (
                "from pathlib import Path\ngetattr(Path('total.py'), 'read_text')()\n",
                "gets Path.read_text",
            ),
        ]:
            with self.subTest(expected=expected):
                accepted, detail = self.check_source(
                    "test_total.py", source, "python", ["total.py"]
                )
                self.assertFalse(accepted)
                self.assertIn(expected, detail)

    def test_python_rejects_dunder_aliases_and_reflection_primitives(self):
        cases = [
            (
                "from total import calculate as function\nfunction.__code__\n",
                "__code__",
            ),
            (
                "import pathlib\n"
                'reader = pathlib.Path.__dict__["read_" + "text"]\n'
                'reader(pathlib.Path("total.py"))\n',
                "__dict__",
            ),
            ("import inspect\n", "unsafe introspection module inspect"),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                accepted, detail = self.check_source(
                    "test_total.py", source, "python", ["total.py"]
                )
                self.assertFalse(accepted)
                self.assertIn(expected, detail)

    def test_python_rejects_private_aliases_frames_and_class_patterns(self):
        cases = [
            ("import fileinput\n", "unsafe introspection module fileinput"),
            ("import gc\n", "unsafe introspection module gc"),
            (
                "import total as _production\n",
                "private alias _production",
            ),
            (
                "from total import _private_reader\n",
                "unsafe re-export total._private_reader",
            ),
            (
                "from total import calculate as _function\n",
                "unsafe re-export total.calculate",
            ),
            (
                "from unittest import mock\nmock.patch('total.calculate')\n",
                "unsafe re-export unittest.mock",
            ),
            (
                "try:\n    raise RuntimeError\n"
                "except RuntimeError as error:\n"
                "    error.__traceback__.tb_frame.f_code.co_consts\n",
                "forbidden reflection attribute",
            ),
            (
                "class Record:\n    __match_args__ = ('value',)\n"
                "match object():\n"
                "    case Record(value):\n        pass\n",
                "class pattern",
            ),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                accepted, detail = self.check_source(
                    "test_total.py", source, "python", ["total.py"]
                )
                self.assertFalse(accepted)
                self.assertIn(expected, detail)

    def test_python_requires_exact_full_module_allowlist(self):
        for source, module in [
            ("import unittest.mock\n", "unittest.mock"),
            ("from pathlib.private import reader\n", "pathlib.private"),
            ("__import__('unittest.mock')\n", "unittest.mock"),
        ]:
            with self.subTest(module=module):
                accepted, detail = self.check_source(
                    "test_total.py", source, "python", ["total.py"]
                )
                self.assertFalse(accepted)
                self.assertIn(module, detail)

    def test_python_rejects_non_stdlib_dependency(self):
        cases = [
            ("import requests\nfrom total import calculate\n", "imports non-stdlib"),
            (
                "import importlib\nimportlib.import_module('requests')\n",
                "dynamically imports non-stdlib",
            ),
            (
                "import subprocess\nsubprocess.run(['cat', 'total.py'])\n",
                "calls subprocess.run",
            ),
            (
                "import subprocess\nrun = subprocess.run\nrun(['cat', 'total.py'])\n",
                "calls subprocess.run through an alias",
            ),
            (
                "import importlib\nload = importlib.import_module\nload('requests')\n",
                "calls importlib.import_module through an alias",
            ),
            (
                "from pathlib import Path\n"
                "name = ''.join(['total', '.py'])\n"
                "Path(name).read_text()\n",
                "calls Path.read_text",
            ),
            (
                "from pathlib import Path\n"
                "method = ''.join(['read', '_text'])\n"
                "getattr(Path('total.py'), method)()\n",
                "calls builtins.getattr",
            ),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                accepted, detail = self.check_source(
                    "test_total.py", source, "python", ["total.py"]
                )
                self.assertFalse(accepted)
                self.assertIn(expected, detail)

    def test_javascript_rejects_computed_fs_access_but_allows_helper_dispatch(self):
        rejected, detail = self.check_source(
            "state.test.js",
            "const fs = require('node:' + 'fs');\n"
            "const read = fs['read' + 'FileSync'];\n",
            "javascript",
            ["state.js"],
        )
        self.assertFalse(rejected)
        self.assertIn("node:fs", detail)

        accepted, detail = self.check_source(
            "state.test.js",
            "const apply = (subject, name) => subject[name]();\n",
            "javascript",
            ["state.js"],
        )
        self.assertTrue(accepted, detail)

    def test_javascript_rejects_loader_aliases_and_detached_module_names(self):
        sources = [
            "const load = require;\n"
            "const moduleName = 'node:' + 'fs';\n"
            "load(moduleName);\n",
            "const load = process.getBuiltinModule;\n"
            "const moduleName = 'node:fs';\n"
            "load(moduleName);\n",
            "const forbiddenButNotCalled = 'node:' + 'fs';\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state.test.js", source, "javascript", ["state.js"]
                )
                self.assertFalse(accepted)
                self.assertIn("node:fs", detail)

    def test_javascript_rejects_runtime_loader_and_function_source_paths(self):
        sources = [
            'const load = module.constructor["_lo" + "ad"];\n',
            "Function.prototype.toString.call(importedProductionFunction);\n",
            "const probe = process.execArgv.join(' ');\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state.test.js", source, "javascript", ["state.js"]
                )
                self.assertFalse(accepted)
                self.assertIn("runtime introspection", detail)

        accepted, detail = self.check_source(
            "state.test.js",
            "const test = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "const apply = (subject, name) => subject[name]();\n"
            "test('sequence', () => assert.deepEqual(apply(subject, 'snapshot'), expected));\n",
            "javascript",
            ["state.js"],
        )
        self.assertTrue(accepted, detail)

    def test_javascript_rejects_dynamic_import_globals_and_escaped_identifiers(self):
        cases = [
            ("import('./state.js');\n", "primitive import"),
            ("globalThis.process.getBuiltinModule('node:fs');\n", "globalThis"),
            (
                r"const runtime = proce\u0073s; "
                r"const name = 'node:' + 'fs'; runtime.getBuiltinModule(name);",
                "escaped identifier syntax",
            ),
            ("Function('return 1')();\n", "primitive Function"),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                accepted, detail = self.check_source(
                    "state.test.js", source, "javascript", ["state.js"]
                )
                self.assertFalse(accepted)
                self.assertIn(expected, detail)

    def test_javascript_requires_exact_module_allowlist(self):
        accepted, detail = self.check_source(
            "state.test.js",
            "const assert = require('node:assert');\n",
            "javascript",
            ["state.js"],
        )
        self.assertFalse(accepted)
        self.assertIn("outside the allowlist node:assert", detail)

    def test_go_rejects_production_stat_but_allows_channel_close_loop(self):
        rejected, detail = self.check_source(
            "state_test.go",
            'package state\nimport "os"\nfunc inspect() { _, _ = os.Stat("state.go") }\n',
            "go",
            ["state.go"],
        )
        self.assertFalse(rejected)
        self.assertIn("os.Stat", detail)

        accepted, detail = self.check_source(
            "state_test.go",
            "package state\n"
            "func collect(ch <-chan int) { for { if _, ok := <-ch; !ok { return } } }\n",
            "go",
            ["state.go"],
        )
        self.assertTrue(accepted, detail)

    def test_go_rejects_execution_changing_directives(self):
        sources = [
            "//go:build never\n\npackage state\n",
            "// +build never\n\npackage state\n",
            'package state\nimport _ "embed"\n//go:embed state.go\nvar source string\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state_test.go", source, "go", ["state.go"]
                )
                self.assertFalse(accepted)
                self.assertIn("execution-changing directive", detail)

    def test_go_rejects_timing_and_default_select_synchronization(self):
        sources = [
            'package state\nimport "time"\nfunc wait() { time.Sleep(time.Second) }\n',
            "package state\nfunc poll(ch <-chan int) { select { case <-ch: default: } }\n",
            'package state\nimport "runtime"\nfunc poll() { runtime.Gosched() }\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state_test.go", source, "go", ["state.go"]
                )
                self.assertFalse(accepted)
                self.assertTrue(
                    "outside the allowlist time" in detail
                    or "default-select polling" in detail
                    or "runtime.Gosched" in detail,
                    detail,
                )

    def test_go_rejects_process_and_all_source_file_reads(self):
        sources = [
            'package state\nimport "os/exec"\nfunc read() { _, _ = exec.Command("cat", "state.go").Output() }\n',
            'package state\nimport "os"\nfunc read(name string) { _, _ = os.ReadFile(name) }\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state_test.go", source, "go", ["state.go"]
                )
                self.assertFalse(accepted)
                self.assertIn("calls", detail)

    def test_go_rejects_parse_dir_dirfs_and_reflective_polling(self):
        sources = [
            'package state\nimport ("go/parser"; "go/token")\n'
            'func inspect() { _, _ = parser.ParseDir(token.NewFileSet(), ".", nil, 0) }\n',
            'package state\nimport ("io"; "os")\n'
            'func inspect() { file, _ := os.DirFS(".").Open("state.go"); _, _ = io.ReadAll(file) }\n',
            'package state\nimport ("reflect"; "runtime")\n'
            "func poll() { yield := reflect.ValueOf(runtime.Gosched); yield.Call(nil) }\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state_test.go", source, "go", ["state.go"]
                )
                self.assertFalse(accepted)
                self.assertIn("outside the allowlist", detail)

        accepted, detail = self.check_source(
            "state_test.go",
            'package state\nimport ("bytes"; "math/rand"; "sort"; "testing"; "testing/quick")\n'
            "func TestContract(t *testing.T) {\n"
            " values := []int{2, 1}; sort.Ints(values); _ = bytes.Equal(nil, nil);\n"
            " _ = quick.Check(func(value int) bool { return value == value }, &quick.Config{Rand: rand.New(rand.NewSource(1))})\n"
            "}\n",
            "go",
            ["state.go"],
        )
        self.assertTrue(accepted, detail)

    def test_go_rejects_cgo_unsafe_http_and_compile_time_constructs(self):
        sources = [
            'package state\nimport "C"\nfunc TestC() {}\n',
            'package state\nimport "unsafe"\nvar _ = unsafe.Sizeof(0)\n',
            'package state\nimport "net/http"\n'
            'func inspect(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, "state.go") }\n',
            "//go:nosplit\npackage state\nfunc inspect() {}\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                accepted, detail = self.check_source(
                    "state_test.go", source, "go", ["state.go"]
                )
                self.assertFalse(accepted)
                self.assertTrue(
                    "outside the allowlist" in detail
                    or "execution-changing directive" in detail,
                    detail,
                )

    @unittest.skipUnless(shutil.which("go"), "Go is unavailable")
    def test_go_compile_environment_ignores_ambient_toolchain_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "GOFLAGS": "-toolexec=untrusted",
                    "GOEXPERIMENT": "untrusted",
                    "GODEBUG": "gocacheverify=1",
                },
                clear=False,
            ):
                _go, environment = _private_go_environment(Path(temporary), race=False)
        self.assertNotIn("GOFLAGS", environment)
        self.assertNotIn("GOEXPERIMENT", environment)
        self.assertNotIn("GODEBUG", environment)
        self.assertEqual(environment["GOENV"], "off")
        self.assertEqual(environment["GOWORK"], "off")
        self.assertEqual(environment["GOTOOLCHAIN"], "local")
        self.assertEqual(environment["GOPROXY"], "off")

    def test_python_runs_after_protected_source_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            workspace.joinpath("subject.py").write_text(
                "from pathlib import Path\n"
                "def source_absent():\n    return not Path(__file__).exists()\n",
                encoding="utf-8",
            )
            workspace.joinpath("test_subject.py").write_text(
                "import unittest\nfrom subject import source_absent\n"
                "class SubjectTests(unittest.TestCase):\n"
                "    def test_source_absent(self):\n"
                "        self.assertTrue(source_absent())\n",
                encoding="utf-8",
            )
            with patch.object(verifier_lib, "run", side_effect=self.run_locally):
                result = run_python_tests(workspace, ["subject.py"], 15)
        self.assertTrue(result.passed, result.summary())

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_node_runs_with_sources_and_runtime_code_paths_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            workspace.joinpath("subject.js").write_text(
                '"use strict";\nconst fs = require("node:fs");\n'
                "function sourceAbsent() { return !fs.existsSync('subject.js'); }\n"
                "function visible() { return 1; }\n"
                "module.exports = { sourceAbsent, visible };\n",
                encoding="utf-8",
            )
            workspace.joinpath("subject.test.js").write_text(
                '"use strict";\nconst test = require("node:test");\n'
                'const assert = require("node:assert/strict");\n'
                "const subject = require('./subject');\n"
                "test('runtime boundary', async () => {\n"
                "  assert.equal(subject.sourceAbsent(), true);\n"
                "  assert.match(`${subject.visible}`, /source unavailable/);\n"
                "  assert.equal(typeof process, 'undefined');\n"
                "  assert.throws(() => Function('return 1')(), /Code generation from strings disallowed/);\n"
                "  await assert.rejects(import('./subject.js'), /ESM module loading is disabled/);\n"
                "});\n",
                encoding="utf-8",
            )
            with patch.object(verifier_lib, "run", side_effect=self.run_locally):
                result = run_node_tests(
                    workspace, ["subject.js"], ["subject.test.js"], 15
                )
        self.assertTrue(result.passed, result.summary())

    @unittest.skipUnless(shutil.which("go"), "Go is unavailable")
    def test_go_binary_runs_after_all_module_sources_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            workspace.joinpath("go.mod").write_text(
                "module example.test/sourceabsent\n\ngo 1.22\n", encoding="utf-8"
            )
            workspace.joinpath("subject.go").write_text(
                'package sourceabsent\nimport ("errors"; "os")\n'
                "func SourcesAbsent() bool {\n"
                ' for _, name := range []string{"go.mod", "subject.go", "subject_test.go"} {\n'
                "  if _, err := os.Stat(name); !errors.Is(err, os.ErrNotExist) { return false }\n"
                " }\n return true\n}\n",
                encoding="utf-8",
            )
            workspace.joinpath("subject_test.go").write_text(
                'package sourceabsent\nimport "testing"\n'
                "func TestSourcesAbsent(t *testing.T) {\n"
                ' if !SourcesAbsent() { t.Fatal("module source remained at runtime") }\n'
                "}\n",
                encoding="utf-8",
            )
            with patch.object(verifier_lib, "run", side_effect=self.run_locally):
                result = run_go_tests(workspace, timeout_seconds=90)
        self.assertTrue(result.passed, result.summary())

    def test_tests_only_guard_rejects_non_test_files_and_vendored_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture"
            workspace = root / "workspace"
            fixture.mkdir()
            (fixture / "total.py").write_text("def total(): return 4\n")
            (fixture / "test_total.py").write_text("def test_old(): pass\n")
            shutil.copytree(fixture, workspace)
            (workspace / "test_total.py").write_text(
                "from total import total\ndef test_total(): assert total() == 4\n"
            )
            accepted, detail = tests_only_changes(workspace, fixture, "python")
            self.assertTrue(accepted, detail)

            (workspace / "requirements.txt").write_text("requests==2.0\n")
            accepted, detail = tests_only_changes(workspace, fixture, "python")
            self.assertFalse(accepted)
            self.assertIn("added non-test file requirements.txt", detail)

            (workspace / "requirements.txt").unlink()
            vendor = workspace / "requests"
            vendor.mkdir()
            (vendor / "__init__.py").write_text("def get(): pass\n")
            accepted, detail = tests_only_changes(workspace, fixture, "python")
            self.assertFalse(accepted)
            self.assertIn("added non-test file requests/__init__.py", detail)

    def test_sqlite_check_accepts_function_and_constructor_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "test_one.py").write_text(
                "import sqlite3 as db\ndb.connect(':memory:')\n", encoding="utf-8"
            )
            (workspace / "test_two.py").write_text(
                "from sqlite3 import Connection as Database\nDatabase(':memory:')\n",
                encoding="utf-8",
            )
            accepted, detail = python_uses_sqlite(
                workspace, ["test_one.py", "test_two.py"]
            )
        self.assertTrue(accepted, detail)
        self.assertIn("sqlite3.connect", detail)
        self.assertIn("sqlite3.Connection", detail)

    def test_sqlite_check_rejects_only_statically_unreachable_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "test_registry.py").write_text(
                "import sqlite3\nif False:\n    sqlite3.connect(':memory:')\n",
                encoding="utf-8",
            )
            accepted, detail = python_uses_sqlite(workspace, ["test_registry.py"])
        self.assertFalse(accepted)
        self.assertIn("no sqlite3", detail)

    def test_mutant_runtime_error_counts_as_behavioral_failure(self):
        result = CommandResult(
            1,
            "Ran 1 test in 0.001s\nFAILED (errors=1)\nERROR: test_contract",
            "TypeError: bad mutant result",
        )
        self.assertTrue(assertion_failure("python", result))


if __name__ == "__main__":
    unittest.main()
