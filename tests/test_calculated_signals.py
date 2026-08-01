from __future__ import annotations

import array

import numpy as np
import pytest

from core.calculated_signals import (
    CalculatedSignalDefinition,
    CalculatedSignalError,
    CalculatedSignalManager,
    calculate_series,
    estimate_output_points,
    parse_formula,
)
from core.signal_store import SignalSeries


def _series(key: str, timestamps, values, unit: str = "") -> SignalSeries:
    channel_text, message, signal = key.split("::", 2)
    channel = None if channel_text == "CH?" else int(channel_text[2:])
    return SignalSeries(
        channel=channel,
        message_name=message,
        message_id=1,
        signal_name=signal,
        unit=unit,
        timestamps=array.array("d", timestamps),
        values=array.array("d", values),
    )


@pytest.fixture()
def sources():
    a_key = "CH1::Message::A"
    b_key = "CH1::Message::B"
    return {
        a_key: _series(a_key, [0.0, 1.0, 2.0], [2.0, 4.0, 8.0]),
        b_key: _series(b_key, [0.0, 1.0, 2.0], [1.0, 4.0, 10.0]),
    }


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("`CH1::Message::A` + 2", [4.0, 6.0, 10.0]),
        ("`CH1::Message::A` - 2", [0.0, 2.0, 6.0]),
        ("`CH1::Message::A` * 2", [4.0, 8.0, 16.0]),
        ("`CH1::Message::A` / 2", [1.0, 2.0, 4.0]),
        ("`CH1::Message::A` ** 2", [4.0, 16.0, 64.0]),
        ("-`CH1::Message::A` + +1", [-1.0, -3.0, -7.0]),
        ("(`CH1::Message::A` + 2) * 3", [12.0, 18.0, 30.0]),
        ("`CH1::Message::A` < `CH1::Message::B`", [0.0, 0.0, 1.0]),
        ("`CH1::Message::A` <= `CH1::Message::B`", [0.0, 1.0, 1.0]),
        ("`CH1::Message::A` > `CH1::Message::B`", [1.0, 0.0, 0.0]),
        ("`CH1::Message::A` >= `CH1::Message::B`", [1.0, 1.0, 0.0]),
        ("`CH1::Message::A` == `CH1::Message::B`", [0.0, 1.0, 0.0]),
        ("`CH1::Message::A` != `CH1::Message::B`", [1.0, 0.0, 1.0]),
        ("`CH1::Message::A` AND `CH1::Message::B`", [1.0, 1.0, 1.0]),
        ("(`CH1::Message::A` < 4) OR (`CH1::Message::B` == 10)", [1.0, 0.0, 1.0]),
    ],
)
def test_supported_operators(expression, expected, sources):
    result = calculate_series(CalculatedSignalDefinition("Result", expression), sources)
    np.testing.assert_allclose(result.numpy_values(), expected)


def test_union_timebase_uses_zero_order_hold_after_all_sources_start():
    a_key = "CH1::M::A"
    b_key = "CH1::M::B"
    sources = {
        a_key: _series(a_key, [0.0, 2.0], [1.0, 3.0]),
        b_key: _series(b_key, [1.0, 3.0], [10.0, 20.0]),
    }
    definition = CalculatedSignalDefinition("Sum", f"`{a_key}` + `{b_key}`", "V")

    result = calculate_series(definition, sources)

    np.testing.assert_allclose(result.numpy_timestamps(), [1.0, 2.0, 3.0])
    np.testing.assert_allclose(result.numpy_values(), [11.0, 13.0, 23.0])
    assert result.key == "CH?::Generate Signals::Sum"
    assert result.unit == "V"
    assert estimate_output_points(definition, sources) == 4


def test_division_by_zero_and_nonfinite_inputs_become_nan():
    a_key = "CH1::M::A"
    b_key = "CH1::M::B"
    sources = {
        a_key: _series(a_key, [0.0, 1.0, 2.0], [2.0, np.inf, 4.0]),
        b_key: _series(b_key, [0.0, 1.0, 2.0], [1.0, 1.0, 0.0]),
    }

    result = calculate_series(
        CalculatedSignalDefinition("Ratio", f"`{a_key}` / `{b_key}`"), sources
    ).numpy_values()

    assert result[0] == 2.0
    assert np.isnan(result[1])
    assert np.isnan(result[2])


