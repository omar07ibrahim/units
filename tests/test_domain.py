from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction

from unitsentinel.domain import (
    AMOUNT_OF_SUBSTANCE,
    BASE_DIMENSIONS,
    DIMENSIONLESS,
    ELECTRIC_CURRENT,
    LENGTH,
    LUMINOUS_INTENSITY,
    MASS,
    MAX_EXPONENT_DENOMINATOR,
    MAX_EXPONENT_NUMERATOR,
    THERMODYNAMIC_TEMPERATURE,
    TIME,
    BaseDimension,
    ConversionError,
    Dimension,
    DimensionError,
    Quantity,
    QuantityKind,
    Unit,
    UnitDefinitionError,
)


def meter() -> Unit:
    return Unit("meter", "m", LENGTH, Fraction(1))


def kilometer() -> Unit:
    return Unit("kilometer", "km", LENGTH, Fraction(1_000))


def kelvin() -> Unit:
    return Unit(
        "kelvin",
        "K",
        THERMODYNAMIC_TEMPERATURE,
        Fraction(1),
        kind=QuantityKind.ABSOLUTE_TEMPERATURE,
    )


def celsius() -> Unit:
    return Unit(
        "degree-celsius",
        "°C",
        THERMODYNAMIC_TEMPERATURE,
        Fraction(1),
        Fraction(27_315, 100),
        QuantityKind.ABSOLUTE_TEMPERATURE,
    )


def fahrenheit() -> Unit:
    return Unit(
        "degree-fahrenheit",
        "°F",
        THERMODYNAMIC_TEMPERATURE,
        Fraction(5, 9),
        Fraction(45_967, 180),
        QuantityKind.ABSOLUTE_TEMPERATURE,
    )


def celsius_delta() -> Unit:
    return Unit(
        "delta-celsius",
        "Δ°C",
        THERMODYNAMIC_TEMPERATURE,
        Fraction(1),
        kind=QuantityKind.TEMPERATURE_DELTA,
    )


def fahrenheit_delta() -> Unit:
    return Unit(
        "delta-fahrenheit",
        "Δ°F",
        THERMODYNAMIC_TEMPERATURE,
        Fraction(5, 9),
        kind=QuantityKind.TEMPERATURE_DELTA,
    )


