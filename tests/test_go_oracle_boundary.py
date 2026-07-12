from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SUITE_ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = SUITE_ROOT / "cases"
SHARED_ROOT = SUITE_ROOT / "cases" / "testing" / "_shared"
SOFTWARE_ROOT = SUITE_ROOT / "cases" / "software"
sys.path.insert(0, str(CASES_ROOT))
sys.path.insert(0, str(SHARED_ROOT))

from _calibration_tools import (  # noqa: E402
    private_tool_environment,
    sandbox_tool_paths,
)
import go_external_oracle  # noqa: E402
from go_external_oracle import (  # noqa: E402
    PROTOCOL,
    BuiltGoOracle,
    GoModulePolicy,
    GoOracleError,
    build_external_go_oracle,
    run_go_mode,
)
from verifier_lib import CommandResult  # noqa: E402


PROBE_POLICY = GoModulePolicy(
    module_path="example.test/candidate",
    package_name="candidate",
    required_source="candidate.go",
)
PROBE_SOURCE = """package candidate

func Value() int {
	return 7
}
"""
PROBE_HARNESS = r"""package main

import (
	"encoding/json"
	"flag"
	"os"

	candidate "example.test/candidate"
)

type envelope struct {
	Protocol     string `json:"protocol"`
	Token        string `json:"token"`
	Mode         string `json:"mode"`
	Complete     bool   `json:"complete"`
	Observations any    `json:"observations"`
}

func main() {
	mode := flag.String("mode", "", "mode")
	token := flag.String("token", "", "token")
	flag.Parse()
	if *mode == "" || *token == "" || flag.NArg() != 0 {
		os.Exit(2)
	}
	_, statErr := os.Stat("mode-state")
	fresh := os.IsNotExist(statErr)
	if statErr != nil && !os.IsNotExist(statErr) {
		os.Exit(3)
	}
	if err := os.WriteFile("mode-state", []byte(*mode), 0600); err != nil {
		os.Exit(4)
	}
	sourcesAbsent := true
	for _, name := range []string{"candidate.go", "go.mod", "main.go"} {
		if _, err := os.Stat(name); !os.IsNotExist(err) {
			sourcesAbsent = false
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(envelope{
		Protocol: "external-go-oracle-v1",
		Token: *token,
		Mode: *mode,
		Complete: true,
		Observations: map[string]any{
			"fresh": fresh, "sources_absent": sourcesAbsent, "value": candidate.Value(),
		},
	}); err != nil {
		os.Exit(5)
	}
}
"""


