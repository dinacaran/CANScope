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
TIME_SIGNAL_KEY = "Time"
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
    implementation: Callable[..., object] | None
    temporal: bool = False


_FUNCTIONS: dict[str, FunctionSpec] = {}


def _register(
    name: str,
    min_args: int,
    max_args: int,
    category: str,
    signature: str,
    description: str,
    implementation: Callable[..., object] | None,
    *,
    temporal: bool = False,
) -> None:
    _FUNCTIONS[name.lower()] = FunctionSpec(
        name=name,
        min_args=min_args,
        max_args=max_args,
        category=category,
        signature=signature,
        description=description,
        implementation=implementation,
        temporal=temporal,
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

_register(
    "lag", 2, 2, "Time", "lag(signal, samples)",
    "Value from an earlier source sample; missing history gives NaN",
    None, temporal=True,
)
_register(
    "delay", 2, 2, "Time", "delay(signal, seconds)",
    "Value at t - seconds using zero-order hold; missing history gives NaN",
    None, temporal=True,
)
_register(
    "rolling_sum", 2, 2, "Time", "rolling_sum(signal, samples)",
    "Sum of the current and previous source samples; incomplete windows give NaN",
    None, temporal=True,
)
_register(
    "integral", 1, 1, "Time", "integral(signal)",
    "Cumulative time integral using zero-order hold",
    None, temporal=True,
)
_register(
    "derivative", 1, 1, "Time", "derivative(x)",
    "Backward difference (v[i] - v[i-1]) / (t[i] - t[i-1]) on the source's own "
    "timebase, held onto the output grid; the first source sample and "
    "duplicate source timestamps give NaN. Unlike integral(), this is local "
    "rather than cumulative; a bad sample poisons only the two intervals "
    "touching it, not everything after it",
    None, temporal=True,
)
_register(
    "diag", 3, 3, "Diagnostic", "diag(event, increment, decrement)",
    "Counter starting at 0; increment while event is nonzero, otherwise "
    "decrement without going below 0",
    None, temporal=True,
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
        if spec.temporal and (
            not isinstance(node.args[0], ast.Name)
            or node.args[0].id not in self._permitted_names
        ):
            raise CalculatedSignalError(
                f"{spec.name}() expects a direct signal reference as its first argument"
            )
        if spec.temporal:
            for argument in node.args[1:]:
                if any(
                    isinstance(part, ast.Name) and part.id in self._permitted_names
                    for part in ast.walk(argument)
                ):
                    if spec.name.lower() == "diag":
                        raise CalculatedSignalError(
                            "diag() expects fixed numeric increment and decrement steps"
                        )
                    raise CalculatedSignalError(
                        f"{spec.name}() expects a single numeric value as its second argument"
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


def _replace_formula_reference(formula: str, old_key: str, new_key: str) -> str:
    """Rewrite one exact backtick-delimited reference, preserving all others."""
    return _REFERENCE_RE.sub(
        lambda match: f"`{new_key}`"
        if match.group(1).strip() == old_key
        else match.group(0),
        formula,
    )


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


@dataclass(frozen=True, slots=True)
class _SourceArrays:
    timestamps: np.ndarray
    values: np.ndarray


def _aligned_inputs(
    references: Sequence[str],
    source_series: Mapping[str, SignalSeries],
) -> tuple[
    np.ndarray,
    dict[str, np.ndarray],
    np.ndarray,
    dict[str, _SourceArrays],
]:
    timestamps: dict[str, np.ndarray] = {}
    values: dict[str, np.ndarray] = {}
    prepared_sources: dict[str, _SourceArrays] = {}
    for key in references:
        series = source_series[key]
        ts = series.numpy_timestamps()
        vs = series.numpy_values()
        if ts.size == 0:
            raise CalculatedSignalError(f"Measurement signal has no samples: {key}")
        if ts.size != vs.size:
            raise CalculatedSignalError(f"Timestamp/value length mismatch: {key}")
        if ts.size > 1 and np.any(ts[1:] < ts[:-1]):
            # A few measurement formats can contain an out-of-order sample or
            # timestamp-reset tail. Keep every value paired with its timestamp
            # and repair the time base for searchsorted/alignment. Stable order
            # also gives deterministic last-sample-wins behaviour at duplicate
            # timestamps, matching the existing zero-order hold semantics.
            order = np.argsort(ts, kind="stable")
            ts = ts[order]
            vs = vs[order]
        timestamps[key] = ts
        values[key] = vs
        prepared_sources[key] = _SourceArrays(ts, vs)

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
    return grid, aligned, all_inputs_finite, prepared_sources


@dataclass(frozen=True, slots=True)
class _EvaluationContext:
    grid: np.ndarray
    aligned_values: Mapping[str, np.ndarray]
    source_by_token: Mapping[str, _SourceArrays]
    temporal_inputs_finite: np.ndarray


def _current_value_tokens(node: ast.AST) -> set[str]:
    """Signal tokens read at the current grid time, outside lag/delay inputs."""
    tokens: set[str] = set()

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Name):
            spec = _FUNCTIONS.get(current.func.id.lower())
            if spec is not None and spec.temporal:
                # The first argument is read from its original timebase by the
                # temporal evaluator, not from the current aligned value.
                for argument in current.args[1:]:
                    visit(argument)
                return
        if isinstance(current, ast.Name) and current.id.startswith("__signal_"):
            tokens.add(current.id)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return tokens


def _scalar_temporal_argument(
    node: ast.AST,
    context: _EvaluationContext,
    function_name: str,
    argument_label: str = "second argument",
) -> float:
    value = np.asarray(_evaluate_node(node, context), dtype=np.float64)
    if value.ndim != 0:
        raise CalculatedSignalError(
            f"{function_name}() expects a single numeric value as its {argument_label}"
        )
    scalar = float(value)
    if not np.isfinite(scalar):
        raise CalculatedSignalError(
            f"{function_name}() expects a finite {argument_label}"
        )
    if scalar < 0:
        raise CalculatedSignalError(
            f"{function_name}() does not accept a negative {argument_label}"
        )
    return scalar


def _evaluate_temporal_call(
    node: ast.Call,
    context: _EvaluationContext,
    spec: FunctionSpec,
) -> np.ndarray:
    signal_node = node.args[0]
    # The validator guarantees this is a direct, permitted signal token.
    if not isinstance(signal_node, ast.Name):  # pragma: no cover - defensive
        raise CalculatedSignalError(
            f"{spec.name}() expects a direct signal reference as its first argument"
        )
    source = context.source_by_token[signal_node.id]
    source_timestamps = source.timestamps
    source_values = source.values
    result = np.full(context.grid.size, np.nan, dtype=np.float64)
    function_name = spec.name.lower()

    if function_name == "diag":
        increment = _scalar_temporal_argument(
            node.args[1], context, spec.name, "increment step"
        )
        decrement = _scalar_temporal_argument(
            node.args[2], context, spec.name, "decrement step"
        )

        # This is a sample counter, not a time integral: update it exactly once
        # for every event-source sample, then hold that count on any shared
        # output-grid timestamps introduced by other formula inputs.
        finite_source = np.isfinite(source_timestamps) & np.isfinite(source_values)
        with np.errstate(over="ignore", invalid="ignore"):
            changes = np.where(source_values != 0, increment, -decrement)
            raw_count = np.cumsum(changes, dtype=np.float64)
            # Reflect the cumulative sum at zero. This is equivalent to
            # counter = max(0, counter + change) for every source sample.
            running_floor = np.minimum.accumulate(np.minimum(raw_count, 0.0))
            source_counts = raw_count - running_floor
        source_counts[~np.logical_and.accumulate(finite_source)] = np.nan

        source_indices = np.searchsorted(
            source_timestamps, context.grid, side="right"
        ) - 1
        valid = source_indices >= 0
        result[valid] = source_counts[source_indices[valid]]
        context.temporal_inputs_finite[:] &= np.isfinite(result)
        return result

    if function_name == "integral":
        # Prefix area at source sample i is the integral over all completed
        # intervals before i. Each interval holds its left value.
        deltas = np.diff(source_timestamps)
        with np.errstate(over="ignore", invalid="ignore"):
            increments = source_values[:-1] * deltas
            prefix_area = np.empty(source_values.size, dtype=np.float64)
            prefix_area[0] = 0.0
            prefix_area[1:] = np.cumsum(increments, dtype=np.float64)

        # Do not invent area after the last source sample. At intermediate
        # grid points, include the partial zero-order-held interval.
        query_times = np.minimum(context.grid, source_timestamps[-1])
        source_indices = np.searchsorted(
            source_timestamps, query_times, side="right"
        ) - 1
        valid = source_indices >= 0
        if np.any(valid):
            indices = source_indices[valid]
            with np.errstate(over="ignore", invalid="ignore"):
                result[valid] = (
                    prefix_area[indices]
                    + source_values[indices]
                    * (query_times[valid] - source_timestamps[indices])
                )
            # Once a non-finite source sample is encountered, the cumulative
            # quantity is unknown from that sample onward.
            finite_through_sample = np.logical_and.accumulate(
                np.isfinite(source_values) & np.isfinite(source_timestamps)
            )
            result[valid] = np.where(
                finite_through_sample[indices], result[valid], np.nan
            )
        context.temporal_inputs_finite[:] &= np.isfinite(result)
        return result

    if function_name == "derivative":
        # Backward difference on the source's own timebase, then held onto
        # the output grid via zero-order hold, matching integral()'s timebase
        # discipline. Unlike integral(), this is local, not cumulative: a
        # plain elementwise diff means a non-finite sample poisons only the
        # two intervals touching it, not everything downstream, so this does
        # not use integral()'s np.logical_and.accumulate approach.
        source_derivative = np.full(source_values.size, np.nan, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            source_derivative[1:] = np.diff(source_values) / np.diff(source_timestamps)

        source_indices = np.searchsorted(
            source_timestamps, context.grid, side="right"
        ) - 1
        valid = source_indices >= 0
        result[valid] = source_derivative[source_indices[valid]]
        context.temporal_inputs_finite[:] &= np.isfinite(result)
        return result

    amount = _scalar_temporal_argument(node.args[1], context, spec.name)

    if function_name in {"lag", "rolling_sum"}:
        if not amount.is_integer():
            raise CalculatedSignalError(
                f"{spec.name}() sample count must be a whole number"
            )
        sample_count = int(amount)
        source_indices = np.searchsorted(
            source_timestamps, context.grid, side="right"
        ) - 1
        if function_name == "lag":
            if sample_count >= source_values.size:
                context.temporal_inputs_finite[:] = False
                return result
            source_indices -= sample_count
        else:
            if sample_count < 1:
                raise CalculatedSignalError(
                    "rolling_sum() sample count must be greater than zero"
                )
            if sample_count > source_values.size:
                context.temporal_inputs_finite[:] = False
                return result
            safe_values = np.where(np.isfinite(source_values), source_values, 0.0)
            invalid_values = (~np.isfinite(source_values)).astype(np.int64)
            with np.errstate(over="ignore", invalid="ignore"):
                prefix_sum = np.concatenate(
                    ([0.0], np.cumsum(safe_values, dtype=np.float64))
                )
            prefix_invalid = np.concatenate(
                ([0], np.cumsum(invalid_values, dtype=np.int64))
            )
            valid = source_indices >= sample_count - 1
            if np.any(valid):
                ends = source_indices[valid] + 1
                starts = ends - sample_count
                window_values = prefix_sum[ends] - prefix_sum[starts]
                window_invalid = (
                    prefix_invalid[ends] - prefix_invalid[starts]
                )
                result[valid] = np.where(
                    window_invalid == 0, window_values, np.nan
                )
            context.temporal_inputs_finite[:] &= np.isfinite(result)
            return result
    elif function_name == "delay":
        with np.errstate(over="ignore", invalid="ignore"):
            query_times = context.grid - amount
        source_indices = np.searchsorted(
            source_timestamps, query_times, side="right"
        ) - 1
    else:  # pragma: no cover - registry invariant
        raise CalculatedSignalError(f"Unknown temporal function: {spec.name}")

    valid = source_indices >= 0
    result[valid] = source_values[source_indices[valid]]
    context.temporal_inputs_finite[:] &= np.isfinite(result)
    return result


def _evaluate_node(node: ast.AST, context: _EvaluationContext):
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, context)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        return context.aligned_values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        spec = _FUNCTIONS[node.func.id.lower()]
        if spec.temporal:
            return _evaluate_temporal_call(node, context, spec)
        args = [_evaluate_node(arg, context) for arg in node.args]
        if spec.implementation is None:  # pragma: no cover - registry invariant
            raise CalculatedSignalError(f"Function is not executable: {spec.name}")
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return spec.implementation(*args)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, context)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Not):
                return np.logical_not(np.asarray(operand) != 0)
            return _bitwise_invert(operand)  # ast.Invert
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, context)
        right = _evaluate_node(node.right, context)
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
        operands = [
            np.asarray(_evaluate_node(item, context)) != 0 for item in node.values
        ]
        reducer = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
        result = operands[0]
        for operand in operands[1:]:
            result = reducer(result, operand)
        return result
    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, context)
        combined = None
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate_node(comparator, context)
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


