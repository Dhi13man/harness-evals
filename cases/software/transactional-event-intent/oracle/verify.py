#!/usr/bin/env python3
"""Hidden SQLite atomicity, idempotency, and recovery oracle."""

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


CHILD_TIMEOUT_SECONDS = 10


def assertion(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


def standard_library_only(workspace: Path) -> tuple[bool, str]:
    local_modules = {path.stem for path in workspace.glob("*.py")}
    external: set[str] = set()
    for source in workspace.rglob("*.py"):
        if "tests" in source.parts or "__pycache__" in source.parts:
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            external.add(f"unreadable:{source.name}:{type(error).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = {node.module.partition(".")[0]}
            else:
                continue
            external.update(names - sys.stdlib_module_names - local_modules)
    dependency_names = {
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "uv.lock",
    }
    manifests = sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
        and (path.name in dependency_names or path.name.startswith("requirements"))
    )
    passed = not external and not manifests
    return (
        passed,
        f"external imports={sorted(external)}, dependency manifests={manifests}",
    )


worker_source = r"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys

captured = io.StringIO()
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    spec = importlib.util.spec_from_file_location("candidate_orders", Path.cwd() / "orders.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load orders.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
OrderService = module.OrderService

class RecordingBus:
    def __init__(self):
        self.events = []
    def publish(self, event_id, payload):
        self.events.append((event_id, payload))

class FailBeforeRecordingBus:
    def publish(self, event_id, payload):
        raise ConnectionError("temporary bus outage")

class FailAfterRecordingBus(RecordingBus):
    def publish(self, event_id, payload):
        super().publish(event_id, payload)
        raise ConnectionError("connection dropped after broker accepted event")

def count_orders(connection):
    return int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])

