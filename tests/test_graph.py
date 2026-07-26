from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction

from unitsentinel.graph import (
    MAX_AXIS_SIZE,
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    MAX_GRAPH_VALUES,
    MAX_TENSOR_RANK,
    ComputationGraph,
    GraphValidationError,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.registry import BUILTIN_REGISTRY, UnknownUnitError


def value(
    value_id: str,
    *,
    unit_id: str | None = None,
    shape: tuple[int | str, ...] = ("batch",),
) -> ValueSpec:
    return ValueSpec(value_id, ScalarType.FLOAT32, shape, unit_id)


def arithmetic_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="ratio-pipeline",
        values=(
            value("denominator", unit_id="meter"),
            value("numerator", unit_id="meter"),
            value("ratio"),
            value("sum", unit_id="meter"),
        ),
        inputs=("numerator", "denominator"),
        nodes=(
            Node(
                "sum-inputs",
                Operation.ADD,
                ("numerator", "denominator"),
                "sum",
            ),
            Node(
                "normalize-sum",
                Operation.DIVIDE,
                ("sum", "denominator"),
                "ratio",
            ),
        ),
        outputs=("ratio",),
    )


class ValueAndNodeTests(unittest.TestCase):
    def test_scalar_and_symbolic_shapes_are_exact(self) -> None:
        scalar = value("threshold", shape=())
        tensor = value("features", shape=("batch", 32))

        self.assertEqual(scalar.canonical_record()["shape"], [])
        self.assertEqual(tensor.canonical_record()["shape"], ["batch", 32])

    def test_shape_types_rank_and_sizes_are_bounded(self) -> None:
        cases = (
            ((True,), "integers or symbols"),
            ((0,), "out of bounds"),
            ((MAX_AXIS_SIZE + 1,), "out of bounds"),
            (("Batch",), "integers or symbols"),
            ((1,) * (MAX_TENSOR_RANK + 1), "rank limit"),
        )
        for shape, message in cases:
            with (
                self.subTest(shape=shape),
                self.assertRaisesRegex(GraphValidationError, message),
            ):
                value("tensor", shape=shape)  # type: ignore[arg-type]

        with self.assertRaisesRegex(GraphValidationError, "shape must be a tuple"):
            ValueSpec("tensor", ScalarType.FLOAT32, [1])  # type: ignore[arg-type]
        with self.assertRaisesRegex(GraphValidationError, "dtype is not supported"):
            ValueSpec("tensor", "float32", (1,))  # type: ignore[arg-type]

    def test_value_identifiers_and_optional_units_are_canonical(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "value identifier"):
            value("Input")
        with self.assertRaisesRegex(GraphValidationError, "unit identifier"):
            value("input", unit_id="m/s")

    def test_operation_arities_and_attributes_are_closed(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "not supported"):
            Node(
                "bad-operation",
                "add",  # type: ignore[arg-type]
                ("left", "right"),
                "output",
            )
        with self.assertRaisesRegex(GraphValidationError, "inputs must be a tuple"):
            Node(
                "bad-inputs",
                Operation.ADD,
                ["left", "right"],  # type: ignore[arg-type]
                "output",
            )
        with self.assertRaisesRegex(GraphValidationError, "input arity"):
            Node("bad-add", Operation.ADD, ("left",), "output")
        with self.assertRaisesRegex(GraphValidationError, "exact exponent"):
            Node("bad-power", Operation.POWER, ("input",), "output")
        with self.assertRaisesRegex(GraphValidationError, "exact exponent"):
            Node(
                "float-power",
                Operation.POWER,
                ("input",),
                "output",
                exponent=2,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(GraphValidationError, "target unit"):
            Node("bad-convert", Operation.CONVERT, ("input",), "output")
        with self.assertRaisesRegex(GraphValidationError, "cannot declare a target"):
            Node(
                "confused-power",
                Operation.POWER,
                ("input",),
                "output",
                exponent=Fraction(2),
                target_unit_id="meter",
            )
        with self.assertRaisesRegex(GraphValidationError, "cannot declare an exponent"):
            Node(
                "confused-conversion",
                Operation.CONVERT,
                ("input",),
                "output",
                exponent=Fraction(1),
                target_unit_id="meter",
            )
        with self.assertRaisesRegex(GraphValidationError, "does not accept"):
            Node(
                "bad-identity",
                Operation.IDENTITY,
                ("input",),
                "output",
                target_unit_id="meter",
            )

        power = Node(
            "square",
            Operation.POWER,
            ("input",),
            "output",
            exponent=Fraction(2),
        )
        conversion = Node(
            "to-si",
            Operation.CONVERT,
            ("input",),
            "output",
            target_unit_id="meter-per-second",
        )
        self.assertEqual(power.canonical_record()["attributes"], {"exponent": "2"})
        self.assertEqual(
            conversion.canonical_record()["attributes"],
            {"unit_id": "meter-per-second"},
        )

    def test_value_and_node_receiver_subclasses_fail_closed(self) -> None:
        class DerivedValue(ValueSpec):
            pass

        class DerivedNode(Node):
            pass

        with self.assertRaisesRegex(GraphValidationError, "exact ValueSpec"):
            DerivedValue("value", ScalarType.FLOAT32, ())
        with self.assertRaisesRegex(GraphValidationError, "exact Node"):
            DerivedNode(
                "node",
                Operation.IDENTITY,
                ("input",),
                "output",
            )


class ComputationGraphTests(unittest.TestCase):
    def test_graph_identity_and_canonical_record_are_stable(self) -> None:
        graph = arithmetic_graph()

        self.assertEqual(graph.value("sum").unit_id, "meter")
        self.assertEqual(
            graph.digest,
            "09ab5202fac1c1ca37e5e617612d41c056449695b9fa21ee2aed88be52fefe1a",
        )
        self.assertEqual(graph.canonical_record()["graph_id"], "ratio-pipeline")
        self.assertEqual(graph.canonical_record()["schema"], "unitsentinel.graph/v1")
        self.assertEqual(graph.canonical_bytes()[-1:], b"}")

    def test_values_must_be_sorted_unique_and_exact(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "sorted"):
            ComputationGraph(
                "identity",
                (value("z"), value("a")),
                ("z",),
                (),
                ("z",),
            )
        with self.assertRaisesRegex(GraphValidationError, "unique"):
            ComputationGraph(
                "identity",
                (value("x"), value("x")),
                ("x",),
                (),
                ("x",),
            )
        with self.assertRaisesRegex(GraphValidationError, "exact ValueSpec"):
            ComputationGraph(
                "identity",
                (object(),),  # type: ignore[arg-type]
                ("x",),
                (),
                ("x",),
            )

    def test_topology_rejects_forward_edges_and_multiple_producers(self) -> None:
        values = (value("input"), value("middle"), value("output"))
        with self.assertRaisesRegex(GraphValidationError, "earlier available"):
            ComputationGraph(
                "forward-edge",
                values,
                ("input",),
                (
                    Node(
                        "consume-future",
                        Operation.IDENTITY,
                        ("middle",),
                        "output",
                    ),
                    Node(
                        "produce-middle",
                        Operation.IDENTITY,
                        ("input",),
                        "middle",
                    ),
                ),
                ("output",),
            )
        with self.assertRaisesRegex(GraphValidationError, "one producer"):
            ComputationGraph(
                "overwrite-input",
                (value("input"),),
                ("input",),
                (Node("overwrite", Operation.IDENTITY, ("input",), "input"),),
                ("input",),
            )

    def test_declared_values_and_outputs_form_one_closed_graph(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "one producer"):
            ComputationGraph(
                "missing-producer",
                (value("input"), value("orphan")),
                ("input",),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "declared value"):
            ComputationGraph(
                "undeclared-output",
                (value("input"),),
                ("input",),
                (Node("emit", Operation.IDENTITY, ("input",), "missing"),),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "contribute"):
            ComputationGraph(
                "dead-input",
                (value("unused"), value("used")),
                ("used", "unused"),
                (),
                ("used",),
            )
        with self.assertRaisesRegex(GraphValidationError, "not a declared value"):
            ComputationGraph(
                "unknown-public-output",
                (value("input"),),
                ("input",),
                (),
                ("missing",),
            )

    def test_node_identifiers_are_unique_and_disjoint_from_values(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "collides"):
            ComputationGraph(
                "collision",
                (value("input"), value("output")),
                ("input",),
                (
                    Node(
                        "input",
                        Operation.IDENTITY,
                        ("input",),
                        "output",
                    ),
                ),
                ("output",),
            )
        with self.assertRaisesRegex(GraphValidationError, "node identifiers"):
            ComputationGraph(
                "duplicate-node",
                (value("input"), value("middle"), value("output")),
                ("input",),
                (
                    Node("step", Operation.IDENTITY, ("input",), "middle"),
                    Node("step", Operation.IDENTITY, ("middle",), "output"),
                ),
                ("output",),
            )

    def test_graph_resource_and_container_bounds_fail_before_traversal(self) -> None:
        with self.assertRaisesRegex(GraphValidationError, "at least one value"):
            ComputationGraph("empty-values", (), ("input",), (), ("input",))
        with self.assertRaisesRegex(GraphValidationError, "values must be a tuple"):
            ComputationGraph(
                "bad-values",
                [value("input")],  # type: ignore[arg-type]
                ("input",),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "too many values"):
            ComputationGraph(
                "too-many-values",
                (value("input"),) * (MAX_GRAPH_VALUES + 1),
                ("input",),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "too many inputs"):
            ComputationGraph(
                "too-many-inputs",
                (value("input"),),
                ("input",) * (MAX_GRAPH_INPUTS + 1),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "inputs must be a tuple"):
            ComputationGraph(
                "bad-input-container",
                (value("input"),),
                ["input"],  # type: ignore[arg-type]
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "at least one input"):
            ComputationGraph(
                "empty-inputs",
                (value("input"),),
                (),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(
            GraphValidationError,
            "input identifiers must be unique",
        ):
            ComputationGraph(
                "duplicate-inputs",
                (value("input"),),
                ("input", "input"),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "not a declared value"):
            ComputationGraph(
                "unknown-input",
                (value("input"),),
                ("missing",),
                (),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "nodes must be a tuple"):
            ComputationGraph(
                "bad-node-container",
                (value("input"),),
                ("input",),
                [],  # type: ignore[arg-type]
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "exact Node"):
            ComputationGraph(
                "bad-node",
                (value("input"),),
                ("input",),
                (object(),),  # type: ignore[arg-type]
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "too many nodes"):
            ComputationGraph(
                "too-many-nodes",
                (value("input"),),
                ("input",),
                (Node("step", Operation.IDENTITY, ("input",), "input"),)
                * (MAX_GRAPH_NODES + 1),
                ("input",),
            )
        with self.assertRaisesRegex(GraphValidationError, "outputs must be a tuple"):
            ComputationGraph(
                "bad-output-container",
                (value("input"),),
                ("input",),
                (),
                ["input"],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(GraphValidationError, "at least one output"):
            ComputationGraph(
                "empty-outputs",
                (value("input"),),
                ("input",),
                (),
                (),
            )
        with self.assertRaisesRegex(
            GraphValidationError,
            "output identifiers must be unique",
        ):
            ComputationGraph(
                "duplicate-outputs",
                (value("input"),),
                ("input",),
                (),
                ("input", "input"),
            )
        with self.assertRaisesRegex(GraphValidationError, "too many outputs"):
            ComputationGraph(
                "too-many-outputs",
                (value("input"),),
                ("input",),
                (),
                ("input",) * (MAX_GRAPH_OUTPUTS + 1),
            )

    def test_registry_validation_rejects_aliases_and_unknown_units(self) -> None:
        canonical = ComputationGraph(
            "canonical-unit",
            (value("input", unit_id="meter"),),
            ("input",),
            (),
            ("input",),
        )
        canonical.validate_units(BUILTIN_REGISTRY)

        alias = ComputationGraph(
            "alias-unit",
            (value("input", unit_id="metre"),),
            ("input",),
            (),
            ("input",),
        )
        with self.assertRaisesRegex(GraphValidationError, "canonical registry"):
            alias.validate_units(BUILTIN_REGISTRY)

        unknown = ComputationGraph(
            "unknown-unit",
            (value("input", unit_id="smoot"),),
            ("input",),
            (),
            ("input",),
        )
        with self.assertRaises(UnknownUnitError):
            unknown.validate_units(BUILTIN_REGISTRY)
        with self.assertRaisesRegex(GraphValidationError, "exact UnitRegistry"):
            canonical.validate_units(object())  # type: ignore[arg-type]

    def test_nested_mutation_and_subclasses_fail_closed(self) -> None:
        class DerivedGraph(ComputationGraph):
            pass

        with self.assertRaisesRegex(GraphValidationError, "exact ComputationGraph"):
            DerivedGraph(
                "derived",
                (value("input"),),
                ("input",),
                (),
                ("input",),
            )

        graph = arithmetic_graph()
        object.__setattr__(graph.values[0], "unit_id", "kilometer")
        with self.assertRaisesRegex(GraphValidationError, "does not match"):
            graph.canonical_bytes()

        graph = arithmetic_graph()
        object.__setattr__(graph, "_digest", "not-a-digest")
        with self.assertRaisesRegex(GraphValidationError, "digest is malformed"):
            graph.canonical_record()

        with self.assertRaisesRegex(GraphValidationError, "not present"):
            arithmetic_graph().value("missing")

        graph = arithmetic_graph()
        with self.assertRaises(FrozenInstanceError):
            graph.outputs = ("sum",)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
