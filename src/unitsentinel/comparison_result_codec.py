"""Strict canonical JSON codec for bounded comparison-result claims.

Decoding establishes structural and content-address integrity only. Comparison
results and their embedded verification and lineage records are unsigned
detached claims; successful decoding does not authenticate them or prove that
either verifier run was fresh.
"""

from __future__ import annotations

import hmac
from enum import StrEnum
from typing import Final, TypeVar, cast

from .certificate import (
    MAX_CERTIFICATE_CHECKS,
    MAX_CERTIFICATE_CONSTRAINTS,
    MAX_CERTIFICATE_SOLVER_VERSION_LENGTH,
    _decode_constraint,
    _decode_contract,
    _decode_limits,
)
from .comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_RESULT_SCHEMA,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonError,
    ComparisonReason,
    ComparisonResult,
    ComparisonStatus,
    ContractComparison,
    InterfaceSnapshot,
    MismatchCode,
    OutputNormalizationComparison,
)
from .comparison_contract import (
    MAX_COMPARISON_BINDINGS,
    InterfaceEndpoint,
    InterfaceRole,
)
from .domain import BASE_DIMENSION_COUNT, UnitSentinelError
from .graph import (
    BINARY_OPERATIONS,
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    MAX_GRAPH_VALUES,
    MAX_TENSOR_RANK,
    Operation,
    ValueSpec,
)
from .graph_codec import _decode_value
from .json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
)
from .lineage import (
    LINEAGE_AUTHENTICATION,
    NORMALIZATION_LINEAGE_SCHEMA,
    NORMALIZATION_SITE_SCHEMA,
    LineageExpression,
    LineageSide,
    NormalizationLineage,
    NormalizationSite,
    OutputLineage,
)
from .registry import SHA256_HEX
from .verification import (
    InferredContract,
    UnknownReason,
    VerificationResult,
    VerificationStatus,
)

_EnumT = TypeVar("_EnumT", bound=StrEnum)

# A stress result at the graph-count ceilings (512 nodes, 385 sites, 64 outputs)
# measures 7,538,814 bytes. The reproducible shape-only envelope in
# ``tools.measure_comparison_result_boundary`` measures 24,402,018 bytes and
# 779,409 preflight tokens, so the next powers of two provide bounded headroom.
# The largest model-backed array is one witness per graph constraint.
MAX_COMPARISON_RESULT_BYTES: Final = 33_554_432
MAX_COMPARISON_RESULT_JSON_DEPTH: Final = 10
MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS: Final = MAX_CERTIFICATE_CONSTRAINTS
MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES: Final = 1_048_576
MAX_COMPARISON_RESULT_JSON_STRING_LENGTH: Final = 192
MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS: Final = 10
_COMPARISON_RESULT_JSON_LIMITS: Final = CanonicalJSONLimits(
    max_bytes=MAX_COMPARISON_RESULT_BYTES,
    max_depth=MAX_COMPARISON_RESULT_JSON_DEPTH,
    max_container_items=MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS,
    max_total_values=MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES,
    max_string_length=MAX_COMPARISON_RESULT_JSON_STRING_LENGTH,
    max_integer_digits=MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS,
)

