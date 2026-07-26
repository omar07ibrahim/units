from __future__ import annotations

import unittest
from fractions import Fraction
from unittest.mock import patch

import unitsentinel.verifier as verifier_module
from unitsentinel.domain import (
    DIMENSIONLESS,
    LENGTH,
    MAX_EXPONENT_NUMERATOR,
    THERMODYNAMIC_TEMPERATURE,
    TIME,
    QuantityKind,
)
from unitsentinel.graph import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY, UnitRegistry
from unitsentinel.verification import (
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)
from unitsentinel.verifier import verify_graph


def value(value_id: str, unit_id: str | None = None) -> ValueSpec:
    return ValueSpec(value_id, ScalarType.FLOAT64, (), unit_id)


def graph(
    graph_id: str,
    *,
    values: tuple[ValueSpec, ...],
    inputs: tuple[str, ...],
    nodes: tuple[Node, ...] = (),
    outputs: tuple[str, ...],
) -> ComputationGraph:
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(sorted(values, key=lambda item: item.value_id)),
        inputs=inputs,
        nodes=nodes,
        outputs=outputs,
    )


def unary_graph(
    operation: Operation,
    *,
    input_unit: str | None,
    output_unit: str | None = None,
    exponent: Fraction | None = None,
    target_unit_id: str | None = None,
) -> ComputationGraph:
    return graph(
        f"{operation.value}-contract",
        values=(value("input", input_unit), value("output", output_unit)),
        inputs=("input",),
        nodes=(
            Node(
                f"apply-{operation.value}",
                operation,
                ("input",),
                "output",
                exponent=exponent,
                target_unit_id=target_unit_id,
            ),
        ),
        outputs=("output",),
    )


def binary_graph(
    operation: Operation,
    *,
    left_unit: str | None,
    right_unit: str | None,
    output_unit: str | None = None,
) -> ComputationGraph:
    return graph(
        f"{operation.value}-contract",
        values=(
            value("left", left_unit),
            value("output", output_unit),
            value("right", right_unit),
        ),
        inputs=("left", "right"),
        nodes=(
            Node(
                f"apply-{operation.value}",
                operation,
                ("left", "right"),
                "output",
            ),
        ),
        outputs=("output",),
    )


def verify(
    candidate: ComputationGraph,
    *,
    limits: SolverLimits | None = None,
) -> VerificationResult:
    return verify_graph(
        candidate,
        registry=BUILTIN_REGISTRY,
        limits=SolverLimits() if limits is None else limits,
    )


def contracts_by_id(result: VerificationResult) -> dict[str, InferredContract]:
    return {contract.value_id: contract for contract in result.contracts}


class VerificationOutcomeTests(unittest.TestCase):
    def test_identity_propagates_the_complete_unit_transform(self) -> None:
        candidate = unary_graph(
            Operation.IDENTITY,
            input_unit="kilometer",
        )

        result = verify(candidate)

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(result.graph_digest, candidate.digest)
        self.assertEqual(result.registry_digest, BUILTIN_REGISTRY.digest)
        self.assertGreaterEqual(result.checks_performed, 2)
        contracts = contracts_by_id(result)
        self.assertEqual(set(contracts), {"input", "output"})
        self.assertEqual(contracts["output"].dimension, LENGTH)
        self.assertEqual(contracts["output"].kind, QuantityKind.LINEAR)

    def test_unannotated_identity_reports_every_ambiguous_value(self) -> None:
        result = verify(
            unary_graph(
                Operation.IDENTITY,
                input_unit=None,
            )
        )

        self.assertEqual(result.status, VerificationStatus.UNDERCONSTRAINED)
        self.assertEqual(result.contracts, ())
        self.assertEqual(result.underconstrained_values, ("input", "output"))
        self.assertIsNone(result.unknown_reason)

    def test_explicit_conversion_fixes_its_target_transform(self) -> None:
        result = verify(
            unary_graph(
                Operation.CONVERT,
                input_unit="kilometer",
                target_unit_id="meter",
            )
        )

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        contracts = contracts_by_id(result)
        self.assertEqual(set(contracts), {"input", "output"})
        self.assertEqual(contracts["output"].dimension, LENGTH)
        self.assertEqual(contracts["output"].kind, QuantityKind.LINEAR)

    def test_conversion_with_unknown_source_transform_is_underconstrained(self) -> None:
        result = verify(
            unary_graph(
                Operation.CONVERT,
                input_unit=None,
                target_unit_id="meter",
            )
        )

        self.assertEqual(result.status, VerificationStatus.UNDERCONSTRAINED)
        self.assertEqual(result.underconstrained_values, ("input",))
        self.assertEqual(tuple(contracts_by_id(result)), ("output",))
        self.assertEqual(result.contracts[0].dimension, LENGTH)

    def test_conversion_rejects_a_mismatched_declared_output(self) -> None:
        result = verify(
            unary_graph(
                Operation.CONVERT,
                input_unit="kilometer",
                output_unit="centimeter",
                target_unit_id="meter",
            )
        )

        self.assertEqual(result.status, VerificationStatus.CONFLICT)
        self.assertTrue(result.conflict_core)
        self.assertIsNone(result.unknown_reason)


