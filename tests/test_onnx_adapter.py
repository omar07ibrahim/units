from __future__ import annotations

import copy
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

import onnx
from onnx import TensorProto, helper

import unitsentinel
from unitsentinel import (
    ONNX_CONTRACT_METADATA_KEY,
    ONNX_CONTRACT_SCHEMA,
    ONNX_IMPORT_SCHEMA,
    OnnxAdapterError,
    OnnxContractError,
    OnnxDependencyError,
    OnnxModelError,
    Operation,
    ScalarType,
    VerificationStatus,
    import_onnx_model,
    onnx_adapter,
    verify_graph,
)
from unitsentinel.canonical import canonical_json_bytes, sha256_hex
from unitsentinel.graph_codec import encode_graph


def speed_contract() -> dict[str, object]:
    return {
        "graph_id": "onnx-speed-contract",
        "nodes": [
            {
                "node_id": "derive-speed",
                "onnx_name": "derive-speed",
            }
        ],
        "schema": ONNX_CONTRACT_SCHEMA,
        "values": [
            {
                "onnx_name": "distance",
                "unit_id": "meter",
                "value_id": "distance",
            },
            {
                "onnx_name": "duration",
                "unit_id": "second",
                "value_id": "duration",
            },
            {
                "onnx_name": "speed",
                "unit_id": "meter-per-second",
                "value_id": "speed",
            },
        ],
    }


def set_contract(
    model: onnx.ModelProto,
    contract: dict[str, object] | None = None,
    *,
    raw: str | None = None,
) -> None:
    model.ClearField("metadata_props")
    metadata = model.metadata_props.add()
    metadata.key = ONNX_CONTRACT_METADATA_KEY
    if raw is None:
        assert contract is not None
        raw = canonical_json_bytes(contract).decode("utf-8")
    metadata.value = raw


def serialize(model: onnx.ModelProto) -> bytes:
    return model.SerializeToString(deterministic=True)


def speed_model() -> onnx.ModelProto:
    graph = helper.make_graph(
        [
            helper.make_node(
                "Div",
                ["distance", "duration"],
                ["speed"],
                name="derive-speed",
            )
        ],
        "speed-model",
        [
            helper.make_tensor_value_info(
                "distance",
                TensorProto.FLOAT,
                [4, 8],
            ),
            helper.make_tensor_value_info(
                "duration",
                TensorProto.FLOAT,
                [4, 8],
            ),
        ],
        [
            helper.make_tensor_value_info(
                "speed",
                TensorProto.FLOAT,
                [4, 8],
            )
        ],
    )
    model = helper.make_model(
        graph,
        producer_name="unitsentinel-tests",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=8,
    )
    set_contract(model, speed_contract())
    return model


def two_node_contract() -> dict[str, object]:
    return {
        "graph_id": "onnx-two-node-contract",
        "nodes": [
            {"node_id": "copy-speed", "onnx_name": "copy-speed"},
            {"node_id": "derive-speed", "onnx_name": "derive-speed"},
        ],
        "schema": ONNX_CONTRACT_SCHEMA,
        "values": [
            {
                "onnx_name": "distance",
                "unit_id": "meter",
                "value_id": "distance",
            },
            {
                "onnx_name": "duration",
                "unit_id": "second",
                "value_id": "duration",
            },
            {
                "onnx_name": "speed",
                "unit_id": "meter-per-second",
                "value_id": "speed",
            },
            {
                "onnx_name": "speed_raw",
                "unit_id": "meter-per-second",
                "value_id": "speed-raw",
            },
        ],
    }


