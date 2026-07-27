from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.comparison_codec import (
    MAX_COMPARISON_BYTES,
    MAX_COMPARISON_JSON_CONTAINER_ITEMS,
    MAX_COMPARISON_JSON_DEPTH,
    MAX_COMPARISON_JSON_STRING_LENGTH,
    MAX_COMPARISON_JSON_TOTAL_VALUES,
    ComparisonDecodeError,
    decode_comparison_plan,
    encode_comparison_plan,
)
from unitsentinel.comparison_contract import (
    COMPARISON_SCHEMA,
    MAX_COMPARISON_BINDINGS,
    ComparisonPlan,
    ComparisonValidationError,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)

TRAINING_DIGEST = "1" * 64
SERVING_DIGEST = "2" * 64
REGISTRY_DIGEST = "3" * 64


def endpoint(
    value_id: str,
    role: InterfaceRole = InterfaceRole.OUTPUT,
) -> InterfaceEndpoint:
    return InterfaceEndpoint(role=role, value_id=value_id)


def sample_plan() -> ComparisonPlan:
    return ComparisonPlan(
        comparison_id="checkout-model",
        training_graph_digest=TRAINING_DIGEST,
        serving_graph_digest=SERVING_DIGEST,
        registry_digest=REGISTRY_DIGEST,
        bindings=(
            ContractBinding(
                contract_id="feature-temperature",
                training=endpoint("normalized-temperature"),
                serving=endpoint(
                    "request-temperature",
                    InterfaceRole.INPUT,
                ),
            ),
            ContractBinding(
                contract_id="prediction",
                training=endpoint("prediction"),
                serving=endpoint("response-score"),
            ),
        ),
    )


def decoded_record() -> dict[str, object]:
    return sample_plan().canonical_record()


