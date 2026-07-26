"""Bounded, verification-backed unit-annotation repair proposals.

This module never edits a caller's graph.  It can only propose replacing one
explicit canonical unit annotation that participates in a freshly verified
minimal conflict core.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .canonical import canonical_json_bytes, sha256_hex
from .domain import MAX_UNIT_ID_LENGTH, UNIT_ID, Unit, UnitSentinelError
from .graph import ComputationGraph, ValueSpec
from .registry import BUILTIN_REGISTRY, SHA256_HEX, UnitRegistry
from .verification import (
    ConstraintSource,
    InferredContract,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .verifier import verify_graph

REPAIR_SCHEMA: Final = "unitsentinel.unit-annotation-repair/v1"
MAX_REPAIR_SITES: Final = 64
MAX_REPAIR_CANDIDATES: Final = 512
MAX_REPAIR_VERIFIER_CALLS: Final = 1_024
MAX_REPAIR_WORK_ITEMS: Final = 8_192
MAX_REPAIR_TOTAL_TIMEOUT_MS: Final = 60_000


class RepairError(UnitSentinelError):
    """Raised when a repair request or result contract is malformed."""


class RepairStatus(StrEnum):
    """Closed outcomes for one repair search."""

    PROPOSED = "proposed"
    ABSTAINED = "abstained"
    INDETERMINATE = "indeterminate"


class RepairReason(StrEnum):
    """Stable, redacted reasons for non-proposal outcomes."""

    SOURCE_VERIFIED = "source-verified"
    SOURCE_UNDERCONSTRAINED = "source-underconstrained"
    SOURCE_UNKNOWN = "source-unknown"
    SOURCE_CONFLICT_NOT_MINIMAL = "source-conflict-not-minimal"
    NO_ELIGIBLE_DECLARATION = "no-eligible-declaration"
    MULTIPLE_CONFLICTS_REMAIN = "multiple-conflicts-remain"
    RELAXED_GRAPH_UNDERCONSTRAINED = "relaxed-graph-underconstrained"
    NO_CANONICAL_MATCH = "no-canonical-match"
    NO_VERIFIED_CANDIDATE = "no-verified-candidate"
    AMBIGUOUS_CANDIDATES = "ambiguous-candidates"
    SITE_LIMIT = "site-limit"
    CANDIDATE_LIMIT = "candidate-limit"
    WORK_LIMIT = "work-limit"
    DEADLINE = "deadline"
    VERIFIER_FAILURE = "verifier-failure"
    INTERNAL_FAILURE = "internal-failure"
    RELAXED_VERIFICATION_UNKNOWN = "relaxed-verification-unknown"
    CANDIDATE_VERIFICATION_UNKNOWN = "candidate-verification-unknown"


_ABSTENTION_REASONS: Final = frozenset(
    {
        RepairReason.SOURCE_VERIFIED,
        RepairReason.SOURCE_UNDERCONSTRAINED,
        RepairReason.NO_ELIGIBLE_DECLARATION,
        RepairReason.MULTIPLE_CONFLICTS_REMAIN,
        RepairReason.RELAXED_GRAPH_UNDERCONSTRAINED,
        RepairReason.NO_CANONICAL_MATCH,
        RepairReason.NO_VERIFIED_CANDIDATE,
        RepairReason.AMBIGUOUS_CANDIDATES,
    }
)
_INDETERMINATE_REASONS: Final = frozenset(RepairReason) - _ABSTENTION_REASONS


def _require_exact_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise RepairError(f"{label} must be an exact integer")
    return value


def _require_unit_id(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_UNIT_ID_LENGTH
        or UNIT_ID.fullmatch(value) is None
    ):
        raise RepairError(f"{label} is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class RepairLimits:
    """Aggregate site, candidate, verifier, work, and wall-clock bounds."""

    max_sites: int = 16
    max_candidates: int = 64
    max_verifier_calls: int = 96
    max_work_items: int = 2_048
    total_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not RepairLimits:
            raise RepairError("repair limits must be an exact RepairLimits")
        sites = _require_exact_int(self.max_sites, label="repair site limit")
        candidates = _require_exact_int(
            self.max_candidates,
            label="repair candidate limit",
        )
        verifier_calls = _require_exact_int(
            self.max_verifier_calls,
            label="repair verifier-call limit",
        )
        work_items = _require_exact_int(
            self.max_work_items,
            label="repair work-item limit",
        )
        timeout = _require_exact_int(
            self.total_timeout_ms,
            label="repair total timeout",
        )
        if sites < 1 or sites > MAX_REPAIR_SITES:
            raise RepairError("repair site limit is out of bounds")
        if candidates < 1 or candidates > MAX_REPAIR_CANDIDATES:
            raise RepairError("repair candidate limit is out of bounds")
        if verifier_calls < 1 or verifier_calls > MAX_REPAIR_VERIFIER_CALLS:
            raise RepairError("repair verifier-call limit is out of bounds")
        if work_items < 1 or work_items > MAX_REPAIR_WORK_ITEMS:
            raise RepairError("repair work-item limit is out of bounds")
        if timeout < 1 or timeout > MAX_REPAIR_TOTAL_TIMEOUT_MS:
            raise RepairError("repair total timeout is out of bounds")

    def canonical_record(self) -> dict[str, int]:
        self.validate()
        return {
            "max_candidates": self.max_candidates,
            "max_sites": self.max_sites,
            "max_verifier_calls": self.max_verifier_calls,
            "max_work_items": self.max_work_items,
            "total_timeout_ms": self.total_timeout_ms,
        }


def _same_graph_except_unit(
    left: ComputationGraph,
    right: ComputationGraph,
    *,
    value_id: str,
    left_unit_id: str | None,
    right_unit_id: str | None,
) -> bool:
    left.validate()
    right.validate()
    if (
        left.graph_id != right.graph_id
        or left.inputs != right.inputs
        or left.nodes != right.nodes
        or left.outputs != right.outputs
        or len(left.values) != len(right.values)
    ):
        return False
    found = False
    for left_value, right_value in zip(left.values, right.values, strict=True):
        if (
            left_value.value_id != right_value.value_id
            or left_value.dtype is not right_value.dtype
            or left_value.shape != right_value.shape
        ):
            return False
        if left_value.value_id == value_id:
            found = True
            if (
                left_value.unit_id != left_unit_id
                or right_value.unit_id != right_unit_id
            ):
                return False
        elif left_value.unit_id != right_value.unit_id:
            return False
    return found


@dataclass(frozen=True, slots=True)
class UnitRepairCandidate:
    """One single-declaration proposal with verified relaxed and repaired graphs."""

    constraint_id: str
    value_id: str
    previous_unit_id: str
    replacement_unit_id: str
    relaxed_graph: ComputationGraph
    repaired_graph: ComputationGraph
    relaxed_verification: VerificationResult
    repaired_verification: VerificationResult
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not UnitRepairCandidate:
            raise RepairError("repair candidate must be an exact UnitRepairCandidate")
        value_id = _require_unit_id(self.value_id, label="repair value identifier")
        if self.constraint_id != f"declaration/{value_id}/unit":
            raise RepairError("repair constraint identifier is not canonical")
        previous = _require_unit_id(
            self.previous_unit_id,
            label="previous unit identifier",
        )
        replacement = _require_unit_id(
            self.replacement_unit_id,
            label="replacement unit identifier",
        )
        if previous == replacement:
            raise RepairError("repair replacement must change the annotation")
        if type(self.relaxed_graph) is not ComputationGraph:
            raise RepairError("relaxed graph must be an exact ComputationGraph")
        if type(self.repaired_graph) is not ComputationGraph:
            raise RepairError("repaired graph must be an exact ComputationGraph")
        try:
            self.relaxed_graph.validate()
            self.repaired_graph.validate()
        except UnitSentinelError:
            raise RepairError("repair graph lineage is rejected or mutated") from None
        if not _same_graph_except_unit(
            self.relaxed_graph,
            self.repaired_graph,
            value_id=value_id,
            left_unit_id=None,
            right_unit_id=replacement,
        ):
            raise RepairError(
                "repaired graph must add exactly one replacement annotation"
            )
        for label, result, graph in (
            (
                "relaxed",
                self.relaxed_verification,
                self.relaxed_graph,
            ),
            (
                "repaired",
                self.repaired_verification,
                self.repaired_graph,
            ),
        ):
            if type(result) is not VerificationResult:
                raise RepairError(
                    f"{label} verification must be an exact VerificationResult"
                )
            try:
                result.validate()
            except UnitSentinelError:
                raise RepairError(
                    f"{label} verification is rejected or mutated"
                ) from None
            if (
                result.status is not VerificationStatus.VERIFIED
                or result.graph_digest != graph.digest
            ):
                raise RepairError(f"{label} verification does not verify its graph")
        if (
            self.relaxed_verification.registry_digest
            != self.repaired_verification.registry_digest
        ):
            raise RepairError("repair lineage changes the registry snapshot")
        if (
            self.relaxed_verification.limits != self.repaired_verification.limits
            or self.relaxed_verification.solver_version
            != self.repaired_verification.solver_version
        ):
            raise RepairError("repair lineage changes verifier identity or limits")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise RepairError("repair candidate digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise RepairError("repair candidate digest does not match its contents")

    def _canonical_record_unchecked(self) -> dict[str, str]:
        return {
            "constraint_id": self.constraint_id,
            "previous_unit_id": self.previous_unit_id,
            "relaxed_graph_digest": self.relaxed_graph.digest,
            "relaxed_verification_digest": self.relaxed_verification.digest,
            "repaired_graph_digest": self.repaired_graph.digest,
            "repaired_verification_digest": self.repaired_verification.digest,
            "replacement_unit_id": self.replacement_unit_id,
            "value_id": self.value_id,
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, str]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self._canonical_record_unchecked())


@dataclass(frozen=True, slots=True)
class UnitRepairResult:
    """Content-addressed outcome of one bounded repair search."""

    status: RepairStatus
    reason: RepairReason | None
    source_graph: ComputationGraph
    registry: UnitRegistry
    repair_limits: RepairLimits
    solver_limits: SolverLimits
    verification_calls: int
    sites_considered: int
    candidates_considered: int
    work_items: int
    source_verification: VerificationResult | None = None
    candidate: UnitRepairCandidate | None = None
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not UnitRepairResult:
            raise RepairError("repair result must be an exact UnitRepairResult")
        if type(self.status) is not RepairStatus:
            raise RepairError("repair status is unknown")
        if self.reason is not None and type(self.reason) is not RepairReason:
            raise RepairError("repair reason is unknown")
        if type(self.source_graph) is not ComputationGraph:
            raise RepairError("repair source must be an exact ComputationGraph")
        if type(self.registry) is not UnitRegistry:
            raise RepairError("repair registry must be an exact UnitRegistry")
        if type(self.repair_limits) is not RepairLimits:
            raise RepairError("repair limits must be an exact RepairLimits")
        if type(self.solver_limits) is not SolverLimits:
            raise RepairError("solver limits must be an exact SolverLimits")
        try:
            self.source_graph.validate()
            self.registry.validate()
            self.repair_limits.validate()
            self.solver_limits.validate()
        except UnitSentinelError:
            raise RepairError("repair result source is rejected or mutated") from None
        counters = (
            (
                "verification call count",
                self.verification_calls,
                self.repair_limits.max_verifier_calls,
            ),
            (
                "site count",
                self.sites_considered,
                self.repair_limits.max_sites,
            ),
            (
                "candidate count",
                self.candidates_considered,
                self.repair_limits.max_candidates,
            ),
            (
                "work-item count",
                self.work_items,
                self.repair_limits.max_work_items,
            ),
        )
        for label, count, maximum in counters:
            exact_count = _require_exact_int(count, label=label)
            if exact_count < 0 or exact_count > maximum:
                raise RepairError(f"{label} is out of bounds")
        if self.source_verification is not None:
            if type(self.source_verification) is not VerificationResult:
                raise RepairError(
                    "source verification must be an exact VerificationResult"
                )
            try:
                self.source_verification.validate()
            except UnitSentinelError:
                raise RepairError(
                    "source verification is rejected or mutated"
                ) from None
            if (
                self.source_verification.graph_digest != self.source_graph.digest
                or self.source_verification.registry_digest != self.registry.digest
            ):
                raise RepairError("source verification does not bind the repair inputs")
            if self.source_verification.limits != _effective_solver_limits(
                self.repair_limits,
                self.solver_limits,
            ):
                raise RepairError("source verification uses unexpected solver limits")
        if self.candidate is not None:
            if type(self.candidate) is not UnitRepairCandidate:
                raise RepairError(
                    "repair result candidate must be an exact UnitRepairCandidate"
                )
            self.candidate.validate()
        self._validate_outcome()

    def _validate_outcome(self) -> None:
        if self.status is RepairStatus.PROPOSED:
            if (
                self.reason is not None
                or self.source_verification is None
                or self.source_verification.status is not VerificationStatus.CONFLICT
                or self.source_verification.core_minimal is not True
                or self.candidate is None
            ):
                raise RepairError("proposed repair fields are inconsistent")
            self._validate_candidate_lineage(self.candidate)
            return
        if self.candidate is not None or self.reason is None:
            raise RepairError("non-proposal repair fields are inconsistent")
        if (
            self.status is RepairStatus.ABSTAINED
            and self.reason not in _ABSTENTION_REASONS
        ):
            raise RepairError("abstention reason is inconsistent")
        if (
            self.status is RepairStatus.INDETERMINATE
            and self.reason not in _INDETERMINATE_REASONS
        ):
            raise RepairError("indeterminate reason is inconsistent")
        self._validate_nonproposal_source()

    def _validate_nonproposal_source(self) -> None:
        assert self.reason is not None
        source = self.source_verification
        if self.reason is RepairReason.SOURCE_VERIFIED:
            expected = VerificationStatus.VERIFIED
        elif self.reason is RepairReason.SOURCE_UNDERCONSTRAINED:
            expected = VerificationStatus.UNDERCONSTRAINED
        elif self.reason is RepairReason.SOURCE_UNKNOWN:
            expected = VerificationStatus.UNKNOWN
        else:
            expected = None
        if expected is not None:
            if source is None or source.status is not expected:
                raise RepairError("repair reason does not match source verification")
            return
        if self.reason is RepairReason.SOURCE_CONFLICT_NOT_MINIMAL:
            if (
                source is None
                or source.status is not VerificationStatus.CONFLICT
                or source.core_minimal is not False
            ):
                raise RepairError("repair reason does not match source conflict")
            return
        post_conflict_reasons = {
            RepairReason.NO_ELIGIBLE_DECLARATION,
            RepairReason.MULTIPLE_CONFLICTS_REMAIN,
            RepairReason.RELAXED_GRAPH_UNDERCONSTRAINED,
            RepairReason.NO_CANONICAL_MATCH,
            RepairReason.NO_VERIFIED_CANDIDATE,
            RepairReason.AMBIGUOUS_CANDIDATES,
            RepairReason.SITE_LIMIT,
            RepairReason.CANDIDATE_LIMIT,
            RepairReason.RELAXED_VERIFICATION_UNKNOWN,
            RepairReason.CANDIDATE_VERIFICATION_UNKNOWN,
        }
        if self.reason in post_conflict_reasons and (
            source is None
            or source.status is not VerificationStatus.CONFLICT
            or source.core_minimal is not True
        ):
            raise RepairError("repair reason requires a minimal source conflict")

    def _validate_candidate_lineage(
        self,
        candidate: UnitRepairCandidate,
    ) -> None:
        assert self.source_verification is not None
        if candidate.relaxed_verification.registry_digest != self.registry.digest:
            raise RepairError("repair candidate changes the registry snapshot")
        if (
            self.source_verification.limits != candidate.relaxed_verification.limits
            or self.source_verification.solver_version
            != candidate.relaxed_verification.solver_version
        ):
            raise RepairError("repair candidate changes verifier identity or limits")
        try:
            candidate.relaxed_graph.validate_units(self.registry)
            candidate.repaired_graph.validate_units(self.registry)
        except UnitSentinelError:
            raise RepairError(
                "repair candidate is rejected by the bound registry"
            ) from None
        if not _same_graph_except_unit(
            self.source_graph,
            candidate.relaxed_graph,
            value_id=candidate.value_id,
            left_unit_id=candidate.previous_unit_id,
            right_unit_id=None,
        ):
            raise RepairError("relaxed graph must remove exactly one source annotation")
        if not any(
            witness.source is ConstraintSource.DECLARATION
            and witness.source_id == candidate.value_id
            and witness.rule == "unit-annotation"
            and witness.constraint_id == candidate.constraint_id
            for witness in self.source_verification.conflict_core
        ):
            raise RepairError("repair annotation is not in the source conflict core")
        try:
            replacement = self.registry.resolve(candidate.replacement_unit_id)
        except UnitSentinelError:
            raise RepairError(
                "replacement is not in the bound registry snapshot"
            ) from None
        if replacement.unit_id != candidate.replacement_unit_id:
            raise RepairError("replacement must be a canonical registry unit")
        relaxed_contract = _contract_for(
            candidate.relaxed_verification,
            candidate.value_id,
        )
        if relaxed_contract is None or not _unit_matches_contract(
            replacement,
            relaxed_contract,
        ):
            raise RepairError("replacement does not exactly match the relaxed contract")
        repaired_contract = _contract_for(
            candidate.repaired_verification,
            candidate.value_id,
        )
        if repaired_contract is None or not _unit_matches_contract(
            replacement,
            repaired_contract,
        ):
            raise RepairError("verified repair does not bind the replacement contract")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise RepairError("repair result digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise RepairError("repair result digest does not match its contents")

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "candidate": (
                None if self.candidate is None else self.candidate.canonical_record()
            ),
            "candidates_considered": self.candidates_considered,
            "reason": None if self.reason is None else self.reason.value,
            "registry_digest": self.registry.digest,
            "repair_limits": self.repair_limits.canonical_record(),
            "schema": REPAIR_SCHEMA,
            "sites_considered": self.sites_considered,
            "solver_limits": self.solver_limits.canonical_record(),
            "source_graph_digest": self.source_graph.digest,
            "source_verification_digest": (
                None
                if self.source_verification is None
                else self.source_verification.digest
            ),
            "status": self.status.value,
            "verification_calls": self.verification_calls,
            "work_items": self.work_items,
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


@dataclass(slots=True)
class _RepairBudget:
    limits: RepairLimits
    started_at: float
    verification_calls: int = 0
    sites_considered: int = 0
    candidates_considered: int = 0
    work_items: int = 0

    @property
    def expired(self) -> bool:
        return time.monotonic() >= (
            self.started_at + self.limits.total_timeout_ms / 1_000
        )

    def reserve_work(self) -> None:
        if self.expired:
            raise _StopSearch(RepairReason.DEADLINE)
        if self.work_items >= self.limits.max_work_items:
            raise _StopSearch(RepairReason.WORK_LIMIT)
        self.work_items += 1

    def reserve_verification(self) -> None:
        if self.verification_calls >= self.limits.max_verifier_calls:
            raise _StopSearch(RepairReason.WORK_LIMIT)
        self.reserve_work()
        self.verification_calls += 1

    def finish_operation(self) -> None:
        if self.expired:
            raise _StopSearch(RepairReason.DEADLINE)


class _StopSearch(Exception):
    def __init__(self, reason: RepairReason) -> None:
        self.reason = reason


_DEFAULT_REPAIR_LIMITS: Final = RepairLimits()
_DEFAULT_SOLVER_LIMITS: Final = SolverLimits()


def _effective_solver_limits(
    repair_limits: RepairLimits,
    solver_limits: SolverLimits,
) -> SolverLimits:
    per_call_total = max(
        1,
        min(
            solver_limits.total_timeout_ms,
            repair_limits.total_timeout_ms // repair_limits.max_verifier_calls,
        ),
    )
    return SolverLimits(
        per_check_timeout_ms=min(
            solver_limits.per_check_timeout_ms,
            per_call_total,
        ),
        total_timeout_ms=per_call_total,
        max_memory_mb=solver_limits.max_memory_mb,
        max_core_shrink_checks=solver_limits.max_core_shrink_checks,
        max_uniqueness_checks=solver_limits.max_uniqueness_checks,
    )


def _clone_with_unit(
    graph: ComputationGraph,
    *,
    value_id: str,
    unit_id: str | None,
) -> ComputationGraph:
    values = tuple(
        ValueSpec(
            value.value_id,
            value.dtype,
            value.shape,
            unit_id if value.value_id == value_id else value.unit_id,
        )
        for value in graph.values
    )
    return ComputationGraph(
        graph_id=graph.graph_id,
        values=values,
        inputs=graph.inputs,
        nodes=graph.nodes,
        outputs=graph.outputs,
    )


def _contract_for(
    verification: VerificationResult,
    value_id: str,
) -> InferredContract | None:
    for contract in verification.contracts:
        if contract.value_id == value_id:
            return contract
    return None


def _unit_matches_contract(unit: Unit, contract: InferredContract) -> bool:
    return (
        unit.dimension == contract.dimension
        and unit.kind is contract.kind
        and unit.scale == contract.scale
        and unit.offset == contract.offset
    )


def _eligible_sites(
    graph: ComputationGraph,
    source: VerificationResult,
    budget: _RepairBudget,
) -> tuple[str, ...]:
    sites: set[str] = set()
    for witness in source.conflict_core:
        budget.reserve_work()
        value_id = witness.source_id
        if (
            witness.source is ConstraintSource.DECLARATION
            and witness.rule == "unit-annotation"
            and witness.constraint_id == f"declaration/{value_id}/unit"
            and graph.value(value_id).unit_id is not None
        ):
            sites.add(value_id)
    return tuple(sorted(sites))


def propose_unit_annotation_repair(
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    repair_limits: RepairLimits = _DEFAULT_REPAIR_LIMITS,
    solver_limits: SolverLimits = _DEFAULT_SOLVER_LIMITS,
) -> UnitRepairResult:
    """Return one verified single-annotation proposal, or fail closed.

    The returned graph is a new immutable value.  This function never mutates
    or installs a proposal and never infers the scientific intent of a caller.
    """

    if type(graph) is not ComputationGraph:
        raise RepairError("repair graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise RepairError("repair registry must be an exact UnitRegistry")
    if type(repair_limits) is not RepairLimits:
        raise RepairError("repair limits must be an exact RepairLimits")
    if type(solver_limits) is not SolverLimits:
        raise RepairError("solver limits must be an exact SolverLimits")
    try:
        graph.validate()
        registry.validate()
        graph.validate_units(registry)
        repair_limits.validate()
        solver_limits.validate()
    except Exception:
        raise RepairError("repair inputs are rejected or mutated") from None

    budget = _RepairBudget(repair_limits, time.monotonic())
    effective_limits = _effective_solver_limits(repair_limits, solver_limits)
    source: VerificationResult | None = None

    def finish(
        status: RepairStatus,
        reason: RepairReason | None,
        *,
        candidate: UnitRepairCandidate | None = None,
    ) -> UnitRepairResult:
        completed = status in {
            RepairStatus.PROPOSED,
            RepairStatus.ABSTAINED,
        }
        if completed:
            budget.finish_operation()
        result = UnitRepairResult(
            status=status,
            reason=reason,
            source_graph=graph,
            registry=registry,
            repair_limits=repair_limits,
            solver_limits=solver_limits,
            verification_calls=budget.verification_calls,
            sites_considered=budget.sites_considered,
            candidates_considered=budget.candidates_considered,
            work_items=budget.work_items,
            source_verification=source,
            candidate=candidate,
        )
        if completed:
            budget.finish_operation()
        return result

    def run_verifier(candidate_graph: ComputationGraph) -> VerificationResult:
        budget.reserve_verification()
        try:
            result = verify_graph(
                candidate_graph,
                registry=registry,
                limits=effective_limits,
            )
        except Exception:
            raise _StopSearch(RepairReason.VERIFIER_FAILURE) from None
        budget.finish_operation()
        if type(result) is not VerificationResult:
            raise _StopSearch(RepairReason.VERIFIER_FAILURE)
        try:
            result.validate()
        except UnitSentinelError:
            raise _StopSearch(RepairReason.VERIFIER_FAILURE) from None
        if (
            result.graph_digest != candidate_graph.digest
            or result.registry_digest != registry.digest
            or result.limits != effective_limits
        ):
            raise _StopSearch(RepairReason.VERIFIER_FAILURE)
        return result

    try:
        source = run_verifier(graph)
        if source.status is VerificationStatus.VERIFIED:
            return finish(RepairStatus.ABSTAINED, RepairReason.SOURCE_VERIFIED)
        if source.status is VerificationStatus.UNDERCONSTRAINED:
            return finish(
                RepairStatus.ABSTAINED,
                RepairReason.SOURCE_UNDERCONSTRAINED,
            )
        if source.status is VerificationStatus.UNKNOWN:
            return finish(
                RepairStatus.INDETERMINATE,
                RepairReason.SOURCE_UNKNOWN,
            )
        if source.core_minimal is not True:
            return finish(
                RepairStatus.INDETERMINATE,
                RepairReason.SOURCE_CONFLICT_NOT_MINIMAL,
            )

        sites = _eligible_sites(graph, source, budget)
        if not sites:
            return finish(
                RepairStatus.ABSTAINED,
                RepairReason.NO_ELIGIBLE_DECLARATION,
            )
        if len(sites) > repair_limits.max_sites:
            return finish(
                RepairStatus.INDETERMINATE,
                RepairReason.SITE_LIMIT,
            )

        verified_candidates: list[UnitRepairCandidate] = []
        saw_relaxed_conflict = False
        saw_relaxed_underconstrained = False
        saw_relaxed_verified = False
        saw_canonical_match = False

        for value_id in sites:
            budget.sites_considered += 1
            budget.reserve_work()
            previous_unit_id = graph.value(value_id).unit_id
            assert previous_unit_id is not None
            relaxed_graph = _clone_with_unit(
                graph,
                value_id=value_id,
                unit_id=None,
            )
            relaxed = run_verifier(relaxed_graph)
            if relaxed.status is VerificationStatus.UNKNOWN:
                return finish(
                    RepairStatus.INDETERMINATE,
                    RepairReason.RELAXED_VERIFICATION_UNKNOWN,
                )
            if relaxed.status is VerificationStatus.CONFLICT:
                saw_relaxed_conflict = True
                continue
            if relaxed.status is VerificationStatus.UNDERCONSTRAINED:
                saw_relaxed_underconstrained = True
                continue

            saw_relaxed_verified = True
            inferred = _contract_for(relaxed, value_id)
            if inferred is None:
                return finish(
                    RepairStatus.INDETERMINATE,
                    RepairReason.VERIFIER_FAILURE,
                )
            for unit in registry.units:
                budget.reserve_work()
                if unit.unit_id == previous_unit_id or not _unit_matches_contract(
                    unit, inferred
                ):
                    continue
                saw_canonical_match = True
                if budget.candidates_considered >= repair_limits.max_candidates:
                    return finish(
                        RepairStatus.INDETERMINATE,
                        RepairReason.CANDIDATE_LIMIT,
                    )
                budget.candidates_considered += 1
                budget.reserve_work()
                repaired_graph = _clone_with_unit(
                    relaxed_graph,
                    value_id=value_id,
                    unit_id=unit.unit_id,
                )
                repaired = run_verifier(repaired_graph)
                if repaired.status is VerificationStatus.UNKNOWN:
                    return finish(
                        RepairStatus.INDETERMINATE,
                        RepairReason.CANDIDATE_VERIFICATION_UNKNOWN,
                    )
                if repaired.status is not VerificationStatus.VERIFIED:
                    continue
                proposal = UnitRepairCandidate(
                    constraint_id=f"declaration/{value_id}/unit",
                    value_id=value_id,
                    previous_unit_id=previous_unit_id,
                    replacement_unit_id=unit.unit_id,
                    relaxed_graph=relaxed_graph,
                    repaired_graph=repaired_graph,
                    relaxed_verification=relaxed,
                    repaired_verification=repaired,
                )
                verified_candidates.append(proposal)
                if len(verified_candidates) > 1:
                    return finish(
                        RepairStatus.ABSTAINED,
                        RepairReason.AMBIGUOUS_CANDIDATES,
                    )

        if len(verified_candidates) == 1:
            return finish(
                RepairStatus.PROPOSED,
                None,
                candidate=verified_candidates[0],
            )
        if saw_relaxed_conflict:
            reason = RepairReason.MULTIPLE_CONFLICTS_REMAIN
        elif saw_relaxed_underconstrained and not saw_relaxed_verified:
            reason = RepairReason.RELAXED_GRAPH_UNDERCONSTRAINED
        elif saw_relaxed_verified and not saw_canonical_match:
            reason = RepairReason.NO_CANONICAL_MATCH
        else:
            reason = RepairReason.NO_VERIFIED_CANDIDATE
        return finish(RepairStatus.ABSTAINED, reason)
    except _StopSearch as stop:
        return finish(RepairStatus.INDETERMINATE, stop.reason)
    except Exception:
        return finish(
            RepairStatus.INDETERMINATE,
            RepairReason.INTERNAL_FAILURE,
        )
