from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from unittest.mock import PropertyMock, patch

import unitsentinel.repair as repair_module
from unitsentinel.domain import DIMENSIONLESS, Unit, UnitSentinelError
from unitsentinel.graph import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY, UnitRegistry
from unitsentinel.repair import (
    MAX_REPAIR_CANDIDATES,
    MAX_REPAIR_SITES,
    MAX_REPAIR_TOTAL_TIMEOUT_MS,
    MAX_REPAIR_VERIFIER_CALLS,
    MAX_REPAIR_WORK_ITEMS,
    RepairError,
    RepairLimits,
    RepairReason,
    RepairStatus,
    UnitRepairCandidate,
    UnitRepairResult,
    propose_unit_annotation_repair,
)
from unitsentinel.verification import (
    SolverLimits,
    UnknownReason,
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


def exp_graph(input_unit: str | None) -> ComputationGraph:
    return graph(
        "exp-contract",
        values=(value("input", input_unit), value("output")),
        inputs=("input",),
        nodes=(
            Node(
                "apply-exp",
                Operation.EXP,
                ("input",),
                "output",
            ),
        ),
        outputs=("output",),
    )


def identity_graph(
    input_unit: str | None,
    output_unit: str | None = None,
) -> ComputationGraph:
    return graph(
        "identity-contract",
        values=(value("input", input_unit), value("output", output_unit)),
        inputs=("input",),
        nodes=(
            Node(
                "apply-identity",
                Operation.IDENTITY,
                ("input",),
                "output",
            ),
        ),
        outputs=("output",),
    )


def underconstrained_relaxation() -> ComputationGraph:
    return graph(
        "temperature-multiplication",
        values=(
            value("left", "kelvin"),
            value("output"),
            value("right"),
        ),
        inputs=("left", "right"),
        nodes=(
            Node(
                "multiply-values",
                Operation.MULTIPLY,
                ("left", "right"),
                "output",
            ),
        ),
        outputs=("output",),
    )


def conversion_mismatch() -> ComputationGraph:
    return graph(
        "conversion-mismatch",
        values=(
            value("input", "kilometer"),
            value("output", "centimeter"),
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


def independent_conflicts() -> ComputationGraph:
    return graph(
        "independent-conflicts",
        values=(
            value("distance", "meter"),
            value("duration", "second"),
            value("first"),
            value("mass", "kilogram"),
            value("second"),
            value("temperature", "kelvin"),
            value("total"),
        ),
        inputs=("distance", "duration", "mass", "temperature"),
        nodes=(
            Node(
                "add-distance-duration",
                Operation.ADD,
                ("distance", "duration"),
                "first",
            ),
            Node(
                "add-mass-temperature",
                Operation.ADD,
                ("mass", "temperature"),
                "second",
            ),
            Node(
                "combine-conflicts",
                Operation.MULTIPLY,
                ("first", "second"),
                "total",
            ),
        ),
        outputs=("total",),
    )


class VerifiedRepairTests(unittest.TestCase):
    def test_unique_conflict_to_verified_lineage_is_real_and_non_mutating(
        self,
    ) -> None:
        source = exp_graph("percent")
        before_bytes = source.canonical_bytes()
        before_digest = source.digest

        result = propose_unit_annotation_repair(source)

        self.assertEqual(result.status, RepairStatus.PROPOSED)
        self.assertIsNone(result.reason)
        self.assertEqual(result.verification_calls, 3)
        self.assertEqual(result.sites_considered, 1)
        self.assertEqual(result.candidates_considered, 1)
        self.assertEqual(result.source_verification.status, VerificationStatus.CONFLICT)
        self.assertIs(result.source_verification.core_minimal, True)

        proposal = result.candidate
        assert proposal is not None
        self.assertEqual(
            proposal.constraint_id,
            "declaration/input/unit",
        )
        self.assertEqual(proposal.previous_unit_id, "percent")
        self.assertEqual(proposal.replacement_unit_id, "one")
        self.assertIsNone(proposal.relaxed_graph.value("input").unit_id)
        self.assertEqual(proposal.repaired_graph.value("input").unit_id, "one")
        self.assertEqual(
            proposal.relaxed_verification.status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            proposal.repaired_verification.status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            proposal.repaired_verification.graph_digest,
            proposal.repaired_graph.digest,
        )
        self.assertNotEqual(source.digest, proposal.relaxed_graph.digest)
        self.assertNotEqual(
            proposal.relaxed_graph.digest,
            proposal.repaired_graph.digest,
        )

        self.assertEqual(source.canonical_bytes(), before_bytes)
        self.assertEqual(source.digest, before_digest)
        self.assertEqual(source.value("input").unit_id, "percent")
        self.assertIsNot(source, proposal.relaxed_graph)
        self.assertIsNot(source, proposal.repaired_graph)

    def test_repeated_search_has_deterministic_records_and_digests(self) -> None:
        source = exp_graph("percent")

        first = propose_unit_annotation_repair(source)
        second = propose_unit_annotation_repair(source)

        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.digest, second.digest)
        assert first.candidate is not None
        assert second.candidate is not None
        self.assertEqual(
            first.candidate.canonical_bytes(),
            second.candidate.canonical_bytes(),
        )
        self.assertEqual(first.candidate.digest, second.candidate.digest)

    def test_two_verified_single_edit_candidates_force_abstention(self) -> None:
        result = propose_unit_annotation_repair(identity_graph("meter", "second"))

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.AMBIGUOUS_CANDIDATES)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.sites_considered, 2)
        self.assertEqual(result.candidates_considered, 2)

    def test_operation_only_core_has_no_eligible_declaration(self) -> None:
        source = conversion_mismatch()
        verification = verify_graph(source)
        self.assertEqual(
            tuple(item.constraint_id for item in verification.conflict_core),
            ("operation/convert-distance/unit-transform",),
        )

        result = propose_unit_annotation_repair(source)

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.NO_ELIGIBLE_DECLARATION)
        self.assertEqual(result.sites_considered, 0)
        self.assertEqual(result.candidates_considered, 0)

    def test_removing_one_annotation_never_hides_an_independent_conflict(
        self,
    ) -> None:
        result = propose_unit_annotation_repair(independent_conflicts())

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.MULTIPLE_CONFLICTS_REMAIN)
        self.assertIsNone(result.candidate)

    def test_registry_match_is_exact_and_canonical(self) -> None:
        percent_only = UnitRegistry(
            version="1.0.0",
            units=(BUILTIN_REGISTRY.resolve("percent"),),
        )

        result = propose_unit_annotation_repair(
            exp_graph("percent"),
            registry=percent_only,
        )

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.NO_CANONICAL_MATCH)
        self.assertEqual(result.candidates_considered, 0)

    def test_underconstrained_relaxation_cannot_supply_a_candidate(self) -> None:
        result = propose_unit_annotation_repair(underconstrained_relaxation())

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(
            result.reason,
            RepairReason.RELAXED_GRAPH_UNDERCONSTRAINED,
        )
        self.assertIsNone(result.candidate)


