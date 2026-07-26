"""Strict canonical JSON codec for bounded computation graphs."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import cast

from .domain import UnitSentinelError, _fraction_text
from .graph import (
    GRAPH_SCHEMA,
    ComputationGraph,
    GraphError,
    Node,
    Operation,
    ScalarType,
    ShapeAxis,
    ValueSpec,
)
from .json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
)

MAX_GRAPH_BYTES = 1_048_576
MAX_JSON_DEPTH = 8
MAX_JSON_CONTAINER_ITEMS = 1_024
MAX_JSON_TOTAL_VALUES = 32_768
MAX_JSON_STRING_LENGTH = 128
MAX_JSON_INTEGER_DIGITS = 10
MAX_EXPONENT_TEXT_LENGTH = 16
FRACTION_TEXT = re.compile(r"^(?:0|-?[1-9][0-9]*)(?:/[1-9][0-9]*)?$")
_GRAPH_JSON_LIMITS = CanonicalJSONLimits(
    max_bytes=MAX_GRAPH_BYTES,
    max_depth=MAX_JSON_DEPTH,
    max_container_items=MAX_JSON_CONTAINER_ITEMS,
    max_total_values=MAX_JSON_TOTAL_VALUES,
    max_string_length=MAX_JSON_STRING_LENGTH,
    max_integer_digits=MAX_JSON_INTEGER_DIGITS,
)

ROOT_FIELDS = frozenset({"graph_id", "inputs", "nodes", "outputs", "schema", "values"})
VALUE_FIELDS = frozenset({"dtype", "shape", "unit_id", "value_id"})
NODE_FIELDS = frozenset({"attributes", "inputs", "node_id", "operation", "output"})


class GraphDecodeError(GraphError):
    """Raised when graph bytes are unsafe, noncanonical, or semantically invalid."""


def encode_graph(graph: ComputationGraph) -> bytes:
    """Return canonical bytes for one exact, currently valid graph."""

    if type(graph) is not ComputationGraph:
        raise GraphDecodeError("graph encoder requires an exact ComputationGraph")
    try:
        return graph.canonical_bytes()
    except UnitSentinelError as error:
        raise GraphDecodeError(f"graph encoding failed: {error}") from None


def decode_graph(payload: bytes) -> ComputationGraph:
    """Decode byte-for-byte canonical v1 JSON without coercion or extensions."""

    try:
        parsed = decode_canonical_json(
            payload,
            limits=_GRAPH_JSON_LIMITS,
            label="graph",
        )
    except CanonicalJSONError as error:
        raise GraphDecodeError(str(error)) from None

    graph = _decode_semantic_graph(parsed)
    if graph.canonical_bytes() != payload:
        raise GraphDecodeError("graph payload does not match the canonical graph model")
    return graph


def _decode_semantic_graph(value: object) -> ComputationGraph:
    root = _expect_object(value, ROOT_FIELDS, label="graph document")
    schema = _expect_string(root["schema"], label="graph schema")
    if schema != GRAPH_SCHEMA:
        raise GraphDecodeError("graph schema is not supported")

    try:
        graph = ComputationGraph(
            graph_id=_expect_string(root["graph_id"], label="graph identifier"),
            values=tuple(
                _decode_value(item)
                for item in _expect_array(root["values"], label="graph values")
            ),
            inputs=_decode_string_array(root["inputs"], label="graph inputs"),
            nodes=tuple(
                _decode_node(item)
                for item in _expect_array(root["nodes"], label="graph nodes")
            ),
            outputs=_decode_string_array(root["outputs"], label="graph outputs"),
        )
    except GraphDecodeError:
        raise
    except UnitSentinelError as error:
        raise GraphDecodeError(f"graph semantic contract failed: {error}") from None
    return graph


def _decode_value(value: object) -> ValueSpec:
    record = _expect_object(value, VALUE_FIELDS, label="value declaration")
    dtype_text = _expect_string(record["dtype"], label="value dtype")
    try:
        dtype = ScalarType(dtype_text)
    except ValueError:
        raise GraphDecodeError("value dtype is not supported") from None

    shape_values = _expect_array(record["shape"], label="value shape")
    shape: list[ShapeAxis] = []
    for axis in shape_values:
        if type(axis) not in {int, str}:
            raise GraphDecodeError("value shape contains an unsupported axis")
        shape.append(cast(ShapeAxis, axis))

    unit_value = record["unit_id"]
    if unit_value is not None and type(unit_value) is not str:
        raise GraphDecodeError("value unit identifier must be text or null")
    return ValueSpec(
        value_id=_expect_string(record["value_id"], label="value identifier"),
        dtype=dtype,
        shape=tuple(shape),
        unit_id=unit_value,
    )


def _decode_node(value: object) -> Node:
    record = _expect_object(value, NODE_FIELDS, label="node declaration")
    operation_text = _expect_string(record["operation"], label="node operation")
    try:
        operation = Operation(operation_text)
    except ValueError:
        raise GraphDecodeError("node operation is not supported") from None

    attributes = _expect_object(
        record["attributes"],
        _attribute_fields(operation),
        label="node attributes",
    )
    exponent: Fraction | None = None
    target_unit_id: str | None = None
    if operation is Operation.POWER:
        exponent = _decode_exponent(attributes["exponent"])
    elif operation is Operation.CONVERT:
        target_unit_id = _expect_string(
            attributes["unit_id"],
            label="conversion target unit identifier",
        )

    return Node(
        node_id=_expect_string(record["node_id"], label="node identifier"),
        operation=operation,
        inputs=_decode_string_array(record["inputs"], label="node inputs"),
        output=_expect_string(record["output"], label="node output identifier"),
        exponent=exponent,
        target_unit_id=target_unit_id,
    )


def _attribute_fields(operation: Operation) -> frozenset[str]:
    if operation is Operation.POWER:
        return frozenset({"exponent"})
    if operation is Operation.CONVERT:
        return frozenset({"unit_id"})
    return frozenset()


def _decode_exponent(value: object) -> Fraction:
    text = _expect_string(value, label="power exponent")
    if len(text) > MAX_EXPONENT_TEXT_LENGTH or FRACTION_TEXT.fullmatch(text) is None:
        raise GraphDecodeError("power exponent is not a canonical rational")
    exponent = Fraction(text)
    if _fraction_text(exponent) != text:
        raise GraphDecodeError("power exponent is not reduced")
    return exponent


def _expect_object(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise GraphDecodeError(f"{label} must be an object")
    record = cast(dict[str, object], value)
    if set(record) != fields:
        raise GraphDecodeError(f"{label} has missing or unknown fields")
    return record


def _expect_array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise GraphDecodeError(f"{label} must be an array")
    return cast(list[object], value)


def _decode_string_array(value: object, *, label: str) -> tuple[str, ...]:
    items = _expect_array(value, label=label)
    return tuple(_expect_string(item, label=f"{label} entry") for item in items)


def _expect_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise GraphDecodeError(f"{label} must be text")
    return value
