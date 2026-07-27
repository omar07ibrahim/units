from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from unittest.mock import patch

import unitsentinel.comparison as comparison_module
from unitsentinel.comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_RESULT_SCHEMA,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonError,
    ComparisonPolicy,
    ComparisonReason,
    ComparisonResult,
    ComparisonStatus,
    ContractComparison,
    InterfaceSnapshot,
    MismatchCode,
    compare_graphs,
)
from unitsentinel.comparison_contract import (
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from unitsentinel.domain import TIME
from unitsentinel.graph import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY
from unitsentinel.verification import (
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationResult,
    VerificationStatus,
)
from unitsentinel.verifier import verify_graph


def value(
    value_id: str,
    unit_id: str | None,
    *,
    dtype: ScalarType = ScalarType.FLOAT64,
    shape: tuple[int | str, ...] = (),
) -> ValueSpec:
    return ValueSpec(value_id, dtype, shape, unit_id)


def boundary_graph(
    graph_id: str,
    values: tuple[ValueSpec, ...],
    *,
    inputs: tuple[str, ...] | None = None,
    outputs: tuple[str, ...] | None = None,
) -> ComputationGraph:
    value_ids = tuple(item.value_id for item in values)
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(sorted(values, key=lambda item: item.value_id)),
        inputs=value_ids if inputs is None else inputs,
        nodes=(),
        outputs=value_ids if outputs is None else outputs,
    )


def identity_graph(
    graph_id: str,
    *,
    input_id: str,
    output_id: str,
    input_unit: str | None,
    output_unit: str | None,
) -> ComputationGraph:
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(
            sorted(
                (
                    value(input_id, input_unit),
                    value(output_id, output_unit),
                ),
                key=lambda item: item.value_id,
            )
        ),
        inputs=(input_id,),
        nodes=(
            Node(
                node_id="propagate-contract",
                operation=Operation.IDENTITY,
                inputs=(input_id,),
                output=output_id,
            ),
        ),
        outputs=(output_id,),
    )


def endpoint(role: InterfaceRole, value_id: str) -> InterfaceEndpoint:
    return InterfaceEndpoint(role, value_id)


def plan(
    comparison_id: str,
    training: ComputationGraph,
    serving: ComputationGraph,
    bindings: tuple[ContractBinding, ...],
) -> ComparisonPlan:
    return ComparisonPlan(
        comparison_id=comparison_id,
        training_graph_digest=training.digest,
        serving_graph_digest=serving.digest,
        registry_digest=BUILTIN_REGISTRY.digest,
        bindings=tuple(sorted(bindings, key=lambda item: item.contract_id)),
    )


def aligned_plan(
    training: ComputationGraph,
    serving: ComputationGraph,
    pairs: tuple[tuple[str, str], ...],
    *,
    comparison_id: str = "aligned-boundaries",
) -> ComparisonPlan:
    bindings: list[ContractBinding] = []
    for index, (training_id, serving_id) in enumerate(pairs):
        for role in InterfaceRole:
            bindings.append(
                ContractBinding(
                    contract_id=f"{role.value}-{index:02d}",
                    training=endpoint(role, training_id),
                    serving=endpoint(role, serving_id),
                )
            )
    return plan(comparison_id, training, serving, tuple(bindings))


def interface_plan(
    training: ComputationGraph,
    serving: ComputationGraph,
    *,
    training_input: str,
    serving_input: str,
    training_output: str,
    serving_output: str,
    comparison_id: str = "interface-boundaries",
) -> ComparisonPlan:
    return plan(
        comparison_id,
        training,
        serving,
        (
            ContractBinding(
                "logical-input",
                endpoint(InterfaceRole.INPUT, training_input),
                endpoint(InterfaceRole.INPUT, serving_input),
            ),
            ContractBinding(
                "logical-output",
                endpoint(InterfaceRole.OUTPUT, training_output),
                endpoint(InterfaceRole.OUTPUT, serving_output),
            ),
        ),
    )


def one_value_pair(
    *,
    training_id: str = "training-value",
    serving_id: str = "serving-value",
    training_unit: str | None = "meter",
    serving_unit: str | None = "meter",
) -> tuple[ComputationGraph, ComputationGraph, ComparisonPlan]:
    training = boundary_graph(
        "training-graph",
        (value(training_id, training_unit),),
    )
    serving = boundary_graph(
        "serving-graph",
        (value(serving_id, serving_unit),),
    )
    return (
        training,
        serving,
        aligned_plan(training, serving, ((training_id, serving_id),)),
    )


def run(
    comparison_plan: ComparisonPlan,
    training: ComputationGraph,
    serving: ComputationGraph,
    *,
    limits: SolverLimits | None = None,
    policy: ComparisonPolicy | None = None,
) -> ComparisonResult:
    return compare_graphs(
        comparison_plan,
        training_graph=training,
        serving_graph=serving,
        limits=SolverLimits() if limits is None else limits,
        policy=ComparisonPolicy() if policy is None else policy,
    )


