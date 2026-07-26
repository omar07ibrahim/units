# Architecture and verification boundary

This document defines the contract UnitSentinel will implement. Statements
about future components are design requirements, not claims of completed
behavior.

## Design objective

Given a bounded computation graph, a versioned unit registry, and optional unit
annotations, determine one of four outcomes:

1. `verified`: every value has a unique, consistent dimensional interpretation;
2. `underconstrained`: more than one interpretation remains;
3. `conflict`: a tracked subset of declarations and operations is inconsistent;
4. `unknown`: the solver or resource boundary could not establish a result.

Only `verified` may produce a positive proof certificate. A timeout, unsupported
operation, malformed graph, or non-unique interpretation is never converted
into success.

## Quantity model

### Dimension

A dimension is a seven-component vector ordered as:

```text
length, mass, time, electric-current,
thermodynamic-temperature, amount-of-substance, luminous-intensity
```

Each component is an exact rational exponent. Multiplication adds vectors,
division subtracts them, and exponentiation multiplies every component by the
same bounded rational. Equality is exact; floating-point tolerances do not
participate in type checking.

### Unit

A unit contains:

- a canonical identifier;
- one dimension vector;
- an exact rational scale relative to a canonical reference unit;
- an exact rational offset when the conversion is affine;
- a quantity kind that distinguishes linear values, absolute temperatures, and
  temperature differences.

An affine unit is not generally closed under multiplication, division, or
exponentiation. An absolute Celsius magnitude may be normalized onto the
canonical absolute-temperature reference scale, but that conversion does not
reclassify it as a linear quantity. Direct temperature units must declare
whether they represent an absolute value or a difference; a temperature
difference has no absolute offset.

### Tensor value

The core verifier reasons about a value contract, not tensor payload bytes:

- scalar element type;
- optional symbolic shape;
- dimension or dimension variable;
- optional concrete unit;
- semantic quantity kind;
- source declaration identity.

Shape and dimensional inference are separate. A dimensionally valid graph can
still have an invalid tensor shape, and vice versa.

## Canonical graph format

The first adapter will accept strict JSON with:

- a schema version;
- a bounded, unique list of graph inputs;
- a bounded, unique topological list of nodes;
- declared outputs;
- explicit operation names and closed parameter objects;
- optional unit declarations tied to values;
- no executable code, import path, URL, or extension hook.

Parsing rejects duplicate JSON keys, unknown fields, invalid UTF-8, noncanonical
numbers, cycles, forward references, excessive nesting, and values outside
declared limits. Source locations are graph identifiers and JSON pointers, not
host filesystem paths.

## Constraint compilation

Each value receives seven exact solver expressions, one per base dimension.
Operations emit labelled equations:

| Operation family | Dimensional rule |
| --- | --- |
| identity, cast, reshape, transpose, reduce | output equals input |
| add, subtract, minimum, maximum | operands and output are equal |
| multiply | output equals left plus right |
| divide | output equals left minus right |
| power by rational constant | output equals input multiplied by exponent |
| exponential, logarithm, sigmoid, softmax | input and output are dimensionless |
| matrix multiplication | output equals left plus right |
| explicit conversion | dimensions equal; unit transform is declared |

Every equation is tracked by a stable constraint identifier derived from its
graph declaration. Solver-generated names never appear in user-facing output.

### Inference

A satisfiable system is not automatically verified. UnitSentinel will ask
whether each unresolved dimension component has a unique value. If two models
assign different values to an observable contract, the graph is
`underconstrained`.

Extracted rational values are validated against exponent numerator and
denominator bounds before they enter the trusted domain model.

### Conflict cores

Tracked assertions provide an initial unsatisfiable core. Because solver cores
need not be minimal, UnitSentinel will deterministically shrink the core within
a fixed check budget. The resulting core is a diagnostic witness, not a proof
that every omitted declaration is irrelevant to scientific intent.

## Bounded repair

Repair generation is downstream of verification and never mutates input. The
first repair operators are deliberately small:

- replace one declared unit with another compatible registry unit;
- insert one explicit scale conversion;
- reinterpret an absolute temperature input as a declared delta only when the
  graph contract permits it;
- remove one contradictory optional annotation.

Candidates are ordered by a deterministic cost tuple and recompiled through the
same verifier. A candidate that is not independently `verified` is discarded.
Ambiguous top candidates cause abstention.

An optional learned reranker may later order already-verified candidates. It
will not create candidates, bypass the verifier, or turn abstention into an
automatic edit.

## Proof certificate

A canonical certificate will bind:

- graph schema version and SHA-256;
- unit registry version and SHA-256;
- verifier and solver versions;
- configured resource limits;
- ordered inferred contracts;
- ordered constraint identities;
- outcome and, where applicable, conflict core or repair identity;
- certificate schema version and whole-document digest.

Offline replay reparses the graph and registry, recompiles constraints, and
compares a fresh result. A certificate is evidence of one bounded verifier
execution, not a cryptographic signature or a proof of scientific validity.

## Training and serving comparison

Two individually verified graphs may still disagree. Contract comparison will
align declared public inputs and outputs, then report:

- missing or extra values;
- dimension changes;
- compatible dimension but different scale or offset;
- quantity-kind changes;
- normalization provenance changes;
- underconstrained values on either side.

Statistical drift tools remain complementary: UnitSentinel finds semantic
contract skew even when no representative payload samples are available.

## ONNX adapter boundary

ONNX describes tensor element types and static or symbolic shapes. Its metadata
can carry strings, but UnitSentinel will not treat arbitrary metadata as trusted
unit semantics. The adapter will:

1. validate the model with the official ONNX checker;
2. import only a closed operator subset;
3. read a versioned UnitSentinel contract namespace;
4. lower into the same canonical core graph;
5. reject unsupported control flow or operator semantics rather than guessing.

The adapter is not part of the first implementation slice.

## Resource and security limits

- No network access is required by verification or replay.
- No shell, `eval`, dynamic import, or arbitrary model execution.
- Fixed limits for document bytes, nodes, edges, exponent size, core-shrink
  checks, repair candidates, solver memory, and solver time.
- Deterministic errors omit host paths, environment values, and raw solver
  diagnostics.
- Unknown unit identifiers and unsupported operations fail closed.
- Canonical JSON readers reject duplicate fields and non-finite numbers.
- The same registry snapshot is used for compile, repair, certificate, and
  replay.

The threat model excludes a same-UID process that can rewrite the verifier,
solver binary, or committed inputs during execution. Reproducible evidence
tooling will state that boundary explicitly.

## Current implementation map

| Area | Current | Next |
| --- | --- | --- |
| Package boundary | Python package metadata and public version | Exact domain value objects |
| Dimension semantics | Specified here | Exact algebra and property tests |
| Unit registry | Supported subset and exclusions specified | Versioned immutable registry |
| Graph IR | Closed JSON contract specified | Strict decoder and graph validation |
| Solver | Constraint and fail-closed behavior specified | Tracked exact constraints |
| Repairs | Bounded operators specified | Verified candidate enumeration |
| Certificates | Required bindings specified | Canonical codec and replay |
| Visual evidence | No placeholders | Generate from implemented behavior |
