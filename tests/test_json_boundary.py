from __future__ import annotations

import unittest
from unittest.mock import patch

from unitsentinel.json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
)


def limits(**changes: int) -> CanonicalJSONLimits:
    values = {
        "max_bytes": 1_024,
        "max_depth": 4,
        "max_container_items": 8,
        "max_total_values": 64,
        "max_string_length": 32,
        "max_integer_digits": 4,
    }
    values.update(changes)
    return CanonicalJSONLimits(**values)


class CanonicalJSONLimitTests(unittest.TestCase):
    def test_limits_require_positive_exact_integers(self) -> None:
        fields = (
            "max_bytes",
            "max_depth",
            "max_container_items",
            "max_total_values",
            "max_string_length",
            "max_integer_digits",
        )
        for field in fields:
            for invalid in (0, True):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaisesRegex(
                        CanonicalJSONError,
                        "positive exact integers",
                    ),
                ):
                    limits(**{field: invalid})  # type: ignore[arg-type]

    def test_decoder_revalidates_exact_limits_and_label(self) -> None:
        class DerivedLimits(CanonicalJSONLimits):
            pass

        derived = DerivedLimits(1_024, 4, 8, 64, 32, 4)
        with self.assertRaisesRegex(CanonicalJSONError, "exact CanonicalJSONLimits"):
            decode_canonical_json(b"{}", limits=derived, label="test")
        with self.assertRaisesRegex(CanonicalJSONError, "exact CanonicalJSONLimits"):
            decode_canonical_json(
                b"{}",
                limits="limits",  # type: ignore[arg-type]
                label="test",
            )
        for label in ("Test", "", 1):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(CanonicalJSONError, "label"),
            ):
                decode_canonical_json(
                    b"{}",
                    limits=limits(),
                    label=label,  # type: ignore[arg-type]
                )

        mutated = limits()
        object.__setattr__(mutated, "max_bytes", 0)
        with self.assertRaisesRegex(CanonicalJSONError, "positive exact integers"):
            decode_canonical_json(b"{}", limits=mutated, label="test")


class CanonicalJSONDecodeTests(unittest.TestCase):
    def test_canonical_document_round_trips_without_coercion(self) -> None:
        parsed = decode_canonical_json(
            b'{"enabled":true,"items":[null,-12,"ok"]}',
            limits=limits(),
            label="test",
        )

        self.assertEqual(
            parsed,
            {"enabled": True, "items": [None, -12, "ok"]},
        )
        self.assertIs(type(parsed), dict)

    def test_byte_encoding_and_canonical_spelling_fail_closed(self) -> None:
        cases = (
            (b"", "empty"),
            (b" " * 1_025, "byte limit"),
            (b"\xef\xbb\xbf{}", "BOM"),
            (b"\xff", "UTF-8"),
            (b"{}\n", "canonical JSON"),
            (b'{"x":"\\ud800"}', "invalid Unicode scalar"),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(CanonicalJSONError, message),
            ):
                decode_canonical_json(
                    payload,
                    limits=limits(),
                    label="test",
                )
        with self.assertRaisesRegex(CanonicalJSONError, "exact bytes"):
            decode_canonical_json(  # type: ignore[arg-type]
                bytearray(b"{}"),
                limits=limits(),
                label="test",
            )

    def test_parser_rejects_duplicate_numeric_and_malformed_values(self) -> None:
        cases = (
            (b'{"x":1,"x":2}', "duplicate object key"),
            (b'{"x":1.0}', "floating-point"),
            (b'{"x":NaN}', "non-finite"),
            (b'{"x":10000}', "digit limit"),
            (b"]", "valid bounded JSON"),
            (b",", "valid bounded JSON"),
            (b'{"x":"unterminated}', "valid bounded JSON"),
            (b'{"x":}', "valid bounded JSON"),
        )
        for payload, message in cases:
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(CanonicalJSONError, message),
            ):
                decode_canonical_json(
                    payload,
                    limits=limits(),
                    label="test",
                )

    def test_structural_limits_apply_before_and_after_materialization(self) -> None:
        cases = (
            (
                b"[[[0]]]",
                limits(max_depth=2),
                "nesting limit",
            ),
            (
                b"[0,1,2]",
                limits(max_container_items=2),
                "array exceeds the item limit",
            ),
            (
                b'{"a":0,"b":1,"c":2}',
                limits(max_container_items=2),
                "object exceeds the item limit",
            ),
            (
                b'{"long":"12345"}',
                limits(max_string_length=4),
                "string exceeds the length limit",
            ),
            (
                b"[0,1,2]",
                limits(max_total_values=3),
                "JSON value limit",
            ),
        )
        for payload, boundary_limits, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(CanonicalJSONError, message),
            ):
                decode_canonical_json(
                    payload,
                    limits=boundary_limits,
                    label="certificate",
                )

    def test_parser_recursion_and_encoding_failures_are_redacted(self) -> None:
        with (
            patch(
                "unitsentinel.json_boundary.json.loads",
                side_effect=RecursionError("private parser detail"),
            ),
            self.assertRaisesRegex(CanonicalJSONError, "valid bounded JSON"),
        ):
            decode_canonical_json(b"{}", limits=limits(), label="test")

        with (
            patch(
                "unitsentinel.json_boundary.canonical_json_bytes",
                side_effect=UnicodeEncodeError(
                    "utf-8",
                    "private",
                    0,
                    1,
                    "private detail",
                ),
            ),
            self.assertRaisesRegex(CanonicalJSONError, "invalid Unicode scalar"),
        ):
            decode_canonical_json(b"{}", limits=limits(), label="test")

    def test_wide_object_is_rejected_before_parser_materialization(self) -> None:
        with (
            patch(
                "unitsentinel.json_boundary.json.loads",
                side_effect=AssertionError("parser must not run"),
            ) as parser,
            self.assertRaisesRegex(
                CanonicalJSONError,
                "object exceeds the item limit",
            ),
        ):
            decode_canonical_json(
                b'{"a":0,"b":1,"c":2}',
                limits=limits(max_container_items=2),
                label="certificate",
            )
        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
