from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from typing import cast
from unittest.mock import patch

import z3

import unitsentinel.replay as replay_module
from examples.build_speed_contract import build_graph
from unitsentinel.certificate import ProofCertificate, create_certificate
from unitsentinel.domain import DIMENSIONLESS, LENGTH, QuantityKind, Unit
from unitsentinel.graph import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY, UnitAlias, UnitRegistry
from unitsentinel.replay import (
    CERTIFICATE_REPLAY_SCHEMA,
    CertificateReplay,
    CertificateReplayError,
    ReplayReason,
    ReplayStatus,
    replay_certificate,
)
from unitsentinel.verification import (
    ConstraintWitness,
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)
from unitsentinel.verifier import (
    _replay_claimed_contracts,
    constraint_catalog,
    verify_graph,
)
from unitsentinel.version import VERSION


def ambiguous_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="ambiguous-replay",
        values=(
            ValueSpec("input", ScalarType.FLOAT64, ()),
            ValueSpec("output", ScalarType.FLOAT64, ()),
        ),
        inputs=("input",),
        nodes=(
            Node(
                "copy-input",
                Operation.IDENTITY,
                ("input",),
                "output",
            ),
        ),
        outputs=("output",),
    )


def conflicting_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="conflicting-replay",
        values=(
            ValueSpec(
                "raw-speed",
                ScalarType.FLOAT64,
                ("batch",),
                "kilometer-per-hour",
            ),
            ValueSpec(
                "si-speed",
                ScalarType.FLOAT64,
                ("batch",),
                "kilogram",
            ),
        ),
        inputs=("raw-speed",),
        nodes=(
            Node(
                "normalize-speed",
                Operation.CONVERT,
                ("raw-speed",),
                "si-speed",
                target_unit_id="meter-per-second",
            ),
        ),
        outputs=("si-speed",),
    )


def custom_registry_and_graph() -> tuple[UnitRegistry, ComputationGraph]:
    registry = UnitRegistry(
        version="1.2.3",
        units=(Unit("smoot", "smoot", LENGTH, Fraction(17_018, 10_000)),),
    )
    graph = ComputationGraph(
        graph_id="custom-replay",
        values=(ValueSpec("distance", ScalarType.FLOAT64, (), "smoot"),),
        inputs=("distance",),
        nodes=(),
        outputs=("distance",),
    )
    return registry, graph


def forged_certificate(
    graph: ComputationGraph,
    contracts: tuple[InferredContract, ...],
    *,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    registry_version: str | None = None,
    verifier_version: str = VERSION,
    limits: SolverLimits | None = None,
) -> ProofCertificate:
    run_limits = SolverLimits() if limits is None else limits
    result = VerificationResult(
        status=VerificationStatus.VERIFIED,
        graph_digest=graph.digest,
        registry_digest=registry.digest,
        solver_version=z3.get_version_string(),
        limits=run_limits,
        checks_performed=2,
        contracts=contracts,
    )
    return ProofCertificate(
        registry_version=(
            registry.version if registry_version is None else registry_version
        ),
        verifier_version=verifier_version,
        constraints=constraint_catalog(graph, registry),
        result=result,
    )


def report_arguments(report: CertificateReplay) -> dict[str, object]:
    return {
        "status": report.status,
        "reason": report.reason,
        "certificate_digest": report.certificate_digest,
        "graph_digest": report.graph_digest,
        "registry_digest": report.registry_digest,
        "registry_version": report.registry_version,
        "strict_toolchain": report.strict_toolchain,
        "certificate_verifier_version": report.certificate_verifier_version,
        "certificate_solver_version": report.certificate_solver_version,
        "current_verifier_version": report.current_verifier_version,
        "current_solver_version": report.current_solver_version,
        "toolchain_match": report.toolchain_match,
        "fresh_result": report.fresh_result,
    }