class SourceOutcomeTests(unittest.TestCase):
    def test_verified_source_is_not_rewritten(self) -> None:
        result = propose_unit_annotation_repair(identity_graph("meter"))

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.SOURCE_VERIFIED)
        self.assertIsNone(result.candidate)

    def test_underconstrained_source_is_not_rewritten(self) -> None:
        result = propose_unit_annotation_repair(identity_graph(None))

        self.assertEqual(result.status, RepairStatus.ABSTAINED)
        self.assertEqual(result.reason, RepairReason.SOURCE_UNDERCONSTRAINED)
        self.assertIsNone(result.candidate)

    def test_unknown_source_is_indeterminate(self) -> None:
        result = propose_unit_annotation_repair(
            identity_graph(None),
            solver_limits=SolverLimits(max_uniqueness_checks=1),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.SOURCE_UNKNOWN)
        self.assertEqual(
            result.source_verification.unknown_reason,
            UnknownReason.RESOURCE_LIMIT,
        )

    def test_non_minimal_source_core_is_indeterminate(self) -> None:
        result = propose_unit_annotation_repair(
            identity_graph("meter", "second"),
            solver_limits=SolverLimits(max_core_shrink_checks=0),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(
            result.reason,
            RepairReason.SOURCE_CONFLICT_NOT_MINIMAL,
        )


class AggregateBoundTests(unittest.TestCase):
    def test_repair_limit_types_and_ranges_fail_closed(self) -> None:
        cases = (
            ({"max_sites": True}, "exact integer"),
            ({"max_sites": 0}, "site limit"),
            ({"max_sites": MAX_REPAIR_SITES + 1}, "site limit"),
            ({"max_candidates": 0}, "candidate limit"),
            (
                {"max_candidates": MAX_REPAIR_CANDIDATES + 1},
                "candidate limit",
            ),
            ({"max_verifier_calls": 0}, "verifier-call limit"),
            (
                {"max_verifier_calls": MAX_REPAIR_VERIFIER_CALLS + 1},
                "verifier-call limit",
            ),
            ({"max_work_items": 0}, "work-item limit"),
            (
                {"max_work_items": MAX_REPAIR_WORK_ITEMS + 1},
                "work-item limit",
            ),
            ({"total_timeout_ms": 0}, "total timeout"),
            (
                {"total_timeout_ms": MAX_REPAIR_TOTAL_TIMEOUT_MS + 1},
                "total timeout",
            ),
        )
        for changes, message in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(RepairError, message),
            ):
                RepairLimits(**changes)

    def test_site_bound_is_aggregate(self) -> None:
        result = propose_unit_annotation_repair(
            identity_graph("meter", "second"),
            repair_limits=RepairLimits(max_sites=1),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.SITE_LIMIT)
        self.assertEqual(result.sites_considered, 0)

    def test_candidate_bound_prevents_a_partial_uniqueness_claim(self) -> None:
        result = propose_unit_annotation_repair(
            identity_graph("meter", "second"),
            repair_limits=RepairLimits(max_candidates=1),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.CANDIDATE_LIMIT)
        self.assertEqual(result.candidates_considered, 1)
        self.assertIsNone(result.candidate)

    def test_verifier_call_bound_is_shared_by_all_stages(self) -> None:
        result = propose_unit_annotation_repair(
            exp_graph("percent"),
            repair_limits=RepairLimits(max_verifier_calls=1),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.WORK_LIMIT)
        self.assertEqual(result.verification_calls, 1)

    def test_work_bound_covers_non_solver_operations(self) -> None:
        result = propose_unit_annotation_repair(
            exp_graph("percent"),
            repair_limits=RepairLimits(max_work_items=1),
        )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.WORK_LIMIT)
        self.assertEqual(result.work_items, 1)

    def test_elapsed_deadline_is_checked_after_verification(self) -> None:
        with patch.object(
            repair_module._RepairBudget,
            "expired",
            new_callable=PropertyMock,
            side_effect=(False, True),
        ):
            result = propose_unit_annotation_repair(
                exp_graph("percent"),
                repair_limits=RepairLimits(total_timeout_ms=30_000),
            )

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.DEADLINE)
        self.assertEqual(result.verification_calls, 1)

    def test_deadline_is_checked_after_final_proposal_materialization(
        self,
    ) -> None:
        registry = UnitRegistry(
            version="1.0.0",
            units=(
                Unit(
                    "percent",
                    "%",
                    DIMENSIONLESS,
                    Fraction(1, 100),
                ),
                Unit(
                    "z-one",
                    "z1",
                    DIMENSIONLESS,
                    Fraction(1),
                ),
            ),
        )
        source = exp_graph("percent")
        clock = {"now": 0.0}
        original_post_init = UnitRepairCandidate.__post_init__

        def cross_deadline(candidate: UnitRepairCandidate) -> None:
            original_post_init(candidate)
            clock["now"] = 31.0

        with (
            patch.object(
                repair_module.time,
                "monotonic",
                side_effect=lambda: clock["now"],
            ),
            patch.object(
                UnitRepairCandidate,
                "__post_init__",
                cross_deadline,
            ),
        ):
            result = propose_unit_annotation_repair(
                source,
                registry=registry,
                repair_limits=RepairLimits(total_timeout_ms=30_000),
            )

        self.assertEqual(
            tuple(unit.unit_id for unit in registry.units),
            ("percent", "z-one"),
        )
        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.DEADLINE)
        self.assertEqual(result.verification_calls, 3)
        self.assertIsNone(result.candidate)

    def test_deadline_is_checked_after_proposed_result_materialization(
        self,
    ) -> None:
        clock = {"now": 0.0}
        materialized: list[RepairStatus] = []
        original_post_init = UnitRepairResult.__post_init__

        def cross_deadline(result: UnitRepairResult) -> None:
            original_post_init(result)
            materialized.append(result.status)
            if result.status is RepairStatus.PROPOSED:
                clock["now"] = 31.0

        with (
            patch.object(
                repair_module.time,
                "monotonic",
                side_effect=lambda: clock["now"],
            ),
            patch.object(
                UnitRepairResult,
                "__post_init__",
                cross_deadline,
            ),
        ):
            result = propose_unit_annotation_repair(
                exp_graph("percent"),
                repair_limits=RepairLimits(total_timeout_ms=30_000),
            )

        self.assertEqual(
            materialized,
            [RepairStatus.PROPOSED, RepairStatus.INDETERMINATE],
        )
        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.DEADLINE)
        self.assertEqual(result.verification_calls, 3)
        self.assertEqual(
            result.source_verification.status,
            VerificationStatus.CONFLICT,
        )
        self.assertIs(result.source_verification.core_minimal, True)
        self.assertIsNone(result.candidate)

    def test_deadline_is_checked_after_early_abstention_materialization(
        self,
    ) -> None:
        cases = (
            (
                identity_graph("meter"),
                RepairReason.SOURCE_VERIFIED,
                VerificationStatus.VERIFIED,
            ),
            (
                identity_graph(None),
                RepairReason.SOURCE_UNDERCONSTRAINED,
                VerificationStatus.UNDERCONSTRAINED,
            ),
            (
                conversion_mismatch(),
                RepairReason.NO_ELIGIBLE_DECLARATION,
                VerificationStatus.CONFLICT,
            ),
        )
        original_post_init = UnitRepairResult.__post_init__

        for source, provisional_reason, source_status in cases:
            clock = {"now": 0.0}
            materialized: list[tuple[RepairStatus, RepairReason | None]] = []

            def cross_deadline(
                result: UnitRepairResult,
                *,
                case_clock: dict[str, float] = clock,
                records: list[tuple[RepairStatus, RepairReason | None]] = materialized,
            ) -> None:
                original_post_init(result)
                records.append((result.status, result.reason))
                if result.status is RepairStatus.ABSTAINED:
                    case_clock["now"] = 31.0

            with (
                self.subTest(provisional_reason=provisional_reason),
                patch.object(
                    repair_module.time,
                    "monotonic",
                    side_effect=lambda case_clock=clock: case_clock["now"],
                ),
                patch.object(
                    UnitRepairResult,
                    "__post_init__",
                    cross_deadline,
                ),
            ):
                result = propose_unit_annotation_repair(
                    source,
                    repair_limits=RepairLimits(total_timeout_ms=30_000),
                )

            self.assertEqual(
                materialized,
                [
                    (RepairStatus.ABSTAINED, provisional_reason),
                    (RepairStatus.INDETERMINATE, RepairReason.DEADLINE),
                ],
            )
            self.assertEqual(result.status, RepairStatus.INDETERMINATE)
            self.assertEqual(result.reason, RepairReason.DEADLINE)
            self.assertEqual(result.verification_calls, 1)
            self.assertEqual(result.source_verification.status, source_status)
            self.assertIsNone(result.candidate)


