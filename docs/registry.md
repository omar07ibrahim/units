# Built-in registry contract

UnitSentinel's registry is a versioned input to verification, not a mutable
process-wide table. A graph certificate will bind the exact registry bytes it
was checked against.

## Snapshot identity

| Field | Value |
| --- | --- |
| Schema | `unitsentinel.unit-registry/v1` |
| Registry version | `1.0.0` |
| Canonical units | 33 |
| Explicit aliases | 10 |
| SHA-256 | `fc80cbb596f3341b1d2ff13795e50d2d1e05c792b34f24804afc97c3470913e5` |

The digest covers the schema, registry version, ordered unit records, aliases,
dimension exponents, quantity kinds, scales, offsets, identifiers, and display
symbols. It is computed over compact, key-sorted UTF-8 JSON with no BOM or
trailing newline. The digest itself is not part of the hashed record.

Changing any unit or alias requires a new registry version and produces a new
digest. Reusing `1.0.0` for different bytes would violate the registry contract.

## Validation invariants

A registry snapshot is accepted only when:

- its version is canonical three-component SemVer;
- units and aliases are exact immutable tuples within declared size limits;
- every entry is an exact, currently valid domain value;
- canonical unit identifiers and display symbols are unique;
- units and aliases are sorted by identifier;
- every alias is unique, does not collide with a canonical unit, and points
  directly to a canonical unit in the same snapshot;
- the freshly recomputed digest matches the digest captured at construction.

Lookup repeats the complete validation. This catches a nested `Unit` or alias
that was changed through low-level Python mutation after construction. The
registry deliberately does not case-fold, guess plurals, synthesize SI
prefixes, follow alias chains, or resolve display symbols.

## Curated surface

| Family | Canonical unit identifiers |
| --- | --- |
| Dimensionless | `one`, `percent` |
| Length | `meter`, `kilometer`, `centimeter`, `millimeter`, `micrometer` |
| Mass | `kilogram`, `gram`, `milligram` |
| Time | `second`, `millisecond`, `minute`, `hour` |
| Other SI bases | `ampere`, `mole`, `candela` |
| Absolute temperature | `kelvin`, `degree-celsius`, `degree-fahrenheit` |
| Temperature differences | `delta-kelvin`, `delta-celsius`, `delta-fahrenheit` |
| Kinematics | `meter-per-second`, `kilometer-per-hour`, `meter-per-second-squared` |
| Derived engineering units | `coulomb`, `hertz`, `newton`, `pascal`, `joule`, `watt`, `volt` |

The explicit aliases are:

```text
celsius           -> degree-celsius
celsius-delta     -> delta-celsius
centimetre        -> centimeter
fahrenheit        -> degree-fahrenheit
fahrenheit-delta  -> delta-fahrenheit
kelvin-delta      -> delta-kelvin
kilometre         -> kilometer
metre             -> meter
micrometre        -> micrometer
millimetre        -> millimeter
```

Equivalent transforms are allowed when their semantics remain explicit.
`delta-kelvin` and `delta-celsius`, for example, intentionally have the same
dimension, scale, and offset but distinct public identifiers and symbols.

## Deliberate exclusions

The first snapshot omits units whose scientific meaning would collapse under
the current `Dimension` and `QuantityKind` model:

- angle degrees or radians versus a plain dimensionless ratio;
- becquerel versus hertz;
- gray versus sievert;
- torque units versus energy units.

Adding those names before the type system can preserve their semantic
distinction would create a registry that is numerically convenient but unsafe
for proof certificates.

## Reproducing the fingerprint

From a clean checkout with the package installed:

```bash
PYTHONPATH=src .venv/bin/python -c \
  'from unitsentinel import BUILTIN_REGISTRY; print(BUILTIN_REGISTRY.digest)'
```

The golden digest and the full canonical record are also exercised by
`tests/test_registry.py`.
