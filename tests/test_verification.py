from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from unitsentinel.domain import LENGTH, QuantityKind
from unitsentinel.verification import (
    MAX_CORE_SHRINK_CHECKS,
    MAX_SOLVER_MEMORY_MB,
    MAX_SOLVER_TIMEOUT_MS,
    MAX_TOTAL_TIMEOUT_MS,
    MAX_UNIQUENESS_CHECKS,
    ConstraintSource,
    ConstraintWitness,
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)

GRAPH_DIGEST = "1" * 64
REGISTRY_DIGEST = "2" * 64


def contract(value_id: str = "distance") -> InferredContract:
    return InferredContract(value_id, LENGTH, QuantityKind.LINEAR)


def witness(constraint_id: str = "declaration/distance/unit") -> ConstraintWitness:
    return ConstraintWitness(
        constraint_id,
        ConstraintSource.DECLARATION,
        "distance",
        "unit-annotation",
    )


def result(
    status: VerificationStatus,
    **changes: object,
) -> VerificationResult:
    arguments: dict[str, object] = {
        "status": status,
        "graph_digest": GRAPH_DIGEST,
        "registry_digest": REGISTRY_DIGEST,
        "solver_version": "4.16.0",
        "limits": SolverLimits(),
        "checks_performed": 1,
    }
    arguments.update(changes)
    return VerificationResult(**arguments)  # type: ignore[arg-type]


class SolverLimitTests(unittest.TestCase):
    def test_default_limits_are_explicit_and_canonical(self) -> None:
        limits = SolverLimits()
        self.assertEqual(
            limits.canonical_record(),
            {
                "max_core_shrink_checks": 64,
                "max_memory_mb": 256,
                "max_uniqueness_checks": 577,
                "per_check_timeout_ms": 250,
                "total_timeout_ms": 5_000,
            },
        )

    def test_limit_types_and_ranges_fail_closed(self) -> None:
        cases = (
            ({"per_check_timeout_ms": True}, "exact integer"),
            ({"per_check_timeout_ms": 0}, "per-check timeout"),
            (
                {"per_check_timeout_ms": MAX_SOLVER_TIMEOUT_MS + 1},
                "per-check timeout",
            ),
            (
                {"per_check_timeout_ms": 500, "total_timeout_ms": 499},
                "total timeout",
            ),
            ({"total_timeout_ms": MAX_TOTAL_TIMEOUT_MS + 1}, "total timeout"),
            ({"max_memory_mb": 31}, "memory"),
            ({"max_memory_mb": MAX_SOLVER_MEMORY_MB + 1}, "memory"),
            ({"max_core_shrink_checks": -1}, "core-shrink"),
            (
                {"max_core_shrink_checks": MAX_CORE_SHRINK_CHECKS + 1},
                "core-shrink",
            ),
            ({"max_uniqueness_checks": 0}, "uniqueness"),
            (
                {"max_uniqueness_checks": MAX_UNIQUENESS_CHECKS + 1},
                "uniqueness",
            ),
        )
        for changes, message in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(VerificationError, message),
            ):
                SolverLimits(**changes)  # type: ignore[arg-type]


class VerificationValueTests(unittest.TestCase):
    def test_constraint_witness_and_contract_records_are_stable(self) -> None:
        self.assertEqual(
            witness().canonical_record(),
            {
                "constraint_id": "declaration/distance/unit",
                "rule": "unit-annotation",
                "source": "declaration",
                "source_id": "distance",
            },
        )
        self.assertEqual(
            contract().canonical_record(),
            {
                "dimension": [{"base": "length", "exponent": "1"}],
                "kind": "linear",
                "value_id": "distance",
            },
        )

    def test_exact_receiver_and_identifier_policies_fail_closed(self) -> None:
        class DerivedWitness(ConstraintWitness):
            pass

        class DerivedContract(InferredContract):
            pass

        with self.assertRaisesRegex(VerificationError, "exact ConstraintWitness"):
            DerivedWitness(
                "declaration/distance/unit",
                ConstraintSource.DECLARATION,
                "distance",
                "unit-annotation",
            )
        with self.assertRaisesRegex(VerificationError, "exact InferredContract"):
            DerivedContract("distance", LENGTH, QuantityKind.LINEAR)
        with self.assertRaisesRegex(VerificationError, "not canonical"):
            witness("Declaration/distance")
        with self.assertRaisesRegex(VerificationError, "source is unknown"):
            ConstraintWitness(
                "declaration/distance/unit",
                "declaration",  # type: ignore[arg-type]
                "distance",
                "unit-annotation",
            )
        with self.assertRaisesRegex(VerificationError, "quantity kind"):
            InferredContract(
                "distance",
                LENGTH,
                "linear",  # type: ignore[arg-type]
            )


