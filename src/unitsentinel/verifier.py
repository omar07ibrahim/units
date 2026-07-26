"""Fail-closed dimensional and unit-transform verification.

The public result deliberately exposes only trusted domain values. Solver
symbols, models, diagnostics, and implementation-specific assertion names never
cross this module's boundary.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from fractions import Fraction
from typing import Any, Final

import z3  # type: ignore[import-untyped]

from .domain import (
    BASE_DIMENSION_COUNT,
    MAX_RATIONAL_BITS,
    THERMODYNAMIC_TEMPERATURE,
    Dimension,
    DimensionError,
    QuantityKind,
    Unit,
    UnitSentinelError,
)
from .graph import ComputationGraph, GraphValidationError, Node, Operation
from .registry import BUILTIN_REGISTRY, UnitRegistry
from .verification import (
    ConstraintSource,
    ConstraintWitness,
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)

_LINEAR: Final = 0
_ABSOLUTE_TEMPERATURE: Final = 1
_TEMPERATURE_DELTA: Final = 2
_KIND_CODES: Final = {
    QuantityKind.LINEAR: _LINEAR,
    QuantityKind.ABSOLUTE_TEMPERATURE: _ABSOLUTE_TEMPERATURE,
    QuantityKind.TEMPERATURE_DELTA: _TEMPERATURE_DELTA,
}
_CODE_KINDS: Final = {code: kind for kind, code in _KIND_CODES.items()}
_RESOURCE_MARKERS: Final = (
    "canceled",
    "max. memory",
    "memout",
    "rlimit",
    "timeout",
)
_DEFAULT_SOLVER_LIMITS: Final = SolverLimits()


@dataclass(frozen=True, slots=True)
class _ValueTerms:
    dimension: tuple[Any, ...]
    kind: Any
    scale: Any
    offset: Any


@dataclass(frozen=True, slots=True)
class _TrackedConstraint:
    witness: ConstraintWitness
    expression: Any
    track_index: int = -1


@dataclass(frozen=True, slots=True)
class _CompiledProblem:
    context: Any
    terms: dict[str, _ValueTerms]
    background: tuple[Any, ...]
    constraints: tuple[_TrackedConstraint, ...]


@dataclass(frozen=True, slots=True)
class _ModelValue:
    dimension: Dimension
    kind: QuantityKind
    scale: Fraction
    offset: Fraction


class _CheckState(Enum):
    SAT = auto()
    UNSAT = auto()
    RESOURCE_LIMIT = auto()
    SOLVER_UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class _CheckResult:
    state: _CheckState
    solver: Any | None = None


class _CheckBudget:
    """One monotonic deadline shared by every solver check in a run."""

    def __init__(
        self,
        *,
        context: Any,
        background: tuple[Any, ...],
        limits: SolverLimits,
        started_at: float,
    ) -> None:
        self._context = context
        self._background = background
        self._limits = limits
        self._deadline = started_at + limits.total_timeout_ms / 1_000
        self._checks_performed = 0

    @property
    def checks_performed(self) -> int:
        return self._checks_performed

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._deadline

    def check(
        self,
        constraints: Sequence[_TrackedConstraint],
        *,
        extra: Any | None = None,
    ) -> _CheckResult:
        remaining_ms = int((self._deadline - time.monotonic()) * 1_000)
        if remaining_ms < 1:
            return _CheckResult(_CheckState.RESOURCE_LIMIT)

        try:
            solver = z3.Solver(ctx=self._context)
            solver.set(
                max_memory=self._limits.max_memory_mb,
                random_seed=0,
                threads=1,
            )
            solver.add(*self._background)
            for constraint in constraints:
                solver.assert_and_track(
                    constraint.expression,
                    z3.Bool(
                        f"tracked_{constraint.track_index:04d}",
                        ctx=self._context,
                    ),
                )
            if extra is not None:
                solver.add(extra)

            remaining_ms = int((self._deadline - time.monotonic()) * 1_000)
            if remaining_ms < 1:
                return _CheckResult(_CheckState.RESOURCE_LIMIT)
            solver.set(
                timeout=max(
                    1,
                    min(self._limits.per_check_timeout_ms, remaining_ms),
                )
            )
            self._checks_performed += 1
            status = solver.check()
            if time.monotonic() >= self._deadline:
                return _CheckResult(_CheckState.RESOURCE_LIMIT)
            if status == z3.sat:
                return _CheckResult(_CheckState.SAT, solver)
            if status == z3.unsat:
                return _CheckResult(_CheckState.UNSAT, solver)
            reason = solver.reason_unknown().lower()
        except MemoryError:
            return _CheckResult(_CheckState.RESOURCE_LIMIT)
        except z3.Z3Exception:
            reason = ""
        if time.monotonic() >= self._deadline or any(
            marker in reason for marker in _RESOURCE_MARKERS
        ):
            return _CheckResult(_CheckState.RESOURCE_LIMIT)
        return _CheckResult(_CheckState.SOLVER_UNKNOWN)


def _rational(value: Fraction, context: Any) -> Any:
    return z3.Q(value.numerator, value.denominator, context)


def _integer(value: int, context: Any) -> Any:
    return z3.IntVal(value, ctx=context)


def _kind_code(kind: QuantityKind) -> int:
    return _KIND_CODES[kind]


def _dimension_equals(
    terms: _ValueTerms,
    dimension: Dimension,
    context: Any,
) -> list[Any]:
    return [
        term == _rational(exponent, context)
        for term, exponent in zip(
            terms.dimension,
            dimension.exponents,
            strict=True,
        )
    ]


def _same_dimension(left: _ValueTerms, right: _ValueTerms) -> list[Any]:
    return [
        left_term == right_term
        for left_term, right_term in zip(
            left.dimension,
            right.dimension,
            strict=True,
        )
    ]


def _constraint(
    *,
    constraint_id: str,
    source: ConstraintSource,
    source_id: str,
    rule: str,
    expression: Any,
) -> _TrackedConstraint:
    return _TrackedConstraint(
        ConstraintWitness(
            constraint_id=constraint_id,
            source=source,
            source_id=source_id,
            rule=rule,
        ),
        expression,
    )


def _declaration_constraint(
    *,
    value_id: str,
    terms: _ValueTerms,
    unit: Unit,
    context: Any,
) -> _TrackedConstraint:
    expressions = _dimension_equals(terms, unit.dimension, context)
    expressions.extend(
        (
            terms.kind == _integer(_kind_code(unit.kind), context),
            terms.scale == _rational(unit.scale, context),
            terms.offset == _rational(unit.offset, context),
        )
    )
    return _constraint(
        constraint_id=f"declaration/{value_id}/unit",
        source=ConstraintSource.DECLARATION,
        source_id=value_id,
        rule="unit-annotation",
        expression=z3.And(*expressions),
    )


def _operation_constraint(
    *,
    node: Node,
    aspect: str,
    expression: Any,
) -> _TrackedConstraint:
    return _constraint(
        constraint_id=f"operation/{node.node_id}/{aspect}",
        source=ConstraintSource.OPERATION,
        source_id=node.node_id,
        rule=f"{node.operation.value}-{aspect}",
        expression=expression,
    )


def _kind_is(terms: _ValueTerms, code: int, context: Any) -> Any:
    return terms.kind == _integer(code, context)


def _kind_tuple(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
    values: tuple[int, int, int],
    context: Any,
) -> Any:
    return z3.And(
        _kind_is(left, values[0], context),
        _kind_is(right, values[1], context),
        _kind_is(output, values[2], context),
    )


def _equal_scales(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
) -> Any:
    return z3.And(left.scale == right.scale, output.scale == left.scale)


def _add_kind_expression(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
    context: Any,
) -> Any:
    allowed = (
        (_LINEAR, _LINEAR, _LINEAR),
        (_ABSOLUTE_TEMPERATURE, _TEMPERATURE_DELTA, _ABSOLUTE_TEMPERATURE),
        (_TEMPERATURE_DELTA, _ABSOLUTE_TEMPERATURE, _ABSOLUTE_TEMPERATURE),
        (_TEMPERATURE_DELTA, _TEMPERATURE_DELTA, _TEMPERATURE_DELTA),
    )
    return z3.Or(
        *(_kind_tuple(left, right, output, values, context) for values in allowed)
    )


def _subtract_kind_expression(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
    context: Any,
) -> Any:
    allowed = (
        (_LINEAR, _LINEAR, _LINEAR),
        (_ABSOLUTE_TEMPERATURE, _ABSOLUTE_TEMPERATURE, _TEMPERATURE_DELTA),
        (_ABSOLUTE_TEMPERATURE, _TEMPERATURE_DELTA, _ABSOLUTE_TEMPERATURE),
        (_TEMPERATURE_DELTA, _TEMPERATURE_DELTA, _TEMPERATURE_DELTA),
    )
    return z3.Or(
        *(_kind_tuple(left, right, output, values, context) for values in allowed)
    )


def _add_transform_expression(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
    context: Any,
) -> Any:
    same_scale = _equal_scales(left, right, output)
    cases = (
        (
            (_LINEAR, _LINEAR, _LINEAR),
            z3.And(
                same_scale,
                left.offset == 0,
                right.offset == 0,
                output.offset == 0,
            ),
        ),
        (
            (
                _ABSOLUTE_TEMPERATURE,
                _TEMPERATURE_DELTA,
                _ABSOLUTE_TEMPERATURE,
            ),
            z3.And(same_scale, output.offset == left.offset),
        ),
        (
            (
                _TEMPERATURE_DELTA,
                _ABSOLUTE_TEMPERATURE,
                _ABSOLUTE_TEMPERATURE,
            ),
            z3.And(same_scale, output.offset == right.offset),
        ),
        (
            (
                _TEMPERATURE_DELTA,
                _TEMPERATURE_DELTA,
                _TEMPERATURE_DELTA,
            ),
            z3.And(same_scale, output.offset == 0),
        ),
    )
    return z3.And(
        *(
            z3.Implies(
                _kind_tuple(left, right, output, kinds, context),
                transform,
            )
            for kinds, transform in cases
        )
    )


def _subtract_transform_expression(
    left: _ValueTerms,
    right: _ValueTerms,
    output: _ValueTerms,
    context: Any,
) -> Any:
    same_scale = _equal_scales(left, right, output)
    cases = (
        (
            (_LINEAR, _LINEAR, _LINEAR),
            z3.And(
                same_scale,
                left.offset == 0,
                right.offset == 0,
                output.offset == 0,
            ),
        ),
        (
            (
                _ABSOLUTE_TEMPERATURE,
                _ABSOLUTE_TEMPERATURE,
                _TEMPERATURE_DELTA,
            ),
            z3.And(
                same_scale,
                left.offset == right.offset,
                output.offset == 0,
            ),
        ),
        (
            (
                _ABSOLUTE_TEMPERATURE,
                _TEMPERATURE_DELTA,
                _ABSOLUTE_TEMPERATURE,
            ),
            z3.And(same_scale, output.offset == left.offset),
        ),
        (
            (
                _TEMPERATURE_DELTA,
                _TEMPERATURE_DELTA,
                _TEMPERATURE_DELTA,
            ),
            z3.And(same_scale, output.offset == 0),
        ),
    )
    return z3.And(
        *(
            z3.Implies(
                _kind_tuple(left, right, output, kinds, context),
                transform,
            )
            for kinds, transform in cases
        )
    )


def _positive_integer_power(expression: Any, exponent: int, context: Any) -> Any:
    if exponent == 0:
        return _rational(Fraction(1), context)
    return expression**exponent


def _power_scale_expression(
    source: _ValueTerms,
    output: _ValueTerms,
    exponent: Fraction,
    context: Any,
) -> Any:
    numerator = exponent.numerator
    denominator = exponent.denominator
    output_power = _positive_integer_power(output.scale, denominator, context)
    if numerator >= 0:
        return output_power == _positive_integer_power(
            source.scale,
            numerator,
            context,
        )
    return output_power * _positive_integer_power(
        source.scale, -numerator, context
    ) == _rational(Fraction(1), context)


def _node_dimension_expression(
    node: Node,
    terms: dict[str, _ValueTerms],
    registry: UnitRegistry,
    context: Any,
) -> Any:
    inputs = tuple(terms[value_id] for value_id in node.inputs)
    output = terms[node.output]
    if node.operation in {
        Operation.IDENTITY,
        Operation.ADD,
        Operation.SUBTRACT,
        Operation.MINIMUM,
        Operation.MAXIMUM,
    }:
        equations = _same_dimension(inputs[0], output)
        if len(inputs) == 2:
            equations.extend(_same_dimension(inputs[0], inputs[1]))
        return z3.And(*equations)
    if node.operation in {Operation.MULTIPLY, Operation.MATMUL}:
        return z3.And(
            *(
                out == left + right
                for out, left, right in zip(
                    output.dimension,
                    inputs[0].dimension,
                    inputs[1].dimension,
                    strict=True,
                )
            )
        )
    if node.operation is Operation.DIVIDE:
        return z3.And(
            *(
                out == left - right
                for out, left, right in zip(
                    output.dimension,
                    inputs[0].dimension,
                    inputs[1].dimension,
                    strict=True,
                )
            )
        )
    if node.operation is Operation.POWER:
        assert node.exponent is not None
        factor = _rational(node.exponent, context)
        return z3.And(
            *(
                out == source * factor
                for out, source in zip(
                    output.dimension,
                    inputs[0].dimension,
                    strict=True,
                )
            )
        )
    if node.operation in {
        Operation.EXP,
        Operation.LOG,
        Operation.SIGMOID,
        Operation.SOFTMAX,
    }:
        equations = _dimension_equals(
            inputs[0],
            Dimension.dimensionless(),
            context,
        )
        equations.extend(_dimension_equals(output, Dimension.dimensionless(), context))
        return z3.And(*equations)
    assert node.operation is Operation.CONVERT
    assert node.target_unit_id is not None
    target = registry.resolve(node.target_unit_id)
    equations = _dimension_equals(inputs[0], target.dimension, context)
    equations.extend(_dimension_equals(output, target.dimension, context))
    return z3.And(*equations)


def _node_kind_expression(
    node: Node,
    terms: dict[str, _ValueTerms],
    registry: UnitRegistry,
    context: Any,
) -> Any:
    inputs = tuple(terms[value_id] for value_id in node.inputs)
    output = terms[node.output]
    if node.operation is Operation.IDENTITY:
        return output.kind == inputs[0].kind
    if node.operation is Operation.ADD:
        return _add_kind_expression(inputs[0], inputs[1], output, context)
    if node.operation is Operation.SUBTRACT:
        return _subtract_kind_expression(inputs[0], inputs[1], output, context)
    if node.operation in {Operation.MINIMUM, Operation.MAXIMUM}:
        return z3.And(
            inputs[0].kind == inputs[1].kind,
            output.kind == inputs[0].kind,
        )
    if node.operation in {
        Operation.MULTIPLY,
        Operation.MATMUL,
        Operation.DIVIDE,
        Operation.POWER,
    }:
        return z3.And(
            *(
                value.kind != _integer(_ABSOLUTE_TEMPERATURE, context)
                for value in inputs
            ),
            output.kind != _integer(_ABSOLUTE_TEMPERATURE, context),
        )
    if node.operation in {
        Operation.EXP,
        Operation.LOG,
        Operation.SIGMOID,
        Operation.SOFTMAX,
    }:
        return z3.And(
            _kind_is(inputs[0], _LINEAR, context),
            _kind_is(output, _LINEAR, context),
        )
    assert node.operation is Operation.CONVERT
    assert node.target_unit_id is not None
    target_code = _kind_code(registry.resolve(node.target_unit_id).kind)
    return z3.And(
        _kind_is(inputs[0], target_code, context),
        _kind_is(output, target_code, context),
    )


def _node_transform_expression(
    node: Node,
    terms: dict[str, _ValueTerms],
    registry: UnitRegistry,
    graph: ComputationGraph,
    context: Any,
) -> Any:
    inputs = tuple(terms[value_id] for value_id in node.inputs)
    output = terms[node.output]
    if node.operation is Operation.IDENTITY:
        return z3.And(
            output.scale == inputs[0].scale,
            output.offset == inputs[0].offset,
        )
    if node.operation is Operation.ADD:
        return _add_transform_expression(inputs[0], inputs[1], output, context)
    if node.operation is Operation.SUBTRACT:
        return _subtract_transform_expression(
            inputs[0],
            inputs[1],
            output,
            context,
        )
    if node.operation in {Operation.MINIMUM, Operation.MAXIMUM}:
        return z3.And(
            inputs[0].scale == inputs[1].scale,
            output.scale == inputs[0].scale,
            inputs[0].offset == inputs[1].offset,
            output.offset == inputs[0].offset,
        )
    if node.operation in {Operation.MULTIPLY, Operation.MATMUL}:
        return output.scale == inputs[0].scale * inputs[1].scale
    if node.operation is Operation.DIVIDE:
        return output.scale * inputs[1].scale == inputs[0].scale
    if node.operation is Operation.POWER:
        assert node.exponent is not None
        return _power_scale_expression(
            inputs[0],
            output,
            node.exponent,
            context,
        )
    if node.operation in {
        Operation.EXP,
        Operation.LOG,
        Operation.SIGMOID,
        Operation.SOFTMAX,
    }:
        one = _rational(Fraction(1), context)
        zero = _rational(Fraction(0), context)
        return z3.And(
            inputs[0].scale == one,
            inputs[0].offset == zero,
            output.scale == one,
            output.offset == zero,
        )

    assert node.operation is Operation.CONVERT
    assert node.target_unit_id is not None
    target = registry.resolve(node.target_unit_id)
    output_annotation = graph.value(node.output).unit_id
    exact_output_contract = (
        output_annotation is None or output_annotation == target.unit_id
    )
    return z3.And(
        output.scale == _rational(target.scale, context),
        output.offset == _rational(target.offset, context),
        z3.BoolVal(exact_output_contract, ctx=context),
    )


def _compile_problem(
    graph: ComputationGraph,
    registry: UnitRegistry,
    context: Any,
) -> _CompiledProblem:
    terms: dict[str, _ValueTerms] = {}
    background: list[Any] = []
    constraints: list[_TrackedConstraint] = []
    temperature = THERMODYNAMIC_TEMPERATURE.exponents

    for index, value in enumerate(graph.values):
        value_terms = _ValueTerms(
            dimension=tuple(
                z3.Real(f"d_{index:03d}_{axis}", ctx=context)
                for axis in range(BASE_DIMENSION_COUNT)
            ),
            kind=z3.Int(f"k_{index:03d}", ctx=context),
            scale=z3.Real(f"s_{index:03d}", ctx=context),
            offset=z3.Real(f"o_{index:03d}", ctx=context),
        )
        terms[value.value_id] = value_terms
        pure_temperature = z3.And(
            *(
                term == _rational(exponent, context)
                for term, exponent in zip(
                    value_terms.dimension,
                    temperature,
                    strict=True,
                )
            )
        )
        temperature_kind = z3.Or(
            _kind_is(value_terms, _ABSOLUTE_TEMPERATURE, context),
            _kind_is(value_terms, _TEMPERATURE_DELTA, context),
        )
        background.extend(
            (
                z3.Or(
                    _kind_is(value_terms, _LINEAR, context),
                    _kind_is(value_terms, _ABSOLUTE_TEMPERATURE, context),
                    _kind_is(value_terms, _TEMPERATURE_DELTA, context),
                ),
                temperature_kind == pure_temperature,
                value_terms.scale > _rational(Fraction(0), context),
                z3.Implies(
                    value_terms.kind
                    != _integer(
                        _ABSOLUTE_TEMPERATURE,
                        context,
                    ),
                    value_terms.offset == _rational(Fraction(0), context),
                ),
            )
        )
        if value.unit_id is not None:
            constraints.append(
                _declaration_constraint(
                    value_id=value.value_id,
                    terms=value_terms,
                    unit=registry.resolve(value.unit_id),
                    context=context,
                )
            )

    for node in graph.nodes:
        constraints.extend(
            (
                _operation_constraint(
                    node=node,
                    aspect="dimension",
                    expression=_node_dimension_expression(
                        node,
                        terms,
                        registry,
                        context,
                    ),
                ),
                _operation_constraint(
                    node=node,
                    aspect="kind",
                    expression=_node_kind_expression(
                        node,
                        terms,
                        registry,
                        context,
                    ),
                ),
                _operation_constraint(
                    node=node,
                    aspect="unit-transform",
                    expression=_node_transform_expression(
                        node,
                        terms,
                        registry,
                        graph,
                        context,
                    ),
                ),
            )
        )

    constraints.sort(key=lambda item: item.witness.constraint_id)
    indexed_constraints = tuple(
        _TrackedConstraint(
            witness=constraint.witness,
            expression=constraint.expression,
            track_index=index,
        )
        for index, constraint in enumerate(constraints)
    )
    return _CompiledProblem(
        context=context,
        terms=terms,
        background=tuple(background),
        constraints=indexed_constraints,
    )


def _fraction_from_solver(value: Any) -> Fraction:
    if not z3.is_rational_value(value):
        raise VerificationError("solver model contains a non-rational value")
    numerator = value.numerator_as_long()
    denominator = value.denominator_as_long()
    result = Fraction(numerator, denominator)
    if (
        abs(result.numerator).bit_length() > MAX_RATIONAL_BITS
        or result.denominator.bit_length() > MAX_RATIONAL_BITS
    ):
        raise VerificationError("solver rational exceeds the domain size limit")
    return result


def _extract_model(
    problem: _CompiledProblem,
    solver: Any,
) -> dict[str, _ModelValue]:
    model = solver.model()
    extracted: dict[str, _ModelValue] = {}
    for value_id, terms in problem.terms.items():
        dimension_values = tuple(
            _fraction_from_solver(model.eval(term, model_completion=True))
            for term in terms.dimension
        )
        kind_value = model.eval(terms.kind, model_completion=True)
        if not z3.is_int_value(kind_value):
            raise VerificationError("solver model contains a non-integer kind")
        kind = _CODE_KINDS.get(kind_value.as_long())
        if kind is None:
            raise VerificationError("solver model contains an unknown kind")
        try:
            dimension = Dimension(dimension_values)
        except DimensionError as error:
            raise VerificationError(
                "solver dimension is outside the trusted domain"
            ) from error
        extracted[value_id] = _ModelValue(
            dimension=dimension,
            kind=kind,
            scale=_fraction_from_solver(model.eval(terms.scale, model_completion=True)),
            offset=_fraction_from_solver(
                model.eval(terms.offset, model_completion=True)
            ),
        )
    return extracted


def _model_difference(
    problem: _CompiledProblem,
    model_values: dict[str, _ModelValue],
    value_ids: Sequence[str],
) -> Any:
    alternatives: list[Any] = []
    for value_id in value_ids:
        terms = problem.terms[value_id]
        baseline = model_values[value_id]
        alternatives.extend(
            term != _rational(exponent, problem.context)
            for term, exponent in zip(
                terms.dimension,
                baseline.dimension.exponents,
                strict=True,
            )
        )
        alternatives.extend(
            (
                terms.kind != _integer(_kind_code(baseline.kind), problem.context),
                terms.scale != _rational(baseline.scale, problem.context),
                terms.offset != _rational(baseline.offset, problem.context),
            )
        )
    return z3.Or(*alternatives)


def _coherent_model_value(value: _ModelValue) -> bool:
    pure_temperature = value.dimension == THERMODYNAMIC_TEMPERATURE
    temperature_kind = value.kind in {
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.TEMPERATURE_DELTA,
    }
    if pure_temperature != temperature_kind or value.scale <= 0:
        return False
    return not (
        value.kind is not QuantityKind.ABSOLUTE_TEMPERATURE and value.offset != 0
    )


def _unit_matches(value: _ModelValue, unit: Unit) -> bool:
    return (
        value.dimension == unit.dimension
        and value.kind is unit.kind
        and value.scale == unit.scale
        and value.offset == unit.offset
    )


def _same_transform(*values: _ModelValue) -> bool:
    first = values[0]
    return all(
        value.scale == first.scale and value.offset == first.offset
        for value in values[1:]
    )


def _replay_add(
    left: _ModelValue,
    right: _ModelValue,
    output: _ModelValue,
) -> bool:
    if not (left.dimension == right.dimension == output.dimension):
        return False
    kinds = (left.kind, right.kind, output.kind)
    if kinds == (
        QuantityKind.LINEAR,
        QuantityKind.LINEAR,
        QuantityKind.LINEAR,
    ):
        return _same_transform(left, right, output)
    if kinds == (
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.ABSOLUTE_TEMPERATURE,
    ):
        return (
            left.scale == right.scale == output.scale and output.offset == left.offset
        )
    if kinds == (
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.ABSOLUTE_TEMPERATURE,
    ):
        return (
            left.scale == right.scale == output.scale and output.offset == right.offset
        )
    if kinds == (
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.TEMPERATURE_DELTA,
    ):
        return left.scale == right.scale == output.scale
    return False


def _replay_subtract(
    left: _ModelValue,
    right: _ModelValue,
    output: _ModelValue,
) -> bool:
    if not (left.dimension == right.dimension == output.dimension):
        return False
    kinds = (left.kind, right.kind, output.kind)
    if kinds == (
        QuantityKind.LINEAR,
        QuantityKind.LINEAR,
        QuantityKind.LINEAR,
    ):
        return _same_transform(left, right, output)
    if kinds == (
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.TEMPERATURE_DELTA,
    ):
        return (
            left.scale == right.scale == output.scale
            and left.offset == right.offset
            and output.offset == 0
        )
    if kinds == (
        QuantityKind.ABSOLUTE_TEMPERATURE,
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.ABSOLUTE_TEMPERATURE,
    ):
        return (
            left.scale == right.scale == output.scale and output.offset == left.offset
        )
    if kinds == (
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.TEMPERATURE_DELTA,
        QuantityKind.TEMPERATURE_DELTA,
    ):
        return left.scale == right.scale == output.scale
    return False


def _replay_power_scale(
    source: Fraction,
    output: Fraction,
    exponent: Fraction,
) -> bool:
    numerator = exponent.numerator
    denominator = exponent.denominator
    if numerator >= 0:
        return output**denominator == source**numerator
    return output**denominator * source ** (-numerator) == 1


def _replay_node(
    node: Node,
    values: dict[str, _ModelValue],
    graph: ComputationGraph,
    registry: UnitRegistry,
) -> bool:
    inputs = tuple(values[value_id] for value_id in node.inputs)
    output = values[node.output]
    if node.operation is Operation.IDENTITY:
        return (
            inputs[0].dimension == output.dimension
            and inputs[0].kind is output.kind
            and _same_transform(inputs[0], output)
        )
    if node.operation is Operation.ADD:
        return _replay_add(inputs[0], inputs[1], output)
    if node.operation is Operation.SUBTRACT:
        return _replay_subtract(inputs[0], inputs[1], output)
    if node.operation in {Operation.MINIMUM, Operation.MAXIMUM}:
        return (
            inputs[0].dimension == inputs[1].dimension == output.dimension
            and inputs[0].kind is inputs[1].kind is output.kind
            and _same_transform(inputs[0], inputs[1], output)
        )
    if node.operation in {Operation.MULTIPLY, Operation.MATMUL}:
        return (
            output.dimension == inputs[0].dimension.multiply(inputs[1].dimension)
            and all(
                value.kind is not QuantityKind.ABSOLUTE_TEMPERATURE
                for value in (*inputs, output)
            )
            and output.scale == inputs[0].scale * inputs[1].scale
        )
    if node.operation is Operation.DIVIDE:
        return (
            output.dimension == inputs[0].dimension.divide(inputs[1].dimension)
            and all(
                value.kind is not QuantityKind.ABSOLUTE_TEMPERATURE
                for value in (*inputs, output)
            )
            and output.scale * inputs[1].scale == inputs[0].scale
        )
    if node.operation is Operation.POWER:
        assert node.exponent is not None
        try:
            expected_dimension = inputs[0].dimension.power(node.exponent)
        except DimensionError:
            return False
        return (
            output.dimension == expected_dimension
            and inputs[0].kind is not QuantityKind.ABSOLUTE_TEMPERATURE
            and output.kind is not QuantityKind.ABSOLUTE_TEMPERATURE
            and _replay_power_scale(
                inputs[0].scale,
                output.scale,
                node.exponent,
            )
        )
    if node.operation in {
        Operation.EXP,
        Operation.LOG,
        Operation.SIGMOID,
        Operation.SOFTMAX,
    }:
        return all(
            value.dimension.is_dimensionless
            and value.kind is QuantityKind.LINEAR
            and value.scale == 1
            and value.offset == 0
            for value in (inputs[0], output)
        )

    assert node.operation is Operation.CONVERT
    assert node.target_unit_id is not None
    target = registry.resolve(node.target_unit_id)
    output_annotation = graph.value(node.output).unit_id
    return (
        inputs[0].dimension == target.dimension
        and inputs[0].kind is target.kind
        and _unit_matches(output, target)
        and (output_annotation is None or output_annotation == target.unit_id)
    )


def _replay_model(
    graph: ComputationGraph,
    registry: UnitRegistry,
    model_values: dict[str, _ModelValue],
) -> bool:
    if not all(_coherent_model_value(value) for value in model_values.values()):
        return False
    for value_spec in graph.values:
        if value_spec.unit_id is not None and not _unit_matches(
            model_values[value_spec.value_id],
            registry.resolve(value_spec.unit_id),
        ):
            return False
    try:
        return all(
            _replay_node(node, model_values, graph, registry) for node in graph.nodes
        )
    except (DimensionError, UnitSentinelError):
        return False


def _contracts(
    value_ids: Sequence[str],
    model_values: dict[str, _ModelValue],
) -> tuple[InferredContract, ...]:
    return tuple(
        InferredContract(
            value_id=value_id,
            dimension=model_values[value_id].dimension,
            kind=model_values[value_id].kind,
            scale=model_values[value_id].scale,
            offset=model_values[value_id].offset,
        )
        for value_id in sorted(value_ids)
    )


def _unknown_result(
    *,
    graph_digest: str,
    registry_digest: str,
    solver_version: str,
    limits: SolverLimits,
    checks_performed: int,
    reason: UnknownReason,
) -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.UNKNOWN,
        graph_digest=graph_digest,
        registry_digest=registry_digest,
        solver_version=solver_version,
        limits=limits,
        checks_performed=checks_performed,
        unknown_reason=reason,
    )


def _unknown_from_check(
    *,
    check: _CheckResult,
    graph_digest: str,
    registry_digest: str,
    solver_version: str,
    limits: SolverLimits,
    checks_performed: int,
) -> VerificationResult:
    reason = (
        UnknownReason.RESOURCE_LIMIT
        if check.state is _CheckState.RESOURCE_LIMIT
        else UnknownReason.SOLVER_UNKNOWN
    )
    return _unknown_result(
        graph_digest=graph_digest,
        registry_digest=registry_digest,
        solver_version=solver_version,
        limits=limits,
        checks_performed=checks_performed,
        reason=reason,
    )


def _tracked_conflict_seed(
    problem: _CompiledProblem,
    solver: Any,
) -> tuple[_TrackedConstraint, ...]:
    by_token = {
        f"tracked_{constraint.track_index:04d}": constraint
        for constraint in problem.constraints
    }
    selected: list[_TrackedConstraint] = []
    seen: set[str] = set()
    for token in solver.unsat_core():
        token_name = token.decl().name()
        constraint = by_token.get(token_name)
        if constraint is None or token_name in seen:
            raise VerificationError("solver returned an invalid tracked core")
        seen.add(token_name)
        selected.append(constraint)
    selected.sort(key=lambda item: item.witness.constraint_id)
    if not selected:
        raise VerificationError("solver returned an empty tracked core")
    return tuple(selected)


def _shrink_conflict(
    seed: tuple[_TrackedConstraint, ...],
    budget: _CheckBudget,
    limits: SolverLimits,
) -> tuple[tuple[_TrackedConstraint, ...], bool]:
    core = list(seed)
    index = 0
    shrink_checks = 0
    complete = True
    while index < len(core):
        if shrink_checks >= limits.max_core_shrink_checks:
            complete = False
            break
        candidate = core[:index] + core[index + 1 :]
        check = budget.check(candidate)
        shrink_checks += 1
        if check.state is _CheckState.UNSAT:
            core = candidate
            continue
        if check.state in {
            _CheckState.RESOURCE_LIMIT,
            _CheckState.SOLVER_UNKNOWN,
        }:
            complete = False
        index += 1
    return tuple(core), complete


def _validate_inputs(
    graph: object,
    registry: object,
    limits: object,
) -> tuple[ComputationGraph, UnitRegistry, SolverLimits]:
    if type(graph) is not ComputationGraph:
        raise VerificationError("graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise VerificationError("registry must be an exact UnitRegistry")
    if type(limits) is not SolverLimits:
        raise VerificationError("limits must be an exact SolverLimits")
    try:
        graph.validate()
    except UnitSentinelError as error:
        raise VerificationError("graph contract is malformed or mutated") from error
    try:
        registry.validate()
    except UnitSentinelError as error:
        raise VerificationError("unit registry is malformed or mutated") from error
    limits.validate()
    return graph, registry, limits


def verify_graph(
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    limits: SolverLimits = _DEFAULT_SOLVER_LIMITS,
) -> VerificationResult:
    """Verify one immutable graph against one immutable registry snapshot.

    ``verified`` means dimensions, quantity kinds, numeric scales, and affine
    offsets are all uniquely determined and independently replayed. Any solver
    ambiguity, resource exhaustion, unsupported model value, or invalid unit
    provenance fails closed.
    """

    graph, registry, limits = _validate_inputs(graph, registry, limits)
    graph_digest = graph.digest
    registry_digest = registry.digest
    solver_version = z3.get_version_string()

    try:
        graph.validate_units(registry)
    except (GraphValidationError, UnitSentinelError):
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=0,
            reason=UnknownReason.CONTRACT_REJECTED,
        )

    started_at = time.monotonic()
    try:
        context = z3.Context()
        problem = _compile_problem(graph, registry, context)
    except MemoryError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=0,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    except (UnitSentinelError, z3.Z3Exception):
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=0,
            reason=UnknownReason.INTERNAL_INCONSISTENCY,
        )

    budget = _CheckBudget(
        context=problem.context,
        background=problem.background,
        limits=limits,
        started_at=started_at,
    )
    initial = budget.check(problem.constraints)
    if initial.state in {
        _CheckState.RESOURCE_LIMIT,
        _CheckState.SOLVER_UNKNOWN,
    }:
        return _unknown_from_check(
            check=initial,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
        )

    if initial.state is _CheckState.UNSAT:
        assert initial.solver is not None
        try:
            seed = _tracked_conflict_seed(problem, initial.solver)
        except MemoryError:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        except (VerificationError, z3.Z3Exception):
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.INTERNAL_INCONSISTENCY,
            )
        if budget.expired:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        core, core_minimal = _shrink_conflict(seed, budget, limits)
        if budget.expired:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        if not core:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.INTERNAL_INCONSISTENCY,
            )
        return VerificationResult(
            status=VerificationStatus.CONFLICT,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            conflict_core=tuple(constraint.witness for constraint in core),
            core_minimal=core_minimal,
        )

    assert initial.solver is not None
    try:
        model_values = _extract_model(problem, initial.solver)
    except MemoryError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    except z3.Z3Exception:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.SOLVER_UNKNOWN,
        )
    except VerificationError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.MODEL_OUT_OF_DOMAIN,
        )
    try:
        replay_valid = _replay_model(graph, registry, model_values)
    except MemoryError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    if not replay_valid:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.INTERNAL_INCONSISTENCY,
        )

    value_ids = tuple(sorted(problem.terms))
    uniqueness_checks = 0
    try:
        global_difference = _model_difference(problem, model_values, value_ids)
    except MemoryError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    except z3.Z3Exception:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.INTERNAL_INCONSISTENCY,
        )
    unique = budget.check(problem.constraints, extra=global_difference)
    uniqueness_checks += 1
    if unique.state in {
        _CheckState.RESOURCE_LIMIT,
        _CheckState.SOLVER_UNKNOWN,
    }:
        return _unknown_from_check(
            check=unique,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
        )
    if unique.state is _CheckState.UNSAT:
        try:
            verified_contracts = _contracts(value_ids, model_values)
        except MemoryError:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        except VerificationError:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.INTERNAL_INCONSISTENCY,
            )
        if budget.expired:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        return VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            contracts=verified_contracts,
        )

    underconstrained: list[str] = []
    uniquely_inferred: list[str] = []
    for value_id in value_ids:
        if uniqueness_checks >= limits.max_uniqueness_checks:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        try:
            value_difference = _model_difference(
                problem,
                model_values,
                (value_id,),
            )
        except MemoryError:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.RESOURCE_LIMIT,
            )
        except z3.Z3Exception:
            return _unknown_result(
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
                reason=UnknownReason.INTERNAL_INCONSISTENCY,
            )
        classification = budget.check(
            problem.constraints,
            extra=value_difference,
        )
        uniqueness_checks += 1
        if classification.state in {
            _CheckState.RESOURCE_LIMIT,
            _CheckState.SOLVER_UNKNOWN,
        }:
            return _unknown_from_check(
                check=classification,
                graph_digest=graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=budget.checks_performed,
            )
        if classification.state is _CheckState.SAT:
            underconstrained.append(value_id)
        else:
            uniquely_inferred.append(value_id)

    if not underconstrained:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.INTERNAL_INCONSISTENCY,
        )
    try:
        inferred_contracts = _contracts(uniquely_inferred, model_values)
    except MemoryError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    except VerificationError:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.INTERNAL_INCONSISTENCY,
        )
    if budget.expired:
        return _unknown_result(
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=budget.checks_performed,
            reason=UnknownReason.RESOURCE_LIMIT,
        )
    return VerificationResult(
        status=VerificationStatus.UNDERCONSTRAINED,
        graph_digest=graph_digest,
        registry_digest=registry_digest,
        solver_version=solver_version,
        limits=limits,
        checks_performed=budget.checks_performed,
        contracts=inferred_contracts,
        underconstrained_values=tuple(underconstrained),
    )
