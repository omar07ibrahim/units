from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from unitsentinel import cli
from unitsentinel.canonical import canonical_json_bytes, sha256_hex
from unitsentinel.comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_RESULT_SCHEMA,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonError,
    ComparisonPolicy,
    ComparisonReason,
    ComparisonStatus,
    MismatchCode,
    compare_graphs,
)
from unitsentinel.comparison_codec import encode_comparison_plan
from unitsentinel.comparison_contract import (
    COMPARISON_SCHEMA,
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from unitsentinel.comparison_result_codec import (
    ComparisonResultDecodeError,
    decode_comparison_result,
)
from unitsentinel.domain import UnitSentinelError
from unitsentinel.graph import (
    GRAPH_SCHEMA,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.graph_codec import decode_graph, encode_graph
from unitsentinel.registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA
from unitsentinel.verification import SolverLimits, VerificationStatus
from unitsentinel.version import VERSION


def ratio_graph(
    graph_id: str,
    *,
    left_id: str,
    right_id: str,
    output_id: str,
    reversed_divide: bool = False,
    annotated: bool = True,
) -> ComputationGraph:
    operands = (right_id, left_id) if reversed_divide else (left_id, right_id)
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(
            sorted(
                (
                    ValueSpec(
                        left_id,
                        ScalarType.FLOAT32,
                        ("batch",),
                        "meter" if annotated else None,
                    ),
                    ValueSpec(
                        right_id,
                        ScalarType.FLOAT32,
                        ("batch",),
                        "meter" if annotated else None,
                    ),
                    ValueSpec(
                        output_id,
                        ScalarType.FLOAT32,
                        ("batch",),
                        "one" if annotated else None,
                    ),
                ),
                key=lambda value: value.value_id,
            )
        ),
        inputs=(left_id, right_id),
        nodes=(
            Node(
                node_id="normalize-ratio",
                operation=Operation.DIVIDE,
                inputs=operands,
                output=output_id,
            ),
        ),
        outputs=(output_id,),
    )


def ratio_plan(
    comparison_id: str,
    training: ComputationGraph,
    serving: ComputationGraph,
    *,
    registry_digest: str = BUILTIN_REGISTRY.digest,
) -> ComparisonPlan:
    return ComparisonPlan(
        comparison_id=comparison_id,
        training_graph_digest=training.digest,
        serving_graph_digest=serving.digest,
        registry_digest=registry_digest,
        bindings=(
            ContractBinding(
                "input-feature",
                InterfaceEndpoint(InterfaceRole.INPUT, "feature-distance"),
                InterfaceEndpoint(InterfaceRole.INPUT, "request-distance"),
            ),
            ContractBinding(
                "input-reference",
                InterfaceEndpoint(InterfaceRole.INPUT, "reference-distance"),
                InterfaceEndpoint(InterfaceRole.INPUT, "calibration-distance"),
            ),
            ContractBinding(
                "output-normalized",
                InterfaceEndpoint(InterfaceRole.OUTPUT, "normalized-score"),
                InterfaceEndpoint(InterfaceRole.OUTPUT, "serving-score"),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class RatioCase:
    name: str
    serving: ComputationGraph
    plan: ComparisonPlan
    status: ComparisonStatus
    reason: ComparisonReason | None
    exit_code: int


@dataclass(frozen=True, slots=True)
class CasePaths:
    plan: Path
    serving: Path


class CompareCLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

        self.training = ratio_graph(
            "ratio-training",
            left_id="feature-distance",
            right_id="reference-distance",
            output_id="normalized-score",
        )
        compatible_serving = ratio_graph(
            "ratio-serving-renamed",
            left_id="request-distance",
            right_id="calibration-distance",
            output_id="serving-score",
        )
        drift_serving = ratio_graph(
            "ratio-serving-reversed",
            left_id="request-distance",
            right_id="calibration-distance",
            output_id="serving-score",
            reversed_divide=True,
        )
        indeterminate_serving = ratio_graph(
            "ratio-serving-underconstrained",
            left_id="request-distance",
            right_id="calibration-distance",
            output_id="serving-score",
            annotated=False,
        )
        self.cases = {
            case.name: case
            for case in (
                RatioCase(
                    "compatible",
                    compatible_serving,
                    ratio_plan(
                        "ratio-compatible",
                        self.training,
                        compatible_serving,
                    ),
                    ComparisonStatus.COMPATIBLE,
                    None,
                    cli.EXIT_SUCCESS,
                ),
                RatioCase(
                    "drift",
                    drift_serving,
                    ratio_plan(
                        "ratio-drift",
                        self.training,
                        drift_serving,
                    ),
                    ComparisonStatus.DRIFT,
                    None,
                    cli.EXIT_MISMATCH,
                ),
                RatioCase(
                    "indeterminate",
                    indeterminate_serving,
                    ratio_plan(
                        "ratio-indeterminate",
                        self.training,
                        indeterminate_serving,
                    ),
                    ComparisonStatus.INDETERMINATE,
                    ComparisonReason.SERVING_NOT_VERIFIED,
                    cli.EXIT_INDETERMINATE,
                ),
            )
        }

        self.training_path = self.directory / "training.json"
        self.training_path.write_bytes(encode_graph(self.training))
        self.paths: dict[str, CasePaths] = {}
        for case in self.cases.values():
            plan_path = self.directory / f"{case.name}.plan.json"
            serving_path = self.directory / f"{case.name}.serving.json"
            plan_path.write_bytes(encode_comparison_plan(case.plan))
            serving_path.write_bytes(encode_graph(case.serving))
            self.paths[case.name] = CasePaths(plan_path, serving_path)

    def arguments(
        self,
        case_name: str,
        *extra: str,
        expected_plan_digest: str | None = None,
    ) -> tuple[str, ...]:
        case = self.cases[case_name]
        paths = self.paths[case_name]
        return (
            "compare",
            str(paths.plan),
            "--training-graph",
            str(self.training_path),
            "--serving-graph",
            str(paths.serving),
            "--expect-plan-sha256",
            case.plan.digest if expected_plan_digest is None else expected_plan_digest,
            *extra,
        )

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()


class CompareOutcomeTests(CompareCLITestCase):
    def test_real_ratio_outcomes_have_stable_text_and_exit_contracts(self) -> None:
        for case in self.cases.values():
            with self.subTest(case=case.name):
                exit_code, stdout, stderr = self.invoke(*self.arguments(case.name))

                self.assertEqual(exit_code, case.exit_code)
                self.assertEqual(stderr, "")
                self.assertTrue(stdout.endswith("\n"))
                self.assertIn(
                    f"UnitSentinel comparison: {case.status.value.upper()}\n",
                    stdout,
                )
                self.assertIn(f"exit code: {case.exit_code}\n", stdout)
                self.assertIn(f"tool: unitsentinel {VERSION}\n", stdout)
                self.assertIn(
                    "reason: "
                    + ("none" if case.reason is None else case.reason.value)
                    + "\n",
                    stdout,
                )
                self.assertIn(f"plan sha256: {case.plan.digest}\n", stdout)
                self.assertIn(
                    f"expected plan sha256: {case.plan.digest}\n",
                    stdout,
                )
                self.assertIn("plan authentication: not-provided\n", stdout)
                self.assertIn("result authentication: not-provided\n", stdout)
                self.assertIn("result scope: under-plan\n", stdout)
                self.assertIn(
                    "solver limits per graph side: "
                    "check=250ms total=5000ms memory=256MiB "
                    "core-shrink=64 uniqueness=577\n",
                    stdout,
                )
                self.assertIn("comparison result output: not-requested\n", stdout)

                if case.status is ComparisonStatus.INDETERMINATE:
                    self.assertIn(
                        "serving verification: underconstrained ",
                        stdout,
                    )
                    self.assertIn(
                        "bindings (0, mismatches=0):\n",
                        stdout,
                    )
                    self.assertNotIn("  output-normalized |", stdout)
                else:
                    self.assertIn("bindings (3, mismatches=", stdout)
                    self.assertIn(
                        "input:feature-distance@0 -> input:request-distance@0",
                        stdout,
                    )
                    self.assertIn(
                        "output:normalized-score@0 -> output:serving-score@0",
                        stdout,
                    )

                if case.status is ComparisonStatus.DRIFT:
                    self.assertIn(
                        MismatchCode.NORMALIZATION_LINEAGE_DRIFT.value,
                        stdout,
                    )
                    self.assertIn("mismatches=1", stdout)
                elif case.status is ComparisonStatus.COMPATIBLE:
                    self.assertIn("mismatches=0", stdout)

    def test_json_envelopes_are_canonical_self_describing_and_bound(self) -> None:
        for case in self.cases.values():
            with self.subTest(case=case.name):
                exit_code, stdout, stderr = self.invoke(
                    *self.arguments(case.name, "--json")
                )

                record = json.loads(stdout)
                result_record = record["result"]["record"]
                self.assertEqual(exit_code, case.exit_code)
                self.assertEqual(stderr, "")
                self.assertEqual(
                    stdout,
                    canonical_json_bytes(record).decode("utf-8") + "\n",
                )
                self.assertEqual(record["schema"], cli.COMPARE_OUTPUT_SCHEMA)
                self.assertEqual(record["exit_code"], case.exit_code)
                self.assertEqual(
                    record["tool"],
                    {"name": "unitsentinel", "version": VERSION},
                )
                self.assertEqual(
                    record["plan"],
                    {
                        "authentication": AUTHENTICATION_NOT_PROVIDED,
                        "expected_sha256": case.plan.digest,
                        "schema": COMPARISON_SCHEMA,
                        "sha256": case.plan.digest,
                    },
                )
                self.assertEqual(
                    record["graphs"],
                    {
                        "serving": {
                            "graph_id": case.serving.graph_id,
                            "schema": GRAPH_SCHEMA,
                            "sha256": case.serving.digest,
                        },
                        "training": {
                            "graph_id": self.training.graph_id,
                            "schema": GRAPH_SCHEMA,
                            "sha256": self.training.digest,
                        },
                    },
                )
                self.assertEqual(
                    record["registry"],
                    {
                        "schema": REGISTRY_SCHEMA,
                        "sha256": BUILTIN_REGISTRY.digest,
                        "version": BUILTIN_REGISTRY.version,
                    },
                )
                self.assertEqual(record["result_output"], "not-requested")
                self.assertEqual(
                    record["result"]["authentication"],
                    AUTHENTICATION_NOT_PROVIDED,
                )
                self.assertEqual(
                    record["result"]["schema"],
                    COMPARISON_RESULT_SCHEMA,
                )
                self.assertEqual(
                    record["result"]["sha256"],
                    sha256_hex(canonical_json_bytes(result_record)),
                )
                self.assertEqual(result_record["status"], case.status.value)
                self.assertEqual(
                    result_record["reason"],
                    None if case.reason is None else case.reason.value,
                )
                self.assertEqual(
                    result_record["scope"],
                    COMPARISON_SCOPE_UNDER_PLAN,
                )
                self.assertEqual(
                    result_record["authentication"],
                    AUTHENTICATION_NOT_PROVIDED,
                )
                self.assertEqual(
                    result_record["limits"],
                    SolverLimits().canonical_record(),
                )

    def test_drift_is_only_ordered_normalization_lineage_drift(self) -> None:
        exit_code, stdout, stderr = self.invoke(*self.arguments("drift", "--json"))

        record = json.loads(stdout)
        bindings = {
            binding["contract_id"]: binding
            for binding in record["result"]["record"]["bindings"]
        }
        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stderr, "")
        self.assertEqual(bindings["input-feature"]["mismatches"], [])
        self.assertEqual(bindings["input-reference"]["mismatches"], [])
        self.assertEqual(
            bindings["output-normalized"]["mismatches"],
            [MismatchCode.NORMALIZATION_LINEAGE_DRIFT.value],
        )
        self.assertNotEqual(
            bindings["output-normalized"]["normalization"]["training_sha256"],
            bindings["output-normalized"]["normalization"]["serving_sha256"],
        )

    def test_indeterminate_result_does_not_claim_partial_bindings(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            *self.arguments("indeterminate", "--json")
        )

        result = json.loads(stdout)["result"]["record"]
        self.assertEqual(exit_code, cli.EXIT_INDETERMINATE)
        self.assertEqual(stderr, "")
        self.assertEqual(result["reason"], ComparisonReason.SERVING_NOT_VERIFIED.value)
        self.assertEqual(result["bindings"], [])
        self.assertIsNone(result["normalization_lineage"]["training"])
        self.assertIsNone(result["normalization_lineage"]["serving"])
        self.assertEqual(
            result["verification"]["training"]["record"]["status"],
            VerificationStatus.VERIFIED.value,
        )
        self.assertEqual(
            result["verification"]["serving"]["record"]["status"],
            VerificationStatus.UNDERCONSTRAINED.value,
        )

    def test_one_sided_public_inputs_render_absence_and_exact_drift_codes(
        self,
    ) -> None:
        compatible = self.cases["compatible"]
        training_only_input = ValueSpec(
            "training-only-input",
            ScalarType.FLOAT32,
            ("batch",),
            "meter",
        )
        training_only_output = ValueSpec(
            "training-only-output",
            ScalarType.FLOAT32,
            ("batch",),
            "meter",
        )
        serving_only_input = ValueSpec(
            "serving-only-input",
            ScalarType.FLOAT32,
            ("batch",),
            "meter",
        )
        serving_only_output = ValueSpec(
            "serving-only-output",
            ScalarType.FLOAT32,
            ("batch",),
            "meter",
        )
        training = ComputationGraph(
            graph_id="ratio-training-with-extra",
            values=tuple(
                sorted(
                    (
                        *self.training.values,
                        training_only_input,
                        training_only_output,
                    ),
                    key=lambda value: value.value_id,
                )
            ),
            inputs=(*self.training.inputs, training_only_input.value_id),
            nodes=(
                *self.training.nodes,
                Node(
                    node_id="project-training-only",
                    operation=Operation.IDENTITY,
                    inputs=(training_only_input.value_id,),
                    output=training_only_output.value_id,
                ),
            ),
            outputs=(*self.training.outputs, training_only_output.value_id),
        )
        serving = ComputationGraph(
            graph_id="ratio-serving-with-extra",
            values=tuple(
                sorted(
                    (
                        *compatible.serving.values,
                        serving_only_input,
                        serving_only_output,
                    ),
                    key=lambda value: value.value_id,
                )
            ),
            inputs=(*compatible.serving.inputs, serving_only_input.value_id),
            nodes=(
                *compatible.serving.nodes,
                Node(
                    node_id="project-serving-only",
                    operation=Operation.IDENTITY,
                    inputs=(serving_only_input.value_id,),
                    output=serving_only_output.value_id,
                ),
            ),
            outputs=(*compatible.serving.outputs, serving_only_output.value_id),
        )
        base_bindings = ratio_plan("unused", training, serving).bindings
        plan = ComparisonPlan(
            comparison_id="one-sided-ratio-inputs",
            training_graph_digest=training.digest,
            serving_graph_digest=serving.digest,
            registry_digest=BUILTIN_REGISTRY.digest,
            bindings=tuple(
                sorted(
                    (
                        *base_bindings,
                        ContractBinding(
                            "extra-input-in-serving",
                            None,
                            InterfaceEndpoint(
                                InterfaceRole.INPUT,
                                serving_only_input.value_id,
                            ),
                        ),
                        ContractBinding(
                            "extra-output-in-serving",
                            None,
                            InterfaceEndpoint(
                                InterfaceRole.OUTPUT,
                                serving_only_output.value_id,
                            ),
                        ),
                        ContractBinding(
                            "missing-input-in-serving",
                            InterfaceEndpoint(
                                InterfaceRole.INPUT,
                                training_only_input.value_id,
                            ),
                            None,
                        ),
                        ContractBinding(
                            "missing-output-in-serving",
                            InterfaceEndpoint(
                                InterfaceRole.OUTPUT,
                                training_only_output.value_id,
                            ),
                            None,
                        ),
                    ),
                    key=lambda binding: binding.contract_id,
                )
            ),
        )
        training_path = self.directory / "one-sided.training.json"
        serving_path = self.directory / "one-sided.serving.json"
        plan_path = self.directory / "one-sided.plan.json"
        training_path.write_bytes(encode_graph(training))
        serving_path.write_bytes(encode_graph(serving))
        plan_path.write_bytes(encode_comparison_plan(plan))

        exit_code, stdout, stderr = self.invoke(
            "compare",
            str(plan_path),
            "--training-graph",
            str(training_path),
            "--serving-graph",
            str(serving_path),
            "--expect-plan-sha256",
            plan.digest,
        )

        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stderr, "")
        self.assertIn(
            "extra-input-in-serving | absent -> input:serving-only-input@2 | "
            "extra-in-serving\n",
            stdout,
        )
        self.assertIn(
            "missing-input-in-serving | input:training-only-input@2 -> absent | "
            "missing-in-serving\n",
            stdout,
        )
        self.assertIn("bindings (7, mismatches=4):\n", stdout)


class CompareArgumentContractTests(CompareCLITestCase):
    def test_help_lists_command_required_pin_and_every_solver_bound(self) -> None:
        cases = (
            (("--help",), ("compare",)),
            (
                ("compare", "--help"),
                (
                    "--training-graph",
                    "--serving-graph",
                    "--expect-plan-sha256",
                    "--per-check-timeout-ms",
                    "--total-timeout-ms",
                    "--max-memory-mb",
                    "--max-core-shrink-checks",
                    "--max-uniqueness-checks",
                    "--result",
                    "--json",
                ),
            ),
        )
        for arguments, expected_fragments in cases:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main(arguments)

            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(stderr.getvalue(), "")
            for fragment in expected_fragments:
                self.assertIn(fragment, stdout.getvalue())

    def test_missing_and_uppercase_plan_pins_fail_before_reads(self) -> None:
        complete = self.arguments("compatible")
        uppercase_digest = self.cases["compatible"].plan.digest.upper()
        self.assertNotEqual(uppercase_digest, self.cases["compatible"].plan.digest)
        cases = (
            complete[:-2],
            (*complete[:-1], uppercase_digest),
        )
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                patch.object(cli, "_read_bounded_file") as read_file,
            ):
                exit_code, stdout, stderr = self.invoke(*arguments)

                self.assertEqual(exit_code, cli.EXIT_USAGE)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    (
                        "unitsentinel: error: invalid command-line arguments; "
                        "use --help\n"
                    ),
                )
                read_file.assert_not_called()


