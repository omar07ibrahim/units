"""Bounded immutable computation-graph contracts.

The graph layer validates structure and provenance identifiers. Dimensional
rules are compiled by the verifier in a later layer; accepting a graph here is
not a claim that its operations are dimensionally consistent.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Final, TypeAlias

from .canonical import canonical_json_bytes, sha256_hex
from .domain import (
    MAX_UNIT_ID_LENGTH,
    UNIT_ID,
    UnitSentinelError,
    _fraction_text,
    _validate_exponent,
)
from .registry import SHA256_HEX, UnitRegistry

GRAPH_SCHEMA: Final = "unitsentinel.graph/v1"
MAX_GRAPH_ID_LENGTH: Final = 64
MAX_GRAPH_INPUTS: Final = 64
MAX_GRAPH_NODES: Final = 512
MAX_GRAPH_OUTPUTS: Final = 64
MAX_GRAPH_VALUES: Final = MAX_GRAPH_INPUTS + MAX_GRAPH_NODES
MAX_TENSOR_RANK: Final = 8
MAX_AXIS_SIZE: Final = 2_147_483_647
MAX_AXIS_SYMBOL_LENGTH: Final = 32
AXIS_SYMBOL: Final = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

ShapeAxis: TypeAlias = int | str


class GraphError(UnitSentinelError):
    """Base class for stable graph-contract failures."""


class GraphValidationError(GraphError):
    """Raised when an immutable graph value violates the v1 contract."""


class ScalarType(StrEnum):
    """Closed scalar types accepted by the dimensional graph core."""

    FLOAT16 = "float16"
    BFLOAT16 = "bfloat16"
    FLOAT32 = "float32"
    FLOAT64 = "float64"


class Operation(StrEnum):
    """Closed v1 operation set with independently specified dimensional rules."""

    ADD = "add"
    CONVERT = "convert"
    DIVIDE = "divide"
    EXP = "exp"
    IDENTITY = "identity"
    LOG = "log"
    MATMUL = "matmul"
    MAXIMUM = "maximum"
    MINIMUM = "minimum"
    MULTIPLY = "multiply"
    POWER = "power"
    SIGMOID = "sigmoid"
    SOFTMAX = "softmax"
    SUBTRACT = "subtract"


UNARY_OPERATIONS: Final = frozenset(
    {
        Operation.CONVERT,
        Operation.EXP,
        Operation.IDENTITY,
        Operation.LOG,
        Operation.POWER,
        Operation.SIGMOID,
        Operation.SOFTMAX,
    }
)
BINARY_OPERATIONS: Final = frozenset(Operation) - UNARY_OPERATIONS


def _require_identifier(
    value: object,
    *,
    label: str,
    max_length: int = MAX_UNIT_ID_LENGTH,
) -> str:
    if (
        type(value) is not str
        or len(value) > max_length
        or UNIT_ID.fullmatch(value) is None
    ):
        raise GraphValidationError(f"{label} is not canonical")
    return value


def _validate_shape_axis(axis: object) -> None:
    if type(axis) is int:
        if axis <= 0 or axis > MAX_AXIS_SIZE:
            raise GraphValidationError("concrete shape dimensions are out of bounds")
        return
    if (
        type(axis) is str
        and len(axis) <= MAX_AXIS_SYMBOL_LENGTH
        and AXIS_SYMBOL.fullmatch(axis) is not None
    ):
        return
    raise GraphValidationError("shape dimensions must be bounded integers or symbols")


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """One declared tensor value, optionally annotated with a concrete unit."""

    value_id: str
    dtype: ScalarType
    shape: tuple[ShapeAxis, ...]
    unit_id: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not ValueSpec:
            raise GraphValidationError("value declaration must be an exact ValueSpec")
        _require_identifier(self.value_id, label="value identifier")
        if type(self.dtype) is not ScalarType:
            raise GraphValidationError("value dtype is not supported")
        if type(self.shape) is not tuple:
            raise GraphValidationError("value shape must be a tuple")
        if len(self.shape) > MAX_TENSOR_RANK:
            raise GraphValidationError("value shape exceeds the rank limit")
        for axis in self.shape:
            _validate_shape_axis(axis)
        if self.unit_id is not None:
            _require_identifier(self.unit_id, label="value unit identifier")

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "dtype": self.dtype.value,
            "shape": list(self.shape),
            "unit_id": self.unit_id,
            "value_id": self.value_id,
        }


@dataclass(frozen=True, slots=True)
class Node:
    """One source-labelled operation producing exactly one declared value."""

    node_id: str
    operation: Operation
    inputs: tuple[str, ...]
    output: str
    exponent: Fraction | None = None
    target_unit_id: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not Node:
            raise GraphValidationError("node must be an exact Node")
        _require_identifier(self.node_id, label="node identifier")
        if type(self.operation) is not Operation:
            raise GraphValidationError("node operation is not supported")
        if type(self.inputs) is not tuple:
            raise GraphValidationError("node inputs must be a tuple")
        for input_id in self.inputs:
            _require_identifier(input_id, label="node input identifier")
        _require_identifier(self.output, label="node output identifier")

        expected_arity = 1 if self.operation in UNARY_OPERATIONS else 2
        if len(self.inputs) != expected_arity:
            raise GraphValidationError("node input arity does not match its operation")

        if self.operation is Operation.POWER:
            if type(self.exponent) is not Fraction:
                raise GraphValidationError("power nodes require an exact exponent")
            _validate_exponent(self.exponent)
            if self.target_unit_id is not None:
                raise GraphValidationError("power nodes cannot declare a target unit")
            return
        if self.operation is Operation.CONVERT:
            if self.exponent is not None:
                raise GraphValidationError(
                    "conversion nodes cannot declare an exponent"
                )
            _require_identifier(
                self.target_unit_id,
                label="conversion target unit identifier",
            )
            return
        if self.exponent is not None or self.target_unit_id is not None:
            raise GraphValidationError("operation does not accept node attributes")

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        attributes: dict[str, str] = {}
        if self.operation is Operation.POWER:
            assert self.exponent is not None
            attributes["exponent"] = _fraction_text(self.exponent)
        elif self.operation is Operation.CONVERT:
            assert self.target_unit_id is not None
            attributes["unit_id"] = self.target_unit_id
        return {
            "attributes": attributes,
            "inputs": list(self.inputs),
            "node_id": self.node_id,
            "operation": self.operation.value,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class ComputationGraph:
    """A closed topological graph with a mutation-detecting content digest."""

    graph_id: str
    values: tuple[ValueSpec, ...]
    inputs: tuple[str, ...]
    nodes: tuple[Node, ...]
    outputs: tuple[str, ...]
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not ComputationGraph:
            raise GraphValidationError("graph must be an exact ComputationGraph")
        _require_identifier(
            self.graph_id,
            label="graph identifier",
            max_length=MAX_GRAPH_ID_LENGTH,
        )
        self._validate_values()
        self._validate_inputs()
        self._validate_nodes_and_topology()
        self._validate_outputs_and_reachability()

    def _validate_values(self) -> None:
        if type(self.values) is not tuple:
            raise GraphValidationError("graph values must be a tuple")
        if not self.values:
            raise GraphValidationError("graph must declare at least one value")
        if len(self.values) > MAX_GRAPH_VALUES:
            raise GraphValidationError("graph contains too many values")
        value_ids: list[str] = []
        for value in self.values:
            if type(value) is not ValueSpec:
                raise GraphValidationError(
                    "graph values must be exact ValueSpec instances"
                )
            value.validate()
            value_ids.append(value.value_id)
        if len(set(value_ids)) != len(value_ids):
            raise GraphValidationError("graph value identifiers must be unique")
        if value_ids != sorted(value_ids):
            raise GraphValidationError("graph values must be sorted by identifier")

    def _validate_inputs(self) -> None:
        if type(self.inputs) is not tuple:
            raise GraphValidationError("graph inputs must be a tuple")
        if not self.inputs:
            raise GraphValidationError("graph must declare at least one input")
        if len(self.inputs) > MAX_GRAPH_INPUTS:
            raise GraphValidationError("graph contains too many inputs")
        for input_id in self.inputs:
            _require_identifier(input_id, label="graph input identifier")
        if len(set(self.inputs)) != len(self.inputs):
            raise GraphValidationError("graph input identifiers must be unique")
        declared = {value.value_id for value in self.values}
        if not set(self.inputs).issubset(declared):
            raise GraphValidationError("graph input is not a declared value")

    def _validate_nodes_and_topology(self) -> None:
        if type(self.nodes) is not tuple:
            raise GraphValidationError("graph nodes must be a tuple")
        if len(self.nodes) > MAX_GRAPH_NODES:
            raise GraphValidationError("graph contains too many nodes")

        declared = {value.value_id for value in self.values}
        available = set(self.inputs)
        node_ids: set[str] = set()
        for node in self.nodes:
            if type(node) is not Node:
                raise GraphValidationError("graph nodes must be exact Node instances")
            node.validate()
            if node.node_id in node_ids:
                raise GraphValidationError("graph node identifiers must be unique")
            if node.node_id in declared:
                raise GraphValidationError("node identifier collides with a value")
            node_ids.add(node.node_id)
            if not set(node.inputs).issubset(available):
                raise GraphValidationError(
                    "node inputs must reference earlier available values"
                )
            if node.output not in declared:
                raise GraphValidationError("node output is not a declared value")
            if node.output in available:
                raise GraphValidationError("graph values must have one producer")
            available.add(node.output)
        if available != declared:
            raise GraphValidationError(
                "every non-input value must have exactly one producer"
            )

    def _validate_outputs_and_reachability(self) -> None:
        if type(self.outputs) is not tuple:
            raise GraphValidationError("graph outputs must be a tuple")
        if not self.outputs:
            raise GraphValidationError("graph must declare at least one output")
        if len(self.outputs) > MAX_GRAPH_OUTPUTS:
            raise GraphValidationError("graph contains too many outputs")
        for output_id in self.outputs:
            _require_identifier(output_id, label="graph output identifier")
        if len(set(self.outputs)) != len(self.outputs):
            raise GraphValidationError("graph output identifiers must be unique")

        declared = {value.value_id for value in self.values}
        if not set(self.outputs).issubset(declared):
            raise GraphValidationError("graph output is not a declared value")

        producer_inputs = {node.output: node.inputs for node in self.nodes}
        required = set(self.outputs)
        pending = list(self.outputs)
        while pending:
            value_id = pending.pop()
            for input_id in producer_inputs.get(value_id, ()):
                if input_id not in required:
                    required.add(input_id)
                    pending.append(input_id)
        if required != declared:
            raise GraphValidationError(
                "every graph value must contribute to a declared output"
            )

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise GraphValidationError("graph digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise GraphValidationError("graph digest does not match its contents")

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "graph_id": self.graph_id,
            "inputs": list(self.inputs),
            "nodes": [node.canonical_record() for node in self.nodes],
            "outputs": list(self.outputs),
            "schema": GRAPH_SCHEMA,
            "values": [value.canonical_record() for value in self.values],
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

    def value(self, value_id: str) -> ValueSpec:
        self.validate()
        lookup_id = _require_identifier(value_id, label="value lookup identifier")
        for value in self.values:
            if value.value_id == lookup_id:
                return value
        raise GraphValidationError("value identifier is not present in this graph")

    def validate_units(self, registry: UnitRegistry) -> None:
        """Require graph annotations to name canonical units in one snapshot."""

        self.validate()
        if type(registry) is not UnitRegistry:
            raise GraphValidationError("unit registry must be an exact UnitRegistry")
        registry.validate()
        unit_ids = tuple(
            value.unit_id for value in self.values if value.unit_id is not None
        ) + tuple(
            node.target_unit_id
            for node in self.nodes
            if node.target_unit_id is not None
        )
        for unit_id in unit_ids:
            assert unit_id is not None
            resolved = registry.resolve(unit_id)
            if resolved.unit_id != unit_id:
                raise GraphValidationError(
                    "graph unit annotations must use canonical registry identifiers"
                )
