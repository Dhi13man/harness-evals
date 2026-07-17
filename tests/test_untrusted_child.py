from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SUITE_ROOT = Path(__file__).resolve().parents[1]
SHARED_ROOT = SUITE_ROOT / "cases" / "testing" / "_shared"
sys.path.insert(0, str(SHARED_ROOT))

import untrusted_child  # noqa: E402
from untrusted_child import (  # noqa: E402
    CANDIDATE_TMPFS_BYTES,
    CANDIDATE_TMPFS_INODES,
    MAX_CAPTURE_BYTES,
    _copy_source_tree,
    run_untrusted,
)
from verifier_lib import run as run_candidate_test  # noqa: E402


def sandbox_environment(workspace: Path) -> dict[str, str]:
    resolved = {
        name: shutil.which(name) for name in ("unshare", "mount", "setpriv", "env")
    }
    if any(path is None for path in resolved.values()):
        raise AssertionError(f"missing sandbox tool: {resolved}")
    tool_bin = workspace.parent / ".sandbox-tool-bin"
    tool_bin.mkdir(mode=0o700, exist_ok=True)
    python = Path(sys.executable).resolve()
    shutil.copy2(python, tool_bin / Path(sys.executable).name)
    return {
        "EVAL_WORKSPACE": str(workspace),
        "EVAL_SUITE_ROOT": str(SUITE_ROOT),
        "EVAL_CASE_ROOT": str(SUITE_ROOT / "cases" / "software"),
        "EVAL_SHARED_ROOT": str(SHARED_ROOT),
        "EVAL_TOOL_BIN": str(tool_bin),
        "EVAL_HOST_UID": str(os.getuid()),
        "EVAL_UNSHARE": str(resolved["unshare"]),
        "EVAL_MOUNT": str(resolved["mount"]),
        "EVAL_SETPRIV": str(resolved["setpriv"]),
        "EVAL_ENV": str(resolved["env"]),
    }


class UntrustedChildIntegrationTests(unittest.TestCase):
    def test_non_workspace_namespace_roots_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="namespace-roots-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            source = r"""
import json
from pathlib import Path

results = {}
for root in ("/home", "/run", "/var/tmp", "/dev/shm"):
    target = Path(root) / "candidate-residue"
    try:
        target.write_text("residue", encoding="utf-8")
    except OSError:
        results[root] = False
    else:
        results[root] = True
Path("/tmp/write-probe").write_text("allowed", encoding="utf-8")
results["/tmp"] = Path("/tmp/write-probe").is_file()
print(json.dumps(results, sort_keys=True))
"""
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted([sys.executable, "-c", source], clone, 10)
        self.assertTrue(completed.passed, completed)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "/dev/shm": False,
                "/home": False,
                "/run": False,
                "/tmp": True,
                "/var/tmp": False,
            },
        )

    def test_host_oracle_process_environment_and_sibling_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="untrusted-test-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            sibling = root / "future-mutant"
            output = root / "agent-output.txt"
            for directory in (workspace, clone, sibling):
                directory.mkdir()
            workspace.joinpath("source.txt").write_text("original", encoding="utf-8")
            shutil.copytree(workspace, clone, dirs_exist_ok=True)
            sibling.joinpath("secret.txt").write_text("future", encoding="utf-8")
            output.write_text("agent response", encoding="utf-8")
            host_namespaces = {
                name: os.readlink(f"/proc/self/ns/{name}")
                for name in ("user", "mnt", "pid", "net", "ipc", "uts")
            }
            probes = [
                str(SUITE_ROOT / "suite.json"),
                str(SHARED_ROOT / "verifier_lib.py"),
                str(sibling / "secret.txt"),
                str(output),
                str(Path.home() / ".claude" / ".credentials.json"),
                f"/run/user/{os.getuid()}/bus",
                "/run/dbus/system_bus_socket",
                "/run/docker.sock",
                "/run/snapd.socket",
            ]
            source = r"""
