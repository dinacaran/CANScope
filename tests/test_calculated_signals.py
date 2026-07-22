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
        "`CH1::Message::A` ** 2",
        "abs(`CH1::Message::A`)",
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
