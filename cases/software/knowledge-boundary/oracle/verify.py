#!/usr/bin/env python3
"""Hidden behavior and ownership-boundary oracle for order cancellation."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from untrusted_child import run_untrusted  # noqa: E402


EXPECTED_PRODUCTION_FILES = {"checkout.py", "gift_cards.py", "inventory.py"}
OWNER_FIELDS = {"_gift_cards": "GiftCards", "_inventory": "Inventory"}
KNOWN_OWNER_INTERNALS = {
    "__dict__",
    "_available",
    "_balances",
    "_charges",
    "_refunded",
    "_reservations",
}


def assertion(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


worker_source = r"""
import contextlib
import importlib
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
    sys.path.insert(0, str(workspace))
    checkout_module = importlib.import_module("checkout")
    inventory_module = sys.modules["inventory"]
    gift_cards_module = sys.modules["gift_cards"]

Checkout = checkout_module.Checkout
Inventory = inventory_module.Inventory
GiftCards = gift_cards_module.GiftCards

def cancellation():
    inventory = Inventory({"SKU-A": 10})
    cards = GiftCards({"CARD-A": 1000})
    checkout = Checkout(inventory, cards)
    placement = checkout.place_order("order-one", "SKU-A", 3, "CARD-A", 400)
    before = [inventory.available("SKU-A"), cards.balance("CARD-A")]
    first = checkout.cancel("order-one")
    restored = [inventory.available("SKU-A"), cards.balance("CARD-A")]
    second = checkout.cancel("order-one")
    repeated = [inventory.available("SKU-A"), cards.balance("CARD-A")]
    return {"placement": placement, "before": before, "first": first, "restored": restored, "second": second, "repeated": repeated}

def lifecycle():
    inventory = Inventory({"SKU-A": 10})
    cards = GiftCards({"CARD-A": 1000})
    checkout = Checkout(inventory, cards)
    checkout.place_order("reusable-order", "SKU-A", 3, "CARD-A", 400)
    checkout.cancel("reusable-order")
    try:
        inventory.reserve("reusable-order", "SKU-A", 2)
        inventory_reused = True
        inventory_error = None
    except Exception as error:
        inventory_reused = False
        inventory_error = type(error).__name__
    balance_before_retry = cards.balance("CARD-A")
    try:
        cards.charge("reusable-order", "CARD-A", 100)
        charge_reused = True
        charge_error = None
    except Exception as error:
        charge_reused = False
        charge_error = type(error).__name__
    return {"inventory_reused": inventory_reused, "inventory_error": inventory_error, "available_after_reuse": inventory.available("SKU-A"), "charge_reused": charge_reused, "charge_error": charge_error, "balance_before_retry": balance_before_retry, "balance_after_retry": cards.balance("CARD-A")}

def isolation():
    inventory = Inventory({"SKU-A": 10, "SKU-B": 8})
    cards = GiftCards({"CARD-A": 1000, "CARD-B": 900})
    checkout = Checkout(inventory, cards)
    unknown_before = [inventory.available("SKU-A"), cards.balance("CARD-A")]
    unknown = checkout.cancel("missing-order")
    unknown_after = [inventory.available("SKU-A"), cards.balance("CARD-A")]
    checkout.place_order("order-a", "SKU-A", 3, "CARD-A", 250)
    checkout.place_order("order-b", "SKU-B", 2, "CARD-B", 400)
    first = checkout.cancel("order-a")
    after_first = {"sku_a": inventory.available("SKU-A"), "sku_b": inventory.available("SKU-B"), "card_a": cards.balance("CARD-A"), "card_b": cards.balance("CARD-B")}
    second = checkout.cancel("order-b")
    after_second = {"sku_a": inventory.available("SKU-A"), "sku_b": inventory.available("SKU-B"), "card_a": cards.balance("CARD-A"), "card_b": cards.balance("CARD-B")}
    return {"unknown": unknown, "unknown_before": unknown_before, "unknown_after": unknown_after, "first": first, "after_first": after_first, "second": second, "after_second": after_second}

def partial():
    inventory = Inventory({"SKU-A": 10})
    cards = GiftCards({"EMPTY": 0, "FUNDED": 1000})
    checkout = Checkout(inventory, cards)
    try:
        checkout.place_order("reservation-only", "SKU-A", 3, "EMPTY", 100)
        placement_failed = False
    except Exception:
        placement_failed = True
    reservation_before = [inventory.available("SKU-A"), cards.balance("EMPTY")]
    reservation_cancel = checkout.cancel("reservation-only")
    reservation_after = [inventory.available("SKU-A"), cards.balance("EMPTY")]
    cards.charge("charge-only", "FUNDED", 250)
    charge_before = [inventory.available("SKU-A"), cards.balance("FUNDED")]
    charge_cancel = checkout.cancel("charge-only")
    charge_after = [inventory.available("SKU-A"), cards.balance("FUNDED")]
    return {"placement_failed": placement_failed, "reservation_cancel": reservation_cancel, "reservation_before": reservation_before, "reservation_after": reservation_after, "charge_cancel": charge_cancel, "charge_before": charge_before, "charge_after": charge_after}