class CompareResultPublicationTests(CompareCLITestCase):
    def test_raw_results_strictly_decode_and_bind_every_closed_outcome(self) -> None:
        for case in self.cases.values():
            with self.subTest(case=case.name):
                result_path = self.directory / f"{case.name}.result.json"
                exit_code, stdout, stderr = self.invoke(
                    *self.arguments(
                        case.name,
                        "--result",
                        str(result_path),
                        "--json",
                    )
                )

                payload = result_path.read_bytes()
                result = decode_comparison_result(payload)
                record = json.loads(stdout)
                self.assertEqual(exit_code, case.exit_code)
                self.assertEqual(stderr, "")
                self.assertFalse(payload.endswith(b"\n"))
                self.assertEqual(payload, result.canonical_bytes())
                self.assertEqual(
                    stat.S_IMODE(result_path.stat().st_mode),
                    0o600,
                )
                self.assertEqual(result.status, case.status)
                self.assertEqual(result.comparison_id, case.plan.comparison_id)
                self.assertEqual(result.plan_digest, case.plan.digest)
                self.assertEqual(result.training_graph_digest, self.training.digest)
                self.assertEqual(result.serving_graph_digest, case.serving.digest)
                self.assertEqual(result.registry_digest, BUILTIN_REGISTRY.digest)
                self.assertEqual(result.authentication, AUTHENTICATION_NOT_PROVIDED)
                self.assertEqual(result.scope, COMPARISON_SCOPE_UNDER_PLAN)
                self.assertEqual(record["result_output"], "written")
                self.assertEqual(record["result"]["record"], result.canonical_record())
                self.assertEqual(record["result"]["sha256"], result.digest)

    def test_existing_result_is_not_overwritten_and_suppresses_stdout(self) -> None:
        result_path = self.directory / "existing.result.json"
        result_path.write_bytes(b"sentinel-result")
        result_path.chmod(0o640)

        exit_code, stdout, stderr = self.invoke(
            *self.arguments(
                "compatible",
                "--result",
                str(result_path),
                "--json",
            )
        )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: comparison result output already exists\n",
        )
        self.assertEqual(result_path.read_bytes(), b"sentinel-result")
        self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o640)
        self.assertEqual(
            tuple(self.directory.glob(".unitsentinel-*.tmp")),
            (),
        )