def two_node_model() -> onnx.ModelProto:
    graph = helper.make_graph(
        [
            helper.make_node(
                "Div",
                ["distance", "duration"],
                ["speed_raw"],
                name="derive-speed",
            ),
            helper.make_node(
                "Identity",
                ["speed_raw"],
                ["speed"],
                name="copy-speed",
            ),
        ],
        "two-node-model",
        [
            helper.make_tensor_value_info(
                "distance",
                TensorProto.FLOAT,
                [4, 8],
            ),
            helper.make_tensor_value_info(
                "duration",
                TensorProto.FLOAT,
                [4, 8],
            ),
        ],
        [
            helper.make_tensor_value_info(
                "speed",
                TensorProto.FLOAT,
                [4, 8],
            )
        ],
        value_info=[
            helper.make_tensor_value_info(
                "speed_raw",
                TensorProto.FLOAT,
                [4, 8],
            )
        ],
    )
    model = helper.make_model(
        graph,
        producer_name="unitsentinel-tests",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=8,
    )
    set_contract(model, two_node_contract())
    return model


def operator_model(op_type: str, arity: int) -> onnx.ModelProto:
    input_names = ["left"] if arity == 1 else ["left", "right"]
    graph = helper.make_graph(
        [
            helper.make_node(
                op_type,
                input_names,
                ["result"],
                name="apply-operation",
            )
        ],
        "operator-model",
        [
            helper.make_tensor_value_info(
                name,
                TensorProto.FLOAT,
                [2, 2],
            )
            for name in input_names
        ],
        [
            helper.make_tensor_value_info(
                "result",
                TensorProto.FLOAT,
                [2, 2],
            )
        ],
    )
    values = [
        {"onnx_name": name, "unit_id": None, "value_id": name} for name in input_names
    ]
    values.append({"onnx_name": "result", "unit_id": None, "value_id": "result"})
    contract = {
        "graph_id": "operator-contract",
        "nodes": [
            {
                "node_id": "apply-operation",
                "onnx_name": "apply-operation",
            }
        ],
        "schema": ONNX_CONTRACT_SCHEMA,
        "values": sorted(values, key=lambda value: value["onnx_name"]),
    }
    model = helper.make_model(
        graph,
        producer_name="unitsentinel-tests",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=8,
    )
    set_contract(model, contract)
    return model


