"""VL-04 test-only reason-path audit and mixed active-plan regression evidence.

The dedicated module keeps the bounded AST checker and its mutation tests apart
from the history builder. This is not a general Python data-flow analyzer.
"""

import ast
import hashlib
import inspect
from dataclasses import replace

import pytest
from test_feasibility_live_http_admission_history import (
    LIMITS,
    History,
    target_page,
)

from feasibility import http_contract as hc
from feasibility import live_http_admission_history as h
from feasibility import live_http_budget_v2 as v2


class ReasonAuditError(AssertionError):
    pass


# Audited grammar, not a schema discovered from the running interpreter.
# '*' is a required list, '?' an optional value. Only the absent 3.11
# FunctionDef/ClassDef.type_params is equivalent to the 3.12+ empty list.
AST_FIELDS = {
    "Add": "",
    "And": "",
    "BitOr": "",
    "Eq": "",
    "Gt": "",
    "In": "",
    "Is": "",
    "IsNot": "",
    "Load": "",
    "Mult": "",
    "Not": "",
    "NotEq": "",
    "Or": "",
    "Store": "",
    "Continue": "",
    "Pass": "",
    "AnnAssign": "target annotation value? simple",
    "Assign": "targets* value type_comment?",
    "Attribute": "value attr ctx",
    "AugAssign": "target op value",
    "BinOp": "left op right",
    "BoolOp": "op values*",
    "Call": "func args* keywords*",
    "ClassDef": "name bases* keywords* body* decorator_list* type_params*",
    "Compare": "left ops* comparators*",
    "Constant": "value kind?",
    "Dict": "keys* values*",
    "DictComp": "key value generators*",
    "Expr": "value",
    "For": "target iter body* orelse* type_comment?",
    "FunctionDef": "name args body* decorator_list* returns? type_comment? type_params*",
    "GeneratorExp": "elt generators*",
    "If": "test body* orelse*",
    "IfExp": "test body orelse",
    "ImportFrom": "module? names* level",
    "List": "elts* ctx",
    "ListComp": "elt generators*",
    "Module": "body* type_ignores*",
    "Name": "id ctx",
    "Raise": "exc? cause?",
    "Return": "value?",
    "Set": "elts*",
    "Starred": "value ctx",
    "Subscript": "value slice ctx",
    "Tuple": "elts* ctx",
    "UnaryOp": "op operand",
    "alias": "name asname?",
    "arg": "arg annotation? type_comment?",
    "arguments": "posonlyargs* args* vararg? kwonlyargs* kw_defaults* kwarg? defaults*",
    "comprehension": "target iter ifs* is_async",
    "keyword": "arg? value",
}
AST_LOCATIONS = {"lineno", "col_offset", "end_lineno", "end_col_offset"}


def canonical_ast(node):
    """Pinned canonical spelling; no ast.dump/show_empty/runtime schema defaults.

    Known empty lists/optional None have one representation. Constant(None),
    list elements of None, and all nonempty fields remain significant. Unknown
    nodes/fields require review, including future interpreter schema additions.
    The spelling preserves the 21 previously reviewed 3.13 fingerprint literals.
    """
    if isinstance(node, ast.AST):
        name = type(node).__name__
        if name not in AST_FIELDS or type(node) is not getattr(ast, name, None):
            raise ReasonAuditError("unsupported AST node: " + name)
        specs = AST_FIELDS[name].split()
        fields = tuple(s.rstrip("*?") for s in specs)
        legacy = fields[:-1] if name in {"FunctionDef", "ClassDef"} else fields
        if node._fields not in (fields, legacy):
            raise ReasonAuditError("unsupported AST fields: " + name)
        if set(vars(node)) - set(fields) - AST_LOCATIONS:
            raise ReasonAuditError("unsupported AST attributes: " + name)
        if set(node._attributes) - AST_LOCATIONS:
            raise ReasonAuditError("unsupported AST metadata: " + name)
        parts = []
        for spec, field in zip(specs, fields, strict=True):
            missing = not hasattr(node, field)
            if field == "type_params":
                value = getattr(node, field, [])
                if type(value) is not list or value:
                    raise ReasonAuditError("unsupported AST type_params: re-audit")
                continue
            if missing and not spec.endswith("?"):
                raise ReasonAuditError("missing AST field: " + name + "." + field)
            value = getattr(node, field, None)
            if spec.endswith("*"):
                if type(value) is not list:
                    raise ReasonAuditError("invalid AST list: " + name + "." + field)
                if not value:
                    continue
            if spec.endswith("?") and value is None:
                continue
            parts.append(field + "=" + canonical_ast(value))
        return name + "(" + ", ".join(parts) + ")"
    if type(node) is list:
        return "[" + ", ".join(canonical_ast(x) for x in node) + "]"
    if node is None or node is Ellipsis or type(node) in {str, int, bool}:
        return repr(node)
    raise ReasonAuditError("unsupported AST value: " + type(node).__name__)


