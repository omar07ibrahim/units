"""Deterministic semantic replay for detached proof-certificate claims."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import z3  # type: ignore[import-untyped]

from .canonical import canonical_json_bytes, sha256_hex
from .certificate import (
    MAX_CERTIFICATE_SOLVER_VERSION_LENGTH,
    CertificateError,
    ProofCertificate,
)
from .domain import UnitSentinelError
from .graph import ComputationGraph
from .registry import (
    BUILTIN_REGISTRY,
    MAX_REGISTRY_VERSION_LENGTH,
    REGISTRY_VERSION,
    SHA256_HEX,
    UnitRegistry,
)
from .verification import (
    SOLVER_VERSION,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .verifier import (
    _replay_claimed_contracts,
    constraint_catalog,
    verify_graph,
)
from .version import VERSION

CERTIFICATE_REPLAY_SCHEMA: Final = "unitsentinel.certificate-replay/v1"
_DEFAULT_REPLAY_LIMITS: Final = SolverLimits()


class CertificateReplayError(CertificateError):
    """Raised when replay inputs or the fresh verifier cannot be trusted."""


class ReplayStatus(StrEnum):
    """Stable high-level outcome of current semantic reproduction."""

    REPRODUCED = "reproduced"
    MISMATCH = "mismatch"
    INDETERMINATE = "indeterminate"


class ReplayReason(StrEnum):
    """Deterministic first reason why a claim was not reproduced."""

    GRAPH_DIGEST_MISMATCH = "graph-digest-mismatch"
    REGISTRY_DIGEST_MISMATCH = "registry-digest-mismatch"
    REGISTRY_VERSION_MISMATCH = "registry-version-mismatch"
    CONSTRAINT_CATALOG_MISMATCH = "constraint-catalog-mismatch"
    CONTRACT_COVERAGE_MISMATCH = "contract-coverage-mismatch"
    CONTRACT_WITNESS_MISMATCH = "contract-witness-mismatch"
    TOOLCHAIN_MISMATCH = "toolchain-mismatch"
    FRESH_CONFLICT = "fresh-conflict"
    FRESH_UNDERCONSTRAINED = "fresh-underconstrained"
    FRESH_CONTRACT_MISMATCH = "fresh-contract-mismatch"
    FRESH_UNKNOWN = "fresh-unknown"


_EARLY_MISMATCH_REASONS: Final = frozenset(
    {
        ReplayReason.GRAPH_DIGEST_MISMATCH,
        ReplayReason.REGISTRY_DIGEST_MISMATCH,
        ReplayReason.REGISTRY_VERSION_MISMATCH,
        ReplayReason.CONSTRAINT_CATALOG_MISMATCH,
        ReplayReason.CONTRACT_COVERAGE_MISMATCH,
        ReplayReason.CONTRACT_WITNESS_MISMATCH,
        ReplayReason.TOOLCHAIN_MISMATCH,
    }
)
_FRESH_MISMATCH_STATUS: Final = {
    ReplayReason.FRESH_CONFLICT: VerificationStatus.CONFLICT,
    ReplayReason.FRESH_UNDERCONSTRAINED: VerificationStatus.UNDERCONSTRAINED,
    ReplayReason.FRESH_CONTRACT_MISMATCH: VerificationStatus.VERIFIED,
}


def _require_semver(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_REGISTRY_VERSION_LENGTH
        or REGISTRY_VERSION.fullmatch(value) is None
    ):
        raise CertificateReplayError(f"{label} must be canonical SemVer")
    return value


def _require_solver_version(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_CERTIFICATE_SOLVER_VERSION_LENGTH
        or SOLVER_VERSION.fullmatch(value) is None
    ):
        raise CertificateReplayError(f"{label} is malformed")
    return value


@dataclass(frozen=True, slots=True)
class CertificateReplay:
    """A content-addressed report of one current reproduction attempt.

    The digest provides integrity, not authentication. Direct construction is
    an unsigned claim; use :func:`replay_certificate` to perform the pure and
    fresh-verifier checks represented by this report.
    """

    status: ReplayStatus
    reason: ReplayReason | None
    certificate_digest: str
    graph_digest: str
    registry_digest: str
    registry_version: str
    strict_toolchain: bool
    certificate_verifier_version: str
    certificate_solver_version: str
    current_verifier_version: str
    current_solver_version: str
    toolchain_match: bool
    fresh_result: VerificationResult | None
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not CertificateReplay:
            raise CertificateReplayError(
                "replay report must be an exact CertificateReplay"
            )
        if type(self.status) is not ReplayStatus:
            raise CertificateReplayError("replay status is unknown")
        if self.reason is not None and type(self.reason) is not ReplayReason:
            raise CertificateReplayError("replay reason is unknown")
        for label, digest in (
            ("replay certificate digest", self.certificate_digest),
            ("replay graph digest", self.graph_digest),
            ("replay registry digest", self.registry_digest),
        ):
            if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
                raise CertificateReplayError(f"{label} is malformed")
        _require_semver(self.registry_version, label="replay registry version")
        _require_semver(
            self.certificate_verifier_version,
            label="certificate verifier version",
        )
        _require_semver(
            self.current_verifier_version,
            label="current verifier version",
        )
        _require_solver_version(
            self.certificate_solver_version,
            label="certificate solver version",
        )
        _require_solver_version(
            self.current_solver_version,
            label="current solver version",
        )
        if type(self.strict_toolchain) is not bool:
            raise CertificateReplayError(
                "strict-toolchain flag must be an exact boolean"
            )
        if type(self.toolchain_match) is not bool:
            raise CertificateReplayError(
                "toolchain match flag must be an exact boolean"
            )
        expected_match = (
            self.certificate_verifier_version == self.current_verifier_version
            and self.certificate_solver_version == self.current_solver_version
        )
        if self.toolchain_match is not expected_match:
            raise CertificateReplayError("toolchain match flag is inconsistent")

        self._validate_fresh_result()
        self._validate_outcome()

    def _validate_fresh_result(self) -> None:
        if self.fresh_result is None:
            return
        if type(self.fresh_result) is not VerificationResult:
            raise CertificateReplayError(
                "fresh result must be an exact VerificationResult or null"
            )
        try:
            self.fresh_result.validate()
        except UnitSentinelError:
            raise CertificateReplayError(
                "fresh verification result is malformed or mutated"
            ) from None
        if (
            self.fresh_result.graph_digest != self.graph_digest
            or self.fresh_result.registry_digest != self.registry_digest
        ):
            raise CertificateReplayError(
                "fresh verification result has inconsistent source bindings"
            )
        if self.fresh_result.solver_version != self.current_solver_version:
            raise CertificateReplayError(
                "fresh verification result has inconsistent toolchain identity"
            )

    def _validate_outcome(self) -> None:
        strict_mismatch = self.strict_toolchain and not self.toolchain_match
        if strict_mismatch and (
            self.fresh_result is not None
            or self.reason is ReplayReason.CONTRACT_WITNESS_MISMATCH
        ):
            raise CertificateReplayError(
                "strict-toolchain replay fields are inconsistent"
            )
        if self.status is ReplayStatus.REPRODUCED:
            if (
                self.reason is not None
                or self.fresh_result is None
                or self.fresh_result.status is not VerificationStatus.VERIFIED
                or strict_mismatch
            ):
                raise CertificateReplayError(
                    "reproduced replay fields are inconsistent"
                )
            return
        if self.status is ReplayStatus.INDETERMINATE:
            if (
                self.reason is not ReplayReason.FRESH_UNKNOWN
                or self.fresh_result is None
                or self.fresh_result.status is not VerificationStatus.UNKNOWN
            ):
                raise CertificateReplayError(
                    "indeterminate replay fields are inconsistent"
                )
            return
        if self.reason in _EARLY_MISMATCH_REASONS:
            if self.fresh_result is not None:
                raise CertificateReplayError(
                    "early mismatch cannot contain a fresh result"
                )
            if self.reason is ReplayReason.TOOLCHAIN_MISMATCH and (
                not self.strict_toolchain or self.toolchain_match
            ):
                raise CertificateReplayError(
                    "toolchain mismatch fields are inconsistent"
                )
            return
        if self.reason is None:
            raise CertificateReplayError("mismatch replay fields are inconsistent")
        expected_status = _FRESH_MISMATCH_STATUS.get(self.reason)
        if (
            expected_status is None
            or self.fresh_result is None
            or self.fresh_result.status is not expected_status
        ):
            raise CertificateReplayError("mismatch replay fields are inconsistent")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise CertificateReplayError("replay report digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise CertificateReplayError(
                "replay report digest does not match its contents"
            )

    def _canonical_record_unchecked(self) -> dict[str, object]:
        fresh_result: dict[str, object] | None = None
        if self.fresh_result is not None:
            fresh_result = {
                "record": self.fresh_result.canonical_record(),
                "sha256": self.fresh_result.digest,
            }
        return {
            "certificate_sha256": self.certificate_digest,
            "fresh_result": fresh_result,
            "graph_sha256": self.graph_digest,
            "reason": None if self.reason is None else self.reason.value,
            "registry": {
                "sha256": self.registry_digest,
                "version": self.registry_version,
            },
            "schema": CERTIFICATE_REPLAY_SCHEMA,
            "status": self.status.value,
            "strict_toolchain": self.strict_toolchain,
            "toolchain": {
                "certificate": {
                    "solver_version": self.certificate_solver_version,
                    "verifier_version": self.certificate_verifier_version,
                },
                "current": {
                    "solver_version": self.current_solver_version,
                    "verifier_version": self.current_verifier_version,
                },
                "match": self.toolchain_match,
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


@dataclass(frozen=True, slots=True)
class _ReplayPins:
    certificate_digest: str
    graph_digest: str
    registry_digest: str
    registry_version: str
    limits_bytes: bytes
    certificate_verifier_version: str
    certificate_solver_version: str
    current_verifier_version: str
    current_solver_version: str
    toolchain_match: bool


def replay_certificate(
    certificate: ProofCertificate,
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    *,
    limits: SolverLimits = _DEFAULT_REPLAY_LIMITS,
    strict_toolchain: bool = False,
) -> CertificateReplay:
    """Reproduce one unsigned claim against exact current source contracts."""

    certificate, graph, registry, limits = _validate_replay_inputs(
        certificate,
        graph,
        registry,
        limits,
        strict_toolchain,
    )
    pins = _pin_replay_inputs(certificate, graph, registry, limits)

    def finish(
        status: ReplayStatus,
        reason: ReplayReason | None,
        fresh_result: VerificationResult | None = None,
    ) -> CertificateReplay:
        _require_replay_inputs_unchanged(
            certificate,
            graph,
            registry,
            limits,
            pins,
        )
        return _replay_outcome(
            pins,
            status=status,
            reason=reason,
            strict_toolchain=strict_toolchain,
            fresh_result=fresh_result,
        )

    if not hmac.compare_digest(
        certificate.result.graph_digest,
        pins.graph_digest,
    ):
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.GRAPH_DIGEST_MISMATCH,
        )
    if not hmac.compare_digest(
        certificate.result.registry_digest,
        pins.registry_digest,
    ):
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.REGISTRY_DIGEST_MISMATCH,
        )
    if certificate.registry_version != pins.registry_version:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.REGISTRY_VERSION_MISMATCH,
        )

    try:
        catalog = constraint_catalog(graph, registry)
    except UnitSentinelError:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.CONSTRAINT_CATALOG_MISMATCH,
        )
    except Exception:
        _require_replay_inputs_unchanged(
            certificate,
            graph,
            registry,
            limits,
            pins,
        )
        raise CertificateReplayError("certificate constraint catalog failed") from None
    _require_replay_inputs_unchanged(
        certificate,
        graph,
        registry,
        limits,
        pins,
    )
    if certificate.constraints != catalog:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.CONSTRAINT_CATALOG_MISMATCH,
        )

    contract_ids = tuple(contract.value_id for contract in certificate.result.contracts)
    expected_ids = tuple(value.value_id for value in graph.values)
    if contract_ids != expected_ids:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.CONTRACT_COVERAGE_MISMATCH,
        )
    if strict_toolchain and not pins.toolchain_match:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.TOOLCHAIN_MISMATCH,
        )

    try:
        witness_matches = _replay_claimed_contracts(
            graph,
            registry,
            certificate.result.contracts,
        )
    except UnitSentinelError:
        _require_replay_inputs_unchanged(
            certificate,
            graph,
            registry,
            limits,
            pins,
        )
        witness_matches = False
    except Exception:
        _require_replay_inputs_unchanged(
            certificate,
            graph,
            registry,
            limits,
            pins,
        )
        raise CertificateReplayError("certificate contract replay failed") from None
    _require_replay_inputs_unchanged(
        certificate,
        graph,
        registry,
        limits,
        pins,
    )
    if not witness_matches:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.CONTRACT_WITNESS_MISMATCH,
        )

    try:
        fresh_result = verify_graph(graph, registry=registry, limits=limits)
    except Exception:
        _require_replay_inputs_unchanged(
            certificate,
            graph,
            registry,
            limits,
            pins,
        )
        raise CertificateReplayError("fresh certificate verification failed") from None
    _require_replay_inputs_unchanged(
        certificate,
        graph,
        registry,
        limits,
        pins,
    )
    _validate_fresh_result(fresh_result, limits=limits, pins=pins)

    if fresh_result.status is VerificationStatus.UNKNOWN:
        return finish(
            ReplayStatus.INDETERMINATE,
            ReplayReason.FRESH_UNKNOWN,
            fresh_result,
        )
    if fresh_result.status is VerificationStatus.CONFLICT:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.FRESH_CONFLICT,
            fresh_result,
        )
    if fresh_result.status is VerificationStatus.UNDERCONSTRAINED:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.FRESH_UNDERCONSTRAINED,
            fresh_result,
        )
    if fresh_result.contracts != certificate.result.contracts:
        return finish(
            ReplayStatus.MISMATCH,
            ReplayReason.FRESH_CONTRACT_MISMATCH,
            fresh_result,
        )
    return finish(ReplayStatus.REPRODUCED, None, fresh_result)


def _validate_replay_inputs(
    certificate: object,
    graph: object,
    registry: object,
    limits: object,
    strict_toolchain: object,
) -> tuple[ProofCertificate, ComputationGraph, UnitRegistry, SolverLimits]:
    if type(certificate) is not ProofCertificate:
        raise CertificateReplayError("replay requires an exact ProofCertificate")
    if type(graph) is not ComputationGraph:
        raise CertificateReplayError("replay graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise CertificateReplayError("replay registry must be an exact UnitRegistry")
    if type(limits) is not SolverLimits:
        raise CertificateReplayError("replay limits must be an exact SolverLimits")
    if type(strict_toolchain) is not bool:
        raise CertificateReplayError("strict-toolchain flag must be an exact boolean")
    try:
        certificate.validate()
        graph.validate()
        registry.validate()
        limits.validate()
    except UnitSentinelError:
        raise CertificateReplayError("replay inputs are malformed or mutated") from None
    return certificate, graph, registry, limits


def _pin_replay_inputs(
    certificate: ProofCertificate,
    graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
) -> _ReplayPins:
    try:
        current_solver_version = z3.get_version_string()
        _require_solver_version(
            current_solver_version,
            label="current solver version",
        )
        return _ReplayPins(
            certificate_digest=certificate.digest,
            graph_digest=graph.digest,
            registry_digest=registry.digest,
            registry_version=registry.version,
            limits_bytes=canonical_json_bytes(limits.canonical_record()),
            certificate_verifier_version=certificate.verifier_version,
            certificate_solver_version=certificate.result.solver_version,
            current_verifier_version=VERSION,
            current_solver_version=current_solver_version,
            toolchain_match=(
                certificate.verifier_version == VERSION
                and certificate.result.solver_version == current_solver_version
            ),
        )
    except Exception:
        raise CertificateReplayError("replay inputs could not be pinned") from None


def _require_replay_inputs_unchanged(
    certificate: ProofCertificate,
    graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    pins: _ReplayPins,
) -> None:
    try:
        certificate.validate()
        graph.validate()
        registry.validate()
        limits.validate()
        unchanged = (
            certificate.digest == pins.certificate_digest
            and graph.digest == pins.graph_digest
            and registry.digest == pins.registry_digest
            and registry.version == pins.registry_version
            and canonical_json_bytes(limits.canonical_record()) == pins.limits_bytes
        )
    except Exception:
        raise CertificateReplayError("replay inputs changed during replay") from None
    if not unchanged:
        raise CertificateReplayError("replay inputs changed during replay")


def _validate_fresh_result(
    result: object,
    *,
    limits: SolverLimits,
    pins: _ReplayPins,
) -> VerificationResult:
    if type(result) is not VerificationResult:
        raise CertificateReplayError("fresh verifier returned an invalid result type")
    try:
        result.validate()
    except Exception:
        raise CertificateReplayError(
            "fresh verifier returned a malformed result"
        ) from None
    if (
        result.graph_digest != pins.graph_digest
        or result.registry_digest != pins.registry_digest
        or result.solver_version != pins.current_solver_version
        or canonical_json_bytes(result.limits.canonical_record())
        != canonical_json_bytes(limits.canonical_record())
    ):
        raise CertificateReplayError("fresh verifier returned an inconsistent result")
    return result


def _replay_outcome(
    pins: _ReplayPins,
    *,
    status: ReplayStatus,
    reason: ReplayReason | None,
    strict_toolchain: bool,
    fresh_result: VerificationResult | None = None,
) -> CertificateReplay:
    return CertificateReplay(
        status=status,
        reason=reason,
        certificate_digest=pins.certificate_digest,
        graph_digest=pins.graph_digest,
        registry_digest=pins.registry_digest,
        registry_version=pins.registry_version,
        strict_toolchain=strict_toolchain,
        certificate_verifier_version=pins.certificate_verifier_version,
        certificate_solver_version=pins.certificate_solver_version,
        current_verifier_version=pins.current_verifier_version,
        current_solver_version=pins.current_solver_version,
        toolchain_match=pins.toolchain_match,
        fresh_result=fresh_result,
    )