def count_auxiliary_rows(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'orders' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    total = 0
    for (name,) in tables:
        quoted = '"' + str(name).replace('"', '""') + '"'
        total += int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return total

def atomic():
    path = Path("atomic.db")
    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
    writes = [0]
    def deny_second_write(action, arg1, _arg2, _db, _source):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and arg1 and not arg1.startswith("sqlite_"):
            writes[0] += 1
            if writes[0] == 2:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(deny_second_write)
    failed = False
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            service.place_order("atomic-request", "SKU-ATOMIC", 1)
    except Exception:
        failed = True
    finally:
        connection.set_authorizer(None)
    connection.close()
    connection = sqlite3.connect(path)
    orders_after_failure = count_orders(connection)
    auxiliary_rows_after_failure = count_auxiliary_rows(connection)
    bus = RecordingBus()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        flush_count = OrderService(connection).flush_events(bus)
    events = bus.events
    connection.close()
    return {"write_failed": failed, "writes_seen": writes[0], "orders_after_failure": orders_after_failure, "auxiliary_rows_after_failure": auxiliary_rows_after_failure, "events_after_restart": events, "flush_count_after_failure": flush_count}

def idempotent():
    connection = sqlite3.connect("idempotent.db")
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        first_id = service.place_order("same-request", "SKU-ONE", 2)
        second_id = service.place_order("same-request", "SKU-CHANGED", 99)
        bus = RecordingBus()
        first_flush_count = service.flush_events(bus)
        second_flush_count = service.flush_events(bus)
    result = {"first_id": first_id, "second_id": second_id, "orders": count_orders(connection), "events": bus.events, "first_flush_count": first_flush_count, "second_flush_count": second_flush_count}
    connection.close()
    return result

def recovery():
    path = Path("recovery.db")
    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        order_id = service.place_order("restart-request", "SKU-RESTART", 3)
    connection.close()
    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        try:
            service.flush_events(FailBeforeRecordingBus())
            failure_raised = False
        except Exception:
            failure_raised = True
    connection.close()
    connection = sqlite3.connect(path)
    bus = RecordingBus()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        retry_flush_count = service.flush_events(bus)
        empty_flush_count = service.flush_events(bus)
    connection.close()
    return {"order_id": order_id, "events": bus.events, "failure_raised": failure_raised, "retry_flush_count": retry_flush_count, "empty_flush_count": empty_flush_count}

def stable():
    path = Path("stable.db")
    connection = sqlite3.connect(path)
    first = FailAfterRecordingBus()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        service.place_order("stable-request", "SKU-STABLE", 4)
        try:
            service.flush_events(first)
            failure_raised = False
        except Exception:
            failure_raised = True
    connection.close()
    connection = sqlite3.connect(path)
    retry = RecordingBus()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        retry_flush_count = service.flush_events(retry)
        empty_flush_count = service.flush_events(retry)
    connection.close()
    return {"first": first.events, "retry": retry.events, "failure_raised": failure_raised, "retry_flush_count": retry_flush_count, "empty_flush_count": empty_flush_count}

def multiple():
    path = Path("multiple.db")
    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        first_id = service.place_order("multi-one", "SKU-ONE", 2)
        second_id = service.place_order("multi-two", "SKU-TWO", 5)
    connection.close()

    delivered = {}
    attempts = []
    class DeduplicatingFailOnceBus:
        def __init__(self):
            self.fail_once = True
        def publish(self, event_id, payload):
            attempts.append((event_id, payload))
            delivered.setdefault(event_id, payload)
            if self.fail_once:
                self.fail_once = False
                raise ConnectionError("broker accepted first event before disconnect")

    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        bus = DeduplicatingFailOnceBus()
        try:
            service.flush_events(bus)
            failure_raised = False
        except Exception:
            failure_raised = True
    connection.close()
    connection = sqlite3.connect(path)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        service = OrderService(connection)
        retry_flush_count = service.flush_events(bus)
        empty_flush_count = service.flush_events(bus)
    connection.close()
    return {"first_id": first_id, "second_id": second_id, "delivered": sorted(delivered.items()), "attempts": attempts, "failure_raised": failure_raised, "retry_flush_count": retry_flush_count, "empty_flush_count": empty_flush_count}

scenario = json.load(sys.stdin)["scenario"]
try:
    value = {"atomic": atomic, "idempotent": idempotent, "recovery": recovery, "stable": stable, "multiple": multiple}[scenario]()
    response = {"ok": True, "value": value}
except Exception as error:
    response = {"ok": False, "error_type": type(error).__name__, "detail": str(error)}
sys.__stdout__.write(json.dumps(response, sort_keys=True))
"""


def run_scenario(workspace: Path, scenario: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"order-{scenario}-") as raw_probe:
        probe = Path(raw_probe) / "workspace"
        shutil.copytree(workspace, probe)
        completed = run_untrusted(
            [sys.executable, "-c", worker_source],
            probe,
            CHILD_TIMEOUT_SECONDS,
            input_text=json.dumps({"scenario": scenario}),
        )
        if not completed.passed:
            if completed.output_limited:
                detail = "candidate output exceeded the one MiB limit"
            else:
                detail = (
                    completed.sandbox_error
                    or (
                        "candidate timed out"
                        if completed.timed_out
                        else completed.stderr
                    )
                    or f"candidate exited {completed.returncode}"
                )
            return {
                "ok": False,
                "infrastructure_error": detail,
            }
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            return {"ok": False, "infrastructure_error": str(error)}
        return response if isinstance(response, dict) else {"ok": False}


def response_value(response: dict[str, object]) -> dict[str, object]:
    value = response.get("value")
    return value if isinstance(value, dict) else {}


def event_pairs(value: object) -> list[list[object]]:
    if not isinstance(value, list):
        return []
    events: list[list[object]] = []
    for event in value:
        if (
            not isinstance(event, list)
            or len(event) != 2
            or not isinstance(event[1], dict)
        ):
            return []
        events.append(event)
    return events


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
results: list[dict[str, object]] = []

atomic_response = run_scenario(workspace, "atomic")
atomic_value = response_value(atomic_response)
atomic_events = event_pairs(atomic_value.get("events_after_restart"))
if atomic_value.get("write_failed") is True:
    atomic_ok = (
        atomic_response.get("ok") is True
        and isinstance(atomic_value.get("writes_seen"), int)
        and not isinstance(atomic_value.get("writes_seen"), bool)
        and atomic_value["writes_seen"] >= 2
        and atomic_value.get("orders_after_failure") == 0
        and atomic_value.get("auxiliary_rows_after_failure") == 0
        and atomic_value.get("events_after_restart") == []
        and atomic_value.get("flush_count_after_failure") == 0
    )
else:
    atomic_ok = (
        atomic_response.get("ok") is True
        and atomic_value.get("writes_seen") == 1
        and len(atomic_events) == 1
        and atomic_value.get("flush_count_after_failure") == 1
    )
results.append(
    assertion(
        "order-event-atomicity",
        atomic_ok,
        "forced failure rolled back both writes or one write retained durable intent"
        if atomic_ok
        else f"atomic facts={atomic_response!r}",
    )
)

idempotent_response = run_scenario(workspace, "idempotent")
idempotent_value = response_value(idempotent_response)
idempotent_events = event_pairs(idempotent_value.get("events"))
idempotent_ok = (
    idempotent_response.get("ok") is True
    and idempotent_value.get("first_id") == idempotent_value.get("second_id")
    and idempotent_value.get("first_id") is not None
    and idempotent_value.get("orders") == 1
    and len(idempotent_events) == 1
    and idempotent_value.get("first_flush_count") == 1
    and idempotent_value.get("second_flush_count") == 0
    and idempotent_events[0][1].get("sku") == "SKU-ONE"
    and idempotent_events[0][1].get("quantity") == 2
)
results.append(
    assertion(
        "request-idempotency",
        idempotent_ok,
        "same request returned one order and one event"
        if idempotent_ok
        else f"idempotency facts={idempotent_response!r}",
    )
)

recovery_response = run_scenario(workspace, "recovery")
recovery_value = response_value(recovery_response)
recovered_events = event_pairs(recovery_value.get("events"))
expected_payload = {
    "order_id": recovery_value.get("order_id"),
    "request_id": "restart-request",
    "sku": "SKU-RESTART",
    "quantity": 3,
}
recovery_ok = (
    recovery_response.get("ok") is True
    and len(recovered_events) == 1
    and recovery_value.get("failure_raised") is True
    and recovery_value.get("retry_flush_count") == 1
    and recovery_value.get("empty_flush_count") == 0
    and recovered_events[0][1] == expected_payload
)
stdlib_ok, stdlib_detail = standard_library_only(workspace)
recovery_ok = recovery_ok and stdlib_ok
results.append(
    assertion(
        "restart-failure-recovery",
        recovery_ok,
        "restart recovered one complete pending notification"
        if recovery_ok
        else f"recovery facts={recovery_response!r}; {stdlib_detail}",
    )
)

stable_response = run_scenario(workspace, "stable")
stable_value = response_value(stable_response)
first_events = event_pairs(stable_value.get("first"))
retry_events = event_pairs(stable_value.get("retry"))
stable_ok = (
    stable_response.get("ok") is True
    and len(first_events) == 1
    and len(retry_events) == 1
    and first_events[0] == retry_events[0]
    and stable_value.get("failure_raised") is True
    and stable_value.get("retry_flush_count") == 1
    and stable_value.get("empty_flush_count") == 0
)
results.append(
    assertion(
        "stable-event-identity",
        stable_ok,
        "ambiguous publish retry reused one stable event identity"
        if stable_ok
        else f"stable facts={stable_response!r}",
    )
)

multiple_response = run_scenario(workspace, "multiple")
multiple_value = response_value(multiple_response)
multiple_delivered = event_pairs(multiple_value.get("delivered"))
multiple_attempts = event_pairs(multiple_value.get("attempts"))
first_id = multiple_value.get("first_id")
second_id = multiple_value.get("second_id")
expected_multiple_payloads = {
    "multi-one": {
        "order_id": first_id,
        "request_id": "multi-one",
        "sku": "SKU-ONE",
        "quantity": 2,
    },
    "multi-two": {
        "order_id": second_id,
        "request_id": "multi-two",
        "sku": "SKU-TWO",
        "quantity": 5,
    },
}
multiple_ok = (
    multiple_response.get("ok") is True
    and isinstance(first_id, int)
    and not isinstance(first_id, bool)
    and isinstance(second_id, int)
    and not isinstance(second_id, bool)
    and first_id != second_id
    and multiple_value.get("failure_raised") is True
    and multiple_value.get("retry_flush_count") == 2
    and multiple_value.get("empty_flush_count") == 0
    and len(multiple_delivered) == 2
    and len({event[0] for event in multiple_delivered}) == 2
    and {event[1].get("request_id"): event[1] for event in multiple_delivered}
    == expected_multiple_payloads
    and len(multiple_attempts) == 3
    and multiple_attempts[0][0] == multiple_attempts[1][0]
)
results.append(
    assertion(
        "multiple-order-event-identity",
        multiple_ok,
        "two orders retained distinct stable event identities across an ambiguous retry"
        if multiple_ok
        else f"multiple-order facts={multiple_response!r}",
    )
)

signature_ok = False
signature_detail = "orders.py could not be inspected"
try:
    syntax_tree = ast.parse((workspace / "orders.py").read_text(encoding="utf-8"))
    classes = {
        node.name: node
        for node in syntax_tree.body
        if isinstance(node, ast.ClassDef) and node.name in {"EventBus", "OrderService"}
    }
    expected_methods = {
        "EventBus": {"publish": ["self", "event_id", "payload"]},
        "OrderService": {
            "__init__": ["self", "connection"],
            "place_order": ["self", "request_id", "sku", "quantity"],
            "flush_events": ["self", "bus"],
        },
    }
    observed = {}
    methods_valid = set(classes) == set(expected_methods)
    for class_name, expected in expected_methods.items():
        class_node = classes.get(class_name)
        methods = (
            {
                node.name: node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (not node.name.startswith("_") or node.name == "__init__")
            }
            if class_node is not None
            else {}
        )
        observed[class_name] = sorted(methods)
        methods_valid = methods_valid and set(methods) == set(expected)
        for method_name, parameter_names in expected.items():
            node = methods.get(method_name)
            if node is None:
                continue
            arguments = node.args
            methods_valid = methods_valid and (
                not arguments.posonlyargs
                and [argument.arg for argument in arguments.args] == parameter_names
                and not arguments.defaults
                and arguments.vararg is None
                and not arguments.kwonlyargs
                and arguments.kwarg is None
            )
    signature_ok = methods_valid
    signature_detail = (
        "OrderService and EventBus public method signatures match the fixture contract"
        if signature_ok
        else f"public method signatures changed: {observed}"
    )
except (OSError, SyntaxError) as error:
    signature_detail = f"signature inspection failed: {type(error).__name__}: {error}"
results.append(assertion("public-method-contract", signature_ok, signature_detail))

print(
    json.dumps(
        {"passed": all(item["passed"] for item in results), "assertions": results}
    )
)