_ROOT_FIELDS: Final = frozenset(
    {
        "authentication",
        "bindings",
        "comparison_id",
        "graphs",
        "limits",
        "normalization_lineage",
        "plan_sha256",
        "reason",
        "registry_sha256",
        "schema",
        "scope",
        "status",
        "verification",
    }
)
_GRAPH_FIELDS: Final = frozenset({"serving_sha256", "training_sha256"})
_SIDES_FIELDS: Final = frozenset({"serving", "training"})
_DETACHED_FIELDS: Final = frozenset({"record", "sha256"})
_VERIFICATION_FIELDS: Final = frozenset(
    {
        "checks_performed",
        "conflict_core",
        "contracts",
        "core_minimal",
        "graph_digest",
        "limits",
        "registry_digest",
        "solver_version",
        "status",
        "underconstrained_values",
        "unknown_reason",
    }
)
_BINDING_FIELDS: Final = frozenset(
    {"contract_id", "mismatches", "normalization", "serving", "training"}
)
_NORMALIZATION_COMPARISON_FIELDS: Final = frozenset(
    {"serving_sha256", "training_sha256"}
)
_SNAPSHOT_FIELDS: Final = frozenset({"endpoint", "inferred", "position", "value"})
_ENDPOINT_FIELDS: Final = frozenset({"role", "value_id"})
_LINEAGE_FIELDS: Final = frozenset(
    {
        "authentication",
        "comparison_id",
        "expressions",
        "graph_sha256",
        "limits",
        "outputs",
        "plan_sha256",
        "registry_sha256",
        "schema",
        "semantic_sha256",
        "side",
        "sites",
        "verification",
    }
)
_EXPRESSION_FIELDS: Final = frozenset(
    {
        "attributes",
        "children_sha256",
        "collapsed_identity",
        "inferred",
        "input_value_ids",
        "logical_roots",
        "node_id",
        "operation",
        "semantic_sha256",
        "value",
        "value_id",
    }
)
_SITE_FIELDS: Final = frozenset(
    {
        "expression_sha256",
        "logical_outputs",
        "logical_roots",
        "node_id",
        "schema",
        "site_sha256",
        "value_id",
    }
)
_OUTPUT_FIELDS: Final = frozenset(
    {
        "contract_id",
        "expression_sha256",
        "normalization_sha256",
        "position",
        "site_sha256_multiset",
        "value_id",
    }
)
_MULTISET_FIELDS: Final = frozenset({"count", "sha256"})
_VALUE_FIELDS: Final = frozenset({"dtype", "shape", "unit_id", "value_id"})
_CONTRACT_FIELDS: Final = frozenset(
    {"dimension", "kind", "offset", "scale", "value_id"}
)


class ComparisonResultDecodeError(ComparisonError):
    """Raised when bytes fail the bounded comparison-result contract."""


def encode_comparison_result(result: ComparisonResult) -> bytes:
    """Return canonical bytes for one exact, currently valid result claim."""

    if type(result) is not ComparisonResult:
        raise ComparisonResultDecodeError(
            "comparison-result encoder requires an exact ComparisonResult"
        )
    try:
        payload = result.canonical_bytes()
        decode_comparison_result(payload)
    except UnitSentinelError as error:
        raise ComparisonResultDecodeError(
            f"comparison-result encoding failed: {error}"
        ) from None
    return payload


def decode_comparison_result(payload: bytes) -> ComparisonResult:
    """Decode one internally coherent unsigned v1 claim from untrusted bytes."""

    try:
        parsed = decode_canonical_json(
            payload,
            limits=_COMPARISON_RESULT_JSON_LIMITS,
            label="comparison-result",
        )
    except CanonicalJSONError as error:
        raise ComparisonResultDecodeError(str(error)) from None

    try:
        result = _decode_result(parsed)
    except UnitSentinelError as error:
        raise ComparisonResultDecodeError(
            f"comparison-result semantic contract failed: {error}"
        ) from None
    if result.canonical_bytes() != payload:
        raise ComparisonResultDecodeError(
            "comparison-result payload does not match the canonical result model"
        )
    return result


