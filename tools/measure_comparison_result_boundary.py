"""Measure the conservative comparison-result JSON transport envelope.

This tool deliberately builds a shape-only JSON document.  It combines
independent collection and metadata ceilings that cannot all occur in one
valid :class:`unitsentinel.comparison.ComparisonResult`.  The measurement is
therefore useful for transport-budget headroom, but it is neither a valid
claim fixture nor a proof of the exact largest possible result.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

from unitsentinel.canonical import canonical_json_bytes, sha256_hex
from unitsentinel.comparison_result_codec import (
    MAX_COMPARISON_RESULT_BYTES,
    MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS,
    MAX_COMPARISON_RESULT_JSON_DEPTH,
    MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS,
    MAX_COMPARISON_RESULT_JSON_STRING_LENGTH,
    MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES,
)

_SCHEMA: Final = "unitsentinel.comparison-result-boundary-measurement/v1"
_DIGEST: Final = "f" * 64
_IDENTIFIER: Final = "a" + ("b" * 63)
_AXIS: Final = "a" + ("b" * 31)
_RATIONAL: Final = "-" + ("9" * 78) + "/" + ("8" * 78)


@dataclass(frozen=True, slots=True)
class JSONStructureMeasurement:
    """Iteratively measured properties of one JSON-compatible tree."""

    tree_values_excluding_object_keys: int
    object_key_tokens: int
    preflight_tokens_including_object_keys: int
    maximum_depth: int
    maximum_container_items: int
    maximum_string_length: int
    maximum_integer_digits: int


def _inferred_record(dimension: list[object]) -> dict[str, object]:
    return {
        "dimension": dimension,
        "kind": "absolute-temperature",
        "offset": _RATIONAL,
        "scale": _RATIONAL,
        "value_id": _IDENTIFIER,
    }


def _value_record(shape: list[object]) -> dict[str, object]:
    return {
        "dtype": "bfloat16",
        "shape": shape,
        "unit_id": _IDENTIFIER,
        "value_id": _IDENTIFIER,
    }


def _detached(record: object) -> dict[str, object]:
    return {"record": record, "sha256": _DIGEST}


def build_conservative_shape_only_envelope() -> dict[str, object]:
    """Return the intentionally non-constructible independent-field envelope.

    Repeated Python references keep the generator's peak memory modest.  JSON
    has no reference identity, so canonical serialization still contains and
    measures every repeated record independently.
    """

    dimension_entry: dict[str, object] = {
        "base": "thermodynamic-temperature",
        "exponent": "-64/11",
    }
    dimension: list[object] = [dimension_entry] * 7
    limits: dict[str, object] = {
        "max_core_shrink_checks": 1024,
        "max_memory_mb": 4096,
        "max_uniqueness_checks": 1024,
        "per_check_timeout_ms": 10000,
        "total_timeout_ms": 60000,
    }
    inferred = _inferred_record(dimension)
    value = _value_record([_AXIS] * 8)

    verification_record: dict[str, object] = {
        "checks_performed": 1025,
        "conflict_core": [],
        "contracts": [inferred] * 576,
        "core_minimal": None,
        "graph_digest": _DIGEST,
        "limits": limits,
        "registry_digest": _DIGEST,
        "solver_version": ("9" * 32) + "." + ("9" * 32) + "." + ("9" * 32),
        "status": "verified",
        "underconstrained_values": [],
        "unknown_reason": None,
    }
    verification_claim = _detached(verification_record)

    input_expression: dict[str, object] = {
        "attributes": {},
        "children_sha256": [],
        "collapsed_identity": False,
        "inferred": inferred,
        "input_value_ids": [],
        "logical_roots": [_IDENTIFIER],
        "node_id": None,
        "operation": None,
        "semantic_sha256": _DIGEST,
        "value": value,
        "value_id": _IDENTIFIER,
    }
    operation_expression: dict[str, object] = {
        "attributes": {},
        "children_sha256": [_DIGEST, _DIGEST],
        "collapsed_identity": False,
        "inferred": inferred,
        "input_value_ids": [_IDENTIFIER, _IDENTIFIER],
        "logical_roots": [_IDENTIFIER] * 64,
        "node_id": _IDENTIFIER,
        "operation": "divide",
        "semantic_sha256": _DIGEST,
        "value": value,
        "value_id": _IDENTIFIER,
    }
    expressions = ([_detached(input_expression)] * 64) + (
        [_detached(operation_expression)] * 512
    )

    site_record: dict[str, object] = {
        "expression_sha256": _DIGEST,
        "logical_outputs": [_IDENTIFIER] * 64,
        "logical_roots": [_IDENTIFIER] * 64,
        "node_id": _IDENTIFIER,
        "schema": "unitsentinel.normalization-site/v1",
        "site_sha256": _DIGEST,
        "value_id": _IDENTIFIER,
    }
    sites = [_detached(site_record)] * 512

    counted_digest: dict[str, object] = {"count": 512, "sha256": _DIGEST}
    output_record: dict[str, object] = {
        "contract_id": _IDENTIFIER,
        "expression_sha256": _DIGEST,
        "normalization_sha256": _DIGEST,
        "position": 63,
        "site_sha256_multiset": [counted_digest] * 512,
        "value_id": _IDENTIFIER,
    }
    outputs = [_detached(output_record)] * 64

    def lineage(side: str) -> dict[str, object]:
        return {
            "authentication": "not-provided",
            "comparison_id": _IDENTIFIER,
            "expressions": expressions,
            "graph_sha256": _DIGEST,
            "limits": limits,
            "outputs": outputs,
            "plan_sha256": _DIGEST,
            "registry_sha256": _DIGEST,
            "schema": "unitsentinel.normalization-lineage/v1",
            "semantic_sha256": _DIGEST,
            "side": side,
            "sites": sites,
            "verification": verification_claim,
        }

    def snapshot(role: str) -> dict[str, object]:
        return {
            "endpoint": {"role": role, "value_id": _IDENTIFIER},
            "inferred": inferred,
            "position": 63,
            "value": value,
        }

    mismatch_codes = [
        "role-drift",
        "position-drift",
        "dtype-drift",
        "shape-drift",
        "explicit-unit-drift",
        "dimension-drift",
        "kind-drift",
        "scale-drift",
        "offset-drift",
        "normalization-lineage-drift",
    ]

    def binding(role: str) -> dict[str, object]:
        return {
            "contract_id": _IDENTIFIER,
            "mismatches": mismatch_codes,
            "normalization": (
                {
                    "serving_sha256": _DIGEST,
                    "training_sha256": _DIGEST,
                }
                if role == "output"
                else None
            ),
            "serving": snapshot(role),
            "training": snapshot(role),
        }

    return {
        "authentication": "not-provided",
        "bindings": ([binding("input")] * 64) + ([binding("output")] * 64),
        "comparison_id": _IDENTIFIER,
        "graphs": {
            "serving_sha256": _DIGEST,
            "training_sha256": _DIGEST,
        },
        "limits": limits,
        "normalization_lineage": {
            "serving": _detached(lineage("serving")),
            "training": _detached(lineage("training")),
        },
        "plan_sha256": _DIGEST,
        "registry_sha256": _DIGEST,
        "reason": None,
        "schema": "unitsentinel.training-serving-comparison-result/v1",
        "scope": "under-plan",
        "status": "drift",
        "verification": {
            "serving": verification_claim,
            "training": verification_claim,
        },
    }


def measure_json_structure(value: object) -> JSONStructureMeasurement:
    """Measure canonical-boundary tokens and limits without recursive walking."""

    stack: list[tuple[object, int]] = [(value, 0)]
    tree_values = 0
    object_keys = 0
    maximum_depth = 0
    maximum_container_items = 0
    maximum_string_length = 0
    maximum_integer_digits = 0

    while stack:
        current, depth = stack.pop()
        tree_values += 1
        maximum_depth = max(maximum_depth, depth)

        if type(current) is dict:
            mapping = current
            maximum_container_items = max(maximum_container_items, len(mapping))
            object_keys += len(mapping)
            for key, child in mapping.items():
                if type(key) is not str:
                    raise TypeError("shape-only JSON objects require exact string keys")
                maximum_string_length = max(maximum_string_length, len(key))
                stack.append((child, depth + 1))
            continue
        if type(current) is list:
            sequence = current
            maximum_container_items = max(maximum_container_items, len(sequence))
            stack.extend((child, depth + 1) for child in sequence)
            continue
        if type(current) is str:
            maximum_string_length = max(maximum_string_length, len(current))
            continue
        if type(current) is int:
            digits = len(str(abs(current)))
            maximum_integer_digits = max(maximum_integer_digits, digits)
            continue
        if current is None or type(current) is bool:
            continue
        raise TypeError("shape-only envelope contains a non-JSON value")

    return JSONStructureMeasurement(
        tree_values_excluding_object_keys=tree_values,
        object_key_tokens=object_keys,
        preflight_tokens_including_object_keys=tree_values + object_keys,
        maximum_depth=maximum_depth,
        maximum_container_items=maximum_container_items,
        maximum_string_length=maximum_string_length,
        maximum_integer_digits=maximum_integer_digits,
    )


def comparison_result_boundary_summary() -> dict[str, object]:
    """Build, canonicalize, and summarize the conservative envelope."""

    envelope = build_conservative_shape_only_envelope()
    payload = canonical_json_bytes(envelope)
    measured = measure_json_structure(envelope)
    selected_limits = {
        "canonical_bytes": MAX_COMPARISON_RESULT_BYTES,
        "maximum_container_items": MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS,
        "maximum_depth": MAX_COMPARISON_RESULT_JSON_DEPTH,
        "maximum_integer_digits": MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS,
        "maximum_string_length": MAX_COMPARISON_RESULT_JSON_STRING_LENGTH,
        "preflight_tokens_including_object_keys": (
            MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES
        ),
    }
    within_limits = {
        "canonical_bytes": len(payload) <= MAX_COMPARISON_RESULT_BYTES,
        "maximum_container_items": (
            measured.maximum_container_items
            <= MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS
        ),
        "maximum_depth": (measured.maximum_depth <= MAX_COMPARISON_RESULT_JSON_DEPTH),
        "maximum_integer_digits": (
            measured.maximum_integer_digits <= MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS
        ),
        "maximum_string_length": (
            measured.maximum_string_length <= MAX_COMPARISON_RESULT_JSON_STRING_LENGTH
        ),
        "preflight_tokens_including_object_keys": (
            measured.preflight_tokens_including_object_keys
            <= MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES
        ),
    }
    return {
        "classification": "conservative-independent-field-shape-only-envelope",
        "disclaimer": (
            "Non-constructible shape only: not a valid ComparisonResult and "
            "not a proof of the exact maximum."
        ),
        "exact_maximum_proven": False,
        "measurements": {
            "canonical_bytes": len(payload),
            "canonical_sha256": sha256_hex(payload),
            "maximum_container_items": measured.maximum_container_items,
            "maximum_depth": measured.maximum_depth,
            "maximum_integer_digits": measured.maximum_integer_digits,
            "maximum_string_length": measured.maximum_string_length,
            "object_key_tokens": measured.object_key_tokens,
            "preflight_tokens_including_object_keys": (
                measured.preflight_tokens_including_object_keys
            ),
            "tree_values_excluding_object_keys": (
                measured.tree_values_excluding_object_keys
            ),
        },
        "model_constructible": False,
        "nonconstructible_reasons": [
            (
                "Identifiers, dimensions, rational transforms, contracts, and "
                "routes deliberately repeat or combine independent field ceilings "
                "without model-level semantic coherence."
            ),
            (
                "Each counted multiset has 512 rows claiming count 512, beyond "
                "the model's expanded 512-site ceiling."
            ),
            (
                "The 98-character solver provenance string exceeds the model's "
                "32-character semantic cap while remaining under the JSON cap."
            ),
        ],
        "schema": _SCHEMA,
        "selected_transport_limits": selected_limits,
        "shape": {
            "bindings": 128,
            "counted_multiset_records_per_output": 512,
            "embedded_verification_contracts_per_lineage": 576,
            "input_expressions_per_lineage": 64,
            "lineages": 2,
            "logical_outputs_per_site": 64,
            "logical_roots_per_site": 64,
            "operation_expressions_per_lineage": 512,
            "outer_verification_records": 2,
            "outputs_per_lineage": 64,
            "sites_per_lineage": 512,
            "snapshots": 256,
        },
        "within_selected_transport_limits": {
            **within_limits,
            "all": all(within_limits.values()),
        },
    }


def main() -> None:
    """Write one deterministic canonical JSON summary."""

    sys.stdout.buffer.write(
        canonical_json_bytes(comparison_result_boundary_summary()) + b"\n"
    )


if __name__ == "__main__":
    main()
