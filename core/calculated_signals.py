from __future__ import annotations

import array
import ast
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np

from core.signal_store import SignalSeries


GENERATED_GROUP = "Generate Signals"
LARGE_OUTPUT_WARNING_POINTS = 5_000_000
_REFERENCE_RE = re.compile(r"`([^`]+)`")


class CalculatedSignalError(ValueError):
    """A user-facing validation or calculation error."""


@dataclass(frozen=True, slots=True)
class CalculatedSignalDefinition:
    name: str
    formula: str
    unit: str = ""

    @property
    def key(self) -> str:
        return f"CH?::{GENERATED_GROUP}::{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "formula": self.formula, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class ParsedFormula:
    tree: ast.Expression
    references: tuple[str, ...]


_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.UAdd,
    ast.USub,
    ast.And,
    ast.Or,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
)


class _FormulaValidator(ast.NodeVisitor):
    def __init__(self, permitted_names: set[str]) -> None:
        self._permitted_names = permitted_names

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise CalculatedSignalError(
                f"Unsupported expression element: {type(node).__name__}"
            )
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in self._permitted_names:
            raise CalculatedSignalError(f"Unknown identifier: {node.id}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            return
        if not isinstance(node.value, (int, float)):
            raise CalculatedSignalError("Only numeric constants are allowed")


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise CalculatedSignalError("Signal name is required")
    if "::" in cleaned or "`" in cleaned:
        raise CalculatedSignalError("Signal name cannot contain '::' or backticks")
    return cleaned


def parse_formula(
    formula: str,
    available_keys: Iterable[str] | None = None,
) -> ParsedFormula:
    text = formula.strip()
    if not text:
        raise CalculatedSignalError("Formula is required")

    references: list[str] = []
    tokens_by_reference: dict[str, str] = {}

    def replace_reference(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if not key:
            raise CalculatedSignalError("Signal reference cannot be empty")
        token = tokens_by_reference.get(key)
        if token is None:
            token = f"__signal_{len(references)}"
            tokens_by_reference[key] = token
            references.append(key)
        return token

    normalized = _REFERENCE_RE.sub(replace_reference, text)
    if "`" in normalized:
        raise CalculatedSignalError("Signal references must use matching backticks")
    if not references:
        raise CalculatedSignalError("Formula must reference at least one measurement signal")

    normalized = re.sub(r"\bAND\b", "and", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bOR\b", "or", normalized, flags=re.IGNORECASE)
    try:
        parsed = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        detail = exc.msg or "invalid syntax"
        raise CalculatedSignalError(f"Invalid formula: {detail}") from None

    permitted_names = set(tokens_by_reference.values())
    _FormulaValidator(permitted_names).visit(parsed)

    if available_keys is not None:
        available = set(available_keys)
        unknown = [key for key in references if key not in available]
        if unknown:
            raise CalculatedSignalError(f"Measurement signal not found: {unknown[0]}")

    return ParsedFormula(parsed, tuple(references))


def estimate_output_points(
    definition: CalculatedSignalDefinition,
    source_series: Mapping[str, SignalSeries],
) -> int:
    parsed = parse_formula(definition.formula, source_series.keys())
    return sum(len(source_series[key].timestamps) for key in parsed.references)


def _aligned_inputs(
    references: Sequence[str],
    source_series: Mapping[str, SignalSeries],
) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
    timestamps: dict[str, np.ndarray] = {}
    values: dict[str, np.ndarray] = {}
    for key in references:
        series = source_series[key]
        ts = series.numpy_timestamps()
        vs = series.numpy_values()
        if ts.size == 0:
            raise CalculatedSignalError(f"Measurement signal has no samples: {key}")
        if ts.size != vs.size:
            raise CalculatedSignalError(f"Timestamp/value length mismatch: {key}")
        if ts.size > 1 and np.any(ts[1:] < ts[:-1]):
            raise CalculatedSignalError(f"Timestamps are not sorted: {key}")
        timestamps[key] = ts
        values[key] = vs

    grid = np.unique(np.concatenate([timestamps[key] for key in references]))
    first_common_time = max(float(timestamps[key][0]) for key in references)
    grid = grid[grid >= first_common_time]
    if grid.size == 0:
        raise CalculatedSignalError("Referenced signals have no overlapping time range")

    aligned: dict[str, np.ndarray] = {}
    all_inputs_finite = np.ones(grid.size, dtype=bool)
    for index, key in enumerate(references):
        ts = timestamps[key]
        idx = np.searchsorted(ts, grid, side="right") - 1
        held = values[key][idx]
        aligned[f"__signal_{index}"] = held
        all_inputs_finite &= np.isfinite(held)
    return grid, aligned, all_inputs_finite


def _evaluate_node(node: ast.AST, values: Mapping[str, np.ndarray]):
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, values)
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, values)
        return +operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, values)
        right = _evaluate_node(node.right, values)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if isinstance(node.op, ast.Add):
                return np.add(left, right)
            if isinstance(node.op, ast.Sub):
                return np.subtract(left, right)
            if isinstance(node.op, ast.Mult):
                return np.multiply(left, right)
            if isinstance(node.op, ast.Div):
                return np.divide(left, right)
    if isinstance(node, ast.BoolOp):
        operands = [np.asarray(_evaluate_node(item, values)) != 0 for item in node.values]
        reducer = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
        result = operands[0]
        for operand in operands[1:]:
            result = reducer(result, operand)
        return result
    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, values)
        combined = None
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate_node(comparator, values)
            if isinstance(operator, ast.Lt):
                current = np.less(left, right)
            elif isinstance(operator, ast.LtE):
                current = np.less_equal(left, right)
            elif isinstance(operator, ast.Gt):
                current = np.greater(left, right)
            elif isinstance(operator, ast.GtE):
                current = np.greater_equal(left, right)
            elif isinstance(operator, ast.Eq):
                current = np.equal(left, right)
            else:
                current = np.not_equal(left, right)
            combined = current if combined is None else np.logical_and(combined, current)
            left = right
        return combined
    raise CalculatedSignalError(f"Unsupported expression element: {type(node).__name__}")


