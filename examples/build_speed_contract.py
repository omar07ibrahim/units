"""Emit the canonical speed-normalization graph used in documentation."""

from __future__ import annotations

import sys

from unitsentinel import (
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
    encode_graph,
)


def build_graph() -> ComputationGraph:
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


def main() -> int:
    sys.stdout.buffer.write(encode_graph(build_graph()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
