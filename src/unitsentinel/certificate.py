"""Detached positive proof certificates for verified computation graphs."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Final

from .canonical import canonical_json_bytes, sha256_hex
from .domain import UnitSentinelError
from .graph import (
    GRAPH_SCHEMA,
    MAX_GRAPH_NODES,
    MAX_GRAPH_VALUES,
    ComputationGraph,
)
from .registry import (
    BUILTIN_REGISTRY,
    MAX_REGISTRY_VERSION_LENGTH,
    REGISTRY_SCHEMA,
    REGISTRY_VERSION,
    SHA256_HEX,
    UnitRegistry,
)
from .verification import (
    ConstraintWitness,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .verifier import constraint_catalog, verify_graph
from .version import VERSION

CERTIFICATE_SCHEMA: Final = "unitsentinel.proof-certificate/v1"
VERIFIER_IMPLEMENTATION: Final = "unitsentinel"
VERIFIER_SEMANTICS: Final = "unitsentinel.verifier/v1"
SOLVER_IMPLEMENTATION: Final = "z3"
MAX_CERTIFICATE_CONSTRAINTS: Final = MAX_GRAPH_VALUES + 3 * MAX_GRAPH_NODES
MAX_CERTIFICATE_CONTRACTS: Final = MAX_GRAPH_VALUES
_DEFAULT_SOLVER_LIMITS: Final = SolverLimits()


class CertificateError(UnitSentinelError):
    """Raised when a proof certificate cannot be created or trusted."""


def _require_semver(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_REGISTRY_VERSION_LENGTH
        or REGISTRY_VERSION.fullmatch(value) is None
    ):
        raise CertificateError(f"{label} must be canonical SemVer")
    return value


@dataclass(frozen=True, slots=True)
class ProofCertificate:
    """A deterministic detached record of one positive verification claim.

    The record is content-addressed, not signed. Direct construction therefore
    produces an untrusted claim; callers should use :func:`create_certificate`
    for issuance and replay the claim against its bound graph and registry
    before relying on it.
    """

    registry_version: str
    verifier_version: str
    constraints: tuple[ConstraintWitness, ...]
    result: VerificationResult
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not ProofCertificate:
            raise CertificateError(
                "proof certificate must be an exact ProofCertificate"
            )
        _require_semver(self.registry_version, label="certificate registry version")
        _require_semver(self.verifier_version, label="certificate verifier version")
        if type(self.constraints) is not tuple:
            raise CertificateError("certificate constraints must be a tuple")
        if not self.constraints:
            raise CertificateError("certificate constraint manifest cannot be empty")
        if len(self.constraints) > MAX_CERTIFICATE_CONSTRAINTS:
            raise CertificateError("certificate constraint manifest exceeds the limit")
        constraint_ids: list[str] = []
        for witness in self.constraints:
            if type(witness) is not ConstraintWitness:
                raise CertificateError(
                    "certificate constraints must contain exact witnesses"
                )
            try:
                witness.validate()
            except UnitSentinelError:
                raise CertificateError(
                    "certificate contains an invalid constraint witness"
                ) from None
            constraint_ids.append(witness.constraint_id)
        if constraint_ids != sorted(set(constraint_ids)):
            raise CertificateError("certificate constraints must be sorted and unique")
        if type(self.result) is not VerificationResult:
            raise CertificateError(
                "certificate result must be an exact VerificationResult"
            )
        try:
            self.result.validate()
        except UnitSentinelError:
            raise CertificateError(
                "certificate contains an invalid verification result"
            ) from None
        if self.result.status is not VerificationStatus.VERIFIED:
            raise CertificateError("proof certificates require a verified result")
        if len(self.result.contracts) > MAX_CERTIFICATE_CONTRACTS:
            raise CertificateError("certificate contracts exceed the limit")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise CertificateError("proof certificate digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise CertificateError(
                "proof certificate digest does not match its contents"
            )

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "graph": {
                "schema": GRAPH_SCHEMA,
                "sha256": self.result.graph_digest,
            },
            "proof": {
                "constraints": [
                    witness.canonical_record() for witness in self.constraints
                ],
                "contracts": [
                    contract.canonical_record() for contract in self.result.contracts
                ],
                "outcome": VerificationStatus.VERIFIED.value,
                "verification_result_sha256": self.result.digest,
            },
            "registry": {
                "schema": REGISTRY_SCHEMA,
                "sha256": self.result.registry_digest,
                "version": self.registry_version,
            },
            "run": {
                "checks_performed": self.result.checks_performed,
                "limits": self.result.limits.canonical_record(),
            },
            "schema": CERTIFICATE_SCHEMA,
            "solver": {
                "implementation": SOLVER_IMPLEMENTATION,
                "version": self.result.solver_version,
            },
            "verifier": {
                "implementation": VERIFIER_IMPLEMENTATION,
                "semantics": VERIFIER_SEMANTICS,
                "version": self.verifier_version,
            },
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


def create_certificate(
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    limits: SolverLimits = _DEFAULT_SOLVER_LIMITS,
) -> ProofCertificate:
    """Run verification and issue a detached certificate only on success."""

    if type(graph) is not ComputationGraph:
        raise CertificateError("certificate graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise CertificateError("certificate registry must be an exact UnitRegistry")
    if type(limits) is not SolverLimits:
        raise CertificateError("certificate limits must be an exact SolverLimits")
    try:
        graph_digest = graph.digest
        registry_digest = registry.digest
        registry_version = registry.version
        limits_record = limits.canonical_record()
        expected_ids = tuple(value.value_id for value in graph.values)
        constraints = constraint_catalog(graph, registry)
    except UnitSentinelError:
        raise CertificateError(
            "certificate source contract is rejected or mutated"
        ) from None

    try:
        result = verify_graph(graph, registry=registry, limits=limits)
    except UnitSentinelError:
        raise CertificateError("certificate verification failed") from None
    try:
        graph.validate()
        registry.validate()
        limits.validate()
        result.validate()
    except UnitSentinelError:
        raise CertificateError(
            "certificate source changed during verification"
        ) from None
    if (
        graph.digest != graph_digest
        or registry.digest != registry_digest
        or registry.version != registry_version
        or limits.canonical_record() != limits_record
        or result.graph_digest != graph_digest
        or result.registry_digest != registry_digest
        or result.limits.canonical_record() != limits_record
    ):
        raise CertificateError("certificate source changed during verification")
    if result.status is not VerificationStatus.VERIFIED:
        raise CertificateError(
            f"certificate issuance requires verified, got {result.status.value}"
        )
    actual_ids = tuple(contract.value_id for contract in result.contracts)
    if actual_ids != expected_ids:
        raise CertificateError("verified contracts do not cover every graph value")
    try:
        return ProofCertificate(
            registry_version=registry_version,
            verifier_version=VERSION,
            constraints=constraints,
            result=result,
        )
    except UnitSentinelError:
        raise CertificateError("verified result could not be certified") from None


def encode_certificate(certificate: ProofCertificate) -> bytes:
    """Return exact canonical bytes for one currently valid certificate."""

    if type(certificate) is not ProofCertificate:
        raise CertificateError("certificate encoder requires an exact ProofCertificate")
    try:
        return certificate.canonical_bytes()
    except UnitSentinelError:
        raise CertificateError("certificate encoding failed") from None