@pytest.mark.parametrize(
    "formula",
    [
        "",
        "1 + 2",
        "`CH1::Message::A`[0]",
        "`CH1::Message::A`.shape",
        "__import__('os')",
        "`CH1::Message::A` + 'text'",
        "`CH1::Message::A",
    ],
)
def test_rejects_unsupported_or_unsafe_expressions(formula, sources):
    with pytest.raises(CalculatedSignalError):
        parse_formula(formula, sources)


def test_rejects_unknown_measurement_reference(sources):
    with pytest.raises(CalculatedSignalError, match="not found"):
        parse_formula("`CH9::Missing::Signal` + 1", sources)


def _calc1(expression: str, x_values: list[float]) -> np.ndarray:
    """Evaluate a formula that references one signal, written as `{x}`, at these values."""
    key = "CH1::M::X"
    formula_sources = {key: _series(key, list(range(len(x_values))), x_values)}
    formula = expression.replace("{x}", f"`{key}`")
    result = calculate_series(CalculatedSignalDefinition("R", formula), formula_sources)
    return result.numpy_values()


@pytest.mark.parametrize(
    ("formula", "x_values", "expected"),
    [
        ("abs({x})", [-3.0, 3.0], [3.0, 3.0]),
        ("sqrt({x})", [4.0, 9.0], [2.0, 3.0]),
        ("exp({x})", [0.0, 1.0], [1.0, np.e]),
        ("log({x})", [1.0, np.e], [0.0, 1.0]),
        ("log10({x})", [1.0, 100.0], [0.0, 2.0]),
        ("log2({x})", [1.0, 8.0], [0.0, 3.0]),
        ("pow({x}, 3)", [2.0, 3.0], [8.0, 27.0]),
        ("sign({x})", [-5.0, 0.0, 5.0], [-1.0, 0.0, 1.0]),
        ("mod({x}, 3)", [7.0, -7.0], [1.0, 2.0]),
        ("round({x})", [1.4, 1.5, 2.5], [1.0, 2.0, 2.0]),
        ("round({x}, 2)", [3.14159, 2.71828], [3.14, 2.72]),
        ("floor({x})", [1.9, -1.1], [1.0, -2.0]),
        ("ceil({x})", [1.1, -1.9], [2.0, -1.0]),
        ("trunc({x})", [1.9, -1.9], [1.0, -1.0]),
        ("min({x}, 5)", [3.0, 7.0], [3.0, 5.0]),
        ("max({x}, 5)", [3.0, 7.0], [5.0, 7.0]),
        ("clamp({x}, 0, 10)", [-5.0, 5.0, 15.0], [0.0, 5.0, 10.0]),
        ("sin({x})", [0.0, np.pi / 2], [0.0, 1.0]),
        ("cos({x})", [0.0, np.pi], [1.0, -1.0]),
        ("tan({x})", [0.0], [0.0]),
        ("asin({x})", [0.0, 1.0], [0.0, np.pi / 2]),
        ("acos({x})", [1.0, 0.0], [0.0, np.pi / 2]),
        ("atan({x})", [0.0, 1.0], [0.0, np.pi / 4]),
        ("atan2({x}, 1)", [0.0, 1.0], [0.0, np.pi / 4]),
        ("degrees({x})", [np.pi], [180.0]),
        ("radians({x})", [180.0], [np.pi]),
    ],
)
def test_function_examples(formula, x_values, expected):
    np.testing.assert_allclose(_calc1(formula, x_values), expected, atol=1e-9)


def test_if_selects_elementwise_from_a_comparison_condition(sources):
    formula = "IF(`CH1::Message::A` > 3, 100, -100)"
    result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
    np.testing.assert_allclose(result.numpy_values(), [-100.0, 100.0, 100.0])


def test_if_selects_elementwise_with_signal_branches(sources):
    formula = "IF(`CH1::Message::A` > `CH1::Message::B`, `CH1::Message::A`, `CH1::Message::B`)"
    result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
    np.testing.assert_allclose(result.numpy_values(), [2.0, 4.0, 10.0])


def test_not_is_case_insensitive_and_elementwise(sources):
    for keyword in ("NOT", "not", "Not"):
        formula = f"{keyword} (`CH1::Message::A` > 3)"
        result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
        np.testing.assert_allclose(result.numpy_values(), [1.0, 0.0, 0.0])