class PositiveAdapterTests(unittest.TestCase):
    def test_public_api_lowers_and_verifies_real_onnx_bytes(self) -> None:
        payload = serialize(speed_model())
        result = import_onnx_model(payload)

        self.assertEqual(result.source_digest, sha256_hex(payload))
        self.assertEqual(result.source_size, len(payload))
        self.assertEqual(result.graph.graph_id, "onnx-speed-contract")
        self.assertEqual(result.graph.inputs, ("distance", "duration"))
        self.assertEqual(result.graph.outputs, ("speed",))
        self.assertEqual(
            tuple(value.value_id for value in result.graph.values),
            ("distance", "duration", "speed"),
        )
        self.assertEqual(
            tuple(value.dtype for value in result.graph.values),
            (ScalarType.FLOAT32,) * 3,
        )
        self.assertEqual(
            tuple(value.shape for value in result.graph.values),
            ((4, 8),) * 3,
        )
        self.assertEqual(result.graph.nodes[0].operation, Operation.DIVIDE)
        self.assertEqual(result.operator_bindings[0].onnx_op_type, "Div")
        self.assertEqual(
            result.canonical_bytes(),
            canonical_json_bytes(result.canonical_record()),
        )
        self.assertEqual(result.digest, sha256_hex(result.canonical_bytes()))
        self.assertEqual(result.canonical_record()["schema"], ONNX_IMPORT_SCHEMA)
        self.assertEqual(
            result.canonical_record()["model"]["model_executed"],
            False,
        )
        self.assertEqual(
            result.canonical_record()["model"]["external_data"],
            False,
        )
        self.assertEqual(
            sha256_hex(encode_graph(result.graph)),
            result.graph.digest,
        )

        verification = verify_graph(result.graph)
        self.assertEqual(verification.status, VerificationStatus.VERIFIED)

    def test_official_checker_runs_with_every_strict_option(self) -> None:
        with patch.object(
            onnx.checker,
            "check_model",
            wraps=onnx.checker.check_model,
        ) as checker:
            import_onnx_model(serialize(speed_model()))

        checker.assert_called_once()
        self.assertEqual(
            checker.call_args.kwargs,
            {
                "check_custom_domain": True,
                "full_check": True,
                "skip_opset_compatibility_check": False,
            },
        )

    def test_every_reviewed_operator_has_an_explicit_binding(self) -> None:
        expected = {
            "Add": (Operation.ADD, 2),
            "Div": (Operation.DIVIDE, 2),
            "Exp": (Operation.EXP, 1),
            "Identity": (Operation.IDENTITY, 1),
            "Log": (Operation.LOG, 1),
            "MatMul": (Operation.MATMUL, 2),
            "Max": (Operation.MAXIMUM, 2),
            "Min": (Operation.MINIMUM, 2),
            "Mul": (Operation.MULTIPLY, 2),
            "Sigmoid": (Operation.SIGMOID, 1),
            "Softmax": (Operation.SOFTMAX, 1),
            "Sub": (Operation.SUBTRACT, 2),
        }
        for op_type, (operation, arity) in expected.items():
            with self.subTest(op_type=op_type):
                result = import_onnx_model(serialize(operator_model(op_type, arity)))
                self.assertEqual(result.graph.nodes[0].operation, operation)
                self.assertEqual(
                    result.operator_bindings[0].onnx_op_type,
                    op_type,
                )

    def test_public_symbols_are_exported_without_importing_onnx_eagerly(self) -> None:
        expected = (
            "MAX_ONNX_MODEL_BYTES",
            "ONNX_CONTRACT_METADATA_KEY",
            "ONNX_CONTRACT_SCHEMA",
            "ONNX_IMPORT_SCHEMA",
            "OnnxAdapterError",
            "OnnxContractError",
            "OnnxDependencyError",
            "OnnxImportResult",
            "OnnxModelError",
            "OnnxOperatorBinding",
            "import_onnx_model",
        )
        for name in expected:
            with self.subTest(name=name):
                self.assertIn(name, unitsentinel.__all__)
                self.assertTrue(hasattr(unitsentinel, name))


class PayloadAndRuntimeBoundaryTests(unittest.TestCase):
    def test_payload_requires_nonempty_bounded_exact_bytes(self) -> None:
        cases = (
            ("text", "exact bytes"),
            (b"", "empty"),
            (
                b"x" * (onnx_adapter.MAX_ONNX_MODEL_BYTES + 1),
                "byte limit",
            ),
        )
        for payload, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(OnnxModelError, message),
            ):
                import_onnx_model(payload)  # type: ignore[arg-type]

    def test_missing_or_wrong_optional_runtime_fails_stably(self) -> None:
        with (
            patch.object(
                onnx_adapter,
                "import_module",
                side_effect=ModuleNotFoundError,
            ),
            self.assertRaisesRegex(
                OnnxDependencyError,
                r"install unitsentinel\[onnx\]",
            ),
        ):
            import_onnx_model(b"x")

        with (
            patch.object(
                onnx_adapter,
                "import_module",
                return_value=SimpleNamespace(__version__="0.0.0"),
            ),
            self.assertRaisesRegex(OnnxDependencyError, "version"),
        ):
            import_onnx_model(b"x")

    def test_incomplete_loader_and_decoder_fail_without_details(self) -> None:
        missing_loader = SimpleNamespace(
            __version__=onnx_adapter.ONNX_RUNTIME_VERSION,
            load_model_from_string=3,
        )
        with (
            patch.object(
                onnx_adapter,
                "import_module",
                return_value=missing_loader,
            ),
            self.assertRaisesRegex(OnnxDependencyError, "incomplete"),
        ):
            import_onnx_model(b"x")

        broken_loader = SimpleNamespace(
            __version__=onnx_adapter.ONNX_RUNTIME_VERSION,
            load_model_from_string=lambda payload: 1 / 0,
        )
        with (
            patch.object(
                onnx_adapter,
                "import_module",
                return_value=broken_loader,
            ),
            self.assertRaisesRegex(OnnxModelError, "could not be decoded"),
        ):
            import_onnx_model(b"x")

        empty_loader = SimpleNamespace(
            __version__=onnx_adapter.ONNX_RUNTIME_VERSION,
            load_model_from_string=lambda payload: None,
        )
        with (
            patch.object(
                onnx_adapter,
                "import_module",
                return_value=empty_loader,
            ),
            self.assertRaisesRegex(OnnxModelError, "could not be decoded"),
        ):
            import_onnx_model(b"x")

    def test_checker_failure_is_redacted_and_incomplete_checker_is_rejected(
        self,
    ) -> None:
        with (
            patch.object(
                onnx.checker,
                "check_model",
                side_effect=RuntimeError("/private/model.onnx"),
            ),
            self.assertRaisesRegex(OnnxModelError, "official ONNX checker"),
        ):
            import_onnx_model(serialize(speed_model()))

        incomplete = SimpleNamespace(check_model=3)
        with (
            patch.object(onnx_adapter, "_get") as get,
            self.assertRaisesRegex(OnnxDependencyError, "checker is incomplete"),
        ):
            get.side_effect = lambda value, name: (
                incomplete if name == "checker" else getattr(value, name)
            )
            onnx_adapter._run_official_checker(onnx, speed_model())