def failure(action):
    try:
        action()
    except Exception as error:
        return [type(error).__name__, str(error)]
    return [None, None]

def validation():
    inventory_units = {}
    for label, units in (("zero", 0), ("negative", -1)):
        inventory = Inventory({"SKU-A": 5})
        before = inventory.available("SKU-A")
        error = failure(lambda inventory=inventory, units=units: inventory.reserve("order-invalid", "SKU-A", units))
        after = inventory.available("SKU-A")
        retry = failure(lambda inventory=inventory: inventory.reserve("order-invalid", "SKU-A", 2))
        inventory_units[label] = {"before": before, "error": error, "after": after, "retry": retry, "after_retry": inventory.available("SKU-A")}

    inventory = Inventory({"SKU-A": 5})
    inventory.reserve("order-duplicate", "SKU-A", 2)
    inventory_duplicate_before = inventory.available("SKU-A")
    inventory_duplicate_error = failure(lambda: inventory.reserve("order-duplicate", "SKU-A", 1))
    inventory_duplicate_after = inventory.available("SKU-A")

    inventory = Inventory({"SKU-A": 2})
    inventory_insufficient_before = inventory.available("SKU-A")
    inventory_insufficient_error = failure(lambda: inventory.reserve("order-insufficient", "SKU-A", 3))
    inventory_insufficient_after = inventory.available("SKU-A")
    inventory_insufficient_retry = failure(lambda: inventory.reserve("order-insufficient", "SKU-A", 1))
    inventory_insufficient_after_retry = inventory.available("SKU-A")

    card_cents = {}
    for label, cents in (("zero", 0), ("negative", -1)):
        cards = GiftCards({"CARD-A": 500})
        before = cards.balance("CARD-A")
        error = failure(lambda cards=cards, cents=cents: cards.charge("order-invalid", "CARD-A", cents))
        after = cards.balance("CARD-A")
        retry = failure(lambda cards=cards: cards.charge("order-invalid", "CARD-A", 100))
        card_cents[label] = {"before": before, "error": error, "after": after, "retry": retry, "after_retry": cards.balance("CARD-A")}

    cards = GiftCards({"CARD-A": 500})
    cards.charge("order-duplicate", "CARD-A", 100)
    card_duplicate_before = cards.balance("CARD-A")
    card_duplicate_error = failure(lambda: cards.charge("order-duplicate", "CARD-A", 50))
    card_duplicate_after = cards.balance("CARD-A")

    cards = GiftCards({"CARD-A": 100})
    card_insufficient_before = cards.balance("CARD-A")
    card_insufficient_error = failure(lambda: cards.charge("order-insufficient", "CARD-A", 150))
    card_insufficient_after = cards.balance("CARD-A")
    card_insufficient_retry = failure(lambda: cards.charge("order-insufficient", "CARD-A", 50))
    card_insufficient_after_retry = cards.balance("CARD-A")

    return {
        "inventory_units": inventory_units,
        "inventory_duplicate": {"before": inventory_duplicate_before, "error": inventory_duplicate_error, "after": inventory_duplicate_after},
        "inventory_insufficient": {"before": inventory_insufficient_before, "error": inventory_insufficient_error, "after": inventory_insufficient_after, "retry": inventory_insufficient_retry, "after_retry": inventory_insufficient_after_retry},
        "card_cents": card_cents,
        "card_duplicate": {"before": card_duplicate_before, "error": card_duplicate_error, "after": card_duplicate_after},
        "card_insufficient": {"before": card_insufficient_before, "error": card_insufficient_error, "after": card_insufficient_after, "retry": card_insufficient_retry, "after_retry": card_insufficient_after_retry},
    }

scenarios = {"cancellation": cancellation, "lifecycle": lifecycle, "isolation": isolation, "partial": partial, "validation": validation}
protocol = sys.__stdout__
for line in sys.stdin:
    request = json.loads(line)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            value = scenarios[request["scenario"]]()
        response = {"id": request["id"], "ok": True, "value": value}
    except Exception as error:
        response = {"id": request["id"], "ok": False, "error_type": type(error).__name__, "detail": str(error)}
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