class VerificationResultTests(unittest.TestCase):
    def test_each_outcome_has_one_unambiguous_shape(self) -> None:
        verified = result(
            VerificationStatus.VERIFIED,
            contracts=(contract(),),
        )
        underconstrained = result(
            VerificationStatus.UNDERCONSTRAINED,
            underconstrained_values=("distance",),
        )
        conflict = result(
            VerificationStatus.CONFLICT,
            conflict_core=(witness(),),
            core_minimal=True,
        )
        unknown = result(
            VerificationStatus.UNKNOWN,
            checks_performed=0,
            unknown_reason=UnknownReason.CONTRACT_REJECTED,
        )

        self.assertEqual(verified.status.value, "verified")
        self.assertEqual(
            underconstrained.canonical_record()["underconstrained_values"],
            ["distance"],
        )
        self.assertTrue(conflict.canonical_record()["core_minimal"])
        self.assertEqual(
            unknown.canonical_record()["unknown_reason"],
            "contract-rejected",
        )
        self.assertEqual(len(verified.digest), 64)
        self.assertEqual(verified.canonical_bytes()[-1:], b"}")

    def test_outcome_specific_fields_cannot_be_mixed(self) -> None:
        cases = (
            (
                VerificationStatus.VERIFIED,
                {"contracts": ()},
                "verified",
            ),
            (
                VerificationStatus.VERIFIED,
                {
                    "contracts": (contract(),),
                    "underconstrained_values": ("x",),
                },
                "verified",
            ),
            (
                VerificationStatus.UNDERCONSTRAINED,
                {},
                "underconstrained",
            ),
            (
                VerificationStatus.CONFLICT,
                {"conflict_core": (witness(),), "core_minimal": None},
                "conflict",
            ),
            (
                VerificationStatus.UNKNOWN,
                {},
                "unknown",
            ),
        )
        for status, changes, message in cases:
            with (
                self.subTest(status=status, changes=changes),
                self.assertRaisesRegex(VerificationError, message),
            ):
                result(status, **changes)

    def test_result_collections_are_exact_sorted_and_unique(self) -> None:
        with self.assertRaisesRegex(VerificationError, "sorted and unique"):
            result(
                VerificationStatus.VERIFIED,
                contracts=(contract("z"), contract("a")),
            )
        with self.assertRaisesRegex(VerificationError, "sorted and unique"):
            result(
                VerificationStatus.UNDERCONSTRAINED,
                underconstrained_values=("z", "a"),
            )
        with self.assertRaisesRegex(VerificationError, "identifiers must be unique"):
            result(
                VerificationStatus.CONFLICT,
                conflict_core=(witness(), witness()),
                core_minimal=False,
            )

    def test_metadata_and_low_level_mutation_are_revalidated(self) -> None:
        with self.assertRaisesRegex(VerificationError, "graph digest"):
            result(
                VerificationStatus.VERIFIED,
                graph_digest="bad",
                contracts=(contract(),),
            )
        with self.assertRaisesRegex(VerificationError, "solver version"):
            result(
                VerificationStatus.VERIFIED,
                solver_version="latest",
                contracts=(contract(),),
            )
        with self.assertRaisesRegex(VerificationError, "cannot be negative"):
            result(
                VerificationStatus.VERIFIED,
                checks_performed=-1,
                contracts=(contract(),),
            )

        verified = result(
            VerificationStatus.VERIFIED,
            contracts=(contract(),),
        )
        object.__setattr__(
            verified.contracts[0], "kind", QuantityKind.TEMPERATURE_DELTA
        )
        with self.assertRaisesRegex(VerificationError, "does not match"):
            verified.canonical_record()

        with self.assertRaises(FrozenInstanceError):
            verified.status = VerificationStatus.UNKNOWN  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
