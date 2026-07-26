from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction

from unitsentinel.domain import (
    LENGTH,
    MAX_UNIT_ID_LENGTH,
    Quantity,
    Unit,
)
from unitsentinel.registry import (
    BUILTIN_REGISTRY,
    MAX_REGISTRY_ALIASES,
    MAX_REGISTRY_UNITS,
    REGISTRY_SCHEMA,
    RegistryError,
    UnitAlias,
    UnitRegistry,
    UnknownUnitError,
)


def meter() -> Unit:
    return Unit("meter", "m", LENGTH, Fraction(1))


def kilometer() -> Unit:
    return Unit("kilometer", "km", LENGTH, Fraction(1_000))


def small_registry(
    *,
    aliases: tuple[UnitAlias, ...] = (UnitAlias("metre", "meter"),),
) -> UnitRegistry:
    return UnitRegistry(
        version="1.0.0",
        units=(kilometer(), meter()),
        aliases=aliases,
    )


class BuiltinRegistryTests(unittest.TestCase):
    def test_snapshot_identity_is_pinned_and_canonical(self) -> None:
        self.assertEqual(BUILTIN_REGISTRY.version, "1.0.0")
        self.assertEqual(len(BUILTIN_REGISTRY.units), 33)
        self.assertEqual(len(BUILTIN_REGISTRY.aliases), 10)
        self.assertEqual(
            BUILTIN_REGISTRY.digest,
            "fc80cbb596f3341b1d2ff13795e50d2d1e05c792b34f24804afc97c3470913e5",
        )

        encoded = BUILTIN_REGISTRY.canonical_bytes()
        self.assertFalse(encoded.endswith(b"\n"))
        self.assertIn("°C".encode(), encoded)
        self.assertNotIn(b"\\u00b0", encoded)
        self.assertEqual(
            json.loads(encoded),
            BUILTIN_REGISTRY.canonical_record(),
        )
        self.assertEqual(
            BUILTIN_REGISTRY.canonical_record()["schema"],
            REGISTRY_SCHEMA,
        )
        next_version = UnitRegistry(
            "1.0.1",
            BUILTIN_REGISTRY.units,
            BUILTIN_REGISTRY.aliases,
        )
        self.assertNotEqual(next_version.digest, BUILTIN_REGISTRY.digest)

    def test_canonical_and_alias_lookup_are_explicit(self) -> None:
        self.assertEqual(BUILTIN_REGISTRY.resolve("meter").unit_id, "meter")
        self.assertEqual(BUILTIN_REGISTRY.resolve("metre").unit_id, "meter")
        self.assertEqual(
            BUILTIN_REGISTRY.resolve("celsius").unit_id,
            "degree-celsius",
        )

        with self.assertRaisesRegex(UnknownUnitError, "not present"):
            BUILTIN_REGISTRY.resolve("m")
        with self.assertRaisesRegex(RegistryError, "not canonical"):
            BUILTIN_REGISTRY.resolve("°C")
        with self.assertRaisesRegex(RegistryError, "not canonical"):
            BUILTIN_REGISTRY.resolve(1)  # type: ignore[arg-type]

    def test_exact_engineering_and_temperature_conversions(self) -> None:
        speed = Quantity(
            Fraction(90),
            BUILTIN_REGISTRY.resolve("kilometer-per-hour"),
        )
        freezing = Quantity(Fraction(32), BUILTIN_REGISTRY.resolve("fahrenheit"))

        self.assertEqual(
            speed.to(BUILTIN_REGISTRY.resolve("meter-per-second")).magnitude,
            Fraction(25),
        )
        self.assertEqual(
            freezing.to(BUILTIN_REGISTRY.resolve("degree-celsius")).magnitude,
            Fraction(0),
        )

    def test_conversion_targets_do_not_cross_quantity_kinds(self) -> None:
        absolute_ids = tuple(
            unit.unit_id for unit in BUILTIN_REGISTRY.conversion_targets("celsius")
        )
        delta_ids = tuple(
            unit.unit_id
            for unit in BUILTIN_REGISTRY.conversion_targets("celsius-delta")
        )

        self.assertEqual(
            absolute_ids,
            ("degree-celsius", "degree-fahrenheit", "kelvin"),
        )
        self.assertEqual(
            delta_ids,
            ("delta-celsius", "delta-fahrenheit", "delta-kelvin"),
        )