def run_behavior(
    workspace: Path,
) -> tuple[dict[str, dict[str, object]], set[str], str | None]:
    requests = [
        {"id": "cancellation", "scenario": "cancellation"},
        {"id": "lifecycle", "scenario": "lifecycle"},
        {"id": "isolation", "scenario": "isolation"},
        {"id": "partial", "scenario": "partial"},
        {"id": "validation", "scenario": "validation"},
    ]
    with tempfile.TemporaryDirectory(prefix="knowledge-boundary-worker-") as raw_worker:
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
            10,
            input_text="".join(json.dumps(item) + "\n" for item in requests),
        )
    if not completed.passed:
        detail = completed.sandbox_error
        if detail is None and completed.output_limited:
            detail = "candidate output exceeded the one MiB limit"
        if detail is None and completed.timed_out:
            detail = "candidate timed out"
        if detail is None:
            detail = completed.stderr or f"candidate exited {completed.returncode}"
        return {}, set(), detail
    try:
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        meta = responses.pop()
        if set(meta) != {"meta"} or len(responses) != len(requests):
            raise ValueError(
                "candidate worker returned an incomplete protocol response"
            )
        by_id = {str(response["id"]): response for response in responses}
        if len(by_id) != len(requests):
            raise ValueError("candidate worker returned duplicate response IDs")
        raw_loaded = meta["meta"]["loaded_files"]
        if not isinstance(raw_loaded, list) or not all(
            isinstance(item, str) for item in raw_loaded
        ):
            raise TypeError("candidate worker returned invalid loaded-file metadata")
        return by_id, set(raw_loaded), None
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {}, set(), f"invalid candidate protocol: {type(error).__name__}: {error}"


def response_value(
    responses: dict[str, dict[str, object]], identifier: str
) -> dict[str, object]:
    response = responses.get(identifier, {})
    value = response.get("value")
    if response.get("ok") is True and isinstance(value, dict):
        return value
    return {}


def find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def find_method(class_node: ast.ClassDef | None, name: str) -> ast.FunctionDef | None:
    if class_node is None:
        return None
    matches = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def exact_signature(method: ast.FunctionDef | None, names: list[str]) -> bool:
    if method is None:
        return False
    arguments = method.args
    return (
        not arguments.posonlyargs
        and [argument.arg for argument in arguments.args] == names
        and not arguments.defaults
        and arguments.vararg is None
        and not arguments.kwonlyargs
        and arguments.kwarg is None
    )


def imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


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


@dataclass(frozen=True)
class AliasFacts:
    owners: frozenset[str] = frozenset()
    internals: frozenset[str] = frozenset()

    def merge(self, *others: AliasFacts) -> AliasFacts:
        owners = set(self.owners)
        internals = set(self.internals)
        for other in others:
            owners.update(other.owners)
            internals.update(other.internals)
        return AliasFacts(frozenset(owners), frozenset(internals))


EMPTY_ALIAS = AliasFacts()
MUTATING_COLLECTION_METHODS = {
    "add",
    "clear",
    "discard",
    "pop",
    "popitem",
    "remove",
    "setdefault",
    "update",
}


@dataclass(frozen=True)
class OwnerMethodEffect:
    mutates_private_state: bool = False
    returns_private_state: bool = False


def assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return assigned_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return set().union(*(assigned_names(item) for item in target.elts))
    return set()


def owner_private_value(
    node: ast.AST | None,
    aliases: set[str],
    method_names: set[str],
    returning_methods: set[str],
) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr.startswith("_")
            and node.attr not in method_names
        ):
            return True
        return owner_private_value(node.value, aliases, method_names, returning_methods)
    if isinstance(node, ast.Subscript):
        return owner_private_value(node.value, aliases, method_names, returning_methods)
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in returning_methods
        ):
            return True
        if isinstance(node.func, ast.Attribute) and owner_private_value(
            node.func.value, aliases, method_names, returning_methods
        ):
            return True
        return any(
            owner_private_value(item, aliases, method_names, returning_methods)
            for item in [*node.args, *(keyword.value for keyword in node.keywords)]
        )
    if isinstance(node, ast.IfExp):
        return owner_private_value(
            node.body, aliases, method_names, returning_methods
        ) or owner_private_value(node.orelse, aliases, method_names, returning_methods)
    if isinstance(node, ast.NamedExpr):
        return owner_private_value(node.value, aliases, method_names, returning_methods)
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
        values: list[ast.AST] = []
        if isinstance(node, ast.Dict):
            values.extend(key for key in node.keys if key is not None)
            values.extend(node.values)
        else:
            values.extend(node.elts)
        return any(
            owner_private_value(item, aliases, method_names, returning_methods)
            for item in values
        )
    return False


