from __future__ import annotations

import json

import pytest

from core.calculated_signals import CalculatedSignalDefinition, CalculatedSignalError
from core.formula_library import (
    FORMULA_LIBRARY_TYPE,
    FORMULA_LIBRARY_VERSION,
    load_formula_library,
    save_formula_library,
)


def test_round_trip_preserves_definitions(tmp_path):
    definitions = [
        CalculatedSignalDefinition("Scaled", "`CH1::Message::A` * 100", "rpm"),
        CalculatedSignalDefinition("Sum", "`CH1::Message::A` + `CH1::Message::B`"),
    ]
    path = tmp_path / "library.formulas.json"

    save_formula_library(path, definitions)
    result = load_formula_library(path)

    assert result.definitions == definitions
    assert result.errors == []

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["type"] == FORMULA_LIBRARY_TYPE
    assert payload["version"] == FORMULA_LIBRARY_VERSION
    assert payload["formulas"] == [d.to_dict() for d in definitions]


def test_reads_formulas_out_of_a_real_config_json(tmp_path):
    config = {
        "version": "1.2.3",
        "measurement_path": "C:/measurements/run1.blf",
        "signals": [],
        "generated_signals": [
            {"name": "Scaled", "formula": "`CH1::Message::A` * 100", "unit": "rpm"},
        ],
    }
    path = tmp_path / "canscope_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    result = load_formula_library(path)

    assert result.errors == []
    assert result.definitions == [
        CalculatedSignalDefinition("Scaled", "`CH1::Message::A` * 100", "rpm"),
    ]


def test_malformed_json_raises_calculated_signal_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CalculatedSignalError):
        load_formula_library(path)


def test_missing_formulas_and_generated_signals_keys_raises(tmp_path):
    path = tmp_path / "unrelated.json"
    path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    with pytest.raises(CalculatedSignalError, match="No formulas found"):
        load_formula_library(path)


def test_non_object_json_raises(tmp_path):
    path = tmp_path / "array.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(CalculatedSignalError):
        load_formula_library(path)


def test_per_entry_errors_are_collected_without_aborting_the_file(tmp_path):
    path = tmp_path / "library.formulas.json"
    path.write_text(
        json.dumps(
            {
                "type": FORMULA_LIBRARY_TYPE,
                "version": FORMULA_LIBRARY_VERSION,
                "formulas": [
                    {"name": "", "formula": "`CH1::Message::A` + 1", "unit": ""},
                    {"name": "Good", "formula": "`CH1::Message::A` + 1", "unit": "V"},
                    {"name": "BadFormula", "formula": "1 + 2", "unit": ""},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_formula_library(path)

    assert [d.name for d in result.definitions] == ["Good"]
    assert len(result.errors) == 2


def test_available_keys_are_not_checked_when_loading(tmp_path):
    path = tmp_path / "library.formulas.json"
    save_formula_library(
        path,
        [CalculatedSignalDefinition("Orphan", "`CH9::Missing::Signal` + 1")],
    )

    result = load_formula_library(path)

    assert result.errors == []
    assert result.definitions[0].name == "Orphan"
