from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from core.calculated_signals import (
    CalculatedSignalDefinition,
    CalculatedSignalError,
    parse_formula,
    validate_name,
)


FORMULA_LIBRARY_TYPE = "canscope_formulas"
FORMULA_LIBRARY_VERSION = "1"


@dataclass(frozen=True, slots=True)
class FormulaLibraryLoadResult:
    definitions: list[CalculatedSignalDefinition]
    errors: list[str]


def save_formula_library(
    path: str | Path,
    definitions: Sequence[CalculatedSignalDefinition],
) -> None:
    payload = {
        "type": FORMULA_LIBRARY_TYPE,
        "version": FORMULA_LIBRARY_VERSION,
        "formulas": [definition.to_dict() for definition in definitions],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_formula_library(path: str | Path) -> FormulaLibraryLoadResult:
    """Permissively read a formula library file OR a CANScope config file.

    A dedicated formula-library file stores its entries under "formulas";
    a full CANScope configuration stores the same shape under
    "generated_signals". Either is accepted so users can reuse formulas
    straight out of a saved configuration without loading it in full.
    """
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CalculatedSignalError(f"Could not read formula library file: {exc}") from None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CalculatedSignalError(f"Formula library file is not valid JSON: {exc}") from None

    if not isinstance(data, dict):
        raise CalculatedSignalError("Formula library file must contain a JSON object")

    if "formulas" in data:
        raw_formulas = data["formulas"]
    elif "generated_signals" in data:
        raw_formulas = data["generated_signals"]
    else:
        raise CalculatedSignalError(
            "No formulas found: expected a 'formulas' key (formula library file) "
            "or a 'generated_signals' key (CANScope configuration file)"
        )

    if not isinstance(raw_formulas, list):
        raise CalculatedSignalError("Formula list must be an array")

    definitions: list[CalculatedSignalDefinition] = []
    errors: list[str] = []
    for index, item in enumerate(raw_formulas):
        try:
            if not isinstance(item, dict):
                raise CalculatedSignalError("Formula entry must be an object")
            name = validate_name(str(item.get("name", "")))
            formula = str(item.get("formula", ""))
            unit = str(item.get("unit", ""))
            parse_formula(formula)
        except CalculatedSignalError as exc:
            errors.append(f"Entry {index + 1}: {exc}")
            continue
        definitions.append(CalculatedSignalDefinition(name=name, formula=formula, unit=unit))

    return FormulaLibraryLoadResult(definitions=definitions, errors=errors)
