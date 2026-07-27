from __future__ import annotations

import copy
import json
import math
import unittest
from collections.abc import Callable
from fractions import Fraction
from typing import cast

from unitsentinel.canonical import canonical_json_bytes, sha256_hex
from unitsentinel.certificate import (
    MAX_CERTIFICATE_CHECKS,
    MAX_CERTIFICATE_CONSTRAINTS,
    MAX_CERTIFICATE_SOLVER_VERSION_LENGTH,
)
from unitsentinel.comparison import (
    ComparisonReason,
    ComparisonResult,
    ComparisonStatus,
    MismatchCode,
    compare_graphs,
)
from unitsentinel.comparison_contract import (
    MAX_COMPARISON_BINDINGS,
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from unitsentinel.comparison_result_codec import (
    MAX_COMPARISON_RESULT_BYTES,
    MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS,
    MAX_COMPARISON_RESULT_JSON_DEPTH,
    MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS,
    MAX_COMPARISON_RESULT_JSON_STRING_LENGTH,
    MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES,
    ComparisonResultDecodeError,
    decode_comparison_result,
    encode_comparison_result,
)
from unitsentinel.domain import (
    BASE_DIMENSION_COUNT,
    BaseDimension,
    Dimension,
    QuantityKind,
)
from unitsentinel.graph import (
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    MAX_GRAPH_VALUES,
    MAX_TENSOR_RANK,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY
from unitsentinel.verification import (
    ConstraintSource,
    ConstraintWitness,
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationResult,
    VerificationStatus,
)

ResultRecord = dict[str, object]
RecordMutation = Callable[[ResultRecord], None]


def value(
    value_id: str,
    unit_id: str | None,
    *,
    shape: tuple[int | str, ...] = (),
) -> ValueSpec:
    return ValueSpec(value_id, ScalarType.FLOAT64, shape, unit_id)


def endpoint(role: InterfaceRole, value_id: str) -> InterfaceEndpoint:
    return InterfaceEndpoint(role, value_id)


def ratio_graph(
    graph_id: str,
    *,
    left_id: str,
    right_id: str,
    output_id: str,
    reverse: bool = False,
) -> ComputationGraph:
    inputs = (right_id, left_id) if reverse else (left_id, right_id)
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(
            sorted(
                (
                    value(left_id, "centimeter", shape=(2, "batch")),
                    value(right_id, "centimeter", shape=(2, "batch")),
                    value(output_id, "one", shape=(2, "batch")),
                ),
                key=lambda item: item.value_id,
            )
        ),
        inputs=(left_id, right_id),
        nodes=(Node("normalize", Operation.DIVIDE, inputs, output_id),),
        outputs=(output_id,),
    )


def ratio_plan(
    training: ComputationGraph,
    serving: ComputationGraph,
) -> ComparisonPlan:
    return ComparisonPlan(
        comparison_id="codec-ratio",
        training_graph_digest=training.digest,
        serving_graph_digest=serving.digest,
        registry_digest=BUILTIN_REGISTRY.digest,
        bindings=(
            ContractBinding(
                "input-left",
                endpoint(InterfaceRole.INPUT, training.inputs[0]),
                endpoint(InterfaceRole.INPUT, serving.inputs[0]),
            ),
            ContractBinding(
                "input-right",
                endpoint(InterfaceRole.INPUT, training.inputs[1]),
                endpoint(InterfaceRole.INPUT, serving.inputs[1]),
            ),
            ContractBinding(
                "output-ratio",
                endpoint(InterfaceRole.OUTPUT, training.outputs[0]),
                endpoint(InterfaceRole.OUTPUT, serving.outputs[0]),
            ),
        ),
    )


def decisive_result(*, reverse_serving: bool = False) -> ComparisonResult:
    training = ratio_graph(
        "codec-training",
        left_id="training-left",
        right_id="training-right",
        output_id="training-ratio",
    )
    serving = ratio_graph(
        "codec-serving",
        left_id="feature-left",
        right_id="feature-right",
        output_id="prediction",
        reverse=reverse_serving,
    )
    return compare_graphs(
        ratio_plan(training, serving),
        training_graph=training,
        serving_graph=serving,
    )


def attributed_graph(graph_id: str) -> ComputationGraph:
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(
            sorted(
                (
                    value("converted", "centimeter", shape=(2, "batch")),
                    value("distance", "meter", shape=(2, "batch")),
                    value("published", "centimeter", shape=(2, "batch")),
                    value("squared", None, shape=(2, "batch")),
                ),
                key=lambda item: item.value_id,
            )
        ),
        inputs=("distance",),
        nodes=(
            Node(
                "convert-distance",
                Operation.CONVERT,
                ("distance",),
                "converted",
                target_unit_id="centimeter",
            ),
            Node(
                "square-distance",
                Operation.POWER,
                ("distance",),
                "squared",
                exponent=Fraction(2),
            ),
            Node(
                "publish-converted",
                Operation.IDENTITY,
                ("converted",),
                "published",
            ),
        ),
        outputs=("published", "squared"),
    )