class ModelPreflightTests(unittest.TestCase):
    def assert_rejected(
        self,
        model: onnx.ModelProto,
        message: str,
    ) -> None:
        with self.assertRaisesRegex(OnnxAdapterError, message):
            import_onnx_model(serialize(model))

    def test_model_version_and_feature_envelope_is_closed(self) -> None:
        cases: list[tuple[str, Callable[[onnx.ModelProto], None], str]] = [
            (
                "ir",
                lambda model: setattr(model, "ir_version", 9),
                "IR version",
            ),
            (
                "missing-opset",
                lambda model: model.ClearField("opset_import"),
                "exactly one opset",
            ),
            (
                "wrong-opset",
                lambda model: setattr(
                    model.opset_import[0],
                    "version",
                    14,
                ),
                "opset is not supported",
            ),
            (
                "training",
                lambda model: model.training_info.add(),
                "training graphs",
            ),
            (
                "function",
                lambda model: model.functions.add(),
                "local functions",
            ),
            (
                "initializer",
                lambda model: model.graph.initializer.append(
                    helper.make_tensor(
                        "weights",
                        TensorProto.FLOAT,
                        [1],
                        [1.0],
                    )
                ),
                "initializers",
            ),
            (
                "sparse-initializer",
                lambda model: model.graph.sparse_initializer.add(),
                "initializers",
            ),
            (
                "quantization",
                lambda model: model.graph.quantization_annotation.add(),
                "quantization",
            ),
            (
                "no-input",
                lambda model: model.graph.ClearField("input"),
                "input count",
            ),
            (
                "no-output",
                lambda model: model.graph.ClearField("output"),
                "output count",
            ),
        ]
        for label, mutate, message in cases:
            model = speed_model()
            mutate(model)
            with self.subTest(label=label):
                self.assert_rejected(model, message)

    def test_node_subset_rejects_attributes_domains_unknown_ops_and_arity(
        self,
    ) -> None:
        cases: list[tuple[str, Callable[[onnx.ModelProto], None], str]] = [
            (
                "attribute",
                lambda model: model.graph.node[0].attribute.append(
                    helper.make_attribute("axis", 1)
                ),
                "attributes",
            ),
            (
                "domain",
                lambda model: setattr(
                    model.graph.node[0],
                    "domain",
                    "example.custom",
                ),
                "custom operator domains",
            ),
            (
                "pow",
                lambda model: setattr(
                    model.graph.node[0],
                    "op_type",
                    "Pow",
                ),
                "reviewed subset",
            ),
            (
                "input-arity",
                lambda model: model.graph.node[0].input.pop(),
                "arity",
            ),
            (
                "output-arity",
                lambda model: model.graph.node[0].output.append("other"),
                "arity",
            ),
        ]
        for label, mutate, message in cases:
            model = speed_model()
            mutate(model)
            with self.subTest(label=label):
                self.assert_rejected(model, message)

    def test_metadata_envelope_requires_one_bounded_exact_key(self) -> None:
        model = speed_model()
        model.ClearField("metadata_props")
        self.assert_rejected(model, "exactly one metadata contract")

        model = speed_model()
        extra = model.metadata_props.add()
        extra.key = "example.extra"
        extra.value = "x"
        self.assert_rejected(model, "exactly one metadata contract")

        model = speed_model()
        model.metadata_props[0].key = "example.wrong"
        self.assert_rejected(model, "key is not supported")

        model = speed_model()
        model.metadata_props[0].value = "x" * (onnx_adapter.MAX_ONNX_CONTRACT_BYTES + 1)
        self.assert_rejected(model, "byte limit")


