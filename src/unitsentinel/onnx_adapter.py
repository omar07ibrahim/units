"""Fail-closed ONNX metadata adapter for the canonical graph core.

The adapter parses model bytes, runs the official ONNX checker, and lowers only a
small reviewed subset. It never executes a model, resolves external tensor data,
or infers unit annotations from source names.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import import_module
from types import ModuleType
from typing import Final, cast

from .canonical import canonical_json_bytes, sha256_hex
from .domain import MAX_UNIT_ID_LENGTH, UNIT_ID, UnitSentinelError
from .graph import (
    GRAPH_SCHEMA,
    MAX_AXIS_SIZE,
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    MAX_TENSOR_RANK,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from .json_boundary import (
    CanonicalJSONError,
    CanonicalJSONLimits,
    decode_canonical_json,
)
from .registry import BUILTIN_REGISTRY, SHA256_HEX
from .version import VERSION

ONNX_CONTRACT_METADATA_KEY: Final = (
    "io.github.omar07ibrahim.unitsentinel.contract"
)
ONNX_CONTRACT_SCHEMA: Final = "unitsentinel.onnx-contract/v1"
ONNX_IMPORT_SCHEMA: Final = "unitsentinel.onnx-import/v1"
ONNX_RUNTIME_VERSION: Final = "1.22.0"
ONNX_IR_VERSION: Final = 8
ONNX_OPSET_VERSION: Final = 13
MAX_ONNX_MODEL_BYTES: Final = 8_388_608
MAX_ONNX_CONTRACT_BYTES: Final = 131_072
MAX_ONNX_SOURCE_NAME_LENGTH: Final = 128

_ONNX_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/-]{0,127}$")
_CONTRACT_LIMITS: Final = CanonicalJSONLimits(
    max_bytes=MAX_ONNX_CONTRACT_BYTES,
    max_depth=6,
    max_container_items=1_024,
    max_total_values=8_192,
    max_string_length=MAX_ONNX_SOURCE_NAME_LENGTH,
    max_integer_digits=10,
)
_OPERATOR_MAP: Final = {
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
_SCALAR_TYPES: Final = {
    1: ScalarType.FLOAT32,
    10: ScalarType.FLOAT16,
    11: ScalarType.FLOAT64,
    16: ScalarType.BFLOAT16,
}


class OnnxAdapterError(UnitSentinelError):
    """Base class for stable ONNX adapter failures."""


class OnnxDependencyError(OnnxAdapterError):
    """Raised when the pinned optional ONNX runtime is unavailable."""


class OnnxModelError(OnnxAdapterError):
    """Raised when ONNX bytes are invalid or outside the reviewed subset."""


class OnnxContractError(OnnxAdapterError):
    """Raised when the versioned metadata contract is invalid."""


@dataclass(frozen=True, slots=True)
class OnnxOperatorBinding:
    """One explicit ONNX-node to canonical-operation binding."""

    onnx_name: str
    onnx_op_type: str
    node_id: str
    operation: Operation

    def validate(self) -> None:
        if type(self) is not OnnxOperatorBinding:
            raise OnnxAdapterError(
                "operator binding must be an exact OnnxOperatorBinding"
            )
        _require_source_name(self.onnx_name, label="ONNX node name")
        if self.onnx_op_type not in _OPERATOR_MAP:
            raise OnnxAdapterError("operator binding names an unsupported ONNX op")
        _require_core_identifier(self.node_id, label="canonical node identifier")
        if type(self.operation) is not Operation:
            raise OnnxAdapterError("operator binding operation is invalid")
        expected, _ = _OPERATOR_MAP[self.onnx_op_type]
        if self.operation is not expected:
            raise OnnxAdapterError(
                "operator binding operation does not match its ONNX op"
            )

    def canonical_record(self) -> dict[str, str]:
        self.validate()
        return {
            "node_id": self.node_id,
            "onnx_name": self.onnx_name,
            "onnx_op_type": self.onnx_op_type,
            "unitsentinel_operation": self.operation.value,
        }


@dataclass(frozen=True, slots=True)
class OnnxImportResult:
    """Immutable source-to-core lowering receipt."""

    graph: ComputationGraph
    source_digest: str
    source_size: int
    contract_digest: str
    operator_bindings: tuple[OnnxOperatorBinding, ...]
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def validate(self) -> None:
        if type(self) is not OnnxImportResult:
            raise OnnxAdapterError(
                "import result must be an exact OnnxImportResult"
            )
        if type(self.graph) is not ComputationGraph:
            raise OnnxAdapterError("import result graph is invalid")
        self.graph.validate()
        self.graph.validate_units(BUILTIN_REGISTRY)
        if (
            type(self.source_digest) is not str
            or SHA256_HEX.fullmatch(self.source_digest) is None
        ):
            raise OnnxAdapterError("import result source digest is invalid")
        if (
            type(self.source_size) is not int
            or self.source_size < 1
            or self.source_size > MAX_ONNX_MODEL_BYTES
        ):
            raise OnnxAdapterError("import result source size is invalid")
        if (
            type(self.contract_digest) is not str
            or SHA256_HEX.fullmatch(self.contract_digest) is None
        ):
            raise OnnxAdapterError("import result contract digest is invalid")
        if type(self.operator_bindings) is not tuple:
            raise OnnxAdapterError("import result operator bindings must be a tuple")
        if len(self.operator_bindings) != len(self.graph.nodes):
            raise OnnxAdapterError("import result operator bindings are incomplete")
        names: set[str] = set()
        for binding, node in zip(
            self.operator_bindings,
            self.graph.nodes,
            strict=True,
        ):
            if type(binding) is not OnnxOperatorBinding:
                raise OnnxAdapterError(
                    "import result contains an invalid operator binding"
                )
            binding.validate()
            if binding.onnx_name in names:
                raise OnnxAdapterError(
                    "import result ONNX node names must be unique"
                )
            names.add(binding.onnx_name)
            if (
                binding.node_id != node.node_id
                or binding.operation is not node.operation
            ):
                raise OnnxAdapterError(
                    "import result operator binding does not match the graph"
                )
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise OnnxAdapterError("import result digest is invalid")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise OnnxAdapterError("import result digest does not match its contents")

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "checker": {
                "custom_domain_check": True,
                "full_check": True,
                "name": "onnx.checker.check_model",
                "runtime_version": ONNX_RUNTIME_VERSION,
            },
            "contract": {
                "metadata_key": ONNX_CONTRACT_METADATA_KEY,
                "schema": ONNX_CONTRACT_SCHEMA,
                "sha256": self.contract_digest,
            },
            "graph": {
                "graph_id": self.graph.graph_id,
                "inputs": len(self.graph.inputs),
                "nodes": len(self.graph.nodes),
                "outputs": len(self.graph.outputs),
                "schema": GRAPH_SCHEMA,
                "sha256": self.graph.digest,
                "values": len(self.graph.values),
            },
            "model": {
                "bytes": self.source_size,
                "dynamic_shapes": False,
                "external_data": False,
                "ir_version": ONNX_IR_VERSION,
                "model_executed": False,
                "opset": {"domain": "", "version": ONNX_OPSET_VERSION},
                "sha256": self.source_digest,
            },
            "operators": [
                binding.canonical_record() for binding in self.operator_bindings
            ],
            "schema": ONNX_IMPORT_SCHEMA,
            "tool": {"name": "unitsentinel", "version": VERSION},
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self._canonical_record_unchecked())


@dataclass(frozen=True, slots=True)
class _ValueBinding:
    onnx_name: str
    value_id: str
    unit_id: str | None


@dataclass(frozen=True, slots=True)
class _NodeBinding:
    onnx_name: str
    node_id: str


@dataclass(frozen=True, slots=True)
class _Contract:
    graph_id: str
    values: tuple[_ValueBinding, ...]
    nodes: tuple[_NodeBinding, ...]


@dataclass(frozen=True, slots=True)
class _TensorSpec:
    dtype: ScalarType
    shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _SourceNode:
    onnx_name: str
    op_type: str
    operation: Operation
    inputs: tuple[str, ...]
    output: str


def import_onnx_model(payload: bytes) -> OnnxImportResult:
    """Check and lower exact ONNX bytes without executing the represented model."""

    if type(payload) is not bytes:
        raise OnnxModelError("ONNX model payload must be exact bytes")
    if not payload:
        raise OnnxModelError("ONNX model payload is empty")
    if len(payload) > MAX_ONNX_MODEL_BYTES:
        raise OnnxModelError("ONNX model payload exceeds the byte limit")

    onnx = _load_onnx()
    model = _decode_model(onnx, payload)
    graph_proto, contract_payload = _preflight_model(model)
    _run_official_checker(onnx, model)
    contract = _decode_contract(contract_payload)
    input_names, output_names, source_nodes, tensor_specs = _decode_source_graph(
        graph_proto
    )
    graph, operator_bindings = _lower_graph(
        contract,
        input_names=input_names,
        output_names=output_names,
        source_nodes=source_nodes,
        tensor_specs=tensor_specs,
    )
    return OnnxImportResult(
        graph=graph,
        source_digest=sha256_hex(payload),
        source_size=len(payload),
        contract_digest=sha256_hex(contract_payload),
        operator_bindings=operator_bindings,
    )


def _load_onnx() -> ModuleType:
    try:
        module = import_module("onnx")
    except ModuleNotFoundError:
        raise OnnxDependencyError(
            "ONNX support is unavailable; install unitsentinel[onnx]"
        ) from None
    version = _get(module, "__version__")
    if version != ONNX_RUNTIME_VERSION:
        raise OnnxDependencyError("installed ONNX runtime version is not supported")
    return module


def _decode_model(onnx: ModuleType, payload: bytes) -> object:
    loader = _get(onnx, "load_model_from_string")
    if not callable(loader):
        raise OnnxDependencyError("installed ONNX runtime is incomplete")
    try:
        model = cast(Callable[[bytes], object], loader)(payload)
    except Exception:
        raise OnnxModelError("ONNX model bytes could not be decoded") from None
    if model is None:
        raise OnnxModelError("ONNX model bytes could not be decoded")
    return model


def _preflight_model(model: object) -> tuple[object, bytes]:
    if _integer(model, "ir_version") != ONNX_IR_VERSION:
        raise OnnxModelError("ONNX IR version is not supported")

    opsets = _items(model, "opset_import")
    if len(opsets) != 1:
        raise OnnxModelError("ONNX model must declare exactly one opset")
    if (
        _text(opsets[0], "domain") != ""
        or _integer(opsets[0], "version") != ONNX_OPSET_VERSION
    ):
        raise OnnxModelError("ONNX default-domain opset is not supported")
    if _items(model, "training_info"):
        raise OnnxModelError("ONNX training graphs are not supported")
    if _items(model, "functions"):
        raise OnnxModelError("ONNX local functions are not supported")

    graph = _get(model, "graph")
    if _items(graph, "initializer") or _items(graph, "sparse_initializer"):
        raise OnnxModelError(
            "ONNX initializers and external tensor data are not supported"
        )
    if _items(graph, "quantization_annotation"):
        raise OnnxModelError("ONNX quantization annotations are not supported")

    inputs = _items(graph, "input")
    nodes = _items(graph, "node")
    outputs = _items(graph, "output")
    if not inputs or len(inputs) > MAX_GRAPH_INPUTS:
        raise OnnxModelError("ONNX graph input count is out of bounds")
    if len(nodes) > MAX_GRAPH_NODES:
        raise OnnxModelError("ONNX graph node count is out of bounds")
    if not outputs or len(outputs) > MAX_GRAPH_OUTPUTS:
        raise OnnxModelError("ONNX graph output count is out of bounds")
    for node in nodes:
        if _items(node, "attribute"):
            raise OnnxModelError("ONNX node attributes are not supported")
        if _text(node, "domain") != "":
            raise OnnxModelError("ONNX custom operator domains are not supported")
        op_type = _text(node, "op_type")
        if op_type not in _OPERATOR_MAP:
            raise OnnxModelError("ONNX operator is not in the reviewed subset")
        _, arity = _OPERATOR_MAP[op_type]
        if len(_items(node, "input")) != arity or len(_items(node, "output")) != 1:
            raise OnnxModelError("ONNX node arity is not in the reviewed subset")

    return graph, _contract_payload(model)


def _contract_payload(model: object) -> bytes:
    properties = _items(model, "metadata_props")
    if len(properties) != 1:
        raise OnnxContractError(
            "ONNX model must contain exactly one metadata contract"
        )
    property_value = properties[0]
    if _text(property_value, "key") != ONNX_CONTRACT_METADATA_KEY:
        raise OnnxContractError("ONNX metadata contract key is not supported")
    value = _text(property_value, "value")
    try:
        payload = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise OnnxContractError(
            "ONNX metadata contract is not valid UTF-8"
        ) from None
    if len(payload) > MAX_ONNX_CONTRACT_BYTES:
        raise OnnxContractError("ONNX metadata contract exceeds the byte limit")
    return payload


def _run_official_checker(onnx: ModuleType, model: object) -> None:
    checker = _get(onnx, "checker")
    check_model = _get(checker, "check_model")
    if not callable(check_model):
        raise OnnxDependencyError("installed ONNX checker is incomplete")
    try:
        cast(Callable[..., object], check_model)(
            model,
            full_check=True,
            skip_opset_compatibility_check=False,
            check_custom_domain=True,
        )
    except Exception:
        raise OnnxModelError("official ONNX checker rejected the model") from None


def _decode_contract(payload: bytes) -> _Contract:
    try:
        parsed = decode_canonical_json(
            payload,
            limits=_CONTRACT_LIMITS,
            label="onnx-contract",
        )
    except CanonicalJSONError as error:
        raise OnnxContractError(str(error)) from None

    root = _record(
        parsed,
        frozenset({"graph_id", "nodes", "schema", "values"}),
        label="ONNX contract",
    )
    if _string(root["schema"], label="ONNX contract schema") != ONNX_CONTRACT_SCHEMA:
        raise OnnxContractError("ONNX contract schema is not supported")
    graph_id = _require_core_identifier(
        _string(root["graph_id"], label="ONNX contract graph identifier"),
        label="ONNX contract graph identifier",
    )

    value_items = _array(root["values"], label="ONNX contract values")
    values: list[_ValueBinding] = []
    for item in value_items:
        value = _record(
            item,
            frozenset({"onnx_name", "unit_id", "value_id"}),
            label="ONNX contract value binding",
        )
        unit_value = value["unit_id"]
        if unit_value is not None and type(unit_value) is not str:
            raise OnnxContractError(
                "ONNX contract unit identifier must be text or null"
            )
        values.append(
            _ValueBinding(
                onnx_name=_require_source_name(
                    _string(value["onnx_name"], label="ONNX value name"),
                    label="ONNX value name",
                ),
                value_id=_require_core_identifier(
                    _string(value["value_id"], label="canonical value identifier"),
                    label="canonical value identifier",
                ),
                unit_id=cast(str | None, unit_value),
            )
        )

    node_items = _array(root["nodes"], label="ONNX contract nodes")
    nodes: list[_NodeBinding] = []
    for item in node_items:
        node = _record(
            item,
            frozenset({"node_id", "onnx_name"}),
            label="ONNX contract node binding",
        )
        nodes.append(
            _NodeBinding(
                onnx_name=_require_source_name(
                    _string(node["onnx_name"], label="ONNX node name"),
                    label="ONNX node name",
                ),
                node_id=_require_core_identifier(
                    _string(node["node_id"], label="canonical node identifier"),
                    label="canonical node identifier",
                ),
            )
        )

    value_names = [binding.onnx_name for binding in values]
    node_names = [binding.onnx_name for binding in nodes]
    if value_names != sorted(value_names) or len(value_names) != len(set(value_names)):
        raise OnnxContractError(
            "ONNX contract value bindings must be sorted and unique"
        )
    if node_names != sorted(node_names) or len(node_names) != len(set(node_names)):
        raise OnnxContractError(
            "ONNX contract node bindings must be sorted and unique"
        )
    value_ids = [binding.value_id for binding in values]
    node_ids = [binding.node_id for binding in nodes]
    if len(value_ids) != len(set(value_ids)):
        raise OnnxContractError("canonical value identifiers must be unique")
    if len(node_ids) != len(set(node_ids)):
        raise OnnxContractError("canonical node identifiers must be unique")
    if set(value_ids).intersection(node_ids):
        raise OnnxContractError(
            "canonical node identifiers must not collide with values"
        )
    for binding in values:
        if binding.unit_id is None:
            continue
        _require_core_identifier(
            binding.unit_id,
            label="ONNX contract unit identifier",
        )
        try:
            unit = BUILTIN_REGISTRY.resolve(binding.unit_id)
        except UnitSentinelError:
            raise OnnxContractError(
                "ONNX contract unit identifier is not in the registry"
            ) from None
        if unit.unit_id != binding.unit_id:
            raise OnnxContractError(
                "ONNX contract unit identifiers must be canonical"
            )

    return _Contract(graph_id=graph_id, values=tuple(values), nodes=tuple(nodes))


def _decode_source_graph(
    graph: object,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[_SourceNode, ...],
    dict[str, _TensorSpec],
]:
    input_infos = _items(graph, "input")
    output_infos = _items(graph, "output")
    input_names = tuple(
        _require_source_name(
            _text(info, "name"),
            label="ONNX graph input name",
        )
        for info in input_infos
    )
    if len(input_names) != len(set(input_names)):
        raise OnnxModelError("ONNX graph input names must be unique")

    available = set(input_names)
    node_names: set[str] = set()
    source_nodes: list[_SourceNode] = []
    for node in _items(graph, "node"):
        name = _require_source_name(_text(node, "name"), label="ONNX node name")
        if name in node_names:
            raise OnnxModelError("ONNX node names must be unique")
        node_names.add(name)
        op_type = _text(node, "op_type")
        operation, arity = _OPERATOR_MAP[op_type]
        inputs = tuple(
            _require_source_name(
                _string(item, label="ONNX node input name"),
                label="ONNX node input name",
            )
            for item in _items(node, "input")
        )
        outputs = tuple(
            _require_source_name(
                _string(item, label="ONNX node output name"),
                label="ONNX node output name",
            )
            for item in _items(node, "output")
        )
        if len(inputs) != arity or len(outputs) != 1:
            raise OnnxModelError("ONNX node arity is not in the reviewed subset")
        if not set(inputs).issubset(available):
            raise OnnxModelError(
                "ONNX node inputs must reference earlier available values"
            )
        output = outputs[0]
        if output in available:
            raise OnnxModelError("ONNX values must have exactly one producer")
        available.add(output)
        source_nodes.append(
            _SourceNode(
                onnx_name=name,
                op_type=op_type,
                operation=operation,
                inputs=inputs,
                output=output,
            )
        )

    output_names = tuple(
        _require_source_name(
            _text(info, "name"),
            label="ONNX graph output name",
        )
        for info in output_infos
    )
    if len(output_names) != len(set(output_names)):
        raise OnnxModelError("ONNX graph output names must be unique")
    if not set(output_names).issubset(available):
        raise OnnxModelError("ONNX graph output is not a declared value")

    declarations: dict[str, _TensorSpec] = {}
    all_infos = (
        *input_infos,
        *_items(graph, "value_info"),
        *output_infos,
    )
    for info in all_infos:
        name = _require_source_name(
            _text(info, "name"),
            label="ONNX value declaration name",
        )
        spec = _decode_tensor_spec(info)
        previous = declarations.get(name)
        if previous is not None and previous != spec:
            raise OnnxModelError("ONNX value type declarations conflict")
        declarations[name] = spec
    if set(declarations) != available:
        raise OnnxModelError(
            "ONNX graph must declare one static tensor type for every value"
        )
    return input_names, output_names, tuple(source_nodes), declarations


def _decode_tensor_spec(value_info: object) -> _TensorSpec:
    type_proto = _get(value_info, "type")
    if _which_oneof(type_proto, "value") != "tensor_type":
        raise OnnxModelError("ONNX value type is not a tensor")
    tensor_type = _get(type_proto, "tensor_type")
    element_type = _integer(tensor_type, "elem_type")
    dtype = _SCALAR_TYPES.get(element_type)
    if dtype is None:
        raise OnnxModelError("ONNX tensor dtype is not in the reviewed subset")
    if not _has_field(tensor_type, "shape"):
        raise OnnxModelError("ONNX tensor rank must be explicit")
    shape_proto = _get(tensor_type, "shape")
    dimensions = _items(shape_proto, "dim")
    if len(dimensions) > MAX_TENSOR_RANK:
        raise OnnxModelError("ONNX tensor rank exceeds the core limit")
    shape: list[int] = []
    for dimension in dimensions:
        if _which_oneof(dimension, "value") != "dim_value":
            raise OnnxModelError("ONNX tensor dimensions must be static")
        size = _integer(dimension, "dim_value")
        if size < 1 or size > MAX_AXIS_SIZE:
            raise OnnxModelError("ONNX tensor dimensions are out of bounds")
        shape.append(size)
    return _TensorSpec(dtype=dtype, shape=tuple(shape))


def _lower_graph(
    contract: _Contract,
    *,
    input_names: tuple[str, ...],
    output_names: tuple[str, ...],
    source_nodes: tuple[_SourceNode, ...],
    tensor_specs: dict[str, _TensorSpec],
) -> tuple[ComputationGraph, tuple[OnnxOperatorBinding, ...]]:
    value_bindings = {binding.onnx_name: binding for binding in contract.values}
    node_bindings = {binding.onnx_name: binding for binding in contract.nodes}
    if set(value_bindings) != set(tensor_specs):
        raise OnnxContractError(
            "ONNX contract must bind every graph value exactly once"
        )
    if set(node_bindings) != {node.onnx_name for node in source_nodes}:
        raise OnnxContractError(
            "ONNX contract must bind every graph node exactly once"
        )

    values = tuple(
        sorted(
            (
                ValueSpec(
                    value_id=binding.value_id,
                    dtype=tensor_specs[binding.onnx_name].dtype,
                    shape=tensor_specs[binding.onnx_name].shape,
                    unit_id=binding.unit_id,
                )
                for binding in contract.values
            ),
            key=lambda value: value.value_id,
        )
    )
    nodes = tuple(
        Node(
            node_id=node_bindings[source.onnx_name].node_id,
            operation=source.operation,
            inputs=tuple(
                value_bindings[input_name].value_id
                for input_name in source.inputs
            ),
            output=value_bindings[source.output].value_id,
        )
        for source in source_nodes
    )
    try:
        graph = ComputationGraph(
            graph_id=contract.graph_id,
            values=values,
            inputs=tuple(value_bindings[name].value_id for name in input_names),
            nodes=nodes,
            outputs=tuple(value_bindings[name].value_id for name in output_names),
        )
        graph.validate_units(BUILTIN_REGISTRY)
    except UnitSentinelError:
        raise OnnxContractError(
            "ONNX contract could not be lowered to the canonical graph"
        ) from None

    operator_bindings = tuple(
        OnnxOperatorBinding(
            onnx_name=source.onnx_name,
            onnx_op_type=source.op_type,
            node_id=node_bindings[source.onnx_name].node_id,
            operation=source.operation,
        )
        for source in source_nodes
    )
    return graph, operator_bindings


def _get(value: object, attribute: str) -> object:
    try:
        return cast(object, getattr(value, attribute))
    except AttributeError:
        raise OnnxModelError("ONNX model structure is malformed") from None


def _items(value: object, attribute: str) -> tuple[object, ...]:
    raw = _get(value, attribute)
    if isinstance(raw, (str, bytes, bytearray)):
        raise OnnxModelError("ONNX model structure is malformed")
    try:
        return tuple(cast(Iterable[object], raw))
    except TypeError:
        raise OnnxModelError("ONNX model structure is malformed") from None


def _text(value: object, attribute: str) -> str:
    return _string(_get(value, attribute), label="ONNX text field")


def _integer(value: object, attribute: str) -> int:
    raw = _get(value, attribute)
    if type(raw) is not int:
        raise OnnxModelError("ONNX integer field is malformed")
    return raw


def _which_oneof(value: object, group: str) -> str | None:
    method = _get(value, "WhichOneof")
    if not callable(method):
        raise OnnxModelError("ONNX model structure is malformed")
    result = cast(Callable[[str], object], method)(group)
    if result is not None and type(result) is not str:
        raise OnnxModelError("ONNX model structure is malformed")
    return cast(str | None, result)


def _has_field(value: object, field_name: str) -> bool:
    method = _get(value, "HasField")
    if not callable(method):
        raise OnnxModelError("ONNX model structure is malformed")
    result = cast(Callable[[str], object], method)(field_name)
    if type(result) is not bool:
        raise OnnxModelError("ONNX model structure is malformed")
    return result


def _record(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise OnnxContractError(f"{label} must be an object")
    record = cast(dict[str, object], value)
    if set(record) != fields:
        raise OnnxContractError(f"{label} has missing or unknown fields")
    return record


def _array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise OnnxContractError(f"{label} must be an array")
    return cast(list[object], value)


def _string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise OnnxContractError(f"{label} must be text")
    return value


def _require_source_name(value: str, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_ONNX_SOURCE_NAME_LENGTH
        or _ONNX_NAME.fullmatch(value) is None
    ):
        raise OnnxModelError(f"{label} is not in the reviewed ASCII subset")
    return value


def _require_core_identifier(value: str, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_UNIT_ID_LENGTH
        or UNIT_ID.fullmatch(value) is None
    ):
        raise OnnxContractError(f"{label} is not canonical")
    return value