class CertificateReplayOutcomeTests(unittest.TestCase):
    def test_verified_claim_is_reproduced_deterministically(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)

        first = replay_certificate(certificate, graph)
        second = replay_certificate(certificate, graph)
        record = first.canonical_record()

        self.assertEqual(first.status, ReplayStatus.REPRODUCED)
        self.assertIsNone(first.reason)
        self.assertTrue(first.toolchain_match)
        self.assertIsNotNone(first.fresh_result)
        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(record["schema"], CERTIFICATE_REPLAY_SCHEMA)
        self.assertEqual(record["status"], "reproduced")
        self.assertIsNone(record["reason"])
        fresh_record = cast(dict[str, object], record["fresh_result"])
        assert first.fresh_result is not None
        self.assertEqual(fresh_record["sha256"], first.fresh_result.digest)
        self.assertEqual(len(first.canonical_bytes()), 1_418)
        self.assertEqual(
            first.digest,
            "cb0229922842baac31cac32894f607d264d4b2e62a1637447ccb7237ac716192",
        )

    def test_custom_registry_claim_is_reproduced(self) -> None:
        registry, graph = custom_registry_and_graph()
        certificate = create_certificate(graph, registry)

        result = replay_certificate(certificate, graph, registry)

        self.assertEqual(result.status, ReplayStatus.REPRODUCED)
        self.assertEqual(result.registry_digest, registry.digest)
        self.assertEqual(result.registry_version, registry.version)

    def test_source_binding_mismatches_stop_before_solver(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)
        other_graph = ComputationGraph(
            graph_id="other-speed-contract",
            values=graph.values,
            inputs=graph.inputs,
            nodes=graph.nodes,
            outputs=graph.outputs,
        )
        changed_registry = UnitRegistry(
            version=BUILTIN_REGISTRY.version,
            units=BUILTIN_REGISTRY.units,
            aliases=tuple(
                sorted(
                    (
                        *BUILTIN_REGISTRY.aliases,
                        UnitAlias("meters", "meter"),
                    ),
                    key=lambda alias: alias.alias_id,
                )
            ),
        )
        version_claim = ProofCertificate(
            registry_version="9.9.9",
            verifier_version=certificate.verifier_version,
            constraints=certificate.constraints,
            result=certificate.result,
        )

        cases = (
            (
                "graph",
                certificate,
                other_graph,
                BUILTIN_REGISTRY,
                ReplayReason.GRAPH_DIGEST_MISMATCH,
            ),
            (
                "registry-digest",
                certificate,
                graph,
                changed_registry,
                ReplayReason.REGISTRY_DIGEST_MISMATCH,
            ),
            (
                "registry-version",
                version_claim,
                graph,
                BUILTIN_REGISTRY,
                ReplayReason.REGISTRY_VERSION_MISMATCH,
            ),
        )
        for name, claim, source_graph, registry, reason in cases:
            with (
                self.subTest(case=name),
                patch.object(
                    replay_module,
                    "verify_graph",
                    side_effect=AssertionError("solver must not run"),
                ) as solver,
            ):
                result = replay_certificate(claim, source_graph, registry)

                self.assertEqual(result.status, ReplayStatus.MISMATCH)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.fresh_result)
                solver.assert_not_called()

    def test_catalog_and_coverage_mismatches_stop_before_solver(self) -> None:
        graph = build_graph()
        issued = create_certificate(graph)
        catalog_claim = ProofCertificate(
            registry_version=issued.registry_version,
            verifier_version=issued.verifier_version,
            constraints=issued.constraints[1:],
            result=issued.result,
        )
        partial_result = VerificationResult(
            status=issued.result.status,
            graph_digest=issued.result.graph_digest,
            registry_digest=issued.result.registry_digest,
            solver_version=issued.result.solver_version,
            limits=issued.result.limits,
            checks_performed=issued.result.checks_performed,
            contracts=issued.result.contracts[:1],
        )
        coverage_claim = ProofCertificate(
            registry_version=issued.registry_version,
            verifier_version=issued.verifier_version,
            constraints=issued.constraints,
            result=partial_result,
        )

        cases = (
            (
                "catalog",
                catalog_claim,
                ReplayReason.CONSTRAINT_CATALOG_MISMATCH,
            ),
            (
                "coverage",
                coverage_claim,
                ReplayReason.CONTRACT_COVERAGE_MISMATCH,
            ),
        )
        for name, claim, reason in cases:
            with (
                self.subTest(case=name),
                patch.object(
                    replay_module,
                    "verify_graph",
                    side_effect=AssertionError("solver must not run"),
                ) as solver,
            ):
                result = replay_certificate(claim, graph)

                self.assertEqual(result.status, ReplayStatus.MISMATCH)
                self.assertEqual(result.reason, reason)
                solver.assert_not_called()

    def test_false_contract_witness_stops_before_solver(self) -> None:
        graph = build_graph()
        issued = create_certificate(graph)
        changed_contract = InferredContract(
            value_id=issued.result.contracts[0].value_id,
            dimension=issued.result.contracts[0].dimension,
            kind=issued.result.contracts[0].kind,
            scale=Fraction(1),
            offset=issued.result.contracts[0].offset,
        )
        claim = forged_certificate(
            graph,
            (changed_contract, issued.result.contracts[1]),
        )

        with patch.object(
            replay_module,
            "verify_graph",
            side_effect=AssertionError("solver must not run"),
        ) as solver:
            result = replay_certificate(claim, graph)

        self.assertEqual(result.status, ReplayStatus.MISMATCH)
        self.assertEqual(result.reason, ReplayReason.CONTRACT_WITNESS_MISMATCH)
        solver.assert_not_called()

    def test_satisfying_but_nonunique_claim_fails_fresh_uniqueness(self) -> None:
        graph = ambiguous_graph()
        contracts = (
            InferredContract(
                "input",
                DIMENSIONLESS,
                QuantityKind.LINEAR,
                Fraction(1),
                Fraction(0),
            ),
            InferredContract(
                "output",
                DIMENSIONLESS,
                QuantityKind.LINEAR,
                Fraction(1),
                Fraction(0),
            ),
        )
        claim = forged_certificate(graph, contracts)

        result = replay_certificate(claim, graph)

        self.assertEqual(result.status, ReplayStatus.MISMATCH)
        self.assertEqual(result.reason, ReplayReason.FRESH_UNDERCONSTRAINED)
        assert result.fresh_result is not None
        self.assertEqual(
            result.fresh_result.status,
            VerificationStatus.UNDERCONSTRAINED,
        )

    def test_fresh_conflict_and_contract_difference_are_mismatches(self) -> None:
        conflict = conflicting_graph()
        source_claim = create_certificate(build_graph())
        conflict_claim = forged_certificate(
            conflict,
            source_claim.result.contracts,
        )
        conflict_result = verify_graph(conflict)
        self.assertEqual(conflict_result.status, VerificationStatus.CONFLICT)

        with patch.object(
            replay_module,
            "_replay_claimed_contracts",
            return_value=True,
        ):
            replayed_conflict = replay_certificate(conflict_claim, conflict)

        self.assertEqual(replayed_conflict.status, ReplayStatus.MISMATCH)
        self.assertEqual(replayed_conflict.reason, ReplayReason.FRESH_CONFLICT)

        graph = build_graph()
        issued = create_certificate(graph)
        fresh = verify_graph(graph)
        changed_contract = InferredContract(
            value_id=fresh.contracts[0].value_id,
            dimension=fresh.contracts[0].dimension,
            kind=fresh.contracts[0].kind,
            scale=Fraction(1),
            offset=fresh.contracts[0].offset,
        )
        different_result = VerificationResult(
            status=fresh.status,
            graph_digest=fresh.graph_digest,
            registry_digest=fresh.registry_digest,
            solver_version=fresh.solver_version,
            limits=fresh.limits,
            checks_performed=fresh.checks_performed,
            contracts=(changed_contract, fresh.contracts[1]),
        )
        with patch.object(
            replay_module,
            "verify_graph",
            return_value=different_result,
        ):
            replayed_difference = replay_certificate(issued, graph)

        self.assertEqual(replayed_difference.status, ReplayStatus.MISMATCH)
        self.assertEqual(
            replayed_difference.reason,
            ReplayReason.FRESH_CONTRACT_MISMATCH,
        )

    def test_fresh_unknown_is_indeterminate(self) -> None:
        graph = build_graph()
        issued = create_certificate(graph)
        limits = SolverLimits()
        for unknown_reason in UnknownReason:
            unknown = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                graph_digest=graph.digest,
                registry_digest=BUILTIN_REGISTRY.digest,
                solver_version=z3.get_version_string(),
                limits=limits,
                checks_performed=0,
                unknown_reason=unknown_reason,
            )

            with (
                self.subTest(reason=unknown_reason),
                patch.object(
                    replay_module,
                    "verify_graph",
                    return_value=unknown,
                ),
            ):
                result = replay_certificate(issued, graph, limits=limits)

                self.assertEqual(result.status, ReplayStatus.INDETERMINATE)
                self.assertEqual(result.reason, ReplayReason.FRESH_UNKNOWN)
                self.assertEqual(result.fresh_result, unknown)

    def test_replay_uses_only_caller_supplied_limits(self) -> None:
        graph = build_graph()
        historical_limits = SolverLimits(
            per_check_timeout_ms=10_000,
            total_timeout_ms=60_000,
            max_memory_mb=4_096,
            max_core_shrink_checks=1_024,
            max_uniqueness_checks=1_024,
        )
        certificate = create_certificate(
            graph,
            limits=historical_limits,
        )
        caller_limits = SolverLimits(
            per_check_timeout_ms=100,
            total_timeout_ms=2_000,
            max_memory_mb=128,
            max_core_shrink_checks=4,
            max_uniqueness_checks=8,
        )
        real_verify = replay_module.verify_graph

        def assert_caller_limits(
            candidate: ComputationGraph,
            *,
            registry: UnitRegistry,
            limits: SolverLimits,
        ) -> VerificationResult:
            self.assertIs(limits, caller_limits)
            self.assertIsNot(limits, certificate.result.limits)
            return real_verify(candidate, registry=registry, limits=limits)

        with patch.object(
            replay_module,
            "verify_graph",
            side_effect=assert_caller_limits,
        ):
            result = replay_certificate(
                certificate,
                graph,
                limits=caller_limits,
            )

        self.assertEqual(result.status, ReplayStatus.REPRODUCED)
        assert result.fresh_result is not None
        self.assertEqual(result.fresh_result.limits, caller_limits)

    def test_toolchain_policy_is_explicit_and_solver_short_circuited(self) -> None:
        graph = build_graph()
        issued = create_certificate(graph)
        claim = ProofCertificate(
            registry_version=issued.registry_version,
            verifier_version="9.9.9",
            constraints=issued.constraints,
            result=issued.result,
        )

        nonstrict = replay_certificate(claim, graph)

        self.assertEqual(nonstrict.status, ReplayStatus.REPRODUCED)
        self.assertFalse(nonstrict.toolchain_match)
        self.assertFalse(nonstrict.strict_toolchain)

        with (
            patch.object(
                replay_module,
                "_replay_claimed_contracts",
                side_effect=AssertionError("pure replay must not run"),
            ) as pure,
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=AssertionError("solver must not run"),
            ) as solver,
        ):
            strict = replay_certificate(
                claim,
                graph,
                strict_toolchain=True,
            )

        self.assertEqual(strict.status, ReplayStatus.MISMATCH)
        self.assertEqual(strict.reason, ReplayReason.TOOLCHAIN_MISMATCH)
        self.assertFalse(strict.toolchain_match)
        pure.assert_not_called()
        solver.assert_not_called()