def attributed_result() -> ComparisonResult:
    candidate = attributed_graph("codec-attributes")
    plan = ComparisonPlan(
        comparison_id="codec-attributes",
        training_graph_digest=candidate.digest,
        serving_graph_digest=candidate.digest,
        registry_digest=BUILTIN_REGISTRY.digest,
        bindings=(
            ContractBinding(
                "input-distance",
                endpoint(InterfaceRole.INPUT, "distance"),
                endpoint(InterfaceRole.INPUT, "distance"),
            ),
            ContractBinding(
                "output-converted",
                endpoint(InterfaceRole.OUTPUT, "published"),
                endpoint(InterfaceRole.OUTPUT, "published"),
            ),
            ContractBinding(
                "output-squared",
                endpoint(InterfaceRole.OUTPUT, "squared"),
                endpoint(InterfaceRole.OUTPUT, "squared"),
            ),
        ),
    )
    return compare_graphs(
        plan,
        training_graph=candidate,
        serving_graph=candidate,
    )


def inferred(value_id: str) -> InferredContract:
    unit = BUILTIN_REGISTRY.resolve("meter")
    return InferredContract(
        value_id=value_id,
        dimension=unit.dimension,
        kind=unit.kind,
        scale=unit.scale,
        offset=unit.offset,
    )


def verification_result(
    status: VerificationStatus,
    *,
    graph_digest: str,
    registry_digest: str,
    limits: SolverLimits,
) -> VerificationResult:
    common: dict[str, object] = {
        "status": status,
        "graph_digest": graph_digest,
        "registry_digest": registry_digest,
        "solver_version": "1.2.3",
        "limits": limits,
        "checks_performed": 7,
    }
    if status is VerificationStatus.VERIFIED:
        common["contracts"] = (inferred("claim-value"),)
    elif status is VerificationStatus.UNDERCONSTRAINED:
        common["underconstrained_values"] = ("claim-value",)
    elif status is VerificationStatus.CONFLICT:
        common["conflict_core"] = (
            ConstraintWitness(
                "operation/claim",
                ConstraintSource.OPERATION,
                "claim-node",
                "divide",
            ),
        )
        common["core_minimal"] = True
    else:
        common["unknown_reason"] = UnknownReason.RESOURCE_LIMIT
    return VerificationResult(**common)  # type: ignore[arg-type]


def nonverified_result(
    training_status: VerificationStatus,
    serving_status: VerificationStatus,
) -> ComparisonResult:
    limits = SolverLimits()
    registry_digest = BUILTIN_REGISTRY.digest
    training_digest = "1" * 64
    serving_digest = "2" * 64
    training = verification_result(
        training_status,
        graph_digest=training_digest,
        registry_digest=registry_digest,
        limits=limits,
    )
    serving = verification_result(
        serving_status,
        graph_digest=serving_digest,
        registry_digest=registry_digest,
        limits=limits,
    )
    training_verified = training_status is VerificationStatus.VERIFIED
    serving_verified = serving_status is VerificationStatus.VERIFIED
    reason = (
        ComparisonReason.BOTH_NOT_VERIFIED
        if not training_verified and not serving_verified
        else ComparisonReason.TRAINING_NOT_VERIFIED
        if not training_verified
        else ComparisonReason.SERVING_NOT_VERIFIED
    )
    return ComparisonResult(
        status=ComparisonStatus.INDETERMINATE,
        reason=reason,
        comparison_id="detached-outcome",
        plan_digest="3" * 64,
        training_graph_digest=training_digest,
        serving_graph_digest=serving_digest,
        registry_digest=registry_digest,
        limits=limits,
        training_result=training,
        serving_result=serving,
        training_lineage=None,
        serving_lineage=None,
    )


def record_object(value: object) -> ResultRecord:
    return cast(ResultRecord, value)


def record_array(value: object) -> list[object]:
    return cast(list[object], value)


def detached_record(value: object) -> ResultRecord:
    return record_object(record_object(value)["record"])


def encode_mutation(source: ComparisonResult, mutation: RecordMutation) -> bytes:
    record = copy.deepcopy(source.canonical_record())
    mutation(record)
    return canonical_json_bytes(record)


def replace_claim_digest(claim: ResultRecord) -> None:
    claim["sha256"] = sha256_hex(canonical_json_bytes(claim["record"]))