def test_bitwise_operators_on_integral_values():
    key = "CH1::M::X"
    bitwise_sources = {key: _series(key, [0.0, 1.0, 2.0], [6.0, 12.0, 5.0])}
    x = f"`{key}`"
    cases = [
        (f"{x} & 3", [2.0, 0.0, 1.0]),
        (f"{x} | 1", [7.0, 13.0, 5.0]),
        (f"{x} ^ 5", [3.0, 9.0, 0.0]),
        (f"{x} << 2", [24.0, 48.0, 20.0]),
        (f"{x} >> 1", [3.0, 6.0, 2.0]),
        (f"~{x}", [-7.0, -13.0, -6.0]),
    ]
    for formula, expected in cases:
        result = calculate_series(CalculatedSignalDefinition("R", formula), bitwise_sources)
        np.testing.assert_allclose(result.numpy_values(), expected)


def test_bitwise_precedence_trap_is_rejected(sources):
    with pytest.raises(CalculatedSignalError, match="Parenthesise"):
        parse_formula("`CH1::Message::A` > 0 & `CH1::Message::B` > 0", sources)


def test_bitwise_partial_parenthesization_is_also_rejected(sources):
    with pytest.raises(CalculatedSignalError, match="Parenthesise"):
        parse_formula("(`CH1::Message::A` > 0) & `CH1::Message::B` > 0", sources)


def test_bitwise_of_two_parenthesized_comparisons_is_allowed(sources):
    formula = "(`CH1::Message::A` > 0) & (`CH1::Message::B` > 0)"
    result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
    np.testing.assert_allclose(result.numpy_values(), [1.0, 1.0, 1.0])


def test_bitwise_and_then_compare_against_zero_is_allowed(sources):
    # (a & mask) > 0 is the common "is this bit set" pattern and must stay legal.
    formula = "(`CH1::Message::A` & 3) > 0"
    result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
    np.testing.assert_allclose(result.numpy_values(), [1.0, 0.0, 0.0])


def test_bitwise_binop_masks_nan_and_inf_produced_by_the_formula():
    key = "CH1::M::X"
    nan_sources = {key: _series(key, [0.0, 1.0], [5.0, 0.0])}
    formula = f"(`{key}` / 0) & 3"
    result = calculate_series(CalculatedSignalDefinition("R", formula), nan_sources)
    values = result.numpy_values()
    assert np.isnan(values[0])  # 5 / 0 -> inf
    assert np.isnan(values[1])  # 0 / 0 -> nan


def test_bitwise_shift_out_of_range_gives_nan_instead_of_raising():
    key = "CH1::M::X"
    shift_sources = {key: _series(key, [0.0, 1.0, 2.0], [1.0, 1.0, 1.0])}
    x = f"`{key}`"
    too_far = calculate_series(CalculatedSignalDefinition("R", f"{x} << 64"), shift_sources)
    assert np.all(np.isnan(too_far.numpy_values()))
    negative = calculate_series(CalculatedSignalDefinition("R", f"{x} << -1"), shift_sources)
    assert np.all(np.isnan(negative.numpy_values()))


@pytest.mark.parametrize(
    ("formula", "x_value"),
    [
        ("sqrt({x})", -1.0),
        ("log({x})", 0.0),
        ("log({x})", -1.0),
        ("{x} ** -1", 0.0),
        ("pow({x}, -1)", 0.0),
        ("asin({x})", 2.0),
        ("acos({x})", -2.0),
    ],
)
def test_domain_errors_produce_nan_not_exceptions(formula, x_value):
    result = _calc1(formula, [x_value])
    assert np.isnan(result[0])


@pytest.mark.parametrize(
    ("formula", "x_value", "expected"),
    [
        ("{x} % 3", -7.0, 2.0),
        ("mod({x}, 3)", -7.0, 2.0),
        ("{x} % -3", 7.0, -2.0),
    ],
)
def test_mod_matches_python_sign_convention(formula, x_value, expected):
    np.testing.assert_allclose(_calc1(formula, [x_value]), [expected])


def test_unknown_function_suggests_close_match(sources):
    with pytest.raises(
        CalculatedSignalError, match=r"Unknown function: srqt\. Did you mean sqrt\?"
    ):
        parse_formula("srqt(`CH1::Message::A`)", sources)


def test_unknown_function_without_a_close_match_has_no_suggestion(sources):
    with pytest.raises(CalculatedSignalError, match=r"^Unknown function: zzzzzz\.$"):
        parse_formula("zzzzzz(`CH1::Message::A`)", sources)