class CompareLimitTests(CompareCLITestCase):
    def test_invalid_limits_are_usage_errors_before_any_file_read(self) -> None:
        invalid_flags = (
            ("--per-check-timeout-ms", "0"),
            ("--per-check-timeout-ms", "10001"),
            ("--per-check-timeout-ms", "01"),
            ("--total-timeout-ms", "0"),
            ("--total-timeout-ms", "60001"),
            ("--max-memory-mb", "31"),
            ("--max-memory-mb", "4097"),
            ("--max-core-shrink-checks", "-1"),
            ("--max-core-shrink-checks", "1025"),
            ("--max-uniqueness-checks", "0"),
            ("--max-uniqueness-checks", "1025"),
            (
                "--per-check-timeout-ms",
                "100",
                "--total-timeout-ms",
                "99",
            ),
        )
        for flags in invalid_flags:
            with (
                self.subTest(flags=flags),
                patch.object(cli, "_read_bounded_file") as read_file,
            ):
                exit_code, stdout, stderr = self.invoke(
                    *self.arguments("compatible", *flags)
                )

                self.assertEqual(exit_code, cli.EXIT_USAGE)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    (
                        "unitsentinel: error: invalid command-line arguments; "
                        "use --help\n"
                    ),
                )
                read_file.assert_not_called()

    def test_custom_limits_and_plan_pin_reach_the_engine_and_result(self) -> None:
        custom = SolverLimits(
            per_check_timeout_ms=19,
            total_timeout_ms=250,
            max_memory_mb=64,
            max_core_shrink_checks=0,
            max_uniqueness_checks=7,
        )
        with patch.object(cli, "compare_graphs", wraps=compare_graphs) as comparison:
            exit_code, stdout, stderr = self.invoke(
                *self.arguments(
                    "compatible",
                    "--per-check-timeout-ms",
                    str(custom.per_check_timeout_ms),
                    "--total-timeout-ms",
                    str(custom.total_timeout_ms),
                    "--max-memory-mb",
                    str(custom.max_memory_mb),
                    "--max-core-shrink-checks",
                    str(custom.max_core_shrink_checks),
                    "--max-uniqueness-checks",
                    str(custom.max_uniqueness_checks),
                    "--json",
                )
            )

        record = json.loads(stdout)
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        comparison.assert_called_once()
        self.assertEqual(comparison.call_args.kwargs["limits"], custom)
        self.assertEqual(
            comparison.call_args.kwargs["policy"].expected_plan_digest,
            self.cases["compatible"].plan.digest,
        )
        self.assertEqual(
            record["result"]["record"]["limits"],
            custom.canonical_record(),
        )


