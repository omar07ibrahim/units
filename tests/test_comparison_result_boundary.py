from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tools.measure_comparison_result_boundary import (
    comparison_result_boundary_summary,
)
from unitsentinel.canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]


class ComparisonResultBoundaryMeasurementTests(unittest.TestCase):
    def test_shape_only_envelope_has_exact_reproducible_measurements(self) -> None:
        summary = comparison_result_boundary_summary()

        self.assertEqual(
            summary["classification"],
            "conservative-independent-field-shape-only-envelope",
        )
        self.assertIs(summary["model_constructible"], False)
        self.assertIs(summary["exact_maximum_proven"], False)
        self.assertEqual(
            summary["measurements"],
            {
                "canonical_bytes": 24_402_018,
                "canonical_sha256": (
                    "134cb93f66e7d2b26f38397a3dc3bf45fec3e9788ca7789115a3874e2dc940be"
                ),
                "maximum_container_items": 576,
                "maximum_depth": 10,
                "maximum_integer_digits": 5,
                "maximum_string_length": 158,
                "object_key_tokens": 234_888,
                "preflight_tokens_including_object_keys": 779_409,
                "tree_values_excluding_object_keys": 544_521,
            },
        )
        self.assertEqual(
            summary["within_selected_transport_limits"],
            {
                "all": True,
                "canonical_bytes": True,
                "maximum_container_items": True,
                "maximum_depth": True,
                "maximum_integer_digits": True,
                "maximum_string_length": True,
                "preflight_tokens_including_object_keys": True,
            },
        )
        self.assertEqual(
            summary["selected_transport_limits"],
            {
                "canonical_bytes": 33_554_432,
                "maximum_container_items": 2_112,
                "maximum_depth": 10,
                "maximum_integer_digits": 10,
                "maximum_string_length": 192,
                "preflight_tokens_including_object_keys": 1_048_576,
            },
        )

    def test_module_prints_the_same_canonical_summary(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.measure_comparison_result_boundary",
            ],
            cwd=ROOT,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": str(ROOT / "src"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(
            completed.stdout,
            canonical_json_bytes(comparison_result_boundary_summary()) + b"\n",
        )
        self.assertEqual(completed.stderr, b"")


if __name__ == "__main__":
    unittest.main()