class LinearOperationTests(unittest.TestCase):
    def test_direct_same_dimension_operations_do_not_insert_conversions(self) -> None:
        operations = (
            Operation.ADD,
            Operation.SUBTRACT,
            Operation.MINIMUM,
            Operation.MAXIMUM,
        )
        for operation in operations:
            with self.subTest(operation=operation):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit="meter",
                        right_unit="centimeter",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.CONFLICT)

    def test_direct_arithmetic_propagates_a_compatible_transform(self) -> None:
        operations = (
            Operation.ADD,
            Operation.SUBTRACT,
            Operation.MINIMUM,
            Operation.MAXIMUM,
        )
        for operation in operations:
            with self.subTest(operation=operation):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit="meter",
                        right_unit="meter",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.VERIFIED)
                output = contracts_by_id(result)["output"]
                self.assertEqual(output.dimension, LENGTH)
                self.assertEqual(output.kind, QuantityKind.LINEAR)

    def test_dimensionless_scales_are_not_silently_interchangeable(self) -> None:
        result = verify(
            binary_graph(
                Operation.ADD,
                left_unit="one",
                right_unit="percent",
            )
        )

        self.assertEqual(result.status, VerificationStatus.CONFLICT)

    def test_scale_provenance_is_composed_by_multiplication(self) -> None:
        result = verify(
            binary_graph(
                Operation.MULTIPLY,
                left_unit="percent",
                right_unit="meter",
                output_unit="centimeter",
            )
        )

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(contracts_by_id(result)["output"].dimension, LENGTH)

    def test_scale_provenance_can_expose_a_dimensionless_mismatch(self) -> None:
        result = verify(
            binary_graph(
                Operation.DIVIDE,
                left_unit="kilometer",
                right_unit="meter",
                output_unit="one",
            )
        )

        self.assertEqual(result.status, VerificationStatus.CONFLICT)

    def test_verified_contract_publishes_unique_scale_and_offset(self) -> None:
        kilometer_ratio = verify(
            binary_graph(
                Operation.DIVIDE,
                left_unit="kilometer",
                right_unit="meter",
            )
        )
        meter_ratio = verify(
            binary_graph(
                Operation.DIVIDE,
                left_unit="meter",
                right_unit="meter",
            )
        )

        self.assertEqual(kilometer_ratio.status, VerificationStatus.VERIFIED)
        self.assertEqual(meter_ratio.status, VerificationStatus.VERIFIED)
        kilometer_output = contracts_by_id(kilometer_ratio)["output"]
        meter_output = contracts_by_id(meter_ratio)["output"]
        self.assertEqual(kilometer_output.dimension, DIMENSIONLESS)
        self.assertEqual(meter_output.dimension, DIMENSIONLESS)
        self.assertEqual(kilometer_output.scale, Fraction(1_000))
        self.assertEqual(meter_output.scale, Fraction(1))
        self.assertEqual(kilometer_output.offset, Fraction(0))
        self.assertNotEqual(
            kilometer_output.canonical_record(),
            meter_output.canonical_record(),
        )

    def test_multiplicative_operations_infer_exact_dimensions(self) -> None:
        cases = (
            (
                Operation.MULTIPLY,
                LENGTH.multiply(TIME),
            ),
            (
                Operation.DIVIDE,
                LENGTH.divide(TIME),
            ),
            (
                Operation.MATMUL,
                LENGTH.multiply(TIME),
            ),
        )
        for operation, expected in cases:
            with self.subTest(operation=operation):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit="meter",
                        right_unit="second",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.VERIFIED)
                self.assertEqual(
                    contracts_by_id(result)["output"].dimension,
                    expected,
                )

        power_result = verify(
            unary_graph(
                Operation.POWER,
                input_unit="meter",
                exponent=Fraction(1, 2),
            )
        )
        self.assertEqual(power_result.status, VerificationStatus.VERIFIED)
        self.assertEqual(
            contracts_by_id(power_result)["output"].dimension,
            LENGTH.power(Fraction(1, 2)),
        )

    def test_zero_and_negative_powers_preserve_exact_scale_provenance(self) -> None:
        zero = verify(
            unary_graph(
                Operation.POWER,
                input_unit="kilometer",
                output_unit="one",
                exponent=Fraction(0),
            )
        )
        reciprocal = verify(
            unary_graph(
                Operation.POWER,
                input_unit="kilometer",
                exponent=Fraction(-1),
            )
        )

        self.assertEqual(zero.status, VerificationStatus.VERIFIED)
        self.assertEqual(
            contracts_by_id(zero)["output"].dimension,
            DIMENSIONLESS,
        )
        self.assertEqual(reciprocal.status, VerificationStatus.VERIFIED)
        self.assertEqual(
            contracts_by_id(reciprocal)["output"].dimension,
            LENGTH.power(Fraction(-1)),
        )

    def test_irrational_inferred_scale_fails_the_fraction_trust_boundary(
        self,
    ) -> None:
        result = verify(
            unary_graph(
                Operation.POWER,
                input_unit="kilometer",
                exponent=Fraction(1, 2),
            )
        )

        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual(result.unknown_reason, UnknownReason.MODEL_OUT_OF_DOMAIN)