def test_wrong_arity_rejected_at_parse_time_not_evaluation_time(sources):
    with pytest.raises(CalculatedSignalError, match="sqrt"):
        parse_formula("sqrt(`CH1::Message::A`, 2)", sources)
    with pytest.raises(CalculatedSignalError, match="pow"):
        parse_formula("pow(`CH1::Message::A`)", sources)
    with pytest.raises(CalculatedSignalError, match="clamp"):
        parse_formula("clamp(`CH1::Message::A`, 0)", sources)


@pytest.mark.parametrize(
    "formula",
    [
        "sqrt(x=`CH1::Message::A`)",
        "sqrt(*[`CH1::Message::A`])",
        "np.sqrt(`CH1::Message::A`)",
        "(lambda v: v)(`CH1::Message::A`)",
        "`CH1::Message::A` + (lambda: 1)",
    ],
)
def test_rejects_keyword_starred_attribute_and_lambda_calls(formula, sources):
    with pytest.raises(CalculatedSignalError):
        parse_formula(formula, sources)


def test_lowercase_if_gives_the_specific_uppercase_message(sources):
    with pytest.raises(CalculatedSignalError, match="uppercase IF"):
        parse_formula("if(`CH1::Message::A` > 0, 1, 0)", sources)


def test_mixed_case_if_call_is_still_accepted(sources):
    formula = "If(`CH1::Message::A` > 0, 1, 0)"
    result = calculate_series(CalculatedSignalDefinition("R", formula), sources)
    np.testing.assert_allclose(result.numpy_values(), [1.0, 1.0, 1.0])


def test_constants_pi_and_e_resolve(sources):
    pi_result = calculate_series(
        CalculatedSignalDefinition("R", "`CH1::Message::A` * 0 + pi"), sources
    )
    np.testing.assert_allclose(pi_result.numpy_values(), [np.pi, np.pi, np.pi])

    e_result = calculate_series(
        CalculatedSignalDefinition("R", "`CH1::Message::A` * 0 + e"), sources
    )
    np.testing.assert_allclose(e_result.numpy_values(), [np.e, np.e, np.e])


def test_signal_token_named_similarly_to_a_constant_is_unaffected():
    key = "CH1::Message::epsilon"
    epsilon_sources = {key: _series(key, [0.0, 1.0], [10.0, 20.0])}
    result = calculate_series(CalculatedSignalDefinition("R", f"`{key}` + e"), epsilon_sources)
    np.testing.assert_allclose(result.numpy_values(), [10.0 + np.e, 20.0 + np.e])


def test_help_text_and_function_registry_cannot_drift_apart():
    from core.calculated_signals import _FUNCTIONS
    from gui.calculated_signal_dialog import _formula_help_examples, _formula_help_text

    full_text = _formula_help_text()
    for spec in _FUNCTIONS.values():
        assert spec.signature in full_text, f"{spec.name}'s signature is missing from help text"

    checked = 0
    for _heading, examples in _formula_help_examples():
        for entry in examples:
            for line in entry.splitlines():
                stripped = line.strip()
                if "Example:" in stripped:
                    expression = stripped.split("Example:", 1)[1].strip()
                elif ":" in stripped:
                    expression = stripped.split(":", 1)[1].strip()
                else:
                    continue
                if not expression:
                    continue
                parse_formula(expression)
                checked += 1
    assert checked > 0


def test_manager_persists_definitions_but_not_cached_samples(sources):
    manager = CalculatedSignalManager()
    definition = CalculatedSignalDefinition("Scaled", "`CH1::Message::A` * 100", "rpm")
    calculated = calculate_series(definition, sources)
    manager.commit(definition, calculated)

    payload = manager.to_config()
    restored = CalculatedSignalManager()
    assert restored.replace_definitions(payload) == []

    assert restored.definition(definition.key) == definition
    assert restored.cached_series(definition.key) is None
    with pytest.raises(CalculatedSignalError, match="already exists"):
        restored.commit(CalculatedSignalDefinition("scaled", definition.formula))


def test_import_definitions_merges_without_clearing_existing(sources):
    manager = CalculatedSignalManager()
    existing = CalculatedSignalDefinition("Existing", "`CH1::Message::A` + 1", "V")
    existing_series = calculate_series(existing, sources)
    manager.commit(existing, existing_series)

    incoming = [CalculatedSignalDefinition("NewOne", "`CH1::Message::A` * 2", "V")]
    report = manager.import_definitions(incoming)

    assert report.imported == ["NewOne"]
    assert report.skipped == []
    assert report.errors == []
    assert manager.definition(existing.key) == existing
    assert manager.cached_series(existing.key) is existing_series
    assert manager.definition("CH?::Generate Signals::NewOne") == incoming[0]


