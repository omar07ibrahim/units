from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from unittest.mock import patch

import z3  # type: ignore[import-untyped]

import unitsentinel.lineage as lineage_module
from unitsentinel.comparison import ComparisonPolicy
from unitsentinel.comparison_contract import (
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from unitsentinel.domain import DIMENSIONLESS, QuantityKind
from unitsentinel.graph import (
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.lineage import (
    LINEAGE_AUTHENTICATION,
    NORMALIZATION_LINEAGE_SCHEMA,
    LineageError,
    LineageExpression,
    LineageSide,
    NormalizationLineage,
    NormalizationSite,
    OutputLineage,
    extract_normalization_lineage,
)
from unitsentinel.registry import BUILTIN_REGISTRY
from unitsentinel.verification import (
    InferredContract,
    SolverLimits,
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


def graph(
    graph_id: str,
    *,
    values: tuple[ValueSpec, ...],
    inputs: tuple[str, ...],
    nodes: tuple[Node, ...],
    outputs: tuple[str, ...],
) -> ComputationGraph:
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(sorted(values, key=lambda item: item.value_id)),
        inputs=inputs,
        nodes=nodes,
        outputs=outputs,
    )


def endpoint(role: InterfaceRole, value_id: str) -> InterfaceEndpoint:
    return InterfaceEndpoint(role, value_id)


def side_plan(
    candidate: ComputationGraph,
    *,
    inputs: tuple[tuple[str, str], ...],
    outputs: tuple[tuple[str, str], ...],
    side: LineageSide = LineageSide.TRAINING,
    comparison_id: str = "lineage-plan",
) -> ComparisonPlan:
    bindings: list[ContractBinding] = []
    for contract_id, value_id in inputs:
        selected = endpoint(InterfaceRole.INPUT, value_id)
        bindings.append(
            ContractBinding(
                contract_id,
                selected if side is LineageSide.TRAINING else None,
                selected if side is LineageSide.SERVING else None,
            )
        )
    for contract_id, value_id in outputs:
        selected = endpoint(InterfaceRole.OUTPUT, value_id)
        bindings.append(
            ContractBinding(
                contract_id,
                selected if side is LineageSide.TRAINING else None,
                selected if side is LineageSide.SERVING else None,
            )
        )
    return ComparisonPlan(
        comparison_id=comparison_id,
        training_graph_digest=(
            candidate.digest if side is LineageSide.TRAINING else "a" * 64
        ),
        serving_graph_digest=(
            candidate.digest if side is LineageSide.SERVING else "b" * 64
        ),
        registry_digest=BUILTIN_REGISTRY.digest,
        bindings=tuple(sorted(bindings, key=lambda item: item.contract_id)),
    )


def paired_plan(
    training: ComputationGraph,
    serving: ComputationGraph,
    *,
    training_inputs: tuple[str, ...],
    serving_inputs: tuple[str, ...],
    training_outputs: tuple[str, ...],
    serving_outputs: tuple[str, ...],
) -> ComparisonPlan:
    bindings: list[ContractBinding] = []
    for index, (training_id, serving_id) in enumerate(
        zip(training_inputs, serving_inputs, strict=True)
    ):
        bindings.append(
            ContractBinding(
                f"root-{index:02d}",
                endpoint(InterfaceRole.INPUT, training_id),
                endpoint(InterfaceRole.INPUT, serving_id),
            )
        )
    for index, (training_id, serving_id) in enumerate(
        zip(training_outputs, serving_outputs, strict=True)
    ):
        bindings.append(
            ContractBinding(
                f"result-{index:02d}",
                endpoint(InterfaceRole.OUTPUT, training_id),
                endpoint(InterfaceRole.OUTPUT, serving_id),
            )
        )
    return ComparisonPlan(
        "paired-lineage",
        training.digest,
        serving.digest,
        BUILTIN_REGISTRY.digest,
        tuple(sorted(bindings, key=lambda item: item.contract_id)),
    )


def extract(
    comparison_plan: ComparisonPlan,
    candidate: ComputationGraph,
    *,
    side: LineageSide = LineageSide.TRAINING,
    result: VerificationResult | None = None,
    limits: SolverLimits | None = None,
    policy: ComparisonPolicy | None = None,
) -> NormalizationLineage:
    selected_limits = SolverLimits() if limits is None else limits
    verified = (
        verify_graph(candidate, limits=selected_limits) if result is None else result
    )
    return extract_normalization_lineage(
        comparison_plan,
        side=side,
        graph=candidate,
        verification_result=verified,
        limits=selected_limits,
        policy=(ComparisonPolicy(comparison_plan.digest) if policy is None else policy),
    )


def explicit_verified_result(
    candidate: ComputationGraph,
    *,
    limits: SolverLimits | None = None,
) -> VerificationResult:
    selected_limits = SolverLimits() if limits is None else limits
    contracts: list[InferredContract] = []
    for item in candidate.values:
        if item.unit_id is None:
            raise AssertionError(
                "explicit verification helper requires unit identifiers"
            )
        unit = BUILTIN_REGISTRY.resolve(item.unit_id)
        contracts.append(
            InferredContract(
                item.value_id,
                unit.dimension,
                unit.kind,
                unit.scale,
                unit.offset,
            )
        )
    return VerificationResult(
        status=VerificationStatus.VERIFIED,
        graph_digest=candidate.digest,
        registry_digest=BUILTIN_REGISTRY.digest,
        solver_version=z3.get_version_string(),
        limits=selected_limits,
        checks_performed=0,
        contracts=tuple(contracts),
    )


def ratio_pipeline(
    graph_id: str,
    *,
    ratio_id: str = "ratio",
    divide_node_id: str = "normalize",
    identity_node_id: str = "publish",
    output_id: str = "normalized",
) -> ComputationGraph:
    return graph(
        graph_id,
        values=(
            value("left", "meter"),
            value("right", "meter"),
            value(ratio_id, "one"),
            value(output_id, "one"),
        ),
        inputs=("left", "right"),
        nodes=(
            Node(
                divide_node_id,
                Operation.DIVIDE,
                ("left", "right"),
                ratio_id,
            ),
            Node(
                identity_node_id,
                Operation.IDENTITY,
                (ratio_id,),
                output_id,
            ),
        ),
        outputs=(output_id,),
    )


def ratio_plan(candidate: ComputationGraph) -> ComparisonPlan:
    return side_plan(
        candidate,
        inputs=(("root-left", "left"), ("root-right", "right")),
        outputs=(("result-normalized", "normalized"),),
    )


def binary_graph(operation: Operation, *, swapped: bool = False) -> ComputationGraph:
    inputs = ("right", "left") if swapped else ("left", "right")
    return graph(
        f"{operation.value}-{'reverse' if swapped else 'forward'}",
        values=(
            value("left", "meter"),
            value("right", "meter"),
            value("result", None),
        ),
        inputs=("left", "right"),
        nodes=(Node("apply-operation", operation, inputs, "result"),),
        outputs=("result",),
    )


def binary_plan(candidate: ComputationGraph) -> ComparisonPlan:
    return side_plan(
        candidate,
        inputs=(("root-left", "left"), ("root-right", "right")),
        outputs=(("result-value", "result"),),
    )


def copy_expression(
    source: LineageExpression,
    **changes: object,
) -> LineageExpression:
    arguments: dict[str, object] = {
        "value_id": source.value_id,
        "node_id": source.node_id,
        "operation": source.operation,
        "attributes": source.attributes,
        "input_value_ids": source.input_value_ids,
        "child_digests": source.child_digests,
        "logical_roots": source.logical_roots,
        "collapsed_identity": source.collapsed_identity,
        "value": source.value,
        "inferred": source.inferred,
    }
    arguments.update(changes)
    return LineageExpression(**arguments)  # type: ignore[arg-type]


def copy_site(
    source: NormalizationSite,
    **changes: object,
) -> NormalizationSite:
    arguments: dict[str, object] = {
        "node_id": source.node_id,
        "value_id": source.value_id,
        "expression_digest": source.expression_digest,
        "logical_roots": source.logical_roots,
        "logical_outputs": source.logical_outputs,
    }
    arguments.update(changes)
    return NormalizationSite(**arguments)  # type: ignore[arg-type]


def copy_output(
    source: OutputLineage,
    **changes: object,
) -> OutputLineage:
    arguments: dict[str, object] = {
        "contract_id": source.contract_id,
        "value_id": source.value_id,
        "position": source.position,
        "expression_digest": source.expression_digest,
        "site_digests": source.site_digests,
    }
    arguments.update(changes)
    return OutputLineage(**arguments)  # type: ignore[arg-type]


def copy_lineage(
    source: NormalizationLineage,
    **changes: object,
) -> NormalizationLineage:
    arguments: dict[str, object] = {
        "side": source.side,
        "comparison_id": source.comparison_id,
        "plan_digest": source.plan_digest,
        "graph_digest": source.graph_digest,
        "registry_digest": source.registry_digest,
        "limits": source.limits,
        "verification_result": source.verification_result,
        "expressions": source.expressions,
        "sites": source.sites,
        "outputs": source.outputs,
    }
    arguments.update(changes)
    return NormalizationLineage(**arguments)  # type: ignore[arg-type]


class SemanticLineageTests(unittest.TestCase):
    def test_internal_renames_preserve_semantics_but_change_diagnostics(
        self,
    ) -> None:
        training = ratio_pipeline("training-ratio")
        serving = ratio_pipeline(
            "serving-ratio",
            ratio_id="internal-ratio",
            divide_node_id="serving-normalize",
            identity_node_id="serving-publish",
        )
        comparison_plan = paired_plan(
            training,
            serving,
            training_inputs=training.inputs,
            serving_inputs=serving.inputs,
            training_outputs=training.outputs,
            serving_outputs=serving.outputs,
        )

        training_lineage = extract(
            comparison_plan,
            training,
            side=LineageSide.TRAINING,
        )
        serving_lineage = extract(
            comparison_plan,
            serving,
            side=LineageSide.SERVING,
        )

        self.assertEqual(
            training_lineage.semantic_digest,
            serving_lineage.semantic_digest,
        )
        self.assertEqual(
            training_lineage.sites[0].site_digest,
            serving_lineage.sites[0].site_digest,
        )
        self.assertNotEqual(training_lineage.digest, serving_lineage.digest)
        self.assertNotEqual(
            training_lineage.sites[0].value_id,
            serving_lineage.sites[0].value_id,
        )
        self.assertNotEqual(
            training_lineage.sites[0].node_id,
            serving_lineage.sites[0].node_id,
        )

    def test_public_value_renames_preserve_plan_scoped_semantics(self) -> None:
        training = ratio_pipeline("public-training")
        serving = graph(
            "public-serving",
            values=(
                value("feature-a", "meter"),
                value("feature-b", "meter"),
                value("internal-ratio", "one"),
                value("prediction", "one"),
            ),
            inputs=("feature-a", "feature-b"),
            nodes=(
                Node(
                    "serving-normalize",
                    Operation.DIVIDE,
                    ("feature-a", "feature-b"),
                    "internal-ratio",
                ),
                Node(
                    "serving-publish",
                    Operation.IDENTITY,
                    ("internal-ratio",),
                    "prediction",
                ),
            ),
            outputs=("prediction",),
        )
        comparison_plan = paired_plan(
            training,
            serving,
            training_inputs=("left", "right"),
            serving_inputs=("feature-a", "feature-b"),
            training_outputs=("normalized",),
            serving_outputs=("prediction",),
        )

        training_lineage = extract(
            comparison_plan,
            training,
            side=LineageSide.TRAINING,
        )
        serving_lineage = extract(
            comparison_plan,
            serving,
            side=LineageSide.SERVING,
        )

        self.assertEqual(
            training_lineage.semantic_digest,
            serving_lineage.semantic_digest,
        )
        self.assertEqual(
            training_lineage.sites[0].site_digest,
            serving_lineage.sites[0].site_digest,
        )
        self.assertNotEqual(training_lineage.digest, serving_lineage.digest)

    def test_logical_root_mapping_changes_the_site_digest(self) -> None:
        candidate = ratio_pipeline("root-sensitive")
        first_plan = ratio_plan(candidate)
        second_plan = side_plan(
            candidate,
            inputs=(
                ("reference-baseline", "left"),
                ("root-right", "right"),
            ),
            outputs=(("result-normalized", "normalized"),),
            comparison_id="alternate-roots",
        )

        first = extract(first_plan, candidate)
        second = extract(second_plan, candidate)

        self.assertNotEqual(first.sites[0].site_digest, second.sites[0].site_digest)
        self.assertNotEqual(first.semantic_digest, second.semantic_digest)

    def test_commutative_swap_is_stable_but_ordered_swaps_change(self) -> None:
        for operation, stable in (
            (Operation.ADD, True),
            (Operation.MULTIPLY, True),
            (Operation.MINIMUM, True),
            (Operation.MAXIMUM, True),
            (Operation.SUBTRACT, False),
            (Operation.DIVIDE, False),
            (Operation.MATMUL, False),
        ):
            forward = binary_graph(operation)
            reverse = binary_graph(operation, swapped=True)
            forward_lineage = extract(binary_plan(forward), forward)
            reverse_lineage = extract(binary_plan(reverse), reverse)

            with self.subTest(operation=operation):
                left = forward_lineage.outputs[0].expression_digest
                right = reverse_lineage.outputs[0].expression_digest
                self.assertEqual(left == right, stable)
                if operation is Operation.DIVIDE:
                    self.assertEqual(
                        forward_lineage.sites[0].site_digest
                        == reverse_lineage.sites[0].site_digest,
                        stable,
                    )

    def test_duplicate_ordered_children_preserve_occurrence_multiplicity(self) -> None:
        candidate = graph(
            "duplicate-divide-child",
            values=(value("ratio", "one"), value("source", "meter")),
            inputs=("source",),
            nodes=(
                Node(
                    "self-normalize",
                    Operation.DIVIDE,
                    ("source", "source"),
                    "ratio",
                ),
            ),
            outputs=("ratio",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-source", "source"),),
            outputs=(("result-ratio", "ratio"),),
        )

        lineage = extract(comparison_plan, candidate)
        expression = lineage.expressions[-1]

        self.assertEqual(len(expression.child_digests), 2)
        self.assertEqual(expression.child_digests[0], expression.child_digests[1])
        self.assertEqual(expression.logical_roots, ("root-source",))
        self.assertEqual(len(lineage.sites), 1)

    def test_identity_collapses_only_when_all_metadata_is_preserved(self) -> None:
        safe = graph(
            "safe-identity",
            values=(value("input", "one"), value("output", "one")),
            inputs=("input",),
            nodes=(
                Node(
                    "preserve",
                    Operation.IDENTITY,
                    ("input",),
                    "output",
                ),
            ),
            outputs=("output",),
        )
        changed = graph(
            "changed-identity",
            values=(
                value("input", "delta-kelvin"),
                value("output", "delta-celsius"),
            ),
            inputs=("input",),
            nodes=(
                Node(
                    "change-explicit-id",
                    Operation.IDENTITY,
                    ("input",),
                    "output",
                ),
            ),
            outputs=("output",),
        )
        safe_plan = side_plan(
            safe,
            inputs=(("root-input", "input"),),
            outputs=(("result-output", "output"),),
        )
        changed_plan = side_plan(
            changed,
            inputs=(("root-input", "input"),),
            outputs=(("result-output", "output"),),
        )

        safe_lineage = extract(safe_plan, safe)
        changed_lineage = extract(changed_plan, changed)

        self.assertTrue(safe_lineage.expressions[-1].collapsed_identity)
        self.assertEqual(
            safe_lineage.expressions[0].semantic_digest,
            safe_lineage.expressions[-1].semantic_digest,
        )
        self.assertFalse(changed_lineage.expressions[-1].collapsed_identity)
        self.assertNotEqual(
            changed_lineage.expressions[0].semantic_digest,
            changed_lineage.expressions[-1].semantic_digest,
        )
        uncollapsed_safe = copy_expression(
            safe_lineage.expressions[-1],
            collapsed_identity=False,
        )
        collapsed_changed = copy_expression(
            changed_lineage.expressions[-1],
            collapsed_identity=True,
        )
        for source, replacement in (
            (safe_lineage, uncollapsed_safe),
            (changed_lineage, collapsed_changed),
        ):
            with self.assertRaisesRegex(LineageError, "collapse flag"):
                copy_lineage(
                    source,
                    expressions=(*source.expressions[:-1], replacement),
                )

    def test_only_dimensionless_linear_divides_are_sites(self) -> None:
        candidate = graph(
            "mixed-divides",
            values=(
                value("distance", "meter"),
                value("duration", "second"),
                value("ratio", "one"),
                value("reference", "meter"),
                value("speed", "meter-per-second"),
            ),
            inputs=("distance", "duration", "reference"),
            nodes=(
                Node(
                    "make-ratio",
                    Operation.DIVIDE,
                    ("distance", "reference"),
                    "ratio",
                ),
                Node(
                    "make-speed",
                    Operation.DIVIDE,
                    ("distance", "duration"),
                    "speed",
                ),
            ),
            outputs=("ratio", "speed"),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(
                ("root-distance", "distance"),
                ("root-duration", "duration"),
                ("root-reference", "reference"),
            ),
            outputs=(
                ("result-ratio", "ratio"),
                ("result-speed", "speed"),
            ),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(len(lineage.sites), 1)
        self.assertEqual(lineage.sites[0].node_id, "make-ratio")
        ratio_expression = next(
            item for item in lineage.expressions if item.value_id == "ratio"
        )
        self.assertEqual(ratio_expression.inferred.kind, QuantityKind.LINEAR)
        self.assertEqual(
            lineage.output_site_digest_multiset("result-speed"),
            (),
        )

    def test_dimensionless_site_does_not_require_unit_scale_one(self) -> None:
        candidate = graph(
            "scaled-dimensionless-ratio",
            values=(
                value("denominator", "kilometer"),
                value("numerator", "meter"),
                value("ratio", None),
            ),
            inputs=("numerator", "denominator"),
            nodes=(
                Node(
                    "scaled-normalization",
                    Operation.DIVIDE,
                    ("numerator", "denominator"),
                    "ratio",
                ),
            ),
            outputs=("ratio",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(
                ("root-denominator", "denominator"),
                ("root-numerator", "numerator"),
            ),
            outputs=(("result-ratio", "ratio"),),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(len(lineage.sites), 1)
        expression = next(
            item for item in lineage.expressions if item.value_id == "ratio"
        )
        self.assertEqual(expression.inferred.scale, Fraction(1, 1_000))
        self.assertEqual(expression.inferred.offset, Fraction(0))

    def test_identical_sites_remain_a_counted_multiset(self) -> None:
        candidate = graph(
            "duplicate-sites",
            values=(
                value("combined", "one"),
                value("left", "meter"),
                value("ratio-a", "one"),
                value("ratio-b", "one"),
                value("right", "meter"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "normalize-a",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio-a",
                ),
                Node(
                    "normalize-b",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio-b",
                ),
                Node(
                    "combine-ratios",
                    Operation.ADD,
                    ("ratio-a", "ratio-b"),
                    "combined",
                ),
            ),
            outputs=("combined",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(("result-combined", "combined"),),
        )

        lineage = extract(comparison_plan, candidate)
        multiset = lineage.output_site_digest_multiset("result-combined")

        self.assertEqual(len(lineage.sites), 2)
        self.assertEqual(len(multiset), 2)
        self.assertEqual(multiset[0], multiset[1])
        output_record = lineage.outputs[0].canonical_record()
        self.assertEqual(
            output_record["site_sha256_multiset"],
            [{"count": 2, "sha256": multiset[0]}],
        )

    def test_one_site_routes_to_every_reachable_logical_output(self) -> None:
        candidate = graph(
            "routed-site",
            values=(
                value("left", "meter"),
                value("output-a", "one"),
                value("output-b", "one"),
                value("ratio", "one"),
                value("right", "meter"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "normalize",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio",
                ),
                Node(
                    "publish-a",
                    Operation.IDENTITY,
                    ("ratio",),
                    "output-a",
                ),
                Node(
                    "publish-b",
                    Operation.IDENTITY,
                    ("ratio",),
                    "output-b",
                ),
            ),
            outputs=("output-a", "output-b"),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(("result-a", "output-a"), ("result-b", "output-b")),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(
            lineage.sites[0].logical_outputs,
            ("result-a", "result-b"),
        )
        self.assertEqual(
            lineage.output_site_digest_multiset("result-a"),
            lineage.output_site_digest_multiset("result-b"),
        )

    def test_one_site_routes_to_maximum_public_output_fanout(self) -> None:
        output_ids = tuple(
            f"published-{index:02d}" for index in range(MAX_GRAPH_OUTPUTS)
        )
        candidate = graph(
            "maximum-output-fanout",
            values=(
                value("left", "meter"),
                *(value(output_id, "one") for output_id in output_ids),
                value("ratio", "one"),
                value("right", "meter"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "normalize",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio",
                ),
                *(
                    Node(
                        f"publish-{index:02d}",
                        Operation.IDENTITY,
                        ("ratio",),
                        output_id,
                    )
                    for index, output_id in enumerate(output_ids)
                ),
            ),
            outputs=output_ids,
        )
        output_bindings = tuple(
            (f"result-{index:02d}", output_id)
            for index, output_id in enumerate(output_ids)
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=output_bindings,
        )
        limits = SolverLimits()
        verified = explicit_verified_result(candidate, limits=limits)

        lineage = extract(
            comparison_plan,
            candidate,
            result=verified,
            limits=limits,
        )
        site = lineage.sites[0]

        self.assertEqual(len(lineage.outputs), MAX_GRAPH_OUTPUTS)
        self.assertEqual(
            site.logical_outputs, tuple(item[0] for item in output_bindings)
        )
        self.assertTrue(
            all(
                output.site_digests == (site.site_digest,) for output in lineage.outputs
            )
        )

    def test_shared_diamond_routes_one_site_once_per_output(self) -> None:
        candidate = graph(
            "shared-diamond",
            values=(
                value("branch-a", "one"),
                value("branch-b", "one"),
                value("left", "meter"),
                value("merged", "one"),
                value("ratio", "one"),
                value("right", "meter"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "normalize",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio",
                ),
                Node(
                    "branch-left",
                    Operation.IDENTITY,
                    ("ratio",),
                    "branch-a",
                ),
                Node(
                    "branch-right",
                    Operation.IDENTITY,
                    ("ratio",),
                    "branch-b",
                ),
                Node(
                    "merge",
                    Operation.ADD,
                    ("branch-a", "branch-b"),
                    "merged",
                ),
            ),
            outputs=("merged",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(("result-merged", "merged"),),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(len(lineage.sites), 1)
        self.assertEqual(lineage.sites[0].logical_outputs, ("result-merged",))
        self.assertEqual(
            lineage.output_site_digest_multiset("result-merged"),
            (lineage.sites[0].site_digest,),
        )

    def test_same_value_input_and_output_keeps_distinct_role_contracts(self) -> None:
        candidate = graph(
            "shared-occurrence",
            values=(value("shared", "one"),),
            inputs=("shared",),
            nodes=(),
            outputs=("shared",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-shared", "shared"),),
            outputs=(("result-shared", "shared"),),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(lineage.expressions[0].logical_roots, ("root-shared",))
        self.assertEqual(lineage.outputs[0].contract_id, "result-shared")
        self.assertEqual(lineage.outputs[0].value_id, "shared")
        self.assertEqual(lineage.sites, ())


class VerificationBoundaryTests(unittest.TestCase):
    def test_nonverified_partial_and_wrong_identity_results_are_rejected(
        self,
    ) -> None:
        candidate = ratio_pipeline("verified-boundary")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)
        ambiguous = graph(
            "ambiguous-boundary",
            values=(value("input", None),),
            inputs=("input",),
            nodes=(),
            outputs=("input",),
        )
        ambiguous_plan = side_plan(
            ambiguous,
            inputs=(("root-input", "input"),),
            outputs=(("result-output", "input"),),
        )
        nonverified = verify_graph(ambiguous)
        partial = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=verified.graph_digest,
            registry_digest=verified.registry_digest,
            solver_version=verified.solver_version,
            limits=verified.limits,
            checks_performed=verified.checks_performed,
            contracts=verified.contracts[:1],
        )
        wrong_graph = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest="c" * 64,
            registry_digest=verified.registry_digest,
            solver_version=verified.solver_version,
            limits=verified.limits,
            checks_performed=verified.checks_performed,
            contracts=verified.contracts,
        )
        wrong_registry = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=verified.graph_digest,
            registry_digest="d" * 64,
            solver_version=verified.solver_version,
            limits=verified.limits,
            checks_performed=verified.checks_performed,
            contracts=verified.contracts,
        )
        wrong_solver = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=verified.graph_digest,
            registry_digest=verified.registry_digest,
            solver_version="4.16.1",
            limits=verified.limits,
            checks_performed=verified.checks_performed,
            contracts=verified.contracts,
        )
        alternate_limits = SolverLimits(per_check_timeout_ms=251)
        wrong_limits = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=verified.graph_digest,
            registry_digest=verified.registry_digest,
            solver_version=verified.solver_version,
            limits=alternate_limits,
            checks_performed=verified.checks_performed,
            contracts=verified.contracts,
        )
        cases = (
            (
                ambiguous_plan,
                ambiguous,
                nonverified,
                SolverLimits(),
                "identity",
            ),
            (
                comparison_plan,
                candidate,
                partial,
                SolverLimits(),
                "cover every",
            ),
            (
                comparison_plan,
                candidate,
                wrong_graph,
                SolverLimits(),
                "identity",
            ),
            (
                comparison_plan,
                candidate,
                wrong_registry,
                SolverLimits(),
                "identity",
            ),
            (
                comparison_plan,
                candidate,
                wrong_solver,
                SolverLimits(),
                "identity",
            ),
            (
                comparison_plan,
                candidate,
                wrong_limits,
                SolverLimits(),
                "identity",
            ),
        )
        for plan_value, graph_value, result, limits, message in cases:
            with (
                self.subTest(message=message, result=result.digest),
                self.assertRaisesRegex(LineageError, message),
            ):
                extract(
                    plan_value,
                    graph_value,
                    result=result,
                    limits=limits,
                )

    def test_mutated_and_semantically_false_contracts_are_rejected(self) -> None:
        candidate = ratio_pipeline("replay-boundary")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)
        forged_contracts = list(verified.contracts)
        first = forged_contracts[0]
        forged_contracts[0] = InferredContract(
            first.value_id,
            first.dimension,
            first.kind,
            first.scale * 2,
            first.offset,
        )
        forged = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=verified.graph_digest,
            registry_digest=verified.registry_digest,
            solver_version=verified.solver_version,
            limits=verified.limits,
            checks_performed=verified.checks_performed,
            contracts=tuple(forged_contracts),
        )

        with self.assertRaisesRegex(LineageError, "semantic replay"):
            extract(comparison_plan, candidate, result=forged)

        mutated = verify_graph(candidate)
        object.__setattr__(mutated.contracts[0], "scale", Fraction(2))
        with self.assertRaisesRegex(LineageError, "malformed or mutated"):
            extract(comparison_plan, candidate, result=mutated)

    def test_replay_requires_exact_true_and_redacts_exceptions(self) -> None:
        candidate = ratio_pipeline("strict-replay")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)

        with (
            patch.object(
                lineage_module,
                "_replay_claimed_contracts",
                return_value=1,
            ),
            self.assertRaisesRegex(LineageError, "failed semantic replay"),
        ):
            extract(comparison_plan, candidate, result=verified)

        with (
            patch.object(
                lineage_module,
                "_replay_claimed_contracts",
                side_effect=RuntimeError("sensitive detail"),
            ),
            self.assertRaisesRegex(LineageError, "could not be replayed") as caught,
        ):
            extract(comparison_plan, candidate, result=verified)
        self.assertNotIn("sensitive", str(caught.exception))

    def test_plan_pin_precedes_mutated_binding_interpretation(self) -> None:
        candidate = ratio_pipeline("pinned-lineage")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)
        selected = comparison_plan.bindings[0].training
        assert selected is not None
        object.__setattr__(selected, "value_id", "INVALID")

        with self.assertRaisesRegex(LineageError, "caller-trusted digest pin"):
            extract_normalization_lineage(
                comparison_plan,
                side=LineageSide.TRAINING,
                graph=candidate,
                verification_result=verified,
                policy=ComparisonPolicy("e" * 64),
            )

    def test_wrong_side_digest_and_incomplete_or_unknown_endpoints_fail(self) -> None:
        candidate = ratio_pipeline("coverage-boundary")
        verified = verify_graph(candidate)
        comparison_plan = ratio_plan(candidate)

        with self.assertRaisesRegex(LineageError, "selected plan side"):
            extract_normalization_lineage(
                comparison_plan,
                side=LineageSide.SERVING,
                graph=candidate,
                verification_result=verified,
            )

        incomplete = ComparisonPlan(
            "incomplete-lineage",
            candidate.digest,
            "b" * 64,
            BUILTIN_REGISTRY.digest,
            comparison_plan.bindings[:-1],
        )
        with self.assertRaisesRegex(LineageError, "cover every public"):
            extract(
                incomplete,
                candidate,
                policy=ComparisonPolicy(),
            )

        unknown_bindings = list(comparison_plan.bindings)
        unknown = unknown_bindings[0]
        unknown_bindings[0] = ContractBinding(
            unknown.contract_id,
            endpoint(InterfaceRole.INPUT, "missing"),
            None,
        )
        unknown_plan = ComparisonPlan(
            "unknown-lineage",
            candidate.digest,
            "b" * 64,
            BUILTIN_REGISTRY.digest,
            tuple(unknown_bindings),
        )
        with self.assertRaisesRegex(LineageError, "not a public"):
            extract(unknown_plan, candidate)

    def test_exact_public_input_types_fail_closed(self) -> None:
        candidate = ratio_pipeline("exact-types")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)

        cases = (
            (
                {"plan": object()},
                "exact ComparisonPlan",
            ),
            (
                {"side": "training"},
                "exact LineageSide",
            ),
            (
                {"graph": object()},
                "exact ComputationGraph",
            ),
            (
                {"registry": object()},
                "exact UnitRegistry",
            ),
            (
                {"verification_result": object()},
                "exact VerificationResult",
            ),
            (
                {"limits": object()},
                "exact SolverLimits",
            ),
            (
                {"policy": object()},
                "exact ComparisonPolicy",
            ),
        )
        base: dict[str, object] = {
            "plan": comparison_plan,
            "side": LineageSide.TRAINING,
            "graph": candidate,
            "registry": BUILTIN_REGISTRY,
            "verification_result": verified,
            "limits": SolverLimits(),
            "policy": ComparisonPolicy(),
        }
        for changes, message in cases:
            arguments = dict(base)
            arguments.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(LineageError, message),
            ):
                extract_normalization_lineage(**arguments)  # type: ignore[arg-type]

    def test_input_mutation_during_replay_is_detected(self) -> None:
        candidate = ratio_pipeline("mutation-boundary")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)

        def mutate_graph(
            graph_value: ComputationGraph,
            registry: object,
            contracts: object,
        ) -> bool:
            object.__setattr__(graph_value, "graph_id", "INVALID")
            return True

        with (
            patch.object(
                lineage_module,
                "_replay_claimed_contracts",
                side_effect=mutate_graph,
            ),
            self.assertRaisesRegex(LineageError, "changed during extraction"),
        ):
            extract(comparison_plan, candidate, result=verified)

    def test_policy_registry_pin_and_mutation_failures_are_redacted(self) -> None:
        candidate = ratio_pipeline("policy-boundary")
        comparison_plan = ratio_plan(candidate)
        verified = verify_graph(candidate)

        malformed_policy = ComparisonPolicy()
        object.__setattr__(malformed_policy, "expected_plan_digest", "bad")
        with self.assertRaisesRegex(LineageError, "policy is malformed"):
            extract_normalization_lineage(
                comparison_plan,
                side=LineageSide.TRAINING,
                graph=candidate,
                verification_result=verified,
                policy=malformed_policy,
            )

        with (
            patch.object(
                lineage_module.z3,
                "get_version_string",
                return_value="development",
            ),
            self.assertRaisesRegex(LineageError, "could not be pinned"),
        ):
            extract(
                comparison_plan,
                candidate,
                result=verified,
            )

        wrong_registry_plan = ComparisonPlan(
            "wrong-registry-lineage",
            candidate.digest,
            "b" * 64,
            "c" * 64,
            comparison_plan.bindings,
        )
        with self.assertRaisesRegex(LineageError, "registry does not match"):
            extract(
                wrong_registry_plan,
                candidate,
                result=verified,
                policy=ComparisonPolicy(),
            )

        policy = ComparisonPolicy(comparison_plan.digest)

        def mutate_policy(
            graph_value: ComputationGraph,
            registry: object,
            contracts: object,
        ) -> bool:
            object.__setattr__(policy, "expected_plan_digest", "e" * 64)
            return True

        with (
            patch.object(
                lineage_module,
                "_replay_claimed_contracts",
                side_effect=mutate_policy,
            ),
            self.assertRaisesRegex(LineageError, "changed during extraction"),
        ):
            extract(
                comparison_plan,
                candidate,
                result=verified,
                policy=policy,
            )

    def test_unselected_side_bindings_are_ignored_but_still_bounded(self) -> None:
        candidate = ratio_pipeline("unselected-binding")
        base = ratio_plan(candidate)
        bindings = (
            *base.bindings,
            ContractBinding(
                "serving-only-note",
                None,
                endpoint(InterfaceRole.INPUT, "unrelated-serving-input"),
            ),
        )
        comparison_plan = ComparisonPlan(
            "unselected-side-plan",
            candidate.digest,
            "b" * 64,
            BUILTIN_REGISTRY.digest,
            tuple(sorted(bindings, key=lambda item: item.contract_id)),
        )

        lineage = extract(comparison_plan, candidate)

        self.assertEqual(len(lineage.sites), 1)


