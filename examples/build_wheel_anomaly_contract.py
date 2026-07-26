"""Emit a verified or deliberately conflicting wheel-anomaly feature graph."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final, Literal

from unitsentinel import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
    encode_graph,
)

GraphVariant = Literal["verified", "conflict"]
VARIANTS: Final[tuple[GraphVariant, ...]] = ("verified", "conflict")


def build_graph(variant: GraphVariant = "verified") -> ComputationGraph:
    """Build one realistic feature pipeline with an optional serving-contract bug."""

    if variant not in VARIANTS:
        raise ValueError("graph variant must be verified or conflict")
    acceleration_unit = (
        "meter-per-second-squared" if variant == "verified" else "meter-per-second"
    )
    return ComputationGraph(
        graph_id=f"wheel-anomaly-{variant}",
        values=(
            ValueSpec(
                "acceleration-reference",
                ScalarType.FLOAT32,
                ("batch",),
                "meter-per-second-squared",
            ),
            ValueSpec(
                "acceleration-si",
                ScalarType.FLOAT32,
                ("batch",),
                acceleration_unit,
            ),
            ValueSpec(
                "anomaly-score",
                ScalarType.FLOAT32,
                ("batch",),
                "one",
            ),
            ValueSpec(
                "normalized-acceleration",
                ScalarType.FLOAT32,
                ("batch",),
            ),
            ValueSpec(
                "previous-wheel-speed-kph",
                ScalarType.FLOAT32,
                ("batch",),
                "kilometer-per-hour",
            ),
            ValueSpec(
                "sample-period-ms",
                ScalarType.FLOAT32,
                ("batch",),
                "millisecond",
            ),
            ValueSpec(
                "sample-period-si",
                ScalarType.FLOAT32,
                ("batch",),
            ),
            ValueSpec("speed-delta-kph", ScalarType.FLOAT32, ("batch",)),
            ValueSpec("speed-delta-si", ScalarType.FLOAT32, ("batch",)),
            ValueSpec(
                "wheel-speed-kph",
                ScalarType.FLOAT32,
                ("batch",),
                "kilometer-per-hour",
            ),
        ),
        inputs=(
            "acceleration-reference",
            "previous-wheel-speed-kph",
            "sample-period-ms",
            "wheel-speed-kph",
        ),
        nodes=(
            Node(
                "compute-speed-delta",
                Operation.SUBTRACT,
                ("wheel-speed-kph", "previous-wheel-speed-kph"),
                "speed-delta-kph",
            ),
            Node(
                "normalize-speed-delta",
                Operation.CONVERT,
                ("speed-delta-kph",),
                "speed-delta-si",
                target_unit_id="meter-per-second",
            ),
            Node(
                "normalize-sample-period",
                Operation.CONVERT,
                ("sample-period-ms",),
                "sample-period-si",
                target_unit_id="second",
            ),
            Node(
                "derive-acceleration",
                Operation.DIVIDE,
                ("speed-delta-si", "sample-period-si"),
                "acceleration-si",
            ),
            Node(
                "normalize-acceleration",
                Operation.DIVIDE,
                ("acceleration-si", "acceleration-reference"),
                "normalized-acceleration",
            ),
            Node(
                "score-acceleration",
                Operation.SIGMOID,
                ("normalized-acceleration",),
                "anomaly-score",
            ),
        ),
        outputs=("acceleration-si", "anomaly-score"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit one canonical wheel-anomaly contract graph.",
    )
    parser.add_argument(
        "--variant",
        choices=VARIANTS,
        default="verified",
        help="select the valid pipeline or its deliberate serving-contract bug",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    variant = arguments.variant
    if variant not in VARIANTS:
        raise AssertionError("argparse returned an unsupported graph variant")
    sys.stdout.buffer.write(encode_graph(build_graph(variant)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