class ComparisonPlanTests(unittest.TestCase):
    def test_plan_is_canonical_content_addressed_and_deterministic(self) -> None:
        first = sample_plan()
        second = sample_plan()

        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(len(first.digest), 64)
        self.assertEqual(len(first.canonical_bytes()), 669)
        self.assertEqual(
            first.digest,
            "993ce975406736edc15f6fe2e5c1802c2adf9a180a167e2bb9d09e8e1abdb06c",
        )
        self.assertEqual(first.canonical_record()["schema"], COMPARISON_SCHEMA)
        self.assertNotIn("digest", first.canonical_record())
        self.assertFalse(first.canonical_bytes().endswith(b"\n"))

    def test_values_are_frozen_and_require_exact_nested_types(self) -> None:
        value = endpoint("prediction")
        binding = ContractBinding("prediction", value, value)
        plan = ComparisonPlan(
            "frozen-plan",
            TRAINING_DIGEST,
            SERVING_DIGEST,
            REGISTRY_DIGEST,
            (binding,),
        )

        with self.assertRaises(FrozenInstanceError):
            value.value_id = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            binding.contract_id = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            plan.bindings = ()  # type: ignore[misc]

        with self.assertRaisesRegex(
            ComparisonValidationError,
            "exact InterfaceEndpoint",
        ):
            ContractBinding(
                "bad-endpoint",
                object(),  # type: ignore[arg-type]
                None,
            )
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "exact ContractBinding",
        ):
            ComparisonPlan(
                "bad-binding",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (object(),),  # type: ignore[arg-type]
            )

    def test_receiver_subclasses_fail_closed(self) -> None:
        class EndpointSubclass(InterfaceEndpoint):
            pass

        class BindingSubclass(ContractBinding):
            pass

        class PlanSubclass(ComparisonPlan):
            pass

        with self.assertRaisesRegex(
            ComparisonValidationError,
            "exact InterfaceEndpoint",
        ):
            EndpointSubclass(InterfaceRole.INPUT, "value")
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "exact ContractBinding",
        ):
            BindingSubclass("contract", endpoint("value"), None)
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "exact ComparisonPlan",
        ):
            PlanSubclass(
                "comparison",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (ContractBinding("contract", endpoint("value"), None),),
            )

    def test_identifiers_roles_and_digests_are_exact(self) -> None:
        for bad_id in ("", "Uppercase", "has_underscore", "a" * 65, 1):
            with (
                self.subTest(identifier=bad_id),
                self.assertRaisesRegex(
                    ComparisonValidationError,
                    "not canonical",
                ),
            ):
                endpoint(bad_id)  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            ComparisonValidationError,
            "role is unsupported",
        ):
            InterfaceEndpoint("input", "value")  # type: ignore[arg-type]

        for field, digest in (
            ("training", "A" * 64),
            ("serving", "0" * 63),
            ("registry", 1),
        ):
            values: dict[str, object] = {
                "training_graph_digest": TRAINING_DIGEST,
                "serving_graph_digest": SERVING_DIGEST,
                "registry_digest": REGISTRY_DIGEST,
            }
            values[
                f"{field}_graph_digest" if field != "registry" else "registry_digest"
            ] = digest
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ComparisonValidationError,
                    "digest is malformed",
                ),
            ):
                ComparisonPlan(
                    comparison_id="digest-plan",
                    training_graph_digest=values["training_graph_digest"],  # type: ignore[arg-type]
                    serving_graph_digest=values["serving_graph_digest"],  # type: ignore[arg-type]
                    registry_digest=values["registry_digest"],  # type: ignore[arg-type]
                    bindings=(ContractBinding("mapped", endpoint("x"), None),),
                )

    def test_bindings_are_nonempty_bounded_sorted_and_unique(self) -> None:
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "at least one binding",
        ):
            ComparisonPlan(
                "empty-plan",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (),
            )

        bindings = tuple(
            ContractBinding(
                f"binding-{index:03d}", endpoint(f"value-{index:03d}"), None
            )
            for index in range(MAX_COMPARISON_BINDINGS + 1)
        )
        with self.assertRaisesRegex(ComparisonValidationError, "too many"):
            ComparisonPlan(
                "oversized-plan",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                bindings,
            )

        with self.assertRaisesRegex(ComparisonValidationError, "sorted"):
            ComparisonPlan(
                "unsorted-plan",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (
                    ContractBinding("zeta", endpoint("z"), None),
                    ContractBinding("alpha", endpoint("a"), None),
                ),
            )

        with self.assertRaisesRegex(ComparisonValidationError, "unique"):
            ComparisonPlan(
                "duplicate-plan",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (
                    ContractBinding("same", endpoint("a"), None),
                    ContractBinding("same", endpoint("b"), None),
                ),
            )

    def test_binding_requires_at_least_one_explicit_side(self) -> None:
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "training or serving",
        ):
            ContractBinding("unmapped", None, None)

        training_only = ContractBinding("training-only", endpoint("label"), None)
        serving_only = ContractBinding("serving-only", None, endpoint("response"))
        self.assertIsNone(training_only.serving)
        self.assertIsNone(serving_only.training)

    def test_endpoint_occurrences_are_unique_within_each_side(self) -> None:
        duplicate = endpoint("score")
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "training interface endpoints",
        ):
            ComparisonPlan(
                "training-duplicate",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (
                    ContractBinding("alpha", duplicate, None),
                    ContractBinding("beta", duplicate, None),
                ),
            )
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "serving interface endpoints",
        ):
            ComparisonPlan(
                "serving-duplicate",
                TRAINING_DIGEST,
                SERVING_DIGEST,
                REGISTRY_DIGEST,
                (
                    ContractBinding("alpha", None, duplicate),
                    ContractBinding("beta", None, duplicate),
                ),
            )

        different_role = ComparisonPlan(
            "role-distinguishes-occurrence",
            TRAINING_DIGEST,
            SERVING_DIGEST,
            REGISTRY_DIGEST,
            (
                ContractBinding(
                    "alpha",
                    endpoint("shared", InterfaceRole.INPUT),
                    None,
                ),
                ContractBinding(
                    "beta",
                    endpoint("shared", InterfaceRole.OUTPUT),
                    None,
                ),
            ),
        )
        self.assertEqual(len(different_role.bindings), 2)

    def test_same_endpoint_may_appear_once_on_each_graph_side(self) -> None:
        shared = endpoint("prediction")
        plan = ComparisonPlan(
            "same-name-cross-side",
            TRAINING_DIGEST,
            SERVING_DIGEST,
            REGISTRY_DIGEST,
            (ContractBinding("prediction", shared, shared),),
        )
        self.assertEqual(plan.bindings[0].training, plan.bindings[0].serving)

    def test_role_mismatch_is_preserved_for_the_comparison_engine(self) -> None:
        plan = ComparisonPlan(
            "role-mismatch",
            TRAINING_DIGEST,
            SERVING_DIGEST,
            REGISTRY_DIGEST,
            (
                ContractBinding(
                    "prediction",
                    endpoint("prediction", InterfaceRole.OUTPUT),
                    endpoint("request", InterfaceRole.INPUT),
                ),
            ),
        )
        record = plan.canonical_record()
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        self.assertEqual(
            binding["training"], {"role": "output", "value_id": "prediction"}
        )
        self.assertEqual(binding["serving"], {"role": "input", "value_id": "request"})

    def test_nested_mutation_is_detected_by_the_plan_digest(self) -> None:
        plan = sample_plan()
        training = plan.bindings[0].training
        assert training is not None
        object.__setattr__(training, "value_id", "changed-temperature")

        with self.assertRaisesRegex(
            ComparisonValidationError,
            "digest does not match",
        ):
            plan.validate()
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "digest does not match",
        ):
            _ = plan.digest

    def test_malformed_or_replaced_plan_digest_is_detected(self) -> None:
        plan = sample_plan()
        object.__setattr__(plan, "_digest", "not-a-digest")
        with self.assertRaisesRegex(ComparisonValidationError, "digest is malformed"):
            plan.canonical_bytes()

        plan = sample_plan()
        object.__setattr__(plan, "_digest", "0" * 64)
        with self.assertRaisesRegex(
            ComparisonValidationError,
            "digest does not match",
        ):
            plan.canonical_record()


