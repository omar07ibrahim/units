from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from typing import cast
from unittest.mock import patch

from examples.build_speed_contract import build_graph
from unitsentinel import certificate as certificate_module
from unitsentinel.certificate import (
    CERTIFICATE_SCHEMA,
    MAX_CERTIFICATE_CHECKS,
    SOLVER_IMPLEMENTATION,
    VERIFIER_IMPLEMENTATION,
    VERIFIER_SEMANTICS,
    CertificateError,
    ProofCertificate,
    create_certificate,
    encode_certificate,
)
from unitsentinel.domain import LENGTH, Unit
from unitsentinel.graph import (
    GRAPH_SCHEMA,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA, UnitRegistry
from unitsentinel.verification import (
    SolverLimits,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)
from unitsentinel.verifier import constraint_catalog, verify_graph
from unitsentinel.version import VERSION


def ambiguous_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="ambiguous-identity",
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


class ConstraintCatalogTests(unittest.TestCase):
    def test_catalog_matches_the_compiler_source_manifest(self) -> None:
        graph = build_graph()

        catalog = constraint_catalog(graph)

        self.assertEqual(
            tuple(witness.constraint_id for witness in catalog),
            (
                "declaration/raw-speed/unit",
                "declaration/si-speed/unit",
                "operation/normalize-speed/dimension",
                "operation/normalize-speed/kind",
                "operation/normalize-speed/unit-transform",
            ),
        )
        self.assertEqual(
            tuple(witness.rule for witness in catalog),
            (
                "unit-annotation",
                "unit-annotation",
                "convert-dimension",
                "convert-kind",
                "convert-unit-transform",
            ),
        )

    def test_catalog_rejects_wrong_and_mutated_graphs(self) -> None:
        with self.assertRaisesRegex(VerificationError, "exact ComputationGraph"):
            constraint_catalog("graph")  # type: ignore[arg-type]

        graph = build_graph()
        object.__setattr__(graph.values[0], "unit_id", "meter-per-second")
        with self.assertRaisesRegex(VerificationError, "rejected or mutated"):
            constraint_catalog(graph)

    def test_catalog_honors_the_supplied_registry(self) -> None:
        registry = UnitRegistry(
            version="1.0.0",
            units=(Unit("smoot", "smoot", LENGTH, Fraction(17_018, 10_000)),),
        )
        graph = ComputationGraph(
            graph_id="custom-length",
            values=(ValueSpec("distance", ScalarType.FLOAT64, (), "smoot"),),
            inputs=("distance",),
            nodes=(),
            outputs=("distance",),
        )

        catalog = constraint_catalog(graph, registry)
        certificate = create_certificate(graph, registry)

        self.assertEqual(
            tuple(witness.constraint_id for witness in catalog),
            ("declaration/distance/unit",),
        )
        self.assertEqual(certificate.constraints, catalog)
        self.assertEqual(certificate.result.registry_digest, registry.digest)
        self.assertEqual(certificate.registry_version, registry.version)


class ProofCertificateTests(unittest.TestCase):
    def test_positive_certificate_is_canonical_and_content_addressed(self) -> None:
        graph = build_graph()

        certificate = create_certificate(graph)
        record = certificate.canonical_record()

        self.assertEqual(certificate.result.status, VerificationStatus.VERIFIED)
        self.assertEqual(record["schema"], CERTIFICATE_SCHEMA)
        self.assertEqual(
            record["graph"],
            {"schema": GRAPH_SCHEMA, "sha256": graph.digest},
        )
        self.assertEqual(
            record["registry"],
            {
                "schema": REGISTRY_SCHEMA,
                "sha256": BUILTIN_REGISTRY.digest,
                "version": BUILTIN_REGISTRY.version,
            },
        )
        self.assertEqual(
            record["solver"],
            {
                "implementation": SOLVER_IMPLEMENTATION,
                "version": "4.16.0",
            },
        )
        self.assertEqual(
            record["verifier"],
            {
                "implementation": VERIFIER_IMPLEMENTATION,
                "semantics": VERIFIER_SEMANTICS,
                "version": VERSION,
            },
        )
        proof = record["proof"]
        self.assertIs(type(proof), dict)
        proof_record = cast(dict[str, object], proof)
        self.assertEqual(proof_record["outcome"], "verified")
        self.assertEqual(
            proof_record["verification_result_sha256"],
            certificate.result.digest,
        )
        contracts = cast(list[dict[str, object]], proof_record["contracts"])
        self.assertIs(type(contracts), list)
        self.assertEqual(contracts[0]["scale"], "5/18")
        self.assertEqual(contracts[1]["scale"], "1")
        run_record = cast(dict[str, object], record["run"])
        self.assertEqual(run_record["checks_performed"], 2)
        self.assertEqual(encode_certificate(certificate), certificate.canonical_bytes())
        self.assertEqual(certificate.canonical_bytes()[-1:], b"}")
        self.assertEqual(len(certificate.canonical_bytes()), 1_721)
        self.assertEqual(
            certificate.digest,
            "c5ab1819a73c57111e3977fc69a109a11d43cfa0456e91e3eb38f862779f81b7",
        )

        repeated = create_certificate(graph)
        self.assertEqual(certificate.canonical_bytes(), repeated.canonical_bytes())
        self.assertEqual(certificate.digest, repeated.digest)

    def test_nonverified_graph_never_receives_a_positive_certificate(self) -> None:
        graph = ambiguous_graph()
        result = verify_graph(graph)
        self.assertEqual(result.status, VerificationStatus.UNDERCONSTRAINED)

        with self.assertRaisesRegex(
            CertificateError,
            "requires verified, got underconstrained",
        ):
            create_certificate(graph)

    def test_registry_mutation_during_verification_blocks_issuance(self) -> None:
        graph = build_graph()
        registry = UnitRegistry(
            BUILTIN_REGISTRY.version,
            BUILTIN_REGISTRY.units,
            BUILTIN_REGISTRY.aliases,
        )
        real_verify = certificate_module.verify_graph

        def mutate_registry_after_verification(
            candidate: ComputationGraph,
            *,
            registry: UnitRegistry,
            limits: SolverLimits,
        ) -> VerificationResult:
            result = real_verify(candidate, registry=registry, limits=limits)
            object.__setattr__(registry, "version", "9.9.9")
            return result

        with (
            patch.object(
                certificate_module,
                "verify_graph",
                side_effect=mutate_registry_after_verification,
            ),
            self.assertRaisesRegex(
                CertificateError,
                "source changed during verification",
            ),
        ):
            create_certificate(graph, registry)

    def test_graph_and_limits_mutation_during_verification_blocks_issuance(
        self,
    ) -> None:
        graph = build_graph()
        limits = SolverLimits()
        real_verify = certificate_module.verify_graph

        def mutate_graph_after_verification(
            candidate: ComputationGraph,
            *,
            registry: UnitRegistry,
            limits: SolverLimits,
        ) -> VerificationResult:
            result = real_verify(candidate, registry=registry, limits=limits)
            object.__setattr__(candidate, "graph_id", "changed-speed-contract")
            return result

        with (
            patch.object(
                certificate_module,
                "verify_graph",
                side_effect=mutate_graph_after_verification,
            ),
            self.assertRaisesRegex(
                CertificateError,
                "source changed during verification",
            ),
        ):
            create_certificate(graph, limits=limits)

        graph = build_graph()

        def mutate_limits_after_verification(
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
                certificate_module,
                "verify_graph",
                side_effect=mutate_limits_after_verification,
            ),
            self.assertRaisesRegex(
                CertificateError,
                "source changed during verification",
            ),
        ):
            create_certificate(graph, limits=limits)

    def test_certificate_value_requires_exact_sorted_positive_evidence(
        self,
    ) -> None:
        certificate = create_certificate(build_graph())

        class DerivedCertificate(ProofCertificate):
            pass

        with self.assertRaisesRegex(CertificateError, "exact ProofCertificate"):
            DerivedCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                certificate.result,
            )
        with self.assertRaisesRegex(CertificateError, "canonical SemVer"):
            ProofCertificate(
                "latest",
                certificate.verifier_version,
                certificate.constraints,
                certificate.result,
            )
        with self.assertRaisesRegex(CertificateError, "must be a tuple"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                list(certificate.constraints),  # type: ignore[arg-type]
                certificate.result,
            )
        with self.assertRaisesRegex(CertificateError, "cannot be empty"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                (),
                certificate.result,
            )
        with self.assertRaisesRegex(CertificateError, "sorted and unique"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                tuple(reversed(certificate.constraints)),
                certificate.result,
            )
        with self.assertRaisesRegex(CertificateError, "exact VerificationResult"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                "result",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(CertificateError, "verified result"):
            ProofCertificate(
                BUILTIN_REGISTRY.version,
                VERSION,
                constraint_catalog(ambiguous_graph()),
                verify_graph(ambiguous_graph()),
            )

    def test_certificate_enforces_manifest_and_contract_bounds(self) -> None:
        certificate = create_certificate(build_graph())

        with (
            patch.object(certificate_module, "MAX_CERTIFICATE_CONSTRAINTS", 0),
            self.assertRaisesRegex(CertificateError, "manifest exceeds"),
        ):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                certificate.result,
            )
        with (
            patch.object(certificate_module, "MAX_CERTIFICATE_CONTRACTS", 0),
            self.assertRaisesRegex(CertificateError, "contracts exceed"),
        ):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                certificate.result,
            )

    def test_certificate_bounds_solver_provenance(self) -> None:
        certificate = create_certificate(build_graph())
        result = certificate.result
        long_version = VerificationResult(
            status=result.status,
            graph_digest=result.graph_digest,
            registry_digest=result.registry_digest,
            solver_version="1.0." + ("0" * 29),
            limits=result.limits,
            checks_performed=result.checks_performed,
            contracts=result.contracts,
        )
        with self.assertRaisesRegex(CertificateError, "solver version exceeds"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                long_version,
            )

        too_many_checks = VerificationResult(
            status=result.status,
            graph_digest=result.graph_digest,
            registry_digest=result.registry_digest,
            solver_version=result.solver_version,
            limits=result.limits,
            checks_performed=MAX_CERTIFICATE_CHECKS + 1,
            contracts=result.contracts,
        )
        with self.assertRaisesRegex(CertificateError, "check count exceeds"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                too_many_checks,
            )

    def test_certificate_rejects_invalid_nested_witnesses(self) -> None:
        certificate = create_certificate(build_graph())
        with self.assertRaisesRegex(CertificateError, "exact witnesses"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                ("witness",),  # type: ignore[arg-type]
                certificate.result,
            )

        certificate = create_certificate(build_graph())
        object.__setattr__(certificate.constraints[0], "constraint_id", "INVALID")
        with self.assertRaisesRegex(CertificateError, "invalid constraint witness"):
            ProofCertificate(
                certificate.registry_version,
                certificate.verifier_version,
                certificate.constraints,
                certificate.result,
            )

    def test_certificate_digest_detects_malformed_and_changed_claims(self) -> None:
        certificate = create_certificate(build_graph())
        object.__setattr__(certificate, "_digest", "not-a-digest")
        with self.assertRaisesRegex(CertificateError, "digest is malformed"):
            certificate.validate()

        certificate = create_certificate(build_graph())
        object.__setattr__(certificate, "verifier_version", "0.1.1")
        with self.assertRaisesRegex(CertificateError, "does not match"):
            certificate.validate()

    def test_nested_mutation_invalidates_certificate_evidence(self) -> None:
        certificate = create_certificate(build_graph())
        object.__setattr__(
            certificate.result.contracts[0],
            "scale",
            Fraction(1),
        )

        with self.assertRaisesRegex(CertificateError, "verification result"):
            certificate.canonical_record()
        with self.assertRaisesRegex(CertificateError, "encoding failed"):
            encode_certificate(certificate)

    def test_encoder_and_values_are_exact_and_immutable(self) -> None:
        certificate = create_certificate(build_graph())

        with self.assertRaisesRegex(CertificateError, "exact ProofCertificate"):
            encode_certificate("certificate")  # type: ignore[arg-type]
        with self.assertRaises(FrozenInstanceError):
            certificate.registry_version = "2.0.0"  # type: ignore[misc]

    def test_factory_rejects_wrong_and_mutated_sources(self) -> None:
        graph = build_graph()
        with self.assertRaisesRegex(CertificateError, "exact ComputationGraph"):
            create_certificate("graph")  # type: ignore[arg-type]
        with self.assertRaisesRegex(CertificateError, "exact UnitRegistry"):
            create_certificate(graph, "registry")  # type: ignore[arg-type]
        with self.assertRaisesRegex(CertificateError, "exact SolverLimits"):
            create_certificate(graph, limits="limits")  # type: ignore[arg-type]

        object.__setattr__(graph.values[0], "unit_id", "meter-per-second")
        with self.assertRaisesRegex(CertificateError, "rejected or mutated"):
            create_certificate(graph)

    def test_factory_wraps_verifier_and_certificate_failures(self) -> None:
        graph = build_graph()
        with (
            patch.object(
                certificate_module,
                "verify_graph",
                side_effect=VerificationError("injected verifier failure"),
            ),
            self.assertRaisesRegex(CertificateError, "verification failed"),
        ):
            create_certificate(graph)

        with (
            patch.object(
                certificate_module,
                "ProofCertificate",
                side_effect=CertificateError("injected constructor failure"),
            ),
            self.assertRaisesRegex(CertificateError, "could not be certified"),
        ):
            create_certificate(graph)

    def test_factory_rejects_results_for_another_or_partial_graph(self) -> None:
        graph = build_graph()
        other_graph = ComputationGraph(
            graph_id="other-speed-contract",
            values=graph.values,
            inputs=graph.inputs,
            nodes=graph.nodes,
            outputs=graph.outputs,
        )
        other_result = verify_graph(other_graph)
        with (
            patch.object(
                certificate_module,
                "verify_graph",
                return_value=other_result,
            ),
            self.assertRaisesRegex(CertificateError, "source changed"),
        ):
            create_certificate(graph)

        full_result = verify_graph(graph)
        partial_result = VerificationResult(
            status=full_result.status,
            graph_digest=full_result.graph_digest,
            registry_digest=full_result.registry_digest,
            solver_version=full_result.solver_version,
            limits=full_result.limits,
            checks_performed=full_result.checks_performed,
            contracts=full_result.contracts[:1],
        )
        with (
            patch.object(
                certificate_module,
                "verify_graph",
                return_value=partial_result,
            ),
            self.assertRaisesRegex(CertificateError, "cover every graph value"),
        ):
            create_certificate(graph)


if __name__ == "__main__":
    unittest.main()