def owner_method_effects(class_node: ast.ClassDef) -> dict[str, OwnerMethodEffect]:
    methods = {
        node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)
    }
    method_names = set(methods)
    effects = {name: OwnerMethodEffect() for name in methods}

    while True:
        returning_methods = {
            name for name, effect in effects.items() if effect.returns_private_state
        }
        updated: dict[str, OwnerMethodEffect] = {}
        for name, method in methods.items():
            aliases: set[str] = set()
            changed = True
            while changed:
                changed = False
                for node in ast.walk(method):
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                        value = node.value
                        targets = (
                            node.targets
                            if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                        if owner_private_value(
                            value, aliases, method_names, returning_methods
                        ):
                            for target in targets:
                                before = len(aliases)
                                aliases.update(assigned_names(target))
                                changed = changed or len(aliases) != before

            called_methods: set[str] = set()
            mutates = False
            returns_private = False
            for node in ast.walk(method):
                if isinstance(node, ast.Return) and owner_private_value(
                    node.value, aliases, method_names, returning_methods
                ):
                    returns_private = True
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in methods
                    ):
                        called_methods.add(node.func.attr)
                    if (
                        node.func.attr in MUTATING_COLLECTION_METHODS
                        and owner_private_value(
                            node.func.value,
                            aliases,
                            method_names,
                            returning_methods,
                        )
                    ):
                        mutates = True
                if isinstance(
                    node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)
                ):
                    targets: list[ast.AST]
                    if isinstance(node, ast.Assign):
                        targets = list(node.targets)
                    elif isinstance(node, ast.Delete):
                        targets = list(node.targets)
                    else:
                        targets = [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            continue
                        if owner_private_value(
                            target, aliases, method_names, returning_methods
                        ):
                            mutates = True

            mutates = mutates or any(
                effects[called].mutates_private_state for called in called_methods
            )
            returns_private = returns_private or any(
                effects[called].returns_private_state
                for called in called_methods
                if any(
                    isinstance(returned.value, ast.Call)
                    and isinstance(returned.value.func, ast.Attribute)
                    and isinstance(returned.value.func.value, ast.Name)
                    and returned.value.func.value.id == "self"
                    and returned.value.func.attr == called
                    for returned in ast.walk(method)
                    if isinstance(returned, ast.Return)
                )
            )
            updated[name] = OwnerMethodEffect(mutates, returns_private)
        if updated == effects:
            return effects
        effects = updated


class CheckoutBoundaryAnalyzer:
    def __init__(
        self,
        owner_effects: dict[str, dict[str, OwnerMethodEffect]],
        checkout_class: ast.ClassDef,
    ) -> None:
        self.owner_effects = owner_effects
        self.checkout_methods = {
            node.name: node
            for node in checkout_class.body
            if isinstance(node, ast.FunctionDef)
        }
        self.active_methods: set[str] = set()
        self.public_calls: dict[str, set[str]] = {
            field: set() for field in OWNER_FIELDS
        }
        self.internal_access: set[str] = set()
        self.invalid_public_calls: set[str] = set()

    def analyze(self, method: ast.FunctionDef) -> None:
        if method.name in self.active_methods:
            return
        self.active_methods.add(method.name)
        try:
            self._block(method.body, {})
        finally:
            self.active_methods.remove(method.name)

    @staticmethod
    def _merge_environments(
        *environments: dict[str, AliasFacts],
    ) -> dict[str, AliasFacts]:
        merged: dict[str, AliasFacts] = {}
        for name in set().union(*(environment.keys() for environment in environments)):
            facts = EMPTY_ALIAS
            for environment in environments:
                facts = facts.merge(environment.get(name, EMPTY_ALIAS))
            if facts != EMPTY_ALIAS:
                merged[name] = facts
        return merged

    @staticmethod
    def _bind(
        target: ast.AST, facts: AliasFacts, environment: dict[str, AliasFacts]
    ) -> None:
        if isinstance(target, ast.Name):
            if facts == EMPTY_ALIAS:
                environment.pop(target.id, None)
            else:
                environment[target.id] = facts
        elif isinstance(target, ast.Starred):
            CheckoutBoundaryAnalyzer._bind(target.value, facts, environment)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                CheckoutBoundaryAnalyzer._bind(item, facts, environment)

    def _call_arguments(
        self, node: ast.Call, environment: dict[str, AliasFacts]
    ) -> AliasFacts:
        facts = EMPTY_ALIAS
        for item in [*node.args, *(keyword.value for keyword in node.keywords)]:
            facts = facts.merge(self._expression(item, environment))
        return facts

    def _expression(
        self, node: ast.AST | None, environment: dict[str, AliasFacts]
    ) -> AliasFacts:
        if node is None:
            return EMPTY_ALIAS
        if isinstance(node, ast.Name):
            return environment.get(node.id, EMPTY_ALIAS)
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in OWNER_FIELDS
            ):
                return AliasFacts(owners=frozenset({node.attr}))
            base = self._expression(node.value, environment)
            if base.owners:
                for field in base.owners:
                    if node.attr.startswith("_"):
                        self.internal_access.add(f"{field}.{node.attr}")
                    else:
                        self.internal_access.add(f"non-call:{field}.{node.attr}")
                if node.attr.startswith("_"):
                    return AliasFacts(internals=base.owners)
            if base.internals:
                self.internal_access.update(
                    f"aliased:{field}.{node.attr}" for field in base.internals
                )
                return AliasFacts(internals=base.internals)
            return EMPTY_ALIAS
        if isinstance(node, ast.Subscript):
            base = self._expression(node.value, environment)
            self._expression(node.slice, environment)
            if base.owners:
                self.internal_access.update(
                    f"subscript:{field}" for field in base.owners
                )
                return AliasFacts(internals=base.owners)
            if base.internals:
                self.internal_access.update(
                    f"aliased-subscript:{field}" for field in base.internals
                )
                return base
            return EMPTY_ALIAS
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr in self.checkout_methods
                ):
                    arguments = self._call_arguments(node, environment)
                    if arguments.owners or arguments.internals:
                        self.internal_access.update(
                            f"owner-escape:{field}"
                            for field in arguments.owners | arguments.internals
                        )
                    self.analyze(self.checkout_methods[node.func.attr])
                    return EMPTY_ALIAS
                receiver = self._expression(node.func.value, environment)
                arguments = self._call_arguments(node, environment)
                if arguments.owners or arguments.internals:
                    self.internal_access.update(
                        f"owner-escape:{field}"
                        for field in arguments.owners | arguments.internals
                    )
                if receiver.owners:
                    returned = EMPTY_ALIAS
                    for field in receiver.owners:
                        if node.func.attr.startswith("_"):
                            self.internal_access.add(
                                f"private-call:{field}.{node.func.attr}"
                            )
                            returned = returned.merge(
                                AliasFacts(internals=frozenset({field}))
                            )
                            continue
                        self.public_calls[field].add(node.func.attr)
                        effect = self.owner_effects[field].get(node.func.attr)
                        if effect is None:
                            self.invalid_public_calls.add(
                                f"{field}.{node.func.attr}:missing"
                            )
                        elif effect.returns_private_state:
                            self.internal_access.add(
                                f"private-return:{field}.{node.func.attr}"
                            )
                            returned = returned.merge(
                                AliasFacts(internals=frozenset({field}))
                            )
                    return returned
                if receiver.internals:
                    self.internal_access.update(
                        f"aliased-call:{field}.{node.func.attr}"
                        for field in receiver.internals
                    )
                    return AliasFacts(internals=receiver.internals)
                return EMPTY_ALIAS

            function = self._expression(node.func, environment)
            arguments = self._call_arguments(node, environment)
            escaped = function.merge(arguments)
            if escaped.owners or escaped.internals:
                label = (
                    "reflection"
                    if isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "setattr", "vars"}
                    else "owner-escape"
                )
                self.internal_access.update(
                    f"{label}:{field}" for field in escaped.owners | escaped.internals
                )
            return escaped
        if isinstance(node, ast.NamedExpr):
            facts = self._expression(node.value, environment)
            self._bind(node.target, facts, environment)
            return facts
        if isinstance(node, ast.IfExp):
            self._expression(node.test, environment)
            return self._expression(node.body, environment).merge(
                self._expression(node.orelse, environment)
            )
        if isinstance(node, ast.BoolOp):
            facts = EMPTY_ALIAS
            for value in node.values:
                facts = facts.merge(self._expression(value, environment))
            return facts
        if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
            values: list[ast.AST] = []
            if isinstance(node, ast.Dict):
                values.extend(key for key in node.keys if key is not None)
                values.extend(node.values)
            else:
                values.extend(node.elts)
            facts = EMPTY_ALIAS
            for value in values:
                facts = facts.merge(self._expression(value, environment))
            return facts
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in KNOWN_OWNER_INTERNALS:
                self.internal_access.add(f"private-literal:{node.value}")
            return EMPTY_ALIAS

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._expression(child, environment)
        return EMPTY_ALIAS

    def _target(
        self, target: ast.AST, facts: AliasFacts, environment: dict[str, AliasFacts]
    ) -> None:
        if isinstance(target, ast.Name):
            self._bind(target, facts, environment)
            return
        if isinstance(target, (ast.List, ast.Tuple, ast.Starred)):
            self._bind(target, facts, environment)
            return
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr in OWNER_FIELDS
        ):
            self.internal_access.add(f"replace:{target.attr}")
            return
        self._expression(target, environment)

    def _block(
        self, statements: list[ast.stmt], environment: dict[str, AliasFacts]
    ) -> dict[str, AliasFacts]:
        current = dict(environment)
        for statement in statements:
            if isinstance(statement, ast.Assign):
                facts = self._expression(statement.value, current)
                for target in statement.targets:
                    self._target(target, facts, current)
            elif isinstance(statement, ast.AnnAssign):
                facts = self._expression(statement.value, current)
                self._target(statement.target, facts, current)
            elif isinstance(statement, ast.AugAssign):
                self._expression(statement.value, current)
                self._target(statement.target, EMPTY_ALIAS, current)
            elif isinstance(statement, ast.Delete):
                for target in statement.targets:
                    self._target(target, EMPTY_ALIAS, current)
                    for name in assigned_names(target):
                        current.pop(name, None)
            elif isinstance(statement, ast.Expr):
                self._expression(statement.value, current)
            elif isinstance(statement, ast.Return):
                returned = self._expression(statement.value, current)
                if returned.owners or returned.internals:
                    self.internal_access.update(
                        f"owner-return:{field}"
                        for field in returned.owners | returned.internals
                    )
            elif isinstance(statement, ast.If):
                self._expression(statement.test, current)
                body = self._block(statement.body, dict(current))
                otherwise = self._block(statement.orelse, dict(current))
                current = self._merge_environments(body, otherwise)
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                iteration = self._expression(statement.iter, current)
                body_start = dict(current)
                self._bind(statement.target, iteration, body_start)
                body = self._block(statement.body, body_start)
                otherwise = self._block(statement.orelse, dict(current))
                current = self._merge_environments(current, body, otherwise)
            elif isinstance(statement, ast.While):
                self._expression(statement.test, current)
                body = self._block(statement.body, dict(current))
                otherwise = self._block(statement.orelse, dict(current))
                current = self._merge_environments(current, body, otherwise)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                with_environment = dict(current)
                for item in statement.items:
                    facts = self._expression(item.context_expr, with_environment)
                    if item.optional_vars is not None:
                        self._bind(item.optional_vars, facts, with_environment)
                current = self._block(statement.body, with_environment)
            elif isinstance(statement, ast.Try):
                paths = [self._block(statement.body, dict(current))]
                for handler in statement.handlers:
                    handler_environment = dict(current)
                    if handler.name:
                        handler_environment.pop(handler.name, None)
                    paths.append(self._block(handler.body, handler_environment))
                paths.append(self._block(statement.orelse, dict(paths[0])))
                merged = self._merge_environments(*paths)
                current = self._block(statement.finalbody, merged)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in statement.decorator_list:
                    self._expression(decorator, current)
                for default in [*statement.args.defaults, *statement.args.kw_defaults]:
                    self._expression(default, current)
                self._block(statement.body, dict(current))
                current.pop(statement.name, None)
            else:
                for child in ast.iter_child_nodes(statement):
                    if isinstance(child, ast.expr):
                        self._expression(child, current)
        return current