def _decode_result(value: object) -> ComparisonResult:
    root = _expect_object(value, _ROOT_FIELDS, label="comparison-result document")
    _expect_literal(
        root["schema"],
        COMPARISON_RESULT_SCHEMA,
        label="comparison-result schema",
    )
    _expect_literal(
        root["authentication"],
        AUTHENTICATION_NOT_PROVIDED,
        label="comparison-result authentication",
    )
    _expect_literal(
        root["scope"],
        COMPARISON_SCOPE_UNDER_PLAN,
        label="comparison-result scope",
    )

    graphs = _expect_object(root["graphs"], _GRAPH_FIELDS, label="graph bindings")
    verification = _expect_object(
        root["verification"],
        _SIDES_FIELDS,
        label="verification claims",
    )
    lineages = _expect_object(
        root["normalization_lineage"],
        _SIDES_FIELDS,
        label="normalization-lineage claims",
    )
    status = _decode_enum(
        root["status"],
        ComparisonStatus,
        label="comparison-result status",
    )
    reason = _decode_optional_enum(
        root["reason"],
        ComparisonReason,
        label="comparison-result reason",
    )
    bindings = _expect_bounded_array(
        root["bindings"],
        max_items=MAX_COMPARISON_BINDINGS,
        label="result bindings",
    )

    return ComparisonResult(
        status=status,
        reason=reason,
        comparison_id=_expect_text(
            root["comparison_id"],
            label="comparison identifier",
        ),
        plan_digest=_expect_sha256(
            root["plan_sha256"],
            label="comparison plan digest",
        ),
        training_graph_digest=_expect_sha256(
            graphs["training_sha256"],
            label="training graph digest",
        ),
        serving_graph_digest=_expect_sha256(
            graphs["serving_sha256"],
            label="serving graph digest",
        ),
        registry_digest=_expect_sha256(
            root["registry_sha256"],
            label="registry digest",
        ),
        limits=_decode_limits(root["limits"]),
        training_result=_decode_optional_verification_claim(
            verification["training"],
            label="training verification claim",
        ),
        serving_result=_decode_optional_verification_claim(
            verification["serving"],
            label="serving verification claim",
        ),
        training_lineage=_decode_optional_lineage_claim(
            lineages["training"],
            label="training lineage claim",
        ),
        serving_lineage=_decode_optional_lineage_claim(
            lineages["serving"],
            label="serving lineage claim",
        ),
        comparisons=tuple(_decode_contract_comparison(item) for item in bindings),
    )


def _decode_optional_verification_claim(
    value: object,
    *,
    label: str,
) -> VerificationResult | None:
    if value is None:
        return None
    return _decode_verification_claim(value, label=label)


def _decode_verification_claim(
    value: object,
    *,
    label: str,
) -> VerificationResult:
    claim = _expect_object(value, _DETACHED_FIELDS, label=label)
    claimed_digest = _expect_sha256(
        claim["sha256"],
        label=f"{label} digest",
    )
    result = _decode_verification_record(claim["record"])
    _require_matching_digest(
        claimed_digest,
        result.digest,
        label=f"{label} digest",
    )
    return result


def _decode_verification_record(value: object) -> VerificationResult:
    record = _expect_object(
        value,
        _VERIFICATION_FIELDS,
        label="verification-result record",
    )
    status = _decode_enum(
        record["status"],
        VerificationStatus,
        label="verification status",
    )
    checks_performed = _expect_integer(
        record["checks_performed"],
        label="solver check count",
    )
    solver_version = _expect_text(
        record["solver_version"],
        label="solver version",
    )
    if len(solver_version) > MAX_CERTIFICATE_SOLVER_VERSION_LENGTH:
        raise ComparisonResultDecodeError("solver version exceeds the length limit")
    contracts = _expect_bounded_array(
        record["contracts"],
        max_items=MAX_GRAPH_VALUES,
        label="inferred contracts",
    )
    underconstrained = _expect_bounded_array(
        record["underconstrained_values"],
        max_items=MAX_GRAPH_VALUES,
        label="underconstrained values",
    )
    conflict_core = _expect_bounded_array(
        record["conflict_core"],
        max_items=MAX_CERTIFICATE_CONSTRAINTS,
        label="conflict core",
    )
    if checks_performed > MAX_CERTIFICATE_CHECKS:
        raise ComparisonResultDecodeError(
            "solver check count exceeds the fresh-result limit"
        )
    decoded_conflict_core = tuple(_decode_constraint(item) for item in conflict_core)
    return VerificationResult(
        status=status,
        graph_digest=_expect_sha256(
            record["graph_digest"],
            label="verification graph digest",
        ),
        registry_digest=_expect_sha256(
            record["registry_digest"],
            label="verification registry digest",
        ),
        solver_version=solver_version,
        limits=_decode_limits(record["limits"]),
        checks_performed=checks_performed,
        contracts=tuple(_decode_bounded_contract(item) for item in contracts),
        underconstrained_values=_decode_text_array(
            underconstrained,
            label="underconstrained values",
        ),
        conflict_core=decoded_conflict_core,
        core_minimal=_expect_optional_boolean(
            record["core_minimal"],
            label="core-minimal flag",
        ),
        unknown_reason=_decode_optional_enum(
            record["unknown_reason"],
            UnknownReason,
            label="unknown verification reason",
        ),
    )