def literal_reasons(node, *, saved_propagation=False):
    """Only explicit literals/containers/unions and the audited saved edge."""
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return {node.value}
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return set().union(*(literal_reasons(x) for x in node.elts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return literal_reasons(node.left) | literal_reasons(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"set", "tuple", "list", "sorted"} and not node.keywords:
            if not node.args and node.func.id in {"set", "tuple", "list"}:
                return set()
            if len(node.args) == 1:
                return literal_reasons(
                    node.args[0], saved_propagation=saved_propagation
                )
    if saved_propagation and canonical_ast(node) == canonical_ast(
        ast.parse("saved.reasons", mode="eval").body
    ):
        return set()  # Its origin is _saved; checked separately and dynamically.
    raise ReasonAuditError("unsupported emission shape")


def collect_budget_reasons(source):
    """Inspect the two audited sinks, without importing capacity/exception text.

    The separate AST guard rejects *every* unreviewed structural path change,
    including a newly introduced helper, alias, decorator or module assignment.
    """
    tree = ast.parse(source)
    emitted = set()
    sinks = {
        "_saved": ("SavedCompatibilityV2", 3),
        "budget_result": ("BudgetResultV2", 11),
    }
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        if fn.name not in sinks:
            continue
        sink_name, reason_position = sinks[fn.name]
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    pairs = [(target, node.value)]
                    if isinstance(target, ast.Tuple) and isinstance(
                        node.value, ast.Tuple
                    ):
                        pairs = list(zip(target.elts, node.value.elts, strict=True))
                    for lhs, rhs in pairs:
                        if isinstance(lhs, ast.Name) and lhs.id == "reasons":
                            emitted |= literal_reasons(
                                rhs, saved_propagation=fn.name == "budget_result"
                            )
                        elif any(
                            isinstance(x, ast.Name) and x.id == "reasons"
                            for x in ast.walk(rhs)
                        ):
                            raise ReasonAuditError("unsupported emission shape: alias")
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "reasons":
                    if not isinstance(node.op, ast.BitOr):
                        raise ReasonAuditError("unsupported emission shape: mutation")
                    emitted |= literal_reasons(node.value)
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                if node.func.value.id == "reasons":
                    if (
                        node.func.attr not in {"add", "update"}
                        or len(node.args) != 1
                        or node.keywords
                    ):
                        raise ReasonAuditError("unsupported emission shape: method")
                    emitted |= literal_reasons(node.args[0])
            if isinstance(node.func, ast.Name) and node.func.id == sink_name:
                kw = {x.arg: x.value for x in node.keywords}
                value = kw.get("reasons")
                if value is None and len(node.args) > reason_position:
                    value = node.args[reason_position]
                if value is None:
                    raise ReasonAuditError("unsupported emission shape: sink")
                if canonical_ast(value) != canonical_ast(
                    ast.parse("tuple(sorted(reasons))", mode="eval").body
                ):
                    emitted |= literal_reasons(value)
    return emitted


# Manually reviewed at d307fad: _saved -> SavedCompatibilityV2.reasons;
# budget_result -> set(saved.reasons) -> BudgetResultV2.reasons;
# saved_compatibility_v2 delegates to _saved. Capacity and require exceptions
# are separate categories. Fingerprints are PER AST UNIT (not whole-source SHA).
# Any update requires a reason-path diff audit, including classes/imports/helpers;
# never regenerate these literals merely to make a changed module pass.
AUDITED_AST = {
    "Expr:0": "6a52e7f31053731035530f55d8aba5376c6f059d7afba2db5aa48cc6b59df7f9",
    "ImportFrom:1": "5a46818000a4ba65186172bb0cb9a4b5dbc848a8a97f892692ffc6df7d8c6953",
    "ImportFrom:2": "adb35153d7663ae4db4404eafa7c10c4e620f9cb4399f929dbc546d0511a3c67",
    "ImportFrom:3": "273cbf002df4b79d467a0eef1372325a72496acdb3eb068e79e5453835e42c30",
    "ImportFrom:4": "36f4aa802fe18a96c14c210fc6cc72c88611e6e38eb6d234f7fd7d654c5c477c",
    "ImportFrom:5": "2b6e43cbca224f336aa952cc433b501acbfef8055c26be9d80f011854e813703",
    "ImportFrom:6": "3f692f875096b16b16a39eaf29d1c3e7977a7c0c8fff2394b9b5f1f4cc8fc15f",
    "ImportFrom:7": "ec06225e5935c78b819ae8d60511397875219269a83f982f6c84045732d73220",
    "Assign:8": "09e0abbac6bd82932e6b6459df7ba221f4660f93b8451aedff839fdc78bb027f",
    "require": "8811b88b407ce20997b7c6f41c5346c61c011f1972bb6b784824bf0988600a2a",
    "total": "11131df1d84f41bf0376c6b377a7048cb70e5f4a2d70c5d53c9ed3a1ba5f19d5",
    "BudgetPolicyV2": "520efff6b4c6923668b91c36ea5903419e1ffa926e42bc35b4bb0c461376b492",
    "SavedCompatibilityV2": "68d496188d87c291752e3fb73882e5733afdf3f6b04a601f705b3a6dfe41a224",
    "_saved": "8d4568d17a5934691053aa35faff33ec8bf37d6104a0aeae76aad6bc454c1f69",
    "saved_compatibility_v2": "35d231b2feef36b7cfdfcc8b1f712d4fb9a0ba810753f298713b92524e9bbc75",
    "BudgetEvidenceV2": "14b89eb9578c72af6190fcd54f20981032924359edf0c101db3ba43b0db5a63d",
    "BudgetResultV2": "2285d5fdca47fe820fff2b9f558794a799bdba8a6cee72d7b3ac87a941ef093a",
    "budget_result": "2aca2606651469a438479ca8c1da7c9d37ca000f67f0cad5f4af65d9708a6fcc",
    "PlanCapacityV2": "ec44c4c2f4545a74fcdc75d6c8e303767e8cf9449b256dd86d2bc7ea96bb9b33",
    "CapacityAssessmentV2": "d2b1522de9738dc7beb6decefddf1cfb375911da59475bc6812b22e3e3ec2bce",
    "assess_capacity_v2": "c14b9291b0ea5266227b53ba51042ed99bb027d2123ba7ad51823d8656ecd6e3",
}


def audit_budget_module(source):
    emitted = collect_budget_reasons(source)
    if emitted != h.BUDGET_REASON_REGISTRY:
        raise ReasonAuditError("unregistered or missing reason")
    actual = {}
    for i, node in enumerate(ast.parse(source).body):
        key = getattr(node, "name", f"{type(node).__name__}:{i}")
        if key in actual:
            raise ReasonAuditError("unsupported emission shape: duplicate definition")
        shape = canonical_ast(node)
        actual[key] = hashlib.sha256(shape.encode()).hexdigest()
    if actual != AUDITED_AST:
        changed = sorted(
            k
            for k in actual.keys() | AUDITED_AST.keys()
            if actual.get(k) != AUDITED_AST.get(k)
        )
        raise ReasonAuditError(
            "unsupported emission shape: re-audit " + ",".join(changed)
        )
    return emitted


def test_VL_reason_sink_paths_and_change_guard():
    assert audit_budget_module(inspect.getsource(v2)) == h.BUDGET_REASON_REGISTRY


@pytest.mark.parametrize("unit", tuple(AUDITED_AST))
def test_VL_canonical_fingerprint_all_21_units(unit, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("canonical audit must not call ast.dump")

    monkeypatch.setattr(ast, "dump", forbidden)
    nodes = ast.parse(inspect.getsource(v2)).body
    assert len(nodes) == len(AUDITED_AST) == 21
    by_name = {
        getattr(n, "name", f"{type(n).__name__}:{i}"): n for i, n in enumerate(nodes)
    }
    assert (
        hashlib.sha256(canonical_ast(by_name[unit]).encode()).hexdigest()
        == AUDITED_AST[unit]
    )
    assert audit_budget_module(inspect.getsource(v2)) == h.BUDGET_REASON_REGISTRY


def test_VL_canonical_empty_optional_and_type_params(monkeypatch):
    fn = ast.parse("def f():\n    return None\n").body[0]
    expected = "FunctionDef(name='f', args=arguments(), body=[Return(value=Constant(value=None))])"
    assert canonical_ast(fn) == expected
    # Model the known schema delta explicitly; not a substitute for 3.11 execution.
    fields = tuple(f for f in fn._fields if f != "type_params")
    monkeypatch.setattr(ast.FunctionDef, "_fields", fields)
    if hasattr(fn, "type_params"):
        del fn.type_params
    assert canonical_ast(fn) == expected
    monkeypatch.setattr(ast.FunctionDef, "_fields", (*fields, "type_params"))
    fn.type_params = []
    if hasattr(fn, "returns"):
        del fn.returns
    assert canonical_ast(fn) == expected
    fn.returns = ast.Name(id="int", ctx=ast.Load())
    assert canonical_ast(fn) != expected
    fn.type_params = [ast.Name(id="T", ctx=ast.Load())]
    with pytest.raises(ReasonAuditError, match="unsupported AST type_params"):
        canonical_ast(fn)
    # None in a list is a value, not an omitted optional field.
    assert canonical_ast([None]) == "[None]"
    assert canonical_ast(ast.Constant(value=None)) == "Constant(value=None)"
    assert canonical_ast(ast.parse("if x:\n pass\n").body[0]) != canonical_ast(
        ast.parse("if x:\n pass\nelse:\n pass\n").body[0]
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("node", "unsupported AST node"),
        ("field", "unsupported AST fields"),
        ("attribute", "unsupported AST attributes"),
        ("metadata", "unsupported AST metadata"),
        ("missing", "missing AST field"),
        ("list", "invalid AST list"),
        ("value", "unsupported AST value"),
    ],
)
def test_VL_unknown_ast_structure_is_rejected(monkeypatch, mutation, reason):
    node = ast.parse("f()").body[0].value
    if mutation == "node":

        class FutureNode(ast.AST):
            _fields = ()

        node = FutureNode()
    elif mutation == "field":
        node._fields = (*node._fields, "future")
    elif mutation == "attribute":
        node.future = None
    elif mutation == "metadata":
        monkeypatch.setattr(ast.Call, "_attributes", (*node._attributes, "future"))
    elif mutation == "missing":
        del node.args
    elif mutation == "list":
        node.args = None
    else:
        node.func = object()
    with pytest.raises(ReasonAuditError, match=reason):
        canonical_ast(node)


def test_VL_comments_do_not_change_fingerprints():
    source = inspect.getsource(v2)
    source = "# review-only comment\n" + source.replace(
        'reasons.add("open_unsettled")', 'reasons.add("open_unsettled")  # unchanged'
    )
    assert audit_budget_module(source) == h.BUDGET_REASON_REGISTRY


@pytest.mark.parametrize(
    "expression",
    [
        'getattr(reasons, "add")("new")',
        'setattr(holder, "r", reasons)',
    ],
)
def test_VL_reflective_reason_paths_require_review(expression):
    source = inspect.getsource(v2).replace('reasons.add("open_unsettled")', expression)
    # Preserve the original emitter so registry equality alone cannot catch it.
    source = source.replace(expression, 'reasons.add("open_unsettled"); ' + expression)
    with pytest.raises(ReasonAuditError, match="unsupported emission shape: re-audit"):
        audit_budget_module(source)


@pytest.mark.parametrize(
    "expression",
    [
        'reasons.add("new")',
        'reasons.update({"new"})',
        'reasons |= {"new"}',
        'reasons = {"new"}',
        'reasons = ("new",)',
        'reasons = ["new"]',
        'reasons.update({"new"} | {"sticky_overrun"})',
    ],
)
def test_VL_literal_emission_mutants_are_collected_and_rejected(expression):
    source = inspect.getsource(v2).replace('reasons.add("open_unsettled")', expression)
    assert "new" in collect_budget_reasons(source)
    with pytest.raises(ReasonAuditError, match="unregistered"):
        audit_budget_module(source)


@pytest.mark.parametrize(
    "expression",
    [
        "reasons.update(helper())",
        'reasons.add("new" + suffix)',
        "reasons.update({x for x in inputs})",
        'alias = reasons; alias.add("new")',
        'emit = reasons.add; emit("new")',
    ],
)
def test_VL_dynamic_and_alias_mutants_fail_closed(expression):
    source = inspect.getsource(v2).replace('reasons.add("open_unsettled")', expression)
    with pytest.raises(ReasonAuditError, match="unsupported emission shape"):
        audit_budget_module(source)


@pytest.mark.parametrize("sink", ["saved", "budget"])
def test_VL_direct_constructor_reason_mutant(sink):
    source = inspect.getsource(v2)
    if sink == "saved":
        source = source.replace(
            "SavedCompatibilityV2(not reasons, saved, pages, tuple(sorted(reasons)))",
            'SavedCompatibilityV2(not reasons, saved, pages, ("new",))',
        )
    else:
        source = source.replace(
            "tuple(evidence),\n        tuple(sorted(reasons)),",
            'tuple(evidence),\n        ("new",),',
        )
    assert "new" in collect_budget_reasons(source)
    with pytest.raises(ReasonAuditError, match="unregistered"):
        audit_budget_module(source)


def test_VL_new_helper_and_direct_return_paths_require_review():
    source = inspect.getsource(v2)
    mutants = [
        source + '\ndef helper():\n    return {"new"}\n',
        source.replace(
            "saved = _saved(projection, attempts, peers, measurements)",
            "return helper()\n    saved = _saved(projection, attempts, peers, measurements)",
        ),
        source.replace("return _saved(", "return replacement("),
        source + "\nBudgetResultV2 = replacement\n",
    ]
    for mutant in mutants:
        with pytest.raises(ReasonAuditError, match="unsupported emission shape"):
            audit_budget_module(mutant)


def test_VL_capacity_reason_is_separate_but_structural_change_is_guarded():
    source = inspect.getsource(v2).replace(
        'reasons.add("plan_retained_capacity")', 'reasons.add("capacity_only_new")'
    )
    assert collect_budget_reasons(source) == h.BUDGET_REASON_REGISTRY
    with pytest.raises(ReasonAuditError, match="re-audit assess_capacity_v2"):
        audit_budget_module(source)


@pytest.mark.parametrize("peer", [False, True])
@pytest.mark.parametrize("origin", ["budget", "saved"])
def test_VL_unknown_reason_target_peer_and_both_sinks(monkeypatch, peer, origin):
    f = History(second_plan=True)
    target = f.c.plan.plan_sha
    original_budget, original_saved = v2.budget_result, v2._saved
    seen = []
    current_plan = None

    def budget(plan, *args):
        nonlocal current_plan
        current_plan = plan.sha256
        result = original_budget(plan, *args)
        if (plan.sha256 != target) is peer and origin == "saved":
            assert "new_unregistered_reason" in result.saved.reasons
            assert "new_unregistered_reason" in result.reasons
        if (plan.sha256 != target) is peer and origin == "budget":
            result = replace(
                result, reasons=(*result.reasons, "new_unregistered_reason")
            )
        return result

    def saved(projection, *args):
        result = original_saved(projection, *args)
        # Set by the outer budget call, not guessed from an empty projection.
        if (current_plan != target) is peer and origin == "saved":
            result = replace(
                result, compatible=False, reasons=("new_unregistered_reason",)
            )
            seen.append(result)
        return result

    monkeypatch.setattr(v2, "budget_result", budget)
    monkeypatch.setattr(v2, "_saved", saved)
    results = [
        fn(f.pool(), limits=LIMITS)
        for fn in (h.verify_history_reference, h.verify_history_incremental)
    ]
    assert results[0].to_bytes() == results[1].to_bytes()
    assert not results[0].historical_admissions_valid
    assert results[0].reason == "budget_v2_unknown_budget_reason"
    with pytest.raises(hc.HttpContractError, match="unknown_budget_reason"):
        h.build_admission_v2(f.pool(), limits=LIMITS, cut=f.cut(), page=target_page(f))
    if origin == "saved":
        assert seen


def mixed_result(f, reason=None, position=None):
    results = [
        fn(f.pool(), limits=LIMITS)
        for fn in (h.verify_history_reference, h.verify_history_incremental)
    ]
    assert results[0].to_bytes() == results[1].to_bytes()
    r = results[0]
    assert r.historical_admissions_valid is (reason is None), r.reason
    assert r.reason == reason
    assert r.first_violation == position
    return r


def mixed_history(rounds=1, *, swap=False):
    f = History(second_plan=True)
    if swap:
        f.switch_plan()
    for n in range(rounds):
        for _ in range(2):
            page = n // 3
            f.begin(page, None if page == 0 else "second").settle(
                status=(503, 429, 200)[n % 3],
                size=1 if n % 3 == 2 else 0,
                digest=hashlib.sha256(f"mixed-{f.index}".encode()).hexdigest(),
                next_cursor="second" if n == 2 else None,
            )
            f.switch_plan()
    return f


@pytest.mark.parametrize("swap", [False, True])
def test_VL_mixed_active_control_and_full_binding_allowances(swap):
    f = mixed_history(2, swap=swap)
    assert {p.catalog_state for p in f.pool().plans} == {"enrolled"}
    assert len({m.binding.plan_sha for m in f.measurements}) == 2
    assert len({m.binding for m in f.measurements}) == 4
    mixed_result(f)
    # Synthetic path should make an identical core for a legal next reservation.
    f.begin(illegal=True).settle(status=200, size=1)
    r = mixed_result(f)
    assert len(r.checks) == 5
    assert sum(len(b.evidence) for b in r.final_budgets) == 5


@pytest.mark.parametrize("swap", [False, True])
def test_VL_A_404_after_interleaved_B_is_exact_first_violation(swap):
    f = mixed_history(swap=swap)
    f.begin().settle(status=404)
    mixed_result(f)
    f.begin(illegal=True).settle(status=200, size=1)
    mixed_result(f, "budget_v2_previous_attempt_not_retryable", 3)


@pytest.mark.parametrize("swap", [False, True])
def test_VL_B_illegal_retry_survives_later_A_success(swap):
    f = mixed_history(swap=swap)
    f.switch_plan()
    f.begin().settle(status=404).begin(illegal=True).settle(status=200, size=1)
    f.switch_plan()
    # Only the prior B retry is illegal; A's response cannot erase its position.
    f.begin(illegal=True).settle(
        status=200, size=2, digest=hashlib.sha256(b"AA").hexdigest()
    )
    mixed_result(f, "budget_v2_previous_attempt_not_retryable", 3)


@pytest.mark.parametrize("swap", [False, True])
def test_VL_peer_exhaustion_with_both_plans_active_is_legal(swap):
    f = History(second_plan=True)
    if swap:
        f.switch_plan()
    f.begin().settle(transfer=500)
    f.switch_plan()
    f.begin().settle()
    f.switch_plan()
    f.begin().settle(status=429, transfer=500)
    f.switch_plan()
    f.begin().settle(status=200, size=1)
    r = mixed_result(f)
    assert {p.catalog_state for p in f.pool().plans} == {"enrolled"}
    assert any("byte_budget_exhausted" in b.reasons for b in r.final_budgets)


@pytest.mark.parametrize("swap", [False, True])
def test_VL_peer_sticky_overrun_rejects_inserted_B_reservation(swap):
    f = mixed_history(swap=swap)
    f.begin().settle(transfer=501)
    mixed_result(f)
    f.switch_plan()
    f.begin(illegal=True)
    mixed_result(f, "budget_v2_sticky_overrun", 3)


@pytest.mark.parametrize("capacity", [160, 159])
def test_VL_stopped_retained_exact_and_one_byte_over(capacity):
    f = History(second_plan=True, account_capacity=capacity, account_cumulative=100000)
    f.begin().settle(size=60)
    f.catalog_states[f.c.plan.plan_sha] = "stopped"
    f.switch_plan()
    mixed_result(f)
    f.begin(illegal=capacity == 159)
    if capacity == 160:
        r = mixed_result(f)
        assert r.checks[-1].assessment.capacity.retained_with_reservation == 160
    else:
        mixed_result(f, "budget_v2_account_retained_capacity", 1)


@pytest.mark.parametrize("swap", [False, True])
def test_VL_long_mixed_history_last_retry_has_exact_reason_and_position(swap):
    f = mixed_history(3, swap=swap)
    f.begin(1, "second").settle(status=404)
    f.switch_plan()
    f.begin(1, "second").settle(status=503)
    f.switch_plan()
    mixed_result(f)
    f.begin(1, "second", illegal=True)
    mixed_result(f, "budget_v2_previous_attempt_not_retryable", 8)