class RegistryValidationTests(unittest.TestCase):
    def test_registry_requires_sorted_unique_units_and_symbols(self) -> None:
        cases = (
            (
                (meter(), kilometer()),
                "sorted",
            ),
            (
                (meter(), meter()),
                "identifiers must be unique",
            ),
            (
                (
                    meter(),
                    Unit("metre", "m", LENGTH, Fraction(1)),
                ),
                "symbols must be unique",
            ),
        )
        for units, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(RegistryError, message),
            ):
                UnitRegistry("1.0.0", units)

    def test_aliases_are_direct_sorted_and_noncolliding(self) -> None:
        cases = (
            (
                (UnitAlias("yard", "meter"), UnitAlias("metre", "meter")),
                "sorted",
            ),
            (
                (UnitAlias("metre", "meter"), UnitAlias("metre", "kilometer")),
                "identifiers must be unique",
            ),
            (
                (UnitAlias("meter", "kilometer"),),
                "collides",
            ),
            (
                (UnitAlias("yard", "missing"),),
                "target is not",
            ),
        )
        for aliases, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(RegistryError, message),
            ):
                UnitRegistry(
                    "1.0.0",
                    (kilometer(), meter()),
                    aliases,
                )

        with self.assertRaisesRegex(RegistryError, "point to itself"):
            UnitAlias("meter", "meter")

    def test_registry_bounds_and_exact_container_types_fail_closed(self) -> None:
        with self.assertRaisesRegex(RegistryError, "canonical SemVer"):
            UnitRegistry("01.0.0", (meter(),))
        with self.assertRaisesRegex(RegistryError, "canonical SemVer"):
            UnitRegistry("1.0", (meter(),))
        with self.assertRaisesRegex(RegistryError, "units must be a tuple"):
            UnitRegistry("1.0.0", [meter()])  # type: ignore[arg-type]
        with self.assertRaisesRegex(RegistryError, "at least one"):
            UnitRegistry("1.0.0", ())
        with self.assertRaisesRegex(RegistryError, "too many units"):
            UnitRegistry("1.0.0", (meter(),) * (MAX_REGISTRY_UNITS + 1))
        with self.assertRaisesRegex(RegistryError, "aliases must be a tuple"):
            UnitRegistry("1.0.0", (meter(),), [])  # type: ignore[arg-type]
        with self.assertRaisesRegex(RegistryError, "exact UnitAlias"):
            UnitRegistry("1.0.0", (meter(),), (object(),))  # type: ignore[arg-type]
        with self.assertRaisesRegex(RegistryError, "too many aliases"):
            UnitRegistry(
                "1.0.0",
                (meter(),),
                (UnitAlias("metre", "meter"),) * (MAX_REGISTRY_ALIASES + 1),
            )

    def test_alias_and_lookup_identifier_bounds_are_shared(self) -> None:
        oversized = "a" * (MAX_UNIT_ID_LENGTH + 1)
        with self.assertRaisesRegex(RegistryError, "not canonical"):
            UnitAlias(oversized, "meter")
        with self.assertRaisesRegex(RegistryError, "not canonical"):
            small_registry().resolve(oversized)

    def test_nested_valid_mutation_invalidates_the_snapshot_digest(self) -> None:
        registry = small_registry()
        object.__setattr__(registry.units[0], "scale", Fraction(999))

        with self.assertRaisesRegex(RegistryError, "does not match"):
            registry.resolve("kilometer")

    def test_digest_tampering_and_low_level_container_mutation_fail_closed(
        self,
    ) -> None:
        registry = small_registry()
        object.__setattr__(registry, "_digest", "0" * 64)
        with self.assertRaisesRegex(RegistryError, "does not match"):
            registry.canonical_bytes()

        registry = small_registry()
        object.__setattr__(registry, "_digest", "not-a-digest")
        with self.assertRaisesRegex(RegistryError, "digest is malformed"):
            registry.canonical_record()

        registry = small_registry()
        object.__setattr__(registry, "aliases", [])
        with self.assertRaisesRegex(RegistryError, "aliases must be a tuple"):
            registry.canonical_record()

    def test_registry_values_and_receivers_are_exact_and_immutable(self) -> None:
        class DerivedAlias(UnitAlias):
            pass

        class DerivedRegistry(UnitRegistry):
            pass

        with self.assertRaisesRegex(RegistryError, "exact Unit"):
            UnitRegistry(
                "1.0.0",
                (object(),),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(RegistryError, "exact UnitAlias"):
            DerivedAlias("metre", "meter")
        with self.assertRaisesRegex(RegistryError, "exact UnitRegistry"):
            DerivedRegistry("1.0.0", (meter(),))

        registry = small_registry()
        with self.assertRaises(FrozenInstanceError):
            registry.version = "2.0.0"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            registry.aliases[0].unit_id = "kilometer"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