def _decode_optional_lineage_claim(
    value: object,
    *,
    label: str,
) -> NormalizationLineage | None:
    if value is None:
        return None
    claim = _expect_object(value, _DETACHED_FIELDS, label=label)
    claimed_digest = _expect_sha256(
        claim["sha256"],
        label=f"{label} digest",
    )
    lineage = _decode_lineage_record(claim["record"])
    _require_matching_digest(
        claimed_digest,
        lineage.digest,
        label=f"{label} digest",
    )
    return lineage


def _decode_lineage_record(value: object) -> NormalizationLineage:
    record = _expect_object(
        value,
        _LINEAGE_FIELDS,
        label="normalization-lineage record",
    )
    _expect_literal(
        record["schema"],
        NORMALIZATION_LINEAGE_SCHEMA,
        label="normalization-lineage schema",
    )
    _expect_literal(
        record["authentication"],
        LINEAGE_AUTHENTICATION,
        label="normalization-lineage authentication",
    )
    claimed_semantic_digest = _expect_sha256(
        record["semantic_sha256"],
        label="normalization-lineage semantic digest",
    )
    expressions = _expect_bounded_array(
        record["expressions"],
        max_items=MAX_GRAPH_VALUES,
        label="lineage expressions",
    )
    sites = _expect_bounded_array(
        record["sites"],
        max_items=MAX_GRAPH_NODES,
        label="normalization sites",
    )
    outputs = _expect_bounded_array(
        record["outputs"],
        max_items=MAX_GRAPH_OUTPUTS,
        label="output lineages",
    )
    lineage = NormalizationLineage(
        side=_decode_enum(
            record["side"],
            LineageSide,
            label="normalization-lineage side",
        ),
        comparison_id=_expect_text(
            record["comparison_id"],
            label="lineage comparison identifier",
        ),
        plan_digest=_expect_sha256(
            record["plan_sha256"],
            label="lineage plan digest",
        ),
        graph_digest=_expect_sha256(
            record["graph_sha256"],
            label="lineage graph digest",
        ),
        registry_digest=_expect_sha256(
            record["registry_sha256"],
            label="lineage registry digest",
        ),
        limits=_decode_limits(record["limits"]),
        verification_result=_decode_verification_claim(
            record["verification"],
            label="lineage verification claim",
        ),
        expressions=tuple(_decode_expression_claim(item) for item in expressions),
        sites=tuple(_decode_site_claim(item) for item in sites),
        outputs=tuple(_decode_output_claim(item) for item in outputs),
    )
    _require_matching_digest(
        claimed_semantic_digest,
        lineage.semantic_digest,
        label="normalization-lineage semantic digest",
    )
    return lineage