class MetadataContractTests(unittest.TestCase):
    def assert_contract_rejected(
        self,
        contract: dict[str, object],
        message: str,
    ) -> None:
        model = speed_model()
        set_contract(model, contract)
        with self.assertRaisesRegex(OnnxAdapterError, message):
            import_onnx_model(serialize(model))

    def test_json_must_be_canonical_bounded_and_exact(self) -> None:
        model = speed_model()
        set_contract(model, raw='{"schema": "wrong"}')
        with self.assertRaisesRegex(OnnxContractError, "not canonical JSON"):
            import_onnx_model(serialize(model))

        model = speed_model()
        set_contract(model, raw="{}")
        with self.assertRaisesRegex(OnnxContractError, "missing or unknown"):
            import_onnx_model(serialize(model))

        contract = speed_contract()
        contract["unknown"] = True
        self.assert_contract_rejected(contract, "missing or unknown")

    def test_schema_graph_and_binding_fields_are_exact(self) -> None:
        contract = speed_contract()
        contract["schema"] = "unitsentinel.onnx-contract/v2"
        self.assert_contract_rejected(contract, "schema is not supported")

        contract = speed_contract()
        contract["graph_id"] = "Not Canonical"
        self.assert_contract_rejected(contract, "not canonical")

        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values[0]["extra"] = True
        contract["values"] = values
        self.assert_contract_rejected(contract, "missing or unknown")

        contract = speed_contract()
        nodes = copy.deepcopy(contract["nodes"])
        assert isinstance(nodes, list)
        nodes[0]["extra"] = True
        contract["nodes"] = nodes
        self.assert_contract_rejected(contract, "missing or unknown")

    def test_value_bindings_are_sorted_unique_complete_and_typed(self) -> None:
        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values.reverse()
        contract["values"] = values
        self.assert_contract_rejected(contract, "sorted and unique")

        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values[1]["onnx_name"] = values[0]["onnx_name"]
        contract["values"] = values
        self.assert_contract_rejected(contract, "sorted and unique")

        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values[1]["value_id"] = values[0]["value_id"]
        contract["values"] = values
        self.assert_contract_rejected(contract, "identifiers must be unique")

        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values.pop()
        contract["values"] = values
        self.assert_contract_rejected(contract, "bind every graph value")

        contract = speed_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        values[0]["unit_id"] = 7
        contract["values"] = values
        self.assert_contract_rejected(contract, "text or null")

    def test_units_are_explicit_known_and_canonical(self) -> None:
        for unit_id, message in (
            ("unknown-unit", "not in the registry"),
            ("metre", "must be canonical"),
        ):
            contract = speed_contract()
            values = copy.deepcopy(contract["values"])
            assert isinstance(values, list)
            values[0]["unit_id"] = unit_id
            contract["values"] = values
            with self.subTest(unit_id=unit_id):
                self.assert_contract_rejected(contract, message)

    def test_node_bindings_are_exact_and_do_not_collide(self) -> None:
        contract = speed_contract()
        contract["nodes"] = []
        self.assert_contract_rejected(contract, "bind every graph node")

        contract = speed_contract()
        nodes = copy.deepcopy(contract["nodes"])
        assert isinstance(nodes, list)
        nodes[0]["node_id"] = "distance"
        contract["nodes"] = nodes
        self.assert_contract_rejected(contract, "must not collide")


