"""Fresh, fail-closed training-versus-serving interface comparison.

Comparison plans are unsigned policy inputs.  A ``compatible`` result means
only that the two freshly verified interfaces agree under the exact plan
digest recorded by the result; it does not authenticate that plan or either
graph.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import z3  # type: ignore[import-untyped]

from .canonical import canonical_json_bytes, sha256_hex
from .comparison_contract import (
    MAX_COMPARISON_BINDINGS,
    MAX_COMPARISON_ID_LENGTH,
    MAX_CONTRACT_ID_LENGTH,
    ComparisonPlan,
    InterfaceEndpoint,
    InterfaceRole,
)
from .domain import UNIT_ID, UnitSentinelError
from .graph import (
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_OUTPUTS,
    ComputationGraph,
    ValueSpec,
)
from .registry import BUILTIN_REGISTRY, SHA256_HEX, UnitRegistry
from .verification import (
    SOLVER_VERSION,
    InferredContract,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .verifier import _replay_claimed_contracts, verify_graph

COMPARISON_RESULT_SCHEMA: Final = "unitsentinel.training-serving-comparison-result/v1"
AUTHENTICATION_NOT_PROVIDED: Final = "not-provided"
COMPARISON_SCOPE_UNDER_PLAN: Final = "under-plan"
_DEFAULT_COMPARISON_LIMITS: Final = SolverLimits()


class ComparisonError(UnitSentinelError):
    """Raised when comparison inputs or a fresh verifier result are unsafe."""


class ComparisonStatus(StrEnum):
    """Closed outcomes for one fresh, plan-scoped comparison."""

    COMPATIBLE = "compatible"
    DRIFT = "drift"
    INDETERMINATE = "indeterminate"


class ComparisonReason(StrEnum):
    """Stable reason for a comparison without a decisive interface result."""

    VERIFIER_FAILURE = "verifier-failure"
    TRAINING_NOT_VERIFIED = "training-not-verified"
    SERVING_NOT_VERIFIED = "serving-not-verified"
    BOTH_NOT_VERIFIED = "both-not-verified"


class MismatchCode(StrEnum):
    """Stable interface differences in deterministic presentation order."""

    MISSING_IN_SERVING = "missing-in-serving"
    EXTRA_IN_SERVING = "extra-in-serving"
    ROLE_DRIFT = "role-drift"
    POSITION_DRIFT = "position-drift"
    DTYPE_DRIFT = "dtype-drift"
    SHAPE_DRIFT = "shape-drift"
    EXPLICIT_UNIT_DRIFT = "explicit-unit-drift"
    DIMENSION_DRIFT = "dimension-drift"
    KIND_DRIFT = "kind-drift"
    SCALE_DRIFT = "scale-drift"
    OFFSET_DRIFT = "offset-drift"


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or SHA256_HEX.fullmatch(value) is None:
        raise ComparisonError(f"{label} is malformed")
    return value


def _require_identifier(
    value: object,
    *,
    label: str,
    max_length: int,
) -> str:
    if (
        type(value) is not str
        or len(value) > max_length
        or UNIT_ID.fullmatch(value) is None
    ):
        raise ComparisonError(f"{label} is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    """Caller policy that may pin one previously trusted plan digest."""

    expected_plan_digest: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not ComparisonPolicy:
            raise ComparisonError("comparison policy must be an exact ComparisonPolicy")
        if self.expected_plan_digest is not None:
            _require_digest(
                self.expected_plan_digest,
                label="expected comparison plan digest",
            )

    def canonical_record(self) -> dict[str, str | None]:
        self.validate()
        return {"expected_plan_sha256": self.expected_plan_digest}


_DEFAULT_COMPARISON_POLICY: Final = ComparisonPolicy()


@dataclass(frozen=True, slots=True)
class InterfaceSnapshot:
    """One verified public occurrence and all compared contract metadata."""

    endpoint: InterfaceEndpoint
    position: int
    value: ValueSpec
    inferred: InferredContract

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not InterfaceSnapshot:
            raise ComparisonError(
                "interface snapshot must be an exact InterfaceSnapshot"
            )
        if type(self.endpoint) is not InterfaceEndpoint:
            raise ComparisonError(
                "snapshot endpoint must be an exact InterfaceEndpoint"
            )
        if type(self.value) is not ValueSpec:
            raise ComparisonError("snapshot value must be an exact ValueSpec")
        if type(self.inferred) is not InferredContract:
            raise ComparisonError("snapshot contract must be an exact InferredContract")
        try:
            self.endpoint.validate()
            self.value.validate()
            self.inferred.validate()
        except UnitSentinelError:
            raise ComparisonError(
                "interface snapshot contains malformed or mutated values"
            ) from None
        if type(self.position) is not int:
            raise ComparisonError("snapshot position must be an exact integer")
        maximum = (
            MAX_GRAPH_INPUTS
            if self.endpoint.role is InterfaceRole.INPUT
            else MAX_GRAPH_OUTPUTS
        )
        if self.position < 0 or self.position >= maximum:
            raise ComparisonError("snapshot position is out of bounds")
        if (
            self.endpoint.value_id != self.value.value_id
            or self.endpoint.value_id != self.inferred.value_id
        ):
            raise ComparisonError("snapshot value identities are inconsistent")

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "endpoint": self.endpoint.canonical_record(),
            "inferred": self.inferred.canonical_record(),
            "position": self.position,
            "value": self.value.canonical_record(),
        }


def _expected_mismatches(
    training: InterfaceSnapshot | None,
    serving: InterfaceSnapshot | None,
) -> tuple[MismatchCode, ...]:
    if training is None:
        return (MismatchCode.EXTRA_IN_SERVING,)
    if serving is None:
        return (MismatchCode.MISSING_IN_SERVING,)

    mismatches: list[MismatchCode] = []
    if training.endpoint.role is not serving.endpoint.role:
        mismatches.append(MismatchCode.ROLE_DRIFT)
    if (
        training.endpoint.role is serving.endpoint.role
        and training.position != serving.position
    ):
        mismatches.append(MismatchCode.POSITION_DRIFT)
    if training.value.dtype is not serving.value.dtype:
        mismatches.append(MismatchCode.DTYPE_DRIFT)
    if training.value.shape != serving.value.shape:
        mismatches.append(MismatchCode.SHAPE_DRIFT)
    if training.value.unit_id != serving.value.unit_id:
        mismatches.append(MismatchCode.EXPLICIT_UNIT_DRIFT)
    if training.inferred.dimension != serving.inferred.dimension:
        mismatches.append(MismatchCode.DIMENSION_DRIFT)
    if training.inferred.kind is not serving.inferred.kind:
        mismatches.append(MismatchCode.KIND_DRIFT)
    if training.inferred.scale != serving.inferred.scale:
        mismatches.append(MismatchCode.SCALE_DRIFT)
    if training.inferred.offset != serving.inferred.offset:
        mismatches.append(MismatchCode.OFFSET_DRIFT)
    return tuple(mismatches)


@dataclass(frozen=True, slots=True)
class ContractComparison:
    """The snapshots and exact drift codes for one logical binding."""

    contract_id: str
    training: InterfaceSnapshot | None
    serving: InterfaceSnapshot | None
    mismatches: tuple[MismatchCode, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not ContractComparison:
            raise ComparisonError(
                "contract comparison must be an exact ContractComparison"
            )
        _require_identifier(
            self.contract_id,
            label="comparison contract identifier",
            max_length=MAX_CONTRACT_ID_LENGTH,
        )
        if self.training is None and self.serving is None:
            raise ComparisonError(
                "contract comparison must contain a training or serving snapshot"
            )
        for side, snapshot in (
            ("training", self.training),
            ("serving", self.serving),
        ):
            if snapshot is None:
                continue
            if type(snapshot) is not InterfaceSnapshot:
                raise ComparisonError(
                    f"{side} snapshot must be an exact InterfaceSnapshot or null"
                )
            snapshot.validate()
        if type(self.mismatches) is not tuple:
            raise ComparisonError("comparison mismatches must be a tuple")
        if any(type(code) is not MismatchCode for code in self.mismatches):
            raise ComparisonError(
                "comparison mismatches must contain exact MismatchCode values"
            )
        expected = _expected_mismatches(self.training, self.serving)
        if self.mismatches != expected:
            raise ComparisonError(
                "comparison mismatch codes are incomplete or out of order"
            )

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_id": self.contract_id,
            "mismatches": [code.value for code in self.mismatches],
            "serving": (
                None if self.serving is None else self.serving.canonical_record()
            ),
            "training": (
                None if self.training is None else self.training.canonical_record()
            ),
        }


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Unsigned, content-addressed comparison claim.

    Direct construction cannot prove freshness or authenticate any source.
    Use :func:`compare_graphs` to run the two fresh verifiers, replay their
    complete contracts, and bind the checked inputs represented here.
    """

    status: ComparisonStatus
    reason: ComparisonReason | None
    comparison_id: str
    plan_digest: str
    training_graph_digest: str
    serving_graph_digest: str
    registry_digest: str
    limits: SolverLimits
    training_result: VerificationResult | None
    serving_result: VerificationResult | None
    comparisons: tuple[ContractComparison, ...] = ()
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not ComparisonResult:
            raise ComparisonError("comparison result must be an exact ComparisonResult")
        if type(self.status) is not ComparisonStatus:
            raise ComparisonError("comparison status is unsupported")
        if self.reason is not None and type(self.reason) is not ComparisonReason:
            raise ComparisonError("comparison reason is unsupported")
        _require_identifier(
            self.comparison_id,
            label="result comparison identifier",
            max_length=MAX_COMPARISON_ID_LENGTH,
        )
        for label, digest in (
            ("result plan digest", self.plan_digest),
            ("result training graph digest", self.training_graph_digest),
            ("result serving graph digest", self.serving_graph_digest),
            ("result registry digest", self.registry_digest),
        ):
            _require_digest(digest, label=label)
        if type(self.limits) is not SolverLimits:
            raise ComparisonError("result limits must be an exact SolverLimits")
        try:
            self.limits.validate()
        except UnitSentinelError:
            raise ComparisonError("result limits are malformed or mutated") from None
        self._validate_fresh_result(
            self.training_result,
            side="training",
            graph_digest=self.training_graph_digest,
        )
        self._validate_fresh_result(
            self.serving_result,
            side="serving",
            graph_digest=self.serving_graph_digest,
        )
        if (
            self.training_result is not None
            and self.serving_result is not None
            and self.training_result.solver_version
            != self.serving_result.solver_version
        ):
            raise ComparisonError("fresh results have inconsistent solver identities")
        self._validate_comparisons()
        self._validate_outcome()

    def _validate_fresh_result(
        self,
        result: object | None,
        *,
        side: str,
        graph_digest: str,
    ) -> None:
        if result is None:
            return
        if type(result) is not VerificationResult:
            raise ComparisonError(f"{side} result must be an exact VerificationResult")
        try:
            result.validate()
        except UnitSentinelError:
            raise ComparisonError(f"{side} result is malformed or mutated") from None
        if (
            result.graph_digest != graph_digest
            or result.registry_digest != self.registry_digest
            or canonical_json_bytes(result.limits.canonical_record())
            != canonical_json_bytes(self.limits.canonical_record())
        ):
            raise ComparisonError(f"{side} result source bindings are inconsistent")

    def _validate_comparisons(self) -> None:
        if type(self.comparisons) is not tuple:
            raise ComparisonError("contract comparisons must be a tuple")
        if len(self.comparisons) > MAX_COMPARISON_BINDINGS:
            raise ComparisonError("result contains too many contract comparisons")
        training_contracts = self._result_contracts(self.training_result)
        serving_contracts = self._result_contracts(self.serving_result)
        contract_ids: list[str] = []
        seen_training: set[tuple[InterfaceRole, str]] = set()
        seen_serving: set[tuple[InterfaceRole, str]] = set()
        training_values: dict[str, ValueSpec] = {}
        serving_values: dict[str, ValueSpec] = {}
        for comparison in self.comparisons:
            if type(comparison) is not ContractComparison:
                raise ComparisonError(
                    "result comparisons must be exact ContractComparison values"
                )
            comparison.validate()
            contract_ids.append(comparison.contract_id)
            self._record_snapshot(
                comparison.training,
                side="training",
                seen=seen_training,
                contracts=training_contracts,
                values=training_values,
            )
            self._record_snapshot(
                comparison.serving,
                side="serving",
                seen=seen_serving,
                contracts=serving_contracts,
                values=serving_values,
            )
        if contract_ids != sorted(set(contract_ids)):
            raise ComparisonError("contract comparisons must be sorted and unique")

    @staticmethod
    def _result_contracts(
        result: VerificationResult | None,
    ) -> dict[str, InferredContract]:
        if result is None:
            return {}
        return {contract.value_id: contract for contract in result.contracts}

    @staticmethod
    def _record_snapshot(
        snapshot: InterfaceSnapshot | None,
        *,
        side: str,
        seen: set[tuple[InterfaceRole, str]],
        contracts: dict[str, InferredContract],
        values: dict[str, ValueSpec],
    ) -> None:
        if snapshot is None:
            return
        key = (snapshot.endpoint.role, snapshot.endpoint.value_id)
        if key in seen:
            raise ComparisonError(f"{side} result endpoints must occur at most once")
        seen.add(key)
        inferred = contracts.get(snapshot.endpoint.value_id)
        if inferred is None or snapshot.inferred != inferred:
            raise ComparisonError(
                f"{side} snapshot contradicts its verification result"
            )
        previous = values.setdefault(snapshot.value.value_id, snapshot.value)
        if snapshot.value != previous:
            raise ComparisonError(f"{side} snapshots disagree about one declared value")

    def _validate_outcome(self) -> None:
        both_verified = (
            self.training_result is not None
            and self.serving_result is not None
            and self.training_result.status is VerificationStatus.VERIFIED
            and self.serving_result.status is VerificationStatus.VERIFIED
        )
        mismatch_count = sum(
            len(comparison.mismatches) for comparison in self.comparisons
        )
        if self.status is ComparisonStatus.INDETERMINATE:
            if both_verified or self.comparisons or self.reason is None:
                raise ComparisonError(
                    "indeterminate comparison fields are inconsistent"
                )
            if self.reason is ComparisonReason.VERIFIER_FAILURE:
                if self.training_result is not None and self.serving_result is not None:
                    raise ComparisonError(
                        "verifier-failure comparison fields are inconsistent"
                    )
                return
            if self.training_result is None or self.serving_result is None:
                raise ComparisonError("nonverified comparison fields are inconsistent")
            training_verified = (
                self.training_result.status is VerificationStatus.VERIFIED
            )
            serving_verified = self.serving_result.status is VerificationStatus.VERIFIED
            expected_reason = (
                ComparisonReason.BOTH_NOT_VERIFIED
                if not training_verified and not serving_verified
                else ComparisonReason.TRAINING_NOT_VERIFIED
                if not training_verified
                else ComparisonReason.SERVING_NOT_VERIFIED
            )
            if self.reason is not expected_reason:
                raise ComparisonError("nonverified comparison reason is inconsistent")
            return
        if self.reason is not None or not both_verified or not self.comparisons:
            raise ComparisonError("decisive comparison fields are inconsistent")
        if self.status is ComparisonStatus.COMPATIBLE and mismatch_count:
            raise ComparisonError("compatible comparison cannot contain mismatches")
        if self.status is ComparisonStatus.DRIFT and not mismatch_count:
            raise ComparisonError("drift comparison must contain a mismatch")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise ComparisonError("comparison result digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise ComparisonError(
                "comparison result digest does not match its contents"
            )

    def _canonical_record_unchecked(self) -> dict[str, object]:
        def verification_record(
            result: VerificationResult | None,
        ) -> dict[str, object] | None:
            if result is None:
                return None
            return {
                "record": result.canonical_record(),
                "sha256": result.digest,
            }

        return {
            "authentication": AUTHENTICATION_NOT_PROVIDED,
            "bindings": [
                comparison.canonical_record() for comparison in self.comparisons
            ],
            "comparison_id": self.comparison_id,
            "graphs": {
                "serving_sha256": self.serving_graph_digest,
                "training_sha256": self.training_graph_digest,
            },
            "limits": self.limits.canonical_record(),
            "plan_sha256": self.plan_digest,
            "registry_sha256": self.registry_digest,
            "reason": None if self.reason is None else self.reason.value,
            "schema": COMPARISON_RESULT_SCHEMA,
            "scope": COMPARISON_SCOPE_UNDER_PLAN,
            "status": self.status.value,
            "verification": {
                "serving": verification_record(self.serving_result),
                "training": verification_record(self.training_result),
            },
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    @property
    def authentication(self) -> str:
        self.validate()
        return AUTHENTICATION_NOT_PROVIDED

    @property
    def scope(self) -> str:
        self.validate()
        return COMPARISON_SCOPE_UNDER_PLAN

    @property
    def mismatch_count(self) -> int:
        self.validate()
        return sum(len(item.mismatches) for item in self.comparisons)

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
class _ComparisonPins:
    plan_digest: str
    plan_bytes: bytes
    training_graph_digest: str
    training_graph_bytes: bytes
    serving_graph_digest: str
    serving_graph_bytes: bytes
    registry_digest: str
    registry_bytes: bytes
    limits_bytes: bytes
    policy_bytes: bytes
    solver_version: str


def compare_graphs(
    plan: ComparisonPlan,
    *,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    limits: SolverLimits = _DEFAULT_COMPARISON_LIMITS,
    policy: ComparisonPolicy = _DEFAULT_COMPARISON_POLICY,
) -> ComparisonResult:
    """Freshly verify and compare every public occurrence under one plan.

    A caller-trusted ``ComparisonPolicy(expected_plan_digest=...)`` pin is
    checked before the engine validates or interprets plan bindings. Matching
    that pin provides no author authentication.
    """

    plan, training_graph, serving_graph, registry, limits, policy = (
        _validate_comparison_inputs(
            plan,
            training_graph,
            serving_graph,
            registry,
            limits,
            policy,
        )
    )
    pins = _pin_comparison_inputs(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
    )
    _validate_plan_sources(plan, pins)
    _validate_plan_coverage(plan, training_graph, serving_graph)

    training_candidate: object | None
    serving_candidate: object | None
    try:
        training_candidate = verify_graph(
            training_graph,
            registry=registry,
            limits=limits,
        )
    except Exception:
        training_candidate = None
    _require_comparison_inputs_unchanged(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
    )

    try:
        serving_candidate = verify_graph(
            serving_graph,
            registry=registry,
            limits=limits,
        )
    except Exception:
        serving_candidate = None
    _require_comparison_inputs_unchanged(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
    )
    training_result = _accept_fresh_result(
        training_candidate,
        graph=training_graph,
        registry=registry,
        limits=limits,
        pins=pins,
        side="training",
    )
    serving_result = _accept_fresh_result(
        serving_candidate,
        graph=serving_graph,
        registry=registry,
        limits=limits,
        pins=pins,
        side="serving",
    )
    _require_comparison_inputs_unchanged(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
    )

    if training_result is None or serving_result is None:
        return _finish_result(
            plan,
            training_graph,
            serving_graph,
            registry,
            limits,
            policy,
            pins,
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.VERIFIER_FAILURE,
            training_result=training_result,
            serving_result=serving_result,
            comparisons=(),
        )

    if (
        training_result.status is not VerificationStatus.VERIFIED
        or serving_result.status is not VerificationStatus.VERIFIED
    ):
        reason = (
            ComparisonReason.BOTH_NOT_VERIFIED
            if (
                training_result.status is not VerificationStatus.VERIFIED
                and serving_result.status is not VerificationStatus.VERIFIED
            )
            else ComparisonReason.TRAINING_NOT_VERIFIED
            if training_result.status is not VerificationStatus.VERIFIED
            else ComparisonReason.SERVING_NOT_VERIFIED
        )
        return _finish_result(
            plan,
            training_graph,
            serving_graph,
            registry,
            limits,
            policy,
            pins,
            status=ComparisonStatus.INDETERMINATE,
            reason=reason,
            training_result=training_result,
            serving_result=serving_result,
            comparisons=(),
        )

    comparisons = _compare_bindings(
        plan,
        training_graph,
        serving_graph,
        training_result,
        serving_result,
    )
    status = (
        ComparisonStatus.DRIFT
        if any(item.mismatches for item in comparisons)
        else ComparisonStatus.COMPATIBLE
    )
    return _finish_result(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
        status=status,
        reason=None,
        training_result=training_result,
        serving_result=serving_result,
        comparisons=comparisons,
    )


def _validate_comparison_inputs(
    plan: object,
    training_graph: object,
    serving_graph: object,
    registry: object,
    limits: object,
    policy: object,
) -> tuple[
    ComparisonPlan,
    ComputationGraph,
    ComputationGraph,
    UnitRegistry,
    SolverLimits,
    ComparisonPolicy,
]:
    if type(plan) is not ComparisonPlan:
        raise ComparisonError("comparison requires an exact ComparisonPlan")
    if type(training_graph) is not ComputationGraph:
        raise ComparisonError("training graph must be an exact ComputationGraph")
    if type(serving_graph) is not ComputationGraph:
        raise ComparisonError("serving graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise ComparisonError("comparison registry must be an exact UnitRegistry")
    if type(limits) is not SolverLimits:
        raise ComparisonError("comparison limits must be an exact SolverLimits")
    if type(policy) is not ComparisonPolicy:
        raise ComparisonError("comparison policy must be an exact ComparisonPolicy")
    policy.validate()
    _require_expected_plan_digest(plan, policy)

    try:
        plan.validate()
        training_graph.validate()
        serving_graph.validate()
        registry.validate()
        limits.validate()
    except UnitSentinelError:
        raise ComparisonError("comparison inputs are malformed or mutated") from None
    return plan, training_graph, serving_graph, registry, limits, policy


def _require_expected_plan_digest(
    plan: ComparisonPlan,
    policy: ComparisonPolicy,
) -> None:
    expected = policy.expected_plan_digest
    if expected is None:
        return
    stored = getattr(plan, "_digest", None)
    if type(stored) is not str or not hmac.compare_digest(expected, stored):
        raise ComparisonError(
            "comparison plan does not match the caller-trusted digest pin"
        )


def _pin_comparison_inputs(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    policy: ComparisonPolicy,
) -> _ComparisonPins:
    try:
        solver_version = z3.get_version_string()
        if (
            type(solver_version) is not str
            or SOLVER_VERSION.fullmatch(solver_version) is None
        ):
            raise ComparisonError("current solver version is malformed")
        return _ComparisonPins(
            plan_digest=plan.digest,
            plan_bytes=plan.canonical_bytes(),
            training_graph_digest=training_graph.digest,
            training_graph_bytes=training_graph.canonical_bytes(),
            serving_graph_digest=serving_graph.digest,
            serving_graph_bytes=serving_graph.canonical_bytes(),
            registry_digest=registry.digest,
            registry_bytes=registry.canonical_bytes(),
            limits_bytes=canonical_json_bytes(limits.canonical_record()),
            policy_bytes=canonical_json_bytes(policy.canonical_record()),
            solver_version=solver_version,
        )
    except Exception:
        raise ComparisonError("comparison inputs could not be pinned") from None


def _validate_plan_sources(
    plan: ComparisonPlan,
    pins: _ComparisonPins,
) -> None:
    for label, planned, actual in (
        (
            "training graph",
            plan.training_graph_digest,
            pins.training_graph_digest,
        ),
        (
            "serving graph",
            plan.serving_graph_digest,
            pins.serving_graph_digest,
        ),
        ("registry", plan.registry_digest, pins.registry_digest),
    ):
        if not hmac.compare_digest(planned, actual):
            raise ComparisonError(f"comparison plan {label} digest does not match")


def _public_occurrences(
    graph: ComputationGraph,
) -> tuple[tuple[InterfaceRole, str], ...]:
    return tuple((InterfaceRole.INPUT, value_id) for value_id in graph.inputs) + tuple(
        (InterfaceRole.OUTPUT, value_id) for value_id in graph.outputs
    )


def _validate_plan_coverage(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
) -> None:
    expected_training = set(_public_occurrences(training_graph))
    expected_serving = set(_public_occurrences(serving_graph))
    covered_training: set[tuple[InterfaceRole, str]] = set()
    covered_serving: set[tuple[InterfaceRole, str]] = set()
    for binding in plan.bindings:
        for side, endpoint, expected, covered in (
            (
                "training",
                binding.training,
                expected_training,
                covered_training,
            ),
            (
                "serving",
                binding.serving,
                expected_serving,
                covered_serving,
            ),
        ):
            if endpoint is None:
                continue
            key = (endpoint.role, endpoint.value_id)
            if key not in expected:
                raise ComparisonError(
                    f"{side} endpoint is not a declared public occurrence"
                )
            covered.add(key)
    if covered_training != expected_training:
        raise ComparisonError(
            "comparison plan does not cover every training public occurrence"
        )
    if covered_serving != expected_serving:
        raise ComparisonError(
            "comparison plan does not cover every serving public occurrence"
        )


def _require_comparison_inputs_unchanged(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    policy: ComparisonPolicy,
    pins: _ComparisonPins,
) -> None:
    try:
        plan.validate()
        training_graph.validate()
        serving_graph.validate()
        registry.validate()
        limits.validate()
        policy.validate()
        unchanged = (
            plan.digest == pins.plan_digest
            and plan.canonical_bytes() == pins.plan_bytes
            and training_graph.digest == pins.training_graph_digest
            and training_graph.canonical_bytes() == pins.training_graph_bytes
            and serving_graph.digest == pins.serving_graph_digest
            and serving_graph.canonical_bytes() == pins.serving_graph_bytes
            and registry.digest == pins.registry_digest
            and registry.canonical_bytes() == pins.registry_bytes
            and canonical_json_bytes(limits.canonical_record()) == pins.limits_bytes
            and canonical_json_bytes(policy.canonical_record()) == pins.policy_bytes
        )
    except Exception:
        raise ComparisonError("comparison inputs changed during verification") from None
    if not unchanged:
        raise ComparisonError("comparison inputs changed during verification")


def _validate_fresh_result(
    result: object,
    *,
    graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    pins: _ComparisonPins,
    side: str,
) -> VerificationResult:
    if type(result) is not VerificationResult:
        raise ComparisonError(f"fresh {side} verifier returned an invalid result type")
    try:
        result.validate()
    except Exception:
        raise ComparisonError(
            f"fresh {side} verifier returned a malformed result"
        ) from None
    if (
        result.graph_digest != graph.digest
        or result.registry_digest != pins.registry_digest
        or result.solver_version != pins.solver_version
        or canonical_json_bytes(result.limits.canonical_record())
        != canonical_json_bytes(limits.canonical_record())
    ):
        raise ComparisonError(
            f"fresh {side} verifier returned inconsistent source bindings"
        )
    expected_values = tuple(value.value_id for value in graph.values)
    contract_values = tuple(contract.value_id for contract in result.contracts)
    if result.status is VerificationStatus.VERIFIED:
        if contract_values != expected_values:
            raise ComparisonError(
                f"fresh {side} verified result has incomplete contract coverage"
            )
        try:
            replayed = _replay_claimed_contracts(
                graph,
                registry,
                result.contracts,
            )
            result.validate()
        except Exception:
            raise ComparisonError(
                f"fresh {side} verified contracts could not be replayed"
            ) from None
        if not replayed:
            raise ComparisonError(
                f"fresh {side} verified contracts failed semantic replay"
            )
    elif result.status is VerificationStatus.UNDERCONSTRAINED and (
        set(contract_values).intersection(result.underconstrained_values)
        or tuple(sorted((*contract_values, *result.underconstrained_values)))
        != expected_values
    ):
        raise ComparisonError(
            f"fresh {side} underconstrained result has incomplete coverage"
        )
    return result


def _accept_fresh_result(
    result: object | None,
    *,
    graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    pins: _ComparisonPins,
    side: str,
) -> VerificationResult | None:
    if result is None:
        return None
    try:
        return _validate_fresh_result(
            result,
            graph=graph,
            registry=registry,
            limits=limits,
            pins=pins,
            side=side,
        )
    except ComparisonError:
        return None


def _snapshot(
    endpoint: InterfaceEndpoint,
    graph: ComputationGraph,
    contracts: dict[str, InferredContract],
) -> InterfaceSnapshot:
    sequence = graph.inputs if endpoint.role is InterfaceRole.INPUT else graph.outputs
    return InterfaceSnapshot(
        endpoint=endpoint,
        position=sequence.index(endpoint.value_id),
        value=graph.value(endpoint.value_id),
        inferred=contracts[endpoint.value_id],
    )


def _compare_bindings(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    training_result: VerificationResult,
    serving_result: VerificationResult,
) -> tuple[ContractComparison, ...]:
    training_contracts = {
        contract.value_id: contract for contract in training_result.contracts
    }
    serving_contracts = {
        contract.value_id: contract for contract in serving_result.contracts
    }
    comparisons: list[ContractComparison] = []
    for binding in plan.bindings:
        training = (
            None
            if binding.training is None
            else _snapshot(
                binding.training,
                training_graph,
                training_contracts,
            )
        )
        serving = (
            None
            if binding.serving is None
            else _snapshot(
                binding.serving,
                serving_graph,
                serving_contracts,
            )
        )
        comparisons.append(
            ContractComparison(
                contract_id=binding.contract_id,
                training=training,
                serving=serving,
                mismatches=_expected_mismatches(training, serving),
            )
        )
    return tuple(comparisons)


def _finish_result(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    policy: ComparisonPolicy,
    pins: _ComparisonPins,
    *,
    status: ComparisonStatus,
    reason: ComparisonReason | None,
    training_result: VerificationResult | None,
    serving_result: VerificationResult | None,
    comparisons: tuple[ContractComparison, ...],
) -> ComparisonResult:
    _require_comparison_inputs_unchanged(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
    )
    accepted_training = _accept_fresh_result(
        training_result,
        graph=training_graph,
        registry=registry,
        limits=limits,
        pins=pins,
        side="training",
    )
    accepted_serving = _accept_fresh_result(
        serving_result,
        graph=serving_graph,
        registry=registry,
        limits=limits,
        pins=pins,
        side="serving",
    )
    if accepted_training is None or accepted_serving is None:
        status = ComparisonStatus.INDETERMINATE
        reason = ComparisonReason.VERIFIER_FAILURE
        comparisons = ()
    result = ComparisonResult(
        status=status,
        reason=reason,
        comparison_id=plan.comparison_id,
        plan_digest=pins.plan_digest,
        training_graph_digest=pins.training_graph_digest,
        serving_graph_digest=pins.serving_graph_digest,
        registry_digest=pins.registry_digest,
        limits=limits,
        training_result=accepted_training,
        serving_result=accepted_serving,
        comparisons=comparisons,
    )
    _require_comparison_inputs_unchanged(
        plan,
        training_graph,
        serving_graph,
        registry,
        limits,
        policy,
        pins,
    )
    result.validate()
    return result