def owner_boundary_facts(
    trees: dict[str, ast.Module], checkout_class: ast.ClassDef | None
) -> tuple[bool, str]:
    place_order = find_method(checkout_class, "place_order")
    cancel = find_method(checkout_class, "cancel")
    inventory_class = find_class(trees["inventory.py"], "Inventory")
    cards_class = find_class(trees["gift_cards.py"], "GiftCards")
    if (
        place_order is None
        or cancel is None
        or inventory_class is None
        or cards_class is None
    ):
        return False, "required owner or Checkout coordination definition is missing"

    owner_effects = {
        "_inventory": owner_method_effects(inventory_class),
        "_gift_cards": owner_method_effects(cards_class),
    }
    analyzer = CheckoutBoundaryAnalyzer(owner_effects, checkout_class)
    analyzer.analyze(place_order)
    analyzer.analyze(cancel)
    delegated = all(
        analyzer.public_calls[field]
        and analyzer.public_calls[field] <= owner_effects[field].keys()
        and any(
            owner_effects[field][method_name].mutates_private_state
            for method_name in analyzer.public_calls[field]
        )
        for field in OWNER_FIELDS
    )
    inventory_imports = imported_roots(trees["inventory.py"])
    card_imports = imported_roots(trees["gift_cards.py"])
    owner_modules = {"checkout", "gift_cards", "inventory"}
    cross_owner_imports = sorted((inventory_imports | card_imports) & owner_modules)
    passed = (
        delegated
        and not analyzer.internal_access
        and not analyzer.invalid_public_calls
        and not cross_owner_imports
    )
    detail = (
        f"public calls={{{', '.join(f'{key}: {sorted(value)}' for key, value in sorted(analyzer.public_calls.items()))}}}, "
        f"internal access={sorted(analyzer.internal_access)}, "
        f"invalid public calls={sorted(analyzer.invalid_public_calls)}, "
        f"cross-owner imports={cross_owner_imports}"
    )
    return passed, detail


