"""Stable result contracts for dimensional verification."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .canonical import canonical_json_bytes, sha256_hex
from .domain import (
    MAX_UNIT_ID_LENGTH,
    UNIT_ID,
    Dimension,
    QuantityKind,
    UnitSentinelError,
)
from .registry import SHA256_HEX

MAX_SOLVER_TIMEOUT_MS: Final = 10_000
MAX_TOTAL_TIMEOUT_MS: Final = 60_000
MAX_SOLVER_MEMORY_MB: Final = 4_096
MAX_CORE_SHRINK_CHECKS: Final = 1_024
MAX_UNIQUENESS_CHECKS: Final = 1_024
MAX_CONSTRAINT_ID_LENGTH: Final = 192
CONSTRAINT_ID: Final = re.compile(r"^[a-z][a-z0-9]*(?:(?:-|/)[a-z0-9]+)*$")
SOLVER_VERSION: Final = re.compile(r"^[0-9]+(?:\.[0-9]+){2,3}$")


class VerificationError(UnitSentinelError):
    """Raised when a verifier configuration or result object is malformed."""


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNDERCONSTRAINED = "underconstrained"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class UnknownReason(StrEnum):
    CONTRACT_REJECTED = "contract-rejected"
    INTERNAL_INCONSISTENCY = "internal-inconsistency"
    MODEL_OUT_OF_DOMAIN = "model-out-of-domain"
    RESOURCE_LIMIT = "resource-limit"
    SOLVER_UNKNOWN = "solver-unknown"


class ConstraintSource(StrEnum):
    DECLARATION = "declaration"
    OPERATION = "operation"


def _require_exact_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise VerificationError(f"{label} must be an exact integer")
    return value


def _require_public_identifier(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_UNIT_ID_LENGTH
        or UNIT_ID.fullmatch(value) is None
    ):
        raise VerificationError(f"{label} is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class SolverLimits:
    """Explicit per-check and whole-verification resource limits."""

    per_check_timeout_ms: int = 250
    total_timeout_ms: int = 5_000
    max_memory_mb: int = 256
    max_core_shrink_checks: int = 64
    max_uniqueness_checks: int = 577

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not SolverLimits:
            raise VerificationError("solver limits must be an exact SolverLimits")
        per_check = _require_exact_int(
            self.per_check_timeout_ms,
            label="per-check timeout",
        )
        total = _require_exact_int(
            self.total_timeout_ms,
            label="total timeout",
        )
        memory = _require_exact_int(self.max_memory_mb, label="solver memory")
        core_checks = _require_exact_int(
            self.max_core_shrink_checks,
            label="core-shrink check limit",
        )
        uniqueness_checks = _require_exact_int(
            self.max_uniqueness_checks,
            label="uniqueness check limit",
        )
        if per_check < 1 or per_check > MAX_SOLVER_TIMEOUT_MS:
            raise VerificationError("per-check timeout is out of bounds")
        if total < per_check or total > MAX_TOTAL_TIMEOUT_MS:
            raise VerificationError("total timeout is out of bounds")
        if memory < 32 or memory > MAX_SOLVER_MEMORY_MB:
            raise VerificationError("solver memory limit is out of bounds")
        if core_checks < 0 or core_checks > MAX_CORE_SHRINK_CHECKS:
            raise VerificationError("core-shrink check limit is out of bounds")
        if uniqueness_checks < 1 or uniqueness_checks > MAX_UNIQUENESS_CHECKS:
            raise VerificationError("uniqueness check limit is out of bounds")

    def canonical_record(self) -> dict[str, int]:
        self.validate()
        return {
            "max_core_shrink_checks": self.max_core_shrink_checks,
            "max_memory_mb": self.max_memory_mb,
            "max_uniqueness_checks": self.max_uniqueness_checks,
            "per_check_timeout_ms": self.per_check_timeout_ms,
            "total_timeout_ms": self.total_timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class ConstraintWitness:
    """A stable public source label for one tracked assertion group."""

    constraint_id: str
    source: ConstraintSource
    source_id: str
    rule: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not ConstraintWitness:
            raise VerificationError(
                "constraint witness must be an exact ConstraintWitness"
            )
        if (
            type(self.constraint_id) is not str
            or len(self.constraint_id) > MAX_CONSTRAINT_ID_LENGTH
            or CONSTRAINT_ID.fullmatch(self.constraint_id) is None
        ):
            raise VerificationError("constraint identifier is not canonical")
        if type(self.source) is not ConstraintSource:
            raise VerificationError("constraint source is unknown")
        _require_public_identifier(self.source_id, label="constraint source identifier")
        _require_public_identifier(self.rule, label="constraint rule")

    def canonical_record(self) -> dict[str, str]:
        self.validate()
        return {
            "constraint_id": self.constraint_id,
            "rule": self.rule,
            "source": self.source.value,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class InferredContract:
    """One uniquely inferred dimension and semantic quantity kind."""

    value_id: str
    dimension: Dimension
    kind: QuantityKind

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not InferredContract:
            raise VerificationError(
                "inferred contract must be an exact InferredContract"
            )
        _require_public_identifier(self.value_id, label="inferred value identifier")
        if type(self.dimension) is not Dimension:
            raise VerificationError("inferred dimension must be an exact Dimension")
        self.dimension.validate()
        if type(self.kind) is not QuantityKind:
            raise VerificationError("inferred quantity kind is unknown")

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "dimension": [
                {"base": base, "exponent": exponent}
                for base, exponent in self.dimension.canonical_pairs()
            ],
            "kind": self.kind.value,
            "value_id": self.value_id,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """A content-addressed, outcome-specific verifier result."""

    status: VerificationStatus
    graph_digest: str
    registry_digest: str
    solver_version: str
    limits: SolverLimits
    checks_performed: int
    contracts: tuple[InferredContract, ...] = ()
    underconstrained_values: tuple[str, ...] = ()
    conflict_core: tuple[ConstraintWitness, ...] = ()
    core_minimal: bool | None = None
    unknown_reason: UnknownReason | None = None
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not VerificationResult:
            raise VerificationError(
                "verification result must be an exact VerificationResult"
            )
        if type(self.status) is not VerificationStatus:
            raise VerificationError("verification status is unknown")
        for label, digest in (
            ("graph digest", self.graph_digest),
            ("registry digest", self.registry_digest),
        ):
            if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
                raise VerificationError(f"{label} is malformed")
        if (
            type(self.solver_version) is not str
            or SOLVER_VERSION.fullmatch(self.solver_version) is None
        ):
            raise VerificationError("solver version is malformed")
        if type(self.limits) is not SolverLimits:
            raise VerificationError("result limits must be an exact SolverLimits")
        self.limits.validate()
        checks = _require_exact_int(self.checks_performed, label="solver check count")
        if checks < 0:
            raise VerificationError("solver check count cannot be negative")
        self._validate_collections()
        self._validate_outcome_shape()

    def _validate_collections(self) -> None:
        if type(self.contracts) is not tuple:
            raise VerificationError("inferred contracts must be a tuple")
        contract_ids: list[str] = []
        for contract in self.contracts:
            if type(contract) is not InferredContract:
                raise VerificationError(
                    "contracts must contain exact InferredContract values"
                )
            contract.validate()
            contract_ids.append(contract.value_id)
        if contract_ids != sorted(set(contract_ids)):
            raise VerificationError("inferred contracts must be sorted and unique")

        if type(self.underconstrained_values) is not tuple:
            raise VerificationError("underconstrained values must be a tuple")
        for value_id in self.underconstrained_values:
            _require_public_identifier(
                value_id,
                label="underconstrained value identifier",
            )
        if list(self.underconstrained_values) != sorted(
            set(self.underconstrained_values)
        ):
            raise VerificationError("underconstrained values must be sorted and unique")

        if type(self.conflict_core) is not tuple:
            raise VerificationError("conflict core must be a tuple")
        constraint_ids: set[str] = set()
        for witness in self.conflict_core:
            if type(witness) is not ConstraintWitness:
                raise VerificationError(
                    "conflict core must contain exact ConstraintWitness values"
                )
            witness.validate()
            if witness.constraint_id in constraint_ids:
                raise VerificationError("conflict core identifiers must be unique")
            constraint_ids.add(witness.constraint_id)

    def _validate_outcome_shape(self) -> None:
        if self.status is VerificationStatus.VERIFIED:
            if (
                not self.contracts
                or self.underconstrained_values
                or self.conflict_core
                or self.core_minimal is not None
                or self.unknown_reason is not None
            ):
                raise VerificationError("verified result fields are inconsistent")
            return
        if self.status is VerificationStatus.UNDERCONSTRAINED:
            if (
                not self.underconstrained_values
                or self.conflict_core
                or self.core_minimal is not None
                or self.unknown_reason is not None
            ):
                raise VerificationError(
                    "underconstrained result fields are inconsistent"
                )
            return
        if self.status is VerificationStatus.CONFLICT:
            if (
                self.contracts
                or self.underconstrained_values
                or not self.conflict_core
                or type(self.core_minimal) is not bool
                or self.unknown_reason is not None
            ):
                raise VerificationError("conflict result fields are inconsistent")
            return
        if (
            self.contracts
            or self.underconstrained_values
            or self.conflict_core
            or self.core_minimal is not None
            or type(self.unknown_reason) is not UnknownReason
        ):
            raise VerificationError("unknown result fields are inconsistent")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise VerificationError("verification result digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise VerificationError(
                "verification result digest does not match its contents"
            )

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "checks_performed": self.checks_performed,
            "conflict_core": [
                witness.canonical_record() for witness in self.conflict_core
            ],
            "contracts": [contract.canonical_record() for contract in self.contracts],
            "core_minimal": self.core_minimal,
            "graph_digest": self.graph_digest,
            "limits": self.limits.canonical_record(),
            "registry_digest": self.registry_digest,
            "solver_version": self.solver_version,
            "status": self.status.value,
            "underconstrained_values": list(self.underconstrained_values),
            "unknown_reason": (
                None if self.unknown_reason is None else self.unknown_reason.value
            ),
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self._canonical_record_unchecked())