class ComparisonOutcomeTests(unittest.TestCase):
    def test_explicit_rename_is_compatible_only_under_the_exact_plan(self) -> None:
        training, serving, comparison_plan = one_value_pair()

        result = run(
            comparison_plan,
            training,
            serving,
            policy=ComparisonPolicy(comparison_plan.digest),
        )

        self.assertEqual(result.status, ComparisonStatus.COMPATIBLE)
        self.assertIsNone(result.reason)
        self.assertEqual(result.mismatch_count, 0)
        self.assertEqual(result.plan_digest, comparison_plan.digest)
        self.assertEqual(result.authentication, AUTHENTICATION_NOT_PROVIDED)
        self.assertEqual(result.scope, COMPARISON_SCOPE_UNDER_PLAN)
        self.assertEqual(
            result.canonical_record()["schema"],
            COMPARISON_RESULT_SCHEMA,
        )
        self.assertEqual(
            result.canonical_record()["authentication"],
            "not-provided",
        )
        self.assertEqual(result.canonical_record()["scope"], "under-plan")
        self.assertEqual(len(result.digest), 64)
        self.assertFalse(result.canonical_bytes().endswith(b"\n"))
        self.assertEqual(
            [item.contract_id for item in result.comparisons],
            ["input-00", "output-00"],
        )
        self.assertNotEqual(
            result.comparisons[0].training.endpoint.value_id,  # type: ignore[union-attr]
            result.comparisons[0].serving.endpoint.value_id,  # type: ignore[union-attr]
        )

    def test_all_metadata_drift_codes_are_exact_and_stably_ordered(self) -> None:
        training_values = (
            value(
                "dtype-shape",
                "meter",
                dtype=ScalarType.FLOAT64,
                shape=(),
            ),
            value("explicit-unit", "delta-kelvin"),
            value("dimension", "meter"),
            value("kind", "kelvin"),
            value("scale", "meter"),
            value("offset", "kelvin"),
        )
        serving_values = (
            value(
                "dtype-shape",
                "meter",
                dtype=ScalarType.FLOAT32,
                shape=(2,),
            ),
            value("explicit-unit", "delta-celsius"),
            value("dimension", "second"),
            value("kind", "delta-kelvin"),
            value("scale", "kilometer"),
            value("offset", "degree-celsius"),
        )
        training = boundary_graph("training-metadata", training_values)
        serving = boundary_graph("serving-metadata", serving_values)
        pairs = tuple((item.value_id, item.value_id) for item in training_values)
        comparison_plan = aligned_plan(training, serving, pairs)

        result = run(comparison_plan, training, serving)

        self.assertEqual(result.status, ComparisonStatus.DRIFT)
        by_id = {item.contract_id: item.mismatches for item in result.comparisons}
        expected_by_index = (
            (MismatchCode.DTYPE_DRIFT, MismatchCode.SHAPE_DRIFT),
            (MismatchCode.EXPLICIT_UNIT_DRIFT,),
            (
                MismatchCode.EXPLICIT_UNIT_DRIFT,
                MismatchCode.DIMENSION_DRIFT,
            ),
            (
                MismatchCode.EXPLICIT_UNIT_DRIFT,
                MismatchCode.KIND_DRIFT,
            ),
            (
                MismatchCode.EXPLICIT_UNIT_DRIFT,
                MismatchCode.SCALE_DRIFT,
            ),
            (
                MismatchCode.EXPLICIT_UNIT_DRIFT,
                MismatchCode.OFFSET_DRIFT,
            ),
        )
        for index, expected in enumerate(expected_by_index):
            with self.subTest(index=index):
                self.assertEqual(by_id[f"input-{index:02d}"], expected)
                self.assertEqual(by_id[f"output-{index:02d}"], expected)
        self.assertEqual(
            tuple(MismatchCode),
            (
                MismatchCode.MISSING_IN_SERVING,
                MismatchCode.EXTRA_IN_SERVING,
                MismatchCode.ROLE_DRIFT,
                MismatchCode.POSITION_DRIFT,
                MismatchCode.DTYPE_DRIFT,
                MismatchCode.SHAPE_DRIFT,
                MismatchCode.EXPLICIT_UNIT_DRIFT,
                MismatchCode.DIMENSION_DRIFT,
                MismatchCode.KIND_DRIFT,
                MismatchCode.SCALE_DRIFT,
                MismatchCode.OFFSET_DRIFT,
            ),
        )

    def test_inferred_semantic_drifts_are_independent_of_explicit_units(
        self,
    ) -> None:
        cases = (
            ("dimension", "meter", "second", MismatchCode.DIMENSION_DRIFT),
            ("kind", "kelvin", "delta-kelvin", MismatchCode.KIND_DRIFT),
            ("scale", "meter", "centimeter", MismatchCode.SCALE_DRIFT),
            ("offset", "kelvin", "degree-celsius", MismatchCode.OFFSET_DRIFT),
        )
        for label, training_unit, serving_unit, expected in cases:
            with self.subTest(label=label):
                training = identity_graph(
                    f"training-{label}",
                    input_id="training-input",
                    output_id="training-output",
                    input_unit=None,
                    output_unit=training_unit,
                )
                serving = identity_graph(
                    f"serving-{label}",
                    input_id="serving-input",
                    output_id="serving-output",
                    input_unit=None,
                    output_unit=serving_unit,
                )
                comparison_plan = interface_plan(
                    training,
                    serving,
                    training_input="training-input",
                    serving_input="serving-input",
                    training_output="training-output",
                    serving_output="serving-output",
                    comparison_id=f"inferred-{label}",
                )

                result = run(comparison_plan, training, serving)

                self.assertEqual(result.status, ComparisonStatus.DRIFT)
                input_comparison = result.comparisons[0]
                self.assertEqual(input_comparison.contract_id, "logical-input")
                self.assertEqual(input_comparison.mismatches, (expected,))
                assert input_comparison.training is not None
                assert input_comparison.serving is not None
                self.assertIsNone(input_comparison.training.value.unit_id)
                self.assertIsNone(input_comparison.serving.value.unit_id)

    def test_position_is_role_local_and_role_drift_does_not_imply_position(
        self,
    ) -> None:
        training = boundary_graph(
            "training-order",
            (value("alpha", "meter"), value("beta", "meter")),
        )
        serving = boundary_graph(
            "serving-order",
            (value("first", "meter"), value("second", "meter")),
        )
        position_plan = aligned_plan(
            training,
            serving,
            (("alpha", "second"), ("beta", "first")),
        )

        positioned = run(position_plan, training, serving)

        self.assertEqual(positioned.status, ComparisonStatus.DRIFT)
        for item in positioned.comparisons:
            self.assertEqual(item.mismatches, (MismatchCode.POSITION_DRIFT,))

        one_training = boundary_graph(
            "training-role",
            (value("shared", "meter"),),
        )
        one_serving = boundary_graph(
            "serving-role",
            (value("shared", "meter"),),
        )
        role_plan = plan(
            "crossed-roles",
            one_training,
            one_serving,
            (
                ContractBinding(
                    "logical-input",
                    endpoint(InterfaceRole.INPUT, "shared"),
                    endpoint(InterfaceRole.OUTPUT, "shared"),
                ),
                ContractBinding(
                    "logical-output",
                    endpoint(InterfaceRole.OUTPUT, "shared"),
                    endpoint(InterfaceRole.INPUT, "shared"),
                ),
            ),
        )

        crossed = run(role_plan, one_training, one_serving)

        self.assertEqual(crossed.status, ComparisonStatus.DRIFT)
        for item in crossed.comparisons:
            self.assertEqual(item.mismatches, (MismatchCode.ROLE_DRIFT,))

    def test_one_sided_bindings_report_missing_and_extra(self) -> None:
        training, serving, _ = one_value_pair()
        one_sided = plan(
            "one-sided-boundaries",
            training,
            serving,
            (
                ContractBinding(
                    "serving-input",
                    None,
                    endpoint(InterfaceRole.INPUT, "serving-value"),
                ),
                ContractBinding(
                    "serving-output",
                    None,
                    endpoint(InterfaceRole.OUTPUT, "serving-value"),
                ),
                ContractBinding(
                    "training-input",
                    endpoint(InterfaceRole.INPUT, "training-value"),
                    None,
                ),
                ContractBinding(
                    "training-output",
                    endpoint(InterfaceRole.OUTPUT, "training-value"),
                    None,
                ),
            ),
        )

        result = run(one_sided, training, serving)

        self.assertEqual(result.status, ComparisonStatus.DRIFT)
        self.assertEqual(
            [item.mismatches for item in result.comparisons],
            [
                (MismatchCode.EXTRA_IN_SERVING,),
                (MismatchCode.EXTRA_IN_SERVING,),
                (MismatchCode.MISSING_IN_SERVING,),
                (MismatchCode.MISSING_IN_SERVING,),
            ],
        )

    def test_same_value_in_both_roles_is_two_covered_occurrences(self) -> None:
        training, serving, comparison_plan = one_value_pair(
            training_id="shared",
            serving_id="shared",
        )

        result = run(comparison_plan, training, serving)

        self.assertEqual(result.status, ComparisonStatus.COMPATIBLE)
        self.assertEqual(len(result.comparisons), 2)
        self.assertEqual(
            {item.training.endpoint.role for item in result.comparisons},  # type: ignore[union-attr]
            {InterfaceRole.INPUT, InterfaceRole.OUTPUT},
        )

    def test_maximum_plan_is_bounded_and_repeats_canonical_output(self) -> None:
        training_values = tuple(
            value(f"training-{index:02d}", "meter") for index in range(64)
        )
        serving_values = tuple(
            value(f"serving-{index:02d}", "meter") for index in range(64)
        )
        training = boundary_graph("maximum-training", training_values)
        serving = boundary_graph("maximum-serving", serving_values)
        bindings: list[ContractBinding] = []
        for role in InterfaceRole:
            for index in range(64):
                bindings.append(
                    ContractBinding(
                        contract_id=f"training-{role.value}-{index:02d}",
                        training=endpoint(
                            role,
                            f"training-{index:02d}",
                        ),
                        serving=None,
                    )
                )
                bindings.append(
                    ContractBinding(
                        contract_id=f"serving-{role.value}-{index:02d}",
                        training=None,
                        serving=endpoint(
                            role,
                            f"serving-{index:02d}",
                        ),
                    )
                )
        comparison_plan = plan(
            "maximum-interface-plan",
            training,
            serving,
            tuple(bindings),
        )

        with patch.object(
            comparison_module,
            "verify_graph",
            wraps=verify_graph,
        ) as verifier:
            first = run(comparison_plan, training, serving)
        second = run(comparison_plan, training, serving)

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(len(comparison_plan.bindings), 256)
        self.assertEqual(first.status, ComparisonStatus.DRIFT)
        self.assertEqual(len(first.comparisons), 256)
        self.assertEqual(first.mismatch_count, 256)
        self.assertTrue(all(len(item.mismatches) == 1 for item in first.comparisons))
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.digest, second.digest)