def allowed_test_artifact(path: Path, workspace: Path) -> bool:
    relative = path.relative_to(workspace)
    if not relative.parts or relative.parts[0] != "tests":
        return False
    if path.name == "__init__.py":
        return not path.read_text(encoding="utf-8").strip()
    return path.suffix == ".py" and path.name.startswith("test_")


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
results: list[dict[str, object]] = []
responses, loaded_workspace_files, behavior_error = run_behavior(workspace)
cancellation = response_value(responses, "cancellation")
isolation = response_value(responses, "isolation")
partial = response_value(responses, "partial")
validation = response_value(responses, "validation")
validation_expected = {
    "inventory_units": {
        "zero": {
            "before": 5,
            "error": ["ValueError", "units must be positive"],
            "after": 5,
            "retry": [None, None],
            "after_retry": 3,
        },
        "negative": {
            "before": 5,
            "error": ["ValueError", "units must be positive"],
            "after": 5,
            "retry": [None, None],
            "after_retry": 3,
        },
    },
    "inventory_duplicate": {
        "before": 3,
        "error": ["ValueError", "order already has a reservation"],
        "after": 3,
    },
    "inventory_insufficient": {
        "before": 2,
        "error": ["ValueError", "insufficient inventory"],
        "after": 2,
        "retry": [None, None],
        "after_retry": 1,
    },
    "card_cents": {
        "zero": {
            "before": 500,
            "error": ["ValueError", "cents must be positive"],
            "after": 500,
            "retry": [None, None],
            "after_retry": 400,
        },
        "negative": {
            "before": 500,
            "error": ["ValueError", "cents must be positive"],
            "after": 500,
            "retry": [None, None],
            "after_retry": 400,
        },
    },
    "card_duplicate": {
        "before": 400,
        "error": ["ValueError", "order was already charged"],
        "after": 400,
    },
    "card_insufficient": {
        "before": 100,
        "error": ["ValueError", "insufficient gift-card balance"],
        "after": 100,
        "retry": [None, None],
        "after_retry": 50,
    },
}
cancellation_ok = (
    behavior_error is None
    and cancellation.get("placement") is None
    and cancellation.get("before") == [7, 600]
    and cancellation.get("first") is True
    and cancellation.get("restored") == [10, 1000]
    and cancellation.get("second") is False
    and cancellation.get("repeated") == [10, 1000]
    and isolation.get("unknown") is False
    and isolation.get("unknown_before") == [10, 1000]
    and isolation.get("unknown_after") == [10, 1000]
    and isolation.get("first") is True
    and isolation.get("after_first")
    == {"sku_a": 10, "sku_b": 6, "card_a": 1000, "card_b": 500}
    and isolation.get("second") is True
    and isolation.get("after_second")
    == {"sku_a": 10, "sku_b": 8, "card_a": 1000, "card_b": 900}
    and partial.get("placement_failed") is True
    and partial.get("reservation_cancel") is False
    and partial.get("reservation_before") == [7, 0]
    and partial.get("reservation_after") == [7, 0]
    and partial.get("charge_cancel") is False
    and partial.get("charge_before") == [7, 750]
    and partial.get("charge_after") == [7, 750]
    and validation == validation_expected
)
results.append(
    assertion(
        "cancellation-restores-owned-state",
        cancellation_ok,
        "cancellation and every legacy validation failure preserved the contracted owner state"
        if cancellation_ok
        else f"cancellation={cancellation!r}, isolation={isolation!r}, partial={partial!r}, validation={validation!r}, error={behavior_error!r}",
    )
)