def test_import_definitions_skips_case_insensitive_collision_by_default(sources):
    manager = CalculatedSignalManager()
    existing = CalculatedSignalDefinition("Speed", "`CH1::Message::A` + 1", "V")
    manager.commit(existing)

    colliding = CalculatedSignalDefinition("speed", "`CH1::Message::A` * 2", "rpm")
    report = manager.import_definitions([colliding])

    assert report.imported == []
    assert report.skipped == ["speed"]
    assert report.errors == []
    assert manager.definition(existing.key) == existing


def test_import_definitions_overwrite_replaces_definition_and_drops_cache(sources):
    manager = CalculatedSignalManager()
    existing = CalculatedSignalDefinition("Speed", "`CH1::Message::A` + 1", "V")
    manager.commit(existing, calculate_series(existing, sources))

    replacement = CalculatedSignalDefinition("Speed", "`CH1::Message::A` * 2", "rpm")
    report = manager.import_definitions([replacement], overwrite=True)

    assert report.imported == ["Speed"]
    assert report.skipped == []
    assert manager.definition(replacement.key) == replacement
    assert manager.cached_series(replacement.key) is None


def test_import_definitions_overwrite_with_different_name_case_replaces_original(sources):
    manager = CalculatedSignalManager()
    existing = CalculatedSignalDefinition("Speed", "`CH1::Message::A` + 1", "V")
    manager.commit(existing, calculate_series(existing, sources))

    replacement = CalculatedSignalDefinition("speed", "`CH1::Message::A` * 2", "rpm")
    report = manager.import_definitions([replacement], overwrite=True)

    assert report.imported == ["speed"]
    assert manager.definition(existing.key) is None
    assert manager.definition(replacement.key) == replacement
    assert manager.cached_series(replacement.key) is None
    assert manager.keys() == [replacement.key]


def test_import_definitions_collects_per_entry_errors_without_aborting(sources):
    manager = CalculatedSignalManager()
    bad = CalculatedSignalDefinition("Bad", "1 + 2", "")
    good = CalculatedSignalDefinition("Good", "`CH1::Message::A` + 1", "V")

    report = manager.import_definitions([bad, good])

    assert report.imported == ["Good"]
    assert report.skipped == []
    assert len(report.errors) == 1


def _generated(name: str) -> str:
    return f"`CH?::Generate Signals::{name}`"


@pytest.fixture()
def chained():
    manager = CalculatedSignalManager()
    a = CalculatedSignalDefinition("A", "`CH1::Message::A` * 2")
    b = CalculatedSignalDefinition("B", f"{_generated('A')} + 1")
    c = CalculatedSignalDefinition("C", f"{_generated('B')} / 2")
    for definition in (a, b, c):
        manager.commit(definition)
    return manager, a, b, c


def test_dependencies_of_lists_only_generated_inputs(chained):
    manager, a, b, c = chained

    assert manager.dependencies_of(a.key) == []
    assert manager.dependencies_of(b.key) == [a.key]
    assert manager.dependencies_of(c.key) == [b.key]
    assert manager.dependencies_of("CH?::Generate Signals::Unknown") == []


def test_dependents_of_direct_and_transitive(chained):
    manager, a, b, c = chained

    assert manager.dependents_of(a.key, transitive=False) == [b.key]
    assert manager.dependents_of(a.key) == [b.key, c.key]
    assert manager.dependents_of(c.key) == []


def test_resolution_order_places_dependencies_first(chained):
    manager, a, b, c = chained

    assert manager.resolution_order(c.key) == [a.key, b.key, c.key]
    assert manager.resolution_order(a.key) == [a.key]


def test_resolution_order_visits_a_diamond_dependency_once():
    manager = CalculatedSignalManager()
    c = CalculatedSignalDefinition("C", "`CH1::Message::A` * 2")
    a = CalculatedSignalDefinition("A", f"{_generated('C')} + 1")
    b = CalculatedSignalDefinition("B", f"{_generated('C')} - 1")
    d = CalculatedSignalDefinition("D", f"{_generated('A')} + {_generated('B')}")
    for definition in (c, a, b, d):
        manager.commit(definition)

    order = manager.resolution_order(d.key)

    assert order.count(c.key) == 1
    assert order[0] == c.key
    assert order[-1] == d.key
    assert sorted(order[1:3]) == sorted([a.key, b.key])


