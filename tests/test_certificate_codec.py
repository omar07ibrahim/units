from __future__ import annotations

import copy
import unittest
from collections.abc import Callable
from typing import cast

from examples.build_speed_contract import build_graph
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.certificate import (
    MAX_CERTIFICATE_BYTES,
    MAX_CERTIFICATE_CHECKS,
    MAX_CERTIFICATE_SOLVER_VERSION_LENGTH,
    CertificateDecodeError,
    ProofCertificate,
    create_certificate,
    decode_certificate,
    encode_certificate,
)
from unitsentinel.verification import VerificationResult
from unitsentinel.version import VERSION

CertificateRecord = dict[str, object]
RecordMutation = Callable[[CertificateRecord], None]


def certificate_record() -> CertificateRecord:
    return create_certificate(build_graph()).canonical_record()


def nested(record: CertificateRecord, field: str) -> CertificateRecord:
    return cast(CertificateRecord, record[field])


def items(record: CertificateRecord, field: str) -> list[object]:
    return cast(list[object], record[field])


def encoded_mutation(mutation: RecordMutation) -> bytes:
    record = copy.deepcopy(certificate_record())
    mutation(record)
    return canonical_json_bytes(record)


class CertificateBoundaryTests(unittest.TestCase):
    def test_decoder_round_trips_exact_canonical_bytes(self) -> None:
        issued = create_certificate(build_graph())
        payload = encode_certificate(issued)

        decoded = decode_certificate(payload)

        self.assertEqual(decoded, issued)
        self.assertEqual(decoded.digest, issued.digest)
        self.assertEqual(decoded.result.digest, issued.result.digest)
        self.assertEqual(decoded.canonical_bytes(), payload)
        self.assertEqual(decode_certificate(payload), decoded)

    def test_decoder_round_trips_the_model_boundary(self) -> None:
        issued = create_certificate(build_graph())
        source = issued.result
        solver_version = "1.0." + ("0" * 28)
        self.assertEqual(
            len(solver_version),
            MAX_CERTIFICATE_SOLVER_VERSION_LENGTH,
        )
        boundary_result = VerificationResult(
            status=source.status,
            graph_digest=source.graph_digest,
            registry_digest=source.registry_digest,
            solver_version=solver_version,
            limits=source.limits,
            checks_performed=MAX_CERTIFICATE_CHECKS,
            contracts=source.contracts,
        )
        boundary_claim = ProofCertificate(
            registry_version=issued.registry_version,
            verifier_version=VERSION,
            constraints=issued.constraints,
            result=boundary_result,
        )

        decoded = decode_certificate(boundary_claim.canonical_bytes())

        self.assertEqual(decoded, boundary_claim)

    def test_decoder_rejects_unsafe_or_noncanonical_json(self) -> None:
        payload = encode_certificate(create_certificate(build_graph()))
        cases: tuple[tuple[str, object, str], ...] = (
            ("wrong-type", "certificate", "exact bytes"),
            ("empty", b"", "payload is empty"),
            ("trailing-newline", payload + b"\n", "not canonical JSON"),
            (
                "duplicate-key",
                b'{"schema":"first","schema":"second"}',
                "duplicate object key",
            ),
            (
                "oversized",
                b"0" * (MAX_CERTIFICATE_BYTES + 1),
                "byte limit",
            ),
        )

        for name, candidate, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateDecodeError, message),
            ):
                decode_certificate(candidate)  # type: ignore[arg-type]

    def test_decoder_requires_exact_root_and_identity_records(self) -> None:
        def add_extension(record: CertificateRecord) -> None:
            record["extension"] = None

        def wrong_schema(record: CertificateRecord) -> None:
            record["schema"] = "unitsentinel.proof-certificate/v2"

        def wrong_graph_shape(record: CertificateRecord) -> None:
            record["graph"] = []

        def wrong_graph_schema(record: CertificateRecord) -> None:
            nested(record, "graph")["schema"] = "unitsentinel.graph/v2"

        def malformed_graph_digest(record: CertificateRecord) -> None:
            nested(record, "graph")["sha256"] = "0"

        def wrong_registry_shape(record: CertificateRecord) -> None:
            record["registry"] = []

        def wrong_registry_schema(record: CertificateRecord) -> None:
            nested(record, "registry")["schema"] = "registry/v2"

        def wrong_registry_version_type(record: CertificateRecord) -> None:
            nested(record, "registry")["version"] = 1

        def wrong_solver_shape(record: CertificateRecord) -> None:
            record["solver"] = []

        def wrong_solver_implementation(record: CertificateRecord) -> None:
            nested(record, "solver")["implementation"] = "other"

        def wrong_solver_version_type(record: CertificateRecord) -> None:
            nested(record, "solver")["version"] = 4

        def wrong_verifier_shape(record: CertificateRecord) -> None:
            record["verifier"] = []

        def wrong_verifier_implementation(record: CertificateRecord) -> None:
            nested(record, "verifier")["implementation"] = "other"

        def wrong_verifier_semantics(record: CertificateRecord) -> None:
            nested(record, "verifier")["semantics"] = "unitsentinel.verifier/v2"

        def wrong_verifier_version_type(record: CertificateRecord) -> None:
            nested(record, "verifier")["version"] = 1

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("extension", add_extension, "missing or unknown fields"),
            ("schema", wrong_schema, "certificate schema is not supported"),
            ("graph-shape", wrong_graph_shape, "graph binding must be an object"),
            ("graph-schema", wrong_graph_schema, "graph schema is not supported"),
            ("graph-digest", malformed_graph_digest, "graph digest is malformed"),
            (
                "registry-shape",
                wrong_registry_shape,
                "registry binding must be an object",
            ),
            (
                "registry-schema",
                wrong_registry_schema,
                "registry schema is not supported",
            ),
            (
                "registry-version",
                wrong_registry_version_type,
                "registry version must be text",
            ),
            ("solver-shape", wrong_solver_shape, "solver identity must be an object"),
            (
                "solver-implementation",
                wrong_solver_implementation,
                "solver implementation is not supported",
            ),
            (
                "solver-version",
                wrong_solver_version_type,
                "solver version must be text",
            ),
            (
                "verifier-shape",
                wrong_verifier_shape,
                "verifier identity must be an object",
            ),
            (
                "verifier-implementation",
                wrong_verifier_implementation,
                "verifier implementation is not supported",
            ),
            (
                "verifier-semantics",
                wrong_verifier_semantics,
                "verifier semantics is not supported",
            ),
            (
                "verifier-version",
                wrong_verifier_version_type,
                "verifier version must be text",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateDecodeError, message),
            ):
                decode_certificate(encoded_mutation(mutation))

    def test_decoder_requires_exact_run_and_proof_records(self) -> None:
        def wrong_run_shape(record: CertificateRecord) -> None:
            record["run"] = []

        def wrong_limit_shape(record: CertificateRecord) -> None:
            nested(record, "run")["limits"] = []

        def bool_check_count(record: CertificateRecord) -> None:
            nested(record, "run")["checks_performed"] = True

        def bool_timeout(record: CertificateRecord) -> None:
            limits = nested(nested(record, "run"), "limits")
            limits["per_check_timeout_ms"] = True

        def invalid_timeout(record: CertificateRecord) -> None:
            limits = nested(nested(record, "run"), "limits")
            limits["per_check_timeout_ms"] = 0

        def wrong_proof_shape(record: CertificateRecord) -> None:
            record["proof"] = []

        def wrong_outcome(record: CertificateRecord) -> None:
            nested(record, "proof")["outcome"] = "conflict"

        def malformed_result_digest(record: CertificateRecord) -> None:
            nested(record, "proof")["verification_result_sha256"] = "0"

        def mismatched_result_digest(record: CertificateRecord) -> None:
            nested(record, "proof")["verification_result_sha256"] = "0" * 64

        def wrong_constraint_array(record: CertificateRecord) -> None:
            nested(record, "proof")["constraints"] = {}

        def wrong_contract_array(record: CertificateRecord) -> None:
            nested(record, "proof")["contracts"] = {}

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("run-shape", wrong_run_shape, "verification run must be an object"),
            ("limits-shape", wrong_limit_shape, "solver limits must be an object"),
            ("check-bool", bool_check_count, "solver check count must be an exact"),
            ("limit-bool", bool_timeout, "per-check timeout must be an exact"),
            ("limit-range", invalid_timeout, "semantic contract failed"),
            ("proof-shape", wrong_proof_shape, "proof claim must be an object"),
            ("outcome", wrong_outcome, "proof outcome is not supported"),
            (
                "result-digest-shape",
                malformed_result_digest,
                "verification-result digest is malformed",
            ),
            (
                "result-digest-value",
                mismatched_result_digest,
                "does not match its contents",
            ),
            (
                "constraint-array",
                wrong_constraint_array,
                "proof constraints must be an array",
            ),
            (
                "contract-array",
                wrong_contract_array,
                "proof contracts must be an array",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateDecodeError, message),
            ):
                decode_certificate(encoded_mutation(mutation))

    def test_decoder_rejects_malformed_constraint_claims(self) -> None:
        def first_constraint(record: CertificateRecord) -> CertificateRecord:
            proof = nested(record, "proof")
            return cast(CertificateRecord, items(proof, "constraints")[0])

        def wrong_shape(record: CertificateRecord) -> None:
            items(nested(record, "proof"), "constraints")[0] = []

        def extension(record: CertificateRecord) -> None:
            first_constraint(record)["extension"] = None

        def wrong_source_type(record: CertificateRecord) -> None:
            first_constraint(record)["source"] = 1

        def unknown_source(record: CertificateRecord) -> None:
            first_constraint(record)["source"] = "unknown"

        def wrong_id_type(record: CertificateRecord) -> None:
            first_constraint(record)["constraint_id"] = 1

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("shape", wrong_shape, "constraint witness must be an object"),
            ("extension", extension, "missing or unknown fields"),
            ("source-type", wrong_source_type, "constraint source must be text"),
            ("source-value", unknown_source, "constraint source is not supported"),
            ("identifier-type", wrong_id_type, "constraint identifier must be text"),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateDecodeError, message),
            ):
                decode_certificate(encoded_mutation(mutation))

    def test_decoder_rejects_malformed_contract_claims(self) -> None:
        def first_contract(record: CertificateRecord) -> CertificateRecord:
            proof = nested(record, "proof")
            return cast(CertificateRecord, items(proof, "contracts")[0])

        def first_term(record: CertificateRecord) -> CertificateRecord:
            contract = first_contract(record)
            return cast(CertificateRecord, items(contract, "dimension")[0])

        def wrong_shape(record: CertificateRecord) -> None:
            items(nested(record, "proof"), "contracts")[0] = []

        def extension(record: CertificateRecord) -> None:
            first_contract(record)["extension"] = None

        def wrong_kind_type(record: CertificateRecord) -> None:
            first_contract(record)["kind"] = 1

        def unknown_kind(record: CertificateRecord) -> None:
            first_contract(record)["kind"] = "unknown"

        def wrong_value_id_type(record: CertificateRecord) -> None:
            first_contract(record)["value_id"] = 1

        def wrong_dimension_shape(record: CertificateRecord) -> None:
            first_contract(record)["dimension"] = {}

        def wrong_term_shape(record: CertificateRecord) -> None:
            items(first_contract(record), "dimension")[0] = []

        def wrong_base_type(record: CertificateRecord) -> None:
            first_term(record)["base"] = 1

        def unknown_base(record: CertificateRecord) -> None:
            first_term(record)["base"] = "currency"

        def duplicate_base(record: CertificateRecord) -> None:
            terms = items(first_contract(record), "dimension")
            terms.append(copy.deepcopy(terms[0]))

        def wrong_rational_type(record: CertificateRecord) -> None:
            first_contract(record)["scale"] = 1

        def malformed_rational(record: CertificateRecord) -> None:
            first_contract(record)["scale"] = "01"

        def unreduced_rational(record: CertificateRecord) -> None:
            first_contract(record)["scale"] = "10/36"

        def invalid_semantics(record: CertificateRecord) -> None:
            first_contract(record)["scale"] = "0"

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("shape", wrong_shape, "inferred contract must be an object"),
            ("extension", extension, "missing or unknown fields"),
            ("kind-type", wrong_kind_type, "inferred quantity kind must be text"),
            ("kind-value", unknown_kind, "quantity kind is not supported"),
            ("value-id", wrong_value_id_type, "inferred value identifier must be text"),
            ("dimension-shape", wrong_dimension_shape, "dimension must be an array"),
            ("term-shape", wrong_term_shape, "dimension term must be an object"),
            ("base-type", wrong_base_type, "dimension base must be text"),
            ("base-value", unknown_base, "dimension base is not supported"),
            ("duplicate-base", duplicate_base, "duplicate base"),
            ("rational-type", wrong_rational_type, "inferred scale must be text"),
            ("rational-shape", malformed_rational, "not a canonical rational"),
            ("rational-reduction", unreduced_rational, "is not reduced"),
            ("semantic-contract", invalid_semantics, "semantic contract failed"),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(CertificateDecodeError, message),
            ):
                decode_certificate(encoded_mutation(mutation))

    def test_decoder_rejects_semantically_equivalent_noncanonical_model(self) -> None:
        def reverse_dimension(record: CertificateRecord) -> None:
            proof = nested(record, "proof")
            contract = cast(CertificateRecord, items(proof, "contracts")[0])
            items(contract, "dimension").reverse()

        with self.assertRaisesRegex(
            CertificateDecodeError,
            "does not match the canonical certificate model",
        ):
            decode_certificate(encoded_mutation(reverse_dimension))

    def test_decoder_accepts_an_internally_coherent_unsigned_partial_claim(
        self,
    ) -> None:
        issued = create_certificate(build_graph())
        source = issued.result
        partial_result = VerificationResult(
            status=source.status,
            graph_digest=source.graph_digest,
            registry_digest=source.registry_digest,
            solver_version=source.solver_version,
            limits=source.limits,
            checks_performed=source.checks_performed,
            contracts=source.contracts[:1],
        )
        unsigned_claim = ProofCertificate(
            registry_version=issued.registry_version,
            verifier_version=VERSION,
            constraints=issued.constraints,
            result=partial_result,
        )

        decoded = decode_certificate(unsigned_claim.canonical_bytes())

        self.assertEqual(decoded, unsigned_claim)
        self.assertEqual(len(decoded.result.contracts), 1)


if __name__ == "__main__":
    unittest.main()