class ImmutableRecordTests(unittest.TestCase):
    def test_records_are_frozen_content_addressed_and_canonical(self) -> None:
        candidate = ratio_pipeline("immutable-lineage")
        lineage = extract(ratio_plan(candidate), candidate)

        self.assertEqual(
            lineage.canonical_record()["schema"],
            NORMALIZATION_LINEAGE_SCHEMA,
        )
        self.assertEqual(
            lineage.canonical_record()["authentication"],
            LINEAGE_AUTHENTICATION,
        )
        self.assertEqual(len(lineage.semantic_digest), 64)
        self.assertEqual(len(lineage.digest), 64)
        self.assertFalse(lineage.canonical_bytes().endswith(b"\n"))
        with self.assertRaises(FrozenInstanceError):
            lineage.side = LineageSide.SERVING  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            lineage.sites[0].node_id = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            lineage.expressions[0].value_id = "changed"  # type: ignore[misc]

    def test_nested_mutation_is_detected_at_every_digest_boundary(self) -> None:
        candidate = ratio_pipeline("nested-mutation")
        lineage = extract(ratio_plan(candidate), candidate)
        expression = lineage.expressions[0]
        object.__setattr__(expression.value, "unit_id", "kilometer")
        with self.assertRaisesRegex(LineageError, "does not match|mutated"):
            expression.validate()
        with self.assertRaisesRegex(LineageError, "does not match|mutated"):
            lineage.validate()

        lineage = extract(
            ratio_plan(candidate := ratio_pipeline("site-mutation")), candidate
        )
        object.__setattr__(lineage.sites[0], "node_id", "renamed")
        with self.assertRaisesRegex(LineageError, "does not match"):
            lineage.sites[0].validate()

        lineage = extract(
            ratio_plan(candidate := ratio_pipeline("output-mutation")),
            candidate,
        )
        object.__setattr__(lineage.outputs[0], "position", 1)
        with self.assertRaisesRegex(LineageError, "does not match|complete"):
            lineage.validate()

    def test_lookup_and_nested_shapes_validate_exactly(self) -> None:
        candidate = ratio_pipeline("lookup-lineage")
        lineage = extract(ratio_plan(candidate), candidate)

        with self.assertRaisesRegex(LineageError, "not present"):
            lineage.output_site_digest_multiset("missing-output")
        with self.assertRaisesRegex(LineageError, "not canonical"):
            lineage.output_site_digest_multiset("INVALID")

        expression = lineage.expressions[-1]
        assert expression.node_id is not None
        with self.assertRaisesRegex(LineageError, "exact boolean"):
            LineageExpression(
                expression.value_id,
                expression.node_id,
                expression.operation,
                expression.attributes,
                expression.input_value_ids,
                expression.child_digests,
                expression.logical_roots,
                1,  # type: ignore[arg-type]
                expression.value,
                expression.inferred,
            )
        with self.assertRaisesRegex(LineageError, "nonempty and sorted"):
            NormalizationSite(
                "node",
                "value",
                "a" * 64,
                (),
                ("output",),
            )
        with self.assertRaisesRegex(LineageError, "exact integer"):
            OutputLineage(
                "result",
                "value",
                True,  # type: ignore[arg-type]
                "a" * 64,
                (),
            )

    def test_closed_node_attributes_are_canonical(self) -> None:
        powered = graph(
            "power-attributes",
            values=(value("input", "meter"), value("output", None)),
            inputs=("input",),
            nodes=(
                Node(
                    "raise-power",
                    Operation.POWER,
                    ("input",),
                    "output",
                    exponent=Fraction(2, 3),
                ),
            ),
            outputs=("output",),
        )
        squared = graph(
            "integer-power-attributes",
            values=(value("input", "meter"), value("output", None)),
            inputs=("input",),
            nodes=(
                Node(
                    "square-value",
                    Operation.POWER,
                    ("input",),
                    "output",
                    exponent=Fraction(2),
                ),
            ),
            outputs=("output",),
        )
        converted = graph(
            "convert-attributes",
            values=(
                value("input", "kilometer"),
                value("output", "meter"),
            ),
            inputs=("input",),
            nodes=(
                Node(
                    "convert-distance",
                    Operation.CONVERT,
                    ("input",),
                    "output",
                    target_unit_id="meter",
                ),
            ),
            outputs=("output",),
        )
        for candidate, expected in (
            (powered, (("exponent", "2/3"),)),
            (squared, (("exponent", "2"),)),
            (converted, (("unit_id", "meter"),)),
        ):
            comparison_plan = side_plan(
                candidate,
                inputs=(("root-input", "input"),),
                outputs=(("result-output", "output"),),
            )
            with self.subTest(candidate=candidate.graph_id):
                lineage = extract(comparison_plan, candidate)
                self.assertEqual(lineage.expressions[-1].attributes, expected)

    def test_expression_validator_rejects_every_malformed_shape(self) -> None:
        candidate = ratio_pipeline("expression-validation")
        lineage = extract(ratio_plan(candidate), candidate)
        input_expression = lineage.expressions[0]
        divide_expression = lineage.expressions[2]
        identity_expression = lineage.expressions[3]

        class DerivedExpression(LineageExpression):
            pass

        derived = object.__new__(DerivedExpression)
        with self.assertRaisesRegex(LineageError, "exact LineageExpression"):
            derived.validate()

        cases = (
            (
                input_expression,
                {"value_id": "INVALID"},
                "not canonical",
            ),
            (
                input_expression,
                {"value": object()},
                "exact ValueSpec",
            ),
            (
                input_expression,
                {"inferred": object()},
                "exact InferredContract",
            ),
            (
                input_expression,
                {"value": value("different", "meter")},
                "identities",
            ),
            (
                input_expression,
                {"logical_roots": ["root-left"]},
                "must be a tuple",
            ),
            (
                input_expression,
                {"logical_roots": ()},
                "nonempty and sorted",
            ),
            (
                input_expression,
                {"logical_roots": ("z-root", "a-root")},
                "nonempty and sorted",
            ),
            (
                input_expression,
                {"input_value_ids": []},
                "input identifiers must be a tuple",
            ),
            (
                input_expression,
                {"child_digests": []},
                "child digests must be a tuple",
            ),
            (
                divide_expression,
                {"child_digests": divide_expression.child_digests[:1]},
                "children and diagnostics",
            ),
            (
                input_expression,
                {"node_id": "unexpected-node"},
                "input expression fields",
            ),
            (
                divide_expression,
                {"operation": "divide"},
                "operation is unsupported",
            ),
            (
                divide_expression,
                {
                    "input_value_ids": ("left",),
                    "child_digests": divide_expression.child_digests[:1],
                },
                "arity",
            ),
            (
                divide_expression,
                {"attributes": []},
                "attributes must be a tuple",
            ),
            (
                divide_expression,
                {"attributes": (("bad",),)},
                "attributes are malformed",
            ),
            (
                divide_expression,
                {"attributes": (("z", "1"), ("a", "1"))},
                "sorted and unique",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("wrong", "2"),),
                },
                "power lineage attributes",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "01"),),
                },
                "not canonical",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "2/4"),),
                },
                "not canonical",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "1/1"),),
                },
                "not canonical",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "-0"),),
                },
                "not canonical",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "0/2"),),
                },
                "not canonical",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "1" * 1_000),),
                },
                "too long",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.POWER,
                    "attributes": (("exponent", "65"),),
                },
                "out of bounds",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.CONVERT,
                    "attributes": (("wrong", "meter"),),
                },
                "conversion lineage attributes",
            ),
            (
                identity_expression,
                {
                    "operation": Operation.CONVERT,
                    "attributes": (("unit_id", "INVALID"),),
                },
                "not canonical",
            ),
            (
                divide_expression,
                {"attributes": (("unexpected", "value"),)},
                "does not accept",
            ),
            (
                divide_expression,
                {"collapsed_identity": True},
                "only identity",
            ),
            (
                divide_expression,
                {
                    "logical_roots": tuple(
                        f"root-{index:02d}" for index in range(MAX_GRAPH_INPUTS + 1)
                    )
                },
                "exceed the graph input limit",
            ),
        )
        for source, changes, message in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_expression(source, **changes)

        commutative = extract(
            binary_plan(candidate := binary_graph(Operation.ADD)),
            candidate,
        ).expressions[-1]
        self.assertNotEqual(
            commutative.child_digests[0],
            commutative.child_digests[1],
        )
        with self.assertRaisesRegex(LineageError, "must be sorted"):
            copy_expression(
                commutative,
                child_digests=tuple(reversed(commutative.child_digests)),
            )

        damaged = copy_expression(input_expression)
        object.__setattr__(damaged, "_semantic_digest", "f" * 64)
        with self.assertRaisesRegex(LineageError, "semantic digest"):
            damaged.validate()
        damaged = copy_expression(input_expression)
        object.__setattr__(damaged, "_digest", "bad")
        with self.assertRaisesRegex(LineageError, "digest is malformed"):
            damaged.validate()
        damaged = copy_expression(input_expression)
        object.__setattr__(damaged, "logical_roots", ("changed-root",))
        with self.assertRaisesRegex(LineageError, "does not match"):
            damaged.validate()
        self.assertTrue(copy_expression(input_expression).canonical_bytes())

    def test_site_and_output_validators_reject_malformed_records(self) -> None:
        candidate = ratio_pipeline("site-output-validation")
        lineage = extract(ratio_plan(candidate), candidate)
        site = lineage.sites[0]
        output = lineage.outputs[0]

        class DerivedSite(NormalizationSite):
            pass

        class DerivedOutput(OutputLineage):
            pass

        derived_site = object.__new__(DerivedSite)
        derived_output = object.__new__(DerivedOutput)
        with self.assertRaisesRegex(LineageError, "exact NormalizationSite"):
            derived_site.validate()
        with self.assertRaisesRegex(LineageError, "exact OutputLineage"):
            derived_output.validate()

        site_cases = (
            ({"expression_digest": "bad"}, "digest.*malformed"),
            ({"logical_roots": ["root-left"]}, "must be a tuple"),
            ({"logical_roots": ()}, "nonempty and sorted"),
            (
                {
                    "logical_roots": tuple(
                        f"root-{index:02d}" for index in range(MAX_GRAPH_INPUTS + 1)
                    )
                },
                "exceed the graph interface limit",
            ),
            (
                {"logical_outputs": ("z-output", "a-output")},
                "nonempty and sorted",
            ),
            (
                {
                    "logical_outputs": tuple(
                        f"result-{index:02d}" for index in range(MAX_GRAPH_OUTPUTS + 1)
                    )
                },
                "exceed the graph interface limit",
            ),
        )
        for changes, message in site_cases:
            with (
                self.subTest(site_changes=changes),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_site(site, **changes)

        output_cases = (
            ({"position": -1}, "out of bounds"),
            ({"position": 64}, "out of bounds"),
            ({"expression_digest": "bad"}, "digest.*malformed"),
            ({"site_digests": []}, "must be a tuple"),
            ({"site_digests": ("f" * 64, "a" * 64)}, "must be sorted"),
            (
                {"site_digests": ("f" * 64,) * (MAX_GRAPH_NODES + 1)},
                "exceed the graph node limit",
            ),
        )
        for changes, message in output_cases:
            with (
                self.subTest(output_changes=changes),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_output(output, **changes)

        damaged_site = copy_site(site)
        object.__setattr__(damaged_site, "_site_digest", "f" * 64)
        with self.assertRaisesRegex(LineageError, "semantic contents"):
            damaged_site.validate()
        damaged_site = copy_site(site)
        object.__setattr__(damaged_site, "_digest", "bad")
        with self.assertRaisesRegex(LineageError, "diagnostic digest is malformed"):
            damaged_site.validate()
        damaged_site = copy_site(site)
        object.__setattr__(damaged_site, "node_id", "changed-node")
        with self.assertRaisesRegex(LineageError, "diagnostic digest does not match"):
            damaged_site.validate()

        damaged_output = copy_output(output)
        object.__setattr__(damaged_output, "_digest", "bad")
        with self.assertRaisesRegex(LineageError, "digest is malformed"):
            damaged_output.validate()
        damaged_output = copy_output(output)
        object.__setattr__(damaged_output, "contract_id", "changed-output")
        with self.assertRaisesRegex(LineageError, "does not match"):
            damaged_output.validate()

    def test_lineage_validator_enforces_dag_and_collection_invariants(self) -> None:
        candidate = ratio_pipeline("lineage-invariants")
        lineage = extract(ratio_plan(candidate), candidate)
        left, right, ratio, published = lineage.expressions
        site = lineage.sites[0]
        output = lineage.outputs[0]

        class DerivedLineage(NormalizationLineage):
            pass

        derived = object.__new__(DerivedLineage)
        with self.assertRaisesRegex(LineageError, "exact NormalizationLineage"):
            derived.validate()

        simple_cases = (
            ({"side": "training"}, "side is unsupported"),
            ({"limits": object()}, "exact SolverLimits"),
            ({"verification_result": object()}, "exact VerificationResult"),
            ({"graph_digest": "f" * 64}, "bindings are inconsistent"),
            ({"expressions": []}, "expressions must be a tuple"),
            ({"expressions": ()}, "count is out of bounds"),
            ({"expressions": (object(),)}, "exact LineageExpression"),
            ({"sites": []}, "sites must be a bounded tuple"),
            ({"outputs": []}, "nonempty bounded tuple"),
            ({"outputs": ()}, "nonempty bounded tuple"),
        )
        for changes, message in simple_cases:
            with (
                self.subTest(simple_changes=changes),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_lineage(lineage, **changes)

        invalid_right_root = copy_expression(
            right,
            logical_roots=left.logical_roots,
        )
        duplicate_node = copy_expression(
            published,
            node_id=ratio.node_id,
        )
        missing_input = copy_expression(
            ratio,
            input_value_ids=("missing", "right"),
        )
        wrong_child = copy_expression(
            ratio,
            child_digests=("f" * 64, ratio.child_digests[1]),
        )
        wrong_roots = copy_expression(
            ratio,
            logical_roots=("root-left",),
        )
        changed_identity = copy_expression(
            published,
            value=value("normalized", "percent"),
        )
        expression_cases = (
            (
                (left, left, ratio, published),
                "value identifiers must be unique",
            ),
            (
                (left, invalid_right_root, ratio, published),
                "input roots must be unique",
            ),
            (
                (left, right, ratio, duplicate_node),
                "node identifiers must be unique",
            ),
            (
                (left, right, missing_input, published),
                "reference earlier",
            ),
            (
                (left, right, wrong_child, published),
                "child digests",
            ),
            (
                (left, right, wrong_roots, published),
                "logical roots",
            ),
            (
                (left, right, ratio, changed_identity),
                "collapse flag",
            ),
            (
                (left, right, ratio),
                "cover every verified value",
            ),
        )
        for expressions, message in expression_cases:
            with (
                self.subTest(expression_message=message),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_lineage(lineage, expressions=expressions)

        late_input_graph = graph(
            "late-input-invariant",
            values=(
                value("left", "one"),
                value("published", "one"),
                value("right", "one"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "publish-left",
                    Operation.IDENTITY,
                    ("left",),
                    "published",
                ),
            ),
            outputs=("published", "right"),
        )
        late_input_plan = side_plan(
            late_input_graph,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(
                ("result-published", "published"),
                ("result-right", "right"),
            ),
        )
        late = extract(late_input_plan, late_input_graph)
        with self.assertRaisesRegex(LineageError, "inputs must precede"):
            copy_lineage(
                late,
                expressions=(
                    late.expressions[0],
                    late.expressions[2],
                    late.expressions[1],
                ),
            )

        site_cases = (
            ((object(),), "exact NormalizationSite"),
            ((copy_site(site, node_id="other-node"),), "does not match"),
            ((site, site), "identities must be unique"),
            ((), "cover every qualifying"),
            (
                (
                    copy_site(
                        site,
                        logical_outputs=("different-output",),
                    ),
                ),
                "output routing",
            ),
        )
        for sites, message in site_cases:
            with (
                self.subTest(site_message=message),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_lineage(
                    lineage,
                    sites=sites,  # type: ignore[arg-type]
                )

        output_cases = (
            ((object(),), "exact OutputLineage"),
            (
                (
                    copy_output(
                        output,
                        expression_digest="f" * 64,
                    ),
                ),
                "does not match its expression",
            ),
            ((output, output), "occurrences must be unique"),
            ((copy_output(output, position=1),), "positions must be complete"),
            (
                (copy_output(output, site_digests=()),),
                "multiset is inconsistent",
            ),
        )
        for outputs, message in output_cases:
            with (
                self.subTest(output_message=message),
                self.assertRaisesRegex(LineageError, message),
            ):
                copy_lineage(
                    lineage,
                    outputs=outputs,  # type: ignore[arg-type]
                )

        routed_candidate = graph(
            "sorted-output-invariant",
            values=(
                value("left", "meter"),
                value("output-a", "one"),
                value("output-b", "one"),
                value("ratio", "one"),
                value("right", "meter"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "normalize",
                    Operation.DIVIDE,
                    ("left", "right"),
                    "ratio",
                ),
                Node(
                    "publish-a",
                    Operation.IDENTITY,
                    ("ratio",),
                    "output-a",
                ),
                Node(
                    "publish-b",
                    Operation.IDENTITY,
                    ("ratio",),
                    "output-b",
                ),
            ),
            outputs=("output-a", "output-b"),
        )
        routed_plan = side_plan(
            routed_candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(("result-a", "output-a"), ("result-b", "output-b")),
        )
        routed = extract(routed_plan, routed_candidate)
        with self.assertRaisesRegex(LineageError, "sorted and unique"):
            copy_lineage(routed, outputs=tuple(reversed(routed.outputs)))

        detached_candidate = graph(
            "detached-lineage-invariant",
            values=(
                value("left", "one"),
                value("left-output", "one"),
                value("right", "one"),
            ),
            inputs=("left", "right"),
            nodes=(
                Node(
                    "publish-left",
                    Operation.IDENTITY,
                    ("left",),
                    "left-output",
                ),
            ),
            outputs=("left-output", "right"),
        )
        detached_plan = side_plan(
            detached_candidate,
            inputs=(("root-left", "left"), ("root-right", "right")),
            outputs=(
                ("result-left", "left-output"),
                ("result-right", "right"),
            ),
        )
        detached = extract(detached_plan, detached_candidate)
        with self.assertRaisesRegex(LineageError, "must reach a logical output"):
            copy_lineage(detached, outputs=detached.outputs[:1])

        damaged = copy_lineage(lineage)
        object.__setattr__(damaged, "_semantic_digest", "f" * 64)
        with self.assertRaisesRegex(LineageError, "semantic digest"):
            damaged.validate()
        damaged = copy_lineage(lineage)
        object.__setattr__(damaged, "_digest", "bad")
        with self.assertRaisesRegex(LineageError, "digest is malformed"):
            damaged.validate()
        damaged = copy_lineage(lineage)
        object.__setattr__(damaged, "comparison_id", "changed-lineage")
        with self.assertRaisesRegex(LineageError, "does not match"):
            damaged.validate()

        mutated_result = verify_graph(candidate)
        object.__setattr__(mutated_result.contracts[0], "scale", Fraction(2))
        with self.assertRaisesRegex(LineageError, "metadata is malformed"):
            copy_lineage(lineage, verification_result=mutated_result)

    def test_direct_lineage_enforces_graph_input_and_operation_limits(self) -> None:
        candidate = ratio_pipeline("direct-lineage-limits")
        lineage = extract(ratio_plan(candidate), candidate)
        one = BUILTIN_REGISTRY.resolve("one")

        input_values = tuple(
            value(f"input-{index:03d}", "one") for index in range(MAX_GRAPH_INPUTS + 1)
        )
        input_contracts = tuple(
            InferredContract(
                item.value_id,
                one.dimension,
                one.kind,
                one.scale,
                one.offset,
            )
            for item in input_values
        )
        input_expressions = tuple(
            LineageExpression(
                value_id=item.value_id,
                node_id=None,
                operation=None,
                attributes=(),
                input_value_ids=(),
                child_digests=(),
                logical_roots=(f"root-{index:03d}",),
                collapsed_identity=False,
                value=item,
                inferred=input_contracts[index],
            )
            for index, item in enumerate(input_values)
        )
        oversized_input_result = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=lineage.graph_digest,
            registry_digest=lineage.registry_digest,
            solver_version=lineage.verification_result.solver_version,
            limits=lineage.limits,
            checks_performed=0,
            contracts=input_contracts,
        )
        with self.assertRaisesRegex(LineageError, "too many input expressions"):
            copy_lineage(
                lineage,
                verification_result=oversized_input_result,
                expressions=input_expressions,
            )

        operation_values = tuple(
            value(f"value-{index:03d}", "one") for index in range(MAX_GRAPH_NODES + 2)
        )
        operation_contracts = tuple(
            InferredContract(
                item.value_id,
                one.dimension,
                one.kind,
                one.scale,
                one.offset,
            )
            for item in operation_values
        )
        first = LineageExpression(
            value_id=operation_values[0].value_id,
            node_id=None,
            operation=None,
            attributes=(),
            input_value_ids=(),
            child_digests=(),
            logical_roots=("root-input",),
            collapsed_identity=False,
            value=operation_values[0],
            inferred=operation_contracts[0],
        )
        operation_expressions = [first]
        for index in range(1, MAX_GRAPH_NODES + 2):
            previous = operation_expressions[-1]
            operation_expressions.append(
                LineageExpression(
                    value_id=operation_values[index].value_id,
                    node_id=f"identity-{index:03d}",
                    operation=Operation.IDENTITY,
                    attributes=(),
                    input_value_ids=(previous.value_id,),
                    child_digests=(previous.semantic_digest,),
                    logical_roots=previous.logical_roots,
                    collapsed_identity=True,
                    value=operation_values[index],
                    inferred=operation_contracts[index],
                )
            )
        oversized_operation_result = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=lineage.graph_digest,
            registry_digest=lineage.registry_digest,
            solver_version=lineage.verification_result.solver_version,
            limits=lineage.limits,
            checks_performed=0,
            contracts=operation_contracts,
        )
        with self.assertRaisesRegex(LineageError, "too many operation expressions"):
            copy_lineage(
                lineage,
                verification_result=oversized_operation_result,
                expressions=tuple(operation_expressions),
            )

    def test_direct_lineage_cannot_replace_a_verified_contract(self) -> None:
        candidate = graph(
            "forged-site-guard",
            values=(
                value("distance", "meter"),
                value("duration", "second"),
                value("speed", None),
            ),
            inputs=("distance", "duration"),
            nodes=(
                Node(
                    "compute-speed",
                    Operation.DIVIDE,
                    ("distance", "duration"),
                    "speed",
                ),
            ),
            outputs=("speed",),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(
                ("root-distance", "distance"),
                ("root-duration", "duration"),
            ),
            outputs=(("result-speed", "speed"),),
        )
        lineage = extract(comparison_plan, candidate)
        self.assertEqual(lineage.sites, ())
        speed = lineage.expressions[-1]
        forged_contract = InferredContract(
            "speed",
            DIMENSIONLESS,
            QuantityKind.LINEAR,
            Fraction(1),
            Fraction(0),
        )
        forged_expression = copy_expression(
            speed,
            inferred=forged_contract,
        )
        assert forged_expression.node_id is not None
        forged_site = NormalizationSite(
            forged_expression.node_id,
            forged_expression.value_id,
            forged_expression.semantic_digest,
            forged_expression.logical_roots,
            ("result-speed",),
        )
        forged_output = copy_output(
            lineage.outputs[0],
            expression_digest=forged_expression.semantic_digest,
            site_digests=(forged_site.site_digest,),
        )

        with self.assertRaisesRegex(LineageError, "verified contracts"):
            copy_lineage(
                lineage,
                expressions=(*lineage.expressions[:-1], forged_expression),
                sites=(forged_site,),
                outputs=(forged_output,),
            )

    def test_maximum_node_graph_is_iterative_and_deterministic(self) -> None:
        values = [value("value-000", "one")]
        nodes: list[Node] = []
        previous = "value-000"
        for index in range(1, MAX_GRAPH_NODES + 1):
            output = f"value-{index:03d}"
            values.append(value(output, "one"))
            nodes.append(
                Node(
                    f"identity-{index:03d}",
                    Operation.IDENTITY,
                    (previous,),
                    output,
                )
            )
            previous = output
        candidate = graph(
            "maximum-lineage",
            values=tuple(values),
            inputs=("value-000",),
            nodes=tuple(nodes),
            outputs=(previous,),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(("root-input", "value-000"),),
            outputs=(("result-output", previous),),
        )
        limits = SolverLimits()
        contracts = tuple(
            InferredContract(
                item.value_id,
                DIMENSIONLESS,
                QuantityKind.LINEAR,
                Fraction(1),
                Fraction(0),
            )
            for item in candidate.values
        )
        verified = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=candidate.digest,
            registry_digest=BUILTIN_REGISTRY.digest,
            solver_version=z3.get_version_string(),
            limits=limits,
            checks_performed=0,
            contracts=contracts,
        )

        first = extract(
            comparison_plan,
            candidate,
            result=verified,
            limits=limits,
        )
        second = extract(
            comparison_plan,
            candidate,
            result=verified,
            limits=limits,
        )

        self.assertEqual(len(first.expressions), MAX_GRAPH_NODES + 1)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.semantic_digest, second.semantic_digest)
        self.assertTrue(all(item.collapsed_identity for item in first.expressions[1:]))

    def test_maximum_qualifying_sites_are_iterative_and_fully_routed(self) -> None:
        values = [value("reference", "one"), value("value-000", "one")]
        nodes: list[Node] = []
        previous = "value-000"
        for index in range(1, MAX_GRAPH_NODES + 1):
            output = f"value-{index:03d}"
            values.append(value(output, "one"))
            nodes.append(
                Node(
                    f"normalize-{index:03d}",
                    Operation.DIVIDE,
                    (previous, "reference"),
                    output,
                )
            )
            previous = output
        candidate = graph(
            "maximum-normalization-lineage",
            values=tuple(values),
            inputs=("value-000", "reference"),
            nodes=tuple(nodes),
            outputs=(previous,),
        )
        comparison_plan = side_plan(
            candidate,
            inputs=(
                ("root-reference", "reference"),
                ("root-value", "value-000"),
            ),
            outputs=(("result-output", previous),),
        )
        limits = SolverLimits()
        verified = explicit_verified_result(candidate, limits=limits)

        lineage = extract(
            comparison_plan,
            candidate,
            result=verified,
            limits=limits,
        )
        rebuilt = copy_lineage(lineage)

        self.assertEqual(len(lineage.sites), MAX_GRAPH_NODES)
        self.assertEqual(
            tuple(site.node_id for site in lineage.sites),
            tuple(f"normalize-{index:03d}" for index in range(1, MAX_GRAPH_NODES + 1)),
        )
        self.assertTrue(
            all(site.logical_outputs == ("result-output",) for site in lineage.sites)
        )
        self.assertEqual(
            len(lineage.output_site_digest_multiset("result-output")),
            MAX_GRAPH_NODES,
        )
        self.assertEqual(lineage.digest, rebuilt.digest)
        self.assertEqual(lineage.semantic_digest, rebuilt.semantic_digest)


if __name__ == "__main__":
    unittest.main()