lifecycle = response_value(responses, "lifecycle")
lifecycle_ok = (
    behavior_error is None
    and lifecycle.get("inventory_reused") is True
    and lifecycle.get("inventory_error") is None
    and lifecycle.get("available_after_reuse") == 8
    and lifecycle.get("charge_reused") is False
    and lifecycle.get("charge_error") == "ValueError"
    and lifecycle.get("balance_before_retry") == 1000
    and lifecycle.get("balance_after_retry") == 1000
)
results.append(
    assertion(
        "independent-policy-divergence",
        lifecycle_ok,
        "released inventory identity was reusable while refunded charge identity remained historical"
        if lifecycle_ok
        else f"lifecycle={lifecycle!r}, error={behavior_error!r}",
    )
)

trees: dict[str, ast.Module] = {}
source_errors: list[str] = []
for name in sorted(EXPECTED_PRODUCTION_FILES):
    try:
        trees[name] = ast.parse((workspace / name).read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        source_errors.append(f"{name}:{type(error).__name__}")

checkout_class = (
    find_class(trees["checkout.py"], "Checkout") if not source_errors else None
)
inventory_class = (
    find_class(trees["inventory.py"], "Inventory") if not source_errors else None
)
cards_class = (
    find_class(trees["gift_cards.py"], "GiftCards") if not source_errors else None
)
signatures = {
    "Checkout.__init__": exact_signature(
        find_method(checkout_class, "__init__"), ["self", "inventory", "gift_cards"]
    ),
    "Checkout.place_order": exact_signature(
        find_method(checkout_class, "place_order"),
        ["self", "order_id", "sku", "units", "card_id", "cents"],
    ),
    "Checkout.cancel": exact_signature(
        find_method(checkout_class, "cancel"), ["self", "order_id"]
    ),
    "Inventory.__init__": exact_signature(
        find_method(inventory_class, "__init__"), ["self", "available"]
    ),
    "Inventory.reserve": exact_signature(
        find_method(inventory_class, "reserve"), ["self", "order_id", "sku", "units"]
    ),
    "Inventory.available": exact_signature(
        find_method(inventory_class, "available"), ["self", "sku"]
    ),
    "GiftCards.charge": exact_signature(
        find_method(cards_class, "charge"),
        ["self", "order_id", "card_id", "cents"],
    ),
    "GiftCards.__init__": exact_signature(
        find_method(cards_class, "__init__"), ["self", "balances"]
    ),
    "GiftCards.balance": exact_signature(
        find_method(cards_class, "balance"), ["self", "card_id"]
    ),
}
signature_ok = not source_errors and all(signatures.values())
results.append(
    assertion(
        "public-call-contract",
        signature_ok,
        "legacy signatures were preserved and cancel has exact signature (self, order_id)"
        if signature_ok
        else f"source errors={source_errors}, signatures={signatures}",
    )
)

all_files = sorted(
    path.relative_to(workspace).as_posix()
    for path in workspace.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
)
unexpected_files = sorted(
    relative
    for relative in all_files
    if relative not in EXPECTED_PRODUCTION_FILES
    and not allowed_test_artifact(workspace / relative, workspace)
)
reachable_extra = sorted(
    relative
    for relative in loaded_workspace_files - EXPECTED_PRODUCTION_FILES
    if "__pycache__" not in Path(relative).parts and not relative.endswith(".pyc")
)
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
local_modules = {
    path.stem for path in workspace.rglob("*.py") if "__pycache__" not in path.parts
}
external_imports: set[str] = set()
for tree in trees.values():
    external_imports.update(
        imported_roots(tree) - sys.stdlib_module_names - local_modules - {"__future__"}
    )
evaluator_introspection = sorted(
    f"{name}:{fact}"
    for name, tree in trees.items()
    for fact in evaluator_introspection_facts(tree)
)

if source_errors:
    boundary_ok = False
    boundary_detail = f"source errors={source_errors}"
else:
    boundary_ok, boundary_detail = owner_boundary_facts(trees, checkout_class)
scope_ok = (
    boundary_ok
    and not unexpected_files
    and not reachable_extra
    and not dependency_files
    and not external_imports
    and not evaluator_introspection
)
results.append(
    assertion(
        "owner-encapsulation",
        scope_ok,
        "Checkout delegated through separate public owner APIs with no shared production layer or reachable test helper"
        if scope_ok
        else (
            f"{boundary_detail}; unexpected files={unexpected_files}, "
            f"reachable extras={reachable_extra}, dependencies={dependency_files}, "
            f"external imports={sorted(external_imports)}, "
            f"evaluator introspection={evaluator_introspection}"
        ),
    )
)

print(
    json.dumps(
        {"passed": all(item["passed"] for item in results), "assertions": results}
    )
)
