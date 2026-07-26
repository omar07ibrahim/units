from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tools.evidence import repair_evidence
from tools.evidence.generate import EvidenceError
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.graph_codec import decode_graph
from unitsentinel.registry import BUILTIN_REGISTRY

ROOT = Path(__file__).resolve().parents[1]


def canonical_document(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if type(value) is not dict:
        raise AssertionError(f"{path.relative_to(ROOT)} is not an object")
    if payload != canonical_json_bytes(value) + b"\n":
        raise AssertionError(f"{path.relative_to(ROOT)} is not canonical JSON")
    return value


class RepairEvidenceTests(unittest.TestCase):
    def test_repair_generator_reproduces_committed_slice(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.evidence.repair_evidence",
                "--check",
            ],
            cwd=ROOT,
            env={
                "HOME": str(ROOT / ".unitsentinel" / "repair-test-home"),
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

    def test_repair_recorder_has_a_closed_output_allowlist(self) -> None:
        self.assertEqual(
            {
                path.relative_to(ROOT).as_posix()
                for path in repair_evidence.EXPECTED_OUTPUT_PATHS
            },
            {
                "docs/assets/unit-repair-lineage.svg",
                "docs/evidence/captures/repair.json",
                "docs/evidence/captures/repair.txt",
                "docs/evidence/repair-provenance.json",
            },
        )
        self.assertEqual(
            repair_evidence.REPAIR_ARGUMENTS,
            (
                "repair",
                "docs/evidence/contracts/wheel-anomaly-conflict.json",
                "--max-sites",
                "1",
                "--max-candidates",
                "1",
                "--max-verifier-calls",
                "3",
                "--max-work-items",
                "64",
                "--total-timeout-ms",
                "30000",
            ),
        )

    def test_repair_capture_is_canonical_and_cross_bound(self) -> None:
        capture = canonical_document(repair_evidence.CAPTURE_JSON_PATH)
        source = decode_graph(repair_evidence.SOURCE_GRAPH_PATH.read_bytes())
        validated = repair_evidence._validate_record(
            capture,
            source_graph=source,
        )
        self.assertEqual(validated, capture)
        self.assertEqual(capture["application"], "not-performed")

        report_envelope = capture["report"]
        self.assertIs(type(report_envelope), dict)
        report = report_envelope["record"]
        self.assertEqual(report["status"], "proposed")
        self.assertEqual(report["sites_considered"], 1)
        self.assertEqual(report["candidates_considered"], 1)
        self.assertEqual(report["verification_calls"], 3)

        proposal = capture["proposal"]
        candidate = report["candidate"]
        self.assertEqual(
            candidate["constraint_id"],
            "declaration/acceleration-si/unit",
        )
        self.assertEqual(candidate["previous_unit_id"], "meter-per-second")
        self.assertEqual(
            candidate["replacement_unit_id"],
            "meter-per-second-squared",
        )
        self.assertEqual(
            proposal["candidate_sha256"],
            hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        )

        relaxed = decode_graph(
            canonical_json_bytes(proposal["relaxed_graph"]["record"])
        )
        repaired = decode_graph(
            canonical_json_bytes(proposal["repaired_graph"]["record"])
        )
        self.assertEqual(source.value("acceleration-si").unit_id, "meter-per-second")
        self.assertIsNone(relaxed.value("acceleration-si").unit_id)
        self.assertEqual(
            repaired.value("acceleration-si").unit_id,
            "meter-per-second-squared",
        )
        self.assertEqual(source.graph_id, repaired.graph_id)
        self.assertNotEqual(
            repaired.graph_id,
            "wheel-anomaly-verified",
        )

    def test_repair_transcript_contains_exact_cli_stdout(self) -> None:
        capture = repair_evidence.CAPTURE_JSON_PATH.read_bytes()
        transcript = repair_evidence.CAPTURE_TEXT_PATH.read_bytes()
        expected = (
            "\n".join(repair_evidence.TRANSCRIPT_COMMAND_LINES).encode("utf-8")
            + b"\n"
            + capture
            + b"[exit 0]\n"
        )
        self.assertEqual(transcript, expected)

    def test_repair_provenance_binds_every_lineage_stage(self) -> None:
        capture = canonical_document(repair_evidence.CAPTURE_JSON_PATH)
        provenance = canonical_document(repair_evidence.PROVENANCE_PATH)
        self.assertEqual(
            provenance["schema"],
            "unitsentinel.repair-evidence-provenance/v1",
        )
        self.assertEqual(provenance["application"], "not-performed")
        self.assertEqual(
            provenance["capture"]["record_sha256"],
            hashlib.sha256(canonical_json_bytes(capture)).hexdigest(),
        )
        self.assertEqual(
            provenance["registry"]["sha256"],
            BUILTIN_REGISTRY.digest,
        )
        self.assertEqual(
            provenance["graphs"]["source"]["sha256"],
            capture["graph"]["sha256"],
        )
        self.assertEqual(
            provenance["graphs"]["relaxed"]["sha256"],
            capture["proposal"]["relaxed_graph"]["sha256"],
        )
        self.assertEqual(
            provenance["graphs"]["repaired"]["sha256"],
            capture["proposal"]["repaired_graph"]["sha256"],
        )
        self.assertEqual(
            provenance["report"]["sha256"],
            capture["report"]["sha256"],
        )
        self.assertEqual(
            provenance["candidate"]["sha256"],
            capture["proposal"]["candidate_sha256"],
        )

    def test_repair_validator_rejects_a_misleading_application_claim(self) -> None:
        capture = canonical_document(repair_evidence.CAPTURE_JSON_PATH)
        tampered = copy.deepcopy(capture)
        tampered["application"] = "performed"
        source = decode_graph(repair_evidence.SOURCE_GRAPH_PATH.read_bytes())
        with self.assertRaisesRegex(
            EvidenceError,
            "non-applied proposal",
        ):
            repair_evidence._validate_record(
                tampered,
                source_graph=source,
            )


if __name__ == "__main__":
    unittest.main()