class FailureAndIntegrityTests(unittest.TestCase):
    def test_verifier_exceptions_are_redacted_into_closed_results(self) -> None:
        with patch.object(
            repair_module,
            "verify_graph",
            side_effect=RuntimeError("secret /private/model/path"),
        ):
            result = propose_unit_annotation_repair(exp_graph("percent"))

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.VERIFIER_FAILURE)
        self.assertIsNone(result.source_verification)
        self.assertNotIn(b"secret", result.canonical_bytes())
        self.assertNotIn(b"/private", result.canonical_bytes())

    def test_wrong_verifier_return_type_is_not_trusted(self) -> None:
        with patch.object(repair_module, "verify_graph", return_value=object()):
            result = propose_unit_annotation_repair(exp_graph("percent"))

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.VERIFIER_FAILURE)

    def test_internal_exceptions_are_redacted_into_closed_results(self) -> None:
        with patch.object(
            repair_module,
            "_clone_with_unit",
            side_effect=RuntimeError("secret /private/repair/path"),
        ):
            result = propose_unit_annotation_repair(exp_graph("percent"))

        self.assertEqual(result.status, RepairStatus.INDETERMINATE)
        self.assertEqual(result.reason, RepairReason.INTERNAL_FAILURE)
        self.assertNotIn(b"secret", result.canonical_bytes())
        self.assertNotIn(b"/private", result.canonical_bytes())

    def test_unknown_relaxed_and_candidate_checks_are_indeterminate(self) -> None:
        real_verifier = repair_module.verify_graph

        for unknown_call, expected_reason in (
            (2, RepairReason.RELAXED_VERIFICATION_UNKNOWN),
            (3, RepairReason.CANDIDATE_VERIFICATION_UNKNOWN),
        ):
            calls = 0

            def verifier_with_unknown(
                candidate_graph: ComputationGraph,
                *,
                registry: UnitRegistry,
                limits: SolverLimits,
                target_call: int = unknown_call,
            ) -> VerificationResult:
                nonlocal calls
                calls += 1
                if calls != target_call:
                    return real_verifier(
                        candidate_graph,
                        registry=registry,
                        limits=limits,
                    )
                return VerificationResult(
                    status=VerificationStatus.UNKNOWN,
                    graph_digest=candidate_graph.digest,
                    registry_digest=registry.digest,
                    solver_version="4.16.0",
                    limits=limits,
                    checks_performed=1,
                    unknown_reason=UnknownReason.SOLVER_UNKNOWN,
                )

            with (
                self.subTest(unknown_call=unknown_call),
                patch.object(
                    repair_module,
                    "verify_graph",
                    side_effect=verifier_with_unknown,
                ),
            ):
                result = propose_unit_annotation_repair(exp_graph("percent"))

            self.assertEqual(result.status, RepairStatus.INDETERMINATE)
            self.assertEqual(result.reason, expected_reason)

    def test_public_boundaries_reject_subclasses(self) -> None:
        class DerivedGraph(ComputationGraph):
            pass

        class DerivedRegistry(UnitRegistry):
            pass

        class DerivedRepairLimits(RepairLimits):
            pass

        class DerivedSolverLimits(SolverLimits):
            pass

        with self.assertRaisesRegex(RepairError, "exact ComputationGraph"):
            propose_unit_annotation_repair(object.__new__(DerivedGraph))
        with self.assertRaisesRegex(RepairError, "exact UnitRegistry"):
            propose_unit_annotation_repair(
                exp_graph("percent"),
                registry=object.__new__(DerivedRegistry),
            )
        with self.assertRaisesRegex(RepairError, "exact RepairLimits"):
            propose_unit_annotation_repair(
                exp_graph("percent"),
                repair_limits=object.__new__(DerivedRepairLimits),
            )
        with self.assertRaisesRegex(RepairError, "exact SolverLimits"):
            propose_unit_annotation_repair(
                exp_graph("percent"),
                solver_limits=object.__new__(DerivedSolverLimits),
            )

    def test_models_are_frozen_and_nested_mutation_is_detected(self) -> None:
        result = propose_unit_annotation_repair(exp_graph("percent"))
        proposal = result.candidate
        assert proposal is not None

        with self.assertRaises(FrozenInstanceError):
            result.status = RepairStatus.ABSTAINED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            proposal.value_id = "output"  # type: ignore[misc]

        object.__setattr__(proposal, "replacement_unit_id", "meter")
        with self.assertRaises(RepairError):
            proposal.validate()
        with self.assertRaises(RepairError):
            result.validate()

    def test_invalid_exact_inputs_raise_only_redacted_repair_errors(self) -> None:
        source = exp_graph("percent")
        object.__setattr__(source, "_digest", "0" * 64)

        with self.assertRaisesRegex(
            RepairError,
            "repair inputs are rejected or mutated",
        ) as captured:
            propose_unit_annotation_repair(source)

        self.assertNotIsInstance(captured.exception.__cause__, UnitSentinelError)