class CompareFailClosedInputTests(CompareCLITestCase):
    def test_plan_pin_mismatch_stops_before_decode_and_graph_reads(self) -> None:
        wrong_digest = "0" * 64
        self.assertNotEqual(wrong_digest, self.cases["compatible"].plan.digest)
        with (
            patch.object(cli, "decode_comparison_plan") as decode_plan,
            patch.object(cli, "_decode_graph_file") as decode_graph_file,
        ):
            exit_code, stdout, stderr = self.invoke(
                *self.arguments(
                    "compatible",
                    expected_plan_digest=wrong_digest,
                )
            )

        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: comparison plan sha256 does not match "
                "the expected digest\n"
            ),
        )
        decode_plan.assert_not_called()
        decode_graph_file.assert_not_called()

    def test_malformed_pinned_plan_stops_before_graph_reads(self) -> None:
        payload = b"{}"
        plan_path = self.directory / "malformed-plan.json"
        plan_path.write_bytes(payload)
        arguments = list(self.arguments("compatible"))
        arguments[1] = str(plan_path)
        arguments[-1] = sha256_hex(payload)

        with patch.object(cli, "_decode_graph_file") as decode_graph_file:
            exit_code, stdout, stderr = self.invoke(*arguments)

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertIn("comparison plan input is invalid", stderr)
        decode_graph_file.assert_not_called()

    def test_registry_mismatch_stops_before_graph_reads(self) -> None:
        wrong_registry = "0" * 64
        self.assertNotEqual(wrong_registry, BUILTIN_REGISTRY.digest)
        serving = self.cases["compatible"].serving
        plan = ratio_plan(
            "wrong-registry",
            self.training,
            serving,
            registry_digest=wrong_registry,
        )
        plan_path = self.directory / "wrong-registry.plan.json"
        plan_path.write_bytes(encode_comparison_plan(plan))
        arguments = list(self.arguments("compatible"))
        arguments[1] = str(plan_path)
        arguments[-1] = plan.digest

        with patch.object(cli, "_decode_graph_file") as decode_graph_file:
            exit_code, stdout, stderr = self.invoke(*arguments)

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: comparison plan registry does not match "
                "the current registry\n"
            ),
        )
        decode_graph_file.assert_not_called()

    def test_training_digest_mismatch_stops_before_decode_and_serving_read(
        self,
    ) -> None:
        self.training_path.write_bytes(encode_graph(self.cases["compatible"].serving))
        with (
            patch.object(
                cli,
                "_decode_graph_file",
                wraps=cli._decode_graph_file,
            ) as decode_graph_file,
            patch.object(cli, "decode_graph", wraps=decode_graph) as graph_decoder,
        ):
            exit_code, stdout, stderr = self.invoke(*self.arguments("compatible"))

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: training graph input sha256 does not "
                "match the comparison plan\n"
            ),
        )
        self.assertEqual(decode_graph_file.call_count, 1)
        self.assertEqual(
            decode_graph_file.call_args.kwargs["label"],
            "training graph input",
        )
        graph_decoder.assert_not_called()

    def test_serving_digest_mismatch_happens_after_only_training_decode(
        self,
    ) -> None:
        self.paths["compatible"].serving.write_bytes(
            encode_graph(self.cases["drift"].serving)
        )
        with (
            patch.object(
                cli,
                "_decode_graph_file",
                wraps=cli._decode_graph_file,
            ) as decode_graph_file,
            patch.object(cli, "decode_graph", wraps=decode_graph) as graph_decoder,
        ):
            exit_code, stdout, stderr = self.invoke(*self.arguments("compatible"))

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: serving graph input sha256 does not "
                "match the comparison plan\n"
            ),
        )
        self.assertEqual(decode_graph_file.call_count, 2)
        self.assertEqual(
            [call.kwargs["label"] for call in decode_graph_file.call_args_list],
            ["training graph input", "serving graph input"],
        )
        self.assertEqual(graph_decoder.call_count, 1)


