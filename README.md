# UnitSentinel

Dimensional proof certificates for scientific and machine-learning computation
graphs.

> **Status:** active implementation. Exact dimensional values and a
> content-addressed 33-unit registry are implemented; the bounded graph format
> is the next slice. There are no solver, benchmark, ONNX-support, or model
> accuracy claims yet.

## The failure mode

Production ML contracts usually preserve tensor dtypes and shapes while the
physical meaning of a feature remains in preprocessing code, prose, or loose
metadata. That leaves several bugs structurally valid:

- a serving feature arrives in `km/h` while training consumed `m/s`;
- an absolute Celsius temperature is treated like a temperature difference;
- a normalization constant is copied from a sensor with a different scale;
- two tensors have the same shape and dtype but incompatible dimensions;
- an exported model keeps its numerical graph but loses the unit contract around
  its inputs and outputs.

[ONNX tensor types](https://onnx.ai/onnx/intro/concepts.html#supported-types)
describe element types and shapes. [TensorFlow Data
Validation](https://www.tensorflow.org/tfx/guide/tfdv) checks schema properties
and statistical skew. UnitSentinel targets a different layer: exact physical
dimension and scale semantics across the computation itself.

## What this project is

UnitSentinel treats a feature or model graph as a typed program:

```text
canonical graph + versioned unit registry
                    │
                    ▼
           typed quantity IR
                    │
                    ▼
       exact dimensional constraints
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
    verified graph      tracked conflict
          │                   │
          ▼                   ▼
 proof certificate    bounded repair candidates
```

The complete verifier is intended to:

- represent SI dimensions with exact rational exponents;
- keep scale, offset, and absolute-versus-delta temperature semantics explicit;
- compile graph operations into source-labelled constraints;
- infer omitted dimensions when the system has a unique solution;
- return a small, deterministic conflict core when constraints are inconsistent;
- suggest bounded edits without silently changing a graph;
- issue canonical JSON certificates that can be replayed offline;
- compare training and serving graph contracts before values reach a model;
- add an ONNX adapter only after the core semantics are independently testable.

## What runs today

The current package already provides:

- immutable vectors over the seven SI base dimensions;
- exact rational multiplication, division, and bounded powers;
- exact unit scales and affine offsets without float coercion;
- separate types for absolute temperatures and temperature differences;
- a bounded immutable registry with canonical ASCII identifiers;
- explicit aliases that resolve only to canonical identifiers;
- canonical UTF-8 JSON and a pinned SHA-256 registry fingerprint;
- nested revalidation that detects low-level mutation before lookup or
  serialization.

This is a real conversion through the committed registry:

```python
from fractions import Fraction

from unitsentinel import BUILTIN_REGISTRY, Quantity

speed = Quantity(
    Fraction(90),
    BUILTIN_REGISTRY.resolve("kilometer-per-hour"),
)
converted = speed.to(BUILTIN_REGISTRY.resolve("meter-per-second"))

assert converted.magnitude == Fraction(25)
assert BUILTIN_REGISTRY.digest == (
    "fc80cbb596f3341b1d2ff13795e50d2d1e05c792b34f24804afc97c3470913e5"
)
```

Symbols such as `m/s` and `°C` are display metadata, not implicit lookup keys.
Contracts use canonical identifiers or one of the registry's explicit ASCII
aliases. The full snapshot and versioning rules are documented in
[docs/registry.md](docs/registry.md).

## Why a solver belongs here

Local arithmetic catches obvious errors when every intermediate unit is known.
Real graphs are partially annotated. A constraint solver can infer missing
dimensions and explain a contradiction in terms of the declarations and
operations that caused it. The solver is not the trust boundary by itself:
UnitSentinel will revalidate every extracted model, cap graph and solver
resources, and fail closed on `unknown` or timeout.

## Non-goals for v1

- Full UCUM compatibility.
- Currency, calendar arithmetic, logarithmic units, or context-dependent
  chemistry conversions.
- Executing user-provided Python, model code, or arbitrary plugins.
- Guessing scientific intent from names.
- Applying an LLM-generated repair without formal re-verification.
- Claiming that dimensional consistency proves scientific correctness.

## Development slices

1. **Complete:** exact dimensions, units, quantities, and affine conversion
   semantics.
2. **Complete:** a bounded immutable unit registry with canonical serialization
   and a pinned content digest.
3. A bounded canonical graph format and typed intermediate representation.
4. Tracked SMT constraints, inference, deterministic conflict cores, and
   fail-closed resource handling.
5. Bounded repairs, canonical proof certificates, and independent replay.
6. Training/serving contract comparison and an ONNX metadata adapter.
7. A grouped synthetic fault benchmark with calibration and abstention metrics.
8. Real CLI captures, architecture and lineage diagrams, benchmark plots, and a
   short end-to-end demo generated from committed code.

Visual evidence will be added only when it can be reproduced from implemented
behavior. No mock dashboard or invented result is used as a placeholder.

## Local quality checks

The bootstrap package uses Python 3.11+ and keeps development tooling optional:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -s tests
.venv/bin/coverage report
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m build
.venv/bin/pip-audit
```

The repository intentionally has no license yet. Licensing is a decision for
Omar before third-party reuse is invited.
