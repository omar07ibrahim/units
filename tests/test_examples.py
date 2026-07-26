from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from examples.build_wheel_anomaly_contract import build_graph
from unitsentinel import (
    VerificationStatus,
    constraint_catalog,
    decode_graph,
    verify_graph,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ExampleTests(unittest.TestCase):
    def test_speed_contract_generator_emits_exact_decodable_bytes(self) -> None:
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPOSITORY_ROOT / "examples" / "build_speed_contract.py"),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        self.assertFalse(completed.stdout.endswith(b"\n"))
        graph = decode_graph(completed.stdout)
        self.assertEqual(graph.graph_id, "speed-contract")
        self.assertEqual(
            graph.digest,
            "aecbeff2ce89cfd7b2aab6a0414ec307a5061577f4b8b0d1c53298f896569546",
        )

    def test_wheel_anomaly_variants_are_real_positive_and_conflict_cases(
        self,
    ) -> None:
        verified = verify_graph(build_graph("verified"))
        conflict_graph = build_graph("conflict")
        conflict = verify_graph(conflict_graph)

        self.assertEqual(verified.status, VerificationStatus.VERIFIED)
        self.assertEqual(len(verified.contracts), 10)
        self.assertEqual(verified.checks_performed, 2)
        self.assertEqual(conflict.status, VerificationStatus.CONFLICT)
        self.assertTrue(conflict.core_minimal)
        self.assertEqual(
            tuple(witness.constraint_id for witness in conflict.conflict_core),
            (
                "declaration/acceleration-si/unit",
                "operation/derive-acceleration/dimension",
                "operation/normalize-sample-period/dimension",
                "operation/normalize-speed-delta/dimension",
            ),
        )
        self.assertEqual(len(constraint_catalog(conflict_graph)), 24)

    def test_wheel_anomaly_generator_emits_exact_canonical_variants(self) -> None:
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        }
        expected = {
            "verified": (
                "wheel-anomaly-verified",
                "139e3e3d99d64c3d9cde89e9e1f116f09452c3532eaaee2e0513c71a0f2ada3c",
            ),
            "conflict": (
                "wheel-anomaly-conflict",
                "6ae6457c38e5dbe707187031a521e4c76124ee55ac58869a36ba746978a4f708",
            ),
        }
        for variant, (graph_id, digest) in expected.items():
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(
                        REPOSITORY_ROOT / "examples" / "build_wheel_anomaly_contract.py"
                    ),
                    "--variant",
                    variant,
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=5,
            )
            with self.subTest(variant=variant):
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode(),
                )
                self.assertEqual(completed.stderr, b"")
                self.assertFalse(completed.stdout.endswith(b"\n"))
                graph = decode_graph(completed.stdout)
                self.assertEqual(graph.graph_id, graph_id)
                self.assertEqual(graph.digest, digest)


if __name__ == "__main__":
    unittest.main()