def sandbox_environment(
    workspace: Path, tool_environment: dict[str, str]
) -> dict[str, str]:
    resolved = sandbox_tool_paths()
    environment = dict(tool_environment)
    environment.update(
        {
            "EVAL_WORKSPACE": str(workspace),
            "EVAL_CASE_ROOT": str(SOFTWARE_ROOT),
            "EVAL_SHARED_ROOT": str(SHARED_ROOT),
            "EVAL_HOST_UID": str(os.getuid()),
            "EVAL_UNSHARE": str(resolved["unshare"]),
            "EVAL_MOUNT": str(resolved["mount"]),
            "EVAL_SETPRIV": str(resolved["setpriv"]),
            "EVAL_ENV": str(resolved["env"]),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
    )
    return environment


def write_probe_candidate(root: Path) -> None:
    root.mkdir()
    root.joinpath("go.mod").write_text(
        "module example.test/candidate\n\ngo 1.22\n", encoding="utf-8"
    )
    root.joinpath("candidate.go").write_text(PROBE_SOURCE, encoding="utf-8")


@unittest.skipUnless(shutil.which("go"), "Go is unavailable")
class ExternalGoOracleBoundaryTests(unittest.TestCase):
    def test_source_guard_timeout_is_independent_of_harness_build_timeout(self) -> None:
        payload = {
            "production_files": ["candidate.go"],
            "test_files": [],
            "violations": [],
        }
        completed = subprocess.CompletedProcess(
            ["go", "run"], 0, stdout=json.dumps(payload), stderr=""
        )
        with patch.object(
            go_external_oracle.subprocess, "run", return_value=completed
        ) as mocked:
            result = go_external_oracle._inspect_candidate(
                "go",
                Path("/candidate"),
                PROBE_POLICY,
                {},
                1,
            )
        self.assertEqual(result, payload)
        self.assertEqual(
            mocked.call_args.kwargs["timeout"],
            go_external_oracle.SOURCE_GUARD_TIMEOUT_SECONDS,
        )
        self.assertEqual(go_external_oracle.SOURCE_GUARD_TIMEOUT_SECONDS, 90)

    def test_checked_in_go_oracles_have_cold_build_and_outer_margin(self) -> None:
        suite = json.loads((SUITE_ROOT / "suite.json").read_text(encoding="utf-8"))
        outer_timeouts = {
            case["id"]: case["verifier"]["timeout_seconds"]
            for case in suite["cases"]
            if case["id"]
            in {"software-concurrent-store", "software-representative-performance"}
        }
        constants_by_case = {}
        for case_id in outer_timeouts:
            case = case_id.removeprefix("software-")
            source = (SOFTWARE_ROOT / case / "oracle" / "verify.py").read_text(
                encoding="utf-8"
            )
            tree = ast.parse(source)
            constants_by_case[case_id] = {
                node.targets[0].id: node.value.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)
            }
            build_arguments = [
                keyword.value.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_external_go_oracle"
                for keyword in node.keywords
                if keyword.arg == "timeout_seconds"
                and isinstance(keyword.value, ast.Name)
            ]
            self.assertEqual(build_arguments, ["BUILD_TIMEOUT_SECONDS"])

        performance = constants_by_case["software-representative-performance"]
        concurrent = constants_by_case["software-concurrent-store"]
        performance_children = (
            go_external_oracle.SOURCE_GUARD_TIMEOUT_SECONDS
            + performance["BUILD_TIMEOUT_SECONDS"]
            + 10
            + 5
            + (
                1
                + performance["PERFORMANCE_WORKLOADS"]
                * performance["PERFORMANCE_ROUNDS"]
            )
            * performance["MODE_TIMEOUT_SECONDS"]
        )
        concurrent_children = (
            go_external_oracle.SOURCE_GUARD_TIMEOUT_SECONDS
            + concurrent["BUILD_TIMEOUT_SECONDS"]
            + 10
            + 5
            + 3 * concurrent["MODE_TIMEOUT_SECONDS"]
        )
        self.assertEqual(
            outer_timeouts,
            {
                "software-concurrent-store": concurrent_children + 31,
                "software-representative-performance": performance_children + 41,
            },
        )

    def test_parent_go_environment_is_fixed_and_offline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="go-boundary-env-") as raw_root:
            root = Path(raw_root)
            normal_root = root / "normal"
            race_root = root / "race"
            normal_root.mkdir()
            race_root.mkdir()
            go = str(Path(shutil.which("go") or "").resolve(strict=True))

            normal = go_external_oracle._go_environment(normal_root, go, race=False)
            race = go_external_oracle._go_environment(race_root, go, race=True)

        expected_keys = {
            "CGO_ENABLED",
            "GOCACHE",
            "GOENV",
            "GOMODCACHE",
            "GOROOT",
            "GOPROXY",
            "GOSUMDB",
            "GOTOOLCHAIN",
            "GOWORK",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "TMPDIR",
            "TZ",
        }
        self.assertEqual(set(normal), expected_keys)
        self.assertEqual(set(race), expected_keys)
        for environment in (normal, race):
            self.assertEqual(environment["GOENV"], "off")
            self.assertEqual(environment["GOWORK"], "off")
            self.assertEqual(environment["GOTOOLCHAIN"], "local")
            self.assertEqual(environment["GOPROXY"], "off")
            self.assertEqual(environment["GOSUMDB"], "off")
            self.assertTrue(Path(environment["GOROOT"]).is_absolute())
        self.assertEqual(normal["CGO_ENABLED"], "0")
        self.assertEqual(race["CGO_ENABLED"], "1")

    def test_benign_tests_are_excluded_and_every_mode_gets_a_fresh_clone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="go-boundary-positive-") as raw_root:
            root = Path(raw_root)
            candidate = root / "candidate-workspace"
            write_probe_candidate(candidate)
            candidate.joinpath("candidate_test.go").write_text(
                """package candidate

import "testing"

func TestBenign(_ *testing.T) {
	_ = deliberatelyUndefinedIfTestsAreCompiled
}
""",
                encoding="utf-8",
            )
            with private_tool_environment() as tool_environment:
                environment = sandbox_environment(candidate, tool_environment)
                with patch.dict(os.environ, environment, clear=True):
                    with build_external_go_oracle(
                        candidate,
                        PROBE_POLICY,
                        PROBE_HARNESS,
                        race=False,
                        timeout_seconds=20,
                    ) as built:
                        self.assertEqual(
                            built.excluded_test_files, ("candidate_test.go",)
                        )
                        self.assertEqual(
                            {
                                path.name
                                for path in built.executable_workspace.iterdir()
                            },
                            {"oracle-probe"},
                        )
                        self.assertFalse(
                            built.executable_workspace.parent.joinpath(
                                "candidate"
                            ).exists()
                        )
                        self.assertFalse(
                            built.executable_workspace.parent.joinpath(
                                "harness"
                            ).exists()
                        )
                        first = run_go_mode(built, "first", 10)
                        second = run_go_mode(built, "second", 10)

            expected = {"fresh": True, "sources_absent": True, "value": 7}
            self.assertEqual(first.observations, expected)
            self.assertEqual(second.observations, expected)

    def test_forbidden_source_module_and_build_controls_are_rejected(self) -> None:
        cases = {
            "test-main": {
                "candidate_test.go": """package candidate

import "testing"

func TestMain(_ *testing.M) {}
"""
            },
            "embed": {
                "embed.go": """package candidate

import _ "embed"

//go:embed *.go
var source string
"""
            },
            "cgo": {
                "cgo.go": """package candidate

import "C"
"""
            },
            "unsafe-linkname": {
                "linkname.go": """package candidate

import _ "unsafe"

//go:linkname nanotime runtime.nanotime
func nanotime() int64
"""
            },
            "build-directive": {
                "conditional.go": """//go:build linux

package candidate
"""
            },
            "assembly": {"bypass.S": "TEXT bypass(SB),$0-0\n\tRET\n"},
            "swig": {"bypass.swig": "%module bypass\n"},
        }
        for name, additions in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(
                    prefix=f"go-boundary-{name}-"
                ) as raw_root:
                    candidate = Path(raw_root) / "candidate"
                    write_probe_candidate(candidate)
                    for relative, content in additions.items():
                        candidate.joinpath(relative).write_text(
                            content, encoding="utf-8"
                        )
                    with self.assertRaises(GoOracleError):
                        with build_external_go_oracle(
                            candidate,
                            PROBE_POLICY,
                            PROBE_HARNESS,
                            race=False,
                            timeout_seconds=20,
                        ):
                            self.fail("forbidden candidate reached execution")

        forbidden_manifests = {
            "dependency": """module example.test/candidate

go 1.22

require example.invalid/dependency v1.0.0
""",
            "replace": """module example.test/candidate

go 1.22

replace example.invalid/dependency => ../dependency
""",
            "toolchain": """module example.test/candidate

go 1.22

toolchain go1.22.2
""",
        }
        for name, manifest in forbidden_manifests.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(
                    prefix=f"go-module-{name}-"
                ) as raw_root:
                    candidate = Path(raw_root) / "candidate"
                    write_probe_candidate(candidate)
                    candidate.joinpath("go.mod").write_text(manifest, encoding="utf-8")
                    with self.assertRaises(GoOracleError):
                        with build_external_go_oracle(
                            candidate,
                            PROBE_POLICY,
                            PROBE_HARNESS,
                            race=False,
                            timeout_seconds=20,
                        ):
                            self.fail("forbidden module reached execution")

    def test_completion_protocol_rejects_exit_zero_and_forged_envelopes(self) -> None:
        built = BuiltGoOracle(Path("/unused"), "go test", "0" * 64, ())
        mutations = ("empty", "wrong-token", "incomplete", "extra-key", "stderr")

        def invalid_result(mutation: str):
            def run(command, _cwd, _timeout):
                token = command[-1]
                payload = {
                    "complete": True,
                    "mode": "probe",
                    "observations": {},
                    "protocol": PROTOCOL,
                    "token": token,
                }
                if mutation == "empty":
                    return CommandResult(0, "", "")
                if mutation == "wrong-token":
                    payload["token"] = "not-the-parent-token"
                elif mutation == "incomplete":
                    payload["complete"] = False
                elif mutation == "extra-key":
                    payload["unexpected"] = True
                stderr = "unexpected stderr" if mutation == "stderr" else ""
                return CommandResult(0, json.dumps(payload), stderr)

            return run

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with patch.object(
                    go_external_oracle, "run", side_effect=invalid_result(mutation)
                ):
                    with self.assertRaises(GoOracleError):
                        run_go_mode(built, "probe", 10)

    def test_alternate_valid_case_implementations_pass_real_oracles(self) -> None:
        concurrent_source = """package counterstore

import "sync"

type Store struct {
	lock sync.Mutex
	counts map[string]int
}

func NewStore() *Store {
	return &Store{counts: map[string]int{}}
}

func (store *Store) Increment(key string) int {
	store.lock.Lock()
	defer store.lock.Unlock()
	store.counts[key]++
	return store.counts[key]
}

func (store *Store) Get(key string) int {
	store.lock.Lock()
	defer store.lock.Unlock()
	return store.counts[key]
}

func (store *Store) Transfer(from, to string, amount int) bool {
	store.lock.Lock()
	defer store.lock.Unlock()
	if amount <= 0 || store.counts[from] < amount {
		return false
	}
	store.counts[from] -= amount
	store.counts[to] += amount
	return true
}

func (store *Store) Snapshot() map[string]int {
	store.lock.Lock()
	defer store.lock.Unlock()
	copy := make(map[string]int, len(store.counts))
	for key, value := range store.counts {
		copy[key] = value
	}
	return copy
}
"""
        performance_source = """package tagrank

import "sort"

type Entry struct {
	Value string
	Count int
}

func MostFrequent(values []string, limit int) []Entry {
	if limit <= 0 {
		return []Entry{}
	}
	counts := make(map[string]int, len(values))
	first := make([]string, 0, len(values))
	for _, value := range values {
		if counts[value] == 0 {
			first = append(first, value)
		}
		counts[value]++
	}
	entries := make([]Entry, 0, len(first))
	for _, value := range first {
		entries = append(entries, Entry{Value: value, Count: counts[value]})
	}
	sort.SliceStable(entries, func(left, right int) bool {
		return entries[left].Count > entries[right].Count
	})
	if limit < len(entries) {
		entries = entries[:limit]
	}
	return entries
}
"""
        cases = (
            (
                "concurrent-store",
                "example.com/counterstore",
                "store.go",
                concurrent_source,
                280,
            ),
            (
                "representative-performance",
                "example.com/tagrank",
                "ranking.go",
                performance_source,
                500,
            ),
        )
        with private_tool_environment() as tool_environment:
            for case_name, module, source_name, source, timeout_seconds in cases:
                with self.subTest(case=case_name):
                    verdict = self._run_case_verifier(
                        case_name,
                        module,
                        source_name,
                        source,
                        timeout_seconds,
                        tool_environment,
                    )
                    self.assertTrue(verdict["passed"], verdict)

    def _run_case_verifier(
        self,
        case_name: str,
        module: str,
        source_name: str,
        source: str,
        timeout_seconds: int,
        tool_environment: dict[str, str],
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix=f"alternate-{case_name}-") as raw_root:
            root = Path(raw_root)
            workspace = root / "workspace"
            workspace.mkdir()
            workspace.joinpath("go.mod").write_text(
                f"module {module}\n\ngo 1.22\n", encoding="utf-8"
            )
            workspace.joinpath(source_name).write_text(source, encoding="utf-8")
            environment = sandbox_environment(workspace, tool_environment)
            verifier = SOFTWARE_ROOT / case_name / "oracle" / "verify.py"
            completed = subprocess.run(
                [sys.executable, str(verifier)],
                cwd=SUITE_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            verdict = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"{case_name} emitted invalid JSON: {error}: {completed.stdout!r}"
            )
        self.assertIsInstance(verdict, dict)
        return verdict


if __name__ == "__main__":
    unittest.main()
