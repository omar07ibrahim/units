"""Reusable bounded canonical-JSON trust boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import NoReturn, cast

from .canonical import canonical_json_bytes
from .domain import UnitSentinelError

_BOUNDARY_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class CanonicalJSONError(UnitSentinelError):
    """Raised when untrusted JSON bytes violate a canonical bounded contract."""


@dataclass(frozen=True, slots=True)
class CanonicalJSONLimits:
    """Trusted resource limits for one canonical JSON document family."""

    max_bytes: int
    max_depth: int
    max_container_items: int
    max_total_values: int
    max_string_length: int
    max_integer_digits: int

    def __post_init__(self) -> None:
        values = (
            self.max_bytes,
            self.max_depth,
            self.max_container_items,
            self.max_total_values,
            self.max_string_length,
            self.max_integer_digits,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise CanonicalJSONError(
                "canonical JSON limits must be positive exact integers"
            )


@dataclass(slots=True)
class _ContainerFrame:
    opening: str
    commas: int = 0


def decode_canonical_json(
    payload: bytes,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> object:
    """Decode canonical UTF-8 JSON after structural allocation preflight."""

    if type(limits) is not CanonicalJSONLimits:
        raise CanonicalJSONError(
            "canonical JSON limits must be an exact CanonicalJSONLimits"
        )
    if type(label) is not str or _BOUNDARY_LABEL.fullmatch(label) is None:
        raise CanonicalJSONError("canonical JSON boundary label is invalid")
    limits.__post_init__()
    if type(payload) is not bytes:
        raise CanonicalJSONError(f"{label} payload must be exact bytes")
    if not payload:
        raise CanonicalJSONError(f"{label} payload is empty")
    if len(payload) > limits.max_bytes:
        raise CanonicalJSONError(f"{label} payload exceeds the byte limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise CanonicalJSONError(f"{label} payload must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise CanonicalJSONError(f"{label} payload is not valid UTF-8") from None

    _preflight_json_structure(text, limits=limits, label=label)
    parsed = _parse_json(text, limits=limits, label=label)
    _validate_json_tree(parsed, limits=limits, label=label)
    try:
        canonical = canonical_json_bytes(parsed)
    except UnicodeEncodeError:
        raise CanonicalJSONError(
            f"{label} payload contains an invalid Unicode scalar"
        ) from None
    if canonical != payload:
        raise CanonicalJSONError(f"{label} payload is not canonical JSON")
    return parsed


def _preflight_json_structure(
    text: str,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> None:
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
            _require_preflight_token_budget(
                token_count,
                limits=limits,
                label=label,
            )
            index = _scan_json_string(text, index + 1, label=label)
            previous_significant = '"'
            continue
        if character in "[{":
            token_count += 1
            _require_preflight_token_budget(
                token_count,
                limits=limits,
                label=label,
            )
            stack.append(_ContainerFrame(character))
            if len(stack) > limits.max_depth:
                raise CanonicalJSONError(f"{label} payload exceeds the nesting limit")
            previous_significant = character
            index += 1
            continue
        if character in "]}":
            expected = "[" if character == "]" else "{"
            if not stack or stack[-1].opening != expected:
                raise CanonicalJSONError(f"{label} payload is not valid bounded JSON")
            stack.pop()
            previous_significant = character
            index += 1
            continue
        if character == ",":
            if not stack:
                raise CanonicalJSONError(f"{label} payload is not valid bounded JSON")
            stack[-1].commas += 1
            if stack[-1].commas + 1 > limits.max_container_items:
                kind = "array" if stack[-1].opening == "[" else "object"
                raise CanonicalJSONError(
                    f"{label} payload {kind} exceeds the item limit"
                )
            previous_significant = character
            index += 1
            continue
        if previous_significant is None or previous_significant in "[{,:":
            token_count += 1
            _require_preflight_token_budget(
                token_count,
                limits=limits,
                label=label,
            )
        previous_significant = character
        index += 1

    if stack:
        raise CanonicalJSONError(f"{label} payload is not valid bounded JSON")


def _scan_json_string(text: str, index: int, *, label: str) -> int:
    while index < len(text):
        character = text[index]
        if character == '"':
            return index + 1
        if character == "\\":
            index += 1
            if index >= len(text):
                break
        index += 1
    raise CanonicalJSONError(f"{label} payload is not valid bounded JSON")


def _require_preflight_token_budget(
    token_count: int,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> None:
    if token_count > limits.max_total_values:
        raise CanonicalJSONError(f"{label} payload exceeds the JSON value limit")


def _parse_json(
    text: str,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> object:
    try:
        return cast(
            object,
            json.loads(
                text,
                object_pairs_hook=lambda pairs: _object_without_duplicates(
                    pairs,
                    label=label,
                ),
                parse_constant=lambda value: _reject_nonfinite_number(
                    value,
                    label=label,
                ),
                parse_float=lambda value: _reject_float(
                    value,
                    label=label,
                ),
                parse_int=lambda value: _parse_bounded_integer(
                    value,
                    limits=limits,
                    label=label,
                ),
            ),
        )
    except CanonicalJSONError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise CanonicalJSONError(f"{label} payload is not valid bounded JSON") from None


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
    *,
    label: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJSONError(f"{label} payload contains a duplicate object key")
        result[key] = value
    return result


def _reject_nonfinite_number(_: str, *, label: str) -> NoReturn:
    raise CanonicalJSONError(f"{label} payload contains a non-finite number")


def _reject_float(_: str, *, label: str) -> NoReturn:
    raise CanonicalJSONError(f"{label} payload contains a floating-point number")


def _parse_bounded_integer(
    text: str,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> int:
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > limits.max_integer_digits:
        raise CanonicalJSONError(f"{label} payload integer exceeds the digit limit")
    return int(text)


def _validate_json_tree(
    value: object,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> None:
    counter = [0]
    _walk_json(
        value,
        depth=0,
        counter=counter,
        limits=limits,
        label=label,
    )


def _walk_json(
    value: object,
    *,
    depth: int,
    counter: list[int],
    limits: CanonicalJSONLimits,
    label: str,
) -> None:
    counter[0] += 1
    if counter[0] > limits.max_total_values:
        raise CanonicalJSONError(f"{label} payload exceeds the JSON value limit")
    if depth > limits.max_depth:
        raise CanonicalJSONError(f"{label} payload exceeds the nesting limit")

    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        if len(mapping) > limits.max_container_items:
            raise CanonicalJSONError(f"{label} payload object exceeds the item limit")
        for key, child in mapping.items():
            _validate_json_string(key, limits=limits, label=label)
            _walk_json(
                child,
                depth=depth + 1,
                counter=counter,
                limits=limits,
                label=label,
            )
        return
    if type(value) is list:
        sequence = cast(list[object], value)
        if len(sequence) > limits.max_container_items:
            raise CanonicalJSONError(f"{label} payload array exceeds the item limit")
        for child in sequence:
            _walk_json(
                child,
                depth=depth + 1,
                counter=counter,
                limits=limits,
                label=label,
            )
        return
    if type(value) is str:
        _validate_json_string(value, limits=limits, label=label)
        return
    if value is None or type(value) in {bool, int}:
        return
    raise CanonicalJSONError(f"{label} payload contains an unsupported JSON value")


def _validate_json_string(
    value: str,
    *,
    limits: CanonicalJSONLimits,
    label: str,
) -> None:
    if len(value) > limits.max_string_length:
        raise CanonicalJSONError(f"{label} payload string exceeds the length limit")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CanonicalJSONError(f"{label} payload contains an invalid Unicode scalar")