class TemperatureSemanticsTests(unittest.TestCase):
    def test_valid_temperature_truth_table_is_enforced(self) -> None:
        cases = (
            (
                Operation.ADD,
                "degree-celsius",
                "delta-celsius",
                "degree-celsius",
                QuantityKind.ABSOLUTE_TEMPERATURE,
            ),
            (
                Operation.ADD,
                "delta-celsius",
                "degree-celsius",
                "degree-celsius",
                QuantityKind.ABSOLUTE_TEMPERATURE,
            ),
            (
                Operation.ADD,
                "delta-celsius",
                "delta-celsius",
                "delta-celsius",
                QuantityKind.TEMPERATURE_DELTA,
            ),
            (
                Operation.SUBTRACT,
                "degree-celsius",
                "degree-celsius",
                "delta-celsius",
                QuantityKind.TEMPERATURE_DELTA,
            ),
            (
                Operation.SUBTRACT,
                "degree-celsius",
                "delta-celsius",
                "degree-celsius",
                QuantityKind.ABSOLUTE_TEMPERATURE,
            ),
            (
                Operation.SUBTRACT,
                "delta-celsius",
                "delta-celsius",
                "delta-celsius",
                QuantityKind.TEMPERATURE_DELTA,
            ),
            (
                Operation.MINIMUM,
                "degree-celsius",
                "degree-celsius",
                "degree-celsius",
                QuantityKind.ABSOLUTE_TEMPERATURE,
            ),
            (
                Operation.MAXIMUM,
                "degree-celsius",
                "degree-celsius",
                "degree-celsius",
                QuantityKind.ABSOLUTE_TEMPERATURE,
            ),
        )
        for operation, left, right, output, expected_kind in cases:
            with self.subTest(
                operation=operation,
                left=left,
                right=right,
            ):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit=left,
                        right_unit=right,
                        output_unit=output,
                    )
                )
                self.assertEqual(result.status, VerificationStatus.VERIFIED)
                inferred = contracts_by_id(result)["output"]
                self.assertEqual(inferred.dimension, THERMODYNAMIC_TEMPERATURE)
                self.assertEqual(inferred.kind, expected_kind)

    def test_invalid_temperature_truth_table_is_rejected(self) -> None:
        cases = (
            (
                Operation.ADD,
                "degree-celsius",
                "degree-celsius",
            ),
            (
                Operation.SUBTRACT,
                "delta-celsius",
                "degree-celsius",
            ),
            (
                Operation.MINIMUM,
                "degree-celsius",
                "delta-celsius",
            ),
            (
                Operation.MAXIMUM,
                "delta-celsius",
                "degree-celsius",
            ),
        )
        for operation, left, right in cases:
            with self.subTest(operation=operation, left=left, right=right):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit=left,
                        right_unit=right,
                    )
                )
                self.assertEqual(result.status, VerificationStatus.CONFLICT)

    def test_cross_scale_temperature_arithmetic_requires_convert(self) -> None:
        implicit = verify(
            binary_graph(
                Operation.SUBTRACT,
                left_unit="degree-celsius",
                right_unit="degree-fahrenheit",
                output_unit="delta-celsius",
            )
        )
        self.assertEqual(implicit.status, VerificationStatus.CONFLICT)

        explicit_graph = graph(
            "explicit-temperature-conversion",
            values=(
                value("celsius", "degree-celsius"),
                value("converted", "degree-celsius"),
                value("difference", "delta-celsius"),
                value("fahrenheit", "degree-fahrenheit"),
            ),
            inputs=("celsius", "fahrenheit"),
            nodes=(
                Node(
                    "convert-fahrenheit",
                    Operation.CONVERT,
                    ("fahrenheit",),
                    "converted",
                    target_unit_id="degree-celsius",
                ),
                Node(
                    "subtract-temperatures",
                    Operation.SUBTRACT,
                    ("celsius", "converted"),
                    "difference",
                ),
            ),
            outputs=("difference",),
        )
        explicit = verify(explicit_graph)
        self.assertEqual(explicit.status, VerificationStatus.VERIFIED)
        self.assertEqual(
            contracts_by_id(explicit)["difference"].kind,
            QuantityKind.TEMPERATURE_DELTA,
        )

    def test_absolute_temperatures_are_not_multiplicative_values(self) -> None:
        binary_cases = (
            (Operation.MULTIPLY, "degree-celsius", "one"),
            (Operation.MULTIPLY, "one", "kelvin"),
            (Operation.DIVIDE, "degree-celsius", "one"),
            (Operation.DIVIDE, "one", "kelvin"),
            (Operation.MATMUL, "degree-celsius", "one"),
        )
        for operation, left, right in binary_cases:
            with self.subTest(operation=operation, left=left, right=right):
                result = verify(
                    binary_graph(
                        operation,
                        left_unit=left,
                        right_unit=right,
                    )
                )
                self.assertEqual(result.status, VerificationStatus.CONFLICT)

        powered = verify(
            unary_graph(
                Operation.POWER,
                input_unit="kelvin",
                exponent=Fraction(1),
            )
        )
        self.assertEqual(powered.status, VerificationStatus.CONFLICT)

    def test_conversion_cannot_reinterpret_absolute_as_delta(self) -> None:
        result = verify(
            unary_graph(
                Operation.CONVERT,
                input_unit="degree-celsius",
                output_unit="delta-celsius",
                target_unit_id="delta-celsius",
            )
        )

        self.assertEqual(result.status, VerificationStatus.CONFLICT)

    def test_temperature_deltas_compose_without_becoming_absolute(self) -> None:
        cases = (
            (
                binary_graph(
                    Operation.DIVIDE,
                    left_unit="delta-kelvin",
                    right_unit="one",
                    output_unit="delta-kelvin",
                ),
                THERMODYNAMIC_TEMPERATURE,
                QuantityKind.TEMPERATURE_DELTA,
            ),
            (
                binary_graph(
                    Operation.DIVIDE,
                    left_unit="delta-kelvin",
                    right_unit="delta-kelvin",
                    output_unit="one",
                ),
                DIMENSIONLESS,
                QuantityKind.LINEAR,
            ),
            (
                binary_graph(
                    Operation.MULTIPLY,
                    left_unit="delta-kelvin",
                    right_unit="delta-kelvin",
                ),
                THERMODYNAMIC_TEMPERATURE.power(Fraction(2)),
                QuantityKind.LINEAR,
            ),
        )
        for candidate, expected_dimension, expected_kind in cases:
            with self.subTest(graph_id=candidate.graph_id):
                result = verify(candidate)
                self.assertEqual(result.status, VerificationStatus.VERIFIED)
                output = contracts_by_id(result)["output"]
                self.assertEqual(output.dimension, expected_dimension)
                self.assertEqual(output.kind, expected_kind)