class FailClosedInputTests(unittest.TestCase):
    def test_expected_digest_pin_precedes_mutated_binding_interpretation(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        training_endpoint = comparison_plan.bindings[0].training
        assert training_endpoint is not None
        object.__setattr__(training_endpoint, "value_id", "INVALID")

        with (
            patch.object(
                comparison_module,
                "verify_graph",
                side_effect=AssertionError("verifier must not run"),
            ) as verifier,
            self.assertRaisesRegex(ComparisonError, "caller-trusted digest pin"),
        ):
            run(
                comparison_plan,
                training,
                serving,
                policy=ComparisonPolicy("f" * 64),
            )

        verifier.assert_not_called()

    def test_plan_source_digest_mismatches_stop_before_verification(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        cases = (
            ComparisonPlan(
                comparison_plan.comparison_id,
                "1" * 64,
                comparison_plan.serving_graph_digest,
                comparison_plan.registry_digest,
                comparison_plan.bindings,
            ),
            ComparisonPlan(
                comparison_plan.comparison_id,
                comparison_plan.training_graph_digest,
                "2" * 64,
                comparison_plan.registry_digest,
                comparison_plan.bindings,
            ),
            ComparisonPlan(
                comparison_plan.comparison_id,
                comparison_plan.training_graph_digest,
                comparison_plan.serving_graph_digest,
                "3" * 64,
                comparison_plan.bindings,
            ),
        )
        for candidate in cases:
            with (
                self.subTest(candidate=candidate),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=AssertionError("verifier must not run"),
                ) as verifier,
                self.assertRaisesRegex(ComparisonError, "digest does not match"),
            ):
                run(candidate, training, serving)
            verifier.assert_not_called()

    def test_unknown_endpoint_and_incomplete_occurrence_coverage_are_errors(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        unknown = plan(
            "unknown-endpoint",
            training,
            serving,
            (
                ContractBinding(
                    "bad-input",
                    endpoint(InterfaceRole.INPUT, "training-value"),
                    endpoint(InterfaceRole.INPUT, "serving-value"),
                ),
                ContractBinding(
                    "bad-output",
                    endpoint(InterfaceRole.OUTPUT, "training-value"),
                    endpoint(InterfaceRole.OUTPUT, "missing"),
                ),
            ),
        )
        incomplete = ComparisonPlan(
            "incomplete-plan",
            training.digest,
            serving.digest,
            BUILTIN_REGISTRY.digest,
            (comparison_plan.bindings[0],),
        )
        for candidate, message in (
            (unknown, "not a declared public occurrence"),
            (incomplete, "does not cover every"),
        ):
            with (
                self.subTest(candidate=candidate),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=AssertionError("verifier must not run"),
                ) as verifier,
                self.assertRaisesRegex(ComparisonError, message),
            ):
                run(candidate, training, serving)
            verifier.assert_not_called()

    def test_public_boundary_types_are_exact(self) -> None:
        training, serving, comparison_plan = one_value_pair()

        class DerivedGraph(ComputationGraph):
            pass

        class DerivedPolicy(ComparisonPolicy):
            pass

        with self.assertRaisesRegex(ComparisonError, "exact ComputationGraph"):
            compare_graphs(
                comparison_plan,
                training_graph=object(),  # type: ignore[arg-type]
                serving_graph=serving,
                policy=ComparisonPolicy("f" * 64),
            )
        with self.assertRaisesRegex(ComparisonError, "exact SolverLimits"):
            compare_graphs(
                comparison_plan,
                training_graph=training,
                serving_graph=serving,
                limits=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ComparisonError, "exact ComparisonPolicy"):
            run(
                comparison_plan,
                training,
                serving,
                policy=DerivedPolicy(),
            )
        with self.assertRaisesRegex(ComparisonError, "expected.*malformed"):
            ComparisonPolicy("A" * 64)
        derived = object.__new__(DerivedGraph)
        with self.assertRaisesRegex(ComparisonError, "exact ComputationGraph"):
            compare_graphs(
                comparison_plan,
                training_graph=derived,
                serving_graph=serving,
            )

        with self.assertRaisesRegex(ComparisonError, "exact ComparisonPlan"):
            compare_graphs(
                object(),  # type: ignore[arg-type]
                training_graph=training,
                serving_graph=serving,
            )
        with self.assertRaisesRegex(ComparisonError, "exact ComputationGraph"):
            compare_graphs(
                comparison_plan,
                training_graph=training,
                serving_graph=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ComparisonError, "exact UnitRegistry"):
            compare_graphs(
                comparison_plan,
                training_graph=training,
                serving_graph=serving,
                registry=object(),  # type: ignore[arg-type]
            )

    def test_mutated_contract_and_pin_failures_are_stable_input_errors(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        object.__setattr__(comparison_plan, "comparison_id", "INVALID")
        with self.assertRaisesRegex(ComparisonError, "malformed or mutated"):
            run(comparison_plan, training, serving)

        training, serving, comparison_plan = one_value_pair()
        with (
            patch.object(
                comparison_module.z3,
                "get_version_string",
                return_value="development",
            ),
            self.assertRaisesRegex(ComparisonError, "could not be pinned"),
        ):
            run(comparison_plan, training, serving)

    def test_serving_only_coverage_omission_is_distinguished(self) -> None:
        training, serving, _ = one_value_pair()
        incomplete = plan(
            "missing-serving-output",
            training,
            serving,
            (
                ContractBinding(
                    "mapped-input",
                    endpoint(InterfaceRole.INPUT, "training-value"),
                    endpoint(InterfaceRole.INPUT, "serving-value"),
                ),
                ContractBinding(
                    "training-output",
                    endpoint(InterfaceRole.OUTPUT, "training-value"),
                    None,
                ),
            ),
        )

        with self.assertRaisesRegex(
            ComparisonError,
            "every serving public occurrence",
        ):
            run(incomplete, training, serving)


class FreshVerificationTests(unittest.TestCase):
    def test_both_verifiers_run_when_one_graph_is_not_verified(self) -> None:
        training, serving, _ = one_value_pair(training_unit=None)
        comparison_plan = aligned_plan(
            training,
            serving,
            (("training-value", "serving-value"),),
        )

        with patch.object(
            comparison_module,
            "verify_graph",
            wraps=verify_graph,
        ) as verifier:
            result = run(comparison_plan, training, serving)

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.TRAINING_NOT_VERIFIED)
        self.assertEqual(result.comparisons, ())
        self.assertEqual(result.mismatch_count, 0)
        assert result.training_result is not None
        assert result.serving_result is not None
        self.assertEqual(
            result.training_result.status,
            VerificationStatus.UNDERCONSTRAINED,
        )
        self.assertEqual(
            result.serving_result.status,
            VerificationStatus.VERIFIED,
        )

    def test_two_negative_results_have_one_bounded_indeterminate_outcome(
        self,
    ) -> None:
        training, serving, _ = one_value_pair(
            training_unit=None,
            serving_unit=None,
        )
        comparison_plan = aligned_plan(
            training,
            serving,
            (("training-value", "serving-value"),),
        )

        result = run(comparison_plan, training, serving)

        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.BOTH_NOT_VERIFIED)
        self.assertEqual(result.comparisons, ())

    def test_conflict_outcomes_are_side_aware_and_publish_no_interface_diff(
        self,
    ) -> None:
        training_verified = identity_graph(
            "training-verified",
            input_id="training-input",
            output_id="training-output",
            input_unit="meter",
            output_unit="meter",
        )
        training_conflict = identity_graph(
            "training-conflict",
            input_id="training-input",
            output_id="training-output",
            input_unit="meter",
            output_unit="second",
        )
        serving_verified = identity_graph(
            "serving-verified",
            input_id="serving-input",
            output_id="serving-output",
            input_unit="meter",
            output_unit="meter",
        )
        serving_conflict = identity_graph(
            "serving-conflict",
            input_id="serving-input",
            output_id="serving-output",
            input_unit="meter",
            output_unit="second",
        )
        cases = (
            (
                training_conflict,
                serving_verified,
                ComparisonReason.TRAINING_NOT_VERIFIED,
            ),
            (
                training_verified,
                serving_conflict,
                ComparisonReason.SERVING_NOT_VERIFIED,
            ),
            (
                training_conflict,
                serving_conflict,
                ComparisonReason.BOTH_NOT_VERIFIED,
            ),
        )
        for training, serving, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                comparison_plan = interface_plan(
                    training,
                    serving,
                    training_input="training-input",
                    serving_input="serving-input",
                    training_output="training-output",
                    serving_output="serving-output",
                    comparison_id=f"conflict-{expected_reason.value}",
                )

                result = run(comparison_plan, training, serving)

                self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
                self.assertEqual(result.reason, expected_reason)
                self.assertEqual(result.comparisons, ())
                assert result.training_result is not None
                assert result.serving_result is not None
                self.assertIn(
                    VerificationStatus.CONFLICT,
                    {
                        result.training_result.status,
                        result.serving_result.status,
                    },
                )

    def test_unknown_outcomes_are_side_aware_and_publish_no_interface_diff(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        verified_training = verify_graph(training)
        verified_serving = verify_graph(serving)

        def unknown(result: VerificationResult) -> VerificationResult:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                graph_digest=result.graph_digest,
                registry_digest=result.registry_digest,
                solver_version=result.solver_version,
                limits=result.limits,
                checks_performed=result.checks_performed,
                unknown_reason=UnknownReason.RESOURCE_LIMIT,
            )

        cases = (
            (
                unknown(verified_training),
                verified_serving,
                ComparisonReason.TRAINING_NOT_VERIFIED,
            ),
            (
                verified_training,
                unknown(verified_serving),
                ComparisonReason.SERVING_NOT_VERIFIED,
            ),
            (
                unknown(verified_training),
                unknown(verified_serving),
                ComparisonReason.BOTH_NOT_VERIFIED,
            ),
        )
        for training_result, serving_result, expected_reason in cases:
            with (
                self.subTest(reason=expected_reason),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=(training_result, serving_result),
                ) as verifier,
            ):
                result = run(comparison_plan, training, serving)

            self.assertEqual(verifier.call_count, 2)
            self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
            self.assertEqual(result.reason, expected_reason)
            self.assertEqual(result.comparisons, ())

    def test_exception_is_redacted_but_second_verifier_still_runs(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        serving_result = verify_graph(serving)

        with patch.object(
            comparison_module,
            "verify_graph",
            side_effect=(RuntimeError("secret detail"), serving_result),
        ) as verifier:
            result = run(comparison_plan, training, serving)

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
        self.assertIsNone(result.training_result)
        self.assertEqual(result.serving_result, serving_result)
        self.assertEqual(result.comparisons, ())
        self.assertNotIn("secret", result.canonical_bytes().decode())

    def test_serving_exception_is_also_redacted(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        training_result = verify_graph(training)

        with patch.object(
            comparison_module,
            "verify_graph",
            side_effect=(training_result, RuntimeError("private serving detail")),
        ) as verifier:
            result = run(comparison_plan, training, serving)

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
        self.assertEqual(result.training_result, training_result)
        self.assertIsNone(result.serving_result)
        self.assertNotIn("private", result.canonical_bytes().decode())

    def test_nonexact_mutated_and_wrong_identity_results_fail_closed(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        valid_training = verify_graph(training)
        valid_serving = verify_graph(serving)
        mutated = verify_graph(training)
        object.__setattr__(
            mutated.contracts[0],
            "scale",
            mutated.contracts[0].scale * 2,
        )
        wrong_identity = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest="a" * 64,
            registry_digest=valid_training.registry_digest,
            solver_version=valid_training.solver_version,
            limits=valid_training.limits,
            checks_performed=valid_training.checks_performed,
            contracts=valid_training.contracts,
        )
        cases: tuple[object, ...] = (object(), mutated, wrong_identity)
        for candidate in cases:
            with (
                self.subTest(candidate=type(candidate).__name__),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=(candidate, valid_serving),
                ) as verifier,
            ):
                result = run(comparison_plan, training, serving)
            self.assertEqual(verifier.call_count, 2)
            self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
            self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
            self.assertIsNone(result.training_result)
            self.assertEqual(result.comparisons, ())

    def test_wrong_registry_limits_and_solver_identity_results_fail_closed(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        valid_training = verify_graph(training)
        valid_serving = verify_graph(serving)
        different_limits = SolverLimits(
            per_check_timeout_ms=251,
            total_timeout_ms=5_000,
        )

        def changed(
            *,
            registry_digest: str = valid_training.registry_digest,
            limits: SolverLimits = valid_training.limits,
            solver_version: str = valid_training.solver_version,
        ) -> VerificationResult:
            return VerificationResult(
                status=VerificationStatus.VERIFIED,
                graph_digest=valid_training.graph_digest,
                registry_digest=registry_digest,
                solver_version=solver_version,
                limits=limits,
                checks_performed=valid_training.checks_performed,
                contracts=valid_training.contracts,
            )

        candidates = (
            changed(registry_digest="b" * 64),
            changed(limits=different_limits),
            changed(solver_version="0.0.0"),
        )
        for candidate in candidates:
            with (
                self.subTest(candidate=candidate.digest),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=(candidate, valid_serving),
                ),
            ):
                result = run(comparison_plan, training, serving)

            self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
            self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
            self.assertIsNone(result.training_result)
            self.assertEqual(result.comparisons, ())

    def test_partial_or_semantically_false_verified_contracts_are_rejected(
        self,
    ) -> None:
        training = boundary_graph(
            "training-two-values",
            (value("alpha", "meter"), value("beta", "meter")),
        )
        serving = boundary_graph(
            "serving-two-values",
            (value("first", "meter"), value("second", "meter")),
        )
        comparison_plan = aligned_plan(
            training,
            serving,
            (("alpha", "first"), ("beta", "second")),
        )
        fresh_training = verify_graph(training)
        fresh_serving = verify_graph(serving)
        partial = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=fresh_training.graph_digest,
            registry_digest=fresh_training.registry_digest,
            solver_version=fresh_training.solver_version,
            limits=fresh_training.limits,
            checks_performed=fresh_training.checks_performed,
            contracts=fresh_training.contracts[:1],
        )
        first = fresh_training.contracts[0]
        forged_contracts = (
            InferredContract(
                first.value_id,
                first.dimension,
                first.kind,
                first.scale * 2,
                first.offset,
            ),
            fresh_training.contracts[1],
        )
        forged = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=fresh_training.graph_digest,
            registry_digest=fresh_training.registry_digest,
            solver_version=fresh_training.solver_version,
            limits=fresh_training.limits,
            checks_performed=fresh_training.checks_performed,
            contracts=forged_contracts,
        )
        for candidate in (partial, forged):
            with (
                self.subTest(candidate=candidate.digest),
                patch.object(
                    comparison_module,
                    "verify_graph",
                    side_effect=(candidate, fresh_serving),
                ),
            ):
                result = run(comparison_plan, training, serving)
            self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
            self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
            self.assertIsNone(result.training_result)
            self.assertEqual(result.comparisons, ())

    def test_incomplete_underconstrained_coverage_is_verifier_failure(self) -> None:
        training = boundary_graph(
            "ambiguous-training",
            (value("alpha", None), value("beta", None)),
        )
        serving = boundary_graph(
            "verified-serving",
            (value("first", "meter"), value("second", "meter")),
        )
        comparison_plan = aligned_plan(
            training,
            serving,
            (("alpha", "first"), ("beta", "second")),
        )
        actual = verify_graph(training)
        incomplete = VerificationResult(
            status=VerificationStatus.UNDERCONSTRAINED,
            graph_digest=actual.graph_digest,
            registry_digest=actual.registry_digest,
            solver_version=actual.solver_version,
            limits=actual.limits,
            checks_performed=actual.checks_performed,
            underconstrained_values=("alpha",),
        )
        serving_result = verify_graph(serving)

        with patch.object(
            comparison_module,
            "verify_graph",
            side_effect=(incomplete, serving_result),
        ):
            result = run(comparison_plan, training, serving)

        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
        self.assertIsNone(result.training_result)
        self.assertEqual(result.comparisons, ())

    def test_input_mutation_during_verification_is_a_contract_error(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        original = verify_graph(training)

        def mutate_input(
            candidate: ComputationGraph,
            *,
            registry: object,
            limits: object,
        ) -> VerificationResult:
            object.__setattr__(candidate, "graph_id", "INVALID")
            return original

        with (
            patch.object(
                comparison_module,
                "verify_graph",
                side_effect=mutate_input,
            ),
            self.assertRaisesRegex(ComparisonError, "changed during verification"),
        ):
            run(comparison_plan, training, serving)

    def test_second_verifier_cannot_mutate_the_first_accepted_candidate(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        training_result = verify_graph(training)
        serving_result = verify_graph(serving)
        calls = 0

        def mutate_first_result(
            candidate: ComputationGraph,
            *,
            registry: object,
            limits: object,
        ) -> VerificationResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return training_result
            object.__setattr__(
                training_result.contracts[0],
                "scale",
                training_result.contracts[0].scale * 2,
            )
            return serving_result

        with patch.object(
            comparison_module,
            "verify_graph",
            side_effect=mutate_first_result,
        ) as verifier:
            result = run(comparison_plan, training, serving)

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
        self.assertIsNone(result.training_result)
        self.assertEqual(result.serving_result, serving_result)
        self.assertEqual(result.comparisons, ())

    def test_policy_mutation_and_replay_exception_fail_closed(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        policy = ComparisonPolicy(comparison_plan.digest)
        fresh_training = verify_graph(training)

        def mutate_policy(
            candidate: ComputationGraph,
            *,
            registry: object,
            limits: object,
        ) -> VerificationResult:
            object.__setattr__(policy, "expected_plan_digest", "e" * 64)
            return fresh_training

        with (
            patch.object(
                comparison_module,
                "verify_graph",
                side_effect=mutate_policy,
            ),
            self.assertRaisesRegex(ComparisonError, "changed during verification"),
        ):
            run(
                comparison_plan,
                training,
                serving,
                policy=policy,
            )

        training, serving, comparison_plan = one_value_pair()
        with patch.object(
            comparison_module,
            "_replay_claimed_contracts",
            side_effect=RuntimeError("replay detail"),
        ):
            result = run(comparison_plan, training, serving)

        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason, ComparisonReason.VERIFIER_FAILURE)
        self.assertIsNone(result.training_result)
        self.assertIsNone(result.serving_result)
        self.assertNotIn("replay detail", result.canonical_bytes().decode())


class ResultContractTests(unittest.TestCase):
    def test_result_and_nested_values_are_frozen_and_mutation_detecting(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        result = run(comparison_plan, training, serving)

        with self.assertRaises(FrozenInstanceError):
            result.status = ComparisonStatus.DRIFT  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.comparisons[0].contract_id = "changed"  # type: ignore[misc]
        snapshot = result.comparisons[0].training
        assert snapshot is not None
        with self.assertRaises(FrozenInstanceError):
            snapshot.position = 9  # type: ignore[misc]

        object.__setattr__(snapshot.inferred, "scale", Fraction(2))
        with self.assertRaisesRegex(ComparisonError, "malformed or mutated"):
            result.canonical_record()

    def test_snapshot_requires_exact_consistent_bounded_values(self) -> None:
        training, _, _ = one_value_pair()
        verified = verify_graph(training)
        endpoint_value = endpoint(InterfaceRole.INPUT, "training-value")
        declared = training.value("training-value")
        inferred = verified.contracts[0]

        with self.assertRaisesRegex(ComparisonError, "exact InterfaceEndpoint"):
            InterfaceSnapshot(
                object(),  # type: ignore[arg-type]
                0,
                declared,
                inferred,
            )
        with self.assertRaisesRegex(ComparisonError, "exact integer"):
            InterfaceSnapshot(
                endpoint_value,
                True,  # type: ignore[arg-type]
                declared,
                inferred,
            )
        with self.assertRaisesRegex(ComparisonError, "out of bounds"):
            InterfaceSnapshot(endpoint_value, 64, declared, inferred)
        with self.assertRaisesRegex(ComparisonError, "identities"):
            InterfaceSnapshot(
                endpoint_value,
                0,
                value("other", "meter"),
                inferred,
            )

        class DerivedSnapshot(InterfaceSnapshot):
            pass

        derived = object.__new__(DerivedSnapshot)
        with self.assertRaisesRegex(ComparisonError, "exact InterfaceSnapshot"):
            derived.validate()
        with self.assertRaisesRegex(ComparisonError, "exact ValueSpec"):
            InterfaceSnapshot(
                endpoint_value,
                0,
                object(),  # type: ignore[arg-type]
                inferred,
            )
        with self.assertRaisesRegex(ComparisonError, "exact InferredContract"):
            InterfaceSnapshot(
                endpoint_value,
                0,
                declared,
                object(),  # type: ignore[arg-type]
            )
        damaged = InterfaceEndpoint(InterfaceRole.INPUT, "training-value")
        object.__setattr__(damaged, "value_id", "INVALID")
        with self.assertRaisesRegex(ComparisonError, "malformed or mutated"):
            InterfaceSnapshot(damaged, 0, declared, inferred)

    def test_contract_comparison_recomputes_codes_and_rejects_duplicates(
        self,
    ) -> None:
        training, serving, comparison_plan = one_value_pair()
        result = run(comparison_plan, training, serving)
        first = result.comparisons[0]
        assert first.training is not None
        assert first.serving is not None

        with self.assertRaisesRegex(ComparisonError, "incomplete or out of order"):
            ContractComparison(
                first.contract_id,
                first.training,
                first.serving,
                (MismatchCode.DTYPE_DRIFT,),
            )
        with self.assertRaisesRegex(ComparisonError, "training or serving"):
            ContractComparison("empty", None, None, ())
        with self.assertRaisesRegex(ComparisonError, "not canonical"):
            ContractComparison("INVALID", first.training, first.serving, ())
        with self.assertRaisesRegex(ComparisonError, "exact InterfaceSnapshot"):
            ContractComparison(
                "bad-snapshot",
                object(),  # type: ignore[arg-type]
                None,
                (MismatchCode.MISSING_IN_SERVING,),
            )
        with self.assertRaisesRegex(ComparisonError, "must be a tuple"):
            ContractComparison(
                "bad-container",
                first.training,
                first.serving,
                [],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ComparisonError, "exact MismatchCode"):
            ContractComparison(
                "bad-code",
                first.training,
                first.serving,
                ("dtype-drift",),  # type: ignore[arg-type]
            )

        class DerivedComparison(ContractComparison):
            pass

        derived = object.__new__(DerivedComparison)
        with self.assertRaisesRegex(ComparisonError, "exact ContractComparison"):
            derived.validate()
        with self.assertRaisesRegex(ComparisonError, "at most once"):
            ComparisonResult(
                status=ComparisonStatus.COMPATIBLE,
                reason=None,
                comparison_id=result.comparison_id,
                plan_digest=result.plan_digest,
                training_graph_digest=result.training_graph_digest,
                serving_graph_digest=result.serving_graph_digest,
                registry_digest=result.registry_digest,
                limits=result.limits,
                training_result=result.training_result,
                serving_result=result.serving_result,
                comparisons=(first, first),
            )

    def test_result_snapshots_must_match_their_nested_verifications(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        result = run(comparison_plan, training, serving)
        first = result.comparisons[0]
        assert first.training is not None
        assert first.serving is not None
        forged_training = InterfaceSnapshot(
            endpoint=first.training.endpoint,
            position=first.training.position,
            value=first.training.value,
            inferred=InferredContract(
                value_id=first.training.inferred.value_id,
                dimension=TIME,
                kind=first.training.inferred.kind,
                scale=first.training.inferred.scale,
                offset=first.training.inferred.offset,
            ),
        )
        forged_serving = InterfaceSnapshot(
            endpoint=first.serving.endpoint,
            position=first.serving.position,
            value=first.serving.value,
            inferred=InferredContract(
                value_id=first.serving.inferred.value_id,
                dimension=TIME,
                kind=first.serving.inferred.kind,
                scale=first.serving.inferred.scale,
                offset=first.serving.inferred.offset,
            ),
        )
        forged = ContractComparison(
            contract_id=first.contract_id,
            training=forged_training,
            serving=forged_serving,
            mismatches=(),
        )

        with self.assertRaisesRegex(
            ComparisonError,
            "snapshot contradicts its verification result",
        ):
            ComparisonResult(
                status=ComparisonStatus.COMPATIBLE,
                reason=None,
                comparison_id=result.comparison_id,
                plan_digest=result.plan_digest,
                training_graph_digest=result.training_graph_digest,
                serving_graph_digest=result.serving_graph_digest,
                registry_digest=result.registry_digest,
                limits=result.limits,
                training_result=result.training_result,
                serving_result=result.serving_result,
                comparisons=(forged, *result.comparisons[1:]),
            )

        second = result.comparisons[1]
        assert second.training is not None
        assert second.serving is not None
        changed_declaration = ValueSpec(
            value_id=second.training.value.value_id,
            dtype=second.training.value.dtype,
            shape=second.training.value.shape,
            unit_id="centimeter",
        )
        changed_snapshot = InterfaceSnapshot(
            endpoint=second.training.endpoint,
            position=second.training.position,
            value=changed_declaration,
            inferred=second.training.inferred,
        )
        inconsistent_value = ContractComparison(
            contract_id=second.contract_id,
            training=changed_snapshot,
            serving=second.serving,
            mismatches=(MismatchCode.EXPLICIT_UNIT_DRIFT,),
        )
        with self.assertRaisesRegex(
            ComparisonError,
            "snapshots disagree about one declared value",
        ):
            ComparisonResult(
                status=ComparisonStatus.DRIFT,
                reason=None,
                comparison_id=result.comparison_id,
                plan_digest=result.plan_digest,
                training_graph_digest=result.training_graph_digest,
                serving_graph_digest=result.serving_graph_digest,
                registry_digest=result.registry_digest,
                limits=result.limits,
                training_result=result.training_result,
                serving_result=result.serving_result,
                comparisons=(first, inconsistent_value),
            )

    def test_outcome_reason_and_interface_shapes_cannot_be_mixed(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        decisive = run(comparison_plan, training, serving)
        assert decisive.training_result is not None
        assert decisive.serving_result is not None

        common: dict[str, object] = {
            "comparison_id": decisive.comparison_id,
            "plan_digest": decisive.plan_digest,
            "training_graph_digest": decisive.training_graph_digest,
            "serving_graph_digest": decisive.serving_graph_digest,
            "registry_digest": decisive.registry_digest,
            "limits": decisive.limits,
            "training_result": decisive.training_result,
            "serving_result": decisive.serving_result,
        }
        with self.assertRaisesRegex(ComparisonError, "indeterminate"):
            ComparisonResult(
                status=ComparisonStatus.INDETERMINATE,
                reason=ComparisonReason.TRAINING_NOT_VERIFIED,
                comparisons=(),
                **common,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ComparisonError, "decisive"):
            ComparisonResult(
                status=ComparisonStatus.COMPATIBLE,
                reason=ComparisonReason.VERIFIER_FAILURE,
                comparisons=decisive.comparisons,
                **common,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ComparisonError, "must contain a mismatch"):
            ComparisonResult(
                status=ComparisonStatus.DRIFT,
                reason=None,
                comparisons=decisive.comparisons,
                **common,  # type: ignore[arg-type]
            )

        drift = run(
            plan(
                "one-sided-result",
                training,
                serving,
                (
                    ContractBinding(
                        "serving-input",
                        None,
                        endpoint(InterfaceRole.INPUT, "serving-value"),
                    ),
                    ContractBinding(
                        "serving-output",
                        None,
                        endpoint(InterfaceRole.OUTPUT, "serving-value"),
                    ),
                    ContractBinding(
                        "training-input",
                        endpoint(InterfaceRole.INPUT, "training-value"),
                        None,
                    ),
                    ContractBinding(
                        "training-output",
                        endpoint(InterfaceRole.OUTPUT, "training-value"),
                        None,
                    ),
                ),
            ),
            training,
            serving,
        )
        with self.assertRaisesRegex(ComparisonError, "cannot contain mismatches"):
            ComparisonResult(
                status=ComparisonStatus.COMPATIBLE,
                reason=None,
                comparison_id=drift.comparison_id,
                plan_digest=drift.plan_digest,
                training_graph_digest=drift.training_graph_digest,
                serving_graph_digest=drift.serving_graph_digest,
                registry_digest=drift.registry_digest,
                limits=drift.limits,
                training_result=drift.training_result,
                serving_result=drift.serving_result,
                comparisons=drift.comparisons,
            )

    def test_result_rejects_invalid_nested_types_reasons_and_digests(self) -> None:
        training, serving, comparison_plan = one_value_pair()
        result = run(comparison_plan, training, serving)
        assert result.training_result is not None
        assert result.serving_result is not None
        common: dict[str, object] = {
            "status": ComparisonStatus.COMPATIBLE,
            "reason": None,
            "comparison_id": result.comparison_id,
            "plan_digest": result.plan_digest,
            "training_graph_digest": result.training_graph_digest,
            "serving_graph_digest": result.serving_graph_digest,
            "registry_digest": result.registry_digest,
            "limits": result.limits,
            "training_result": result.training_result,
            "serving_result": result.serving_result,
            "comparisons": result.comparisons,
        }

        cases = (
            ({"status": "compatible"}, "status"),
            ({"reason": "verifier-failure"}, "reason"),
            ({"limits": object()}, "exact SolverLimits"),
            ({"training_result": object()}, "exact VerificationResult"),
            ({"comparisons": []}, "must be a tuple"),
            ({"comparisons": (object(),)}, "exact ContractComparison"),
            ({"plan_digest": "bad"}, "digest"),
        )
        for changes, message in cases:
            arguments = dict(common)
            arguments.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ComparisonError, message),
            ):
                ComparisonResult(**arguments)  # type: ignore[arg-type]

        verifier_failure = dict(common)
        verifier_failure.update(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.VERIFIER_FAILURE,
            comparisons=(),
        )
        with self.assertRaisesRegex(ComparisonError, "indeterminate"):
            ComparisonResult(**verifier_failure)  # type: ignore[arg-type]

        ambiguous_training, ambiguous_serving, _ = one_value_pair(
            training_unit=None,
            serving_unit=None,
        )
        ambiguous_plan = aligned_plan(
            ambiguous_training,
            ambiguous_serving,
            (("training-value", "serving-value"),),
        )
        ambiguous = run(
            ambiguous_plan,
            ambiguous_training,
            ambiguous_serving,
        )
        with self.assertRaisesRegex(ComparisonError, "verifier-failure"):
            ComparisonResult(
                status=ComparisonStatus.INDETERMINATE,
                reason=ComparisonReason.VERIFIER_FAILURE,
                comparison_id=ambiguous.comparison_id,
                plan_digest=ambiguous.plan_digest,
                training_graph_digest=ambiguous.training_graph_digest,
                serving_graph_digest=ambiguous.serving_graph_digest,
                registry_digest=ambiguous.registry_digest,
                limits=ambiguous.limits,
                training_result=ambiguous.training_result,
                serving_result=ambiguous.serving_result,
                comparisons=(),
            )

        missing_result = dict(common)
        missing_result.update(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.TRAINING_NOT_VERIFIED,
            training_result=None,
            comparisons=(),
        )
        with self.assertRaisesRegex(ComparisonError, "nonverified"):
            ComparisonResult(**missing_result)  # type: ignore[arg-type]

        damaged_digest = run(comparison_plan, training, serving)
        object.__setattr__(damaged_digest, "_digest", "bad")
        with self.assertRaisesRegex(ComparisonError, "digest is malformed"):
            damaged_digest.validate()

        damaged_contents = run(comparison_plan, training, serving)
        object.__setattr__(damaged_contents, "comparison_id", "changed")
        with self.assertRaisesRegex(ComparisonError, "does not match"):
            damaged_contents.validate()


if __name__ == "__main__":
    unittest.main()