def test_commit_rejects_self_reference():
    manager = CalculatedSignalManager()

    with pytest.raises(CalculatedSignalError, match="Circular reference: Loop -> Loop"):
        manager.commit(CalculatedSignalDefinition("Loop", f"{_generated('Loop')} + 1"))


def test_commit_rejects_direct_cycle():
    manager = CalculatedSignalManager()
    manager.commit(CalculatedSignalDefinition("A", f"{_generated('B')} + 1"))

    with pytest.raises(CalculatedSignalError, match="Circular reference: B -> A -> B"):
        manager.commit(CalculatedSignalDefinition("B", f"{_generated('A')} + 1"))

    assert manager.definition("CH?::Generate Signals::B") is None


def test_commit_rejects_indirect_cycle():
    manager = CalculatedSignalManager()
    manager.commit(CalculatedSignalDefinition("A", f"{_generated('C')} + 1"))
    manager.commit(CalculatedSignalDefinition("B", f"{_generated('A')} + 1"))

    with pytest.raises(CalculatedSignalError, match="Circular reference"):
        manager.commit(CalculatedSignalDefinition("C", f"{_generated('B')} + 1"))


def test_replace_definitions_reports_the_cyclic_entry_and_keeps_the_rest():
    manager = CalculatedSignalManager()

    errors = manager.replace_definitions([
        {"name": "A", "formula": f"{_generated('B')} + 1", "unit": ""},
        {"name": "B", "formula": f"{_generated('A')} + 1", "unit": ""},
        {"name": "Plain", "formula": "`CH1::Message::A` + 1", "unit": ""},
    ])

    assert len(errors) == 1
    assert "Circular reference" in errors[0]
    assert [definition.name for definition in manager.definitions()] == ["A", "Plain"]


def test_rejected_overwrite_keeps_the_original_definition(sources):
    manager = CalculatedSignalManager()
    original = CalculatedSignalDefinition("A", "`CH1::Message::A` * 2")
    manager.commit(original, calculate_series(original, sources))
    manager.commit(CalculatedSignalDefinition("B", f"{_generated('A')} + 1"))

    report = manager.import_definitions(
        [CalculatedSignalDefinition("A", f"{_generated('B')} + 1")], overwrite=True
    )

    assert report.imported == []
    assert len(report.errors) == 1
    assert manager.definition(original.key) == original
    assert manager.cached_series(original.key) is not None


def test_invalidate_series_drops_only_the_named_cache_entry(sources):
    manager = CalculatedSignalManager()
    a = CalculatedSignalDefinition("A", "`CH1::Message::A` * 2")
    b = CalculatedSignalDefinition("B", "`CH1::Message::B` * 2")
    manager.commit(a, calculate_series(a, sources))
    manager.commit(b, calculate_series(b, sources))

    manager.invalidate_series(a.key)

    assert manager.cached_series(a.key) is None
    assert manager.cached_series(b.key) is not None


def test_chained_definition_calculates_from_a_cached_generated_series(sources):
    a = CalculatedSignalDefinition("A", "`CH1::Message::A` * 2")
    a_series = calculate_series(a, sources)
    b = CalculatedSignalDefinition("B", f"{_generated('A')} + 1")

    result = calculate_series(b, {a.key: a_series})

    np.testing.assert_allclose(result.numpy_values(), [5.0, 9.0, 17.0])


def test_invalid_config_definition_is_skipped():
    manager = CalculatedSignalManager()
    errors = manager.replace_definitions([
        {"name": "Bad", "formula": "1 + 2", "unit": ""},
        {"name": "Good", "formula": "`CH1::M::S` + 1", "unit": "V"},
    ])

    assert len(errors) == 1
    assert [definition.name for definition in manager.definitions()] == ["Good"]


def test_generated_series_uses_existing_export_path(tmp_path, sources):
    from core.export import ExportService

    definition = CalculatedSignalDefinition("Exported", "`CH1::Message::A` + 1", "V")
    generated = calculate_series(definition, sources)
    output = tmp_path / "generated.csv"

    ExportService.export_series_to_csv([generated], output)

    rows = output.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "Time,Exported"
    assert rows[1:] == ["0.0,3.0", "1.0,5.0", "2.0,9.0"]