import json, os
from pathlib import Path
import socket
import subprocess

probes = json.loads(os.environ.pop("PROBES"))
visible = [raw for raw in probes if Path(raw).exists()]
readable = []
for raw in probes:
    try:
        Path(raw).read_bytes()
    except OSError:
        continue
    readable.append(raw)
unix_connected = []
for raw in probes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(raw)
    except OSError:
        pass
    else:
        unix_connected.append(raw)
    finally:
        client.close()
network = True
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.2)
except OSError:
    network = False
pids = sorted(path.name for path in Path("/proc").iterdir() if path.name.isdigit())
status = Path("/proc/self/status").read_text()
security = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith(("CapEff:", "CapBnd:", "NoNewPrivs:"))}
namespaces = {name: os.readlink(f"/proc/self/ns/{name}") for name in ("user", "mnt", "pid", "net", "ipc", "uts")}
try:
    unmount = subprocess.run(["umount", "/home"], capture_output=True).returncode
except FileNotFoundError:
    unmount = 127
try:
    remap = subprocess.run(["unshare", "--user", "--map-root-user", "true"], capture_output=True).returncode
except FileNotFoundError:
    remap = 127
Path("source.txt").write_text("candidate changed clone")
Path("created.txt").write_text("candidate output")
print(json.dumps({"visible": visible, "readable": readable, "unix_connected": unix_connected, "network": network, "pids": pids, "security": security, "namespaces": namespaces, "unmount": unmount, "remap": remap, "eval": sorted(key for key in os.environ if key.startswith("EVAL_"))}, sort_keys=True))
"""
            environment = sandbox_environment(workspace)
            literal_source = source.replace(
                'json.loads(os.environ.pop("PROBES"))', repr(probes)
            )
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted(
                    [sys.executable, "-c", literal_source], clone, 10
                )
            self.assertTrue(completed.passed, completed)
            facts = json.loads(completed.stdout)
            self.assertEqual(facts["visible"], [])
            self.assertEqual(facts["readable"], [])
            self.assertEqual(facts["unix_connected"], [])
            self.assertFalse(facts["network"])
            self.assertEqual(facts["pids"], ["1"])
            self.assertEqual(
                facts["security"],
                {
                    "CapBnd": "0000000000000000",
                    "CapEff": "0000000000000000",
                    "NoNewPrivs": "1",
                },
            )
            self.assertEqual(set(facts["namespaces"]), set(host_namespaces))
            for name, host_namespace in host_namespaces.items():
                self.assertNotEqual(facts["namespaces"][name], host_namespace, name)
            self.assertNotEqual(facts["unmount"], 0)
            self.assertNotEqual(facts["remap"], 0)
            self.assertEqual(facts["eval"], [])
            self.assertEqual(
                workspace.joinpath("source.txt").read_text(encoding="utf-8"),
                "original",
            )
            self.assertFalse(workspace.joinpath("created.txt").exists())

    def test_testing_runner_discards_candidate_source_edits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mutation-isolation-") as raw_root:
            workspace = Path(raw_root) / "declared-workspace"
            workspace.mkdir()
            production = workspace / "production.py"
            production.write_text("VALUE = 1\n", encoding="utf-8")
            removable = workspace / "delete_me.py"
            removable.write_text("VALUE = 2\n", encoding="utf-8")
            environment = sandbox_environment(workspace)
            mutator = (
                "from pathlib import Path; "
                "Path('production.py').write_text('VALUE = 999\\n'); "
                "Path('delete_me.py').unlink(); "
                "Path('persisted.txt').write_text('should disappear')"
            )
            with patch.dict(os.environ, environment, clear=False):
                completed = run_candidate_test(
                    [sys.executable, "-c", mutator], workspace, 10
                )
            self.assertTrue(completed.passed, completed.summary())
            self.assertEqual(production.read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual(removable.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertFalse(workspace.joinpath("persisted.txt").exists())

    def test_software_verifiers_never_import_candidate_code_in_parent(self) -> None:
        software_root = SUITE_ROOT / "cases" / "software"
        violations: list[str] = []
        for verifier in sorted(software_root.glob("*/oracle/verify.py")):
            tree = ast.parse(verifier.read_text(encoding="utf-8"), verifier.as_posix())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if isinstance(function, ast.Attribute) and function.attr in {
                    "exec_module",
                    "load_module",
                }:
                    violations.append(f"{verifier.parent.parent.name}:{node.lineno}")
                if isinstance(function, ast.Name) and function.id in {
                    "eval",
                    "exec",
                    "__import__",
                }:
                    violations.append(f"{verifier.parent.parent.name}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_go_hidden_source_is_removed_before_binary_execution(self) -> None:
        go = shutil.which("go")
        if go is None:
            self.skipTest("Go is unavailable")
        with tempfile.TemporaryDirectory(prefix="go-oracle-source-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            build = root / "disposable-clone"
            workspace.mkdir()
            workspace.joinpath("go.mod").write_text(
                "module example.test/oracleblind\n\ngo 1.22\n", encoding="utf-8"
            )
            workspace.joinpath("oracleblind.go").write_text(
                "package oracleblind\n\n"
                'import "os"\n\n'
                "func HiddenSourceAbsent() bool {\n"
                '    _, err := os.Stat("zz_eval_oracle_test.go")\n'
                "    return os.IsNotExist(err)\n"
                "}\n",
                encoding="utf-8",
            )
            shutil.copytree(workspace, build)
            hidden = build / "zz_eval_oracle_test.go"
            hidden.write_text(
                "package oracleblind\n\n"
                'import "testing"\n\n'
                "func TestOracleBlind(t *testing.T) {\n"
                "    if !HiddenSourceAbsent() {\n"
                '        t.Fatal("hidden source remained visible")\n'
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            binary = build / "zz_eval_oracle.test"
            compiled = subprocess.run(
                [go, "test", "-c", "-o", binary.name, "."],
                cwd=build,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            hidden.unlink()
            self.assertFalse(hidden.exists())
            environment = sandbox_environment(workspace)
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted(
                    [f"./{binary.name}", "-test.run=^TestOracleBlind$"], build, 10
                )
            self.assertTrue(completed.passed, completed)

    def test_bundled_tool_survives_original_directory_masking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bundled-tool-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            tool_bin = root / "private-tool-bin"
            for directory in (workspace, clone, tool_bin):
                directory.mkdir()
            tool = tool_bin / "private-probe"
            tool.write_text(
                "#!/usr/bin/python3\nprint('isolated')\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            environment = sandbox_environment(workspace)
            environment["EVAL_TOOL_BIN"] = str(tool_bin)
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted([str(tool)], clone, 10)
            self.assertTrue(completed.passed, completed)
            self.assertEqual(completed.stdout.strip(), "isolated")
            self.assertFalse(clone.joinpath("tool-ran.txt").exists())

    def test_bundled_tool_can_be_a_nested_outer_file_bind(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nested-tool-bind-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            tool_bin = root / "private-tool-bin"
            for directory in (workspace, clone, tool_bin):
                directory.mkdir()
            source_tool = root / "source-tool"
            source_tool.write_text(
                "#!/usr/bin/python3\nprint('isolated')\n",
                encoding="utf-8",
            )
            source_tool.chmod(0o700)
            mounted_tool = tool_bin / "nested-probe"
            mounted_tool.touch(mode=0o700)
            environment = sandbox_environment(workspace)
            environment["EVAL_TOOL_BIN"] = str(tool_bin)
            helper_source = r"""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from untrusted_child import run_untrusted