class DimensionlessOperationTests(unittest.TestCase):
    def test_transcendentals_require_the_canonical_dimensionless_transform(
        self,
    ) -> None:
        operations = (
            Operation.EXP,
            Operation.LOG,
            Operation.SIGMOID,
            Operation.SOFTMAX,
        )
        for operation in operations:
            with self.subTest(operation=operation, case="canonical"):
                result = verify(
                    unary_graph(
                        operation,
                        input_unit="one",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.VERIFIED)
                output = contracts_by_id(result)["output"]
                self.assertEqual(output.dimension, DIMENSIONLESS)
                self.assertEqual(output.kind, QuantityKind.LINEAR)

            with self.subTest(operation=operation, case="scaled-input"):
                result = verify(
                    unary_graph(
                        operation,
                        input_unit="percent",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.CONFLICT)

            with self.subTest(operation=operation, case="scaled-output"):
                result = verify(
                    unary_graph(
                        operation,
                        input_unit="one",
                        output_unit="percent",
                    )
                )
                self.assertEqual(result.status, VerificationStatus.CONFLICT)


class DiagnosticAndBoundTests(unittest.TestCase):
    def test_conflict_core_is_public_minimal_and_deterministic(self) -> None:
        candidate = graph(
            "incompatible-addition",
            values=(
                value("distance", "meter"),
                value("duration", "second"),
                value("sum"),
            ),
            inputs=("distance", "duration"),
            nodes=(
                Node(
                    "add-values",
                    Operation.ADD,
                    ("distance", "duration"),
                    "sum",
                ),
            ),
            outputs=("sum",),
        )

        first = verify(candidate)
        second = verify(candidate)

        self.assertEqual(first.status, VerificationStatus.CONFLICT)
        self.assertIs(first.core_minimal, True)
        self.assertEqual(
            tuple(item.constraint_id for item in first.conflict_core),
            (
                "declaration/distance/unit",
                "declaration/duration/unit",
                "operation/add-values/dimension",
            ),
        )
        self.assertEqual(first.conflict_core, second.conflict_core)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.digest, second.digest)

    def test_tracked_core_excludes_a_long_irrelevant_suffix(self) -> None:
        values = [
            value("distance", "meter"),
            value("duration", "second"),
            value("v000"),
        ]
        nodes = [
            Node(
                "a-conflict",
                Operation.ADD,
                ("distance", "duration"),
                "v000",
            )
        ]
        for index in range(1, 31):
            previous = f"v{index - 1:03d}"
            current = f"v{index:03d}"
            values.append(value(current))
            nodes.append(
                Node(
                    f"b{index:03d}",
                    Operation.IDENTITY,
                    (previous,),
                    current,
                )
            )
        candidate = graph(
            "tracked-core-suffix",
            values=tuple(values),
            inputs=("distance", "duration"),
            nodes=tuple(nodes),
            outputs=("v030",),
        )

        result = verify(candidate)

        self.assertEqual(result.status, VerificationStatus.CONFLICT)
        self.assertIs(result.core_minimal, True)
        self.assertLessEqual(result.checks_performed, 5)
        self.assertEqual(
            tuple(item.constraint_id for item in result.conflict_core),
            (
                "declaration/distance/unit",
                "declaration/duration/unit",
                "operation/a-conflict/dimension",
            ),
        )
        self.assertNotIn(b"tracked_", result.canonical_bytes())

    def test_zero_shrink_budget_preserves_a_known_conflict(self) -> None:
        candidate = binary_graph(
            Operation.ADD,
            left_unit="meter",
            right_unit="second",
        )

        result = verify(
            candidate,
            limits=SolverLimits(max_core_shrink_checks=0),
        )

        self.assertEqual(result.status, VerificationStatus.CONFLICT)
        self.assertTrue(result.conflict_core)
        self.assertIs(result.core_minimal, False)

    def test_uniqueness_budget_exhaustion_fails_closed(self) -> None:
        result = verify(
            unary_graph(
                Operation.IDENTITY,
                input_unit=None,
            ),
            limits=SolverLimits(max_uniqueness_checks=1),
        )

        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual(result.unknown_reason, UnknownReason.RESOURCE_LIMIT)
        self.assertEqual(result.contracts, ())
        self.assertEqual(result.underconstrained_values, ())

    def test_out_of_domain_inferred_exponent_fails_closed(self) -> None:
        candidate = graph(
            "out-of-domain-exponent",
            values=(
                value("base", "meter"),
                value("extreme"),
                value("powered"),
            ),
            inputs=("base",),
            nodes=(
                Node(
                    "raise-to-limit",
                    Operation.POWER,
                    ("base",),
                    "powered",
                    exponent=Fraction(MAX_EXPONENT_NUMERATOR),
                ),
                Node(
                    "exceed-limit",
                    Operation.MULTIPLY,
                    ("powered", "base"),
                    "extreme",
                ),
            ),
            outputs=("extreme",),
        )

        result = verify(candidate)

        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual(result.unknown_reason, UnknownReason.MODEL_OUT_OF_DOMAIN)
        self.assertEqual(result.contracts, ())

    def test_alias_and_unknown_units_are_contract_rejections(self) -> None:
        for unit_id in ("metre", "yard"):
            with self.subTest(unit_id=unit_id):
                candidate = graph(
                    "rejected-unit-contract",
                    values=(value("input", unit_id),),
                    inputs=("input",),
                    outputs=("input",),
                )

                result = verify(candidate)

                self.assertEqual(result.status, VerificationStatus.UNKNOWN)
                self.assertEqual(
                    result.unknown_reason,
                    UnknownReason.CONTRACT_REJECTED,
                )
                self.assertEqual(result.checks_performed, 0)
                self.assertEqual(result.graph_digest, candidate.digest)
                self.assertEqual(result.registry_digest, BUILTIN_REGISTRY.digest)

    def test_verifier_rejects_wrong_receivers_and_mutated_inputs(self) -> None:
        candidate = unary_graph(Operation.IDENTITY, input_unit="meter")
        with self.assertRaisesRegex(VerificationError, "exact ComputationGraph"):
            verify_graph("graph")  # type: ignore[arg-type]
        with self.assertRaisesRegex(VerificationError, "exact UnitRegistry"):
            verify_graph(candidate, registry="registry")  # type: ignore[arg-type]
        with self.assertRaisesRegex(VerificationError, "exact SolverLimits"):
            verify_graph(candidate, limits="limits")  # type: ignore[arg-type]

        object.__setattr__(candidate.values[0], "unit_id", "kilometer")
        with self.assertRaisesRegex(VerificationError, "malformed or mutated"):
            verify(candidate)

        registry = UnitRegistry(
            BUILTIN_REGISTRY.version,
            BUILTIN_REGISTRY.units,
            BUILTIN_REGISTRY.aliases,
        )
        object.__setattr__(registry, "version", "1.0.1")
        fresh_candidate = unary_graph(Operation.IDENTITY, input_unit="meter")
        with self.assertRaisesRegex(VerificationError, "malformed or mutated"):
            verify_graph(fresh_candidate, registry=registry)

    def test_deadline_and_solver_unknown_never_become_certificates(self) -> None:
        candidate = unary_graph(Operation.IDENTITY, input_unit="meter")
        with patch(
            "unitsentinel.verifier.time.monotonic",
            side_effect=(0.0, 10.0),
        ):
            exhausted = verify(
                candidate,
                limits=SolverLimits(
                    per_check_timeout_ms=1,
                    total_timeout_ms=1,
                ),
            )
        self.assertEqual(exhausted.status, VerificationStatus.UNKNOWN)
        self.assertEqual(exhausted.unknown_reason, UnknownReason.RESOURCE_LIMIT)
        self.assertEqual(exhausted.checks_performed, 0)

        forced_unknown = verifier_module._CheckResult(
            verifier_module._CheckState.SOLVER_UNKNOWN
        )
        with patch.object(
            verifier_module._CheckBudget,
            "check",
            return_value=forced_unknown,
        ):
            unknown = verify(candidate)
        self.assertEqual(unknown.status, VerificationStatus.UNKNOWN)
        self.assertEqual(unknown.unknown_reason, UnknownReason.SOLVER_UNKNOWN)
        self.assertEqual(unknown.contracts, ())

        with patch.object(
            verifier_module.z3,
            "Solver",
            side_effect=verifier_module.z3.Z3Exception("private diagnostic"),
        ):
            solver_failure = verify(candidate)
        self.assertEqual(solver_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            solver_failure.unknown_reason,
            UnknownReason.SOLVER_UNKNOWN,
        )
        self.assertNotIn(b"private diagnostic", solver_failure.canonical_bytes())

        with patch.object(
            verifier_module.z3,
            "Solver",
            side_effect=MemoryError,
        ):
            memory_failure = verify(candidate)
        self.assertEqual(memory_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            memory_failure.unknown_reason,
            UnknownReason.RESOURCE_LIMIT,
        )

    def test_whole_deadline_is_rechecked_after_successful_solver_checks(
        self,
    ) -> None:
        candidate = unary_graph(Operation.IDENTITY, input_unit="meter")
        clock = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 10.0)
        with patch(
            "unitsentinel.verifier.time.monotonic",
            side_effect=clock,
        ):
            result = verify(
                candidate,
                limits=SolverLimits(
                    per_check_timeout_ms=1_000,
                    total_timeout_ms=1_000,
                ),
            )

        self.assertEqual(result.status, VerificationStatus.UNKNOWN)
        self.assertEqual(result.unknown_reason, UnknownReason.RESOURCE_LIMIT)
        self.assertEqual(result.checks_performed, 2)
        self.assertEqual(result.contracts, ())

    def test_post_sat_model_failures_are_closed_and_redacted(self) -> None:
        candidate = unary_graph(Operation.IDENTITY, input_unit="meter")
        with patch.object(
            verifier_module,
            "_extract_model",
            side_effect=verifier_module.z3.Z3Exception("raw model /host/private"),
        ):
            solver_failure = verify(candidate)
        self.assertEqual(solver_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            solver_failure.unknown_reason,
            UnknownReason.SOLVER_UNKNOWN,
        )
        self.assertNotIn(b"/host/private", solver_failure.canonical_bytes())

        with patch.object(
            verifier_module,
            "_extract_model",
            side_effect=MemoryError("raw memory /host/private"),
        ):
            memory_failure = verify(candidate)
        self.assertEqual(memory_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            memory_failure.unknown_reason,
            UnknownReason.RESOURCE_LIMIT,
        )
        self.assertNotIn(b"/host/private", memory_failure.canonical_bytes())

        with patch.object(
            verifier_module,
            "_model_difference",
            side_effect=verifier_module.z3.Z3Exception("raw expression"),
        ):
            expression_failure = verify(candidate)
        self.assertEqual(expression_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            expression_failure.unknown_reason,
            UnknownReason.INTERNAL_INCONSISTENCY,
        )
        self.assertNotIn(b"raw expression", expression_failure.canonical_bytes())

        with patch.object(
            verifier_module,
            "_replay_model",
            side_effect=MemoryError("raw replay /host/private"),
        ):
            replay_failure = verify(candidate)
        self.assertEqual(replay_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            replay_failure.unknown_reason,
            UnknownReason.RESOURCE_LIMIT,
        )
        self.assertNotIn(b"/host/private", replay_failure.canonical_bytes())

        with patch.object(
            verifier_module,
            "_contracts",
            side_effect=MemoryError("raw contracts /host/private"),
        ):
            contract_failure = verify(candidate)
        self.assertEqual(contract_failure.status, VerificationStatus.UNKNOWN)
        self.assertEqual(
            contract_failure.unknown_reason,
            UnknownReason.RESOURCE_LIMIT,
        )
        self.assertNotIn(b"/host/private", contract_failure.canonical_bytes())


if __name__ == "__main__":
    unittest.main()
