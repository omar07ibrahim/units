from __future__ import annotations

import unittest

import unitsentinel


class PackageIdentityTests(unittest.TestCase):
    def test_version_and_public_surface_are_explicit(self) -> None:
        self.assertEqual(unitsentinel.__version__, "0.1.0")
        self.assertEqual(
            unitsentinel.__all__,
            [
                "AMOUNT_OF_SUBSTANCE",
                "BUILTIN_REGISTRY",
                "DIMENSIONLESS",
                "ELECTRIC_CURRENT",
                "LENGTH",
                "LUMINOUS_INTENSITY",
                "MASS",
                "THERMODYNAMIC_TEMPERATURE",
                "TIME",
                "BaseDimension",
                "ComputationGraph",
                "ConstraintSource",
                "ConstraintWitness",
                "ConversionError",
                "Dimension",
                "DimensionError",
                "GraphDecodeError",
                "GraphError",
                "GraphValidationError",
                "InferredContract",
                "Node",
                "Operation",
                "Quantity",
                "QuantityKind",
                "RegistryError",
                "ScalarType",
                "SolverLimits",
                "Unit",
                "UnitAlias",
                "UnitDefinitionError",
                "UnitRegistry",
                "UnitSentinelError",
                "UnknownReason",
                "UnknownUnitError",
                "ValueSpec",
                "VerificationError",
                "VerificationResult",
                "VerificationStatus",
                "__version__",
                "decode_graph",
                "encode_graph",
            ],
        )


if __name__ == "__main__":
    unittest.main()