class ComparisonCodecTests(unittest.TestCase):
    def test_round_trip_preserves_exact_bytes_and_digest(self) -> None:
        plan = sample_plan()
        payload = encode_comparison_plan(plan)
        decoded = decode_comparison_plan(payload)

        self.assertEqual(decoded, plan)
        self.assertEqual(decoded.digest, plan.digest)
        self.assertEqual(encode_comparison_plan(decoded), payload)
        self.assertEqual(payload, canonical_json_bytes(decoded_record()))

    def test_maximum_fully_two_sided_plan_round_trips_within_codec_limits(
        self,
    ) -> None:
        def maximal_identifier(prefix: str, index: int) -> str:
            suffix = f"{index:03d}"
            return prefix + ("a" * (64 - len(prefix) - len(suffix))) + suffix

        plan = ComparisonPlan(
            comparison_id=maximal_identifier("comparison", 0),
            training_graph_digest=TRAINING_DIGEST,
            serving_graph_digest=SERVING_DIGEST,
            registry_digest=REGISTRY_DIGEST,
            bindings=tuple(
                ContractBinding(
                    contract_id=maximal_identifier("contract", index),
                    training=endpoint(
                        maximal_identifier("training", index),
                        InterfaceRole.OUTPUT,
                    ),
                    serving=endpoint(
                        maximal_identifier("serving", index),
                        InterfaceRole.INPUT,
                    ),
                )
                for index in range(MAX_COMPARISON_BINDINGS)
            ),
        )

        payload = encode_comparison_plan(plan)
        self.assertGreater(len(payload), 65_536)
        self.assertLessEqual(len(payload), MAX_COMPARISON_BYTES)
        self.assertEqual(decode_comparison_plan(payload), plan)

    def test_encoder_requires_an_exact_unmutated_plan(self) -> None:
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "exact ComparisonPlan",
        ):
            encode_comparison_plan(object())  # type: ignore[arg-type]

        plan = sample_plan()
        object.__setattr__(plan.bindings[0], "contract_id", "changed-contract")
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "comparison encoding failed",
        ):
            encode_comparison_plan(plan)

    def test_payload_type_size_encoding_and_bom_fail_closed(self) -> None:
        cases = (
            (bytearray(b"{}"), "exact bytes"),
            (b"", "empty"),
            (b" " * (MAX_COMPARISON_BYTES + 1), "byte limit"),
            (b"\xef\xbb\xbf{}", "BOM"),
            (b"\xff", "valid UTF-8"),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ComparisonDecodeError, message),
            ):
                decode_comparison_plan(payload)  # type: ignore[arg-type]

    def test_duplicate_float_nonfinite_and_noncanonical_json_fail_closed(
        self,
    ) -> None:
        canonical = encode_comparison_plan(sample_plan())
        payloads = (
            (b"{", "valid bounded JSON"),
            (b'{"schema":1,"schema":2}', "duplicate"),
            (b'{"bindings":[1.0]}', "floating-point"),
            (b'{"bindings":[NaN]}', "non-finite"),
            (b'{"bindings":[Infinity]}', "non-finite"),
            (canonical + b"\n", "not canonical JSON"),
            (b" " + canonical, "not canonical JSON"),
            (
                canonical.replace(b"checkout-model", b"checkout\\u002dmodel"),
                "not canonical JSON",
            ),
        )
        for payload, message in payloads:
            with (
                self.subTest(payload=payload[:80]),
                self.assertRaisesRegex(ComparisonDecodeError, message),
            ):
                decode_comparison_plan(payload)

    def test_resource_limits_precede_semantic_validation(self) -> None:
        huge_string = canonical_json_bytes(
            {"x": "a" * (MAX_COMPARISON_JSON_STRING_LENGTH + 1)}
        )
        huge_array = canonical_json_bytes(
            {"x": [None] * (MAX_COMPARISON_JSON_CONTAINER_ITEMS + 1)}
        )
        too_many_values = canonical_json_bytes(
            [
                [None] * MAX_COMPARISON_JSON_CONTAINER_ITEMS
                for _ in range(
                    (
                        MAX_COMPARISON_JSON_TOTAL_VALUES
                        // MAX_COMPARISON_JSON_CONTAINER_ITEMS
                    )
                    + 1
                )
            ]
        )
        nested: object = None
        for _ in range(MAX_COMPARISON_JSON_DEPTH + 1):
            nested = [nested]

        cases = (
            (huge_string, "string exceeds"),
            (huge_array, "array exceeds"),
            (too_many_values, "JSON value limit"),
            (canonical_json_bytes(nested), "nesting limit"),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ComparisonDecodeError, message),
            ):
                decode_comparison_plan(payload)

    def test_key_order_and_exact_root_fields_are_enforced(self) -> None:
        record = decoded_record()
        unsorted = json.dumps(
            dict(reversed(tuple(record.items()))),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode()
        with self.assertRaisesRegex(ComparisonDecodeError, "not canonical JSON"):
            decode_comparison_plan(unsorted)

        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "document must be an object",
        ):
            decode_comparison_plan(b"null")

        record = decoded_record()
        record["extension"] = {}
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "missing or unknown fields",
        ):
            decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        del record["registry_digest"]
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "missing or unknown fields",
        ):
            decode_comparison_plan(canonical_json_bytes(record))

    def test_schema_arrays_and_nested_fields_are_strict(self) -> None:
        record = decoded_record()
        record["schema"] = "unitsentinel.training-serving-comparison/v2"
        with self.assertRaisesRegex(ComparisonDecodeError, "not supported"):
            decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        record["bindings"] = {}
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "bindings must be an array",
        ):
            decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        binding["extension"] = None
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "missing or unknown fields",
        ):
            decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        training = binding["training"]
        assert isinstance(training, dict)
        training["extension"] = None
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "missing or unknown fields",
        ):
            decode_comparison_plan(canonical_json_bytes(record))

    def test_nested_types_roles_nulls_and_semantics_are_strict(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("contract_id", 1, "contract identifier must be text"),
            ("training", [], "training endpoint must be an object"),
        )
        for field, value, message in cases:
            record = decoded_record()
            bindings = record["bindings"]
            assert isinstance(bindings, list)
            binding = bindings[0]
            assert isinstance(binding, dict)
            binding[field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ComparisonDecodeError, message),
            ):
                decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        training = binding["training"]
        assert isinstance(training, dict)
        training["role"] = "internal"
        with self.assertRaisesRegex(ComparisonDecodeError, "role is unsupported"):
            decode_comparison_plan(canonical_json_bytes(record))

        record = decoded_record()
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        binding["training"] = None
        binding["serving"] = None
        with self.assertRaisesRegex(
            ComparisonDecodeError,
            "semantic contract failed",
        ):
            decode_comparison_plan(canonical_json_bytes(record))


if __name__ == "__main__":
    unittest.main()
