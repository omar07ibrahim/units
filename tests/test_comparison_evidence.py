from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast

from tools.evidence import comparison_evidence
from tools.evidence.generate import EvidenceError
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonStatus,
    MismatchCode,
)
from unitsentinel.comparison_codec import decode_comparison_plan
from unitsentinel.comparison_result_codec import decode_comparison_result
from unitsentinel.graph import Operation
from unitsentinel.graph_codec import decode_graph
from unitsentinel.registry import BUILTIN_REGISTRY
from unitsentinel.verification import VerificationStatus

ROOT = Path(__file__).resolve().parents[1]


def canonical_document(path: Path, *, transport_newline: bool) -> object:
    payload = path.read_bytes()
    value = json.loads(payload)
    suffix = b"\n" if transport_newline else b""
    if payload != canonical_json_bytes(value) + suffix:
        raise AssertionError(f"{path.relative_to(ROOT)} is not canonical JSON")
    return value


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ComparisonEvidenceTests(unittest.TestCase):
    def test_generator_reproduces_every_committed_comparison_byte(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.evidence.comparison_evidence",
                "--check",
            ],
            cwd=ROOT,
            env={
                "HOME": str(ROOT / ".unitsentinel" / "comparison-test-home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": str(ROOT / "src"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_recorder_has_one_closed_output_allowlist(self) -> None:
        self.assertEqual(
            {
                path.relative_to(ROOT).as_posix()
                for path in comparison_evidence.EXPECTED_OUTPUT_PATHS
            },
            {
                "docs/evidence/captures/compare-compatible.json",
                "docs/evidence/captures/compare-compatible.txt",
                "docs/evidence/captures/compare-drift.json",
                "docs/evidence/captures/compare-drift.txt",
                "docs/evidence/captures/compare-indeterminate.json",
                "docs/evidence/captures/compare-indeterminate.txt",
                "docs/evidence/claims/ratio-compatible.result.json",
                "docs/evidence/claims/ratio-drift.result.json",
                "docs/evidence/claims/ratio-indeterminate.result.json",
                "docs/evidence/comparison-provenance.json",
                "docs/evidence/contracts/ratio-serving-renamed.json",
                "docs/evidence/contracts/ratio-serving-reversed.json",
                "docs/evidence/contracts/ratio-serving-underconstrained.json",
                "docs/evidence/contracts/ratio-training.json",
                "docs/evidence/data/comparison-artifacts.json",
                "docs/evidence/plans/ratio-compatible.plan.json",
                "docs/evidence/plans/ratio-drift.plan.json",
                "docs/evidence/plans/ratio-indeterminate.plan.json",
            },
        )
        with self.assertRaisesRegex(EvidenceError, "allowlist"):
            comparison_evidence._write_files({})

    def test_graph_and_plan_fixtures_are_canonical_and_cross_bound(self) -> None:
        training, cases = comparison_evidence._models()
        training_payload = comparison_evidence.TRAINING_GRAPH_PATH.read_bytes()
        self.assertEqual(decode_graph(training_payload), training)
        self.assertEqual(training.nodes[0].operation, Operation.DIVIDE)
        self.assertEqual(training.nodes[0].inputs, ("numerator", "denominator"))

        by_name = {case.name: case for case in cases}
        for case in cases:
            with self.subTest(case=case.name):
                serving_payload = case.serving_path.read_bytes()
                plan_payload = comparison_evidence.PLAN_PATHS[case.name].read_bytes()
                self.assertEqual(decode_graph(serving_payload), case.serving_graph)
                self.assertEqual(decode_comparison_plan(plan_payload), case.plan)
                self.assertEqual(case.plan.training_graph_digest, training.digest)
                self.assertEqual(
                    case.plan.serving_graph_digest,
                    case.serving_graph.digest,
                )
                self.assertEqual(case.plan.registry_digest, BUILTIN_REGISTRY.digest)
                self.assertEqual(len(case.plan.bindings), 3)

        self.assertEqual(
            by_name["compatible"].serving_graph.nodes[0].inputs,
            ("request-numerator", "request-denominator"),
        )
        self.assertEqual(
            by_name["drift"].serving_graph.nodes[0].inputs,
            ("request-denominator", "request-numerator"),
        )
        underconstrained = by_name["indeterminate"].serving_graph
        self.assertIsNone(underconstrained.value("request-numerator").unit_id)
        self.assertIsNone(underconstrained.value("request-denominator").unit_id)
        self.assertEqual(underconstrained.value("prediction").unit_id, "one")

    def test_captures_and_raw_claims_are_strictly_cross_bound(self) -> None:
        training, cases = comparison_evidence._models()
        for case in cases:
            with self.subTest(case=case.name):
                capture_value = canonical_document(
                    comparison_evidence.CAPTURE_JSON_PATHS[case.name],
                    transport_newline=True,
                )
                claim_path = comparison_evidence.CLAIM_PATHS[case.name]
                claim = claim_path.read_bytes()
                claim_value = canonical_document(
                    claim_path,
                    transport_newline=False,
                )
                capture, result = comparison_evidence._validate_capture(
                    capture_value,
                    case=case,
                    training=training,
                    claim_payload=claim,
                )
                self.assertEqual(capture, capture_value)
                self.assertEqual(decode_comparison_result(claim), result)
                self.assertEqual(capture["result"]["record"], claim_value)
                self.assertEqual(capture["result"]["sha256"], sha256(claim))
                self.assertEqual(result.authentication, AUTHENTICATION_NOT_PROVIDED)
                self.assertEqual(result.scope, COMPARISON_SCOPE_UNDER_PLAN)

    def test_compatibility_uses_semantic_normalization_not_lineage_identity(
        self,
    ) -> None:
        compatible = decode_comparison_result(
            comparison_evidence.CLAIM_PATHS["compatible"].read_bytes()
        )
        drift = decode_comparison_result(
            comparison_evidence.CLAIM_PATHS["drift"].read_bytes()
        )

        self.assertEqual(compatible.status, ComparisonStatus.COMPATIBLE)
        self.assertIsNotNone(compatible.training_lineage)
        self.assertIsNotNone(compatible.serving_lineage)
        assert compatible.training_lineage is not None
        assert compatible.serving_lineage is not None
        self.assertNotEqual(
            compatible.training_lineage.digest,
            compatible.serving_lineage.digest,
        )
        self.assertEqual(
            compatible.training_lineage.semantic_digest,
            compatible.serving_lineage.semantic_digest,
        )
        compatible_output = next(
            item for item in compatible.comparisons if item.contract_id == "output-00"
        )
        assert compatible_output.normalization is not None
        self.assertEqual(
            compatible_output.normalization.training_digest,
            compatible_output.normalization.serving_digest,
        )
        self.assertEqual(compatible_output.mismatches, ())

        drift_output = next(
            item for item in drift.comparisons if item.contract_id == "output-00"
        )
        assert drift_output.normalization is not None
        self.assertNotEqual(
            drift_output.normalization.training_digest,
            drift_output.normalization.serving_digest,
        )
        self.assertEqual(
            drift_output.mismatches,
            (MismatchCode.NORMALIZATION_LINEAGE_DRIFT,),
        )

    def test_indeterminate_capture_publishes_no_interface_diff(self) -> None:
        result = decode_comparison_result(
            comparison_evidence.CLAIM_PATHS["indeterminate"].read_bytes()
        )
        self.assertEqual(result.status, ComparisonStatus.INDETERMINATE)
        self.assertEqual(result.reason.value, "serving-not-verified")  # type: ignore[union-attr]
        self.assertEqual(result.comparisons, ())
        self.assertIsNone(result.training_lineage)
        self.assertIsNone(result.serving_lineage)
        assert result.training_result is not None
        assert result.serving_result is not None
        self.assertEqual(
            result.training_result.status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            result.serving_result.status,
            VerificationStatus.UNDERCONSTRAINED,
        )

    def test_text_captures_are_exact_real_cli_transcripts(self) -> None:
        training, cases = comparison_evidence._models()
        for case in cases:
            with self.subTest(case=case.name):
                transcript = comparison_evidence.CAPTURE_TEXT_PATHS[
                    case.name
                ].read_bytes()
                prefix = (
                    "\n".join(comparison_evidence._command_lines(case)).encode() + b"\n"
                )
                suffix = f"[exit {case.exit_code}]\n".encode()
                self.assertTrue(transcript.startswith(prefix))
                self.assertTrue(transcript.endswith(suffix))
                output = transcript[len(prefix) : -len(suffix)]
                result = decode_comparison_result(
                    comparison_evidence.CLAIM_PATHS[case.name].read_bytes()
                )
                comparison_evidence._validate_text_output(
                    output,
                    case=case,
                    training=training,
                    result=result,
                )

    def test_displayed_text_command_is_the_exact_executed_argument_vector(
        self,
    ) -> None:
        _, cases = comparison_evidence._models()
        prefix = f"$ {comparison_evidence.PYTHON_DISPLAY} -m unitsentinel "
        for case in cases:
            with self.subTest(case=case.name):
                lines = comparison_evidence._command_lines(case)
                displayed: list[str] = []
                for index, line in enumerate(lines):
                    content = line
                    if index == 0:
                        self.assertTrue(content.startswith(prefix))
                        content = content[len(prefix) :]
                    else:
                        self.assertTrue(content.startswith("    "))
                        content = content[4:]
                    if content.endswith(" \\"):
                        content = content[:-2]
                    displayed.extend(content.split(" "))
                self.assertEqual(
                    tuple(displayed),
                    comparison_evidence._arguments(
                        case,
                        machine_readable=False,
                    ),
                )

    def test_text_validator_rejects_missing_or_additional_stdout(
        self,
    ) -> None:
        training, cases = comparison_evidence._models()
        case = cases[0]
        result = decode_comparison_result(
            comparison_evidence.CLAIM_PATHS[case.name].read_bytes()
        )
        expected = comparison_evidence._expected_text_output(
            case=case,
            training=training,
            result=result,
        )
        lines = expected.splitlines(keepends=True)
        for tampered in (
            b"".join(lines[:-2] + lines[-1:]),
            expected + b"HOME=/home/private\n",
        ):
            with (
                self.subTest(tampered_bytes=len(tampered)),
                self.assertRaisesRegex(EvidenceError, "exact bound report"),
            ):
                comparison_evidence._validate_text_output(
                    tampered,
                    case=case,
                    training=training,
                    result=result,
                )

    def test_artifact_data_reports_only_exact_file_sizes(self) -> None:
        value = canonical_document(
            comparison_evidence.DATA_PATH,
            transport_newline=True,
        )
        self.assertIs(type(value), dict)
        data = cast(dict[str, Any], value)
        self.assertEqual(
            set(data),
            {"artifacts", "measurement_scope", "schema"},
        )
        self.assertEqual(
            data["measurement_scope"],
            ("exact committed artifact byte lengths; no latency or performance claim"),
        )
        rows = data["artifacts"]
        self.assertIs(type(rows), list)
        self.assertEqual(
            [row["name"] for row in rows],
            ["compatible", "drift", "indeterminate"],
        )
        for row_value in rows:
            row = cast(dict[str, Any], row_value)
            name = cast(str, row["name"])
            self.assertEqual(
                row["capture_json_bytes"],
                comparison_evidence.CAPTURE_JSON_PATHS[name].stat().st_size,
            )
            self.assertEqual(
                row["capture_text_bytes"],
                comparison_evidence.CAPTURE_TEXT_PATHS[name].stat().st_size,
            )
            claim = comparison_evidence.CLAIM_PATHS[name].read_bytes()
            self.assertEqual(row["claim_bytes"], len(claim))
            self.assertEqual(row["claim_sha256"], sha256(claim))
            self.assertNotIn("elapsed", row)
            self.assertNotIn("latency", row)

    def test_provenance_binds_every_capture_fixture_and_semantic_digest(
        self,
    ) -> None:
        value = canonical_document(
            comparison_evidence.PROVENANCE_PATH,
            transport_newline=True,
        )
        self.assertIs(type(value), dict)
        provenance = cast(dict[str, Any], value)
        self.assertEqual(
            set(provenance),
            {"cases", "limits", "registry", "schema", "tool", "trust"},
        )
        self.assertEqual(
            provenance["schema"],
            "unitsentinel.comparison-evidence-provenance/v1",
        )
        self.assertEqual(
            provenance["registry"]["sha256"],
            BUILTIN_REGISTRY.digest,
        )
        self.assertEqual(
            provenance["limits"],
            comparison_evidence.COMPARISON_LIMITS.canonical_record(),
        )
        cases = provenance["cases"]
        self.assertEqual(
            [item["name"] for item in cases],
            ["compatible", "drift", "indeterminate"],
        )
        for item_value in cases:
            item = cast(dict[str, Any], item_value)
            name = cast(str, item["name"])
            self.assertEqual(item["graphs"]["training"]["graph_id"], "ratio-training")
            for mode in ("json", "text"):
                capture_path = (
                    comparison_evidence.CAPTURE_JSON_PATHS[name]
                    if mode == "json"
                    else comparison_evidence.CAPTURE_TEXT_PATHS[name]
                )
                self.assertEqual(
                    item["captures"][mode]["sha256"],
                    sha256(capture_path.read_bytes()),
                )
            claim = comparison_evidence.CLAIM_PATHS[name].read_bytes()
            self.assertEqual(item["claim"]["sha256"], sha256(claim))

        compatible = cases[0]
        self.assertNotEqual(
            compatible["normalization_lineage"]["training_sha256"],
            compatible["normalization_lineage"]["serving_sha256"],
        )
        self.assertEqual(
            compatible["output_normalization"]["training_sha256"],
            compatible["output_normalization"]["serving_sha256"],
        )
        drift = cases[1]
        self.assertNotEqual(
            drift["output_normalization"]["training_sha256"],
            drift["output_normalization"]["serving_sha256"],
        )
        indeterminate = cases[2]
        self.assertEqual(
            indeterminate["output_normalization"],
            {"serving_sha256": None, "training_sha256": None},
        )

    def test_validator_rejects_tampered_stdout_binding(self) -> None:
        training, cases = comparison_evidence._models()
        case = cases[0]
        capture = canonical_document(
            comparison_evidence.CAPTURE_JSON_PATHS[case.name],
            transport_newline=True,
        )
        self.assertIs(type(capture), dict)
        tampered = copy.deepcopy(capture)
        tampered["plan"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(EvidenceError, "plan binding"):
            comparison_evidence._validate_capture(
                tampered,
                case=case,
                training=training,
                claim_payload=comparison_evidence.CLAIM_PATHS[case.name].read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