class ComparisonResultRoundTripTests(unittest.TestCase):
    def test_decisive_result_round_trips_every_nested_claim(self) -> None:
        source = decisive_result()

        payload = encode_comparison_result(source)
        decoded = decode_comparison_result(payload)

        self.assertEqual(decoded, source)
        self.assertEqual(decoded.digest, source.digest)
        self.assertEqual(decoded.canonical_bytes(), payload)
        self.assertEqual(encode_comparison_result(decoded), payload)
        self.assertIsNot(decoded.training_result, source.training_result)
        self.assertIsNot(decoded.training_lineage, source.training_lineage)
        assert decoded.training_lineage is not None
        self.assertEqual(len(decoded.training_lineage.sites), 1)
        self.assertEqual(len(decoded.training_lineage.outputs), 1)
        self.assertEqual(
            decoded.training_lineage.verification_result.digest,
            decoded.training_result.digest,  # type: ignore[union-attr]
        )

    def test_drift_result_preserves_normalization_mismatch(self) -> None:
        source = decisive_result(reverse_serving=True)

        decoded = decode_comparison_result(encode_comparison_result(source))

        self.assertEqual(decoded.status, ComparisonStatus.DRIFT)
        self.assertEqual(
            decoded.comparisons[-1].mismatches,
            (MismatchCode.NORMALIZATION_LINEAGE_DRIFT,),
        )

    def test_power_convert_identity_and_symbolic_shapes_round_trip(self) -> None:
        source = attributed_result()

        decoded = decode_comparison_result(encode_comparison_result(source))

        assert decoded.training_lineage is not None
        by_operation = {
            expression.operation: expression
            for expression in decoded.training_lineage.expressions
            if expression.operation is not None
        }
        self.assertEqual(
            by_operation[Operation.POWER].attributes,
            (("exponent", "2"),),
        )
        self.assertEqual(
            by_operation[Operation.CONVERT].attributes,
            (("unit_id", "centimeter"),),
        )
        self.assertTrue(by_operation[Operation.IDENTITY].collapsed_identity)
        self.assertEqual(
            decoded.comparisons[0].training.value.shape,  # type: ignore[union-attr]
            (2, "batch"),
        )

    def test_all_nonverified_verification_outcomes_round_trip(self) -> None:
        cases = (
            (VerificationStatus.UNDERCONSTRAINED, VerificationStatus.CONFLICT),
            (VerificationStatus.UNKNOWN, VerificationStatus.VERIFIED),
            (VerificationStatus.VERIFIED, VerificationStatus.UNKNOWN),
        )
        seen: set[VerificationStatus] = set()
        for training_status, serving_status in cases:
            with self.subTest(
                training=training_status,
                serving=serving_status,
            ):
                source = nonverified_result(training_status, serving_status)
                decoded = decode_comparison_result(encode_comparison_result(source))
                assert decoded.training_result is not None
                assert decoded.serving_result is not None
                seen.add(decoded.training_result.status)
                seen.add(decoded.serving_result.status)
                self.assertEqual(decoded, source)
                self.assertIsNone(decoded.training_lineage)
                self.assertFalse(decoded.comparisons)
        self.assertEqual(seen, set(VerificationStatus))

    def test_verifier_and_lineage_failure_shapes_round_trip(self) -> None:
        decisive = decisive_result()
        verifier_failure = ComparisonResult(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.VERIFIER_FAILURE,
            comparison_id=decisive.comparison_id,
            plan_digest=decisive.plan_digest,
            training_graph_digest=decisive.training_graph_digest,
            serving_graph_digest=decisive.serving_graph_digest,
            registry_digest=decisive.registry_digest,
            limits=decisive.limits,
            training_result=None,
            serving_result=None,
            training_lineage=None,
            serving_lineage=None,
        )
        lineage_failure = ComparisonResult(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.NORMALIZATION_LINEAGE_FAILURE,
            comparison_id=decisive.comparison_id,
            plan_digest=decisive.plan_digest,
            training_graph_digest=decisive.training_graph_digest,
            serving_graph_digest=decisive.serving_graph_digest,
            registry_digest=decisive.registry_digest,
            limits=decisive.limits,
            training_result=decisive.training_result,
            serving_result=decisive.serving_result,
            training_lineage=None,
            serving_lineage=None,
        )

        for source in (verifier_failure, lineage_failure):
            with self.subTest(reason=source.reason):
                self.assertEqual(
                    decode_comparison_result(encode_comparison_result(source)),
                    source,
                )

    def test_encoder_requires_an_exact_unmutated_bounded_model(self) -> None:
        source = decisive_result()
        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "exact ComparisonResult",
        ):
            encode_comparison_result(object())  # type: ignore[arg-type]

        object.__setattr__(source, "_digest", "0" * 64)
        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "encoding failed",
        ):
            encode_comparison_result(source)

    def test_encoder_enforces_the_transport_string_limit(self) -> None:
        limits = SolverLimits()
        graph_digest = "1" * 64
        registry_digest = BUILTIN_REGISTRY.digest
        solver_version = "1." + ("0" * MAX_COMPARISON_RESULT_JSON_STRING_LENGTH) + ".0"
        claim = VerificationResult(
            status=VerificationStatus.UNKNOWN,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version=solver_version,
            limits=limits,
            checks_performed=0,
            unknown_reason=UnknownReason.SOLVER_UNKNOWN,
        )
        source = ComparisonResult(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.VERIFIER_FAILURE,
            comparison_id="bounded-encoding",
            plan_digest="2" * 64,
            training_graph_digest=graph_digest,
            serving_graph_digest="3" * 64,
            registry_digest=registry_digest,
            limits=limits,
            training_result=claim,
            serving_result=None,
            training_lineage=None,
            serving_lineage=None,
        )

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "string exceeds the length limit",
        ):
            encode_comparison_result(source)

    def test_encoder_rejects_exact_models_beyond_semantic_transport_caps(
        self,
    ) -> None:
        limits = SolverLimits()
        graph_digest = "1" * 64
        registry_digest = BUILTIN_REGISTRY.digest
        contracts = tuple(
            inferred(f"value-{index:03d}") for index in range(MAX_GRAPH_VALUES + 1)
        )
        claim = VerificationResult(
            status=VerificationStatus.VERIFIED,
            graph_digest=graph_digest,
            registry_digest=registry_digest,
            solver_version="1.2.3",
            limits=limits,
            checks_performed=MAX_CERTIFICATE_CHECKS,
            contracts=contracts,
        )
        source = ComparisonResult(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.VERIFIER_FAILURE,
            comparison_id="semantic-transport-bound",
            plan_digest="2" * 64,
            training_graph_digest=graph_digest,
            serving_graph_digest="3" * 64,
            registry_digest=registry_digest,
            limits=limits,
            training_result=claim,
            serving_result=None,
            training_lineage=None,
            serving_lineage=None,
        )

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "inferred contracts exceeds its item limit",
        ):
            encode_comparison_result(source)


class ComparisonResultJSONBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = decisive_result()
        cls.payload = encode_comparison_result(cls.source)

    def test_rejects_unsafe_and_noncanonical_json(self) -> None:
        reversed_record = dict(reversed(list(self.source.canonical_record().items())))
        unsorted = json.dumps(
            reversed_record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode()
        cases: tuple[tuple[str, object, str], ...] = (
            ("wrong-type", "result", "exact bytes"),
            ("bytearray", bytearray(self.payload), "exact bytes"),
            ("empty", b"", "payload is empty"),
            ("bom", b"\xef\xbb\xbf" + self.payload, "UTF-8 BOM"),
            ("invalid-utf8", b"\xff", "not valid UTF-8"),
            ("trailing-newline", self.payload + b"\n", "not canonical JSON"),
            ("unsorted", unsorted, "not canonical JSON"),
            (
                "duplicate-key",
                b'{"schema":"first","schema":"second"}',
                "duplicate object key",
            ),
            ("float", b'{"value":1.5}', "floating-point"),
            ("nonfinite", b'{"value":NaN}', "non-finite"),
            (
                "oversized",
                b"0" * (MAX_COMPARISON_RESULT_BYTES + 1),
                "byte limit",
            ),
        )

        for name, payload, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(payload)  # type: ignore[arg-type]

    def test_rejects_each_structural_resource_limit(self) -> None:
        too_deep = (
            b"[" * (MAX_COMPARISON_RESULT_JSON_DEPTH + 1)
            + b"0"
            + b"]" * (MAX_COMPARISON_RESULT_JSON_DEPTH + 1)
        )
        too_wide = b"[" + b"0," * MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS + b"0]"
        inner_item_count = MAX_COMPARISON_RESULT_JSON_CONTAINER_ITEMS
        inner = b"[" + b"0," * (inner_item_count - 1) + b"0]"
        group_count = (
            math.ceil(MAX_COMPARISON_RESULT_JSON_TOTAL_VALUES / (inner_item_count + 1))
            + 1
        )
        too_many_values = b"[" + inner + (b"," + inner) * (group_count - 1) + b"]"
        too_long_string = (
            b'"' + b"a" * (MAX_COMPARISON_RESULT_JSON_STRING_LENGTH + 1) + b'"'
        )
        too_large_integer = b"1" * (MAX_COMPARISON_RESULT_JSON_INTEGER_DIGITS + 1)
        cases = (
            ("depth", too_deep, "nesting limit"),
            ("container", too_wide, "item limit"),
            ("values", too_many_values, "JSON value limit"),
            ("string", too_long_string, "string exceeds the length limit"),
            ("integer", too_large_integer, "integer exceeds the digit limit"),
        )

        for name, payload, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(payload)

    def test_requires_exact_root_identity_and_binding_records(self) -> None:
        def extension(record: ResultRecord) -> None:
            record["extension"] = None

        def missing(record: ResultRecord) -> None:
            del record["scope"]

        def wrong_graph_shape(record: ResultRecord) -> None:
            record["graphs"] = []

        def malformed_graph_digest(record: ResultRecord) -> None:
            record_object(record["graphs"])["training_sha256"] = "0"

        def wrong_bindings_shape(record: ResultRecord) -> None:
            record["bindings"] = {}

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("extension", extension, "missing or unknown fields"),
            ("missing", missing, "missing or unknown fields"),
            (
                "schema",
                lambda record: record.__setitem__("schema", "result/v2"),
                "schema is not supported",
            ),
            (
                "authentication",
                lambda record: record.__setitem__("authentication", "signed"),
                "authentication is not supported",
            ),
            (
                "scope",
                lambda record: record.__setitem__("scope", "global"),
                "scope is not supported",
            ),
            (
                "status",
                lambda record: record.__setitem__("status", "success"),
                "status is not supported",
            ),
            (
                "reason-type",
                lambda record: record.__setitem__("reason", 1),
                "reason must be text",
            ),
            (
                "reason-value",
                lambda record: record.__setitem__("reason", "other"),
                "reason is not supported",
            ),
            ("graph-shape", wrong_graph_shape, "graph bindings must be an object"),
            ("graph-digest", malformed_graph_digest, "graph digest is malformed"),
            (
                "bindings-shape",
                wrong_bindings_shape,
                "result bindings must be an array",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_exact_integer_and_solver_limit_violations(self) -> None:
        def limit_value(record: ResultRecord, value: object) -> None:
            record_object(record["limits"])["per_check_timeout_ms"] = value

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            (
                "boolean",
                lambda record: limit_value(record, True),
                "exact integer",
            ),
            (
                "range",
                lambda record: limit_value(record, 0),
                "out of bounds",
            ),
            (
                "unknown-limit",
                lambda record: record_object(record["limits"]).__setitem__(
                    "extension",
                    1,
                ),
                "missing or unknown fields",
            ),
        )
        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))


class ComparisonResultSemanticBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = decisive_result()

    def test_rejects_each_semantic_collection_before_reconstruction(self) -> None:
        def training_verification(record: ResultRecord) -> ResultRecord:
            verification = record_object(record["verification"])
            return detached_record(verification["training"])

        def training_lineage(record: ResultRecord) -> ResultRecord:
            lineages = record_object(record["normalization_lineage"])
            return detached_record(lineages["training"])

        def operation_expression(record: ResultRecord) -> ResultRecord:
            expressions = record_array(training_lineage(record)["expressions"])
            return detached_record(expressions[-1])

        def first_site(record: ResultRecord) -> ResultRecord:
            sites = record_array(training_lineage(record)["sites"])
            return detached_record(sites[0])

        def first_output(record: ResultRecord) -> ResultRecord:
            outputs = record_array(training_lineage(record)["outputs"])
            return detached_record(outputs[0])

        def output_binding(record: ResultRecord) -> ResultRecord:
            return record_object(record_array(record["bindings"])[-1])

        def output_snapshot(record: ResultRecord) -> ResultRecord:
            return record_object(output_binding(record)["training"])

        def too_many_bindings(record: ResultRecord) -> None:
            record["bindings"] = [{}] * (MAX_COMPARISON_BINDINGS + 1)

        def long_solver_version(record: ResultRecord) -> None:
            length = MAX_CERTIFICATE_SOLVER_VERSION_LENGTH
            version = "1." + ("0" * (length - 3)) + ".0"
            self.assertEqual(len(version), length + 1)
            training_verification(record)["solver_version"] = version

        def too_many_conflicts(record: ResultRecord) -> None:
            training_verification(record)["conflict_core"] = [{}] * (
                MAX_CERTIFICATE_CONSTRAINTS + 1
            )

        def too_many_expressions(record: ResultRecord) -> None:
            training_lineage(record)["expressions"] = [{}] * (MAX_GRAPH_VALUES + 1)

        def too_many_sites(record: ResultRecord) -> None:
            training_lineage(record)["sites"] = [{}] * (MAX_GRAPH_NODES + 1)

        def too_many_outputs(record: ResultRecord) -> None:
            training_lineage(record)["outputs"] = [{}] * (MAX_GRAPH_OUTPUTS + 1)

        def wrong_expression_arity(record: ResultRecord) -> None:
            expression = operation_expression(record)
            expression["input_value_ids"] = ["value"] * 3
            expression["children_sha256"] = ["0" * 64] * 3

        def too_many_expression_roots(record: ResultRecord) -> None:
            operation_expression(record)["logical_roots"] = ["root"] * (
                MAX_GRAPH_INPUTS + 1
            )

        def too_many_site_roots(record: ResultRecord) -> None:
            first_site(record)["logical_roots"] = ["root"] * (MAX_GRAPH_INPUTS + 1)

        def too_many_site_outputs(record: ResultRecord) -> None:
            first_site(record)["logical_outputs"] = ["output"] * (MAX_GRAPH_OUTPUTS + 1)

        def too_many_multiset_records(record: ResultRecord) -> None:
            output = first_output(record)
            multiset = record_array(output["site_sha256_multiset"])
            output["site_sha256_multiset"] = [copy.deepcopy(multiset[0])] * (
                MAX_GRAPH_NODES + 1
            )

        def too_many_mismatches(record: ResultRecord) -> None:
            output_binding(record)["mismatches"] = [MismatchCode.DTYPE_DRIFT.value] * (
                len(MismatchCode) + 1
            )

        def excessive_rank(record: ResultRecord) -> None:
            snapshot = output_snapshot(record)
            record_object(snapshot["value"])["shape"] = [1] * (MAX_TENSOR_RANK + 1)

        def excessive_dimension(record: ResultRecord) -> None:
            snapshot = output_snapshot(record)
            contract = record_object(snapshot["inferred"])
            contract["dimension"] = [{"base": "length", "exponent": "1"}] * (
                BASE_DIMENSION_COUNT + 1
            )

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("bindings", too_many_bindings, "result bindings exceeds its item limit"),
            ("solver-version", long_solver_version, "solver version exceeds"),
            ("conflict-core", too_many_conflicts, "array exceeds the item limit"),
            (
                "expressions",
                too_many_expressions,
                "lineage expressions exceeds its item limit",
            ),
            (
                "sites",
                too_many_sites,
                "normalization sites exceeds its item limit",
            ),
            (
                "outputs",
                too_many_outputs,
                "output lineages exceeds its item limit",
            ),
            ("expression-arity", wrong_expression_arity, "operation arity"),
            (
                "expression-roots",
                too_many_expression_roots,
                "lineage logical roots exceeds its item limit",
            ),
            (
                "site-roots",
                too_many_site_roots,
                "normalization logical roots exceeds its item limit",
            ),
            (
                "site-outputs",
                too_many_site_outputs,
                "normalization logical outputs exceeds its item limit",
            ),
            (
                "multiset-records",
                too_many_multiset_records,
                "output site digest multiset exceeds its item limit",
            ),
            (
                "mismatches",
                too_many_mismatches,
                "comparison mismatch codes exceeds its item limit",
            ),
            ("shape", excessive_rank, "value shape exceeds its item limit"),
            (
                "dimension",
                excessive_dimension,
                "inferred dimension exceeds its item limit",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_malformed_verification_claims_and_records(self) -> None:
        def training_claim(record: ResultRecord) -> ResultRecord:
            verification = record_object(record["verification"])
            return record_object(verification["training"])

        def add_claim_extension(record: ResultRecord) -> None:
            training_claim(record)["extension"] = None

        def malformed_claim_digest(record: ResultRecord) -> None:
            training_claim(record)["sha256"] = "0"

        def mismatched_claim_digest(record: ResultRecord) -> None:
            training_claim(record)["sha256"] = "0" * 64

        def verification_extension(record: ResultRecord) -> None:
            detached_record(training_claim(record))["extension"] = None

        def wrong_status(record: ResultRecord) -> None:
            detached_record(training_claim(record))["status"] = "success"

        def bool_checks(record: ResultRecord) -> None:
            detached_record(training_claim(record))["checks_performed"] = True

        def wrong_contract_kind(record: ResultRecord) -> None:
            result = detached_record(training_claim(record))
            contract = record_object(record_array(result["contracts"])[0])
            contract["kind"] = "other"

        def noncanonical_rational(record: ResultRecord) -> None:
            result = detached_record(training_claim(record))
            contract = record_object(record_array(result["contracts"])[0])
            contract["scale"] = "01"

        def too_many_checks(record: ResultRecord) -> None:
            detached_record(training_claim(record))["checks_performed"] = (
                MAX_CERTIFICATE_CHECKS + 1
            )

        def too_many_contracts(record: ResultRecord) -> None:
            result = detached_record(training_claim(record))
            result["contracts"] = [{}] * (MAX_GRAPH_VALUES + 1)

        def too_many_underconstrained(record: ResultRecord) -> None:
            result = detached_record(training_claim(record))
            result["underconstrained_values"] = ["value"] * (MAX_GRAPH_VALUES + 1)

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("claim-extension", add_claim_extension, "missing or unknown fields"),
            ("claim-digest-shape", malformed_claim_digest, "digest is malformed"),
            (
                "claim-digest-mismatch",
                mismatched_claim_digest,
                "does not match its contents",
            ),
            (
                "result-extension",
                verification_extension,
                "missing or unknown fields",
            ),
            ("status", wrong_status, "verification status is not supported"),
            ("checks", bool_checks, "solver check count must be an exact"),
            ("kind", wrong_contract_kind, "quantity kind is not supported"),
            ("rational", noncanonical_rational, "not a canonical rational"),
            ("checks-limit", too_many_checks, "fresh-result limit"),
            ("contract-limit", too_many_contracts, "item limit"),
            (
                "underconstrained-limit",
                too_many_underconstrained,
                "item limit",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_final_canonical_model_check_rejects_semantic_array_reordering(
        self,
    ) -> None:
        limits = SolverLimits()
        registry_digest = BUILTIN_REGISTRY.digest
        mixed_dimension = Dimension.from_mapping(
            {
                BaseDimension.LENGTH: Fraction(1),
                BaseDimension.TIME: Fraction(1),
            }
        )
        training = VerificationResult(
            status=VerificationStatus.UNDERCONSTRAINED,
            graph_digest="1" * 64,
            registry_digest=registry_digest,
            solver_version="1.2.3",
            limits=limits,
            checks_performed=1,
            contracts=(
                InferredContract(
                    "claim-value",
                    mixed_dimension,
                    QuantityKind.LINEAR,
                    Fraction(1),
                    Fraction(0),
                ),
            ),
            underconstrained_values=("claim-value",),
        )
        serving = verification_result(
            VerificationStatus.CONFLICT,
            graph_digest="2" * 64,
            registry_digest=registry_digest,
            limits=limits,
        )
        source = ComparisonResult(
            status=ComparisonStatus.INDETERMINATE,
            reason=ComparisonReason.BOTH_NOT_VERIFIED,
            comparison_id="canonical-array-order",
            plan_digest="3" * 64,
            training_graph_digest=training.graph_digest,
            serving_graph_digest=serving.graph_digest,
            registry_digest=registry_digest,
            limits=limits,
            training_result=training,
            serving_result=serving,
            training_lineage=None,
            serving_lineage=None,
        )

        def reverse_dimension(record: ResultRecord) -> None:
            verification = record_object(record["verification"])
            claim = record_object(verification["training"])
            result = detached_record(claim)
            contract = record_object(record_array(result["contracts"])[0])
            record_array(contract["dimension"]).reverse()

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "does not match the canonical result model",
        ):
            decode_comparison_result(encode_mutation(source, reverse_dimension))

    def test_rejects_coherent_verification_claim_bound_to_another_graph(
        self,
    ) -> None:
        def mutation(record: ResultRecord) -> None:
            verification = record_object(record["verification"])
            claim = record_object(verification["training"])
            detached_record(claim)["graph_digest"] = "4" * 64
            replace_claim_digest(claim)

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "source bindings are inconsistent",
        ):
            decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_lineage_claim_digest_and_identity_mutations(self) -> None:
        def training_claim(record: ResultRecord) -> ResultRecord:
            lineages = record_object(record["normalization_lineage"])
            return record_object(lineages["training"])

        def claim_extension(record: ResultRecord) -> None:
            training_claim(record)["extension"] = None

        def malformed_claim_digest(record: ResultRecord) -> None:
            training_claim(record)["sha256"] = "0"

        def wrong_schema(record: ResultRecord) -> None:
            detached_record(training_claim(record))["schema"] = "lineage/v2"

        def wrong_authentication(record: ResultRecord) -> None:
            detached_record(training_claim(record))["authentication"] = "signed"

        def wrong_side(record: ResultRecord) -> None:
            detached_record(training_claim(record))["side"] = "other"

        def wrong_semantic_digest(record: ResultRecord) -> None:
            detached_record(training_claim(record))["semantic_sha256"] = "0" * 64

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("claim-extension", claim_extension, "missing or unknown fields"),
            ("claim-digest", malformed_claim_digest, "digest is malformed"),
            ("schema", wrong_schema, "schema is not supported"),
            (
                "authentication",
                wrong_authentication,
                "authentication is not supported",
            ),
            ("side", wrong_side, "side is not supported"),
            (
                "semantic",
                wrong_semantic_digest,
                "semantic digest does not match",
            ),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_internally_coherent_lineage_bound_to_another_plan(self) -> None:
        def mutation(record: ResultRecord) -> None:
            lineages = record_object(record["normalization_lineage"])
            claim = record_object(lineages["training"])
            detached_record(claim)["plan_sha256"] = "5" * 64
            replace_claim_digest(claim)

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "lineage source bindings are inconsistent",
        ):
            decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_cross_side_lineage_evidence(self) -> None:
        def mutation(record: ResultRecord) -> None:
            lineages = record_object(record["normalization_lineage"])
            lineages["training"], lineages["serving"] = (
                lineages["serving"],
                lineages["training"],
            )

        with self.assertRaisesRegex(
            ComparisonResultDecodeError,
            "lineage source bindings are inconsistent",
        ):
            decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_expression_claim_and_attribute_mutations(self) -> None:
        def first_expression(record: ResultRecord) -> ResultRecord:
            lineages = record_object(record["normalization_lineage"])
            lineage = detached_record(lineages["training"])
            return record_object(record_array(lineage["expressions"])[0])

        def expression_record(record: ResultRecord) -> ResultRecord:
            return detached_record(first_expression(record))

        def wrong_envelope_digest(record: ResultRecord) -> None:
            first_expression(record)["sha256"] = "0" * 64

        def wrong_semantic_digest(record: ResultRecord) -> None:
            expression_record(record)["semantic_sha256"] = "0" * 64

        def unknown_attribute(record: ResultRecord) -> None:
            expression_record(record)["attributes"] = {"extension": "value"}

        def wrong_operation(record: ResultRecord) -> None:
            expression_record(record)["operation"] = "unsupported"

        def wrong_boolean(record: ResultRecord) -> None:
            expression_record(record)["collapsed_identity"] = 1

        def malformed_child_digest(record: ResultRecord) -> None:
            expressions = record_array(
                detached_record(
                    record_object(
                        record_object(record["normalization_lineage"])["training"]
                    )
                )["expressions"]
            )
            operation = detached_record(expressions[-1])
            record_array(operation["children_sha256"])[0] = "bad"

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("envelope", wrong_envelope_digest, "does not match its contents"),
            ("semantic", wrong_semantic_digest, "does not match its contents"),
            ("attribute", unknown_attribute, "missing or unknown fields"),
            ("operation", wrong_operation, "operation is not supported"),
            ("boolean", wrong_boolean, "must be an exact boolean"),
            ("child-digest", malformed_child_digest, "digest.*malformed"),
        )

        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_site_and_output_digest_mutations(self) -> None:
        def lineage_record(record: ResultRecord) -> ResultRecord:
            claims = record_object(record["normalization_lineage"])
            return detached_record(claims["training"])

        def first_site(record: ResultRecord) -> ResultRecord:
            return record_object(record_array(lineage_record(record)["sites"])[0])

        def first_output(record: ResultRecord) -> ResultRecord:
            return record_object(record_array(lineage_record(record)["outputs"])[0])

        def wrong_site_schema(record: ResultRecord) -> None:
            detached_record(first_site(record))["schema"] = "site/v2"

        def wrong_site_semantic(record: ResultRecord) -> None:
            detached_record(first_site(record))["site_sha256"] = "0" * 64

        def wrong_site_envelope(record: ResultRecord) -> None:
            first_site(record)["sha256"] = "0" * 64

        def wrong_output_normalization(record: ResultRecord) -> None:
            detached_record(first_output(record))["normalization_sha256"] = "0" * 64

        def wrong_output_envelope(record: ResultRecord) -> None:
            first_output(record)["sha256"] = "0" * 64

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("site-schema", wrong_site_schema, "schema is not supported"),
            ("site-semantic", wrong_site_semantic, "does not match its contents"),
            ("site-envelope", wrong_site_envelope, "does not match its contents"),
            (
                "output-normalization",
                wrong_output_normalization,
                "does not match its contents",
            ),
            ("output-envelope", wrong_output_envelope, "does not match its contents"),
        )
        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_malformed_or_expanding_digest_multisets(self) -> None:
        def multiset(record: ResultRecord) -> list[object]:
            lineages = record_object(record["normalization_lineage"])
            lineage = detached_record(lineages["training"])
            output = detached_record(record_array(lineage["outputs"])[0])
            return record_array(output["site_sha256_multiset"])

        def set_count(record: ResultRecord, count: object) -> None:
            record_object(multiset(record)[0])["count"] = count

        def duplicate_entry(record: ResultRecord) -> None:
            values = multiset(record)
            values.append(copy.deepcopy(values[0]))

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            (
                "boolean",
                lambda record: set_count(record, True),
                "count must be an exact integer",
            ),
            (
                "zero",
                lambda record: set_count(record, 0),
                "count must be positive",
            ),
            (
                "expansion",
                lambda record: set_count(record, 513),
                "graph node limit",
            ),
            ("duplicate", duplicate_entry, "sorted and unique"),
        )
        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_snapshot_and_comparison_mutations(self) -> None:
        def output_binding(record: ResultRecord) -> ResultRecord:
            return record_object(record_array(record["bindings"])[-1])

        def training_snapshot(record: ResultRecord) -> ResultRecord:
            return record_object(output_binding(record)["training"])

        def wrong_role(record: ResultRecord) -> None:
            snapshot = training_snapshot(record)
            record_object(snapshot["endpoint"])["role"] = "other"

        def bool_position(record: ResultRecord) -> None:
            training_snapshot(record)["position"] = True

        def wrong_dtype(record: ResultRecord) -> None:
            record_object(training_snapshot(record)["value"])["dtype"] = "int64"

        def bool_shape(record: ResultRecord) -> None:
            value_record = record_object(training_snapshot(record)["value"])
            value_record["shape"] = [True]

        def wrong_kind(record: ResultRecord) -> None:
            record_object(training_snapshot(record)["inferred"])["kind"] = "other"

        def mismatched_value_id(record: ResultRecord) -> None:
            record_object(training_snapshot(record)["value"])["value_id"] = "other"

        def wrong_mismatch(record: ResultRecord) -> None:
            output_binding(record)["mismatches"] = ["other"]

        def missing_normalization(record: ResultRecord) -> None:
            output_binding(record)["normalization"] = None

        def contradict_lineage(record: ResultRecord) -> None:
            normalization = record_object(output_binding(record)["normalization"])
            normalization["training_sha256"] = "0" * 64

        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            ("role", wrong_role, "role is not supported"),
            ("position", bool_position, "position must be an exact integer"),
            ("dtype", wrong_dtype, "dtype is not supported"),
            ("shape", bool_shape, "unsupported axis"),
            ("kind", wrong_kind, "quantity kind is not supported"),
            ("identity", mismatched_value_id, "identities are inconsistent"),
            ("mismatch", wrong_mismatch, "mismatch code is not supported"),
            (
                "normalization-missing",
                missing_normalization,
                "require an exact normalization comparison",
            ),
            (
                "normalization-cross-check",
                contradict_lineage,
                "mismatch codes are incomplete",
            ),
        )
        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))

    def test_rejects_root_plan_rebinding_and_inconsistent_outcome(self) -> None:
        cases: tuple[tuple[str, RecordMutation, str], ...] = (
            (
                "plan",
                lambda record: record.__setitem__("plan_sha256", "6" * 64),
                "lineage source bindings are inconsistent",
            ),
            (
                "outcome",
                lambda record: record.__setitem__(
                    "status",
                    ComparisonStatus.INDETERMINATE.value,
                ),
                "indeterminate comparison fields are inconsistent",
            ),
        )
        for name, mutation, message in cases:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ComparisonResultDecodeError, message),
            ):
                decode_comparison_result(encode_mutation(self.source, mutation))
