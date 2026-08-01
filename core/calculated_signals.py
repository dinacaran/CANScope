from __future__ import annotations

import array
import ast
import difflib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

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
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.Mod,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Invert,
    ast.And,
    ast.Or,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
)

_BITWISE_OPS = (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)

_CONSTANTS: dict[str, float] = {"pi": np.pi, "e": np.e}

_INT64_MIN = np.iinfo(np.int64).min
_INT64_MAX = np.iinfo(np.int64).max


def _is_bitwise_binop(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, _BITWISE_OPS)


def _to_int64_with_nan_mask(x) -> tuple[np.ndarray, np.ndarray]:
    """Round-to-nearest int64 cast for bitwise ops; non-finite inputs are masked, not cast."""
    arr = np.asarray(x, dtype=np.float64)
    nan_mask = ~np.isfinite(arr)
    safe = np.where(nan_mask, 0.0, arr)
    clipped = np.clip(np.round(safe), _INT64_MIN, _INT64_MAX)
    return clipped.astype(np.int64), nan_mask


def _bitwise_binop(left, right, op: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> np.ndarray:
    left_int, left_nan = _to_int64_with_nan_mask(left)
    right_int, right_nan = _to_int64_with_nan_mask(right)
    result = op(left_int, right_int).astype(np.float64)
    return np.where(left_nan | right_nan, np.nan, result)


def _bitwise_shift(left, right, op: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> np.ndarray:
    left_int, left_nan = _to_int64_with_nan_mask(left)
    right_int, right_nan = _to_int64_with_nan_mask(right)
    in_range = (right_int >= 0) & (right_int <= 63)
    safe_shift = np.where(in_range, right_int, 0)
    result = op(left_int, safe_shift).astype(np.float64)
    return np.where(left_nan | right_nan | ~in_range, np.nan, result)


def _bitwise_invert(x) -> np.ndarray:
    """~x operates on a 64-bit signed integer, so ~0 is -1."""
    arr_int, nan_mask = _to_int64_with_nan_mask(x)
    result = np.invert(arr_int).astype(np.float64)
    return np.where(nan_mask, np.nan, result)


def _round(x, n: int = 0):
    return np.round(x, int(n))


def _clamp(x, lo, hi):
    return np.clip(x, lo, hi)


def _if(condition, then_value, else_value):
    return np.where(np.asarray(condition) != 0, then_value, else_value)


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    name: str
    min_args: int
    max_args: int
    category: str
    signature: str
    description: str
    implementation: Callable[..., object]


_FUNCTIONS: dict[str, FunctionSpec] = {}


def _register(
    name: str,
    min_args: int,
    max_args: int,
    category: str,
    signature: str,
    description: str,
    implementation: Callable[..., object],
) -> None:
    _FUNCTIONS[name.lower()] = FunctionSpec(
        name=name,
        min_args=min_args,
        max_args=max_args,
        category=category,
        signature=signature,
        description=description,
        implementation=implementation,
    )


_register("abs", 1, 1, "Math", "abs(x)", "Absolute value", np.abs)
_register("sqrt", 1, 1, "Math", "sqrt(x)", "Square root; negative input gives NaN", np.sqrt)
_register("exp", 1, 1, "Math", "exp(x)", "e raised to the power of x", np.exp)
_register(
    "log", 1, 1, "Math", "log(x)",
    "Natural logarithm; zero or negative input gives NaN", np.log,
)
_register(
    "log10", 1, 1, "Math", "log10(x)",
    "Base-10 logarithm; zero or negative input gives NaN", np.log10,
)
_register(
    "log2", 1, 1, "Math", "log2(x)",
    "Base-2 logarithm; zero or negative input gives NaN", np.log2,
)
_register("pow", 2, 2, "Math", "pow(a, b)", "a raised to the power of b", np.power)
_register("sign", 1, 1, "Math", "sign(x)", "-1, 0, or 1 depending on the sign of x", np.sign)
_register(
    "mod", 2, 2, "Math", "mod(a, b)",
    "Remainder of a / b; the result takes the sign of b", np.mod,
)

_register(
    "round", 1, 2, "Rounding", "round(x) or round(x, n)",
    "Round to n decimal places (0 if omitted)", _round,
)
_register("floor", 1, 1, "Rounding", "floor(x)", "Round down to the nearest integer", np.floor)
_register("ceil", 1, 1, "Rounding", "ceil(x)", "Round up to the nearest integer", np.ceil)
_register("trunc", 1, 1, "Rounding", "trunc(x)", "Round toward zero", np.trunc)

_register("min", 2, 2, "Selection", "min(a, b)", "The smaller of a and b, elementwise", np.minimum)
_register("max", 2, 2, "Selection", "max(a, b)", "The larger of a and b, elementwise", np.maximum)
_register(
    "clamp", 3, 3, "Selection", "clamp(x, lo, hi)", "x limited to the range [lo, hi]", _clamp,
)

_register("sin", 1, 1, "Trigonometry", "sin(x)", "Sine, x in radians", np.sin)
_register("cos", 1, 1, "Trigonometry", "cos(x)", "Cosine, x in radians", np.cos)
_register("tan", 1, 1, "Trigonometry", "tan(x)", "Tangent, x in radians", np.tan)
_register(
    "asin", 1, 1, "Trigonometry", "asin(x)",
    "Arcsine in radians; |x| > 1 gives NaN", np.arcsin,
)
_register(
    "acos", 1, 1, "Trigonometry", "acos(x)",
    "Arccosine in radians; |x| > 1 gives NaN", np.arccos,
)
_register("atan", 1, 1, "Trigonometry", "atan(x)", "Arctangent in radians", np.arctan)
_register(
    "atan2", 2, 2, "Trigonometry", "atan2(y, x)",
    "Angle in radians of the point (x, y)", np.arctan2,
)
_register("degrees", 1, 1, "Trigonometry", "degrees(x)", "Convert radians to degrees", np.degrees)
_register("radians", 1, 1, "Trigonometry", "radians(x)", "Convert degrees to radians", np.radians)

_register(
    "IF", 3, 3, "Conditional", "IF(condition, then_value, else_value)",
    "then_value where condition is nonzero, else_value elsewhere", _if,
)


def _arity_text(spec: FunctionSpec) -> str:
    if spec.min_args == spec.max_args:
        count = spec.min_args
        return f"exactly {count} argument{'s' if count != 1 else ''}"
    return f"{spec.min_args} to {spec.max_args} arguments"


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
        if node.id in _CONSTANTS:
            return
        if node.id not in self._permitted_names:
            raise CalculatedSignalError(f"Unknown identifier: {node.id}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            return
        if not isinstance(node.value, (int, float)):
            raise CalculatedSignalError("Only numeric constants are allowed")

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name):
            raise CalculatedSignalError("Only plain function calls are supported, e.g. sqrt(x)")
        if node.keywords:
            raise CalculatedSignalError(f"{node.func.id}() does not accept keyword arguments")
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise CalculatedSignalError(f"{node.func.id}() does not accept starred arguments")
        spec = _FUNCTIONS.get(node.func.id.lower())
        if spec is None:
            matches = difflib.get_close_matches(node.func.id.lower(), _FUNCTIONS.keys(), n=1)
            suggestion = f" Did you mean {matches[0]}?" if matches else ""
            raise CalculatedSignalError(f"Unknown function: {node.func.id}.{suggestion}")
        arg_count = len(node.args)
        if not (spec.min_args <= arg_count <= spec.max_args):
            raise CalculatedSignalError(
                f"{spec.name}() expects {_arity_text(spec)}: {spec.signature}. Got {arg_count}."
            )
        # Only the arguments are visited: falling through to visit_Name on the
        # function name itself would reject it, since a function name is not a
        # signal token.
        for arg in node.args:
            self.visit(arg)

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(_is_bitwise_binop(comparator) for comparator in node.comparators):
            raise CalculatedSignalError("Parenthesise bitwise operands: write (a > 0) & (b > 0)")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, _BITWISE_OPS):
            # `&`/`|`/`^`/`<<`/`>>` bind tighter than comparisons in Python, so an
            # unparenthesized mix silently reparses into something else, e.g.
            # `a > 0 & b > 0` becomes the chained comparison `a > (0 & b) > 0`
            # (caught above, in visit_Compare). A bitwise operand that is a
            # Compare on exactly one side is that same mistake with partial
            # parentheses, e.g. `(a > 0) & b > 0`. Both-or-neither is
            # unambiguous — it is the documented fix — so it is left alone.
            if isinstance(node.left, ast.Compare) != isinstance(node.right, ast.Compare):
                raise CalculatedSignalError(
                    "Parenthesise bitwise operands: write (a > 0) & (b > 0)"
                )
        self.generic_visit(node)


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise CalculatedSignalError("Signal name is required")
    if "::" in cleaned or "`" in cleaned:
        raise CalculatedSignalError("Signal name cannot contain '::' or backticks")
    return cleaned


def formula_references(formula: str) -> list[str]:
    """Backtick-delimited keys in a formula, even one that does not parse yet."""
    return [match.strip() for match in _REFERENCE_RE.findall(formula)]


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
    normalized = re.sub(r"\bNOT\b", "not", normalized, flags=re.IGNORECASE)
    if re.search(r"\bif\s*\(", normalized):
        raise CalculatedSignalError(
            "Use IF(condition, then_value, else_value) — the conditional must be uppercase IF"
        )
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
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        return values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        spec = _FUNCTIONS[node.func.id.lower()]
        args = [_evaluate_node(arg, values) for arg in node.args]
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return spec.implementation(*args)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, values)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Not):
                return np.logical_not(np.asarray(operand) != 0)
            return _bitwise_invert(operand)  # ast.Invert
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
            if isinstance(node.op, ast.Pow):
                return np.power(left, right)
            if isinstance(node.op, ast.Mod):
                return np.mod(left, right)
            if isinstance(node.op, ast.BitAnd):
                return _bitwise_binop(left, right, np.bitwise_and)
            if isinstance(node.op, ast.BitOr):
                return _bitwise_binop(left, right, np.bitwise_or)
            if isinstance(node.op, ast.BitXor):
                return _bitwise_binop(left, right, np.bitwise_xor)
            if isinstance(node.op, ast.LShift):
                return _bitwise_shift(left, right, np.left_shift)
            if isinstance(node.op, ast.RShift):
                return _bitwise_shift(left, right, np.right_shift)
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


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: list[str]
    skipped: list[str]
    errors: list[str]


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

    def _generated_references(
        self,
        definition: CalculatedSignalDefinition,
        known: Mapping[str, CalculatedSignalDefinition],
    ) -> list[str]:
        try:
            parsed = parse_formula(definition.formula)
        except CalculatedSignalError:
            return []
        references: list[str] = []
        for key in parsed.references:
            if key in known and key not in references:
                references.append(key)
        return references

    def dependencies_of(self, key: str) -> list[str]:
        """Generated signals this one reads directly; measurement inputs are excluded."""
        definition = self._definitions.get(key)
        if definition is None:
            return []
        return self._generated_references(definition, self._definitions)

    def dependents_of(self, key: str, transitive: bool = True) -> list[str]:
        direct = [
            other
            for other in self._definitions
            if other != key and key in self.dependencies_of(other)
        ]
        if not transitive:
            return direct
        found: list[str] = []
        pending = list(direct)
        while pending:
            current = pending.pop(0)
            if current in found:
                continue
            found.append(current)
            pending.extend(self.dependents_of(current, transitive=False))
        return found

    def resolution_order(self, key: str) -> list[str]:
        """Generated signals to compute before `key`, dependencies first and `key` last."""
        order: list[str] = []
        visiting: set[str] = set()

        def visit(current: str) -> None:
            if current in order or current in visiting:
                return
            visiting.add(current)
            for dependency in self.dependencies_of(current):
                visit(dependency)
            visiting.discard(current)
            order.append(current)

        if key in self._definitions:
            visit(key)
        return order

    def detect_cycle(self, definition: CalculatedSignalDefinition) -> list[str] | None:
        """Cycle path (keys, start repeated at the end) that committing this would create."""
        candidate = dict(self._definitions)
        candidate[definition.key] = definition
        path: list[str] = []
        on_path: set[str] = set()
        settled: set[str] = set()

        def visit(current: str) -> list[str] | None:
            if current in on_path:
                return path[path.index(current):] + [current]
            if current in settled:
                return None
            path.append(current)
            on_path.add(current)
            for dependency in self._generated_references(candidate[current], candidate):
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle
            path.pop()
            on_path.discard(current)
            settled.add(current)
            return None

        return visit(definition.key)

    def _cycle_name(self, key: str) -> str:
        known = self._definitions.get(key)
        return known.name if known is not None else key.rsplit("::", 1)[-1]

    def commit(
        self,
        definition: CalculatedSignalDefinition,
        series: SignalSeries | None = None,
    ) -> None:
        self.assert_unique_name(definition.name, except_key=definition.key)
        cycle = self.detect_cycle(definition)
        if cycle is not None:
            names = [
                definition.name if key == definition.key else self._cycle_name(key)
                for key in cycle
            ]
            raise CalculatedSignalError(f"Circular reference: {' -> '.join(names)}")
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

    def invalidate_series(self, key: str) -> None:
        self._cache.pop(key, None)

    def _key_for_name(self, name: str) -> str | None:
        cleaned = name.casefold()
        for key, definition in self._definitions.items():
            if definition.name.casefold() == cleaned:
                return key
        return None

    def import_definitions(
        self,
        definitions: Iterable[CalculatedSignalDefinition],
        *,
        overwrite: bool = False,
    ) -> ImportReport:
        """Merge definitions in place; unlike replace_definitions(), never clears existing state."""
        imported: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for definition in definitions:
            try:
                name = validate_name(definition.name)
                parse_formula(definition.formula)
            except CalculatedSignalError as exc:
                errors.append(str(exc))
                continue

            existing_key = self._key_for_name(name)
            if existing_key is not None and not overwrite:
                skipped.append(name)
                continue

            cleaned = CalculatedSignalDefinition(
                name=name, formula=definition.formula, unit=definition.unit
            )
            replaced = self._definitions.get(existing_key) if existing_key else None
            replaced_series = self._cache.get(existing_key) if existing_key else None
            try:
                if existing_key is not None:
                    self._cache.pop(existing_key, None)
                    self._definitions.pop(existing_key, None)
                self.commit(cleaned)
                imported.append(name)
            except CalculatedSignalError as exc:
                # Put the original back: a rejected replacement must not delete it.
                if replaced is not None:
                    self._definitions[existing_key] = replaced
                    if replaced_series is not None:
                        self._cache[existing_key] = replaced_series
                errors.append(str(exc))
        return ImportReport(imported=imported, skipped=skipped, errors=errors)

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
