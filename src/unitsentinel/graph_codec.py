"""Strict canonical JSON codec for bounded computation graphs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import NoReturn, cast

from .canonical import canonical_json_bytes
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

MAX_GRAPH_BYTES = 1_048_576
MAX_JSON_DEPTH = 8
MAX_JSON_CONTAINER_ITEMS = 1_024
MAX_JSON_TOTAL_VALUES = 32_768
MAX_JSON_STRING_LENGTH = 128
MAX_JSON_INTEGER_DIGITS = 10
MAX_EXPONENT_TEXT_LENGTH = 16
FRACTION_TEXT = re.compile(r"^(?:0|-?[1-9][0-9]*)(?:/[1-9][0-9]*)?$")

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

    if type(payload) is not bytes:
        raise GraphDecodeError("graph payload must be exact bytes")
    if not payload:
        raise GraphDecodeError("graph payload is empty")
    if len(payload) > MAX_GRAPH_BYTES:
        raise GraphDecodeError("graph payload exceeds the byte limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise GraphDecodeError("graph payload must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GraphDecodeError("graph payload is not valid UTF-8") from None

    _preflight_json_structure(text)
    parsed = _parse_json(text)
    _validate_json_tree(parsed)
    try:
        canonical = canonical_json_bytes(parsed)
    except UnicodeEncodeError:
        raise GraphDecodeError(
            "graph payload contains an invalid Unicode scalar"
        ) from None
    if canonical != payload:
        raise GraphDecodeError("graph payload is not canonical JSON")

    graph = _decode_semantic_graph(parsed)
    if graph.canonical_bytes() != payload:
        raise GraphDecodeError("graph payload does not match the canonical graph model")
    return graph


@dataclass(slots=True)
class _ContainerFrame:
    opening: str
    commas: int = 0


def _preflight_json_structure(text: str) -> None:
    """Bound structural expansion before ``json.loads`` allocates a tree."""

    stack: list[_ContainerFrame] = []
    token_count = 0
    previous_significant: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if character in " \t\r\n":
            index += 1
            continue
        if character == '"':
            token_count += 1
            _require_preflight_token_budget(token_count)
            index = _scan_json_string(text, index + 1)
            previous_significant = '"'
            continue
        if character in "[{":
            token_count += 1
            _require_preflight_token_budget(token_count)
            stack.append(_ContainerFrame(character))
            if len(stack) > MAX_JSON_DEPTH:
                raise GraphDecodeError("graph payload exceeds the nesting limit")
            previous_significant = character
            index += 1
            continue
        if character in "]}":
            expected = "[" if character == "]" else "{"
            if not stack or stack[-1].opening != expected:
                raise GraphDecodeError("graph payload is not valid bounded JSON")
            stack.pop()
            previous_significant = character
            index += 1
            continue
        if character == ",":
            if not stack:
                raise GraphDecodeError("graph payload is not valid bounded JSON")
            stack[-1].commas += 1
            if stack[-1].commas + 1 > MAX_JSON_CONTAINER_ITEMS:
                kind = "array" if stack[-1].opening == "[" else "object"
                raise GraphDecodeError(f"graph payload {kind} exceeds the item limit")
            previous_significant = character
            index += 1
            continue
        if previous_significant is None or previous_significant in "[{,:":
            token_count += 1
            _require_preflight_token_budget(token_count)
        previous_significant = character
        index += 1

    if stack:
        raise GraphDecodeError("graph payload is not valid bounded JSON")


def _scan_json_string(text: str, index: int) -> int:
    while index < len(text):
        character = text[index]
        if character == '"':
            return index + 1
        if character == "\\":
            index += 1
            if index >= len(text):
                break
        index += 1
    raise GraphDecodeError("graph payload is not valid bounded JSON")


def _require_preflight_token_budget(token_count: int) -> None:
    if token_count > MAX_JSON_TOTAL_VALUES:
        raise GraphDecodeError("graph payload exceeds the JSON value limit")


def _parse_json(text: str) -> object:
    try:
        return cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_nonfinite_number,
                parse_float=_reject_float,
                parse_int=_parse_bounded_integer,
            ),
        )
    except GraphDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise GraphDecodeError("graph payload is not valid bounded JSON") from None


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GraphDecodeError("graph payload contains a duplicate object key")
        result[key] = value
    return result


def _reject_nonfinite_number(_: str) -> NoReturn:
    raise GraphDecodeError("graph payload contains a non-finite number")


def _reject_float(_: str) -> NoReturn:
    raise GraphDecodeError("graph payload contains a floating-point number")


def _parse_bounded_integer(text: str) -> int:
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise GraphDecodeError("graph payload integer exceeds the digit limit")
    return int(text)


def _validate_json_tree(value: object) -> None:
    counter = [0]
    _walk_json(value, depth=0, counter=counter)


def _walk_json(value: object, *, depth: int, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_JSON_TOTAL_VALUES:
        raise GraphDecodeError("graph payload exceeds the JSON value limit")
    if depth > MAX_JSON_DEPTH:
        raise GraphDecodeError("graph payload exceeds the nesting limit")

    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        if len(mapping) > MAX_JSON_CONTAINER_ITEMS:
            raise GraphDecodeError("graph payload object exceeds the item limit")
        for key, child in mapping.items():
            _validate_json_string(key)
            _walk_json(child, depth=depth + 1, counter=counter)
        return
    if type(value) is list:
        sequence = cast(list[object], value)
        if len(sequence) > MAX_JSON_CONTAINER_ITEMS:
            raise GraphDecodeError("graph payload array exceeds the item limit")
        for child in sequence:
            _walk_json(child, depth=depth + 1, counter=counter)
        return
    if type(value) is str:
        _validate_json_string(value)
        return
    if value is None or type(value) in {bool, int}:
        return
    raise GraphDecodeError("graph payload contains an unsupported JSON value")


def _validate_json_string(value: str) -> None:
    if len(value) > MAX_JSON_STRING_LENGTH:
        raise GraphDecodeError("graph payload string exceeds the length limit")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise GraphDecodeError("graph payload contains an invalid Unicode scalar")


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