class StaticGraphBoundaryTests(unittest.TestCase):
    def import_without_checker(self, model: onnx.ModelProto) -> None:
        with patch.object(onnx.checker, "check_model", return_value=None):
            import_onnx_model(serialize(model))

    def assert_source_rejected(
        self,
        model: onnx.ModelProto,
        message: str,
    ) -> None:
        with (
            patch.object(onnx.checker, "check_model", return_value=None),
            self.assertRaisesRegex(OnnxAdapterError, message),
        ):
            import_onnx_model(serialize(model))

    def test_names_topology_and_outputs_are_explicit(self) -> None:
        model = speed_model()
        model.graph.input[1].name = "distance"
        self.assert_source_rejected(model, "input names must be unique")

        model = speed_model()
        model.graph.node[0].name = ""
        self.assert_source_rejected(model, "reviewed ASCII subset")

        model = two_node_model()
        model.graph.node[1].name = "derive-speed"
        self.assert_source_rejected(model, "node names must be unique")

        model = speed_model()
        model.graph.node[0].input[0] = "missing"
        self.assert_source_rejected(model, "earlier available")

        model = speed_model()
        model.graph.node[0].output[0] = "distance"
        self.assert_source_rejected(model, "exactly one producer")

        model = speed_model()
        model.graph.output.append(copy.deepcopy(model.graph.output[0]))
        self.assert_source_rejected(model, "output names must be unique")

        model = speed_model()
        model.graph.output[0].name = "missing"
        self.assert_source_rejected(model, "not a declared value")

    def test_every_value_requires_one_consistent_static_tensor_type(self) -> None:
        model = two_node_model()
        model.graph.ClearField("value_info")
        self.assert_source_rejected(model, "static tensor type for every value")

        model = speed_model()
        model.graph.value_info.append(
            helper.make_tensor_value_info(
                "extra",
                TensorProto.FLOAT,
                [4, 8],
            )
        )
        self.assert_source_rejected(model, "static tensor type for every value")

        model = speed_model()
        model.graph.value_info.append(
            helper.make_tensor_value_info(
                "distance",
                TensorProto.FLOAT,
                [9, 9],
            )
        )
        self.assert_source_rejected(model, "declarations conflict")

        model = speed_model()
        model.graph.input[0].type.Clear()
        self.assert_source_rejected(model, "not a tensor")

        model = speed_model()
        for info in (*model.graph.input, *model.graph.output):
            info.type.tensor_type.elem_type = TensorProto.INT64
        self.assert_source_rejected(model, "dtype")

        model = speed_model()
        model.graph.input[0].type.tensor_type.ClearField("shape")
        self.assert_source_rejected(model, "rank must be explicit")

        model = speed_model()
        dimension = model.graph.input[0].type.tensor_type.shape.dim[0]
        dimension.ClearField("dim_value")
        dimension.dim_param = "batch"
        self.assert_source_rejected(model, "dimensions must be static")

        model = speed_model()
        model.graph.input[0].type.tensor_type.shape.dim[0].dim_value = 0
        self.assert_source_rejected(model, "out of bounds")

        model = speed_model()
        shape = model.graph.input[0].type.tensor_type.shape
        while len(shape.dim) < 9:
            shape.dim.add(dim_value=1)
        self.assert_source_rejected(model, "rank exceeds")

    def test_dead_subgraphs_fail_when_lowered_to_the_core(self) -> None:
        model = two_node_model()
        model.graph.output[0].name = "speed_raw"
        model.graph.value_info[0].name = "speed"
        contract = two_node_contract()
        values = copy.deepcopy(contract["values"])
        assert isinstance(values, list)
        contract["values"] = values
        set_contract(model, contract)
        self.assert_source_rejected(model, "could not be lowered")


