from __future__ import annotations

import unittest
from importlib.metadata import version

import unitsentinel


class PackageIdentityTests(unittest.TestCase):
    def test_version_and_public_surface_are_explicit(self) -> None:
        self.assertEqual(unitsentinel.__version__, "0.1.0")
        self.assertEqual(version("unitsentinel"), unitsentinel.__version__)
        self.assertEqual(
            unitsentinel.__all__,
            [
                "AMOUNT_OF_SUBSTANCE",
                "BUILTIN_REGISTRY",
                "COMPARISON_SCHEMA",
                "DIMENSIONLESS",
                "ELECTRIC_CURRENT",
                "LENGTH",
                "LUMINOUS_INTENSITY",
                "MASS",
                "MAX_COMPARISON_BINDINGS",
                "THERMODYNAMIC_TEMPERATURE",
                "TIME",
                "BaseDimension",
                "CertificateDecodeError",
                "CertificateError",
                "CertificateReplay",
                "CertificateReplayError",
                "ComparisonContractError",
                "ComparisonDecodeError",
                "ComparisonPlan",
                "ComparisonValidationError",
                "ComputationGraph",
                "ConstraintSource",
                "ConstraintWitness",
                "ContractBinding",
                "ConversionError",
                "Dimension",
                "DimensionError",
                "GraphDecodeError",
                "GraphError",
                "GraphValidationError",
                "InferredContract",
                "InterfaceEndpoint",
                "InterfaceRole",
                "Node",
                "Operation",
                "ProofCertificate",
                "Quantity",
                "QuantityKind",
                "RegistryError",
                "RepairError",
                "RepairLimits",
                "RepairReason",
                "RepairStatus",
                "ReplayReason",
                "ReplayStatus",
                "ScalarType",
                "SolverLimits",
                "Unit",
                "UnitAlias",
                "UnitDefinitionError",
                "UnitRegistry",
                "UnitRepairCandidate",
                "UnitRepairResult",
                "UnitSentinelError",
                "UnknownReason",
                "UnknownUnitError",
                "ValueSpec",
                "VerificationError",
                "VerificationResult",
                "VerificationStatus",
                "__version__",
                "constraint_catalog",
                "create_certificate",
                "decode_certificate",
                "decode_comparison_plan",
                "decode_graph",
                "encode_certificate",
                "encode_comparison_plan",
                "encode_graph",
                "propose_unit_annotation_repair",
                "replay_certificate",
                "verify_graph",
            ],
        )


if __name__ == "__main__":
    unittest.main()