class CompareRedactionTests(CompareCLITestCase):
    def test_engine_input_and_unexpected_failures_are_redacted(self) -> None:
        private = "/home/omar/private-comparison-plan"
        cases = (
            (
                ComparisonError(private),
                cli.EXIT_INPUT,
                "unitsentinel: error: comparison inputs are inconsistent\n",
            ),
            (
                UnitSentinelError(private),
                cli.EXIT_INTERNAL,
                ("unitsentinel: error: comparison could not be completed safely\n"),
            ),
            (
                RuntimeError(private),
                cli.EXIT_INTERNAL,
                "unitsentinel: error: internal failure\n",
            ),
        )
        for failure, expected_exit, expected_stderr in cases:
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(cli, "compare_graphs", side_effect=failure),
            ):
                exit_code, stdout, stderr = self.invoke(*self.arguments("compatible"))

            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, expected_stderr)
            self.assertNotIn("omar", stderr.lower())
            self.assertNotIn("private", stderr.lower())

    def test_wrong_result_and_encoding_failure_are_internal_and_redacted(
        self,
    ) -> None:
        result_path = self.directory / "must-not-exist.result.json"
        cases = (
            (
                patch.object(cli, "compare_graphs", return_value=object()),
                "comparison returned an unexpected result",
            ),
            (
                patch.object(
                    cli,
                    "encode_comparison_result",
                    side_effect=ComparisonResultDecodeError(
                        "private /home/omar transport"
                    ),
                ),
                "comparison result could not be encoded safely",
            ),
        )
        for failure_patch, public_message in cases:
            with self.subTest(public_message=public_message), failure_patch:
                exit_code, stdout, stderr = self.invoke(
                    *self.arguments(
                        "compatible",
                        "--result",
                        str(result_path),
                    )
                )

            self.assertEqual(exit_code, cli.EXIT_INTERNAL)
            self.assertEqual(stdout, "")
            self.assertEqual(
                stderr,
                f"unitsentinel: error: {public_message}\n",
            )
            self.assertNotIn("omar", stderr.lower())
            self.assertFalse(result_path.exists())

    def test_valid_result_bound_to_other_limits_is_rejected_before_output(
        self,
    ) -> None:
        case = self.cases["compatible"]
        alternate_limits = SolverLimits(
            per_check_timeout_ms=251,
            total_timeout_ms=5_000,
            max_memory_mb=256,
            max_core_shrink_checks=64,
            max_uniqueness_checks=577,
        )
        wrong_bound_result = compare_graphs(
            case.plan,
            training_graph=self.training,
            serving_graph=case.serving,
            limits=alternate_limits,
            policy=ComparisonPolicy(case.plan.digest),
        )
        result_path = self.directory / "wrong-bound.result.json"

        with patch.object(
            cli,
            "compare_graphs",
            return_value=wrong_bound_result,
        ):
            exit_code, stdout, stderr = self.invoke(
                *self.arguments(
                    "compatible",
                    "--result",
                    str(result_path),
                )
            )

        self.assertEqual(exit_code, cli.EXIT_INTERNAL)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            ("unitsentinel: error: comparison result does not bind the CLI request\n"),
        )
        self.assertFalse(result_path.exists())

    def test_encoded_transport_digest_mismatch_is_internal_before_publication(
        self,
    ) -> None:
        result_path = self.directory / "must-not-publish.result.json"
        with patch.object(
            cli,
            "encode_comparison_result",
            return_value=b"{}",
        ):
            exit_code, stdout, stderr = self.invoke(
                *self.arguments(
                    "compatible",
                    "--result",
                    str(result_path),
                    "--json",
                )
            )

        self.assertEqual(exit_code, cli.EXIT_INTERNAL)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            ("unitsentinel: error: comparison result digest could not be confirmed\n"),
        )
        self.assertFalse(result_path.exists())


if __name__ == "__main__":
    unittest.main()