def _decode_expression_claim(value: object) -> LineageExpression:
    claim = _expect_object(
        value,
        _DETACHED_FIELDS,
        label="lineage expression claim",
    )
    claimed_digest = _expect_sha256(
        claim["sha256"],
        label="lineage expression digest",
    )
    record = _expect_object(
        claim["record"],
        _EXPRESSION_FIELDS,
        label="lineage expression record",
    )
    operation = _decode_optional_enum(
        record["operation"],
        Operation,
        label="lineage expression operation",
    )
    claimed_semantic_digest = _expect_sha256(
        record["semantic_sha256"],
        label="lineage expression semantic digest",
    )
    input_value_ids = _expect_array(
        record["input_value_ids"],
        label="lineage input identifiers",
    )
    child_digests = _expect_array(
        record["children_sha256"],
        label="lineage child digests",
    )
    expected_arity = (
        0 if operation is None else 2 if operation in BINARY_OPERATIONS else 1
    )
    if len(input_value_ids) != expected_arity or len(child_digests) != expected_arity:
        raise ComparisonResultDecodeError(
            "lineage expression inputs do not match its operation arity"
        )
    logical_roots = _expect_bounded_array(
        record["logical_roots"],
        max_items=MAX_GRAPH_INPUTS,
        label="lineage logical roots",
    )
    expression = LineageExpression(
        value_id=_expect_text(
            record["value_id"],
            label="lineage expression value identifier",
        ),
        node_id=_expect_optional_text(
            record["node_id"],
            label="lineage expression node identifier",
        ),
        operation=operation,
        attributes=_decode_expression_attributes(
            record["attributes"],
            operation=operation,
        ),
        input_value_ids=_decode_text_array(
            input_value_ids,
            label="lineage input identifiers",
        ),
        child_digests=_decode_digest_array(
            child_digests,
            label="lineage child digests",
        ),
        logical_roots=_decode_text_array(
            logical_roots,
            label="lineage logical roots",
        ),
        collapsed_identity=_expect_boolean(
            record["collapsed_identity"],
            label="collapsed-identity flag",
        ),
        value=_decode_bounded_value(record["value"]),
        inferred=_decode_bounded_contract(record["inferred"]),
    )
    _require_matching_digest(
        claimed_semantic_digest,
        expression.semantic_digest,
        label="lineage expression semantic digest",
    )
    _require_matching_digest(
        claimed_digest,
        expression.digest,
        label="lineage expression digest",
    )
    return expression


def _decode_expression_attributes(
    value: object,
    *,
    operation: Operation | None,
) -> tuple[tuple[str, str], ...]:
    fields = (
        frozenset({"exponent"})
        if operation is Operation.POWER
        else frozenset({"unit_id"})
        if operation is Operation.CONVERT
        else frozenset()
    )
    record = _expect_object(value, fields, label="lineage expression attributes")
    return tuple(
        (key, _expect_text(item, label=f"lineage {key} attribute"))
        for key, item in sorted(record.items())
    )


def _decode_site_claim(value: object) -> NormalizationSite:
    claim = _expect_object(
        value,
        _DETACHED_FIELDS,
        label="normalization-site claim",
    )
    claimed_digest = _expect_sha256(
        claim["sha256"],
        label="normalization diagnostic digest",
    )
    record = _expect_object(
        claim["record"],
        _SITE_FIELDS,
        label="normalization-site record",
    )
    _expect_literal(
        record["schema"],
        NORMALIZATION_SITE_SCHEMA,
        label="normalization-site schema",
    )
    claimed_site_digest = _expect_sha256(
        record["site_sha256"],
        label="normalization-site semantic digest",
    )
    logical_roots = _expect_bounded_array(
        record["logical_roots"],
        max_items=MAX_GRAPH_INPUTS,
        label="normalization logical roots",
    )
    logical_outputs = _expect_bounded_array(
        record["logical_outputs"],
        max_items=MAX_GRAPH_OUTPUTS,
        label="normalization logical outputs",
    )
    site = NormalizationSite(
        node_id=_expect_text(
            record["node_id"],
            label="normalization node identifier",
        ),
        value_id=_expect_text(
            record["value_id"],
            label="normalization value identifier",
        ),
        expression_digest=_expect_sha256(
            record["expression_sha256"],
            label="normalization expression digest",
        ),
        logical_roots=_decode_text_array(
            logical_roots,
            label="normalization logical roots",
        ),
        logical_outputs=_decode_text_array(
            logical_outputs,
            label="normalization logical outputs",
        ),
    )
    _require_matching_digest(
        claimed_site_digest,
        site.site_digest,
        label="normalization-site semantic digest",
    )
    _require_matching_digest(
        claimed_digest,
        site.digest,
        label="normalization diagnostic digest",
    )
    return site