class CertificateReplaySafetyTests(unittest.TestCase):
    def test_replay_requires_exact_valid_inputs(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "certificate",
                {"certificate": "claim", "graph": graph},
                "exact ProofCertificate",
            ),
            (
                "graph",
                {"certificate": certificate, "graph": "graph"},
                "exact ComputationGraph",
            ),
            (
                "registry",
                {
                    "certificate": certificate,
                    "graph": graph,
                    "registry": "registry",
                },
                "exact UnitRegistry",
            ),
            (
                "limits",
                {
                    "certificate": certificate,
                    "graph": graph,
                    "limits": "limits",
                },
                "exact SolverLimits",
            ),
            (
                "strict",
                {
                    "certificate": certificate,
                    "graph": graph,
                    "strict_toolchain": 1,
                },
                "exact boolean",
            ),
        )

        for name, arguments, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateReplayError, message),
            ):
                replay_certificate(**arguments)  # type: ignore[arg-type]

        object.__setattr__(graph, "graph_id", "mutated")
        with self.assertRaisesRegex(CertificateReplayError, "malformed or mutated"):
            replay_certificate(certificate, graph)

    def test_input_mutation_during_fresh_verification_fails_closed(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)
        registry = UnitRegistry(
            BUILTIN_REGISTRY.version,
            BUILTIN_REGISTRY.units,
            BUILTIN_REGISTRY.aliases,
        )
        limits = SolverLimits()
        real_verify = replay_module.verify_graph

        def mutate_after_verification(
            candidate: ComputationGraph,
            *,
            registry: UnitRegistry,
            limits: SolverLimits,
        ) -> VerificationResult:
            result = real_verify(candidate, registry=registry, limits=limits)
            object.__setattr__(limits, "total_timeout_ms", 6_000)
            return result

        with (
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=mutate_after_verification,
            ),
            self.assertRaisesRegex(
                CertificateReplayError,
                "changed during replay",
            ),
        ):
            replay_certificate(
                certificate,
                graph,
                registry,
                limits=limits,
            )

        graph = build_graph()
        certificate = create_certificate(graph)
        limits = SolverLimits()

        def invalidate_graph_after_verification(
            candidate: ComputationGraph,
            *,
            registry: UnitRegistry,
            limits: SolverLimits,
        ) -> VerificationResult:
            result = real_verify(candidate, registry=registry, limits=limits)
            object.__setattr__(candidate, "graph_id", "INVALID")
            return result

        with (
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=invalidate_graph_after_verification,
            ),
            self.assertRaisesRegex(
                CertificateReplayError,
                "changed during replay",
            ),
        ):
            replay_certificate(certificate, graph, limits=limits)

        graph = build_graph()
        certificate = create_certificate(graph)
        real_validate_fresh = replay_module._validate_fresh_result

        def mutate_after_fresh_validation(
            result: object,
            *,
            limits: SolverLimits,
            pins: object,
        ) -> VerificationResult:
            validated = real_validate_fresh(
                result,
                limits=limits,
                pins=pins,  # type: ignore[arg-type]
            )
            object.__setattr__(graph, "graph_id", "INVALID")
            return validated

        with (
            patch.object(
                replay_module,
                "_validate_fresh_result",
                side_effect=mutate_after_fresh_validation,
            ),
            self.assertRaisesRegex(
                CertificateReplayError,
                "changed during replay",
            ),
        ):
            replay_certificate(certificate, graph)

    def test_certificate_and_registry_mutation_during_replay_fails_closed(
        self,
    ) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)

        def mutate_certificate_during_pure_replay(
            candidate: ComputationGraph,
            registry: UnitRegistry,
            contracts: tuple[InferredContract, ...],
        ) -> bool:
            object.__setattr__(certificate, "verifier_version", "9.9.9")
            return True

        with (
            patch.object(
                replay_module,
                "_replay_claimed_contracts",
                side_effect=mutate_certificate_during_pure_replay,
            ),
            self.assertRaisesRegex(
                CertificateReplayError,
                "changed during replay",
            ),
        ):
            replay_certificate(certificate, graph)

        graph = build_graph()
        certificate = create_certificate(graph)
        registry = UnitRegistry(
            BUILTIN_REGISTRY.version,
            BUILTIN_REGISTRY.units,
            BUILTIN_REGISTRY.aliases,
        )
        real_catalog = replay_module.constraint_catalog

        def mutate_registry_after_catalog(
            candidate: ComputationGraph,
            registry: UnitRegistry,
        ) -> tuple[ConstraintWitness, ...]:
            catalog = real_catalog(candidate, registry)
            object.__setattr__(registry, "version", "9.9.9")
            return catalog

        with (
            patch.object(
                replay_module,
                "constraint_catalog",
                side_effect=mutate_registry_after_catalog,
            ),
            self.assertRaisesRegex(
                CertificateReplayError,
                "changed during replay",
            ),
        ):
            replay_certificate(certificate, graph, registry)

    def test_catalog_and_pure_replay_exceptions_are_contained(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)

        with (
            patch.object(
                replay_module,
                "constraint_catalog",
                side_effect=VerificationError("rejected"),
            ),
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=AssertionError("solver must not run"),
            ) as solver,
        ):
            catalog_mismatch = replay_certificate(certificate, graph)

        self.assertEqual(
            catalog_mismatch.reason,
            ReplayReason.CONSTRAINT_CATALOG_MISMATCH,
        )
        solver.assert_not_called()

        with (
            patch.object(
                replay_module,
                "constraint_catalog",
                side_effect=RuntimeError("private /tmp/catalog"),
            ),
            self.assertRaises(CertificateReplayError) as raised,
        ):
            replay_certificate(certificate, graph)
        self.assertEqual(
            str(raised.exception),
            "certificate constraint catalog failed",
        )
        self.assertNotIn("/tmp", str(raised.exception))

        with (
            patch.object(
                replay_module,
                "_replay_claimed_contracts",
                side_effect=VerificationError("rejected"),
            ),
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=AssertionError("solver must not run"),
            ) as solver,
        ):
            witness_mismatch = replay_certificate(certificate, graph)

        self.assertEqual(
            witness_mismatch.reason,
            ReplayReason.CONTRACT_WITNESS_MISMATCH,
        )
        solver.assert_not_called()

        with (
            patch.object(
                replay_module,
                "_replay_claimed_contracts",
                side_effect=RuntimeError("private /tmp/replay"),
            ),
            self.assertRaises(CertificateReplayError) as raised,
        ):
            replay_certificate(certificate, graph)
        self.assertEqual(
            str(raised.exception),
            "certificate contract replay failed",
        )
        self.assertNotIn("/tmp", str(raised.exception))

    def test_toolchain_pin_failure_is_redacted(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)

        with (
            patch.object(
                replay_module.z3,
                "get_version_string",
                side_effect=RuntimeError("private /tmp/z3"),
            ),
            self.assertRaises(CertificateReplayError) as raised,
        ):
            replay_certificate(certificate, graph)

        self.assertEqual(str(raised.exception), "replay inputs could not be pinned")
        self.assertNotIn("/tmp", str(raised.exception))

    def test_unexpected_verifier_failure_is_redacted(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)

        with (
            patch.object(
                replay_module,
                "verify_graph",
                side_effect=VerificationError("private /tmp/solver-path"),
            ),
            self.assertRaises(CertificateReplayError) as raised,
        ):
            replay_certificate(certificate, graph)

        self.assertEqual(
            str(raised.exception),
            "fresh certificate verification failed",
        )
        self.assertNotIn("/tmp", str(raised.exception))

    def test_fresh_result_type_and_bindings_are_revalidated(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)
        other_graph = ComputationGraph(
            graph_id="other-fresh-result",
            values=graph.values,
            inputs=graph.inputs,
            nodes=graph.nodes,
            outputs=graph.outputs,
        )
        other_result = verify_graph(other_graph)

        cases = (
            ("type", "result", "invalid result type"),
            ("binding", other_result, "inconsistent result"),
        )
        for name, fresh_result, message in cases:
            with (
                self.subTest(case=name),
                patch.object(
                    replay_module,
                    "verify_graph",
                    return_value=fresh_result,
                ),
                self.assertRaisesRegex(CertificateReplayError, message),
            ):
                replay_certificate(certificate, graph)

        malformed_result = verify_graph(graph)
        object.__setattr__(malformed_result, "_digest", "invalid")
        with (
            patch.object(
                replay_module,
                "verify_graph",
                return_value=malformed_result,
            ),
            self.assertRaisesRegex(CertificateReplayError, "malformed result"),
        ):
            replay_certificate(certificate, graph)

    def test_pure_replay_wrapper_rejects_malformed_assignments(self) -> None:
        graph = build_graph()
        certificate = create_certificate(graph)
        with self.assertRaisesRegex(VerificationError, "exact ComputationGraph"):
            _replay_claimed_contracts(  # type: ignore[arg-type]
                "graph",
                BUILTIN_REGISTRY,
                certificate.result.contracts,
            )
        with self.assertRaisesRegex(VerificationError, "exact UnitRegistry"):
            _replay_claimed_contracts(  # type: ignore[arg-type]
                graph,
                "registry",
                certificate.result.contracts,
            )
        with self.assertRaisesRegex(VerificationError, "must be a tuple"):
            _replay_claimed_contracts(  # type: ignore[arg-type]
                graph,
                BUILTIN_REGISTRY,
                list(certificate.result.contracts),
            )
        with self.assertRaisesRegex(VerificationError, "exact InferredContract"):
            _replay_claimed_contracts(  # type: ignore[arg-type]
                graph,
                BUILTIN_REGISTRY,
                ("contract",),
            )
        self.assertFalse(
            _replay_claimed_contracts(
                graph,
                BUILTIN_REGISTRY,
                certificate.result.contracts[:1],
            )
        )
        with self.assertRaisesRegex(VerificationError, "sorted and unique"):
            _replay_claimed_contracts(
                graph,
                BUILTIN_REGISTRY,
                (
                    certificate.result.contracts[0],
                    certificate.result.contracts[0],
                ),
            )

        graph = build_graph()
        object.__setattr__(graph, "graph_id", "INVALID")
        with self.assertRaisesRegex(VerificationError, "rejected or mutated"):
            _replay_claimed_contracts(
                graph,
                BUILTIN_REGISTRY,
                certificate.result.contracts,
            )

        certificate = create_certificate(build_graph())
        object.__setattr__(
            certificate.result.contracts[0],
            "scale",
            "invalid",
        )
        with self.assertRaisesRegex(VerificationError, "malformed or mutated"):
            _replay_claimed_contracts(
                build_graph(),
                BUILTIN_REGISTRY,
                certificate.result.contracts,
            )