subprocess.run(
    [sys.argv[3], "--bind", str(root / "source-tool"), str(root / "private-tool-bin" / "nested-probe")],
    capture_output=True,
    text=True,
    timeout=10,
    check=True,
)
result = run_untrusted(["nested-probe"], root / "disposable-clone", 10)
print(json.dumps(dataclasses.asdict(result), sort_keys=True))
"""
            outer = subprocess.run(
                [
                    environment["EVAL_UNSHARE"],
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--pid",
                    "--fork",
                    "--mount-proc",
                    "--kill-child",
                    sys.executable,
                    "-c",
                    helper_source,
                    str(root),
                    str(SHARED_ROOT),
                    environment["EVAL_MOUNT"],
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=environment,
            )
            self.assertEqual(outer.returncode, 0, outer.stdout + outer.stderr)
            facts = json.loads(outer.stdout)
            self.assertEqual(facts["returncode"], 0, facts)
            self.assertFalse(facts["timed_out"], facts)
            self.assertFalse(facts["output_limited"], facts)
            self.assertIsNone(facts["sandbox_error"], facts)
            self.assertEqual(facts["stdout"].strip(), "isolated")
            self.assertFalse(clone.joinpath("nested-tool-ran.txt").exists())

    def test_missing_bare_top_level_tool_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="missing-bare-tool-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted(
                    ["definitely-not-a-private-eval-tool"], clone, 10
                )
            self.assertEqual(completed.returncode, 125)
            self.assertIn(
                "absent from the private tool bin", completed.sandbox_error or ""
            )

    def test_go_runtime_root_is_descriptor_bound_and_read_only(self) -> None:
        go = shutil.which("go")
        if go is None:
            self.skipTest("Go is unavailable")
        go_root = subprocess.run(
            [go, "env", "GOROOT"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="go-root-bind-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            bundled_go = Path(environment["EVAL_TOOL_BIN"]) / "go"
            shutil.copy2(Path(go).resolve(), bundled_go)
            bundled_go.chmod(0o500)
            environment["EVAL_GO_ROOT"] = go_root
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted(["go", "env", "GOROOT"], clone, 10)
            self.assertTrue(completed.passed, completed)
            self.assertEqual(completed.stdout.strip(), str(Path(go_root).resolve()))
            source = (
                "import os; from pathlib import Path; "
                "p=Path(os.environ['GOROOT'])/'zz-eval-write'; "
                "\ntry: p.write_text('unsafe')\n"
                "except OSError: print('read-only')\n"
                "else: print('writable')"
            )
            with patch.dict(os.environ, environment, clear=False):
                write_probe = run_untrusted([sys.executable, "-c", source], clone, 10)
            self.assertTrue(write_probe.passed, write_probe)
            self.assertEqual(write_probe.stdout.strip(), "read-only")

    def test_closed_tool_bundle_runs_go_race_toolchain(self) -> None:
        resolved = {name: shutil.which(name) for name in ("as", "gcc", "go", "ld")}
        if any(path is None for path in resolved.values()):
            self.skipTest(f"Go race toolchain is unavailable: {resolved}")
        gcc = str(resolved["gcc"])
        go = str(resolved["go"])
        go_root = subprocess.run(
            [go, "env", "GOROOT"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        libgcc = subprocess.run(
            [gcc, "-print-libgcc-file-name"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        machine = subprocess.run(
            [gcc, "-dumpmachine"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        major_version = subprocess.run(
            [gcc, "-dumpversion"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        derived = {}
        for name in ("cc1", "collect2", "lto-wrapper"):
            derived[name] = subprocess.run(
                [gcc, f"-print-prog-name={name}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="closed-go-race-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            clone.joinpath("go.mod").write_text(
                "module eval.local/raceprobe\n\ngo 1.22\n", encoding="utf-8"
            )
            clone.joinpath("probe_test.go").write_text(
                "package raceprobe\n\n"
                'import "testing"\n\n'
                "func TestProbe(t *testing.T) {}\n",
                encoding="utf-8",
            )
            environment = sandbox_environment(workspace)
            tool_bin = Path(environment["EVAL_TOOL_BIN"])
            for name, source in {**resolved, **derived}.items():
                assert source is not None
                shutil.copy2(Path(source).resolve(), tool_bin / name)
                tool_bin.joinpath(name).chmod(0o500)
            environment["EVAL_GO_ROOT"] = go_root
            install_root = Path(libgcc).resolve().parent
            self.assertEqual(install_root.parts[-2:], (machine, major_version))
            environment["EVAL_GCC_EXEC_PREFIX"] = str(install_root.parent.parent)
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted(["go", "test", "-race", "./..."], clone, 60)
            self.assertTrue(completed.passed, completed)
            self.assertIn("ok", completed.stdout)

    def test_nested_tmpfs_caps_aggregate_blocks_and_inodes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmpfs-ceilings-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            source = (
                "import json, os; "
                "v=os.statvfs('/tmp'); "
                "print(json.dumps({'bytes':v.f_blocks*v.f_frsize,'inodes':v.f_files}))"
            )
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted([sys.executable, "-c", source], clone, 10)
            self.assertTrue(completed.passed, completed)
            limits = json.loads(completed.stdout)
            self.assertLessEqual(limits["bytes"], CANDIDATE_TMPFS_BYTES)
            self.assertLessEqual(limits["inodes"], CANDIDATE_TMPFS_INODES)

    def test_source_copy_rejects_many_files_depth_and_aggregate_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-ceilings-") as raw_root:
            root = Path(raw_root)
            source = root / "source"
            source.mkdir()
            source.joinpath("one").write_bytes(b"123")
            source.joinpath("two").write_bytes(b"456")
            source.joinpath("nested", "too-deep").mkdir(parents=True)
            entry_target = root / "entry-target"
            depth_target = root / "depth-target"
            byte_target = root / "byte-target"
            entry_target.mkdir()
            depth_target.mkdir()
            byte_target.mkdir()
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(untrusted_child, "MAX_SOURCE_ENTRIES", 1):
                    with self.assertRaisesRegex(RuntimeError, "maximum entries"):
                        _copy_source_tree(descriptor, entry_target)
                with patch.object(untrusted_child, "MAX_SOURCE_DEPTH", 1):
                    with self.assertRaisesRegex(RuntimeError, "maximum depth"):
                        _copy_source_tree(descriptor, depth_target)
                with patch.object(untrusted_child, "MAX_SOURCE_BYTES", 5):
                    with self.assertRaisesRegex(RuntimeError, "aggregate limit"):
                        _copy_source_tree(descriptor, byte_target)
            finally:
                os.close(descriptor)

    def test_timeout_kills_detached_descendants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="timeout-isolation-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            source = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(1.5); Path('survivor.txt').write_text('escaped')"], start_new_session=True)
time.sleep(10)
"""
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted([sys.executable, "-c", source], clone, 1)
            self.assertTrue(completed.timed_out, completed)
            time.sleep(2)
            self.assertFalse(clone.joinpath("survivor.txt").exists())

    def test_output_overflow_kills_candidate_and_caps_capture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-isolation-") as raw_root:
            root = Path(raw_root)
            workspace = root / "declared-workspace"
            clone = root / "disposable-clone"
            workspace.mkdir()
            clone.mkdir()
            environment = sandbox_environment(workspace)
            source = f"import os; os.write(1, b'x' * {MAX_CAPTURE_BYTES + 65536})"
            with patch.dict(os.environ, environment, clear=False):
                completed = run_untrusted([sys.executable, "-c", source], clone, 10)
            self.assertTrue(completed.output_limited, completed)
            self.assertLessEqual(
                len(completed.stdout.encode("utf-8")), MAX_CAPTURE_BYTES
            )

    def test_declared_workspace_is_rejected_as_an_execution_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="direct-workspace-") as raw_root:
            workspace = Path(raw_root) / "declared-workspace"
            workspace.mkdir()
            environment = sandbox_environment(workspace)
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(ValueError, "disposable clone"):
                    run_untrusted([sys.executable, "-c", "pass"], workspace, 10)


if __name__ == "__main__":
    unittest.main()