def _decode_output_claim(value: object) -> OutputLineage:
    claim = _expect_object(
        value,
        _DETACHED_FIELDS,
        label="output-lineage claim",
    )
    claimed_digest = _expect_sha256(
        claim["sha256"],
        label="output-lineage digest",
    )
    record = _expect_object(
        claim["record"],
        _OUTPUT_FIELDS,
        label="output-lineage record",
    )
    claimed_normalization_digest = _expect_sha256(
        record["normalization_sha256"],
        label="output normalization digest",
    )
    output = OutputLineage(
        contract_id=_expect_text(
            record["contract_id"],
            label="output logical contract identifier",
        ),
        value_id=_expect_text(
            record["value_id"],
            label="output value identifier",
        ),
        position=_expect_integer(
            record["position"],
            label="output position",
        ),
        expression_digest=_expect_sha256(
            record["expression_sha256"],
            label="output expression digest",
        ),
        site_digests=_decode_digest_multiset(
            record["site_sha256_multiset"],
            label="output site digest multiset",
        ),
    )
    _require_matching_digest(
        claimed_normalization_digest,
        output.normalization_digest,
        label="output normalization digest",
    )
    _require_matching_digest(
        claimed_digest,
        output.digest,
        label="output-lineage digest",
    )
    return output


def _decode_digest_multiset(
    value: object,
    *,
    label: str,
) -> tuple[str, ...]:
    records = _expect_bounded_array(
        value,
        max_items=MAX_GRAPH_NODES,
        label=label,
    )
    digests: list[str] = []
    previous: str | None = None
    for item in records:
        record = _expect_object(item, _MULTISET_FIELDS, label=f"{label} entry")
        count = _expect_integer(record["count"], label=f"{label} count")
        if count < 1:
            raise ComparisonResultDecodeError(f"{label} count must be positive")
        digest = _expect_sha256(record["sha256"], label=f"{label} digest")
        if previous is not None and digest <= previous:
            raise ComparisonResultDecodeError(
                f"{label} entries must be sorted and unique"
            )
        if len(digests) + count > MAX_GRAPH_NODES:
            raise ComparisonResultDecodeError(f"{label} exceeds the graph node limit")
        digests.extend((digest,) * count)
        previous = digest
    return tuple(digests)


def _decode_contract_comparison(value: object) -> ContractComparison:
    record = _expect_object(
        value,
        _BINDING_FIELDS,
        label="contract comparison",
    )
    normalization_value = record["normalization"]
    normalization = (
        None
        if normalization_value is None
        else _decode_output_normalization(normalization_value)
    )
    training_value = record["training"]
    serving_value = record["serving"]
    mismatches = _expect_bounded_array(
        record["mismatches"],
        max_items=len(MismatchCode),
        label="comparison mismatch codes",
    )
    return ContractComparison(
        contract_id=_expect_text(
            record["contract_id"],
            label="comparison contract identifier",
        ),
        training=(
            None
            if training_value is None
            else _decode_snapshot(training_value, side="training")
        ),
        serving=(
            None
            if serving_value is None
            else _decode_snapshot(serving_value, side="serving")
        ),
        normalization=normalization,
        mismatches=tuple(
            _decode_enum(item, MismatchCode, label="comparison mismatch code")
            for item in mismatches
        ),
    )


def _decode_output_normalization(value: object) -> OutputNormalizationComparison:
    record = _expect_object(
        value,
        _NORMALIZATION_COMPARISON_FIELDS,
        label="output normalization comparison",
    )
    return OutputNormalizationComparison(
        training_digest=_expect_sha256(
            record["training_sha256"],
            label="training output normalization digest",
        ),
        serving_digest=_expect_sha256(
            record["serving_sha256"],
            label="serving output normalization digest",
        ),
    )


def _decode_snapshot(value: object, *, side: str) -> InterfaceSnapshot:
    record = _expect_object(
        value,
        _SNAPSHOT_FIELDS,
        label=f"{side} interface snapshot",
    )
    return InterfaceSnapshot(
        endpoint=_decode_endpoint(record["endpoint"], side=side),
        position=_expect_integer(
            record["position"],
            label=f"{side} interface position",
        ),
        value=_decode_bounded_value(record["value"]),
        inferred=_decode_bounded_contract(record["inferred"]),
    )