class RepairModelContractTests(unittest.TestCase):
    def test_candidate_and_result_subclasses_are_rejected(self) -> None:
        result = propose_unit_annotation_repair(exp_graph("percent"))
        proposal = result.candidate
        assert proposal is not None

        class DerivedCandidate(UnitRepairCandidate):
            pass

        class DerivedResult(UnitRepairResult):
            pass

        candidate_arguments = {
            "constraint_id": proposal.constraint_id,
            "value_id": proposal.value_id,
            "previous_unit_id": proposal.previous_unit_id,
            "replacement_unit_id": proposal.replacement_unit_id,
            "relaxed_graph": proposal.relaxed_graph,
            "repaired_graph": proposal.repaired_graph,
            "relaxed_verification": proposal.relaxed_verification,
            "repaired_verification": proposal.repaired_verification,
        }
        with self.assertRaisesRegex(RepairError, "exact UnitRepairCandidate"):
            DerivedCandidate(**candidate_arguments)

        result_arguments = {
            "status": result.status,
            "reason": result.reason,
            "source_graph": result.source_graph,
            "registry": result.registry,
            "repair_limits": result.repair_limits,
            "solver_limits": result.solver_limits,
            "verification_calls": result.verification_calls,
            "sites_considered": result.sites_considered,
            "candidates_considered": result.candidates_considered,
            "work_items": result.work_items,
            "source_verification": result.source_verification,
            "candidate": result.candidate,
        }
        with self.assertRaisesRegex(RepairError, "exact UnitRepairResult"):
            DerivedResult(**result_arguments)


if __name__ == "__main__":
    unittest.main()
