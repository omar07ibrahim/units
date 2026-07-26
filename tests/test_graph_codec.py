from __future__ import annotations

import json
import unittest
from fractions import Fraction
from unittest.mock import patch

from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.graph import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.graph_codec import (
    MAX_GRAPH_BYTES,
    MAX_JSON_CONTAINER_ITEMS,
    MAX_JSON_DEPTH,
    MAX_JSON_TOTAL_VALUES,
    GraphDecodeError,
    decode_graph,
    encode_graph,
)


def sample_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="speed-contract",
        values=(
            ValueSpec(
                "raw-speed",
                ScalarType.FLOAT32,
                ("batch",),
                "kilometer-per-hour",
            ),
            ValueSpec(
                "si-speed",
                ScalarType.FLOAT32,
                ("batch",),
                "meter-per-second",
            ),
        ),
        inputs=("raw-speed",),
        nodes=(
            Node(
                "normalize-speed",
                Operation.CONVERT,
                ("raw-speed",),
                "si-speed",
                target_unit_id="meter-per-second",
            ),
        ),
        outputs=("si-speed",),
    )


def decoded_record() -> dict[str, object]:
    return sample_graph().canonical_record()


class GraphCodecRoundTripTests(unittest.TestCase):
    def test_graph_round_trip_preserves_exact_bytes_and_digest(self) -> None:
        graph = sample_graph()
        payload = encode_graph(graph)
        decoded = decode_graph(payload)

        self.assertEqual(decoded, graph)
        self.assertEqual(decoded.digest, graph.digest)
        self.assertEqual(encode_graph(decoded), payload)
        self.assertEqual(payload, canonical_json_bytes(decoded_record()))
        self.assertFalse(payload.endswith(b"\n"))

    def test_power_exponents_round_trip_as_reduced_rational_strings(self) -> None:
        graph = ComputationGraph(
            "root-contract",
            (
                ValueSpec("area", ScalarType.FLOAT64, (), "meter"),
                ValueSpec("length", ScalarType.FLOAT64, (), "meter"),
            ),
            ("area",),
            (
                Node(
                    "take-root",
                    Operation.POWER,
                    ("area",),
                    "length",
                    exponent=Fraction(1, 2),
                ),
            ),
            ("length",),
        )
        decoded = decode_graph(encode_graph(graph))

        self.assertEqual(decoded.nodes[0].exponent, Fraction(1, 2))
        self.assertEqual(
            decoded.canonical_record()["nodes"][0]["attributes"],  # type: ignore[index]
            {"exponent": "1/2"},
        )

    def test_attribute_free_operations_round_trip_with_an_empty_object(self) -> None:
        graph = ComputationGraph(
            "identity-contract",
            (
                ValueSpec("input", ScalarType.FLOAT16, ()),
                ValueSpec("output", ScalarType.FLOAT16, ()),
            ),
            ("input",),
            (
                Node(
                    "copy-value",
                    Operation.IDENTITY,
                    ("input",),
                    "output",
                ),
            ),
            ("output",),
        )

        decoded = decode_graph(encode_graph(graph))
        self.assertEqual(
            decoded.canonical_record()["nodes"][0]["attributes"],  # type: ignore[index]
            {},
        )

    def test_encoder_requires_an_exact_graph_receiver(self) -> None:
        with self.assertRaisesRegex(GraphDecodeError, "exact ComputationGraph"):
            encode_graph(object())  # type: ignore[arg-type]

        graph = sample_graph()
        object.__setattr__(graph.values[0], "unit_id", "meter-per-second")
        with self.assertRaisesRegex(GraphDecodeError, "graph encoding failed"):
            encode_graph(graph)


