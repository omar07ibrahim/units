"""Strict canonical JSON codec for bounded training-serving comparison plans."""

from __future__ import annotations

from typing import cast

from .comparison_contract import (
    COMPARISON_SCHEMA,
    ComparisonContractError,
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from .domain import UnitSentinelError
from .json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
)

MAX_COMPARISON_BYTES = 131_072
MAX_COMPARISON_JSON_DEPTH = 6
MAX_COMPARISON_JSON_CONTAINER_ITEMS = 256
MAX_COMPARISON_JSON_TOTAL_VALUES = 4_096
MAX_COMPARISON_JSON_STRING_LENGTH = 192
MAX_COMPARISON_JSON_INTEGER_DIGITS = 10
_COMPARISON_JSON_LIMITS = CanonicalJSONLimits(
    max_bytes=MAX_COMPARISON_BYTES,
    max_depth=MAX_COMPARISON_JSON_DEPTH,
    max_container_items=MAX_COMPARISON_JSON_CONTAINER_ITEMS,
    max_total_values=MAX_COMPARISON_JSON_TOTAL_VALUES,
    max_string_length=MAX_COMPARISON_JSON_STRING_LENGTH,
    max_integer_digits=MAX_COMPARISON_JSON_INTEGER_DIGITS,
)

ROOT_FIELDS = frozenset(
    {
        "bindings",
        "comparison_id",
        "registry_digest",
        "schema",
        "serving_graph_digest",
        "training_graph_digest",
    }
)
BINDING_FIELDS = frozenset({"contract_id", "serving", "training"})
ENDPOINT_FIELDS = frozenset({"role", "value_id"})


class ComparisonDecodeError(ComparisonContractError):
    """Raised when plan bytes are unsafe, noncanonical, or semantically invalid."""


def encode_comparison_plan(plan: ComparisonPlan) -> bytes:
    """Return canonical bytes for one exact, currently valid comparison plan."""

    if type(plan) is not ComparisonPlan:
        raise ComparisonDecodeError(
            "comparison encoder requires an exact ComparisonPlan"
        )
    try:
        return plan.canonical_bytes()
    except UnitSentinelError as error:
        raise ComparisonDecodeError(f"comparison encoding failed: {error}") from None


def decode_comparison_plan(payload: bytes) -> ComparisonPlan:
    """Decode byte-for-byte canonical v1 JSON without coercion or extensions."""

    try:
        parsed = decode_canonical_json(
            payload,
            limits=_COMPARISON_JSON_LIMITS,
            label="comparison",
        )
    except CanonicalJSONError as error:
        raise ComparisonDecodeError(str(error)) from None

    plan = _decode_semantic_plan(parsed)
    if plan.canonical_bytes() != payload:
        raise ComparisonDecodeError(
            "comparison payload does not match the canonical comparison model"
        )
    return plan


def _decode_semantic_plan(value: object) -> ComparisonPlan:
    root = _expect_object(value, ROOT_FIELDS, label="comparison document")
    schema = _expect_string(root["schema"], label="comparison schema")
    if schema != COMPARISON_SCHEMA:
        raise ComparisonDecodeError("comparison schema is not supported")

    try:
        return ComparisonPlan(
            comparison_id=_expect_string(
                root["comparison_id"],
                label="comparison identifier",
            ),
            training_graph_digest=_expect_string(
                root["training_graph_digest"],
                label="training graph digest",
            ),
            serving_graph_digest=_expect_string(
                root["serving_graph_digest"],
                label="serving graph digest",
            ),
            registry_digest=_expect_string(
                root["registry_digest"],
                label="registry digest",
            ),
            bindings=tuple(
                _decode_binding(item)
                for item in _expect_array(
                    root["bindings"],
                    label="comparison bindings",
                )
            ),
        )
    except ComparisonDecodeError:
        raise
    except UnitSentinelError as error:
        raise ComparisonDecodeError(
            f"comparison semantic contract failed: {error}"
        ) from None


def _decode_binding(value: object) -> ContractBinding:
    record = _expect_object(value, BINDING_FIELDS, label="contract binding")
    return ContractBinding(
        contract_id=_expect_string(
            record["contract_id"],
            label="contract identifier",
        ),
        training=_decode_optional_endpoint(
            record["training"],
            side="training",
        ),
        serving=_decode_optional_endpoint(
            record["serving"],
            side="serving",
        ),
    )


def _decode_optional_endpoint(
    value: object,
    *,
    side: str,
) -> InterfaceEndpoint | None:
    if value is None:
        return None
    record = _expect_object(
        value,
        ENDPOINT_FIELDS,
        label=f"{side} endpoint",
    )
    role_text = _expect_string(
        record["role"],
        label=f"{side} endpoint role",
    )
    try:
        role = InterfaceRole(role_text)
    except ValueError:
        raise ComparisonDecodeError(f"{side} endpoint role is unsupported") from None
    return InterfaceEndpoint(
        role=role,
        value_id=_expect_string(
            record["value_id"],
            label=f"{side} interface value identifier",
        ),
    )


def _expect_object(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ComparisonDecodeError(f"{label} must be an object")
    record = cast(dict[str, object], value)
    if set(record) != fields:
        raise ComparisonDecodeError(f"{label} has missing or unknown fields")
    return record


def _expect_array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise ComparisonDecodeError(f"{label} must be an array")
    return cast(list[object], value)


def _expect_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ComparisonDecodeError(f"{label} must be text")
    return value
