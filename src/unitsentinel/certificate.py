"""Detached positive proof certificates for verified computation graphs."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final, cast

from .canonical import canonical_json_bytes, sha256_hex
from .domain import (
    BaseDimension,
    Dimension,
    QuantityKind,
    UnitSentinelError,
    _fraction_text,
)
from .graph import (
    GRAPH_SCHEMA,
    MAX_GRAPH_NODES,
    MAX_GRAPH_VALUES,
    ComputationGraph,
)
from .json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
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
    MAX_CORE_SHRINK_CHECKS,
    MAX_UNIQUENESS_CHECKS,
    ConstraintSource,
    ConstraintWitness,
    InferredContract,
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
MAX_CERTIFICATE_BYTES: Final = 2_097_152
MAX_CERTIFICATE_JSON_DEPTH: Final = 8
MAX_CERTIFICATE_JSON_VALUES: Final = 65_536
MAX_CERTIFICATE_STRING_LENGTH: Final = 192
MAX_CERTIFICATE_INTEGER_DIGITS: Final = 10
MAX_CERTIFICATE_SOLVER_VERSION_LENGTH: Final = 32
MAX_CERTIFICATE_CHECKS: Final = 1 + max(
    MAX_CORE_SHRINK_CHECKS,
    MAX_UNIQUENESS_CHECKS,
)
_DEFAULT_SOLVER_LIMITS: Final = SolverLimits()
_CERTIFICATE_JSON_LIMITS: Final = CanonicalJSONLimits(
    max_bytes=MAX_CERTIFICATE_BYTES,
    max_depth=MAX_CERTIFICATE_JSON_DEPTH,
    max_container_items=MAX_CERTIFICATE_CONSTRAINTS,
    max_total_values=MAX_CERTIFICATE_JSON_VALUES,
    max_string_length=MAX_CERTIFICATE_STRING_LENGTH,
    max_integer_digits=MAX_CERTIFICATE_INTEGER_DIGITS,
)
_RATIONAL_TEXT: Final = re.compile(r"^(?:0|-?[1-9][0-9]*)(?:/[1-9][0-9]*)?$")
_ROOT_FIELDS: Final = frozenset(
    {"graph", "proof", "registry", "run", "schema", "solver", "verifier"}
)
_GRAPH_FIELDS: Final = frozenset({"schema", "sha256"})
_PROOF_FIELDS: Final = frozenset(
    {
        "constraints",
        "contracts",
        "outcome",
        "verification_result_sha256",
    }
)
_REGISTRY_FIELDS: Final = frozenset({"schema", "sha256", "version"})
_RUN_FIELDS: Final = frozenset({"checks_performed", "limits"})
_SOLVER_FIELDS: Final = frozenset({"implementation", "version"})
_VERIFIER_FIELDS: Final = frozenset({"implementation", "semantics", "version"})
_LIMIT_FIELDS: Final = frozenset(
    {
        "max_core_shrink_checks",
        "max_memory_mb",
        "max_uniqueness_checks",
        "per_check_timeout_ms",
        "total_timeout_ms",
    }
)
_CONSTRAINT_FIELDS: Final = frozenset({"constraint_id", "rule", "source", "source_id"})
_CONTRACT_FIELDS: Final = frozenset(
    {"dimension", "kind", "offset", "scale", "value_id"}
)
_DIMENSION_TERM_FIELDS: Final = frozenset({"base", "exponent"})


class CertificateError(UnitSentinelError):
    """Raised when a proof certificate cannot be created or trusted."""


class CertificateDecodeError(CertificateError):
    """Raised when bytes do not encode one exact bounded certificate claim."""


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
        if len(self.result.solver_version) > MAX_CERTIFICATE_SOLVER_VERSION_LENGTH:
            raise CertificateError("certificate solver version exceeds the limit")
        if self.result.checks_performed > MAX_CERTIFICATE_CHECKS:
            raise CertificateError("certificate solver check count exceeds the limit")

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


def _create_certificate_attempt(
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    limits: SolverLimits = _DEFAULT_SOLVER_LIMITS,
) -> tuple[VerificationResult, ProofCertificate | None]:
    """Verify exactly once and attach a certificate only to a positive result."""

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
    if type(result) is not VerificationResult:
        raise CertificateError("certificate verification returned an invalid result")
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
        return result, None
    actual_ids = tuple(contract.value_id for contract in result.contracts)
    if actual_ids != expected_ids:
        raise CertificateError("verified contracts do not cover every graph value")
    try:
        certificate = ProofCertificate(
            registry_version=registry_version,
            verifier_version=VERSION,
            constraints=constraints,
            result=result,
        )
    except UnitSentinelError:
        raise CertificateError("verified result could not be certified") from None
    try:
        graph.validate()
        registry.validate()
        limits.validate()
        certificate.validate()
        sources_unchanged = (
            graph.digest == graph_digest
            and registry.digest == registry_digest
            and registry.version == registry_version
            and limits.canonical_record() == limits_record
            and certificate.result is result
            and result.graph_digest == graph_digest
            and result.registry_digest == registry_digest
            and result.limits.canonical_record() == limits_record
        )
    except UnitSentinelError:
        raise CertificateError(
            "certificate source changed during certification"
        ) from None
    if not sources_unchanged:
        raise CertificateError("certificate source changed during certification")
    return result, certificate


def create_certificate(
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    limits: SolverLimits = _DEFAULT_SOLVER_LIMITS,
) -> ProofCertificate:
    """Run verification and issue a detached certificate only on success."""

    result, certificate = _create_certificate_attempt(graph, registry, limits)
    if certificate is None:
        raise CertificateError(
            f"certificate issuance requires verified, got {result.status.value}"
        )
    return certificate


def encode_certificate(certificate: ProofCertificate) -> bytes:
    """Return exact canonical bytes for one currently valid certificate."""

    if type(certificate) is not ProofCertificate:
        raise CertificateError("certificate encoder requires an exact ProofCertificate")
    try:
        return certificate.canonical_bytes()
    except UnitSentinelError:
        raise CertificateError("certificate encoding failed") from None


def decode_certificate(payload: bytes) -> ProofCertificate:
    """Decode one well-formed unsigned v1 claim from untrusted JSON bytes.

    Successful decoding establishes structural integrity only. It does not
    authenticate the issuer or reproduce the certificate's semantic claim.
    """

    try:
        parsed = decode_canonical_json(
            payload,
            limits=_CERTIFICATE_JSON_LIMITS,
            label="certificate",
        )
    except CanonicalJSONError as error:
        raise CertificateDecodeError(str(error)) from None

    try:
        certificate, claimed_result_digest = _decode_certificate_record(parsed)
    except UnitSentinelError as error:
        raise CertificateDecodeError(
            f"certificate semantic contract failed: {error}"
        ) from None
    if not hmac.compare_digest(
        claimed_result_digest,
        certificate.result.digest,
    ):
        raise CertificateDecodeError(
            "certificate verification-result digest does not match its contents"
        )
    if certificate.canonical_bytes() != payload:
        raise CertificateDecodeError(
            "certificate payload does not match the canonical certificate model"
        )
    return certificate


def _decode_certificate_record(
    value: object,
) -> tuple[ProofCertificate, str]:
    root = _expect_object(value, _ROOT_FIELDS, label="certificate document")
    _expect_literal(root["schema"], CERTIFICATE_SCHEMA, label="certificate schema")

    graph = _expect_object(root["graph"], _GRAPH_FIELDS, label="graph binding")
    _expect_literal(graph["schema"], GRAPH_SCHEMA, label="graph schema")
    graph_digest = _expect_sha256(graph["sha256"], label="graph digest")

    registry = _expect_object(
        root["registry"],
        _REGISTRY_FIELDS,
        label="registry binding",
    )
    _expect_literal(registry["schema"], REGISTRY_SCHEMA, label="registry schema")
    registry_digest = _expect_sha256(
        registry["sha256"],
        label="registry digest",
    )
    registry_version = _expect_text(
        registry["version"],
        label="registry version",
    )

    solver = _expect_object(root["solver"], _SOLVER_FIELDS, label="solver identity")
    _expect_literal(
        solver["implementation"],
        SOLVER_IMPLEMENTATION,
        label="solver implementation",
    )
    solver_version = _expect_text(solver["version"], label="solver version")

    verifier = _expect_object(
        root["verifier"],
        _VERIFIER_FIELDS,
        label="verifier identity",
    )
    _expect_literal(
        verifier["implementation"],
        VERIFIER_IMPLEMENTATION,
        label="verifier implementation",
    )
    _expect_literal(
        verifier["semantics"],
        VERIFIER_SEMANTICS,
        label="verifier semantics",
    )
    verifier_version = _expect_text(
        verifier["version"],
        label="verifier version",
    )

    run = _expect_object(root["run"], _RUN_FIELDS, label="verification run")
    limits = _decode_limits(run["limits"])
    checks_performed = _expect_integer(
        run["checks_performed"],
        label="solver check count",
    )

    proof = _expect_object(root["proof"], _PROOF_FIELDS, label="proof claim")
    _expect_literal(
        proof["outcome"],
        VerificationStatus.VERIFIED.value,
        label="proof outcome",
    )
    claimed_result_digest = _expect_sha256(
        proof["verification_result_sha256"],
        label="verification-result digest",
    )
    constraints = tuple(
        _decode_constraint(item)
        for item in _expect_array(
            proof["constraints"],
            label="proof constraints",
        )
    )
    contracts = tuple(
        _decode_contract(item)
        for item in _expect_array(
            proof["contracts"],
            label="proof contracts",
        )
    )
    result = VerificationResult(
        status=VerificationStatus.VERIFIED,
        graph_digest=graph_digest,
        registry_digest=registry_digest,
        solver_version=solver_version,
        limits=limits,
        checks_performed=checks_performed,
        contracts=contracts,
    )
    return (
        ProofCertificate(
            registry_version=registry_version,
            verifier_version=verifier_version,
            constraints=constraints,
            result=result,
        ),
        claimed_result_digest,
    )


def _decode_limits(value: object) -> SolverLimits:
    record = _expect_object(value, _LIMIT_FIELDS, label="solver limits")
    return SolverLimits(
        per_check_timeout_ms=_expect_integer(
            record["per_check_timeout_ms"],
            label="per-check timeout",
        ),
        total_timeout_ms=_expect_integer(
            record["total_timeout_ms"],
            label="total timeout",
        ),
        max_memory_mb=_expect_integer(
            record["max_memory_mb"],
            label="solver memory",
        ),
        max_core_shrink_checks=_expect_integer(
            record["max_core_shrink_checks"],
            label="core-shrink check limit",
        ),
        max_uniqueness_checks=_expect_integer(
            record["max_uniqueness_checks"],
            label="uniqueness check limit",
        ),
    )


def _decode_constraint(value: object) -> ConstraintWitness:
    record = _expect_object(
        value,
        _CONSTRAINT_FIELDS,
        label="constraint witness",
    )
    source_text = _expect_text(
        record["source"],
        label="constraint source",
    )
    try:
        source = ConstraintSource(source_text)
    except ValueError:
        raise CertificateError("constraint source is not supported") from None
    return ConstraintWitness(
        constraint_id=_expect_text(
            record["constraint_id"],
            label="constraint identifier",
        ),
        source=source,
        source_id=_expect_text(
            record["source_id"],
            label="constraint source identifier",
        ),
        rule=_expect_text(record["rule"], label="constraint rule"),
    )


def _decode_contract(value: object) -> InferredContract:
    record = _expect_object(value, _CONTRACT_FIELDS, label="inferred contract")
    kind_text = _expect_text(record["kind"], label="inferred quantity kind")
    try:
        kind = QuantityKind(kind_text)
    except ValueError:
        raise CertificateError("inferred quantity kind is not supported") from None
    return InferredContract(
        value_id=_expect_text(
            record["value_id"],
            label="inferred value identifier",
        ),
        dimension=_decode_dimension(record["dimension"]),
        kind=kind,
        scale=_decode_rational(record["scale"], label="inferred scale"),
        offset=_decode_rational(record["offset"], label="inferred offset"),
    )


def _decode_dimension(value: object) -> Dimension:
    terms = _expect_array(value, label="inferred dimension")
    exponents: dict[BaseDimension, Fraction] = {}
    for item in terms:
        record = _expect_object(
            item,
            _DIMENSION_TERM_FIELDS,
            label="dimension term",
        )
        base_text = _expect_text(record["base"], label="dimension base")
        try:
            base = BaseDimension(base_text)
        except ValueError:
            raise CertificateError("dimension base is not supported") from None
        if base in exponents:
            raise CertificateError("inferred dimension contains a duplicate base")
        exponents[base] = _decode_rational(
            record["exponent"],
            label="dimension exponent",
        )
    return Dimension.from_mapping(exponents)


def _decode_rational(value: object, *, label: str) -> Fraction:
    text = _expect_text(value, label=label)
    if _RATIONAL_TEXT.fullmatch(text) is None:
        raise CertificateError(f"{label} is not a canonical rational")
    result = Fraction(text)
    if _fraction_text(result) != text:
        raise CertificateError(f"{label} is not reduced")
    return result


def _expect_object(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise CertificateError(f"{label} must be an object")
    record = cast(dict[str, object], value)
    if set(record) != fields:
        raise CertificateError(f"{label} has missing or unknown fields")
    return record


def _expect_array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise CertificateError(f"{label} must be an array")
    return cast(list[object], value)


def _expect_text(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise CertificateError(f"{label} must be text")
    return value


def _expect_integer(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise CertificateError(f"{label} must be an exact integer")
    return value


def _expect_sha256(value: object, *, label: str) -> str:
    digest = _expect_text(value, label=label)
    if SHA256_HEX.fullmatch(digest) is None:
        raise CertificateError(f"{label} is malformed")
    return digest


def _expect_literal(value: object, expected: str, *, label: str) -> None:
    text = _expect_text(value, label=label)
    if text != expected:
        raise CertificateError(f"{label} is not supported")