class JSONBoundaryTests(unittest.TestCase):
    def test_payload_type_size_encoding_and_bom_are_rejected_first(self) -> None:
        cases = (
            (bytearray(b"{}"), "exact bytes"),
            (b"", "empty"),
            (b" " * (MAX_GRAPH_BYTES + 1), "byte limit"),
            (b"\xef\xbb\xbf{}", "BOM"),
            (b"\xff", "valid UTF-8"),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(GraphDecodeError, message),
            ):
                decode_graph(payload)  # type: ignore[arg-type]

    def test_malformed_duplicate_float_and_nonfinite_json_fail_closed(self) -> None:
        payloads = (
            (b"{", "valid bounded JSON"),
            (b'{"schema":1,"schema":2}', "duplicate"),
            (b'{"shape":[1.0]}', "floating-point"),
            (b'{"shape":[NaN]}', "non-finite"),
            (b'{"shape":[Infinity]}', "non-finite"),
        )
        for payload, message in payloads:
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(GraphDecodeError, message),
            ):
                decode_graph(payload)

    def test_integer_string_container_and_depth_limits_precede_semantics(self) -> None:
        huge_integer = b'{"shape":[' + (b"9" * 11) + b"]}"
        huge_string = canonical_json_bytes({"x": "a" * 129})
        huge_array = canonical_json_bytes(
            {"x": [None] * (MAX_JSON_CONTAINER_ITEMS + 1)}
        )
        huge_object = canonical_json_bytes(
            {f"k{index}": None for index in range(MAX_JSON_CONTAINER_ITEMS + 1)}
        )
        too_many_values = canonical_json_bytes(
            [
                [None] * MAX_JSON_CONTAINER_ITEMS
                for _ in range((MAX_JSON_TOTAL_VALUES // MAX_JSON_CONTAINER_ITEMS) + 1)
            ]
        )
        nested: object = None
        for _ in range(MAX_JSON_DEPTH + 1):
            nested = [nested]
        deep = canonical_json_bytes(nested)
        parser_recursion = (b"[" * 2_000) + b"0" + (b"]" * 2_000)

        cases = (
            (huge_integer, "digit limit"),
            (huge_string, "string exceeds"),
            (huge_array, "array exceeds"),
            (huge_object, "object exceeds"),
            (too_many_values, "JSON value limit"),
            (deep, "nesting limit"),
            (parser_recursion, "valid bounded JSON|nesting limit"),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(GraphDecodeError, message),
            ):
                decode_graph(payload)

    def test_wide_payload_is_rejected_before_json_tree_materialization(self) -> None:
        payload = b"[" + (b"0," * 349_000) + b"0]"
        self.assertLess(len(payload), MAX_GRAPH_BYTES)

        with (
            patch(
                "unitsentinel.graph_codec.json.loads",
                side_effect=AssertionError("parser must not run"),
            ) as parser,
            self.assertRaisesRegex(GraphDecodeError, "array exceeds the item limit"),
        ):
            decode_graph(payload)
        parser.assert_not_called()

    def test_surrogates_and_noncanonical_spellings_are_rejected(self) -> None:
        canonical = encode_graph(sample_graph())
        noncanonical_payloads = (
            canonical + b"\n",
            b" " + canonical,
            canonical.replace(b"speed-contract", b"speed\\u002dcontract"),
            canonical.replace(b'"shape":["batch"]', b'"shape":[-0]'),
            b'{"graph_id":"\\ud800"}',
        )
        for payload in noncanonical_payloads:
            with (
                self.subTest(payload=payload[:80]),
                self.assertRaisesRegex(
                    GraphDecodeError,
                    "canonical JSON|invalid Unicode scalar",
                ),
            ):
                decode_graph(payload)

    def test_key_order_is_part_of_the_canonical_byte_contract(self) -> None:
        record = decoded_record()
        unsorted = json.dumps(
            dict(reversed(tuple(record.items()))),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode()
        self.assertNotEqual(unsorted, encode_graph(sample_graph()))
        with self.assertRaisesRegex(GraphDecodeError, "not canonical JSON"):
            decode_graph(unsorted)


class SemanticDecoderTests(unittest.TestCase):
    def test_root_schema_and_closed_fields_are_enforced(self) -> None:
        with self.assertRaisesRegex(GraphDecodeError, "document must be an object"):
            decode_graph(b"null")

        record = decoded_record()
        record["schema"] = "unitsentinel.graph/v2"
        with self.assertRaisesRegex(GraphDecodeError, "schema is not supported"):
            decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        record["extension"] = {}
        with self.assertRaisesRegex(GraphDecodeError, "missing or unknown fields"):
            decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        del record["outputs"]
        with self.assertRaisesRegex(GraphDecodeError, "missing or unknown fields"):
            decode_graph(canonical_json_bytes(record))

    def test_value_types_and_fields_are_closed(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("dtype", "complex64", "dtype is not supported"),
            ("shape", [True], "unsupported axis"),
            ("unit_id", 1, "text or null"),
        )
        for field, replacement, message in cases:
            record = decoded_record()
            values = record["values"]
            assert isinstance(values, list)
            first = values[0]
            assert isinstance(first, dict)
            first[field] = replacement
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(GraphDecodeError, message),
            ):
                decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        values = record["values"]
        assert isinstance(values, list)
        first = values[0]
        assert isinstance(first, dict)
        first["unknown"] = None
        with self.assertRaisesRegex(GraphDecodeError, "missing or unknown fields"):
            decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        record["values"] = {}
        with self.assertRaisesRegex(GraphDecodeError, "values must be an array"):
            decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        record["inputs"] = [1]
        with self.assertRaisesRegex(GraphDecodeError, "inputs entry must be text"):
            decode_graph(canonical_json_bytes(record))

    def test_operations_and_attribute_shapes_are_closed(self) -> None:
        record = decoded_record()
        nodes = record["nodes"]
        assert isinstance(nodes, list)
        node = nodes[0]
        assert isinstance(node, dict)
        node["operation"] = "execute-python"
        with self.assertRaisesRegex(GraphDecodeError, "operation is not supported"):
            decode_graph(canonical_json_bytes(record))

        record = decoded_record()
        nodes = record["nodes"]
        assert isinstance(nodes, list)
        node = nodes[0]
        assert isinstance(node, dict)
        node["attributes"] = {"unit_id": "meter-per-second", "url": "x"}
        with self.assertRaisesRegex(GraphDecodeError, "missing or unknown fields"):
            decode_graph(canonical_json_bytes(record))

    def test_power_exponents_must_be_canonical_reduced_and_bounded(self) -> None:
        graph = ComputationGraph(
            "power-contract",
            (
                ValueSpec("input", ScalarType.FLOAT32, ()),
                ValueSpec("output", ScalarType.FLOAT32, ()),
            ),
            ("input",),
            (
                Node(
                    "raise-value",
                    Operation.POWER,
                    ("input",),
                    "output",
                    exponent=Fraction(2),
                ),
            ),
            ("output",),
        )
        for exponent, message in (
            ("2/2", "not reduced"),
            ("01", "canonical rational"),
            ("65", "semantic contract"),
            (1, "must be text"),
        ):
            record = graph.canonical_record()
            nodes = record["nodes"]
            assert isinstance(nodes, list)
            node = nodes[0]
            assert isinstance(node, dict)
            attributes = node["attributes"]
            assert isinstance(attributes, dict)
            attributes["exponent"] = exponent
            with (
                self.subTest(exponent=exponent),
                self.assertRaisesRegex(GraphDecodeError, message),
            ):
                decode_graph(canonical_json_bytes(record))

    def test_valid_json_with_invalid_topology_becomes_a_stable_decode_error(
        self,
    ) -> None:
        record = decoded_record()
        nodes = record["nodes"]
        assert isinstance(nodes, list)
        node = nodes[0]
        assert isinstance(node, dict)
        node["inputs"] = ["si-speed"]

        with self.assertRaisesRegex(GraphDecodeError, "semantic contract failed"):
            decode_graph(canonical_json_bytes(record))


if __name__ == "__main__":
    unittest.main()