class CertificateReplayValueTests(unittest.TestCase):
    def test_report_is_exact_immutable_and_content_addressed(self) -> None:
        report = replay_certificate(
            create_certificate(build_graph()),
            build_graph(),
        )

        class DerivedReplay(CertificateReplay):
            pass

        with self.assertRaisesRegex(CertificateReplayError, "exact CertificateReplay"):
            DerivedReplay(**report_arguments(report))  # type: ignore[arg-type]
        with self.assertRaises(FrozenInstanceError):
            report.reason = ReplayReason.FRESH_UNKNOWN  # type: ignore[misc]

        object.__setattr__(report, "_digest", "not-a-digest")
        with self.assertRaisesRegex(CertificateReplayError, "digest is malformed"):
            report.validate()

        report = replay_certificate(
            create_certificate(build_graph()),
            build_graph(),
        )
        object.__setattr__(report, "registry_version", "9.9.9")
        with self.assertRaisesRegex(CertificateReplayError, "does not match"):
            report.validate()

    def test_report_rejects_incoherent_manual_construction(self) -> None:
        report = replay_certificate(
            create_certificate(build_graph()),
            build_graph(),
        )
        base = report_arguments(report)
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            ("status", {**base, "status": "reproduced"}, "status is unknown"),
            ("reason", {**base, "reason": "reason"}, "reason is unknown"),
            (
                "digest",
                {**base, "certificate_digest": "0"},
                "digest is malformed",
            ),
            (
                "registry-version",
                {**base, "registry_version": "latest"},
                "canonical SemVer",
            ),
            (
                "solver-version",
                {**base, "current_solver_version": "latest"},
                "solver version is malformed",
            ),
            (
                "strict-type",
                {**base, "strict_toolchain": 1},
                "exact boolean",
            ),
            (
                "match-type",
                {**base, "toolchain_match": 1},
                "exact boolean",
            ),
            (
                "match-value",
                {**base, "toolchain_match": False},
                "match flag is inconsistent",
            ),
            (
                "fresh-type",
                {**base, "fresh_result": "result"},
                "exact VerificationResult",
            ),
            (
                "reproduced-reason",
                {**base, "reason": ReplayReason.FRESH_UNKNOWN},
                "reproduced replay fields are inconsistent",
            ),
            (
                "indeterminate",
                {
                    **base,
                    "status": ReplayStatus.INDETERMINATE,
                    "reason": ReplayReason.FRESH_UNKNOWN,
                },
                "indeterminate replay fields are inconsistent",
            ),
            (
                "early-with-fresh",
                {
                    **base,
                    "status": ReplayStatus.MISMATCH,
                    "reason": ReplayReason.GRAPH_DIGEST_MISMATCH,
                },
                "early mismatch cannot contain",
            ),
            (
                "toolchain-policy",
                {
                    **base,
                    "status": ReplayStatus.MISMATCH,
                    "reason": ReplayReason.TOOLCHAIN_MISMATCH,
                    "fresh_result": None,
                },
                "toolchain mismatch fields are inconsistent",
            ),
            (
                "mismatch-no-reason",
                {
                    **base,
                    "status": ReplayStatus.MISMATCH,
                    "reason": None,
                    "fresh_result": None,
                },
                "mismatch replay fields are inconsistent",
            ),
            (
                "fresh-mismatch-status",
                {
                    **base,
                    "status": ReplayStatus.MISMATCH,
                    "reason": ReplayReason.FRESH_CONFLICT,
                },
                "mismatch replay fields are inconsistent",
            ),
        )

        for name, arguments, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateReplayError, message),
            ):
                CertificateReplay(**arguments)  # type: ignore[arg-type]

        malformed_fresh = verify_graph(build_graph())
        object.__setattr__(malformed_fresh, "_digest", "invalid")
        with self.assertRaisesRegex(
            CertificateReplayError,
            "malformed or mutated",
        ):
            CertificateReplay(
                **{
                    **base,
                    "fresh_result": malformed_fresh,
                }  # type: ignore[arg-type]
            )

        other_graph = ComputationGraph(
            graph_id="other-report-source",
            values=build_graph().values,
            inputs=build_graph().inputs,
            nodes=build_graph().nodes,
            outputs=build_graph().outputs,
        )
        other_result = verify_graph(other_graph)
        with self.assertRaisesRegex(
            CertificateReplayError,
            "inconsistent source bindings",
        ):
            CertificateReplay(
                **{
                    **base,
                    "fresh_result": other_result,
                }  # type: ignore[arg-type]
            )

        source = verify_graph(build_graph())
        other_solver_result = VerificationResult(
            status=source.status,
            graph_digest=source.graph_digest,
            registry_digest=source.registry_digest,
            solver_version="9.9.9",
            limits=source.limits,
            checks_performed=source.checks_performed,
            contracts=source.contracts,
        )
        with self.assertRaisesRegex(
            CertificateReplayError,
            "inconsistent toolchain identity",
        ):
            CertificateReplay(
                **{
                    **base,
                    "fresh_result": other_solver_result,
                }  # type: ignore[arg-type]
            )

        unknown = VerificationResult(
            status=VerificationStatus.UNKNOWN,
            graph_digest=report.graph_digest,
            registry_digest=report.registry_digest,
            solver_version=report.current_solver_version,
            limits=SolverLimits(),
            checks_performed=0,
            unknown_reason=UnknownReason.RESOURCE_LIMIT,
        )
        with self.assertRaisesRegex(
            CertificateReplayError,
            "strict-toolchain replay fields are inconsistent",
        ):
            CertificateReplay(
                **{
                    **base,
                    "status": ReplayStatus.INDETERMINATE,
                    "reason": ReplayReason.FRESH_UNKNOWN,
                    "strict_toolchain": True,
                    "certificate_verifier_version": "9.9.9",
                    "toolchain_match": False,
                    "fresh_result": unknown,
                }  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