def build_time_signal(source_series: Iterable[SignalSeries]) -> SignalSeries:
    """Build the synthetic elapsed-time input used by calculated formulas.

    Its samples cover the union of the supplied measurement time bases, and
    each value is the timestamp itself in seconds.
    """
    time_bases = [
        series.numpy_timestamps()
        for series in source_series
        if len(series.timestamps) > 0
    ]
    if not time_bases:
        raise CalculatedSignalError("Time signal has no samples")
    grid = np.unique(np.concatenate(time_bases))
    return SignalSeries(
        channel=None,
        message_name="Measurement",
        message_id=0,
        signal_name=TIME_SIGNAL_KEY,
        unit="s",
        timestamps=_to_double_array(grid),
        values=_to_double_array(grid),
    )


def calculate_series(
    definition: CalculatedSignalDefinition,
    source_series: Mapping[str, SignalSeries],
) -> SignalSeries:
    name = validate_name(definition.name)
    parsed = parse_formula(definition.formula, source_series.keys())
    grid, aligned, _all_inputs_finite, prepared_sources = _aligned_inputs(
        parsed.references, source_series
    )

    context = _EvaluationContext(
        grid=grid,
        aligned_values=aligned,
        source_by_token={
            f"__signal_{index}": prepared_sources[key]
            for index, key in enumerate(parsed.references)
        },
        temporal_inputs_finite=np.ones(grid.size, dtype=bool),
    )
    result = np.asarray(_evaluate_node(parsed.tree, context), dtype=np.float64)
    if result.ndim == 0:
        result = np.full(grid.size, float(result), dtype=np.float64)
    else:
        result = np.broadcast_to(result, grid.shape).astype(np.float64, copy=True)
    current_inputs_finite = np.ones(grid.size, dtype=bool)
    for token in _current_value_tokens(parsed.tree):
        current_inputs_finite &= np.isfinite(aligned[token])
    result[
        ~current_inputs_finite
        | ~context.temporal_inputs_finite
        | ~np.isfinite(result)
    ] = np.nan

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

    def rename(self, key: str, new_name: str) -> str:
        """Rename a definition and atomically rewrite every dependent formula."""
        existing = self._definitions.get(key)
        if existing is None:
            raise CalculatedSignalError(f"Generated signal not found: {key}")
        cleaned = validate_name(new_name)
        self.assert_unique_name(cleaned, except_key=key)
        renamed = CalculatedSignalDefinition(
            name=cleaned,
            formula=existing.formula,
            unit=existing.unit,
        )
        new_key = renamed.key
        if new_key == key:
            return key

        definitions: dict[str, CalculatedSignalDefinition] = {}
        for current_key, definition in self._definitions.items():
            if current_key == key:
                updated = renamed
                updated_key = new_key
            else:
                updated = CalculatedSignalDefinition(
                    name=definition.name,
                    formula=_replace_formula_reference(
                        definition.formula, key, new_key
                    ),
                    unit=definition.unit,
                )
                updated_key = current_key
            definitions[updated_key] = updated

        cache: dict[str, SignalSeries] = {}
        for current_key, series in self._cache.items():
            if current_key == key:
                series.signal_name = cleaned
                cache[new_key] = series
            else:
                cache[current_key] = series

        self._definitions = definitions
        self._cache = cache
        return new_key

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