class ResultAndDefensiveBoundaryTests(unittest.TestCase):
    def fresh_result(self) -> onnx_adapter.OnnxImportResult:
        return import_onnx_model(serialize(speed_model()))

    def test_result_detects_mutation_and_malformed_fields(self) -> None:
        cases = (
            ("source_digest", "bad", "source digest"),
            ("source_size", 0, "source size"),
            ("contract_digest", "bad", "contract digest"),
            ("operator_bindings", [], "must be a tuple"),
            ("operator_bindings", (), "bindings are incomplete"),
            ("_digest", "0" * 64, "does not match"),
        )
        for attribute, value, message in cases:
            result = self.fresh_result()
            object.__setattr__(result, attribute, value)
            with (
                self.subTest(attribute=attribute, message=message),
                self.assertRaisesRegex(OnnxAdapterError, message),
            ):
                result.validate()

    def test_operator_bindings_validate_the_exact_mapping(self) -> None:
        result = self.fresh_result()
        binding = result.operator_bindings[0]
        binding.validate()
        self.assertEqual(
            binding.canonical_record()["unitsentinel_operation"],
            "divide",
        )

        cases = (
            (
                onnx_adapter.OnnxOperatorBinding(
                    "",
                    "Div",
                    "derive-speed",
                    Operation.DIVIDE,
                ),
                "ASCII subset",
            ),
            (
                onnx_adapter.OnnxOperatorBinding(
                    "derive-speed",
                    "Pow",
                    "derive-speed",
                    Operation.POWER,
                ),
                "unsupported ONNX op",
            ),
            (
                onnx_adapter.OnnxOperatorBinding(
                    "derive-speed",
                    "Div",
                    "Bad ID",
                    Operation.DIVIDE,
                ),
                "not canonical",
            ),
            (
                onnx_adapter.OnnxOperatorBinding(
                    "derive-speed",
                    "Div",
                    "derive-speed",
                    Operation.ADD,
                ),
                "does not match",
            ),
        )
        for invalid, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(OnnxAdapterError, message),
            ):
                invalid.validate()

    def test_low_level_structure_guards_fail_closed(self) -> None:
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._get(SimpleNamespace(), "missing")
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._items(SimpleNamespace(value="text"), "value")
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._items(SimpleNamespace(value=1), "value")
        with self.assertRaisesRegex(OnnxModelError, "integer field"):
            onnx_adapter._integer(SimpleNamespace(value="1"), "value")
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._which_oneof(
                SimpleNamespace(WhichOneof=1),
                "value",
            )
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._which_oneof(
                SimpleNamespace(WhichOneof=lambda group: 1),
                "value",
            )
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._has_field(
                SimpleNamespace(HasField=1),
                "shape",
            )
        with self.assertRaisesRegex(OnnxModelError, "structure is malformed"):
            onnx_adapter._has_field(
                SimpleNamespace(HasField=lambda name: "yes"),
                "shape",
            )

    def test_contract_helpers_reject_wrong_json_shapes(self) -> None:
        with self.assertRaisesRegex(OnnxContractError, "must be an object"):
            onnx_adapter._record([], frozenset(), label="record")
        with self.assertRaisesRegex(OnnxContractError, "must be an array"):
            onnx_adapter._array({}, label="array")
        with self.assertRaisesRegex(OnnxContractError, "must be text"):
            onnx_adapter._string(1, label="string")


if __name__ == "__main__":
    unittest.main()
