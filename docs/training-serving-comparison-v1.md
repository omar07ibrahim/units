# Training-serving comparison contract v1

Status: the canonical comparison plan and its bounded byte codec are the first
implemented slice. Fresh verification, interface comparison, normalization
lineage, CLI, and evidence are added in later commits on the comparison branch.

## Objective

A training graph and a serving graph can each be dimensionally verified while
their public contracts still disagree. Version 1 defines an explicit,
content-addressed plan for aligning those interfaces without guessing from
names, positions, tensor values, or statistical samples.

The plan is not a compatibility result. It binds the exact graph and registry
bytes that a later comparison must verify and names every intended public
contract slot.

## Closed plan model

`ComparisonPlan` contains:

| Field | Contract |
| --- | --- |
| `comparison_id` | Canonical lowercase identifier for this comparison |
| `training_graph_digest` | SHA-256 of one canonical training graph |
| `serving_graph_digest` | SHA-256 of one canonical serving graph |
| `registry_digest` | SHA-256 of the one registry snapshot used on both sides |
| `bindings` | One to 256 sorted, unique logical contract bindings |

The serialized schema is
`unitsentinel.training-serving-comparison/v1`. The plan digest is the SHA-256
of its canonical bytes and is deliberately not embedded in those bytes.

Each `ContractBinding` has one canonical `contract_id` and two nullable sides:

```json
{
  "contract_id": "current-wheel-speed",
  "serving": {
    "role": "input",
    "value_id": "request-speed"
  },
  "training": {
    "role": "input",
    "value_id": "wheel-speed-feature"
  }
}
```

An endpoint names an intended public occurrence `(role, value_id)`, where
`role` is `input` or `output`. Constructing or decoding the plan does not prove
that the value exists, that the declared role is public, or that every public
occurrence is covered. The later engine must establish all three properties
from the bound graphs and reject internal values.

## Explicit alignment only

The plan never performs fuzzy, positional, alias-based, or embedding-based
matching. Different graph identifiers may be aligned only through one explicit
logical binding.

A two-sided binding declares different `value_id` spellings to be an
intentional rename only under that caller-trusted plan; the rename alone is not
drift. The plan itself grants no permission. The later result is therefore
always scoped as “compatible under this plan”, binds the exact plan SHA-256,
and reports `authentication: not-provided`. Port order remains independent
metadata and is compared separately.

The invariants are:

- bindings are sorted and unique by `contract_id`;
- the same endpoint occurrence cannot appear twice on one side;
- at least one side of every binding is present;
- a two-sided binding may retain different roles so the engine can report an
  explicit role drift rather than silently repairing it;
- a training-only binding declares a serving-side absence;
- a serving-only binding declares a training-side absence.

The comparison engine will additionally establish endpoint membership and
require every public input and output occurrence from both bound graphs to
appear exactly once. An omitted occurrence is an incomplete plan, not
permission to ignore part of an interface.

This representation distinguishes three cases that a same-name join cannot:

1. an intentional rename with otherwise matching semantics;
2. a public value missing on one side;
3. a value moved from input to output or the reverse.

## Byte boundary

The decoder accepts exact canonical UTF-8 JSON bytes and reuses UnitSentinel's
allocation preflight. Version 1 fixes:

- 131,072 document bytes;
- six nested containers;
- 256 entries in one object or array;
- 4,096 total JSON values;
- 192 Unicode scalar values in one string;
- ten digits in an integer token.

Duplicate fields, floats, non-finite numbers, a BOM, invalid UTF-8, unknown or
missing fields, noncanonical ordering or whitespace, oversized structures, and
unsupported schemas fail closed. Decoding reconstructs the immutable model and
then requires its canonical bytes to equal the input byte for byte.

## Required comparison ordering

The later engine must evaluate the plan in this order:

1. validate exact plan, graph, registry, limit, and caller policy object types;
2. when policy supplies an expected plan digest, require it to match before
   interpreting any binding;
3. require both graph digests and the registry digest to match the plan;
4. establish endpoint membership and require total, duplicate-free coverage of
   every public occurrence;
5. freshly run the existing verifier for both graphs with the same registry
   snapshot and bounded solver policy;
6. compare no interfaces unless both results are complete `verified` results;
7. compare each aligned endpoint's role, dtype, declared shape, explicit unit
   identifier, dimension, quantity kind, exact scale, and exact offset;
8. compare the separately bounded normalization-lineage evidence;
9. revalidate all nested objects and identities before publishing a result;
10. emit `authentication: not-provided` and the exact plan digest in every
    report.

A conflict, underconstrained graph, unknown verification, timeout, resource
limit, stale digest, incomplete contract coverage, unexpected verifier return,
or nested mutation can never become a positive compatibility result.

## Planned lineage boundary

Version 1 normalization provenance is intentionally narrower than arbitrary
graph equivalence. It will trace dimensionless ratio-normalization sites
created by `divide` operations.

The lineage fingerprint will:

- use mapped logical contract IDs for public input roots;
- include closed operation attributes and exact inferred contracts;
- collapse identity nodes;
- sort children only for operations that are actually commutative;
- preserve order for subtract, divide, and matrix multiplication;
- ignore internal node/value renames;
- bind every canonical record by SHA-256.

The output can establish that the normalization lineage changed. It does not
establish that training is correct, serving is wrong, or two different
algebraic forms are mathematically equivalent.

## Security and nonclaims

The plan contains no tensor payloads, model code, import paths, URLs, plugins,
callbacks, credentials, or executable metadata. Comparing it requires no
network and does not run a model.

The plan and later report are unsigned content-addressed records. Their digests
detect byte changes; they do not authenticate an author, prove that a
deployment used either graph, validate broadcasting or matrix shapes, measure
statistical drift, or establish scientific correctness.

A party that needs an approved mapping must supply a caller-trusted expected
plan digest or equivalent allow-list policy to the later engine and must reject
a mismatch. Recomputing a digest after replacing a plan establishes only the
integrity of the replacement; it does not make the replacement trusted.