def _to_double_array(data: np.ndarray) -> array.array:
    contiguous = np.ascontiguousarray(data, dtype=np.float64)
    result = array.array("d")
    result.frombytes(contiguous.tobytes())
    return result


def calculate_series(
    definition: CalculatedSignalDefinition,
    source_series: Mapping[str, SignalSeries],
) -> SignalSeries:
    name = validate_name(definition.name)
    parsed = parse_formula(definition.formula, source_series.keys())
    grid, aligned, all_inputs_finite = _aligned_inputs(parsed.references, source_series)

    result = np.asarray(_evaluate_node(parsed.tree, aligned), dtype=np.float64)
    if result.ndim == 0:
        result = np.full(grid.size, float(result), dtype=np.float64)
    else:
        result = np.broadcast_to(result, grid.shape).astype(np.float64, copy=True)
    result[~all_inputs_finite | ~np.isfinite(result)] = np.nan

    return SignalSeries(
        channel=None,
        message_name=GENERATED_GROUP,
        message_id=0,
        signal_name=name,
        unit=definition.unit.strip(),
        timestamps=_to_double_array(grid),
        values=_to_double_array(result),
    )


class CalculatedSignalManager:
    """Definitions and cached outputs, deliberately separate from SignalStore."""

    def __init__(self) -> None:
        self._definitions: dict[str, CalculatedSignalDefinition] = {}
        self._cache: dict[str, SignalSeries] = {}

    def definitions(self) -> list[CalculatedSignalDefinition]:
        return list(self._definitions.values())

    def keys(self) -> list[str]:
        return list(self._definitions.keys())

    def contains_key(self, key: str) -> bool:
        return key in self._definitions

    def definition(self, key: str) -> CalculatedSignalDefinition | None:
        return self._definitions.get(key)

    def cached_series(self, key: str) -> SignalSeries | None:
        return self._cache.get(key)

    def assert_unique_name(self, name: str, except_key: str | None = None) -> None:
        cleaned = validate_name(name)
        for key, definition in self._definitions.items():
            if key != except_key and definition.name.casefold() == cleaned.casefold():
                raise CalculatedSignalError(f"A generated signal named '{cleaned}' already exists")

    def commit(
        self,
        definition: CalculatedSignalDefinition,
        series: SignalSeries | None = None,
    ) -> None:
        self.assert_unique_name(definition.name, except_key=definition.key)
        self._definitions[definition.key] = definition
        if series is None:
            self._cache.pop(definition.key, None)
        else:
            self._cache[definition.key] = series

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)
        self._definitions.pop(key, None)

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def replace_definitions(self, payload: Iterable[object]) -> list[str]:
        self._definitions.clear()
        self._cache.clear()
        errors: list[str] = []
        for item in payload:
            try:
                if not isinstance(item, dict):
                    raise CalculatedSignalError("Definition must be an object")
                definition = CalculatedSignalDefinition(
                    name=validate_name(str(item.get("name", ""))),
                    formula=str(item.get("formula", "")),
                    unit=str(item.get("unit", "")),
                )
                parse_formula(definition.formula)
                self.commit(definition)
            except CalculatedSignalError as exc:
                errors.append(str(exc))
        return errors

    def to_config(self) -> list[dict[str, str]]:
        return [definition.to_dict() for definition in self._definitions.values()]