class DimensionTests(unittest.TestCase):
    def test_base_dimension_order_is_frozen(self) -> None:
        self.assertEqual(
            tuple(base.value for base in BASE_DIMENSIONS),
            (
                "length",
                "mass",
                "time",
                "electric-current",
                "thermodynamic-temperature",
                "amount-of-substance",
                "luminous-intensity",
            ),
        )
        self.assertEqual(
            (
                LENGTH,
                MASS,
                TIME,
                ELECTRIC_CURRENT,
                THERMODYNAMIC_TEMPERATURE,
                AMOUNT_OF_SUBSTANCE,
                LUMINOUS_INTENSITY,
            ),
            tuple(Dimension.base(base) for base in BASE_DIMENSIONS),
        )

    def test_exact_algebra_builds_velocity_acceleration_and_energy(self) -> None:
        velocity = LENGTH.divide(TIME)
        acceleration = velocity.divide(TIME)
        energy = MASS.multiply(acceleration).multiply(LENGTH)

        self.assertEqual(
            velocity.canonical_pairs(),
            (("length", "1"), ("time", "-1")),
        )
        self.assertEqual(
            acceleration.canonical_pairs(),
            (("length", "1"), ("time", "-2")),
        )
        self.assertEqual(
            energy.canonical_pairs(),
            (("length", "2"), ("mass", "1"), ("time", "-2")),
        )
        self.assertTrue(velocity.divide(velocity).is_dimensionless)
        self.assertEqual(DIMENSIONLESS.canonical_pairs(), ())

    def test_rational_power_is_exact_and_bounded(self) -> None:
        area = LENGTH.power(Fraction(2))
        self.assertEqual(area.power(Fraction(1, 2)), LENGTH)
        self.assertEqual(
            LENGTH.power(Fraction(1, 3)).canonical_pairs(),
            (("length", "1/3"),),
        )

        with self.assertRaisesRegex(DimensionError, "declared bounds"):
            LENGTH.power(Fraction(1, MAX_EXPONENT_DENOMINATOR + 1))
        with self.assertRaisesRegex(DimensionError, "declared bounds"):
            LENGTH.power(Fraction(MAX_EXPONENT_NUMERATOR + 1))
        with self.assertRaisesRegex(DimensionError, "declared bounds"):
            LENGTH.power(Fraction(MAX_EXPONENT_NUMERATOR)).multiply(LENGTH)
        with self.assertRaisesRegex(DimensionError, "declared bounds"):
            LENGTH.power(Fraction(1, 5)).multiply(LENGTH.power(Fraction(1, 7)))

    def test_mapping_is_closed_over_known_exact_bases(self) -> None:
        dimension = Dimension.from_mapping(
            {
                BaseDimension.LENGTH: Fraction(2),
                BaseDimension.TIME: Fraction(-1),
            }
        )
        self.assertEqual(
            dimension.canonical_pairs(),
            (("length", "2"), ("time", "-1")),
        )

        with self.assertRaisesRegex(DimensionError, "unknown base"):
            Dimension.from_mapping({"length": Fraction(1)})  # type: ignore[dict-item]
        with self.assertRaisesRegex(DimensionError, "exact Fraction"):
            Dimension.from_mapping({BaseDimension.LENGTH: 1})  # type: ignore[dict-item]
        with self.assertRaisesRegex(DimensionError, "Mapping"):
            Dimension.from_mapping(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(DimensionError, "too many entries"):
            Dimension.from_mapping({str(index): Fraction(1) for index in range(8)})  # type: ignore[arg-type]

    def test_malformed_dimensions_and_subclasses_fail_closed(self) -> None:
        class DerivedDimension(Dimension):
            pass

        with self.assertRaisesRegex(DimensionError, "exact Dimension"):
            DerivedDimension(DIMENSIONLESS.exponents)
        with self.assertRaisesRegex(DimensionError, "known SI dimension"):
            Dimension.base("length")  # type: ignore[arg-type]
        with self.assertRaisesRegex(DimensionError, "tuple"):
            Dimension([Fraction(0)] * 7)  # type: ignore[arg-type]
        with self.assertRaisesRegex(DimensionError, "seven"):
            Dimension((Fraction(0),) * 6)
        with self.assertRaisesRegex(DimensionError, "exact Fraction"):
            Dimension((0,) * 7)  # type: ignore[arg-type]
        with self.assertRaisesRegex(DimensionError, "exact Dimension"):
            LENGTH.multiply(object())  # type: ignore[arg-type]

    def test_dimensions_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            LENGTH.exponents = DIMENSIONLESS.exponents  # type: ignore[misc]

    def test_operations_revalidate_a_low_level_mutated_dimension(self) -> None:
        dimension = Dimension.base(BaseDimension.LENGTH)
        object.__setattr__(dimension, "exponents", (Fraction(0),) * 6)

        with self.assertRaisesRegex(DimensionError, "seven"):
            dimension.multiply(TIME)


class UnitTests(unittest.TestCase):
    def test_linear_scale_conversion_is_exact(self) -> None:
        distance = Quantity(Fraction(5, 4), kilometer())
        converted = distance.to(meter())

        self.assertEqual(converted.magnitude, Fraction(1_250))
        self.assertEqual(converted.unit.unit_id, "meter")
        self.assertEqual(
            converted.canonical_record(),
            {"magnitude": "1250", "unit_id": "meter"},
        )

    def test_absolute_temperature_conversions_are_exact(self) -> None:
        freezing = Quantity(Fraction(32), fahrenheit())

        self.assertEqual(freezing.to(celsius()).magnitude, Fraction(0))
        self.assertEqual(freezing.to(kelvin()).magnitude, Fraction(27_315, 100))
        self.assertEqual(
            Quantity(Fraction(100), celsius()).to(fahrenheit()).magnitude,
            Fraction(212),
        )

    def test_temperature_deltas_do_not_apply_absolute_offsets(self) -> None:
        delta = Quantity(Fraction(18), fahrenheit_delta())
        self.assertEqual(delta.to(celsius_delta()).magnitude, Fraction(10))
        self.assertEqual(
            Quantity(Fraction(10), celsius_delta()).to(fahrenheit_delta()).magnitude,
            Fraction(18),
        )

    def test_absolute_and_delta_temperatures_never_mix_implicitly(self) -> None:
        with self.assertRaisesRegex(ConversionError, "quantity kinds"):
            celsius().convert_value_to(Fraction(20), celsius_delta())
        with self.assertRaisesRegex(ConversionError, "quantity kinds"):
            celsius_delta().convert_value_to(Fraction(20), kelvin())

    def test_dimension_mismatch_is_rejected_before_arithmetic(self) -> None:
        second = Unit("second", "s", TIME, Fraction(1))
        with self.assertRaisesRegex(ConversionError, "dimensions"):
            meter().convert_value_to(Fraction(1), second)

    def test_unit_definition_rejects_unsafe_identifiers_and_symbols(self) -> None:
        cases = (
            ("Meter", "m", "identifier"),
            ("meter_2", "m", "identifier"),
            ("meter", " m", "symbol"),
            ("meter", "m\n", "symbol"),
            ("meter", "m\u200b", "control"),
            ("meter", "m\u0301", "canonical Unicode"),
            ("meter", "m\u2028s", "whitespace"),
            ("meter", 1, "must be text"),
        )
        for unit_id, symbol, message in cases:
            with (
                self.subTest(unit_id=unit_id, symbol=repr(symbol)),
                self.assertRaisesRegex(UnitDefinitionError, message),
            ):
                Unit(unit_id, symbol, LENGTH, Fraction(1))

    def test_unit_definition_enforces_scale_offset_and_kind_semantics(self) -> None:
        cases = (
            (
                ("meter", "m", LENGTH, Fraction(0)),
                "scale must be positive",
            ),
            (
                ("meter", "m", LENGTH, Fraction(1), Fraction(1)),
                "linear units cannot",
            ),
            (
                (
                    "ambiguous-kelvin",
                    "K?",
                    THERMODYNAMIC_TEMPERATURE,
                    Fraction(1),
                ),
                "explicit absolute or delta",
            ),
            (
                (
                    "absolute-meter",
                    "mA",
                    LENGTH,
                    Fraction(1),
                    Fraction(0),
                    QuantityKind.ABSOLUTE_TEMPERATURE,
                ),
                "require temperature",
            ),
            (
                (
                    "delta-celsius",
                    "Δ°C",
                    THERMODYNAMIC_TEMPERATURE,
                    Fraction(1),
                    Fraction(1),
                    QuantityKind.TEMPERATURE_DELTA,
                ),
                "cannot have an offset",
            ),
        )
        for arguments, message in cases:
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(
                    UnitDefinitionError,
                    message,
                ),
            ):
                Unit(*arguments)

    def test_float_and_integer_convenience_values_are_not_accepted(self) -> None:
        with self.assertRaisesRegex(UnitDefinitionError, "exact Fraction"):
            Unit("meter", "m", LENGTH, 1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ConversionError, "exact Fraction"):
            meter().convert_value_to(1.0, kilometer())  # type: ignore[arg-type]
        with self.assertRaisesRegex(UnitDefinitionError, "exact Fraction"):
            Quantity(1, meter())  # type: ignore[arg-type]
        with self.assertRaisesRegex(UnitDefinitionError, "exact Unit"):
            Quantity(Fraction(1), object())  # type: ignore[arg-type]

    def test_rational_size_and_exact_target_types_are_bounded(self) -> None:
        oversized = Fraction(1 << 256)

        with self.assertRaisesRegex(UnitDefinitionError, "size limit"):
            Unit("oversized", "x", LENGTH, oversized)
        with self.assertRaisesRegex(ConversionError, "exact Unit"):
            meter().convert_value_to(Fraction(1), object())  # type: ignore[arg-type]

    def test_canonical_unit_record_uses_rational_strings(self) -> None:
        self.assertEqual(
            fahrenheit().canonical_record(),
            {
                "dimension": [
                    {
                        "base": "thermodynamic-temperature",
                        "exponent": "1",
                    }
                ],
                "kind": "absolute-temperature",
                "offset": "45967/180",
                "scale": "5/9",
                "symbol": "°F",
                "unit_id": "degree-fahrenheit",
            },
        )

    def test_quantity_and_unit_are_immutable(self) -> None:
        quantity = Quantity(Fraction(1), meter())
        with self.assertRaises(FrozenInstanceError):
            quantity.magnitude = Fraction(2)  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            quantity.unit.scale = Fraction(2)  # type: ignore[misc]

    def test_public_operations_revalidate_low_level_mutation(self) -> None:
        corrupted_unit = meter()
        object.__setattr__(corrupted_unit, "kind", "linear")
        with self.assertRaisesRegex(UnitDefinitionError, "kind is unknown"):
            corrupted_unit.canonical_record()

        corrupted_quantity = Quantity(Fraction(1), meter())
        object.__setattr__(corrupted_quantity, "magnitude", 1)
        with self.assertRaisesRegex(UnitDefinitionError, "exact Fraction"):
            corrupted_quantity.to(kilometer())

    def test_domain_value_subclasses_fail_closed_as_receivers(self) -> None:
        class DerivedUnit(Unit):
            pass

        class DerivedQuantity(Quantity):
            pass

        with self.assertRaisesRegex(UnitDefinitionError, "unit must be an exact Unit"):
            DerivedUnit("meter", "m", LENGTH, Fraction(1))
        with self.assertRaisesRegex(
            UnitDefinitionError,
            "quantity must be an exact Quantity",
        ):
            DerivedQuantity(Fraction(1), meter())


if __name__ == "__main__":
    unittest.main()