def _decode_endpoint(value: object, *, side: str) -> InterfaceEndpoint:
    record = _expect_object(
        value,
        _ENDPOINT_FIELDS,
        label=f"{side} endpoint",
    )
    return InterfaceEndpoint(
        role=_decode_enum(
            record["role"],
            InterfaceRole,
            label=f"{side} endpoint role",
        ),
        value_id=_expect_text(
            record["value_id"],
            label=f"{side} endpoint value identifier",
        ),
    )


def _decode_bounded_value(value: object) -> ValueSpec:
    record = _expect_object(value, _VALUE_FIELDS, label="value declaration")
    _expect_bounded_array(
        record["shape"],
        max_items=MAX_TENSOR_RANK,
        label="value shape",
    )
    return _decode_value(value)


def _decode_bounded_contract(value: object) -> InferredContract:
    record = _expect_object(value, _CONTRACT_FIELDS, label="inferred contract")
    _expect_bounded_array(
        record["dimension"],
        max_items=BASE_DIMENSION_COUNT,
        label="inferred dimension",
    )
    return _decode_contract(value)


def _expect_object(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ComparisonResultDecodeError(f"{label} must be an object")
    record = cast(dict[str, object], value)
    if set(record) != fields:
        raise ComparisonResultDecodeError(f"{label} has missing or unknown fields")
    return record


def _expect_array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise ComparisonResultDecodeError(f"{label} must be an array")
    return cast(list[object], value)


def _expect_bounded_array(
    value: object,
    *,
    max_items: int,
    label: str,
) -> list[object]:
    items = _expect_array(value, label=label)
    if len(items) > max_items:
        raise ComparisonResultDecodeError(f"{label} exceeds its item limit")
    return items


def _expect_text(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ComparisonResultDecodeError(f"{label} must be text")
    return value


def _expect_optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _expect_text(value, label=label)


def _expect_integer(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ComparisonResultDecodeError(f"{label} must be an exact integer")
    return value


def _expect_boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ComparisonResultDecodeError(f"{label} must be an exact boolean")
    return value


def _expect_optional_boolean(value: object, *, label: str) -> bool | None:
    if value is None:
        return None
    return _expect_boolean(value, label=label)


def _expect_sha256(value: object, *, label: str) -> str:
    digest = _expect_text(value, label=label)
    if SHA256_HEX.fullmatch(digest) is None:
        raise ComparisonResultDecodeError(f"{label} is malformed")
    return digest


def _expect_literal(value: object, expected: str, *, label: str) -> None:
    if _expect_text(value, label=label) != expected:
        raise ComparisonResultDecodeError(f"{label} is not supported")


def _decode_text_array(value: object, *, label: str) -> tuple[str, ...]:
    return tuple(
        _expect_text(item, label=f"{label} entry")
        for item in _expect_array(value, label=label)
    )


def _decode_digest_array(value: object, *, label: str) -> tuple[str, ...]:
    return tuple(
        _expect_sha256(item, label=f"{label} entry")
        for item in _expect_array(value, label=label)
    )


def _decode_enum(
    value: object,
    enum_type: type[_EnumT],
    *,
    label: str,
) -> _EnumT:
    text = _expect_text(value, label=label)
    try:
        return enum_type(text)
    except ValueError:
        raise ComparisonResultDecodeError(f"{label} is not supported") from None


def _decode_optional_enum(
    value: object,
    enum_type: type[_EnumT],
    *,
    label: str,
) -> _EnumT | None:
    if value is None:
        return None
    return _decode_enum(value, enum_type, label=label)


def _require_matching_digest(claimed: str, actual: str, *, label: str) -> None:
    if not hmac.compare_digest(claimed, actual):
        raise ComparisonResultDecodeError(f"{label} does not match its contents")
